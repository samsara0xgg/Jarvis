"""L2 memory store — every utterance and answer, plus Allen's profile.

One standalone SQLite file (``memory.db``), deliberately separate from the
runtime Event Log so the runtime can be rewritten without touching it.
Append-only: rows are never updated or deleted. Every writer opens its own
short-lived connection, so callers on any thread can write without sharing
state.

Timestamps are ISO 8601 local time with UTC offset at second precision
(``2026-09-12T09:30:00-04:00``). Recency ordering uses ``rowid`` (insertion
order) so a DST switch cannot reorder rows; range filters go through
SQLite's ``datetime()``, which understands the offset.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    source     TEXT NOT NULL,
    text       TEXT NOT NULL,
    audio_path TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    id   TEXT PRIMARY KEY,
    ts   TEXT NOT NULL,
    text TEXT NOT NULL
);
"""

DEFAULT_CONTEXT_DAYS: Final[int] = 7
DEFAULT_SEARCH_LIMIT: Final[int] = 20
_WEEKDAYS: Final[str] = "一二三四五六日"


@dataclass(frozen=True)
class MemorySettings:
    """The ``memory:`` block of ``config/jarvis.yaml``, resolved against the runtime root."""

    db_path: Path
    audio_dir: Path
    context_days: int = DEFAULT_CONTEXT_DAYS
    retain_audio: bool = True

    @classmethod
    def from_config(cls, raw: object, *, runtime_root: Path) -> MemorySettings:
        """Build settings from the raw config block; every key is optional."""
        values: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}

        def _path(key: str, default: Path) -> Path:
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
            return default

        days = values.get("context_days")
        return cls(
            db_path=_path("db_path", runtime_root / "memory.db"),
            audio_dir=_path("audio_dir", runtime_root / "memory" / "audio"),
            context_days=days if isinstance(days, int) and days > 0 else DEFAULT_CONTEXT_DAYS,
            retain_audio=values.get("retain_audio") is not False,
        )


def local_now() -> datetime:
    """Return the current local time with its UTC offset attached."""
    return datetime.now().astimezone()


def iso_seconds(moment: datetime) -> str:
    """Render ``moment`` as ``2026-09-12T09:30:00-04:00``."""
    return moment.isoformat(timespec="seconds")


def open_memory_db(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the memory database at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def append_record(
    path: Path,
    *,
    record_id: str,
    source: str,
    text: str,
    audio_path: str | None = None,
) -> None:
    """Append one record; a repeated ``record_id`` (retry) is a no-op."""
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO records (id, ts, source, text, audio_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (record_id, iso_seconds(local_now()), source, text, audio_path),
        )


def search_records(
    path: Path,
    *,
    keyword: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[tuple[str, str, str]]:
    """Return ``(ts, source, text)`` rows, newest first, matching every given filter."""
    clauses: list[str] = []
    params: list[object] = []
    if keyword:
        # ponytail: LIKE full scan; switch to FTS5 trigram past ~100k rows.
        clauses.append("text LIKE ? ESCAPE '\\'")
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if from_ts:
        clauses.append("datetime(ts) >= datetime(?)")
        params.append(from_ts)
    if to_ts:
        clauses.append("datetime(ts) <= datetime(?)")
        params.append(to_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, limit))
    with closing(open_memory_db(path)) as conn:
        # S608: `where` is assembled from fixed literals; every value is bound.
        sql = f"SELECT ts, source, text FROM records {where} ORDER BY rowid DESC LIMIT ?"  # noqa: S608
        rows = conn.execute(sql, params).fetchall()
    return [(str(ts), str(source), str(text)) for ts, source, text in rows]


def context_note(
    path: Path,
    *,
    context_days: int,
    exclude_id: str,
    now: datetime | None = None,
) -> str:
    """Render the per-turn prompt note: profile, current time, recent records in full.

    ``exclude_id`` is the current turn's own utterance, which the prompt
    already carries as the live user message.
    """
    moment = now or local_now()
    since = iso_seconds(moment - timedelta(days=context_days))
    with closing(open_memory_db(path)) as conn:
        profile = [
            str(text)
            for (text,) in conn.execute("SELECT text FROM profile ORDER BY rowid").fetchall()
        ]
        records = conn.execute(
            "SELECT ts, source, text FROM records "
            "WHERE datetime(ts) >= datetime(?) AND id != ? ORDER BY rowid",
            (since, exclude_id),
        ).fetchall()
    lines = ["[关于 Allen]", *(f"- {text}" for text in profile)]
    if not profile:
        lines.append("- (档案为空)")
    lines.append(f"[现在] {iso_seconds(moment)} 周{_WEEKDAYS[moment.weekday()]}")
    lines.append(
        f"[最近 {context_days} 天的对话记录, 全文, 时间正序; 更早的用 search_records 查]",
    )
    if not records:
        lines.append("(无)")
    lines.extend(f"[{ts}] {source}: {text}" for ts, source, text in records)
    return "\n".join(lines)


# Each record in a brief is cut to about this many characters (ADR-0016 D7).
_BRIEF_RECORD_CHARS = 200


def brief_note(path: Path, *, max_chars: int, now: datetime | None = None) -> str:
    """Render a character-budgeted session brief (ADR-0016 D7).

    The whole profile always fits first; then records are taken from the
    newest backwards, each cut to ``_BRIEF_RECORD_CHARS``, until the budget
    is spent, and emitted in time order.
    Unlike :func:`context_note` there is no tool hint: the reader is the
    Live model, which asks the backend instead of calling ``search_records``.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = [
            str(text)
            for (text,) in conn.execute("SELECT text FROM profile ORDER BY rowid").fetchall()
        ]
        cursor = conn.execute("SELECT ts, source, text FROM records ORDER BY rowid DESC")
        head = ["[关于 Allen]", *(f"- {text}" for text in profile)]
        if not profile:
            head.append("- (档案为空)")
        head.append(f"[现在] {iso_seconds(moment)} 周{_WEEKDAYS[moment.weekday()]}")
        head.append("[最近的对话记录, 时间正序]")
        budget = max_chars - sum(len(line) + 1 for line in head)
        newest_first: list[str] = []
        for ts, source, text in cursor:
            # One long answer must not end the brief: cut the record and go on
            # (2026-09-12: a 938-character row left a 286-character brief).
            body = str(text)
            if len(body) > _BRIEF_RECORD_CHARS:
                body = body[:_BRIEF_RECORD_CHARS] + "..."
            line = f"[{ts}] {source}: {body}"
            if len(line) + 1 > budget:
                break
            budget -= len(line) + 1
            newest_first.append(line)
    if not newest_first:
        head.append("(无)")
    return "\n".join([*head, *reversed(newest_first)])


__all__ = [
    "DEFAULT_CONTEXT_DAYS",
    "DEFAULT_SEARCH_LIMIT",
    "MemorySettings",
    "append_record",
    "brief_note",
    "context_note",
    "iso_seconds",
    "local_now",
    "open_memory_db",
    "search_records",
]
