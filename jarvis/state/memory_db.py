"""L2 memory store — utterances, answers, core memory, and history summaries.

One standalone SQLite file (``memory.db``), deliberately separate from the
runtime Event Log so the runtime can be rewritten without touching it.
Append-only: rows are never updated or deleted. The user's lasting facts are
``core_memory`` versions (ADR 0146); the legacy ``profile`` table is read once, to
migrate it into the first version. Every writer opens its own
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
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final, NamedTuple

from jarvis.shared import lang
from jarvis.state import NewerDataError, core_memory

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
CREATE TABLE IF NOT EXISTS sent (
    record_id TEXT PRIMARY KEY REFERENCES records(id),
    text      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_summaries (
    id           TEXT PRIMARY KEY,
    day          TEXT NOT NULL,
    ts           TEXT NOT NULL,
    summary      TEXT NOT NULL,
    model        TEXT,
    record_count INTEGER NOT NULL,
    input_chars  INTEGER NOT NULL,
    output_chars INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS core_memory (
    id       TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,
    base_id  TEXT,
    origin   TEXT NOT NULL,
    upto_day TEXT,
    doc      TEXT NOT NULL,
    changes  TEXT NOT NULL,
    chars    INTEGER NOT NULL
);
"""

# ``PRAGMA user_version`` (ADR 0068). 0 is a file from before the stamp, same
# schema as 1; a file above this was written by a newer Jarvis and is refused.
# ``sent``, ``day_summaries`` and ``core_memory`` are additive: an older opener ignores them,
# so they need no new version.
SCHEMA_VERSION: Final[int] = 1
DEFAULT_SEARCH_LIMIT: Final[int] = 20
# The Live brief's label for the user's own rows; the stored source stays ``allen``.
_USER_LABEL: Final[str] = "user"
# The time line carries a "since the last exchange" suffix once the gap passes this.
_GAP_NOTE_AFTER: Final[timedelta] = timedelta(minutes=30)

# Answers written before 2026-09-21 carry the retired <voice>/<document>
# envelope; the store keeps them as written, the prompt shows the words.
_ENVELOPE_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"</?(?:voice|document)>[ \t]*\n?", re.IGNORECASE,
)
# Six answers of 2026-09-22..25 open with the "[ts] source:" history label
# the model copied from its prompt (ADR 0044); the prompt shows the words.
_COPIED_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^\[\d{4}-\d\d-\d\dT[^\]\n]*\] \w+: ")

# One transcript row: (id, ts, source, text).
Record = tuple[str, str, str, str]


def retention_days(value: object) -> int | None:
    """A positive whole number of days, else None: keep forever (ADR 0067)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


@dataclass(frozen=True)
class MemorySettings:
    """The ``memory:`` block of ``config/jarvis.yaml``, resolved against the runtime root."""

    db_path: Path
    audio_dir: Path
    retain_audio: bool = True
    # ADR 0067: recordings older than this many days are deleted; None keeps them.
    audio_retention_days: int | None = None

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
            audio_retention_days=retention_days(values.get("audio_retention_days")),
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
    or a compaction. ``recent_records`` (0: every record) shows only the
    latest records of that history; see :func:`render_context`.
    ``replay_sent`` replays each turn's user message as it was sent
    (docs/plans/replay-as-sent-proposal.md).
    """

    idle_before_compact_s: float = 3600.0
    compact_at_context_ratio: float = 0.4
    verbatim_window_days: int = 7
    compact_preset: str = "deep"
    summary_max_chars: int = 8000
    live_brief_max_chars: int = 1500
    compact_prompt: str = ""
    history_since: str = ""
    recent_records: int = 0
    replay_sent: bool = False

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
            recent_records=int(_positive("recent_records", 0)),
            replay_sent=values.get("replay_sent") is True,
        )


class MemoryContext(NamedTuple):
    """The prompt blocks rendered from memory.db for one turn."""

    profile: str  # [About the user] block (core memory) for the system prompt; "" when empty
    history: tuple[dict[str, str], ...]  # summary, then one message per record, by role
    now: str  # the time line: changes every turn, so it goes after the history


