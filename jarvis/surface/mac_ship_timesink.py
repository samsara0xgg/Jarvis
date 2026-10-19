"""TimeSink's database and screenshots, copied to the brain as they are (ADR 0294).

Read-only, through :func:`jarvis.state.timesink.snapshot` (``mode=ro``, one read transaction).
Every table is mirrored from ``sqlite_master`` and ``PRAGMA table_info``, whole row with every
column, so a table or column TimeSink adds later is copied without a code change; only its
migration ledger is skipped.

Per poll: rows past the table's rowid cursor are new; rows with a time column in the last 48 h
are read again; once a day every row is. A row changes the brain's store only through
:func:`~jarvis.surface.mac_ship_core.decide`. Screenshots are shipped once each, keyed by their
``imagePath``, downscaled and re-encoded with macOS's own ``sips``; the entry keeps the original's
digest and size and the shipped file's.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from jarvis.state import timesink
from jarvis.state.shipments import RELPATH
from jarvis.surface.mac_ship_core import (
    HOUR_MS,
    SET_META,
    SET_ROW,
    Conflict,
    Op,
    Out,
    Report,
    ShipFailed,
    Sink,
    State,
    canonical,
    decide,
    digest,
)

LOGGER = logging.getLogger(__name__)

SOURCE: Final = "timesink"
SKIPPED_TABLES: Final = frozenset({"grdb_migrations"})
RECENT: Final = timedelta(hours=48)
SWEEP_MS: Final = 24 * HOUR_MS
_AT_COLUMNS: Final = ("start", "at", "windowStart", "hour", "createdAt", "updatedAt")
_TEXT_COLUMNS: Final = ("text", "title", "label", "name")
_IDENT_CHARS: Final = 200
_SIPS_S: Final = 30


@dataclass(frozen=True)
class TimesinkConfig:
    """What the mirror reads and how it sends pictures."""

    db_path: Path
    images: bool = True
    image_scale: float = 1.0
    image_quality: float = 1.0
    images_per_poll: int = 1000


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _jsonable(value: object) -> Any:  # noqa: ANN401 - a column value, whatever SQLite gives
    return {"$blob_b64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def utc_ms(text: object) -> int | None:
    """The UTC millisecond of a TimeSink ``DATETIME`` text like ``2026-10-19 05:01:47.282``."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000")


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name",
    )
    names = []
    for (name,) in rows.fetchall():
        if name in SKIPPED_TABLES:
            continue
        try:
            conn.execute(f"SELECT rowid FROM {_quote(name)} LIMIT 0")  # noqa: S608 - quoted schema name
        except sqlite3.OperationalError:
            # ponytail: a WITHOUT ROWID table has no cursor; TimeSink (GRDB) has none today
            LOGGER.warning("mac-ship: table %s has no rowid; not mirrored", name)
            continue
        names.append(name)
    return names


class _Table:
    """One table's shape, from the schema."""

    def __init__(self, conn: sqlite3.Connection, name: str) -> None:
        info = conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        self.name = name
        self.columns = [str(row[1]) for row in info]
        self.times = [str(row[1]) for row in info if str(row[2]).upper() == "DATETIME"]
        self.at = next((c for c in _AT_COLUMNS if c in self.times), None) or (
            self.times[0] if self.times else None
        )
        self.text = next((c for c in _TEXT_COLUMNS if c in self.columns), None)
        keys = sorted((row for row in info if row[5]), key=lambda row: row[5])
        self.pk = [str(row[1]) for row in keys]
        self.int_pk = len(keys) == 1 and str(keys[0][2]).upper() == "INTEGER"

    def ident(self, record: dict[str, Any], rowid: int) -> str:
        """The row's identity: its integer key, else its key columns, else its rowid."""
        if self.int_pk:
            return str(record[self.pk[0]])
        if not self.pk:
            return str(rowid)
        text = canonical([record[c] for c in self.pk])
        if len(text) <= _IDENT_CHARS:
            return text
        return "pk-" + hashlib.sha256(text.encode()).hexdigest()[:24]


