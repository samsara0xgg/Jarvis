"""L2 memory store — utterances, answers, Allen's profile, and history summaries.

One standalone SQLite file (``memory.db``), deliberately separate from the
runtime Event Log so the runtime can be rewritten without touching it.
Append-only: rows are never updated or deleted. Every writer opens its own
short-lived connection, so callers on any thread can write without sharing
state.

``records`` is the transcript and is never flagged or rewritten. A
``summaries`` row covers every record up to its anchor (``upto_record_id``);
the prompt shows the current summary and then the records after the anchor
verbatim. Before the first summary every record is in the prompt.

Timestamps are ISO 8601 local time with UTC offset at second precision
(``2026-09-12T09:30:00-04:00``). Recency ordering uses ``rowid`` (insertion
order) so a DST switch cannot reorder rows; range filters go through
SQLite's ``datetime()``, which understands the offset.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, NamedTuple

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
CREATE TABLE IF NOT EXISTS summaries (
    id             TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    base_id        TEXT REFERENCES summaries(id),
    upto_record_id TEXT NOT NULL REFERENCES records(id),
    summary        TEXT NOT NULL,
    model          TEXT,
    input_chars    INTEGER NOT NULL,
    output_chars   INTEGER NOT NULL
);
"""

DEFAULT_SEARCH_LIMIT: Final[int] = 20
_WEEKDAYS: Final[str] = "一二三四五六日"
# The [现在] line carries a "距上次交流" suffix once the gap passes this.
_GAP_NOTE_AFTER: Final[timedelta] = timedelta(minutes=30)

# Answers written before 2026-09-21 carry the retired <voice>/<document>
# envelope; the store keeps them as written, the prompt shows the words.
_ENVELOPE_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"</?(?:voice|document)>[ \t]*\n?", re.IGNORECASE,
)

# One transcript row: (id, ts, source, text).
Record = tuple[str, str, str, str]


@dataclass(frozen=True)
class MemorySettings:
    """The ``memory:`` block of ``config/jarvis.yaml``, resolved against the runtime root."""

    db_path: Path
    audio_dir: Path
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

        return cls(
            db_path=_path("db_path", runtime_root / "memory.db"),
            audio_dir=_path("audio_dir", runtime_root / "memory" / "audio"),
            retain_audio=values.get("retain_audio") is not False,
        )


@dataclass(frozen=True)
class SessionSettings:
    """The ``session:`` block of ``config/jarvis.yaml``.

    When the running conversation is compacted, what a compaction keeps
    verbatim, which preset writes the summary, and how large the Live brief
    may be. Every number is a knob; ``compact_prompt`` is the summariser's
    whole system prompt, and an empty one means no compaction ever runs.
    ``history_since`` is the ISO timestamp the prompt's history starts at;
    earlier records stay in the store for search and never reach the prompt
    or a compaction.
    """

    idle_before_compact_s: float = 3600.0
    compact_at_context_ratio: float = 0.4
    verbatim_window_days: int = 7
    compact_preset: str = "deep"
    summary_max_chars: int = 8000
    live_brief_max_chars: int = 1500
    compact_prompt: str = ""
    history_since: str = ""

    @classmethod
    def from_config(cls, raw: object) -> SessionSettings:
        """Build settings from the raw config block; every key is optional."""
        values: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
        defaults = cls()

        def _positive(key: str, default: float) -> float:
            value = values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
            return default

        preset = values.get("compact_preset")
        prompt = values.get("compact_prompt")
        since = values.get("history_since")
        return cls(
            idle_before_compact_s=_positive(
                "idle_before_compact_s", defaults.idle_before_compact_s,
            ),
            compact_at_context_ratio=_positive(
                "compact_at_context_ratio", defaults.compact_at_context_ratio,
            ),
            verbatim_window_days=int(
                _positive("verbatim_window_days", defaults.verbatim_window_days),
            ),
            compact_preset=(
                preset if isinstance(preset, str) and preset else defaults.compact_preset
            ),
            summary_max_chars=int(_positive("summary_max_chars", defaults.summary_max_chars)),
            live_brief_max_chars=int(
                _positive("live_brief_max_chars", defaults.live_brief_max_chars),
            ),
            compact_prompt=prompt.strip() if isinstance(prompt, str) else "",
            history_since=since.strip() if isinstance(since, str) else "",
        )


class MemoryContext(NamedTuple):
    """The prompt blocks rendered from memory.db for one turn."""

    profile: str  # [关于 Allen] lines for the system prompt; "" when the profile is empty
    history: str  # current summary, then verbatim records: stable between turns
    now: str  # the time line: changes every turn, so it goes after the history


class VerbatimStats(NamedTuple):
    """Size and span of the records the prompt currently shows verbatim."""

    chars: int
    oldest_ts: str | None
    newest_ts: str | None


class CompactionRange(NamedTuple):
    """What one compaction job summarises.

    The previous summary plus the records after its anchor that are older
    than the verbatim window.
    """

    base_id: str | None
    previous_summary: str | None
    records: tuple[Record, ...]  # oldest first; the last one becomes the new anchor


