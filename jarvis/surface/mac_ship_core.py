"""What the Mac shipper shares: its cursor state, its batches and the revision rule (ADR 0294).

The state is one SQLite file under the terminal's runtime root. Nothing in it is advanced until
the brain has confirmed the bytes that justify it: a batch's state changes are applied only after
the brain answered 200 or 201, so a restart, a refusal or a lost answer re-sends and never skips.

The revision rule keeps the brain's append-only store from growing a row per poll. A row (a
TimeSink row, a Claude Code session) is shipped when it first appears; after that a changed
version is shipped when it has settled (unchanged across two polls, or too old to change) or when
the last shipped version is older than the rule's interval, never in between.

Layer rules: stdlib and L2 only (L5 surface).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from pathlib import Path

BATCH_BYTES: Final = 4 * 1024 * 1024
"""A body of entries stays well under the brain's 16 MiB cap."""
BATCH_LINES: Final = 2000
HOUR_MS: Final = 60 * 60 * 1000
HASH_CHARS: Final = 12

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rows (
    tbl TEXT NOT NULL, ident TEXT NOT NULL,
    shipped_sha TEXT NOT NULL, shipped_ms INTEGER NOT NULL, seen_sha TEXT NOT NULL,
    PRIMARY KEY (tbl, ident)
);
CREATE TABLE IF NOT EXISTS sessions (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL, record TEXT NOT NULL, desk TEXT NOT NULL,
    shipped_sha TEXT NOT NULL, shipped_ms INTEGER NOT NULL, seen_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (cwd TEXT PRIMARY KEY, toplevel TEXT NOT NULL, origin TEXT);
CREATE TABLE IF NOT EXISTS code_log (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL, head TEXT NOT NULL, tail TEXT NOT NULL
);
"""

Op = tuple[str, tuple[Any, ...]]


class ShipFailed(RuntimeError):  # noqa: N818 - a stop for this pass, not a fault
    """The brain did not take what was sent; the cursor stays and the next pass sends it again."""


class Conflict(ShipFailed):
    """The brain already holds other bytes under this name; retrying cannot change that."""


class Sink(Protocol):
    """Where a shipment goes: the brain over HTTP, or in-process for a dry run."""

    def send_entries(self, source: str, body: bytes) -> None:
        """Send one JSONL body; raises :class:`ShipFailed` unless the brain kept it."""

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        """Send one file; the SHA-256 the brain kept, or :class:`ShipFailed`."""


@dataclass
class Report:
    """What one pass did; counts and bytes, never content."""

    entries: Counter[str] = field(default_factory=Counter)
    entry_bytes: int = 0
    files: int = 0
    file_bytes: int = 0
    file_original_bytes: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    failed: list[str] = field(default_factory=list)


def canonical(value: object) -> str:
    """``value`` as one deterministic JSON string, for hashing and for the wire."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: object) -> str:
    """The short content hash that ends a shipped key."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:HASH_CHARS]


class State:
    """The shipper's cursors and per-row memory."""

    def __init__(self, path: Path) -> None:
        """Open (creating) the state file at ``path``."""
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # each pass runs on a worker thread (asyncio.to_thread), one pass at a time
        self.conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        """Close the file."""
        self.conn.close()

    def meta(self, key: str) -> str | None:
        """The value kept under ``key``."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def meta_int(self, key: str) -> int:
        """The integer kept under ``key``, 0 if none."""
        value = self.meta(key)
        return int(value) if value is not None else 0

    def row(self, tbl: str, ident: str) -> tuple[str, int, str] | None:
        """``(shipped_sha, shipped_ms, seen_sha)`` of one row, or ``None`` if never seen."""
        found = self.conn.execute(
            "SELECT shipped_sha, shipped_ms, seen_sha FROM rows WHERE tbl = ? AND ident = ?",
            (tbl, ident),
        ).fetchone()
        return None if found is None else (found[0], found[1], found[2])

    def pending(self) -> int:
        """How many things changed but are waiting out the revision rule."""
        rows = self.conn.execute("SELECT count(*) FROM rows WHERE seen_sha != shipped_sha")
        sessions = self.conn.execute("SELECT count(*) FROM sessions WHERE seen_sha != shipped_sha")
        return int(rows.fetchone()[0]) + int(sessions.fetchone()[0])

    def apply(self, ops: list[Op]) -> None:
        """Apply the state changes a confirmed batch justifies, in one transaction."""
        with self.conn:
            for sql, params in ops:
                self.conn.execute(sql, params)


SET_META: Final = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)"
SET_ROW: Final = (
    "INSERT OR REPLACE INTO rows (tbl, ident, shipped_sha, shipped_ms, seen_sha) "
    "VALUES (?, ?, ?, ?, ?)"
)
SET_SESSION: Final = (
    "INSERT OR REPLACE INTO sessions "
    "(path, size, mtime_ns, offset, record, desk, shipped_sha, shipped_ms, seen_sha) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
SET_CODE_LOG: Final = (
    "INSERT OR REPLACE INTO code_log (path, size, mtime_ns, offset, head, tail) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def decide(  # noqa: PLR0913 - the row's memory, its version, the clock and the rule's three knobs
    prev: tuple[str, int, str] | None, sha: str, now_ms: int, *, settled: bool, interval_ms: int,
    unchanged_settles: bool = True,
) -> tuple[bool, tuple[str, int, str]]:
    """Whether to ship this version of a row now, and the row's memory afterwards.

    ``prev`` is ``(shipped_sha, shipped_ms, seen_sha)``. A new row ships. A row equal to what was
    shipped does not. A changed row ships when ``settled`` (the caller knows it cannot change
    any more), when it is the same as at the last look (it has stopped; ``unchanged_settles``), or
    when the shipped version is ``interval_ms`` old; otherwise it is only noted.
    """
    if prev is None:
        return True, (sha, now_ms, sha)
    shipped, shipped_ms, seen = prev
    if sha == shipped:
        return False, (shipped, shipped_ms, sha)
    if settled or (unchanged_settles and sha == seen) or now_ms - shipped_ms >= interval_ms:
        return True, (sha, now_ms, sha)
    return False, (shipped, shipped_ms, sha)


class Out:
    """The lines going to one source, sent in batches; state moves only when a batch is kept."""

    def __init__(self, source: str, sink: Sink, state: State, report: Report) -> None:
        """Nothing is sent until a batch fills or :meth:`flush` is called."""
        self.source = source
        self._sink = sink
        self._state = state
        self._report = report
        self._lines: list[bytes] = []
        self._kinds: list[str] = []
        self._size = 0
        self._ops: list[Op] = []

    def add(self, entry: dict[str, Any], ops: list[Op]) -> None:
        """Queue one entry; its state changes (``ops``) apply when its batch is kept."""
        line = canonical(entry).encode()
        self._lines.append(line)
        self._kinds.append(str(entry["kind"]))
        self._size += len(line) + 1
        self._ops.extend(ops)
        if self._size >= BATCH_BYTES or len(self._lines) >= BATCH_LINES:
            self.flush()

    def note(self, ops: list[Op]) -> None:
        """Queue state changes that ship nothing (a row seen, a cursor moved)."""
        self._ops.extend(ops)

    def flush(self) -> None:
        """Send what is queued; apply the state changes only after the brain kept it.

        Raises:
            ShipFailed: the brain did not keep it; nothing is applied.
        """
        if self._lines:
            body = b"\n".join(self._lines) + b"\n"
            self._sink.send_entries(self.source, body)
            self._report.entry_bytes += len(body)
            self._report.entries.update(f"{self.source}/{kind}" for kind in self._kinds)
        if self._ops:
            self._state.apply(self._ops)
        self._lines, self._kinds, self._size, self._ops = [], [], 0, []
