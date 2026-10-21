"""L2 memory store — utterances, answers, core memory, and history summaries.

One standalone SQLite file (``memory.db``), deliberately separate from the
runtime Event Log so the runtime can be rewritten without touching it.
Append-only: rows are never updated or deleted, except ``places`` (ADR 0234), the current
view of the places he keeps beside its append-only ``place_changes`` log. The user's lasting
facts are ``core_memory`` versions (ADR 0146); the legacy ``profile`` table is read once, to
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

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.shared import lang
from jarvis.shared.geo import metres
from jarvis.shared.html_tags import strip_html
from jarvis.state import NewerDataError, core_memory, imports

if TYPE_CHECKING:
    from jarvis.state.attachments import AttachmentRef, Attachments

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    source     TEXT NOT NULL,
    text       TEXT NOT NULL,
    audio_path TEXT
);
CREATE TABLE IF NOT EXISTS record_attachments (
    record_id     TEXT NOT NULL REFERENCES records(id),
    pos           INTEGER NOT NULL,
    attachment_id TEXT NOT NULL,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    PRIMARY KEY (record_id, pos)
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
CREATE TABLE IF NOT EXISTS day_prose (
    id           TEXT PRIMARY KEY,
    day          TEXT NOT NULL,
    ts           TEXT NOT NULL,
    text         TEXT NOT NULL,
    model        TEXT,
    input_chars  INTEGER NOT NULL,
    output_chars INTEGER NOT NULL,
    line         TEXT,
    highlights   TEXT,
    draft        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS day_experience (
    local_date   TEXT PRIMARY KEY,
    metrics_json TEXT NOT NULL,
    computed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_highlight_marks (
    id         TEXT PRIMARY KEY,
    day        TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    "on"       INTEGER NOT NULL,
    label      TEXT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS doing_runs (
    id              TEXT PRIMARY KEY,
    day             TEXT NOT NULL,
    draft           INTEGER NOT NULL,
    written_at      TEXT NOT NULL,
    covers_until_ms INTEGER NOT NULL,
    model           TEXT,
    inputs_digest   TEXT NOT NULL,
    spans           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS doing_runs_day ON doing_runs (day);
CREATE TABLE IF NOT EXISTS doing_answers (
    id       TEXT PRIMARY KEY,
    day      TEXT NOT NULL,
    span_id  TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER NOT NULL,
    choice   TEXT,
    text     TEXT,
    lat      REAL,
    lng      REAL,
    place_id TEXT,
    origin   TEXT NOT NULL,
    ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS doing_answers_day ON doing_answers (day);
CREATE TABLE IF NOT EXISTS attention_threads (
    id       TEXT PRIMARY KEY,
    key      TEXT NOT NULL,
    thread   TEXT,
    category TEXT,
    origin   TEXT NOT NULL,
    model    TEXT,
    day      TEXT NOT NULL,
    ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attention_threads_key ON attention_threads (key);
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
CREATE TABLE IF NOT EXISTS places (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    lat        REAL,
    lng        REAL,
    radius_m   INTEGER NOT NULL,
    address    TEXT,
    origin     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS place_changes (
    id       TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,
    origin   TEXT NOT NULL,
    op       TEXT NOT NULL,
    place_id TEXT NOT NULL,
    before   TEXT,
    after    TEXT
);
CREATE INDEX IF NOT EXISTS place_changes_place ON place_changes (place_id);
CREATE TABLE IF NOT EXISTS place_prompts (
    id       TEXT PRIMARY KEY,
    lat      REAL NOT NULL,
    lng      REAL NOT NULL,
    shown_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meaning_vectors (
    kind   TEXT NOT NULL,
    ref    TEXT NOT NULL,
    piece  INTEGER NOT NULL,
    model  TEXT NOT NULL,
    digest TEXT NOT NULL,
    vec    BLOB NOT NULL,
    PRIMARY KEY (kind, ref, piece)
);
""" + imports.IMPORT_ENTRIES_DDL + """CREATE TABLE IF NOT EXISTS voiceprints (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    device  TEXT NOT NULL,
    mic     TEXT NOT NULL,
    ts      TEXT NOT NULL,
    seconds REAL NOT NULL,
    weight  REAL NOT NULL,
    seed    INTEGER NOT NULL,
    turn_id TEXT UNIQUE,
    vec     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS voiceprints_mic ON voiceprints (device, mic, id);
"""

# ``PRAGMA user_version`` (ADR 0068). 0 is a file from before the stamp, same
# schema as 1; a file above this was written by a newer Jarvis and is refused.
# ``sent``, ``record_attachments`` (ADR 0255), ``day_summaries``, ``day_experience`` (ADR 0280),
# ``core_memory``, ``day_highlight_marks``, ``doing_runs`` and ``doing_answers`` (ADR 0302),
# ``attention_threads`` (ADR 0331), the
# places tables (ADR 0234), ``import_entries``
# (ADR 0259), ``meaning_vectors`` (ADR 0260) and ``voiceprints`` (ADR 0286) are additive: an
# older opener ignores them, so they need no new version. So are the
# ``day_prose`` columns ``line``, ``highlights`` and ``draft`` (ADR 0235), which
# :func:`open_memory_db` adds to a table made before.
SCHEMA_VERSION: Final[int] = 1
DEFAULT_SEARCH_LIMIT: Final[int] = 20
# ``session.context`` value for ADR 0149's history layout; anything else is the rolling summary.
DAY_SUMMARIES_CONTEXT: Final[str] = "day_summaries"
# ADR 0149: the cap on the raw history cuts the oldest records in blocks of this many.
_RAW_CUT_BLOCK: Final[int] = 50
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
# The line the program puts after an answer whose turn ran tools (ADR 0267); an answer that
# copies it is not what she did, so it is cut from what is shown and replayed.
_DID_MARK: Final = "[You did: "
_DID_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]*\[You did: [^\]\n]*\]")

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
    (docs/plans/replay-as-sent-proposal.md). ``context`` ``day_summaries`` (ADR 0149)
    swaps the rolling summary and the recent window for the latest day summaries and
    every record from the day after the newest one, capped at ``context_raw_max_chars``.
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
    context: str = "rolling"
    context_raw_max_chars: int = 40000

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
            context=(
                DAY_SUMMARIES_CONTEXT
                if values.get("context") == DAY_SUMMARIES_CONTEXT
                else defaults.context
            ),
            context_raw_max_chars=int(
                _positive("context_raw_max_chars", defaults.context_raw_max_chars),
            ),
        )


