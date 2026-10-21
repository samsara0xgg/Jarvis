"""ADR 0259: the exports he hands over, kept raw and read into one table of dated entries.

Two stores, both append-only:

- the raw archive, ``<root>/imports/<source>/<sha256[:16]>-<original filename>``: the file byte for
  byte, written whole or not at all, never stored twice. Nothing sweeps it, clears it or exports
  it (the directory is simply not one of the media directories); erasing all data removes it with
  the runtime root;
- ``import_entries`` in memory.db: one row per payment, message, memory or reflection, unique on
  ``(source, key)``. A row already held is not added again; a held row whose content differs is
  kept as it was and the names of what differs are logged (never the values). Nothing is updated
  or deleted. What a terminal copies on a schedule (ADR 0294) is the same table in
  ``<root>/imports/shipped.db`` instead, read with :func:`newest`.

Parsing is :mod:`jarvis.state.import_parsers`; reading is :func:`entries`, :func:`entry` and
:func:`around`.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final
from xml.etree import ElementTree as ET

from jarvis.state import import_parsers

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from jarvis.state.import_parsers import Parsed

LOGGER = logging.getLogger(__name__)

IMPORTS_DIRNAME: Final = "imports"
"""Under the runtime root."""
SHIPPED_SOURCES: Final = ("timesink", "claude-code", "device-apps", "device-records")
"""Sources a terminal copies in on a schedule (ADR 0294), not exports he hands over. Their rows
are revisions (the key ends in ``:<12 hex of the row's content>``), so a reader takes
:func:`newest`. They live in their own database, :data:`SHIPPED_DB` under the imports directory,
never in memory.db: a few hundred megabytes of screen text must not slow the database every
turn reads."""
DEVICE_SOURCE: Final = "device-apps"
"""His iPhone and iPad (ADR 0304): ``DEVICE_APP`` is one stretch of one app, ``DEVICE_HORIZON`` the
time of a device's newest record the Mac holds, ``DEVICE_NAME`` what a bundle id is called."""
DEVICE_APP: Final = "device_app"
DEVICE_HORIZON: Final = "device_horizon"
DEVICE_NAME: Final = "app_name"
RECORDS_SOURCE: Final = "device-records"
"""The other streams his iPhone and iPad sync to the Mac (ADR 0305): one row per record, kinds
``RECORD_NOW_PLAYING``, ``RECORD_BLUETOOTH``, ``RECORD_MESSAGE``, ``RECORD_CALL`` (who and when, no
words) and ``RECORD_INTENT`` (the other things Siri remembers). The Mac's own databases (ADR 0309)
add ``RECORD_PHOTO`` (one asset of the Photos library, metadata only), ``RECORD_CHAT_MESSAGE`` (one
message of chat.db, with its words) and ``RECORD_SAFARI_VISIT``; unlike the kinds above, their keys
carry the record's own id, so a record that changes later is a new revision of the same row."""
RECORD_NOW_PLAYING: Final = "now_playing"
RECORD_BLUETOOTH: Final = "bluetooth"
RECORD_MESSAGE: Final = "message"
RECORD_CALL: Final = "call"
RECORD_INTENT: Final = "intent"
RECORD_PHOTO: Final = "photo"
RECORD_CHAT_MESSAGE: Final = "chat_message"
RECORD_SAFARI_VISIT: Final = "safari_visit"
SHIPPED_DB: Final = "shipped.db"
"""Under the imports directory; the same ``import_entries`` table as memory.db's."""
IMPORT_ENTRIES_DDL: Final = """CREATE TABLE IF NOT EXISTS import_entries (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    file_digest TEXT NOT NULL,
    pos         TEXT NOT NULL,
    at_ms       INTEGER NOT NULL,
    zone        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    fields      TEXT NOT NULL,
    UNIQUE (source, key)
);
CREATE INDEX IF NOT EXISTS import_entries_at ON import_entries (at_ms);
"""
"""The one definition of the table, run by memory.db's schema and by :func:`open_shipped`."""
REVISION_SUFFIX: Final = 13
"""``:`` and the 12 hex digits that end a shipped key; what comes before is the source row."""


MASK: Final = "[secret removed]"
_ZIP_EPOCH_MS: Final = 946_684_800_000
"""2000-01-01: a file time before it is an archive's default (1980), not when the file was made."""
_SECRET_SHAPES: Final = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abp]-[A-Za-z0-9-]{10,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
    re.DOTALL,
)
_NAMED_TOKEN: Final = re.compile(
    r"(?i)(\b(?:api[_-]?key|key|token|secret|password)\b[\"']?\s*[:=]?\s*[\"']?)"
    r"([A-Za-z0-9_+/=-]{32,})",
)