class _Summary(NamedTuple):
    id: str
    summary: str
    upto_record_id: str
    anchor_rowid: int
    anchor_ts: str


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


def search_records(  # noqa: PLR0913 — one optional filter per tool argument.
    path: Path,
    *,
    keyword: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    record_ids: Sequence[str] | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[Record]:
    """Return records, newest first, matching every given filter.

    Only ``records`` is read: a summary is never a search result.
    """
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
    if record_ids:
        ids = [str(record_id) for record_id in record_ids]
        clauses.append(f"id IN ({', '.join('?' * len(ids))})")
        params.extend(ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, limit))
    with closing(open_memory_db(path)) as conn:
        # S608: `where` is assembled from fixed literals; every value is bound.
        sql = f"SELECT id, ts, source, text FROM records {where} ORDER BY rowid DESC LIMIT ?"  # noqa: S608
        rows = conn.execute(sql, params).fetchall()
    return [(str(i), str(ts), str(source), str(text)) for i, ts, source, text in rows]


def _current_summary(conn: sqlite3.Connection) -> _Summary | None:
    """The latest summary with its anchor resolved inside this read.

    The anchor is a record id, not a rowid: VACUUM may renumber rowids, so
    the rowid is resolved fresh every time. A summary whose anchor record
    is gone is an explicit error, never an empty history.
    """
    row = conn.execute(
        "SELECT id, summary, upto_record_id FROM summaries ORDER BY rowid DESC LIMIT 1",
    ).fetchone()
    if row is None:
        return None
    summary_id, summary, upto = (str(value) for value in row)
    anchor = conn.execute("SELECT rowid, ts FROM records WHERE id = ?", (upto,)).fetchone()
    if anchor is None:
        msg = f"summary {summary_id} anchors a missing record {upto}"
        raise LookupError(msg)
    return _Summary(summary_id, summary, upto, int(anchor[0]), str(anchor[1]))


def _profile_lines(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT text FROM profile ORDER BY rowid").fetchall()
    return [f"- {text}" for (text,) in rows]


def _effective_anchor(conn: sqlite3.Connection, anchor_rowid: int | None, since: str) -> int:
    """The rowid the prompt's verbatim records must come after.

    The current summary's anchor, or the row before the first record on or
    after ``since`` — whichever is later. With no record since the cutoff,
    every row is before the start of history.
    """
    anchor = -1 if anchor_rowid is None else anchor_rowid
    if since:
        (first,) = conn.execute(
            "SELECT MIN(rowid) FROM records WHERE datetime(ts) >= datetime(?)", (since,),
        ).fetchone()
        if first is None:
            (last,) = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()
            floor = int(last)
        else:
            floor = int(first) - 1
        anchor = max(anchor, floor)
    return anchor


def _records_after(conn: sqlite3.Connection, anchor: int) -> list[Record]:
    """Every record after ``anchor`` (see :func:`_effective_anchor`)."""
    rows = conn.execute(
        "SELECT id, ts, source, text FROM records WHERE rowid > ? ORDER BY rowid", (anchor,),
    ).fetchall()
    return [(str(i), str(ts), str(source), str(text)) for i, ts, source, text in rows]


def _plain(text: str) -> str:
    """A record's text without the retired envelope tags."""
    return _ENVELOPE_TAG_RE.sub("", text).strip() if "<" in text else text


def _now_line(moment: datetime, last_ts: str | None) -> str:
    line = f"时间：{moment.isoformat(timespec='minutes')} 周{_WEEKDAYS[moment.weekday()]}"  # noqa: RUF001 — Chinese punctuation is intentional.
    if last_ts is None:
        return line
    gap = moment - datetime.fromisoformat(last_ts)
    if gap <= _GAP_NOTE_AFTER:
        return line
    minutes = int(gap.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} 天"] if days else []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分")
    return f"{line} · 距上次交流 {' '.join(parts)}"


def render_context(
    path: Path, *, exclude_id: str, since: str = "", now: datetime | None = None,
) -> MemoryContext:
    """Render the decision-path prompt blocks in one consistent read.

    ``profile`` goes to the system prompt. ``history`` is the current summary
    (if any) and every record after its anchor and on or after ``since``, in
    full; it only grows at its end between compactions, so the provider's
    prefix cache covers it. ``now`` is the per-turn time line.
    ``exclude_id`` is the current turn's own utterance, which the prompt
    already carries as the live user message.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = _profile_lines(conn)
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        records = _records_after(conn, anchor)
    profile_block = "\n".join(["[关于 Allen]", *profile]) if profile else ""
    lines: list[str] = []
    if current is not None:
        lines.append(
            f"[对话摘要 · 覆盖到 {current.anchor_ts} · "
            "措辞、数字、是否同意 用 read_records 按 record_id 回查原话]",
        )
        lines.append(current.summary)
    lines.append("[对话记录, 全文, 时间正序]")
    shown = [record for record in records if record[0] != exclude_id]
    if not shown:
        lines.append("(无)")
    lines.extend(f"[{ts}] {source}: {_plain(text)}" for _, ts, source, text in shown)
    last_ts = shown[-1][1] if shown else (current.anchor_ts if current else None)
    return MemoryContext(profile_block, "\n".join(lines), _now_line(moment, last_ts))


def _summary_sections(summary: str) -> list[str]:
    """Split a summary at its ``### `` headings; the title block comes first."""
    sections: list[str] = []
    for line in summary.splitlines():
        if line.startswith("### ") or not sections:
            sections.append(line)
        else:
            sections[-1] = f"{sections[-1]}\n{line}"
    return sections