class VerbatimStats(NamedTuple):
    """Size and span of the records the prompt currently shows verbatim.

    ``hidden`` is how many of them a ``recent_records`` window cuts off
    (see :func:`_hidden_records`): the part of history no summary covers yet.
    """

    chars: int
    oldest_ts: str | None
    newest_ts: str | None
    hidden: int = 0


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
    """Open (creating if needed) the memory database at ``path``.

    Raises :class:`NewerDataError` for a file a newer Jarvis wrote (ADR 0068).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        conn.close()
        raise NewerDataError(path.name, found, SCHEMA_VERSION)
    conn.executescript(_SCHEMA)
    if found < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


_NAME_LINE: Final[tuple[str, str]] = ("The user's name is ", ".")


def _keep_item(
    path: Path,
    *,
    topic: str,
    text: str,
    section: str | None,
    origin: str,
) -> None:
    """Append a core-memory version that keeps ``text`` under ``topic`` (ADR 0146)."""
    now = local_now()
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.current(conn, iso_seconds(now))
        doc, change = core_memory.remember(
            base.doc,
            topic=topic,
            text=text,
            section=section,
            day=now.date().isoformat(),
        )
        core_memory.append_version(
            conn,
            base=base,
            doc=doc,
            origin=origin,
            upto_day=base.upto_day,
            changes=[change],
            now=iso_seconds(now),
        )


def set_user_name(path: Path, name: str) -> None:
    """Keep ``name`` as the core memory's name line (first-run setup)."""
    _keep_item(
        path,
        topic=core_memory.NAME_TOPIC,
        text=name.join(_NAME_LINE),
        section=None,
        origin="setup",
    )


def remember_fact(path: Path, topic: str, fact: str, section: str | None = None) -> None:
    """Keep ``fact`` in core memory under ``topic``; the same topic replaces it (ADR 0066, 0146).

    The item keeps its place in the block, so rewriting a fact never reorders the prompt's
    ``[About the user]`` lines. A new topic goes to the end of ``section`` (default: 关于你).
    """
    _keep_item(path, topic=topic, text=f"{topic}: {fact}", section=section, origin="remember")


def current_core_memory(path: Path) -> core_memory.Version:
    """The current core memory version (migrating ``profile`` on the first read)."""
    with closing(open_memory_db(path)) as conn:
        return core_memory.current(conn, iso_seconds(local_now()))


def user_name(path: Path) -> str | None:
    """The name first-run setup saved, or ``None``."""
    if not path.is_file():
        return None
    for item in current_core_memory(path).doc[core_memory.DEFAULT_SECTION]:
        if item["topic"] == core_memory.NAME_TOPIC:
            return str(item["text"]).removeprefix(_NAME_LINE[0]).removesuffix(_NAME_LINE[1])
    return None


def core_memory_pending_days(path: Path, today: date) -> list[str]:
    """Local days before ``today`` with a day summary that no nightly run consolidated yet.

    Every day after the current version's ``upto_day`` (every day when it is unset), oldest first.
    """
    upto = current_core_memory(path).upto_day or ""
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM day_summaries WHERE day > ? AND day < ? ORDER BY day",
            (upto, today.isoformat()),
        ).fetchall()
    return [str(day) for (day,) in rows]


def append_nightly_core_memory(
    path: Path,
    *,
    base_id: str,
    day: str,
    changes: Sequence[Mapping[str, object]],
    review_log: Sequence[Mapping[str, object]] = (),
) -> str | None:
    """Append the nightly version for ``day`` and return its id, or None when the world moved.

    The change list applies to the current document only if ``base_id`` is still the current
    version (the item numbers it cites are that version's); otherwise nothing is stored. An
    empty list appends the same document, which advances ``upto_day``. ``review_log`` are
    Jev review entries (ADR 0146): stored after ``changes`` in the row, never applied.
    """
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.current(conn, now)
        if base.id != base_id:
            return None
        return core_memory.append_version(
            conn,
            base=base,
            doc=core_memory.apply_changes(base.doc, changes, day),
            origin="nightly",
            upto_day=day,
            changes=[*changes, *review_log],
            now=now,
        )


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


def record_sent(path: Path, record_id: str, text: str) -> None:
    """Keep the user message a turn sent for ``record_id``; the first one stays.

    Later histories replay it as sent, so a turn's request starts with the
    previous turn's and the provider's cache, which only reuses a whole
    earlier request, can serve it.
    """
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent (record_id, text) VALUES (?, ?)", (record_id, text),
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