def mask_secrets(text: str) -> str:
    """``text`` with secret-looking strings replaced by ``[secret removed]``.

    Known key shapes, PEM private keys, and a 32+ character mixed letter-and-digit token that
    follows a word like key, token, secret or password. The raw file is never masked.
    """
    def named(found: re.Match[str]) -> str:
        token = found.group(2)
        if token.isalpha() or token.isdigit():
            return found.group(0)  # a long plain word or number is not a token
        return found.group(1) + MASK

    return _NAMED_TOKEN.sub(named, _SECRET_SHAPES.sub(MASK, text))


def _masked(value: object) -> Any:  # noqa: ANN401 - mirrors the JSON shape it is given
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [_masked(one) for one in value]
    if isinstance(value, dict):
        return {key: _masked(one) for key, one in value.items()}
    return value


@dataclass(frozen=True)
class Entry:
    """One row of ``import_entries``; ``fields`` is the source's own, decoded."""

    id: str
    source: str
    file_digest: str
    pos: str
    at_ms: int
    zone: str
    kind: str
    text: str
    fields: dict[str, Any]


@dataclass
class Counts:
    """What an import did; never any content."""

    files_stored: int = 0
    files_skipped: int = 0
    added: int = 0
    held: int = 0
    conflicting: int = 0
    unreadable: int = 0


def open_shipped(directory: Path) -> sqlite3.Connection:
    """The shipped sources' database under the imports ``directory``, created on first use."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / SHIPPED_DB, timeout=5.0)
    conn.executescript(IMPORT_ENTRIES_DDL)
    return conn


def entry_id(source: str, key: str) -> str:
    """The id a model is given for an entry: stable, short, and free of the source's own key."""
    return f"{source}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def keep_raw(directory: Path, source: str, name: str, data: bytes) -> tuple[str, bool]:
    """Keep ``data`` under ``directory/<source>/``; ``(sha256, created)``, False if already held."""
    digest = hashlib.sha256(data).hexdigest()
    folder = directory / source
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(folder.glob(f"{digest[:16]}-*")):
        return digest, False
    kept = f"{digest[:16]}-{name.replace(os.sep, '_')}"
    partial = folder / f".{kept}.{uuid.uuid4().hex}.part"
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        partial.replace(folder / kept)
    finally:
        partial.unlink(missing_ok=True)
    return digest, True


