"""ADR 0390: the topics she keeps, an index over the conversation's records in memory.db.

A topic is a short id (``t1``) and its versions in ``topic_versions``, newest current: its name,
its state, the one line saying where it stands and the recap. ``topic_lines`` files records under
a topic, ``by`` him (the message was sent from the topic) or the ``night`` (the nightly pass);
nothing is ever unfiled, and a record may sit under several topics. ``topic_nights`` keeps each
day the night sorted and what it changed. A merged topic's version names the topic it went into,
which then counts its lines too; ``resolve`` follows that.

Every table is append-only, so a mistake is corrected by a newer version, never by rewriting.

Layer rules: stdlib and ``jarvis.state`` only.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from jarvis.state.core_memory import write_transaction
from jarvis.state.memory_db import iso_seconds, local_now, open_memory_db
from jarvis.state.memory_db import pending_days as unsummarised_days

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Iterable, Mapping, Sequence
    from pathlib import Path

TOPIC_ID: Final = re.compile(r"t[1-9][0-9]{0,6}")
"""A topic's id: ``t`` and its number, given in the order topics were opened."""
NAME_MAX: Final = 24
"""The most characters a topic's name may have: it is a label in a list on the phone."""
LINE_MAX: Final = 80
"""The most characters of the one line saying where a topic stands."""
RECAP_MAX: Final = 800
"""The most characters of a recap: what a turn sent from the topic carries in its state."""
MESSAGE_SOURCES: Final = ("allen", "jarvis", "jarvis_live")
"""The records a topic files: his messages and hers. Mail, picture notes and reactions are not
part of what was said to each other."""

State = Literal["open", "done", "merged", "dropped"]
LISTED: Final = ("open", "done")
"""The states a topic is listed in; a merged or dropped one is not."""
By = Literal["him", "night"]
Origin = Literal["him", "night"]


@dataclass(frozen=True)
class Topic:
    """A topic as its newest version has it."""

    id: str
    name: str
    state: State
    line: str
    recap: str
    merged_into: str | None
    version_id: str


def _current(conn: sqlite3.Connection) -> dict[str, Topic]:
    """Every topic's newest version, merged and dropped ones included, by id."""
    found: dict[str, Topic] = {}
    for vid, tid, name, state, merged, line, recap in conn.execute(
        "SELECT id, topic_id, name, state, merged_into, line, recap FROM topic_versions "
        "ORDER BY rowid",
    ):
        found[str(tid)] = Topic(
            str(tid), str(name), cast("State", str(state)), str(line), str(recap),
            None if merged is None else str(merged), str(vid),
        )
    return found


def current(path: Path) -> dict[str, Topic]:
    """Every topic's newest version by id, merged and dropped ones included."""
    with closing(open_memory_db(path)) as conn:
        return _current(conn)


def resolve(topics: Mapping[str, Topic], topic_id: str) -> Topic | None:
    """The live topic ``topic_id`` names: itself, or the one it was merged into; else ``None``.

    ``None`` for an id that was never given and for a topic that was dropped.
    """
    seen: set[str] = set()
    one = topics.get(topic_id)
    while one is not None and one.state == "merged" and one.merged_into not in seen:
        seen.add(one.id)
        one = topics.get(one.merged_into or "")
    return one if one is not None and one.state in LISTED else None


def live_topic(path: Path, topic_id: str) -> Topic | None:
    """:func:`resolve` against the store; ``None`` for an id that is not a topic's id."""
    if TOPIC_ID.fullmatch(topic_id) is None:
        return None
    return resolve(current(path), topic_id)


def _members(topics: Mapping[str, Topic]) -> dict[str, list[str]]:
    """Each live topic's id -> its own id and the ids of every topic merged into it."""
    members: dict[str, list[str]] = {}
    for one in topics.values():
        live = resolve(topics, one.id)
        if live is not None:
            members.setdefault(live.id, []).append(one.id)
    return members


def _new_id(conn: sqlite3.Connection) -> str:
    numbers = [
        int(str(tid)[1:]) for (tid,) in conn.execute("SELECT DISTINCT topic_id FROM topic_versions")
    ]
    return f"t{max(numbers, default=0) + 1}"