def _core_memory_lines(conn: sqlite3.Connection) -> list[str]:
    """The rendered core memory, one prompt line each (ADR 0146)."""
    version = core_memory.current(conn, iso_seconds(local_now()))
    return core_memory.render(version.doc).splitlines()


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
    """A record's text without the retired envelope tags or a copied history label."""
    text = _COPIED_LABEL_RE.sub("", text)
    return _ENVELOPE_TAG_RE.sub("", text).strip() if "<" in text else text


def _now_line(moment: datetime, last_ts: str | None) -> str:
    line = f"Time: {moment.isoformat(timespec='minutes')} {lang.weekday(moment, 'en')}"
    if last_ts is None:
        return line
    gap = moment - datetime.fromisoformat(last_ts)
    if gap <= _GAP_NOTE_AFTER:
        return line
    minutes = int(gap.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} d"] if days else []
    if hours:
        parts.append(f"{hours} h")
    if minutes and not days:
        parts.append(f"{minutes} min")
    return f"{line} · {' '.join(parts)} since the last exchange"


def _append_turn(turns: list[dict[str, str]], role: str, content: str) -> None:
    """Add one message; a repeated role joins the previous message instead."""
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"] = f"{turns[-1]['content']}\n{content}"
    else:
        turns.append({"role": role, "content": content})


def _hidden_records(shown: int, recent: int) -> int:
    """How many of the oldest ``shown`` records a ``recent`` window leaves out.

    The cut moves ``recent`` records at a time, so between ``recent`` and
    ``2 * recent - 1`` records show and the history's start, which the
    provider's prefix cache needs unchanged, holds for ``recent`` records.
    """
    if recent <= 0 or shown < 2 * recent:
        return 0
    return (shown - recent) // recent * recent