# ADR 0201: (now, the last record's time) -> (the text that follows the core memory, the text that
# follows the time line).
Ledger = Callable[[datetime, datetime | None], tuple[str, str]]
# ADR 0234: now -> the ``[Places he keeps]`` block, "" when none is kept.
PlacesBlock = Callable[[datetime], str]


class MemoryContext(NamedTuple):
    """The prompt blocks rendered from memory.db for one turn."""

    profile: str  # [About the user] block (core memory) for the system prompt; "" when empty
    # summary, then one message per record, by role; a message of his with pictures (ADR 0255)
    # has a list of parts for its content, any other has text
    history: tuple[dict[str, Any], ...]
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
    _add_day_prose_columns(conn)
    if found < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


# The columns ADR 0235 added to ``day_prose``, as appended to a table made before them.
_DAY_PROSE_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("line", "TEXT"), ("highlights", "TEXT"), ("draft", "INTEGER NOT NULL DEFAULT 0"),
)


def _add_day_prose_columns(conn: sqlite3.Connection) -> None:
    """Add the ADR 0235 columns to a ``day_prose`` that lacks them."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(day_prose)")}
    for column, declared in _DAY_PROSE_COLUMNS:
        if column in have:
            continue
        try:
            conn.execute(f"ALTER TABLE day_prose ADD COLUMN {column} {declared}")
        except sqlite3.OperationalError:
            if column not in {row[1] for row in conn.execute("PRAGMA table_info(day_prose)")}:
                raise  # not a second opener that added it first


_NAME_LINE: Final[tuple[str, str]] = ("The user's name is ", ".")
_FACT_TOPIC_CHARS: Final[int] = 40


def _keep_item(  # noqa: PLR0913 — the version's inputs, all keyword-only.
    path: Path,
    *,
    topic: str,
    text: str,
    section: str | None,
    origin: str,
    only_new: bool = False,
) -> bool:
    """Append a core-memory version that keeps ``text`` under ``topic`` (ADR 0146).

    ``only_new`` writes nothing when an item already says exactly ``text``. Whether a version
    was written.
    """
    now = local_now()
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.current(conn, iso_seconds(now))
        if only_new and any(item["text"] == text for _, item, _ in core_memory.listing(base.doc)):
            return False
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
    return True


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

    The old wording stays on the item as history. A new topic goes into ``section`` (default: 经历);
    a section lists pinned items first, then the newest.
    """
    _keep_item(path, topic=topic, text=f"{topic}: {fact}", section=section, origin="remember")


def keep_fact(
    path: Path, text: str, topic: str | None = None, section: str | None = None,
) -> bool:
    """Keep ``text`` in core memory as ``remember_fact`` does; False when it is there already.

    With a ``topic`` the item reads ``topic: text`` and replaces the one of that topic. Without,
    the item is ``text`` alone and its topic is its first words, so only the same text matches.
    """
    return _keep_item(
        path,
        topic=topic or text[:_FACT_TOPIC_CHARS],
        text=f"{topic}: {text}" if topic else text,
        section=section,
        origin="remember",
        only_new=True,
    )


def current_core_memory(path: Path) -> core_memory.Version:
    """The current core memory version (migrating ``profile`` on the first read)."""
    with closing(open_memory_db(path)) as conn:
        return core_memory.current(conn, iso_seconds(local_now()))


def due_items(path: Path) -> list[core_memory.Item]:
    """The items the latest core memory version still holds under 承诺和待办 (ADR 0299).

    Read raw: the section is no longer one, so :func:`current_core_memory` leaves them out. The
    one-time carry-in into the ledger reads them here. Nothing is written.
    """
    with closing(open_memory_db(path)) as conn:
        row = conn.execute("SELECT doc FROM core_memory ORDER BY rowid DESC LIMIT 1").fetchone()
    return [] if row is None else core_memory.retired_items(row[0])


def drop_due_section(path: Path) -> bool:
    """Write a version without 承诺和待办 if the latest still has it; whether one was written.

    The carry-in has taken its items into the ledger (ADR 0299); every other section is kept as
    it was. Once the latest version lacks the section this writes nothing.
    """
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.latest(conn)
        if base is None:
            return False
        (raw,) = conn.execute("SELECT doc FROM core_memory WHERE id = ?", (base.id,)).fetchone()
        if core_memory.DUE_SECTION not in json.loads(raw)["sections"]:
            return False
        core_memory.append_version(
            conn, base=base, doc=base.doc, origin="migration", upto_day=base.upto_day,
            changes=[], now=now,
        )
    return True


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