def _version(  # noqa: PLR0913 - the version's columns, all keyword-only.
    conn: sqlite3.Connection, topic_id: str, *, origin: Origin, name: str, state: State,
    line: str, recap: str, merged_into: str | None = None,
) -> str:
    version_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO topic_versions (id, topic_id, ts, origin, name, state, merged_into, line, "
        "recap) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (version_id, topic_id, iso_seconds(local_now()), origin, name, state, merged_into, line,
         recap),
    )
    return version_id


def _file(
    conn: sqlite3.Connection, topic_id: str, record_ids: Iterable[str], by: By,
) -> int:
    """File the message records among ``record_ids`` under ``topic_id``; how many were new."""
    ids = list(dict.fromkeys(record_ids))
    if not ids:
        return 0
    marks = ", ".join("?" * len(MESSAGE_SOURCES))
    messages = {
        str(rid) for (rid,) in conn.execute(
            f"SELECT id FROM records WHERE source IN ({marks}) "  # noqa: S608 - fixed placeholders
            "AND id IN (SELECT value FROM json_each(?))",
            (*MESSAGE_SOURCES, json.dumps(ids)),
        )
    }
    now = iso_seconds(local_now())
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO topic_lines (topic_id, record_id, by, ts) VALUES (?, ?, ?, ?)",
        [(topic_id, rid, by, now) for rid in ids if rid in messages],
    )
    return conn.total_changes - before


def file_his(path: Path, topic_id: str, record_ids: Sequence[str]) -> str | None:
    """File the records of a turn he sent from ``topic_id`` under it, at once.

    The id resolves as :func:`resolve` does, so a topic merged since the phone listed it still
    takes the lines; the live topic's id, or ``None`` when there is none (nothing is filed).
    """
    if TOPIC_ID.fullmatch(topic_id) is None:
        return None
    with closing(open_memory_db(path)) as conn, write_transaction(conn):
        live = resolve(_current(conn), topic_id)
        if live is None:
            return None
        _file(conn, live.id, record_ids, "him")
        return live.id


@dataclass(frozen=True)
class NightTopic:
    """One topic as the night leaves it: an existing one (``topic_id``) or a new one (``None``)."""

    topic_id: str | None
    name: str
    state: Literal["open", "done"]
    line: str
    recap: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class NightPlan:
    """What one night does to the topics, every id already checked against the store."""

    day: str
    topics: tuple[NightTopic, ...]
    merges: tuple[tuple[str, str], ...]
    """``(from, into)`` pairs: ``from`` goes into ``into``."""


def apply_night(
    path: Path, plan: NightPlan, *, base: Collection[str], model: str,
) -> dict[str, Any] | None:
    """Apply one night's plan and record the night; ``None`` when the topics changed meanwhile.

    ``base`` is the set of version ids the plan was made from: when he renamed, merged or closed
    a topic while the night was asking, the plan is about topics that are no longer there, and
    nothing is written. Returns what was written, as the night's row keeps it.
    """
    with closing(open_memory_db(path)) as conn, write_transaction(conn):
        topics = _current(conn)
        if {one.version_id for one in topics.values()} != set(base):
            return None
        written: dict[str, Any] = {"opened": [], "touched": [], "merged": [], "filed": 0}
        for source, into in plan.merges:
            gone = topics[source]
            _version(
                conn, source, origin="night", name=gone.name, state="merged", line=gone.line,
                recap=gone.recap, merged_into=into,
            )
            written["merged"].append([source, into])
        for one in plan.topics:
            topic_id = one.topic_id or _new_id(conn)
            _version(
                conn, topic_id, origin="night", name=one.name, state=one.state, line=one.line,
                recap=one.recap,
            )
            written["opened" if one.topic_id is None else "touched"].append(topic_id)
            written["filed"] += _file(conn, topic_id, one.record_ids, "night")
        conn.execute(
            "INSERT INTO topic_nights (id, day, ts, model, changes) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, plan.day, iso_seconds(local_now()), model,
             json.dumps(written, ensure_ascii=False)),
        )
        return written


def nights_done(path: Path) -> set[str]:
    """The days the night has sorted."""
    with closing(open_memory_db(path)) as conn:
        return {str(day) for (day,) in conn.execute("SELECT DISTINCT day FROM topic_nights")}