def add_entries(
    conn: sqlite3.Connection, file_digest: str, parsed: Iterable[Parsed], counts: Counts,
) -> None:
    """Add what is not held; count what is, and what is held but reads differently."""
    for one in parsed:
        one = replace(one, text=mask_secrets(one.text), fields=_masked(one.fields))  # noqa: PLW2901
        row = (
            entry_id(one.source, one.key), one.source, one.key, file_digest, one.pos, one.at_ms,
            one.zone, one.kind, one.text,
            json.dumps(one.fields, ensure_ascii=False, sort_keys=True),
        )
        added = conn.execute(
            "INSERT OR IGNORE INTO import_entries "
            "(id, source, key, file_digest, pos, at_ms, zone, kind, text, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        ).rowcount
        if added:
            counts.added += 1
            continue
        counts.held += 1
        held = conn.execute(
            "SELECT at_ms, zone, kind, text, fields FROM import_entries WHERE id = ?", (row[0],),
        ).fetchone()
        differs = [
            name for name, was, now in zip(
                ("at_ms", "zone", "kind", "text", "fields"), held, row[5:], strict=True,
            ) if was != now
        ]
        if differs:
            counts.conflicting += 1
            LOGGER.warning(
                "imports: %s entry %s is held and differs in %s; the held one is kept",
                one.source, row[0], ", ".join(differs),
            )


SOURCES: Final = ("claude", *import_parsers.PARSERS)


def import_files(
    conn: sqlite3.Connection, directory: Path, source: str, paths: Iterable[Path],
) -> Counts:
    """Keep each file raw and read its entries into ``conn``; a directory is every file under it.

    Raises:
        ValueError: ``source`` has no parser yet.
    """
    if source not in SOURCES:
        msg = f"no parser for source {source!r}; the sources are {', '.join(SOURCES)}"
        raise ValueError(msg)
    counts = Counts()
    seen: set[str] = set()
    files = [
        found for path in paths
        for found in (sorted(path.rglob("*")) if path.is_dir() else [path])
        if found.is_file() and not found.name.startswith(".")
        and (source != "claude" or import_parsers.claude_keeps(found.name))
    ]
    for file in files:
        data = file.read_bytes()
        digest, created = keep_raw(directory, source, file.name, data)
        counts.files_stored += created
        counts.files_skipped += not created
        if digest in seen:
            continue  # the same bytes under another name: read once
        seen.add(digest)
        fallback = int(file.stat().st_mtime * 1000)
        if fallback < _ZIP_EPOCH_MS:  # unzipped without times: when he handed it over stands in
            fallback = int(time.time() * 1000)
        try:
            if source == "claude":
                parsed = list(import_parsers.claude(file.parent.name, file.name, data, fallback))
            else:
                parsed = list(import_parsers.PARSERS[source](file.name, data, fallback))
        except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            LOGGER.warning(
                "imports: %s file %s could not be read (%s)",
                source, digest[:16], type(exc).__name__,
            )
            counts.unreadable += 1
            continue
        add_entries(conn, digest, parsed, counts)
    conn.commit()
    return counts


def _entry(row: tuple[Any, ...]) -> Entry:
    rid, source, digest, pos, at_ms, zone, kind, text, fields = row
    if source == "claude":  # rows read before the stand-ins were dropped still hold them
        text = import_parsers.strip_placeholders(text)
    return Entry(rid, source, digest, pos, at_ms, zone, kind, text, json.loads(fields))


_COLUMNS: Final = "id, source, file_digest, pos, at_ms, zone, kind, text, fields"


def entries(
    conn: sqlite3.Connection, *, source: str | None = None,
    start_ms: int | None = None, end_ms: int | None = None, kinds: tuple[str, ...] = (),
) -> list[Entry]:
    """Entries of ``source`` in ``[start_ms, end_ms)``, newest first; none without the table."""
    clauses: list[str] = []
    params: list[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if start_ms is not None:
        clauses.append("at_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        clauses.append("at_ms < ?")
        params.append(end_ms)
    if kinds:
        clauses.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT {_COLUMNS} FROM import_entries {where} ORDER BY at_ms DESC, id"  # noqa: S608 - fixed clauses, bound values
    try:
        return [one for one in map(_entry, conn.execute(sql, params)) if one.text]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []  # a memory.db from before ADR 0259 and before the first import
        raise


def latest_daily(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, source: str = "claude_daily",
) -> list[Entry]:
    """The day entries of the Claude daily letters (ADR 0295) in ``[start_ms, end_ms)``, one a day.

    ``source`` is ``claude_daily`` (the letter on his chats) or ``claude_code_daily`` (the report
    on his Claude Code work, ADR 0303). A day written again is a new entry; the one added last
    stands for the day. Oldest day first.
    """
    sql = (
        f"SELECT {_COLUMNS} FROM import_entries "  # noqa: S608 - fixed clauses, bound values
        "WHERE source = ? AND kind = 'daily' AND at_ms >= ? AND at_ms < ? "
        "ORDER BY rowid"
    )
    try:
        rows = map(_entry, conn.execute(sql, (source, start_ms, end_ms)))
        days = {str(one.fields.get("date")): one for one in rows}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    return sorted((one for one in days.values() if one.text), key=lambda one: one.at_ms)


def entry(conn: sqlite3.Connection, entry_id_: str) -> Entry | None:
    """The entry with this id, or ``None``."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM import_entries WHERE id = ?", (entry_id_,),  # noqa: S608
    ).fetchone()
    return None if row is None else _entry(row)


def around(conn: sqlite3.Connection, one: Entry, count: int) -> tuple[list[Entry], list[Entry]]:
    """Up to ``count`` messages before and after ``one`` in its conversation, oldest first."""
    chat = one.fields.get("conversation")
    rows = [
        row for row in map(_entry, conn.execute(
            f"SELECT {_COLUMNS} FROM import_entries WHERE source = ? AND kind = 'message' "  # noqa: S608
            "AND json_extract(fields, '$.conversation') = ? ORDER BY at_ms, rowid",
            (one.source, chat),
        )) if row.text
    ]
    at = next((i for i, row in enumerate(rows) if row.id == one.id), len(rows))
    return rows[max(0, at - count) : at], rows[at + 1 : at + 1 + count]


def newest(  # noqa: PLR0913 — the source, a window, the kinds and how the scan is bounded.
    conn: sqlite3.Connection, source: str, *,
    start_ms: int | None = None, end_ms: int | None = None, kinds: tuple[str, ...] = (),
    by_prefix: bool = False,
) -> list[Entry]:
    """The newest revision of each row of a shipped ``source`` (ADR 0294), oldest moment first.

    ``conn`` is :func:`open_shipped`'s.

    A row is its key without the content hash; the revision kept last is the newest. Rows whose
    newest revision has no text are included. Finding the newest of every row reads every key of
    the source, hundreds of thousands for TimeSink; ``by_prefix`` limits that read to the keys
    that start ``<kind>:`` (what a terminal ships, ADR 0294) for the ``kinds`` asked, an index
    range instead of a scan, and gives the same rows when every such key does. With both ends
    of a window only the rows with a revision inside it are read, each newest found by its key:
    a row whose newest revision is in the window has one there.
    """
    row = f"substr(key, 1, length(key) - {REVISION_SUFFIX})"
    held = ["source = ?"]
    params: list[Any] = [source]
    if by_prefix and kinds:
        held.append(f"({' OR '.join('(key >= ? AND key < ?)' for _ in kinds)})")
        params.extend(bound for one in kinds for bound in (f"{one}:", f"{one};"))
    window: list[str] = []
    bounds: list[int] = []
    if start_ms is not None:
        window.append("at_ms >= ?")
        bounds.append(start_ms)
    if end_ms is not None:
        window.append("at_ms < ?")
        bounds.append(end_ms)
    if start_ms is not None and end_ms is not None:
        touched = (
            f"SELECT DISTINCT {row} AS r FROM import_entries INDEXED BY import_entries_at "  # noqa: S608 - constant expressions
            f"WHERE {' AND '.join(window + held)}"
        )
        newest_of_each = (
            "SELECT (SELECT max(rowid) FROM import_entries WHERE source = ? AND key >= r || ':' "  # noqa: S608 - constant expressions
            f"AND key < r || ';' AND length(key) = length(r) + {REVISION_SUFFIX}) FROM ({touched})"
        )
        params = [source, *bounds, *params]
    else:
        newest_of_each = (
            f"SELECT max(rowid) FROM import_entries WHERE {' AND '.join(held)} GROUP BY {row}"  # noqa: S608 - constant expressions
        )
    clauses = [f"rowid IN ({newest_of_each})", *window]
    params.extend(bounds)
    if kinds:
        clauses.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    sql = f"SELECT {_COLUMNS} FROM import_entries WHERE {' AND '.join(clauses)} ORDER BY at_ms, id"  # noqa: S608 - fixed clauses, bound values
    try:
        return [_entry(row) for row in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
