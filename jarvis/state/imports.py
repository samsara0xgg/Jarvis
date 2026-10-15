"""ADR 0259: the exports he hands over, kept raw and read into one table of dated entries.

Two stores, both append-only:

- the raw archive, ``<root>/imports/<source>/<sha256[:16]>-<original filename>``: the file byte for
  byte, written whole or not at all, never stored twice. Nothing sweeps it, clears it or exports
  it (the directory is simply not one of the media directories); erasing all data removes it with
  the runtime root;
- ``import_entries`` in memory.db: one row per payment, message, memory or reflection, unique on
  ``(source, key)``. A row already held is not added again; a held row whose content differs is
  kept as it was and the names of what differs are logged (never the values). Nothing is updated
  or deleted.

Parsing is :mod:`jarvis.state.import_parsers`; reading is :func:`entries`, :func:`entry` and
:func:`around`.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
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
        return [_entry(row) for row in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []  # a memory.db from before ADR 0259 and before the first import
        raise


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
        _entry(row) for row in conn.execute(
            f"SELECT {_COLUMNS} FROM import_entries WHERE source = ? AND kind = 'message' "  # noqa: S608
            "AND json_extract(fields, '$.conversation') = ? ORDER BY at_ms, rowid",
            (one.source, chat),
        )
    ]
    at = next((i for i, row in enumerate(rows) if row.id == one.id), len(rows))
    return rows[max(0, at - count) : at], rows[at + 1 : at + 1 + count]