def pending_days(path: Path, today: date, since: str = "") -> list[str]:
    """Days before ``today`` with a day summary and no night after them, oldest first.

    The night goes in order and each day builds on the one before, so a day is pending only when
    it is later than the latest day sorted, and none is pending from the first day with records
    and no summary yet (the nights after it read its summary). ``since`` (a ``YYYY-MM-DD``) is the
    first day the first run looks at, empty for every day with a summary.
    """
    waiting = unsummarised_days(path, today)
    stop = waiting[0][0] if waiting else today.isoformat()
    with closing(open_memory_db(path)) as conn:
        latest = conn.execute("SELECT MAX(day) FROM topic_nights").fetchone()[0]
        floor = str(latest or "")
        days = [
            str(day) for (day,) in conn.execute(
                "SELECT DISTINCT day FROM day_summaries WHERE day > ? AND day < ? ORDER BY day",
                (floor, stop),
            )
        ]
    return [day for day in days if not since or day >= since]


def filed_under(conn: sqlite3.Connection, record_ids: Sequence[str]) -> dict[str, list[str]]:
    """Record id -> the live topics it is filed under, in the order they were opened."""
    topics = _current(conn)
    found: dict[str, list[str]] = {}
    for rid, tid in conn.execute(
        "SELECT record_id, topic_id FROM topic_lines "
        "WHERE record_id IN (SELECT value FROM json_each(?)) ORDER BY rowid",
        (json.dumps(list(record_ids)),),
    ):
        live = resolve(topics, str(tid))
        if live is not None and live.id not in found.setdefault(str(rid), []):
            found[str(rid)].append(live.id)
    return found


def unfiled_messages(conn: sqlite3.Connection, record_ids: Iterable[str]) -> set[str]:
    """Those of ``record_ids`` that are his or her messages and filed under no live topic."""
    topics = _current(conn)
    filed = {
        str(rid) for rid, tid in conn.execute("SELECT record_id, topic_id FROM topic_lines")
        if resolve(topics, str(tid)) is not None
    }
    return {
        str(rid) for (rid,) in conn.execute(
            "SELECT id FROM records WHERE id IN (SELECT value FROM json_each(?)) "
            "AND source IN (SELECT value FROM json_each(?))",
            (json.dumps(list(record_ids)), json.dumps(MESSAGE_SOURCES)),
        )
    } - filed


@dataclass(frozen=True)
class Line:
    """One filed record, where it sits in the conversation."""

    record_id: str
    seq: int
    ts_ms: int
    source: str
    text: str
    by: By


def _lines(conn: sqlite3.Connection, ids: Sequence[str]) -> list[Line]:
    """The records filed under any of ``ids``, each once (his filing wins), oldest first."""
    rows = conn.execute(
        "SELECT r.id, r.rowid, r.ts, r.source, r.text, MIN(l.by) FROM topic_lines l "
        "JOIN records r ON r.id = l.record_id "
        "WHERE l.topic_id IN (SELECT value FROM json_each(?)) GROUP BY r.id ORDER BY r.rowid",
        (json.dumps(list(ids)),),
    ).fetchall()  # MIN: "him" sorts before "night"
    return [
        Line(str(rid), int(seq), int(datetime.fromisoformat(str(ts)).timestamp() * 1000),
             str(source), str(text), cast("By", str(by)))
        for rid, seq, ts, source, text, by in rows
    ]


@dataclass(frozen=True)
class Place:
    """One day a topic was talked about: its lines that day, oldest first."""

    day: date
    lines: tuple[Line, ...]

    @property
    def by(self) -> By:
        """``him`` when he said any of it inside the topic, else ``night``."""
        return "him" if any(one.by == "him" for one in self.lines) else "night"


def places(lines: Sequence[Line], zone: tzinfo) -> list[Place]:
    """``lines`` cut into one place per local day in ``zone``, oldest first."""
    by_day: dict[date, list[Line]] = {}
    for one in lines:
        day = datetime.fromtimestamp(one.ts_ms / 1000, zone).date()
        by_day.setdefault(day, []).append(one)
    return [Place(day, tuple(found)) for day, found in sorted(by_day.items())]


@dataclass(frozen=True)
class Listed:
    """A live topic with where it was talked about."""

    topic: Topic
    places: tuple[Place, ...]

    @property
    def last_ms(self) -> int:
        """When it was last talked about."""
        return self.places[-1].lines[-1].ts_ms