def _entry_text(table: _Table, record: dict[str, Any]) -> str:
    for column in ([table.text] if table.text else []):
        value = record.get(column)
        if isinstance(value, str):
            return value
    return ""


def _mirror_table(  # noqa: PLR0913 - the table's whole pass: where, what, when
    conn: sqlite3.Connection, table: _Table, state: State, out: Out, now_ms: int, *, sweep: bool,
) -> None:
    cursor = state.meta_int(f"cursor:{table.name}")
    top = cursor
    select = f"SELECT rowid AS _rowid_, * FROM {_quote(table.name)}"  # noqa: S608 - quoted schema name
    cutoff = _stamp(datetime.fromtimestamp(now_ms / 1000, UTC) - RECENT)
    queries: list[tuple[str, tuple[Any, ...]]] = [
        (f"{select} WHERE rowid > ? ORDER BY rowid", (cursor,)),
    ]
    if sweep:
        queries.append((f"{select} WHERE rowid <= ? ORDER BY rowid", (cursor,)))
    elif table.times:
        recent = " OR ".join(f"{_quote(c)} >= ?" for c in table.times)
        queries.append((
            f"{select} WHERE rowid <= ? AND ({recent}) ORDER BY rowid",
            (cursor, *([cutoff] * len(table.times))),
        ))
    for number, (sql, params) in enumerate(queries):
        for row in conn.execute(sql, params):
            rowid = int(row[0])
            record = {
                name: _jsonable(value) for name, value in zip(table.columns, row[1:], strict=True)
            }
            if number == 0:
                top = max(top, rowid)
            ident = table.ident(record, rowid)
            sha = digest(record)
            stamps = [str(record[c]) for c in table.times if record[c] is not None]
            prev = state.row(table.name, ident)
            ship, after = decide(
                prev, sha, now_ms, settled=bool(stamps) and max(stamps) < cutoff,
                interval_ms=HOUR_MS,
            )
            keep = ship or prev != after
            ops: list[Op] = [(SET_ROW, (table.name, ident, *after))] if keep else []
            if not ship:
                out.note(ops)
                continue
            at_ms = utc_ms(record.get(table.at)) if table.at else None
            out.add({
                "key": f"{table.name}:{ident}:{sha}", "kind": table.name, "zone": "UTC",
                "at_ms": now_ms if at_ms is None else at_ms,
                "text": _entry_text(table, record), "fields": record,
            }, ops)
    if top != cursor:
        out.note([(SET_META, (f"cursor:{table.name}", str(top)))])


def _sips(*argv: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv; sips is macOS's own tool
        ["/usr/bin/sips", *argv], capture_output=True, text=True, timeout=_SIPS_S, check=True,
    )
    return done.stdout


def _size(path: Path) -> tuple[int, int]:
    """``(width, height)`` in pixels, from sips."""
    found = dict(re.findall(
        r"pixel(Width|Height): (\d+)", _sips("-g", "pixelWidth", "-g", "pixelHeight", str(path)),
    ))
    return int(found["Width"]), int(found["Height"])


def _scaled(path: Path, config: TimesinkConfig) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """The bytes to ship for ``path``, its pixel size and theirs.

    At scale and quality 1 the file itself is shipped.
    """
    width, height = _size(path)
    if config.image_scale >= 1 and config.image_quality >= 1:
        return path.read_bytes(), (width, height), (width, height)
    with tempfile.TemporaryDirectory(prefix="jarvis-ship-") as scratch:
        made = Path(scratch) / "shot.jpg"
        _sips(
            "--resampleHeightWidth", str(max(1, round(height * config.image_scale))),
            str(max(1, round(width * config.image_scale))),
            "-s", "format", "jpeg", "-s", "formatOptions", str(round(config.image_quality * 100)),
            str(path), "--out", str(made),
        )
        return made.read_bytes(), (width, height), _size(made)