def append_nightly_core_memory(  # noqa: PLR0913 — the row's inputs, all keyword-only.
    path: Path,
    *,
    base_id: str,
    day: str,
    changes: Sequence[Mapping[str, object]],
    review_log: Sequence[Mapping[str, object]] = (),
    notes: Sequence[Mapping[str, object]] = (),
) -> str | None:
    """Append the nightly version for ``day`` and return its id, or None when the world moved.

    The change list applies to the current document only if ``base_id`` is still the current
    version (the item numbers it cites are that version's); otherwise nothing is stored. An
    empty list appends the same document, which advances ``upto_day``. ``review_log`` are
    Jev review entries (ADR 0146) and ``notes`` the pin gate kept out of ``changes`` (ADR 0154):
    both stored after ``changes`` in the row, never applied.
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
            changes=[*changes, *review_log, *notes],
            now=now,
        )


def append_record(  # noqa: PLR0913 — the row's columns, all keyword-only.
    path: Path,
    *,
    record_id: str,
    source: str,
    text: str,
    audio_path: str | None = None,
    attachments: Sequence[AttachmentRef] = (),
) -> None:
    """Append one record; a repeated ``record_id`` (retry) is a no-op.

    ``attachments`` are the files that came with his words (ADR 0255): linked to the record in
    the same write, so history and the device API can find them by the record.
    """
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO records (id, ts, source, text, audio_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (record_id, iso_seconds(local_now()), source, text, audio_path),
        )
        _link(conn, record_id, attachments)


def _link(conn: sqlite3.Connection, record_id: str, refs: Sequence[AttachmentRef]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO record_attachments (record_id, pos, attachment_id, kind, name) "
        "VALUES (?, ?, ?, ?, ?)",
        [(record_id, pos, ref.id, ref.kind, ref.name) for pos, ref in enumerate(refs)],
    )


def link_attachments(path: Path, record_id: str, refs: Sequence[AttachmentRef]) -> None:
    """Link files to a record already written (ADR 0255 backfill); nothing when it is not there."""
    with closing(open_memory_db(path)) as conn, conn:
        if conn.execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone():
            _link(conn, record_id, refs)


def recent_image_ids(path: Path, limit: int) -> list[str]:
    """The ids of the pictures sent most recently, newest first (ADR 0255)."""
    with closing(open_memory_db(path)) as conn:
        return [
            str(a) for (a,) in conn.execute(
                "SELECT a.attachment_id FROM record_attachments a "
                "JOIN records r ON r.id = a.record_id WHERE a.kind = 'image' "
                "ORDER BY r.rowid DESC, a.pos DESC LIMIT ?",
                (limit,),
            )
        ]


def linked_attachment_ids(path: Path) -> set[str]:
    """The ids of every file some record is linked to."""
    with closing(open_memory_db(path)) as conn:
        return {str(a) for (a,) in conn.execute("SELECT attachment_id FROM record_attachments")}


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
    return core_memory.render(version.doc, _item_notes(conn, version.doc)).splitlines()


def _item_notes(conn: sqlite3.Connection, doc: core_memory.Doc) -> dict[str, str]:
    """Per item, ``first seen, kind[, pinned]``.

    First seen is the date of the earliest record it cites, else its ``since``. Kind is ``said``
    when all its records are the user's words, ``mail``, ``observed`` when none is,
    ``said+observed`` for a mix, ``set`` when no record backs it.
    """
    cited = {s for items in doc.values() for item in items for s in item.get("sources") or []}
    records = {
        str(rid): (str(source), datetime.fromisoformat(str(ts)).astimezone().date().isoformat())
        for rid, source, ts in conn.execute(
            "SELECT id, source, ts FROM records WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(cited)),),
        )
    }
    notes: dict[str, str] = {}
    for items in doc.values():
        for item in items:
            found = [records[s] for s in item.get("sources") or [] if s in records]
            kinds = {source for source, _ in found}
            kind = (
                "set" if not found
                else "said" if kinds == {"allen"}
                else "mail" if "mail" in kinds
                else "observed" if "allen" not in kinds
                else "said+observed"
            )
            first = min(day for _, day in found) if found else str(item.get("since", "?"))
            pinned = ", pinned" if item.get("pinned") else ""
            notes[item.get("id", "")] = f"{first}, {kind}{pinned}"
    return notes


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
    """A record's text without the retired envelope tags, stray HTML, a copied label or marker."""
    text = _COPIED_LABEL_RE.sub("", text)
    if _DID_MARK in text:
        text = _DID_RE.sub("", text).strip()
    return strip_html(_ENVELOPE_TAG_RE.sub("", text)).strip() if "<" in text else text


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


def _append_turn(
    turns: list[dict[str, Any]], role: str, content: str | list[dict[str, Any]],
) -> None:
    """Add one message; a repeated role joins the previous message instead."""
    if not (turns and turns[-1]["role"] == role):
        turns.append({"role": role, "content": content})
        return
    before = turns[-1]["content"]
    if isinstance(before, str) and isinstance(content, str):
        turns[-1]["content"] = f"{before}\n{content}"
        return
    parts = [{"type": "text", "text": before}] if isinstance(before, str) else list(before)
    parts += [{"type": "text", "text": content}] if isinstance(content, str) else content
    turns[-1]["content"] = parts


def _hidden_records(shown: int, recent: int) -> int:
    """How many of the oldest ``shown`` records a ``recent`` window leaves out.

    The cut moves ``recent`` records at a time, so between ``recent`` and
    ``2 * recent - 1`` records show and the history's start, which the
    provider's prefix cache needs unchanged, holds for ``recent`` records.
    """
    if recent <= 0 or shown < 2 * recent:
        return 0
    return (shown - recent) // recent * recent


def _day_head(conn: sqlite3.Connection) -> tuple[date, str] | None:
    """ADR 0149: the boundary day and the summaries message's text, or None with no summary.

    The boundary is the day after the newest local day with a day summary; the message is the
    current rows of the three latest days, oldest first, each under the heading it was stored with.
    """
    days = [
        str(day)
        for (day,) in conn.execute(
            "SELECT DISTINCT day FROM day_summaries ORDER BY day DESC LIMIT 3",
        )
    ]
    if not days:
        return None
    rows = latest_day_summaries(conn, days[-1], days[0])
    head = (
        "[Earlier days · summaries only · for exact words, numbers or details of these or "
        "older days use conversation]"
    )
    boundary = date.fromisoformat(days[0]) + timedelta(days=1)
    return boundary, "\n\n".join([head, *(summary for _, summary in rows)])


def _raw_cut(sizes: Sequence[int], max_chars: int) -> int:
    """How many of the oldest records to hide so the rest fit ``max_chars``, in blocks of 50."""
    cut, total = 0, sum(sizes)
    while total > max_chars and cut + _RAW_CUT_BLOCK < len(sizes):
        total -= sum(sizes[cut : cut + _RAW_CUT_BLOCK])
        cut += _RAW_CUT_BLOCK
    return cut


HISTORY_PICTURES: Final[int] = 20
"""The most pictures history carries as pictures (ADR 0255): the newest of the window."""

PictureFileId = Callable[[str], str | None]
"""ADR 0255: an attachment id -> the id its picture has at the provider, or ``None`` when it
cannot ride (the file is gone, or could not be sent). It may upload."""

DidOf = Callable[[Sequence[str]], Mapping[str, str]]
"""ADR 0267: answer record ids -> what each answer's turn did, for those whose turn ran a tool."""


def _image_links(conn: sqlite3.Connection, anchor: int) -> dict[str, list[str]]:
    """Record id -> the ids of the pictures sent with it, for the records after ``anchor``."""
    links: dict[str, list[str]] = {}
    for record_id, attachment_id in conn.execute(
        "SELECT a.record_id, a.attachment_id FROM record_attachments a "
        "JOIN records r ON r.id = a.record_id WHERE r.rowid > ? AND a.kind = 'image' "
        "ORDER BY r.rowid, a.pos",
        (anchor,),
    ):
        links.setdefault(str(record_id), []).append(str(attachment_id))
    return links