def listed(path: Path, zone: tzinfo) -> list[Listed]:
    """The live topics with at least one line: open ones first, each side newest talk first."""
    with closing(open_memory_db(path)) as conn:
        topics = _current(conn)
        found = [
            Listed(topics[tid], tuple(places(_lines(conn, ids), zone)))
            for tid, ids in _members(topics).items()
        ]
    shown = [one for one in found if one.places]
    return sorted(shown, key=lambda one: (one.topic.state != "open", -one.last_ms))


def topic_lines(path: Path, topic_id: str) -> list[Line]:
    """Every line filed under the live topic ``topic_id`` and those merged into it."""
    with closing(open_memory_db(path)) as conn:
        topics = _current(conn)
        live = resolve(topics, topic_id)
        if live is None:
            return []
        return _lines(conn, _members(topics)[live.id])


def tag_rows(path: Path, rows: Sequence[dict[str, object]]) -> None:
    """Give each row of his or hers that was said inside a topic ``topic``: ``{id, name}``.

    Only his filing counts: a line the night filed was not said inside the topic, and the
    night's filings are what ``GET /inherent/topics`` lists.
    """
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return
    with closing(open_memory_db(path)) as conn:
        topics = _current(conn)
        said = conn.execute(
            "SELECT record_id, topic_id FROM topic_lines WHERE by = 'him' "
            "AND record_id IN (SELECT value FROM json_each(?)) ORDER BY rowid",
            (json.dumps(ids),),
        ).fetchall()
    tags: dict[str, dict[str, str]] = {}
    for rid, tid in said:
        live = resolve(topics, str(tid))
        if live is not None:
            tags.setdefault(str(rid), {"id": live.id, "name": live.name})
    for row in rows:
        tag = tags.get(str(row["id"]))
        if tag is not None:
            row["topic"] = tag


Op = Literal["rename", "merge", "done", "reopen", "drop"]
_STATE_OF: Final[Mapping[str, State]] = {"done": "done", "reopen": "open", "drop": "dropped"}


@dataclass(frozen=True)
class Change:
    """One change he asked for: the topic it changed, the version before it and the new one."""

    topic: Topic
    before: Topic
    version_id: str


def change(
    path: Path, topic_id: str, op: Op, *, name: str | None = None, into: str | None = None,
) -> Change | None:
    """Write the version ``op`` makes of ``topic_id`` at his word; ``None`` when it is already so.

    ``rename`` takes ``name``; ``merge`` takes ``into``, a live topic other than this one;
    ``reopen`` brings back a done, merged or dropped topic as open.

    Raises:
        ValueError: the topic, the name or the target is not one this change can take.
    """
    with closing(open_memory_db(path)) as conn, write_transaction(conn):
        topics = _current(conn)
        one = topics.get(topic_id)
        if one is None:
            msg = f"there is no topic {topic_id}"
            raise ValueError(msg)
        if op != "reopen" and one.state not in LISTED:
            msg = f"topic {topic_id} was {one.state}; reopen it first"
            raise ValueError(msg)
        state: State = one.state
        new_name, target = one.name, None
        if op == "rename":
            new_name = " ".join((name or "").split())
            if not new_name or len(new_name) > NAME_MAX:
                msg = f"a topic's name is 1 to {NAME_MAX} characters"
                raise ValueError(msg)
        elif op == "merge":
            goal = resolve(topics, into or "")
            if goal is None or goal.id == one.id:
                msg = "merge goes into another topic that is listed"
                raise ValueError(msg)
            state, target = "merged", goal.id
        else:
            state = _STATE_OF[op]
        if (new_name, state, target) == (one.name, one.state, one.merged_into):
            return None
        version_id = _version(
            conn, topic_id, origin="him", name=new_name, state=state, line=one.line,
            recap=one.recap, merged_into=target,
        )
        return Change(_current(conn)[topic_id], one, version_id)


__all__ = [
    "LINE_MAX",
    "LISTED",
    "MESSAGE_SOURCES",
    "NAME_MAX",
    "RECAP_MAX",
    "TOPIC_ID",
    "Change",
    "Line",
    "Listed",
    "NightPlan",
    "NightTopic",
    "Place",
    "Topic",
    "apply_night",
    "change",
    "current",
    "file_his",
    "filed_under",
    "listed",
    "live_topic",
    "nights_done",
    "pending_days",
    "places",
    "resolve",
    "tag_rows",
    "topic_lines",
    "unfiled_messages",
]