def _ship_images(  # noqa: PLR0913
    conn: sqlite3.Connection, config: TimesinkConfig, state: State, out: Out, sink: Sink,
    report: Report, now_ms: int,
) -> None:
    have = {str(row[1]) for row in conn.execute('PRAGMA table_info("capture")')}
    if not {"id", "at", "imagePath"} <= have:
        return
    root = config.db_path.expanduser().parent / "captures"
    last = state.meta_int("cursor:images")
    rows = conn.execute(
        "SELECT id, at, imagePath FROM capture WHERE id > ? AND imagePath IS NOT NULL "
        "ORDER BY id LIMIT ?", (last, config.images_per_poll + 1),
    ).fetchall()
    backlog = len(rows) > config.images_per_poll
    for capture_id, at, rel in rows[: config.images_per_poll]:
        cursor_op: Op = (SET_META, ("cursor:images", str(capture_id)))
        original = root / str(rel)
        shot = None
        original_bytes = b""
        if RELPATH.fullmatch(str(rel)) and original.is_file():
            try:
                original_bytes = original.read_bytes()
                shot = _scaled(original, config)
            except (OSError, subprocess.SubprocessError, KeyError, ValueError):
                LOGGER.warning("mac-ship: capture %s could not be read or scaled", capture_id)
        if shot is None:
            report.skipped["timesink/image"] += 1  # gone, odd path, or unreadable: nothing to ship
            out.note([cursor_op])
            continue
        data, (original_width, original_height), (width, height) = shot
        try:
            kept = sink.send_file(SOURCE, str(rel), data)
        except Conflict:
            # ponytail: a picture already kept in other bytes (the scale changed) stays as kept
            report.skipped["timesink/image-conflict"] += 1
            out.note([cursor_op])
            continue
        if kept != hashlib.sha256(data).hexdigest():
            msg = "the brain's digest of a picture does not match"
            raise ShipFailed(msg)
        report.files += 1
        report.file_bytes += len(data)
        report.file_original_bytes += len(original_bytes)
        fields = {
            "capture_id": capture_id, "imagePath": rel,
            "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "original_bytes": len(original_bytes),
            "original_width": original_width, "original_height": original_height,
            "shipped_sha256": kept, "shipped_bytes": len(data),
            "shipped_width": width, "shipped_height": height,
            "scale": config.image_scale, "quality": config.image_quality,
        }
        out.add({
            "key": f"capture_image:{rel}:{digest(fields)}", "kind": "capture_image", "zone": "UTC",
            "at_ms": utc_ms(at) or now_ms, "text": "", "fields": fields,
        }, [cursor_op])
    out.note([(SET_META, ("images_backlog", "1" if backlog else "0"))])


def _signature(path: Path) -> str:
    """The modification time and size of the database and its write-ahead log."""
    parts = []
    for file in (path, path.with_name(path.name + "-wal")):
        with contextlib.suppress(OSError):
            stat = file.stat()
            parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
    return json.dumps(parts)


def mirror(
    config: TimesinkConfig, state: State, sink: Sink, report: Report, now_ms: int,
) -> None:
    """One pass over TimeSink's database and pictures.

    Raises:
        ShipFailed: the brain did not keep a batch; what was kept stays, the rest is retried.
    """
    db = config.db_path.expanduser()
    sweep = now_ms - state.meta_int("sweep_ms") >= SWEEP_MS
    signature = _signature(db)
    if (
        signature == state.meta("signature") and not sweep and state.pending() == 0
        and state.meta("images_backlog") != "1"
    ):
        report.skipped["timesink/poll"] += 1  # idle: the files have not been touched
        return
    out = Out(SOURCE, sink, state, report)
    with timesink.snapshot(db) as snap:
        if snap is None:
            LOGGER.info("mac-ship: TimeSink's database cannot be read now")
            return
        for name in _tables(snap.conn):
            _mirror_table(snap.conn, _Table(snap.conn, name), state, out, now_ms, sweep=sweep)
        if config.images:
            out.flush()  # rows first: an entry for a picture never precedes its row
            _ship_images(snap.conn, config, state, out, sink, report, now_ms)
    ops = [(SET_META, ("signature", signature))]
    if sweep:
        ops.append((SET_META, ("sweep_ms", str(now_ms))))
    out.note(ops)
    out.flush()