def _riding(
    shown: Sequence[Record], links: Mapping[str, Sequence[str]], file_id: PictureFileId,
) -> dict[str, list[tuple[str, str]]]:
    """Record id -> (attachment id, file id) of the pictures that ride, the newest 20 in all."""
    riding: dict[str, list[tuple[str, str]]] = {}
    left = HISTORY_PICTURES
    for record_id, *_ in reversed(shown):
        for attachment_id in reversed(links.get(record_id, ())):
            if left and (found := file_id(attachment_id)) is not None:
                riding.setdefault(record_id, []).insert(0, (attachment_id, found))
                left -= 1
    return riding


def _append_records(
    turns: list[dict[str, Any]], shown: Sequence[Record], sent: Mapping[str, str],
    riding: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    did: Mapping[str, str] | None = None,
) -> None:
    """Each record as its message: as sent when kept, else its words, a day marker on a new day.

    An answer in ``did`` is followed by ``[You did: ...]`` on a line of its own (ADR 0267).

    A record of his with pictures in ``riding`` carries them after his words as ``image_file``
    parts; the note of a picture that rides is left out, since the picture says more (ADR 0255).
    """
    marked_day = None
    ridden = {a for pictures in (riding or {}).values() for a, _ in pictures}
    for record_id, ts, source, text in shown:
        if source == PICTURE_NOTE_SOURCE and record_id.removeprefix(NOTE_ID_PREFIX) in ridden:
            continue
        role = history_role(source)
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
        pictures = (riding or {}).get(record_id)
        _append_turn(
            turns, role,
            content if not pictures else [
                {"type": "text", "text": content},
                *({"type": "image_file", "file_id": found} for _, found in pictures),
            ],
        )
        if did and record_id in did:
            _append_turn(turns, role, f"{_DID_MARK}{did[record_id]}]")


def render_context(  # noqa: PLR0913 — one read, two layouts, one render.
    path: Path, *, exclude_id: str, since: str = "", now: datetime | None = None,
    recent: int = 0, context: str = "rolling", raw_max_chars: int = 40000,
    ledger: Ledger | None = None, places: PlacesBlock | None = None,
    pictures: PictureFileId | None = None, did: DidOf | None = None,
    also_exclude: Collection[str] = (),
) -> MemoryContext:
    """Render the decision-path prompt blocks in one consistent read.

    ``profile`` (the rendered core memory) goes to the system prompt. ``history`` is the
    current summary (if any) as a ``user`` message, then one message per record after its
    anchor and on or after ``since``, in the role :func:`history_role` gives its source
    (his words and picture notes ``user``, the rest ``assistant``); adjacent rows of one role
    join into one message. Records carry their words only (ADR 0044): no timestamp or
    source label, which the model copied into its answers; the first
    ``user`` row of each day opens with a day marker (``[9月24日 周四]`` /
    ``[Thursday, September 24]``, in the language setting). A row with a
    ``sent`` message is replayed exactly as its turn sent it, state block
    and all, without a marker. It only grows at its end between
    compactions, so the provider's prefix cache covers it. ``now`` is the
    per-turn time line. ``exclude_id`` is the current turn's own utterance,
    which the prompt already carries as the live user message; ``also_exclude`` (ADR 0271) are
    the earlier rows whose words that message holds too. ``recent`` > 0
    shows only the latest records (see :func:`_hidden_records`); a note after
    the summary says how many earlier ones there are and how to find them.

    ``context`` ``day_summaries`` (ADR 0149) replaces the summary and ``recent`` with the
    latest three day summaries, a line naming the boundary day, and every record from that day
    on, the oldest hidden in blocks of 50 while they exceed ``raw_max_chars``. With no day
    summary stored it renders the rolling layout.

    ``ledger`` (ADR 0201) is asked, with the same ``now`` and the time of the last record, for
    the text that follows the core memory in ``profile`` and the text that follows the time line
    in ``now``.

    ``places`` (ADR 0234) is asked for the ``[Places he keeps]`` block, which follows the core
    memory ahead of the ledger's text.

    ``pictures`` (ADR 0255) says which pictures ride the history as pictures: the newest
    :data:`HISTORY_PICTURES` in the window for which it returns a file id. Without it history
    is words only, as before.

    ``did`` (ADR 0267) is asked, once, for the answers shown; each answer whose turn ran tools
    is followed by a ``[You did: ...]`` line of its own, in the same ``assistant`` message.
    """
    moment = now or local_now()
    with closing(open_memory_db(path)) as conn:
        profile = _core_memory_lines(conn)
        day_head = _day_head(conn) if context == DAY_SUMMARIES_CONTEXT else None
        current = None if day_head else _current_summary(conn)
        anchor = _effective_anchor(conn, current.anchor_rowid if current else None, since)
        if day_head:
            start = datetime.combine(day_head[0], time.min).astimezone()
            anchor = _effective_anchor(conn, anchor, iso_seconds(start))
        records = _records_after(conn, anchor)
        sent = dict(
            conn.execute(
                "SELECT s.record_id, s.text FROM sent s JOIN records r ON r.id = s.record_id "
                "WHERE r.rowid > ?",
                (anchor,),
            ).fetchall(),
        )
        last = conn.execute(
            "SELECT ts FROM records WHERE id != ? ORDER BY rowid DESC LIMIT 1", (exclude_id,),
        ).fetchone()
        links = _image_links(conn, anchor) if pictures is not None else {}
    profile_block = "\n".join(["[About the user]", *profile]) if profile else ""
    profile_block = "\n\n".join(
        part for part in (profile_block, "" if places is None else places(moment)) if part
    )
    turns: list[dict[str, Any]] = []
    if day_head:
        _append_turn(turns, "user", day_head[1])
        _append_turn(
            turns,
            "user",
            f"[From {day_head[0].isoformat()} 00:00 on, every word said is below; before that "
            "only the summaries above and the core memory]",
        )
    elif current is not None:
        _append_turn(
            turns,
            "user",
            f"[Conversation summary · up to {current.anchor_ts} · look up exact wording, "
            "numbers and agreement with conversation(open=<record_id>), or find a "
            "record with conversation(query=...)]\n"
            f"{current.summary}",
        )
    shown = [
        record for record in records if record[0] != exclude_id and record[0] not in also_exclude
    ]
    if day_head:
        hidden = _raw_cut([len(sent.get(r[0]) or _plain(r[3])) for r in shown], raw_max_chars)
    else:
        hidden = _hidden_records(len(shown), recent)
    if hidden:
        first_ts = shown[hidden][1]
        where = "use conversation" if day_head else (
            "find them with conversation(query=...) or conversation(date=...), then "
            "conversation(open=<id>)"
        )
        _append_turn(
            turns,
            "user",
            f"[Earlier conversation · {hidden} records before {first_ts} are not shown · {where}]",
        )
        shown = shown[hidden:]
    riding = _riding(shown, links, pictures) if pictures is not None else None
    done = did([r[0] for r in shown if r[2] == "jarvis"]) if did is not None else None
    _append_records(turns, shown, sent, riding, done)
    last_ts = shown[-1][1] if shown else None
    if not shown:
        if day_head and last:
            last_ts = str(last[0])
        elif current:
            last_ts = current.anchor_ts
    now_line = _now_line(moment, last_ts)
    if ledger is not None:
        last = None if last_ts is None else datetime.fromisoformat(last_ts)
        standing, per_turn = ledger(moment, last)
        profile_block = "\n\n".join(part for part in (profile_block, standing) if part)
        now_line = "\n\n".join(part for part in (now_line, per_turn) if part)
    return MemoryContext(profile_block, tuple(turns), now_line)


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

    The records are read newest first and no further than the budget keeps them, so the cost
    follows the size of the brief, not the length of the conversation.
    """
    moment = now or local_now()
    record_lines: list[str] = []
    with closing(open_memory_db(path)) as conn:
        profile = _core_memory_lines(conn)
        current = _current_summary(conn)
        cursor = conn.execute(
            "SELECT id, ts, source, text FROM records WHERE rowid > ? ORDER BY rowid DESC",
            (-1 if current is None else current.anchor_rowid,),
        )
        newest = cursor.fetchone()
        summary_head = (
            [
                f"[Conversation summary · up to {current.anchor_ts} · "
                "ask the backend for exact words]",
            ]
            if current
            else []
        )
        sections = _summary_sections(current.summary) if current else []
        now_line = _now_line(moment, None if newest is None else str(newest[1]))

        def _head() -> list[str]:
            return [
                "[About the user]", *profile, now_line, *summary_head, *sections,
                "[Conversation records, oldest first]",
            ]

        # The newest records that fit beside the head; the first that does not ends the read.
        used = len("\n".join(_head()))
        for _, ts, source, text in () if newest is None else chain((newest,), cursor):
            line = f"[{ts}] {_USER_LABEL if source == 'allen' else source}: {text}"
            used += 1 + len(line)
            if used > max_chars:
                break
            record_lines.append(line)
    record_lines.reverse()

    def _assemble() -> str:
        return "\n".join([*_head(), *(record_lines or ["(none)"])])

    while len(_assemble()) > max_chars:  # only the head is left to cut
        if sections:
            sections.pop()
        elif len(profile) > 2:  # noqa: PLR2004 — a section heading and its first item stay
            profile.pop()
            if profile[-1].startswith("### "):
                profile.pop()
        else:
            break
    return _assemble()


_ATTACHED: Final = "[attached:"
"""How the marker line of a record that came with files begins (``attachments.marker``)."""
PICTURE_NOTE_SOURCE: Final = "picture_note"
"""The source of a picture's note (ADR 0223): memory, never part of what was said."""
NOTE_ID_PREFIX: Final = "picture-note:"
"""A note's record id is this and the picture's attachment id."""
ALLEN_REACTION_SOURCE: Final = "allen_reaction"
JARVIS_REACTION_SOURCE: Final = "jarvis_reaction"
"""The sources of a reaction's line (ADR 0257): one for each side, because a line is read in the
role of whoever reacted. Neither is ever a row of the conversation window."""
_NOT_MESSAGES: Final = (PICTURE_NOTE_SOURCE, ALLEN_REACTION_SOURCE, JARVIS_REACTION_SOURCE)
_NOT_MESSAGES_SQL: Final = f"source NOT IN ({', '.join('?' * len(_NOT_MESSAGES))})"