def render_context(
    path: Path, *, exclude_id: str, since: str = "", now: datetime | None = None,
    recent: int = 0,
) -> MemoryContext:
    """Render the decision-path prompt blocks in one consistent read.

    ``profile`` (the rendered core memory) goes to the system prompt. ``history`` is the
    current summary (if any) as a ``user`` message, then one message per record after its
    anchor and on or after ``since``: ``allen`` rows are ``user``, every
    other source is ``assistant``, and adjacent rows of one role join into
    one message. Records carry their words only (ADR 0044): no timestamp or
    source label, which the model copied into its answers; the first
    ``user`` row of each day opens with a day marker (``[9月24日 周四]`` /
    ``[Thursday, September 24]``, in the language setting). A row with a
    ``sent`` message is replayed exactly as its turn sent it, state block
    and all, without a marker. It only grows at its end between
    compactions, so the provider's prefix cache covers it. ``now`` is the
    per-turn time line. ``exclude_id`` is the current turn's own utterance,
    which the prompt already carries as the live user message. ``recent`` > 0
    shows only the latest records (see :func:`_hidden_records`); a note after
    the summary says how many earlier ones there are and how to find them.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = _core_memory_lines(conn)
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        records = _records_after(conn, anchor)
        sent = dict(
            conn.execute(
                "SELECT s.record_id, s.text FROM sent s JOIN records r ON r.id = s.record_id "
                "WHERE r.rowid > ?",
                (anchor,),
            ).fetchall(),
        )
    profile_block = "\n".join(["[About the user]", *profile]) if profile else ""
    turns: list[dict[str, str]] = []
    if current is not None:
        _append_turn(
            turns,
            "user",
            f"[Conversation summary · up to {current.anchor_ts} · look up exact wording, "
            "numbers and agreement with read_records by record_id, or find a record "
            "with search_records]\n"
            f"{current.summary}",
        )
    shown = [record for record in records if record[0] != exclude_id]
    hidden = _hidden_records(len(shown), recent)
    if hidden:
        first_ts = shown[hidden][1]
        _append_turn(
            turns,
            "user",
            f"[Earlier conversation · {hidden} records before {first_ts} are not shown · "
            f"find them with search_records (to={first_ts}), then read_records]",
        )
        shown = shown[hidden:]
    marked_day = None
    for record_id, ts, source, text in shown:
        role = "user" if source == "allen" else "assistant"
        day = datetime.fromisoformat(ts).date()
        as_sent = sent.get(record_id)
        if as_sent is not None:
            content = str(as_sent)
        else:
            content = _plain(text)
            if role == "user" and day != marked_day:
                content = f"{lang.day_marker(day)}\n{content}"
        if role == "user":
            marked_day = day
        _append_turn(turns, role, content)
    last_ts = shown[-1][1] if shown else (current.anchor_ts if current else None)
    return MemoryContext(profile_block, tuple(turns), _now_line(moment, last_ts))


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
    the summary's sections from the bottom up, and the core memory's items last.
    The retrieval line tells the Live model
    to ask the backend, which it can, instead of naming ``search_records``,
    which it cannot call.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = _core_memory_lines(conn)
        current = _current_summary(conn)
        records = _records_after(conn, -1 if current is None else current.anchor_rowid)
    summary_head = (
        [f"[Conversation summary · up to {current.anchor_ts} · ask the backend for exact words]"]
        if current
        else []
    )
    sections = _summary_sections(current.summary) if current else []
    record_lines = [
        f"[{ts}] {_USER_LABEL if source == 'allen' else source}: {text}"
        for _, ts, source, text in records
    ]
    now_line = _now_line(moment, records[-1][1] if records else None)

    def _assemble() -> str:
        return "\n".join(
            [
                "[About the user]",
                *profile,
                now_line,
                *summary_head,
                *sections,
                "[Conversation records, oldest first]",
                *(record_lines or ["(none)"]),
            ],
        )

    while len(_assemble()) > max_chars:
        if record_lines:
            record_lines.pop(0)
        elif sections:
            sections.pop()
        elif len(profile) > 2:  # noqa: PLR2004 — a section heading and its first item stay
            profile.pop()
            if profile[-1].startswith("### "):
                profile.pop()
        else:
            break
    return _assemble()


def conversation_rows(
    path: Path, *, since: str = "", after: int = 0, limit: int = 200,
) -> list[dict[str, object]]:
    """Records for the conversation window, oldest first, each with its ``seq``.

    ``after`` is the ``seq`` (rowid) the client already holds: 0 asks for
    the newest ``limit`` rows, anything else for the rows past it. ``since``
    is the floor the prompt's history uses; earlier rows never appear here.
    """
    clauses = ["rowid > ?"] if after else []
    params: list[object] = [after] if after else []
    if since:
        clauses.append("datetime(ts) >= datetime(?)")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ORDER BY rowid" if after else "ORDER BY rowid DESC"
    params.append(max(1, limit))
    with closing(open_memory_db(path)) as conn:
        # S608: `where` / `order` are assembled from fixed literals; every value is bound.
        sql = f"SELECT rowid, id, ts, source, text FROM records {where} {order} LIMIT ?"  # noqa: S608
        rows = conn.execute(sql, params).fetchall()
    if not after:
        rows.reverse()
    return [
        {
            "seq": int(seq),
            "id": str(rid),
            "ts": str(ts),
            "source": str(source),
            "text": _plain(str(text)),
        }
        for seq, rid, ts, source, text in rows
    ]


def verbatim_stats(path: Path, *, since: str = "", recent: int = 0) -> VerbatimStats:
    """Size and time span of the records after the current anchor.

    Aggregated in SQL: the sweep calls this on every tick, on the loop thread.
    """
    with closing(open_memory_db(path)) as conn:
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        chars, count, oldest, newest = conn.execute(
            "SELECT COALESCE(SUM(length(text)), 0), COUNT(*), "
            "(SELECT ts FROM records WHERE rowid > ? ORDER BY rowid LIMIT 1), "
            "(SELECT ts FROM records WHERE rowid > ? ORDER BY rowid DESC LIMIT 1) "
            "FROM records WHERE rowid > ?",
            (anchor, anchor, anchor),
        ).fetchone()
    return VerbatimStats(
        int(chars),
        str(oldest) if oldest is not None else None,
        str(newest) if newest is not None else None,
        _hidden_records(int(count), recent),
    )


def compaction_range(
    path: Path,
    *,
    window_days: int,
    since: str = "",
    now: datetime | None = None,
    recent: int = 0,
) -> CompactionRange | None:
    """The records a compaction would fold, or None when there are none.

    With ``recent`` > 0 the record window decides: the records
    :func:`render_context` leaves out of the prompt, nothing else. With 0,
    those after the current anchor and older than ``window_days``. Either
    way a prefix in insertion order, so the new anchor leaves nothing older
    behind it.
    """
    cutoff = (now or local_now()) - timedelta(days=window_days)
    with closing(open_memory_db(path)) as conn:
        current = _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        records = _records_after(conn, anchor)
    older: list[Record] = []
    if recent > 0:
        older = records[: _hidden_records(len(records), recent)]
    else:
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
) -> str | None:
    """Commit one summary row and return its id, or None when the world moved.

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
                return None
            anchor = conn.execute(
                "SELECT rowid FROM records WHERE id = ?", (upto_record_id,),
            ).fetchone()
            if anchor is None or (current is not None and int(anchor[0]) <= current.anchor_rowid):
                conn.execute("ROLLBACK")
                return None
            summary_id = f"summary:{upto_record_id}"
            conn.execute(
                "INSERT INTO summaries (id, ts, base_id, upto_record_id, summary, model, "
                "input_chars, output_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id,
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
    return summary_id


def append_day_summary(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    day: str,
    summary: str,
    model: str,
    record_count: int,
    input_chars: int,
    output_chars: int,
) -> str:
    """Append one day's summary and return its id; the latest row of a day is current.

    Older rows of the same day stay as history. ``day`` is a local calendar date
    (``YYYY-MM-DD``). The summary is derived: ``records`` stays the source of truth.
    """
    summary_id = f"day-summary:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, "
            "input_chars, output_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                summary_id,
                day,
                iso_seconds(local_now()),
                summary,
                model,
                record_count,
                input_chars,
                output_chars,
            ),
        )
    return summary_id