def brief_note(path: Path, *, max_chars: int, now: datetime | None = None) -> str:
    """Render the Live startup brief under one character budget.

    Same content as :func:`render_context`. Trimming cuts whole items and
    never the tail of a text: the oldest verbatim records go first, then
    the summary's sections from the bottom up, and the profile lines last.
    The retrieval line tells the Live model
    to ask the backend, which it can, instead of naming ``search_records``,
    which it cannot call.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = _profile_lines(conn)
        current = _current_summary(conn)
        records = _records_after(conn, -1 if current is None else current.anchor_rowid)
    summary_head = (
        [f"[对话摘要 · 覆盖到 {current.anchor_ts} · 原话细节请向后台查询]"] if current else []
    )
    sections = _summary_sections(current.summary) if current else []
    record_lines = [f"[{ts}] {source}: {text}" for _, ts, source, text in records]
    now_line = _now_line(moment, records[-1][1] if records else None)

    def _assemble() -> str:
        return "\n".join(
            [
                "[关于 Allen]",
                *profile,
                now_line,
                *summary_head,
                *sections,
                "[对话记录, 时间正序]",
                *(record_lines or ["(无)"]),
            ],
        )

    while len(_assemble()) > max_chars:
        if record_lines:
            record_lines.pop(0)
        elif sections:
            sections.pop()
        elif len(profile) > 1:
            profile.pop()
        else:
            break
    return _assemble()


def verbatim_stats(path: Path, *, since: str = "") -> VerbatimStats:
    """Size and time span of the records after the current anchor.

    Aggregated in SQL: the sweep calls this on every tick, on the loop thread.
    """
    with closing(open_memory_db(path)) as conn:
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        chars, oldest, newest = conn.execute(
            "SELECT COALESCE(SUM(length(text)), 0), "
            "(SELECT ts FROM records WHERE rowid > ? ORDER BY rowid LIMIT 1), "
            "(SELECT ts FROM records WHERE rowid > ? ORDER BY rowid DESC LIMIT 1) "
            "FROM records WHERE rowid > ?",
            (anchor, anchor, anchor),
        ).fetchone()
    return VerbatimStats(
        int(chars),
        str(oldest) if oldest is not None else None,
        str(newest) if newest is not None else None,
    )


def compaction_range(
    path: Path, *, window_days: int, since: str = "", now: datetime | None = None,
) -> CompactionRange | None:
    """The records a compaction would fold, or None when there are none.

    Those after the current anchor and older than ``window_days``, taken as
    a prefix in insertion order so the new anchor leaves nothing older
    behind it.
    """
    cutoff = (now or local_now()) - timedelta(days=window_days)
    with closing(open_memory_db(path)) as conn:
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        records = _records_after(conn, anchor)
    older: list[Record] = []
    for record in records:
        if datetime.fromisoformat(record[1]) >= cutoff:
            break
        older.append(record)
    if not older:
        return None
    return CompactionRange(
        current.id if current else None,
        current.summary if current else None,
        tuple(older),
    )


def append_summary(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    base_id: str | None,
    upto_record_id: str,
    summary: str,
    model: str,
    input_chars: int,
    output_chars: int,
) -> bool:
    """Commit one summary row, or return False when the world moved.

    The row lands only if ``base_id`` is still the current summary and the
    new anchor exists and lies after the current one; a job that finished
    against a stale base changes nothing.
    """
    with closing(open_memory_db(path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _current_summary(conn)
            if (current.id if current else None) != base_id:
                conn.execute("ROLLBACK")
                return False
            anchor = conn.execute(
                "SELECT rowid FROM records WHERE id = ?", (upto_record_id,),
            ).fetchone()
            if anchor is None or (current is not None and int(anchor[0]) <= current.anchor_rowid):
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO summaries (id, ts, base_id, upto_record_id, summary, model, "
                "input_chars, output_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"summary:{upto_record_id}",
                    iso_seconds(local_now()),
                    base_id,
                    upto_record_id,
                    summary,
                    model,
                    input_chars,
                    output_chars,
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return True


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "CompactionRange",
    "MemoryContext",
    "MemorySettings",
    "Record",
    "SessionSettings",
    "VerbatimStats",
    "append_record",
    "append_summary",
    "brief_note",
    "compaction_range",
    "iso_seconds",
    "local_now",
    "open_memory_db",
    "render_context",
    "search_records",
    "verbatim_stats",
]