def history_role(source: str) -> str:
    """The prompt-history role of a record from ``source``.

    His words are ``user``, and so are a picture's note and his reactions: the note is context
    about his picture, and as ``assistant`` the model took it for words of its own and copied its
    header. Every other source is ``assistant``, her reactions included.
    """
    return "user" if source in ("allen", PICTURE_NOTE_SOURCE, ALLEN_REACTION_SOURCE) else (
        "assistant"
    )


def message_record(path: Path, record_id: str) -> tuple[str, str] | None:
    """``(source, words)`` of a record a reaction can sit on: his or her message, else ``None``.

    Mail, a picture's note and reaction lines are not messages. Words are the record's text
    without legacy markup, as a conversation row shows it.
    """
    with closing(open_memory_db(path)) as conn:
        row = conn.execute(
            "SELECT source, text FROM records WHERE id = ? AND source IN ('allen', 'jarvis')",
            (record_id,),
        ).fetchone()
    return None if row is None else (str(row[0]), _plain(str(row[1])))

CONVERSATION_PAGE_BYTES = 512 * 1024
"""The most text one page of :func:`conversation_rows` carries (UTF-8 bytes), one row at least."""


def _his_words(text: str) -> str:
    """His words of a record that came with files: no marker line, no filler (ADR 0255)."""
    body, _, last = text.rpartition("\n")
    if last.startswith(_ATTACHED):
        text = body
    return "" if lang.is_filler(text) else text


def _attachment_rows(
    conn: sqlite3.Connection, record_ids: Sequence[str], store: Attachments | None,
) -> dict[str, list[dict[str, object]]]:
    """Record id -> ``{id, kind, name, gone}`` of the files sent with it, in the order sent."""
    found: dict[str, list[dict[str, object]]] = {}
    for record_id, attachment_id, kind, name in conn.execute(
        "SELECT record_id, attachment_id, kind, name FROM record_attachments "
        "WHERE record_id IN (SELECT value FROM json_each(?)) ORDER BY record_id, pos",
        (json.dumps(list(record_ids)),),
    ):
        found.setdefault(str(record_id), []).append({
            "id": str(attachment_id), "kind": str(kind), "name": str(name),
            "gone": store is None or store.get(str(attachment_id)) is None,
        })
    return found