def latest_day_summaries(
    conn: sqlite3.Connection, first_day: str, last_day: str,
) -> list[tuple[str, str]]:
    """``(day, summary)`` of the current row of each day in ``[first_day, last_day]``, by day."""
    rows = conn.execute(
        "SELECT day, summary FROM day_summaries WHERE day >= ? AND day <= ? "
        "ORDER BY day, rowid",
        (first_day, last_day),
    ).fetchall()
    return list(dict(rows).items())  # a later row of the same day replaces the earlier one


def pending_days(path: Path, today: date) -> list[tuple[str, int, int]]:
    """Local dates before ``today`` with records and no summary row, oldest first.

    Each is ``(day, record_count, input_chars)``. The date is the record's ``ts`` on this
    Mac's zone, not the offset it was stored with.
    """
    days: dict[str, list[int]] = {}
    with closing(open_memory_db(path)) as conn:
        done = {str(row[0]) for row in conn.execute("SELECT DISTINCT day FROM day_summaries")}
        for ts, size in conn.execute("SELECT ts, length(text) FROM records"):
            day = datetime.fromisoformat(ts).astimezone().date()
            if day < today and (key := day.isoformat()) not in done:
                tally = days.setdefault(key, [0, 0])
                tally[0] += 1
                tally[1] += int(size)
    return [(day, days[day][0], days[day][1]) for day in sorted(days)]


def day_records(path: Path, day: str) -> tuple[Record, ...]:
    """Every record whose local date is ``day``, oldest first, full text."""
    start = datetime.fromisoformat(day).astimezone()
    after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    end = datetime.fromisoformat(after).astimezone()
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT id, ts, source, text FROM records "
            "WHERE datetime(ts) >= datetime(?) AND datetime(ts) < datetime(?) "
            "ORDER BY datetime(ts), rowid",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return tuple((str(rid), str(ts), str(source), str(text)) for rid, ts, source, text in rows)


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "CompactionRange",
    "MemoryContext",
    "MemorySettings",
    "Record",
    "SessionSettings",
    "VerbatimStats",
    "append_day_summary",
    "append_nightly_core_memory",
    "append_record",
    "append_summary",
    "brief_note",
    "compaction_range",
    "core_memory_pending_days",
    "current_core_memory",
    "day_records",
    "iso_seconds",
    "latest_day_summaries",
    "local_now",
    "open_memory_db",
    "pending_days",
    "remember_fact",
    "render_context",
    "search_records",
    "verbatim_stats",
]