def conversation_rows(  # noqa: PLR0913 — the page's cursor and limits, all keyword-only.
    path: Path, *, since: str = "", after: int = 0, before: int = 0, limit: int = 200,
    attachments: Attachments | None = None,
) -> tuple[list[dict[str, object]], bool]:
    """A page of records for the conversation window, oldest first, each with its ``seq``.

    Picture notes (:data:`PICTURE_NOTE_SOURCE`) are memory, not speech, so they are left out,
    and so are the lines of reactions (ADR 0257), which devices draw on the message instead.

    Each row of his (``source`` ``allen``) carries ``attachments``, the files that came with it
    (``{id, kind, name, gone}``; ``gone`` when ``attachments``, the store, no longer holds the
    file). When it has any, its ``text`` is his words alone: the marker line and the filler the
    program wrote for an attachment-only turn are left out (ADR 0255).

    ``after`` is the ``seq`` (rowid) the client already holds: the rows past it, still under the
    floor. ``before`` is the ``seq`` of its oldest row: the newest ``limit`` rows older than it,
    past the floor back to the first record. With neither, the newest ``limit`` rows from
    ``since``, the floor the prompt's history uses. A page holds at most
    :data:`CONVERSATION_PAGE_BYTES` of text, and at least one row.

    Returns ``(rows, has_more)``: ``has_more`` says rows exist beyond the page in the direction
    it was read (past it for ``after``; older than its first row otherwise, whether or not they
    are under the floor).
    """
    limit = max(1, limit)
    clauses = [_NOT_MESSAGES_SQL]
    params: list[object] = [*_NOT_MESSAGES]
    if after:
        clauses.append("rowid > ?")
        params.append(after)
    elif before:
        clauses.append("rowid < ?")
        params.append(before)
    if since and not before:
        clauses.append("datetime(ts) >= datetime(?)")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ORDER BY rowid" if after else "ORDER BY rowid DESC"
    params.append(limit + 1)
    with closing(open_memory_db(path)) as conn:
        # S608: `where` / `order` are assembled from fixed literals; every value is bound.
        sql = f"SELECT rowid, id, ts, source, text FROM records {where} {order} LIMIT ?"  # noqa: S608
        fetched = conn.execute(sql, params).fetchall()
        rows: list[dict[str, object]] = []
        used, last = 0, before or None
        sent = _attachment_rows(
            conn, [str(rid) for _, rid, _, source, _ in fetched[:limit] if source == "allen"],
            attachments,
        )
        for seq, rid, ts, source, text in fetched[:limit]:
            plain = _plain(str(text))
            files = sent.get(str(rid), []) if source == "allen" else None
            if files:
                plain = _his_words(plain)
            used += len(plain.encode())
            if rows and used > CONVERSATION_PAGE_BYTES:
                break
            row: dict[str, object] = {
                "seq": int(seq), "id": str(rid), "ts": str(ts), "source": str(source),
                "text": plain,
            }
            if files is not None:
                row["attachments"] = files
            rows.append(row)
            last = int(seq)
        if after:
            return rows, len(rows) < len(fetched)
        rows.reverse()
        # Older than the page's first row (the last read, else the cursor), under the floor or
        # not; an empty default page asks whether there is any row.
        probe = conn.execute(
            f"SELECT 1 FROM records WHERE {_NOT_MESSAGES_SQL} "  # noqa: S608 - fixed literal
            "AND (? IS NULL OR rowid < ?) LIMIT 1",
            (*_NOT_MESSAGES, last, last),
        ).fetchone()
        return rows, probe is not None


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


def append_day_prose(  # noqa: PLR0913 — the row's columns, all required but the line.
    path: Path,
    *,
    day: str,
    text: str,
    model: str,
    input_chars: int,
    output_chars: int,
    line: str | None = None,
    highlights: Sequence[Mapping[str, object]] = (),
) -> str:
    """Append one day's "The day" line (ADR 0201), return its id; a day's latest row is current.

    ``line`` and ``highlights`` (ADR 0235) are the night's one sentence to him and her picks, kept
    in the same row. A row without a ``line`` leaves the day's last draft as its line.
    """
    prose_id = f"day-prose:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO day_prose "
            "(id, day, ts, text, model, input_chars, output_chars, line, highlights) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prose_id, day, iso_seconds(local_now()), text, model, input_chars, output_chars,
                line, None if line is None else json.dumps(list(highlights), ensure_ascii=False),
            ),
        )
    return prose_id


def append_day_draft(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    day: str,
    line: str,
    highlights: Sequence[Mapping[str, object]],
    model: str,
    input_chars: int,
    output_chars: int,
) -> str:
    """Append a draft of a day still under way (ADR 0235): its line and picks, no "The day" text.

    A draft is not prose: :func:`prose_days` and the ledger skip it, so it never stops the night's
    row from being written, and that row's line replaces it.
    """
    draft_id = f"day-draft:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO day_prose "
            "(id, day, ts, text, model, input_chars, output_chars, line, highlights, draft) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, 1)",
            (
                draft_id, day, iso_seconds(local_now()), model, input_chars, output_chars, line,
                json.dumps(list(highlights), ensure_ascii=False),
            ),
        )
    return draft_id


def prose_days(path: Path) -> set[str]:
    """The local days that already have a "The day" line; a draft (ADR 0235) is not one."""
    with closing(open_memory_db(path)) as conn:
        return {
            str(day)
            for (day,) in conn.execute("SELECT DISTINCT day FROM day_prose WHERE draft = 0")
        }


def last_draft_at(path: Path, day: str) -> datetime | None:
    """When the newest draft of ``day`` was written, else None."""
    with closing(open_memory_db(path)) as conn:
        row = conn.execute(
            "SELECT ts FROM day_prose WHERE day = ? AND draft = 1 ORDER BY rowid DESC LIMIT 1",
            (day,),
        ).fetchone()
    return None if row is None else datetime.fromisoformat(row[0])


class DayLineRow(NamedTuple):
    """The current line of a day (ADR 0235); its picks are ``{"id", "label", "start_ms"}``."""

    line: str
    highlights: list[dict[str, Any]]
    draft: bool
    written_at: str


def latest_day_lines(
    conn: sqlite3.Connection, first_day: str, last_day: str,
) -> dict[str, DayLineRow]:
    """The newest row that carries a line, of each day in ``[first_day, last_day]``.

    A night's row whose line was dropped by the gates carries none, so the draft before it stays.
    """
    rows = conn.execute(
        "SELECT day, line, highlights, draft, ts FROM day_prose "
        "WHERE line IS NOT NULL AND day >= ? AND day <= ? ORDER BY day, rowid",
        (first_day, last_day),
    ).fetchall()
    return {  # a later row of the same day replaces the earlier one
        str(day): DayLineRow(str(line), json.loads(picks or "[]"), bool(draft), str(ts))
        for day, line, picks, draft, ts in rows
    }


class HighlightMark(NamedTuple):
    """What he said about one item of a day (ADR 0235); ``kind`` and ``start_ms`` are the item's."""

    item_id: str
    on: bool
    label: str | None
    kind: str
    start_ms: int


def append_highlight_mark(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    day: str,
    item_id: str,
    on: bool,
    label: str | None,
    kind: str,
    start_ms: int,
) -> str:
    """Append his marking (``on``) or unmarking of an item of ``day``; the latest per item wins."""
    mark_id = f"highlight-mark:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            'INSERT INTO day_highlight_marks (id, day, item_id, "on", label, ts, kind, start_ms) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mark_id, day, item_id, int(on), label, iso_seconds(local_now()), kind, start_ms),
        )
    return mark_id


def latest_highlight_marks(
    conn: sqlite3.Connection, first_day: str, last_day: str,
) -> dict[str, list[HighlightMark]]:
    """His newest mark of each item, by day, for the days in ``[first_day, last_day]``."""
    rows = conn.execute(
        'SELECT day, item_id, "on", label, kind, start_ms FROM day_highlight_marks '
        "WHERE day >= ? AND day <= ? ORDER BY rowid",
        (first_day, last_day),
    ).fetchall()
    newest: dict[tuple[str, str], HighlightMark] = {}
    for day, item_id, on, label, kind, start_ms in rows:  # a later mark of an item replaces
        newest[(str(day), str(item_id))] = HighlightMark(
            str(item_id), bool(on), label, str(kind), int(start_ms),
        )
    marks: dict[str, list[HighlightMark]] = {}
    for (day, _item), mark in newest.items():
        marks.setdefault(day, []).append(mark)
    return marks


class DoingRun(NamedTuple):
    """One run of a day's doing lane (ADR 0302): its whole list of spans, and how far it speaks.

    ``spans`` are ``{id, start_ms, end_ms, kind, title, detail, category, basis, confidence,
    clues, question, place_id}``; ``covers_until_ms`` is the time past which the program's own
    segments are served instead.
    """

    id: str
    day: str
    draft: bool
    written_at: str
    covers_until_ms: int
    model: str | None
    inputs_digest: str
    spans: list[dict[str, Any]]


def append_doing_run(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    day: str,
    draft: bool,
    covers_until_ms: int,
    model: str | None,
    inputs_digest: str,
    spans: Sequence[Mapping[str, object]],
) -> str:
    """Append one run of ``day``'s doing lane, whole; the latest run of a day is the one served."""
    run_id = f"doing-run:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO doing_runs "
            "(id, day, draft, written_at, covers_until_ms, model, inputs_digest, spans) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, day, int(draft), iso_seconds(local_now()), covers_until_ms, model,
                inputs_digest, json.dumps(list(spans), ensure_ascii=False),
            ),
        )
    return run_id


def latest_doing_run(conn: sqlite3.Connection, day: str) -> DoingRun | None:
    """The newest run of ``day`` (older ones stay as history), else None."""
    row = conn.execute(
        "SELECT id, day, draft, written_at, covers_until_ms, model, inputs_digest, spans "
        "FROM doing_runs WHERE day = ? ORDER BY rowid DESC LIMIT 1",
        (day,),
    ).fetchone()
    if row is None:
        return None
    return DoingRun(
        str(row[0]), str(row[1]), bool(row[2]), str(row[3]), int(row[4]),
        None if row[5] is None else str(row[5]), str(row[6]), json.loads(row[7]),
    )


THREAD_ORIGINS: Final = ("model", "him")
"""Who said what a key served: a model, or he did; his word wins whenever it was given."""


def append_attention_threads(
    path: Path, told: Mapping[str, tuple[str | None, str | None]], *, origin: str,
    model: str | None, day: str,
) -> None:
    """Append what each key of ``told`` served, as (thread, category) (ADR 0331).

    A thread of None says the key serves what is around it. Rows are never rewritten: the newest
    of a key is the one read, his over a model's.
    """
    if origin not in THREAD_ORIGINS:
        msg = f"origin must be one of {', '.join(THREAD_ORIGINS)}, not {origin!r}"
        raise ValueError(msg)
    ts = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO attention_threads (id, key, thread, category, origin, model, day, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (f"thread:{uuid.uuid4().hex}", key, thread, category, origin, model, day, ts)
                for key, (thread, category) in told.items()
            ],
        )


def attention_threads(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """What each key told so far served, as (thread, category): his newest word, else a model's."""
    told: dict[str, tuple[str | None, str | None]] = {}
    rows = conn.execute(
        "SELECT key, thread, category FROM attention_threads "
        "ORDER BY origin = 'him', rowid",
    )
    for key, thread, category in rows:
        told[str(key)] = (thread, category)
    return told


class DoingAnswer(NamedTuple):
    """What he said a stretch of time was (ADR 0302): by time, not by the span's id.

    Exactly one of ``choice`` (an option of the span's question) and ``text`` is set. ``lat``,
    ``lng`` and ``place_id`` are where he was when the span was a stay.
    """

    id: str
    day: str
    span_id: str
    start_ms: int
    end_ms: int
    choice: str | None
    text: str | None
    lat: float | None
    lng: float | None
    place_id: str | None
    origin: str
    ts: str


def append_doing_answer(  # noqa: PLR0913 — the row's columns, all required.
    path: Path,
    *,
    day: str,
    span_id: str,
    start_ms: int,
    end_ms: int,
    choice: str | None,
    text: str | None,
    lat: float | None,
    lng: float | None,
    place_id: str | None,
    origin: str,
) -> str:
    """Append his answer about ``[start_ms, end_ms)`` of ``day``; never rewritten or removed."""
    answer_id = f"doing-answer:{day}:{uuid.uuid4().hex}"
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO doing_answers "
            "(id, day, span_id, start_ms, end_ms, choice, text, lat, lng, place_id, origin, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                answer_id, day, span_id, start_ms, end_ms, choice, text, lat, lng, place_id,
                origin, iso_seconds(local_now()),
            ),
        )
    return answer_id


def _doing_answer(row: Sequence[Any]) -> DoingAnswer:
    return DoingAnswer(
        str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]),
        None if row[5] is None else str(row[5]), None if row[6] is None else str(row[6]),
        None if row[7] is None else float(row[7]), None if row[8] is None else float(row[8]),
        None if row[9] is None else str(row[9]), str(row[10]), str(row[11]),
    )


def doing_answers(conn: sqlite3.Connection, day: str) -> list[DoingAnswer]:
    """Every answer given about ``day``, the oldest first (a later answer wins over an earlier)."""
    rows = conn.execute(
        "SELECT id, day, span_id, start_ms, end_ms, choice, text, lat, lng, place_id, origin, ts "
        "FROM doing_answers WHERE day = ? ORDER BY rowid",
        (day,),
    ).fetchall()
    return [_doing_answer(row) for row in rows]


def doing_answers_near(
    conn: sqlite3.Connection, at: tuple[float, float], radius_m: float, before_ms: int,
    limit: int = 20,
) -> list[DoingAnswer]:
    """The newest ``limit`` answers given within ``radius_m`` of ``at`` (lat, lng).

    Only answers about a stretch that began before ``before_ms`` count; newest first.
    """
    reach = radius_m / 111_000.0  # a degree of latitude is about 111 km: a box, then the exact test
    rows = conn.execute(
        "SELECT id, day, span_id, start_ms, end_ms, choice, text, lat, lng, place_id, origin, ts "
        "FROM doing_answers WHERE lat IS NOT NULL AND lng IS NOT NULL AND start_ms < ? "
        "AND lat BETWEEN ? AND ? ORDER BY rowid DESC",
        (before_ms, at[0] - reach, at[0] + reach),
    ).fetchall()
    near = [
        _doing_answer(row) for row in rows
        if metres(at, (float(row[7]), float(row[8]))) <= radius_m
    ]
    return near[:limit]


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


def put_day_experience(path: Path, day: str, metrics_json: str) -> None:
    """Keep one local date's experience metrics; a date computed again replaces its row."""
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO day_experience (local_date, metrics_json, computed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(local_date) DO UPDATE SET metrics_json = excluded.metrics_json, "
            "computed_at = excluded.computed_at",
            (day, metrics_json, iso_seconds(local_now())),
        )


def experience_days(path: Path) -> set[str]:
    """The local dates that already have a ``day_experience`` row."""
    with closing(open_memory_db(path)) as conn:
        return {str(row[0]) for row in conn.execute("SELECT local_date FROM day_experience")}


def latest_day_experience(conn: sqlite3.Connection, count: int) -> list[tuple[str, str]]:
    """``(local_date, metrics_json)`` of the newest ``count`` rows, oldest first.

    Takes the caller's connection so a read-only one works; no table yet is no rows.
    """
    try:
        rows = conn.execute(
            "SELECT local_date, metrics_json FROM day_experience ORDER BY local_date DESC LIMIT ?",
            (count,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(day), str(metrics)) for day, metrics in reversed(rows)]


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


def messages_since(path: Path, record_id: str, limit: int = 12) -> tuple[Record, ...]:
    """His and her messages from record ``record_id`` on, oldest first, the latest ``limit``.

    Empty when that record is not there. Mail, notes and reactions are left out.
    """
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT id, ts, source, text FROM records "
            "WHERE rowid >= (SELECT rowid FROM records WHERE id = ?) "
            "AND source IN ('allen', 'jarvis') ORDER BY rowid DESC LIMIT ?",
            (record_id, limit),
        ).fetchall()
    return tuple(
        (str(rid), str(ts), str(source), str(text)) for rid, ts, source, text in reversed(rows)
    )


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


_SPOKEN_SOURCES: Final[tuple[str, ...]] = ("allen", "jarvis", "jarvis_live")
"""The sources of what was said in a conversation: his, and hers typed and spoken."""


def spoken_between(
    conn: sqlite3.Connection, start_ms: int, end_ms: int,
) -> list[tuple[int, str, str]]:
    """``(at_ms, source, words)`` of what was said in ``[start_ms, end_ms)``, oldest first.

    His words and hers; a picture's note and a reaction are not said. The words are the record's
    text without markup, without the marker line of files that came with it and without a filler
    the program wrote in place of words, so a turn of files alone has none and is left out.
    """
    first = datetime.fromtimestamp(start_ms / 1000, UTC).isoformat()
    last = datetime.fromtimestamp(end_ms / 1000, UTC).isoformat()
    marks = ", ".join("?" * len(_SPOKEN_SOURCES))
    rows = conn.execute(
        "SELECT ts, source, text FROM records "  # noqa: S608 - fixed placeholders
        f"WHERE source IN ({marks}) AND datetime(ts) >= datetime(?) AND datetime(ts) < datetime(?) "
        "ORDER BY datetime(ts), rowid",
        (*_SPOKEN_SOURCES, first, last),
    ).fetchall()
    return [
        (int(datetime.fromisoformat(str(ts)).timestamp() * 1000), str(source), words)
        for ts, source, text in rows
        if (words := _plain(_his_words(str(text))).strip())
    ]


__all__ = [
    "DAY_SUMMARIES_CONTEXT",
    "DEFAULT_SEARCH_LIMIT",
    "THREAD_ORIGINS",
    "CompactionRange",
    "DayLineRow",
    "DoingAnswer",
    "DoingRun",
    "HighlightMark",
    "Ledger",
    "MemoryContext",
    "MemorySettings",
    "PlacesBlock",
    "Record",
    "SessionSettings",
    "VerbatimStats",
    "append_attention_threads",
    "append_day_draft",
    "append_day_prose",
    "append_day_summary",
    "append_doing_answer",
    "append_doing_run",
    "append_highlight_mark",
    "append_nightly_core_memory",
    "append_record",
    "append_summary",
    "attention_threads",
    "brief_note",
    "compaction_range",
    "core_memory_pending_days",
    "current_core_memory",
    "day_records",
    "doing_answers",
    "doing_answers_near",
    "drop_due_section",
    "due_items",
    "experience_days",
    "iso_seconds",
    "keep_fact",
    "last_draft_at",
    "latest_day_experience",
    "latest_day_lines",
    "latest_day_summaries",
    "latest_doing_run",
    "latest_highlight_marks",
    "local_now",
    "messages_since",
    "open_memory_db",
    "pending_days",
    "prose_days",
    "put_day_experience",
    "remember_fact",
    "render_context",
    "search_records",
    "spoken_between",
    "verbatim_stats",
]
