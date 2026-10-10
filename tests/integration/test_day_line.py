"""ADR 0199: the brain serves each day as one timeline, folded from the log when asked.

Acceptance checks, each against the real code and a real event log:

- the fold, as a table of log rows to the items of a day: a visit reported twice, an open stay,
  an arrival that a later stay ends, motion merged, split, dropped and ended by its neighbour,
  a move inside a stay, sleep stages across midnight, reminders pending, fired and cancelled,
  the window across a clock change, an item that overlaps the window returned whole, and the order
  of items that start together;
- the Mac and her conversations, as a table of TimeSink spans, calls and his records to items: work
  blocks merged, dropped and named by their projects and apps, calls over the floor, talks grouped,
  blocks and talks across midnight returned whole, ids that stay when a block or a talk grows;
- those numbers against the ledger's own text for the same day (the blocks add up to the active
  time she is told, the apps and projects to her tops, the calls and talks to her counts);
- `mac` and `talks` in `missing` when TimeSink or the memory store is not there, without a log line,
  and a source that raises named and logged;
- the window itself against an independent reckoning, and the day text parsed strictly;
- ids that stay the same when the log grows and when the call is repeated;
- a source that cannot be read leaves its items out and is named, without failing the day;
- ``GET /inherent/day``: a bad date is a 400, no date is today in the owner's zone, a remote peer
  needs a paired device's token, and a phone's batch posted to the brain comes back as the day.

Times in the table are minutes from the local midnight that opens the case's day.
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.inherent_loop import _read_day, _serve_day
from jarvis.state import day_line, reminders
from jarvis.state.daily_report import resolve_zone
from jarvis.state.day_line import DaySources, day_window, fold_day, parse_day
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.ledger import LedgerSources, day_numbers_text, hm, window_activity
from jarvis.state.memory_db import open_memory_db
from jarvis.state.phone_location import VISIT_EVENT
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import phone_events
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from tests.integration.test_ledger import _PROJECT, _SPAN, _VERDICT
from tests.integration.test_terminal_voice import REMOTE, _bearer, _brain_log

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

VANCOUVER = "America/Vancouver"
MINUTE = 60_000
DAY_PATH = "/inherent/day"
VISIT, MOTION, HEALTH = "phone.visit_observed", "phone.motion_observed", "phone.health_observed"

# --- rows and items, in minutes from the opening midnight --------------------------------------

_MINUTE_KEYS = frozenset({
    "arrived_at_ms", "departed_at_ms", "started_at_ms", "ended_at_ms", "start_ms", "end_ms",
    "due_at_epoch_ms", "fired_at",
})


class Row(NamedTuple):
    """One log row: type, uid, a payload whose times are minutes, and the minute it was logged."""

    type: str
    uid: str
    payload: dict[str, Any]
    ts: float


def _latest(payload: dict[str, Any]) -> float:
    return max((v for k, v in payload.items() if k in _MINUTE_KEYS), default=0)


def _phone(kind: str, uid: str, payload: dict[str, Any], ts: float | None) -> Row:
    return Row(kind, uid, payload, _latest(payload) if ts is None else ts)


def visit(
    uid: str, arrived: float, departed: float | None = None, place: str | None = None,
    *, ts: float | None = None,
) -> Row:
    """A ``phone.visit_observed`` row: arrival only, or with a departure."""
    payload: dict[str, Any] = {
        "lat": 48.46, "lng": -123.31, "accuracy_m": 65.0, "arrived_at_ms": arrived,
    }
    if departed is not None:
        payload["departed_at_ms"] = departed
    if place is not None:
        payload["place"] = place
    return _phone(VISIT, uid, payload, ts)


def motion(
    uid: str, activity: str, start: float, end: float | None = None, confidence: str = "high",
) -> Row:
    """A ``phone.motion_observed`` row."""
    payload: dict[str, Any] = {
        "activity": activity, "confidence": confidence, "started_at_ms": start,
    }
    if end is not None:
        payload["ended_at_ms"] = end
    return _phone(MOTION, uid, payload, None)


def health(
    uid: str, start: float, end: float, value: float, metric: str = "sleep_min",
) -> Row:
    """A ``phone.health_observed`` row; sleep unless said otherwise."""
    payload = {"metric": metric, "start_ms": start, "end_ms": end, "value": value}
    return _phone(HEALTH, uid, payload, None)


def remind(rid: str, due: float, text: str = "stand up") -> list[Row]:
    """A ``reminder.scheduled`` row."""
    payload = {
        "reminder_id": rid, "due_at_epoch_ms": due, "due_at_local": "-", "text": text,
        "action_id": "A1",
    }
    return [Row("reminder.scheduled", f"{rid}-scheduled", payload, 0)]


def fired(rid: str, at: float) -> Row:
    """A ``reminder.fired`` row."""
    payload = {"reminder_id": rid, "fired_at": at, "late_ms": 0, "delivered": "speak"}
    return Row("reminder.fired", f"{rid}-fired", payload, at)


def acknowledged(rid: str) -> Row:
    """A ``reminder.acknowledged`` row."""
    return Row("reminder.acknowledged", f"{rid}-ack", {"reminder_id": rid}, 0)


def cancelled(rid: str) -> Row:
    """A ``reminder.cancelled`` row."""
    return Row("reminder.cancelled", f"{rid}-cancel", {"reminder_id": rid, "action_id": "A1"}, 0)


class Item(NamedTuple):
    """An expected item; times are minutes from the opening midnight."""

    uid: str
    kind: str
    start: float
    end: float | None
    state: str
    extra: dict[str, Any]


def stay(uid: str, start: float, end: float | None, state: str, place: str | None = None) -> Item:
    """An expected ``stay``, at the coordinates ``visit`` reports."""
    extra: dict[str, Any] = {"lat": 48.46, "lng": -123.31, "accuracy_m": 65.0}
    if place is not None:
        extra["place"] = place
    return Item(uid, "stay", start, end, state, extra)


def move(uid: str, activity: str, start: float, end: float | None, state: str) -> Item:
    """An expected ``move``."""
    return Item(uid, "move", start, end, state, {"activity": activity})


def slept(uid: str, start: float, end: float, state: str, minutes: float) -> Item:
    """An expected ``sleep`` block."""
    return Item(uid, "sleep", start, end, state, {"minutes": minutes})


def due(rid: str, at: float, state: str, text: str = "stand up", fate: str = "") -> Item:
    """An expected ``reminder`` in the lane of her; ``fate`` is "", "fired" or "taken"."""
    extra = {"text": text, "fired": bool(fate), "acknowledged": fate == "taken"}
    return Item(rid, "reminder", at, None, state, extra)


def _ms(origin: int, minutes: float) -> int:
    return origin + round(minutes * MINUTE)


def _load(conn: sqlite3.Connection, rows: list[Row], origin: int) -> None:
    """Append the rows to the log; every ``*_ms`` payload field and the row time are minutes."""
    for row in rows:
        payload = {k: _ms(origin, v) if k in _MINUTE_KEYS else v for k, v in row.payload.items()}
        emit_event(
            conn, type=row.type, payload=payload, ts_epoch_ms=_ms(origin, row.ts),
            event_uid=row.uid, ingestion_node="phone",
        )


def _expected(items: list[Item], origin: int) -> list[dict[str, Any]]:
    """The items as the route words them; an ``@`` in a uid stands for the item's start in ms."""
    return [
        {
            "id": item.uid.replace("@", str(_ms(origin, item.start))),
            "lane": "her" if item.kind in ("reminder", "talk") else "you", "kind": item.kind,
            "start_ms": _ms(origin, item.start),
            "end_ms": None if item.end is None else _ms(origin, item.end),
            "state": item.state, **item.extra,
        }
        for item in items
    ]


class Case(NamedTuple):
    """Log rows of one day, and the items that day folds to."""

    name: str
    rows: list[Row]
    items: list[Item]
    day: str = "2026-10-10"
    zone: str = VANCOUVER
    now: float = 720  # noon


CASES: list[Case] = [
    # --- visits: one stay however many times iOS reports it ---------------------------------
    Case(
        "a visit arrival then the same visit with its departure is one stay",
        [visit("a1", 540, place="Cafe"), visit("a2", 540, 600, "Cafe")],
        [stay("a1", 540, 600, "done", "Cafe")],
    ),
    Case(
        "the row with a departure wins whichever was logged first",
        [visit("d1", 540, 600, "Cafe"), visit("a1", 540, place="Cafe")],
        [stay("d1", 540, 600, "done", "Cafe")],
    ),
    Case(
        "of two rows with a departure the later one wins",
        [visit("a1", 540, 600), visit("a2", 540, 605)],
        [stay("a1", 540, 605, "done")],
    ),
    Case(
        "a stay with no departure and no later stay is still going",
        [visit("o1", 660, place="Home")],
        [stay("o1", 660, None, "now", "Home")],
    ),
    Case(
        "an arrival with no departure ends where the next stay begins",
        [visit("a1", 480), visit("b1", 570, 630), visit("c1", 660)],
        [stay("a1", 480, 570, "done"), stay("b1", 570, 630, "done"), stay("c1", 660, None, "now")],
    ),
    Case(
        "a stay that begins after now is next",
        [visit("f1", 725)],
        [stay("f1", 725, None, "next")],
    ),
    Case(
        "a stay ending exactly now is done and one covering now is now",
        [visit("a1", 600, 720), visit("b1", 720, 800)],
        [stay("a1", 600, 720, "done"), stay("b1", 720, 800, "now")],
    ),
    Case(
        "a stay that overlaps the day is returned whole and those that only touch it are not",
        [
            visit("p1", -120, 420),  # 22:00 the night before to 07:00
            visit("p0", -600, -300),
            visit("e1", -200, 0),  # leaves exactly as the day opens
            visit("s1", 1440, 1500),  # arrives exactly as the day closes
            visit("n1", 1600, 1700),
        ],
        [stay("p1", -120, 420, "done")],
    ),
    Case(
        "a stay that began before the day and is still going is returned whole",
        [visit("y1", -120, place="Home")],
        [stay("y1", -120, None, "now", "Home")],
    ),
    Case(
        "the log is read from two days before the day, by the time the phone reported",
        [visit("mid-open", -2000)],
        [stay("mid-open", -2000, None, "now")],
    ),
    Case(
        "a row reported more than two days before the day is not read",
        [visit("old-open", -3000)],
        [],
    ),
    Case(
        "a stay whose arrival is older than that is found by its departure row",
        [visit("dep-only", -3100, 100, ts=100)],
        [stay("dep-only", -3100, 100, "done")],
    ),
    # --- motion ------------------------------------------------------------------------------
    Case(
        "segments of one activity within three minutes are one move, and three exactly is within",
        [
            motion("m1", "walking", 480, 490), motion("m2", "walking", 492, 500),
            motion("m3", "running", 600, 610), motion("m4", "running", 613, 620),
        ],
        [move("m1", "walking", 480, 500, "done"), move("m3", "running", 600, 620, "done")],
    ),
    Case(
        "segments more than three minutes apart stay two moves",
        [motion("m1", "walking", 480, 490), motion("m2", "walking", 494, 500)],
        [move("m1", "walking", 480, 490, "done"), move("m2", "walking", 494, 500, "done")],
    ),
    Case(
        "a different activity is a different move even when it follows at once",
        [motion("m1", "walking", 480, 490), motion("m2", "automotive", 490, 520)],
        [move("m1", "walking", 480, 490, "done"), move("m2", "automotive", 490, 520, "done")],
    ),
    Case(
        "a short stationary blip between two walks is absorbed",
        [
            motion("m1", "walking", 420, 430), motion("m2", "stationary", 430, 432),
            motion("m3", "walking", 432, 450),
        ],
        [move("m1", "walking", 420, 450, "done")],
    ),
    Case(
        "a merged move under three minutes is dropped and three exactly is kept",
        [
            motion("s1", "walking", 480, 482),
            motion("k1", "cycling", 500, 503),
            motion("p1", "walking", 540, 541), motion("p2", "walking", 543, 544),
        ],
        [move("k1", "cycling", 500, 503, "done"), move("p1", "walking", 540, 544, "done")],
    ),
    Case(
        "stationary, unknown and doubtful segments make no move, a medium one does",
        [
            motion("a", "stationary", 480, 500), motion("b", "unknown", 510, 530),
            motion("c", "walking", 540, 560, "low"), motion("d", "walking", 570, 590, "medium"),
        ],
        [move("d", "walking", 570, 590, "done")],
    ),
    Case(
        "a segment with no end ends where the next segment of any activity starts",
        [
            motion("m1", "walking", 480), motion("m2", "stationary", 495),
            motion("m3", "automotive", 600),
        ],
        [move("m1", "walking", 480, 495, "done"), move("m3", "automotive", 600, None, "now")],
    ),
    Case(
        "the last segment with no end is a move once it has lasted three minutes to now",
        [motion("old", "cycling", 600, 605), motion("last", "running", 715)],
        [move("old", "cycling", 600, 605, "done"), move("last", "running", 715, None, "now")],
    ),
    Case(
        "the last segment with no end is not yet a move before that",
        [motion("old", "cycling", 600, 605), motion("young", "walking", 719)],
        [move("old", "cycling", 600, 605, "done")],
    ),
    Case(
        "the same segment reported with and without its end is one",
        [motion("m1", "walking", 480), motion("m2", "walking", 480, 495),
         motion("m3", "running", 600, 615), motion("m4", "running", 600)],
        [move("m1", "walking", 480, 495, "done"), move("m3", "running", 600, 615, "done")],
    ),
    Case(
        "a move inside one stay is dropped and a move that reaches outside is kept",
        [
            visit("s1", 540, 660, "Library"),
            motion("in1", "walking", 545, 555),
            motion("in2", "cycling", 540, 550),  # starts as he arrives
            motion("in3", "running", 600, 610),
            motion("before", "automotive", 530, 538),
            motion("across", "walking", 650, 680),
            motion("out", "cycling", 700, 720),
        ],
        [
            move("before", "automotive", 530, 538, "done"),
            stay("s1", 540, 660, "done", "Library"),
            move("across", "walking", 650, 680, "done"),
            move("out", "cycling", 700, 720, "done"),
        ],
    ),
    Case(
        "a move inside a stay that is still going is dropped",
        [visit("s1", 540), motion("in", "walking", 560, 580), motion("pre", "walking", 500, 520)],
        [move("pre", "walking", 500, 520, "done"), stay("s1", 540, None, "now")],
    ),
    Case(
        "a move that crosses midnight is returned whole",
        [motion("late", "automotive", -30, 25), motion("next", "walking", 1430, 1450)],
        [move("late", "automotive", -30, 25, "done"), move("next", "walking", 1430, 1450, "next")],
        now=30,
    ),
    # --- sleep -------------------------------------------------------------------------------
    Case(
        "sleep stages across midnight are one block and a gap over thirty minutes starts another",
        [
            health("s1", -50, 40, 90), health("s2", 45, 180, 135), health("s3", 185, 390, 205),
            health("s4", 410, 430, 20), health("s5", 480, 510, 30),
            health("s6", 540, 560, 15),  # exactly thirty minutes after s5
            health("steps", 100, 200, 3000, "steps"),
        ],
        [slept("s1", -50, 430, "done", 450), slept("s5", 480, 560, "done", 45)],
    ),
    Case(
        "overlapping sleep spans are one block and their values add up",
        [health("x1", 600, 700, 100), health("x2", 650, 660, 10.5), health("x3", 1000, 1100, 7)],
        [slept("x1", 600, 700, "done", 110.5), slept("x3", 1000, 1100, "next", 7)],
    ),
    Case(
        "a sleep block in progress is now",
        [health("z1", 700, 760, 60)],
        [slept("z1", 700, 760, "now", 60)],
    ),
    # --- reminders -----------------------------------------------------------------------------
    Case(
        "reminders pending, fired, taken in and cancelled; those off the day are left out",
        [
            *remind("reminder-p", 900, "pending"),
            *remind("reminder-f", 480, "fired"), fired("reminder-f", 481),
            *remind("reminder-a", 600, "taken in"), fired("reminder-a", 601),
            acknowledged("reminder-a"),
            *remind("reminder-c", 800, "cancelled"), cancelled("reminder-c"),
            *remind("reminder-late", 700, "overdue, not yet fired"),
            *remind("reminder-odd", 1000, "fired before it was due"), fired("reminder-odd", 650),
            *remind("reminder-first", 0, "at midnight"), fired("reminder-first", 1),
            *remind("reminder-last", 1440, "next day"),
            *remind("reminder-before", -1, "last day"),
        ],
        [
            due("reminder-first", 0, "done", "at midnight", "fired"),
            due("reminder-f", 480, "done", "fired", "fired"),
            due("reminder-a", 600, "done", "taken in", "taken"),
            due("reminder-late", 700, "now", "overdue, not yet fired"),
            due("reminder-p", 900, "next", "pending"),
            due("reminder-odd", 1000, "done", "fired before it was due", "fired"),
        ],
    ),
    Case(
        "items that start together are ordered by id",
        [
            visit("zz", 540, 560), health("mm", 540, 600, 60), *remind("reminder-x", 540),
            fired("reminder-x", 541),
        ],
        [
            slept("mm", 540, 600, "done", 60),
            due("reminder-x", 540, "done", fate="fired"),
            stay("zz", 540, 560, "done"),
        ],
    ),
    # --- a day with a clock change ------------------------------------------------------------
    Case(
        "a day that gains an hour holds twenty-five hours of items",
        [
            visit("in", 1470, 1490), visit("out", 1500, 1520), *remind("reminder-in", 1499),
        ],
        [stay("in", 1470, 1490, "next"), due("reminder-in", 1499, "next")],
        day="2026-11-01", zone="America/New_York", now=1000,
    ),
    Case(
        "a day that loses an hour holds twenty-three hours of items",
        [visit("in", 1370, 1390), visit("out", 1380, 1400)],
        [stay("in", 1370, 1390, "next")],
        day="2026-03-08", zone=VANCOUVER, now=1000,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_log_folds_into_the_items_of_the_day(tmp_path: Path, case: Case) -> None:
    """Each case: rows appended in order, the day cut in its zone, folded at its own now."""
    start_ms, end_ms = day_window(parse_day(case.day), ZoneInfo(case.zone))
    conn = open_event_log(tmp_path / "events.db")
    _load(conn, case.rows, start_ms)
    sources = _stores(tmp_path, ZoneInfo(case.zone))
    line = fold_day(conn, start_ms, end_ms, _ms(start_ms, case.now), sources)
    conn.close()

    assert line.items == _expected(case.items, start_ms)
    assert line.missing == []


# --- the Mac and her conversations --------------------------------------------------------------

_BUNDLES = {  # the app as the ledger names it: (TimeSink's bundle id, TimeSink's own name for it)
    "Ghostty": ("com.mitchellh.ghostty", "Ghostty"),
    "Chrome": ("com.google.Chrome", "Google Chrome"),
    "Zoom": ("us.zoom.xos", "zoom.us"),
    "WeChat": ("com.tencent.xinWeChat", "微信"),
}
_CALL = (
    'CREATE TABLE "callSpan" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "start" DATETIME NOT NULL, '
    '"end" DATETIME NOT NULL, "appBundleID" TEXT NOT NULL, "appName" TEXT NOT NULL)'
)


class Spun(NamedTuple):
    """A TimeSink span in minutes from the opening midnight; ``project`` "none" has no verdict."""

    start: float
    end: float
    app: str = "Ghostty"
    project: str = "none"


class Call(NamedTuple):
    """A TimeSink call span in minutes from the opening midnight."""

    start: float
    end: float
    app: str = "Zoom"


class Said(NamedTuple):
    """A row of memory.db's records: the minute it was written and who said it."""

    at: float
    source: str = "allen"


def _app(display: str) -> tuple[str, str]:
    return _BUNDLES.get(display, (f"com.example.{display}", display))


def _grdb(ms: int) -> str:
    """GRDB's text form of a time: UTC, a space, milliseconds, no suffix."""
    at = datetime.fromtimestamp(ms / 1000, UTC)
    return at.strftime("%Y-%m-%d %H:%M:%S.") + f"{at.microsecond // 1000:03d}"


def _add_spans(path: Path, origin: int, spans: Sequence[Spun]) -> None:
    """Append spans (and the project verdicts that name them) to a TimeSink-shaped store."""
    with closing(sqlite3.connect(path)) as conn, conn:
        known = dict(conn.execute("SELECT name, id FROM project"))
        for name in sorted({span.project for span in spans} - {"none"} - set(known)):
            conn.execute("INSERT INTO project (name) VALUES (?)", (name,))
            known[name] = conn.execute("SELECT id FROM project WHERE name=?", (name,)).fetchone()[0]
        for span in spans:
            bundle, name = _app(span.app)
            title = f"in-{span.project}"
            conn.execute(
                "INSERT INTO span (start, end, appBundleID, appName, title, document) "
                "VALUES (?, ?, ?, ?, ?, '')",
                (_grdb(_ms(origin, span.start)), _grdb(_ms(origin, span.end)), bundle, name, title),
            )
            if span.project != "none":
                conn.execute(
                    "INSERT INTO jevProjectVerdict VALUES (?, '', ?, '', ?)",
                    (bundle, title, known[span.project]),
                )


def _add_calls(path: Path, origin: int, calls: Sequence[Call]) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for call in calls:
            bundle, name = _app(call.app)
            conn.execute(
                "INSERT INTO callSpan (start, end, appBundleID, appName) VALUES (?, ?, ?, ?)",
                (_grdb(_ms(origin, call.start)), _grdb(_ms(origin, call.end)), bundle, name),
            )


def _add_talks(path: Path, origin: int, zone: ZoneInfo, talks: Sequence[Said]) -> None:
    """Append records, stamped in the owner's local time as the daemon writes them."""
    with closing(open_memory_db(path)) as conn, conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        for n, said in enumerate(talks, start=count):
            at = datetime.fromtimestamp(_ms(origin, said.at) / 1000, zone)
            conn.execute(
                "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, 'x')",
                (f"r{n}", at.isoformat(), said.source),
            )


def _stores(  # noqa: PLR0913 — the three stores of a day and the origin they are timed from.
    tmp_path: Path, zone: ZoneInfo, *, origin: int = 0, spans: Sequence[Spun] = (),
    calls: Sequence[Call] = (), talks: Sequence[Said] = (),
) -> DaySources:
    """A TimeSink-shaped store and a memory.db holding the rows, as a daemon would be wired."""
    ts, memory = tmp_path / "timesink.db", tmp_path / "memory.db"
    with closing(sqlite3.connect(ts)) as conn, conn:
        for ddl in (_SPAN, _PROJECT, _VERDICT, _CALL):
            conn.execute(ddl)
    _add_spans(ts, origin, spans)
    _add_calls(ts, origin, calls)
    open_memory_db(memory).close()
    _add_talks(memory, origin, zone, talks)
    return DaySources(zone, ts, memory)


def work(  # noqa: PLR0913 — a block's span, its active time, its two tops and its state.
    start: float, end: float, active_s: int, projects: Sequence[tuple[str, int]] = (),
    apps: Sequence[tuple[str, int]] = (), state: str = "done",
) -> Item:
    """An expected ``work`` block; the id is ``work-`` and its start in ms."""
    extra = {
        "active_s": active_s,
        "projects": [{"name": name, "seconds": seconds} for name, seconds in projects],
        "apps": [{"name": name, "seconds": seconds} for name, seconds in apps],
    }
    return Item("work-@", "work", start, end, state, extra)


def call(start: float, end: float, app: str = "Zoom", nth: int = 1, state: str = "done") -> Item:
    """An expected ``call``; the id is ``call-`` and its start, then ``-n`` for the nth together."""
    return Item("call-@" if nth == 1 else f"call-@-{nth}", "call", start, end, state, {"app": app})


def talk(start: float, end: float, turns: int, state: str = "done") -> Item:
    """An expected ``talk`` in the lane of her; the id is ``talk-`` and its start in ms."""
    return Item("talk-@", "talk", start, end, state, {"turns": turns})


class MacCase(NamedTuple):
    """TimeSink spans and calls and his records of one day, and the items that day folds to."""

    name: str
    items: list[Item]
    spans: tuple[Spun, ...] = ()
    calls: tuple[Call, ...] = ()
    talks: tuple[Said, ...] = ()
    now: float = 1500


def _s(seconds: float) -> float:
    """Seconds as the minutes the rows are timed in."""
    return seconds / 60


MAC_CASES: list[MacCase] = [
    # --- work blocks -------------------------------------------------------------------------
    MacCase(
        "one span is one block, named by its app and by no project when it has no verdict",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)])],
        spans=(Spun(540, 600),),
    ),
    MacCase(
        "spans fifteen minutes apart are one block, fifteen exactly is within, and the gap is not "
        "active time",
        [work(540, 600, 2700, apps=[("Ghostty", 2700)])],
        spans=(Spun(540, 560), Spun(575, 600)),
    ),
    MacCase(
        "a gap of a second over fifteen minutes makes two blocks",
        [
            work(540, 560, 1200, apps=[("Ghostty", 1200)]),
            work(575 + _s(1), 600, 1499, apps=[("Ghostty", 1499)]),
        ],
        spans=(Spun(540, 560), Spun(575 + _s(1), 600)),
    ),
    MacCase(
        "a block with under five minutes of active time is dropped, five exactly is kept, and "
        "the gaps inside a block do not count toward it",
        [
            work(120, 125, 300, apps=[("Ghostty", 300)]),
            work(300, 312, 300, apps=[("Ghostty", 300)]),
        ],
        spans=(
            Spun(60, 60 + _s(299)),  # 4m59s alone
            Spun(120, 125),  # 5m exactly
            Spun(200, 202), Spun(210, 212),  # 12 minutes across, 4 of them active
            Spun(300, 303), Spun(310, 312),  # 12 minutes across, 5 of them active
        ),
    ),
    MacCase(
        "two apps at once count once, and each project and app is its own union of time",
        [
            work(
                540, 640, 6000,
                projects=[("docs", 4200), ("jarvis", 3600)],
                apps=[("Chrome", 4200), ("Ghostty", 3600)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "jarvis"),
            Spun(570, 630, "Chrome", "docs"),
            Spun(620, 640, "Chrome", "docs"),
        ),
    ),
    MacCase(
        "a block names its three biggest projects and apps; the project none is never named",
        [
            work(
                540, 800, 15600,
                projects=[("alpha", 3600), ("beta", 3000), ("gamma", 2400)],
                apps=[("Notes", 4800), ("Ghostty", 3600), ("Chrome", 3000)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "alpha"),
            Spun(600, 650, "Chrome", "beta"),
            Spun(650, 690, "Xcode", "gamma"),
            Spun(690, 720, "Slack", "delta"),
            Spun(720, 800, "Notes"),  # the most time of any, and no project
        ),
    ),
    MacCase(
        "a project or app under three minutes is not named and three exactly is",
        [
            work(
                540, 605 + _s(59), 3959,
                projects=[("main", 3600), ("brief", 180)],
                apps=[("Ghostty", 3600), ("Chrome", 180)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "main"),
            Spun(600, 603, "Chrome", "brief"),
            Spun(603, 605 + _s(59), "Xcode", "tiny"),  # 2m59s
        ),
    ),
    MacCase(
        "bundle ids TimeSink spells two ways are one app, and equal times are told apart by name",
        [work(540, 580, 2400, apps=[("Chrome", 1200), ("WeChat", 1200)])],
        spans=(Spun(540, 560, "WeChat"), Spun(560, 580, "Chrome")),
    ),
    MacCase(
        "seconds are rounded to the nearest, not cut",
        [
            work(
                540, 600 + _s(0.6), 3601,
                projects=[("jarvis", 3601)], apps=[("Ghostty", 3601)],
            ),
        ],
        spans=(Spun(540, 600 + _s(0.6), "Ghostty", "jarvis"),),
    ),
    MacCase(
        "a block that crosses a midnight is returned whole, from the day it began in",
        [
            work(-50, 20, 4200, apps=[("Ghostty", 4200)]),
            work(1400, 1480, 4800, apps=[("Ghostty", 4800)]),
        ],
        spans=(Spun(-300, -240), Spun(-50, 20), Spun(1400, 1480), Spun(1600, 1700)),
    ),
    MacCase(
        "a block that only touches the day is not in it",
        [],
        spans=(Spun(-120, -60), Spun(-30, 0), Spun(1440, 1500)),
    ),
    MacCase(
        "a block with the clock inside it is now",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)], state="now")],
        spans=(Spun(540, 600),),
        now=570,
    ),
    # --- calls -------------------------------------------------------------------------------
    MacCase(
        "a call of two minutes is kept and one a second shorter is not",
        [call(600, 602)],
        calls=(Call(600, 602), Call(700, 702 - _s(1))),
    ),
    MacCase(
        "a call is named by the app the ledger names, a call across midnight is whole, and a "
        "row seen twice is one call",
        [
            call(-10, 40),
            call(1000, 1030, "WeChat"),
            call(1430, 1500),
        ],
        calls=(
            Call(-10, 40), Call(-10, 40), Call(1000, 1030, "WeChat"), Call(1430, 1500),
            Call(-200, -100),
        ),
    ),
    MacCase(
        "calls that start together are told apart, in order of when they end",
        [call(800, 815, "WeChat"), call(800, 830, "Zoom", nth=2)],
        calls=(Call(800, 830), Call(800, 815, "WeChat")),
    ),
    MacCase(
        "a call and the work around it are two items in the lane of him",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)]), call(570, 600)],
        spans=(Spun(540, 600),),
        calls=(Call(570, 600),),
    ),
    # --- talks -------------------------------------------------------------------------------
    MacCase(
        "his words ten minutes apart are one talk and a second more is another",
        [talk(480, 490, 2), talk(500 + _s(1), 500 + _s(1), 1)],
        talks=(Said(480), Said(490), Said(500 + _s(1))),
    ),
    MacCase(
        "only what he said is a talk",
        [talk(480, 480, 1)],
        talks=(Said(480), Said(481, "jarvis"), Said(482, "mail")),
    ),
    MacCase(
        "one word is a talk of one turn that starts and ends together",
        [talk(700, 700, 1)],
        talks=(Said(700),),
    ),
    MacCase(
        "a talk that crosses a midnight is returned whole",
        [talk(-5, 3, 2), talk(1435, 1445, 2)],
        talks=(Said(-5), Said(3), Said(1435), Said(1445), Said(-600), Said(1600)),
    ),
    MacCase(
        "a talk that ends as the day opens, or begins as it closes, is not in it",
        [],
        talks=(Said(-5), Said(0), Said(1440)),
    ),
    # --- all of it ---------------------------------------------------------------------------
    MacCase(
        "work, a call and a talk together, in the lanes of him and of her, in time order",
        [
            talk(530, 538, 3),
            work(540, 600, 3600, projects=[("jarvis", 3600)], apps=[("Ghostty", 3600)]),
            call(570, 590),
        ],
        spans=(Spun(540, 600, "Ghostty", "jarvis"),),
        calls=(Call(570, 590),),
        talks=(Said(530), Said(535), Said(538)),
    ),
]


@pytest.mark.parametrize("case", MAC_CASES, ids=[case.name for case in MAC_CASES])
def test_the_mac_and_his_words_fold_into_the_items_of_the_day(
    tmp_path: Path, case: MacCase,
) -> None:
    """Each case: the rows written to a TimeSink-shaped store and a memory.db, folded at its now."""
    zone = ZoneInfo(VANCOUVER)
    start_ms, end_ms = day_window(parse_day("2026-10-10"), zone)
    sources = _stores(
        tmp_path, zone, origin=start_ms, spans=case.spans, calls=case.calls, talks=case.talks,
    )
    conn = open_event_log(tmp_path / "events.db")
    line = fold_day(conn, start_ms, end_ms, _ms(start_ms, case.now), sources)
    conn.close()

    assert line.items == _expected(case.items, start_ms)
    assert line.missing == []


def test_a_block_and_a_talk_across_midnight_are_the_same_item_on_both_days(
    tmp_path: Path,
) -> None:
    """Whole, with the same id and numbers, on the day it began and the day it ended."""
    zone = ZoneInfo(VANCOUVER)
    first, second = day_window(date(2026, 10, 10), zone), day_window(date(2026, 10, 11), zone)
    sources = _stores(
        tmp_path, zone, origin=first[0],
        spans=(Spun(1400, 1480, "Ghostty", "jarvis"),), calls=(Call(1430, 1450),),
        talks=(Said(1435), Said(1445)),
    )
    conn = open_event_log(tmp_path / "events.db")
    now = _ms(first[0], 3000)
    days = [fold_day(conn, start, end, now, sources).items for start, end in (first, second)]
    conn.close()
    assert days[0] == days[1]
    assert [(i["kind"], i["start_ms"], i["end_ms"]) for i in days[0]] == [
        ("work", _ms(first[0], 1400), _ms(first[0], 1480)),
        ("call", _ms(first[0], 1430), _ms(first[0], 1450)),
        ("talk", _ms(first[0], 1435), _ms(first[0], 1445)),
    ]


def test_a_block_a_call_and_a_talk_keep_their_ids_as_the_day_goes_on(tmp_path: Path) -> None:
    """A screen diffs by id: a block's id is its first span's start, a talk's its first word's.

    A later span that extends a block, or a word that extends a talk, leaves the id alone; so does
    a second call, and a block begun later is a new id. A span that fills the gap between two
    blocks joins them under the first one's id.
    """
    zone = ZoneInfo(VANCOUVER)
    start_ms, end_ms = day_window(parse_day("2026-10-10"), zone)
    now = _ms(start_ms, 1000)
    sources = _stores(
        tmp_path, zone, origin=start_ms,
        spans=(Spun(540, 560, "Ghostty", "jarvis"), Spun(600, 620)),
        calls=(Call(545, 550),),
        talks=(Said(480), Said(485)),
    )
    assert sources.timesink is not None
    assert sources.memory_db is not None
    conn = open_event_log(tmp_path / "events.db")

    def day() -> dict[str, dict[str, Any]]:
        return {i["id"]: i for i in fold_day(conn, start_ms, end_ms, now, sources).items}

    before = day()
    work_id, other_id = f"work-{_ms(start_ms, 540)}", f"work-{_ms(start_ms, 600)}"
    call_id, talk_id = f"call-{_ms(start_ms, 545)}", f"talk-{_ms(start_ms, 480)}"
    # 540-560 and 600-620 are forty minutes apart: two blocks, each of twenty minutes
    assert set(before) == {work_id, other_id, call_id, talk_id}
    assert day() == before  # asked again, the same
    assert before[work_id]["active_s"] == 1200

    _add_spans(sources.timesink, start_ms, [Spun(560, 570, "Ghostty", "jarvis")])
    _add_calls(sources.timesink, start_ms, [Call(900, 905)])
    _add_talks(sources.memory_db, start_ms, zone, [Said(490)])
    grown = day()
    assert set(grown) == {*before, f"call-{_ms(start_ms, 900)}"}  # only the new call is new
    assert grown[work_id]["end_ms"] == _ms(start_ms, 570)
    assert grown[work_id]["active_s"] == 1800
    assert grown[work_id]["start_ms"] == before[work_id]["start_ms"]
    assert (grown[talk_id]["end_ms"], grown[talk_id]["turns"]) == (_ms(start_ms, 490), 3)
    assert grown[other_id] == before[other_id]

    _add_spans(sources.timesink, start_ms, [Spun(570, 600, "Ghostty", "jarvis")])  # fills the gap
    joined = day()
    assert other_id not in joined
    assert joined[work_id]["end_ms"] == _ms(start_ms, 620)
    assert joined[work_id]["active_s"] == 4800
    conn.close()


# --- the Mac and her conversations against the ledger's own numbers -------------------------------

_ACTIVE = re.compile(r"Computer: .*, active (\d+)h(\d\d)m")


def _line(text: str, label: str) -> str:
    return next(line for line in text.splitlines() if line.lstrip().startswith(label))


@pytest.mark.parametrize(
    ("exact", "spans", "calls", "talks"),
    [
        pytest.param(
            True,
            (
                Spun(540, 600, "Ghostty", "jarvis"), Spun(570, 630, "Chrome", "docs"),
                Spun(645, 705, "Ghostty", "jarvis"),
                Spun(760, 800, "Ghostty", "jarvis"), Spun(800, 820, "Chrome", "docs"),
                Spun(1200, 1290, "Ghostty", "jarvis"),
            ),
            (Call(600, 630), Call(1000, 1015)),
            (Said(480), Said(483), Said(495), Said(1300), Said(1305)),
            id="whole minutes, two apps at once, a gap of fifteen minutes inside one block",
        ),
        pytest.param(
            False,
            (
                Spun(540 + _s(20), 601 + _s(40), "Ghostty", "jarvis"),
                Spun(580 + _s(5), 650 + _s(40), "Chrome", "docs"),
                Spun(900 + _s(50), 1000 + _s(10), "Ghostty", "jarvis"),
            ),
            (Call(300 + _s(30), 345 + _s(7)),),
            (Said(100), Said(120)),
            id="seconds that do not fall on a minute",
        ),
    ],
)
def test_the_blocks_add_up_to_what_the_ledger_tells_her_about_the_same_day(
    tmp_path: Path, *, exact: bool, spans: tuple[Spun, ...], calls: tuple[Call, ...],
    talks: tuple[Said, ...],
) -> None:
    """The oracle is ``ledger.day_numbers_text``, the "last seven days in full" line she is told.

    For a day whose blocks all lie inside it and none is dropped: the active time is the sum of
    the blocks' ``active_s``; each project and app is the sum of its seconds across the blocks;
    each call app is the sum of its calls; "talked to her N times" is the sum of the turns.
    """
    zone = ZoneInfo(VANCOUVER)
    day = date(2026, 10, 8)
    start_ms, end_ms = day_window(day, zone)
    sources = _stores(tmp_path, zone, origin=start_ms, spans=spans, calls=calls, talks=talks)
    assert sources.timesink is not None
    assert sources.memory_db is not None
    conn = open_event_log(tmp_path / "events.db")
    after = _ms(end_ms, 12 * 60)
    items = fold_day(conn, start_ms, end_ms, after, sources).items
    conn.close()
    blocks = [i for i in items if i["kind"] == "work"]
    assert blocks
    assert all(start_ms <= b["start_ms"] and b["end_ms"] <= end_ms for b in blocks)

    ledger = LedgerSources(sources.memory_db, tmp_path / "events.db", sources.timesink, zone)
    told = day_numbers_text(ledger, day, datetime.fromtimestamp(after / 1000, zone))
    found = _ACTIVE.search(told)
    assert found is not None, told
    hours, minutes = int(found.group(1)), int(found.group(2))

    total = sum(b["active_s"] for b in blocks)
    assert hm(total) == f"{hours}h{minutes:02d}m"
    if exact:  # whole minutes: nothing was rounded, so the two agree to the second
        assert total == hours * 3600 + minutes * 60

    def named(kind: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for block in blocks:
            for one in block[kind]:
                out[one["name"]] = out.get(one["name"], 0) + one["seconds"]
        return out

    for label, mine in (("Projects (TimeSink):", named("projects")), ("Top apps:", named("apps"))):
        theirs = _line(told, label).split(":", 1)[1]
        assert sorted(f"{k} {hm(v)}" for k, v in mine.items()) == sorted(
            part.strip() for part in theirs.split(","))

    ringing: dict[str, float] = {}
    for i in items:
        if i["kind"] == "call":
            ringing[i["app"]] = ringing.get(i["app"], 0) + (i["end_ms"] - i["start_ms"]) / 1000
    assert ", ".join(f"{app} call {hm(s)}" for app, s in sorted(ringing.items())) in told
    turns = sum(i["turns"] for i in items if i["kind"] == "talk")
    assert f"Talked to her {turns} times." in told


def test_the_ledger_reader_gives_each_half_only_when_asked_and_cuts_nothing(
    tmp_path: Path,
) -> None:
    """``window_activity``: spans and calls overlapping the window whole, his words inside it.

    A path left out is not read, so its half is empty; a memory store with no records raises, a
    TimeSink path with no file reads as empty, as the ledger's own numbers read it.
    """
    zone = ZoneInfo(VANCOUVER)
    origin = day_window(date(2026, 10, 8), zone)[0]
    stores = _stores(
        tmp_path, zone, origin=origin,
        spans=(Spun(-90, -30), Spun(-30, 30, "Chrome", "docs"), Spun(100, 130), Spun(200, 230)),
        calls=(Call(-50, 10), Call(500, 520)),
        talks=(Said(-20), Said(60), Said(61, "mail"), Said(120)),
    )
    begin, end = (datetime.fromtimestamp(_ms(origin, m) / 1000, UTC) for m in (0, 180))
    both = window_activity(
        begin, end, zone, timesink=stores.timesink, memory_db=stores.memory_db,
    )
    assert [(s.start, s.end, s.app, s.project) for s in both.spans] == [
        (begin - timedelta(minutes=30), begin + timedelta(minutes=30), "Chrome", "docs"),
        (begin + timedelta(minutes=100), begin + timedelta(minutes=130), "Ghostty", "none"),
    ]
    assert [(c.start, c.end, c.app) for c in both.calls] == [
        (begin - timedelta(minutes=50), begin + timedelta(minutes=10), "Zoom"),
    ]
    assert both.talks == [begin + timedelta(minutes=m) for m in (60, 120)]

    only_mac = window_activity(begin, end, zone, timesink=stores.timesink)
    assert (len(only_mac.spans), len(only_mac.calls), only_mac.talks) == (2, 1, [])
    only_talks = window_activity(begin, end, zone, memory_db=stores.memory_db)
    assert (only_talks.spans, only_talks.calls, len(only_talks.talks)) == ([], [], 2)
    assert window_activity(begin, end, zone) == ([], [], [])
    assert window_activity(begin, end, zone, timesink=tmp_path / "gone.db") == ([], [], [])

    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(sqlite3.OperationalError):
        window_activity(begin, end, zone, memory_db=bare)


# --- a source with no store, and one that raises -------------------------------------------------


def _phone_and_reminder(conn: sqlite3.Connection, start_ms: int) -> None:
    _load(conn, [visit("a1", 540, 600, "Cafe"), *remind("reminder-r", 800)], start_ms)


def test_mac_and_talks_are_missing_when_their_store_is_not_there(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """No path, no file, a directory, no memory store: named in ``missing``, never logged.

    The phone and the reminders answer the same in every case, and so does the source whose
    store is there.
    """
    zone = ZoneInfo(VANCOUVER)
    start_ms, end_ms = day_window(parse_day("2026-10-10"), zone)
    whole = _stores(
        tmp_path, zone, origin=start_ms, spans=(Spun(540, 600),), talks=(Said(480),),
    )
    conn = open_event_log(tmp_path / "events.db")
    _phone_and_reminder(conn, start_ms)
    now = _ms(start_ms, 720)
    always = ["a1", "reminder-r"]  # the phone's stay and the reminder
    mac, words = [f"work-{_ms(start_ms, 540)}"], [f"talk-{_ms(start_ms, 480)}"]

    def ids(sources: DaySources) -> tuple[list[str], list[str]]:
        line = fold_day(conn, start_ms, end_ms, now, sources)
        return sorted(i["id"] for i in line.items), line.missing

    with caplog.at_level(logging.DEBUG, logger="jarvis.state.day_line"):
        assert ids(whole) == (sorted([*always, *mac, *words]), [])
        assert ids(DaySources(zone, None, whole.memory_db)) == (sorted([*always, *words]), ["mac"])
        gone = tmp_path / "not-here.db"
        assert ids(DaySources(zone, gone, whole.memory_db)) == (sorted([*always, *words]), ["mac"])
        assert not gone.exists()  # looking did not create it
        assert ids(DaySources(zone, tmp_path, whole.memory_db)) == (
            sorted([*always, *words]), ["mac"],
        )
        assert ids(DaySources(zone, whole.timesink, None)) == (sorted([*always, *mac]), ["talks"])
        assert ids(DaySources(zone)) == (always, ["mac", "talks"])
    conn.close()
    assert [r for r in caplog.records if r.name == "jarvis.state.day_line"] == []


def test_a_source_that_raises_is_logged_and_named_and_the_others_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory.db with no records table loses the talks; a failing TimeSink read loses the Mac."""
    zone = ZoneInfo(VANCOUVER)
    start_ms, end_ms = day_window(parse_day("2026-10-10"), zone)
    whole = _stores(tmp_path, zone, origin=start_ms, spans=(Spun(540, 600),), talks=(Said(480),))
    conn = open_event_log(tmp_path / "events.db")
    _phone_and_reminder(conn, start_ms)
    now = _ms(start_ms, 720)

    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()  # a store with no records table
    with caplog.at_level(logging.ERROR, logger="jarvis.state.day_line"):
        line = fold_day(conn, start_ms, end_ms, now, DaySources(zone, whole.timesink, bare))
    assert ([i["kind"] for i in line.items], line.missing) == (
        ["stay", "work", "reminder"], ["talks"],
    )
    assert any("talks" in record.getMessage() for record in caplog.records)

    real = window_activity

    def refuses(*args: Any, timesink: Path | None = None, **kw: Any) -> Any:  # noqa: ANN401
        if timesink is not None:
            msg = "disk"
            raise sqlite3.OperationalError(msg)
        return real(*args, **kw)

    monkeypatch.setattr(day_line, "window_activity", refuses)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="jarvis.state.day_line"):
        line = fold_day(conn, start_ms, end_ms, now, whole)
    conn.close()
    assert ([i["kind"] for i in line.items], line.missing) == (
        ["talk", "stay", "reminder"], ["mac"],
    )
    assert any("mac" in record.getMessage() for record in caplog.records)


# --- the window ----------------------------------------------------------------------------------


def _oracle(day: date, zone: ZoneInfo) -> tuple[int, int]:
    """The first instants of ``day`` and of the day after, found by walking UTC minute by minute."""
    first = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) - 16 * 3600

    def opens(target: date) -> int:
        at = first
        while datetime.fromtimestamp(at, zone).date() < target:
            at += 60
        return at * 1000

    return opens(day), opens(day + timedelta(days=1))


@pytest.mark.parametrize(
    ("zone", "day", "hours"),
    [
        ("America/New_York", "2026-11-01", 25),
        ("America/New_York", "2026-03-08", 23),
        (VANCOUVER, "2026-03-08", 23),
        ("Europe/London", "2026-10-25", 25),
        ("Australia/Sydney", "2026-10-04", 23),
        ("America/Havana", "2026-03-08", 23),  # the clock skips midnight: the day opens at 01:00
        ("Asia/Kolkata", "2026-10-10", 24),
        ("Etc/UTC", "2026-10-10", 24),
    ],
)
def test_a_day_runs_from_local_midnight_to_local_midnight_whatever_the_clock_does(
    zone: str, day: str, hours: int,
) -> None:
    """A clock change makes a day 23 or 25 hours, not 24 plus a guess."""
    window = day_window(parse_day(day), ZoneInfo(zone))
    assert window == _oracle(parse_day(day), ZoneInfo(zone))
    assert (window[1] - window[0]) // (60 * MINUTE) == hours


def test_the_window_follows_the_tz_database_of_the_machine_for_vancouver_in_november() -> None:
    """Whether 2026-11-01 changes the clock in Vancouver is the machine's tz database's to say."""
    window = day_window(date(2026, 11, 1), ZoneInfo(VANCOUVER))
    assert window == _oracle(date(2026, 11, 1), ZoneInfo(VANCOUVER))
    assert 24 <= (window[1] - window[0]) // (60 * MINUTE) <= 25


@pytest.mark.parametrize("text", [
    "2026-10-10", "0001-01-01", "9999-12-31",
])
def test_the_day_text_is_the_calendar_form_and_only_that(text: str) -> None:
    """``YYYY-MM-DD`` names a day."""
    assert parse_day(text).isoformat() == text


@pytest.mark.parametrize("text", [
    "", " ", "2026-10-10 ", " 2026-10-10", "2026-10-10\n", "20261010", "2026-W41-6", "2026-283",
    "2026-1-5", "10/10/2026", "2026-13-01", "2026-02-30", "2026-00-10", "0000-01-01",
    "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0661\u0660",  # Arabic-Indic digits
    "2026-10-10T00:00", "today", "2026-10-10x",
])
def test_any_other_text_is_not_a_day(text: str) -> None:
    """A date in another form, or not on the calendar, is refused rather than guessed."""
    with pytest.raises(ValueError, match=r"date must be YYYY-MM-DD"):
        parse_day(text)


def test_a_day_at_the_end_of_the_calendar_has_no_window() -> None:
    """The next midnight does not exist, so it is a ValueError (the route's 400), not a crash."""
    with pytest.raises(ValueError, match="out of range"):
        day_window(date(9999, 12, 31), ZoneInfo(VANCOUVER))


# --- ids ---------------------------------------------------------------------------------------


def test_ids_are_the_same_on_every_call_and_when_the_log_grows(tmp_path: Path) -> None:
    """A screen diffs by id.

    A stay keeps its id when its departure arrives, a move when an earlier segment joins it, a
    sleep block when a later stage does.
    """
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZoneInfo(VANCOUVER))
    now = _ms(start_ms, 720)
    sources = _stores(tmp_path, ZoneInfo(VANCOUVER))
    conn = open_event_log(tmp_path / "events.db")
    _load(conn, [
        visit("a1", 540), motion("m1", "walking", 400, 410), health("s1", 100, 200, 100),
        *remind("reminder-r", 800),
    ], start_ms)

    def ids() -> list[str]:
        return [item["id"] for item in fold_day(conn, start_ms, end_ms, now, sources).items]

    before = ids()
    assert before == ["s1", "m1", "a1", "reminder-r"]
    assert ids() == before

    _load(conn, [
        visit("a2", 540, 570), motion("m0", "walking", 396, 399), health("s2", 210, 300, 80),
    ], start_ms)
    items = fold_day(conn, start_ms, end_ms, now, sources).items
    conn.close()
    assert [item["id"] for item in items] == before
    by_id = {item["id"]: item for item in items}
    assert by_id["a1"]["end_ms"] == _ms(start_ms, 570)
    assert (by_id["m1"]["start_ms"], by_id["m1"]["end_ms"]) == (
        _ms(start_ms, 396), _ms(start_ms, 410),
    )
    assert (by_id["s1"]["end_ms"], by_id["s1"]["minutes"]) == (_ms(start_ms, 300), 180)


# --- a source that cannot be read ----------------------------------------------------------------


def _raw(conn: sqlite3.Connection, uid: str, kind: str, payload: str, ts: int) -> None:
    """A row the registry would have refused, as a damaged log would hold it."""
    conn.execute(
        "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json) "
        "VALUES (?, ?, 1, ?, ?)",
        (uid, kind, ts, payload),
    )
    conn.commit()


def test_a_source_that_cannot_be_read_is_named_and_the_others_still_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A damaged phone row loses the phone's items, a damaged reminder row the reminders'."""
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZoneInfo(VANCOUVER))
    now = _ms(start_ms, 720)
    good = [visit("a1", 540, 600), *remind("reminder-r", 800)]
    bad_phone = ("bad-motion", MOTION, '{"nothing": 1}')
    bad_reminder = ("bad-reminder", "reminder.scheduled", "{}")

    sources = _stores(tmp_path, ZoneInfo(VANCOUVER))

    def read(name: str, damage: list[tuple[str, str, str]]) -> day_line.DayLine:
        conn = open_event_log(tmp_path / f"{name}.db")
        _load(conn, good, start_ms)
        for uid, kind, payload in damage:
            _raw(conn, uid, kind, payload, _ms(start_ms, 650))
        try:
            return fold_day(conn, start_ms, end_ms, now, sources)
        finally:
            conn.close()

    healthy = read("healthy", [])
    assert ([item["id"] for item in healthy.items], healthy.missing) == (
        ["a1", "reminder-r"], [],
    )
    with caplog.at_level(logging.ERROR, logger="jarvis.state.day_line"):
        no_phone = read("phone", [bad_phone])
    assert ([item["id"] for item in no_phone.items], no_phone.missing) == (
        ["reminder-r"], ["phone"],
    )
    assert any("phone" in record.getMessage() for record in caplog.records)
    no_reminders = read("reminders", [bad_reminder])
    assert ([item["id"] for item in no_reminders.items], no_reminders.missing) == (
        ["a1"], ["reminders"],
    )
    nothing = read("both", [bad_phone, bad_reminder])
    assert (nothing.items, nothing.missing) == ([], ["phone", "reminders"])


def test_a_reminder_fold_that_raises_is_the_reminders_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the fold raises, it is the source that is missing, not the day."""
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZoneInfo(VANCOUVER))
    conn = open_event_log(tmp_path / "events.db")
    _load(conn, [visit("a1", 540, 600), *remind("reminder-r", 800)], start_ms)

    def boom(_conn: sqlite3.Connection) -> dict[str, reminders.Reminder]:
        msg = "disk"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(reminders, "fold", boom)
    line = fold_day(
        conn, start_ms, end_ms, _ms(start_ms, 720), _stores(tmp_path, ZoneInfo(VANCOUVER)),
    )
    conn.close()
    assert ([item["id"] for item in line.items], line.missing) == (["a1"], ["reminders"])


def test_the_phone_types_the_fold_reads_are_the_phone_types_the_route_accepts() -> None:
    """The names the fold reads are the ones a phone may write, and the rows here use them."""
    read = (VISIT_EVENT, day_line.MOTION_EVENT, day_line.HEALTH_EVENT)
    assert read == (VISIT, MOTION, HEALTH)
    assert set(read) <= phone_events.PHONE_EVENT_TYPES


# --- the route --------------------------------------------------------------------------------


def _client(
    tmp_path: Path, *, zone: str | None = VANCOUVER, phone: bool = False, memory: bool = True,
    mac: bool = True,
) -> tuple[TestClient, Path]:
    """A brain's app as a peer on the private network reaches it, with the day route wired.

    The TimeSink-shaped store (``timesink.db``) and memory.db in ``tmp_path`` start empty; ``mac``
    and ``memory`` say whether the route is wired to them, as a daemon without either is not.
    """
    log = tmp_path / "events.db"
    conn = _brain_log(log)
    stores = _stores(tmp_path, ZoneInfo(zone or VANCOUVER))
    hub = TerminalHub(events=BrainEvents(conn)) if phone else None
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        terminals=hub,
        device_name=functools.partial(device_name_for_token, tmp_path) if phone else None,
        day_read=functools.partial(
            _serve_day, log, None if zone is None else ZoneInfo(zone),
            stores.memory_db if memory else None, stores.timesink if mac else None,
        ),
    ))
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000)), log


def _loopback(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000))


def test_a_bad_date_is_a_400_and_a_good_one_is_the_day(tmp_path: Path) -> None:
    """Every form but ``YYYY-MM-DD`` is refused with the reason; the answer names the day."""
    token = pair_device(tmp_path, "phone")
    client, _log = _client(tmp_path)
    for bad in ("", "tomorrow", "2026-13-01", "2026-02-30", "20261010", "2026-10-10T00:00",
                "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0661\u0660"):  # Arabic-Indic digits
        reply = client.get(DAY_PATH, params={"date": bad}, headers=_bearer(token))
        assert reply.status_code == 400, bad
        assert reply.json() == {"detail": "date must be YYYY-MM-DD"}, bad
    reply = client.get(DAY_PATH, params={"date": "9999-12-31"}, headers=_bearer(token))
    assert (reply.status_code, reply.json()) == (400, {"detail": "date is out of range"})

    reply = client.get(DAY_PATH, params={"date": "2026-10-10"}, headers=_bearer(token))
    assert reply.status_code == 200
    body = reply.json()
    start_ms, end_ms = day_window(date(2026, 10, 10), ZoneInfo(VANCOUVER))
    assert body["date"] == "2026-10-10"
    assert (body["tz"], body["start_ms"], body["end_ms"]) == (VANCOUVER, start_ms, end_ms)
    assert (body["items"], body["missing"]) == ([], [])
    assert abs(body["now_ms"] - time.time() * 1000) < 60_000


def test_no_date_is_today_in_the_owners_zone(tmp_path: Path) -> None:
    """The day is cut where the owner is, not where the server's clock happens to be."""
    token = pair_device(tmp_path, "phone")
    client, log = _client(tmp_path)
    headers = _bearer(token)
    opened = client.get(DAY_PATH, headers=headers).json()
    zone = ZoneInfo(VANCOUVER)
    today = datetime.fromtimestamp(opened["now_ms"] / 1000, zone).date()
    assert opened["date"] == today.isoformat()
    assert (opened["start_ms"], opened["end_ms"]) == day_window(today, zone)

    conn = open_event_log(log)
    _load(conn, remind("reminder-today", 1, "water the plants"), opened["start_ms"])
    conn.close()
    body = client.get(DAY_PATH, headers=headers).json()
    assert body["date"] == opened["date"]
    assert [(item["id"], item["lane"], item["text"]) for item in body["items"]] == [
        ("reminder-today", "her", "water the plants"),
    ]
    assert body["items"][0]["start_ms"] == opened["start_ms"] + MINUTE
    assert client.get(DAY_PATH, params={"date": (today + timedelta(days=1)).isoformat()},
                      headers=headers).json()["items"] == []


def test_with_no_zone_set_the_day_is_cut_in_the_machines_zone(tmp_path: Path) -> None:
    """``work_state.timezone`` unset: the same fallback every other day window uses."""
    name, zone = resolve_zone(None, None)
    token = pair_device(tmp_path, "phone")
    client, _log = _client(tmp_path, zone=None)
    answer = client.get(DAY_PATH, params={"date": "2026-10-10"}, headers=_bearer(token)).json()
    assert answer["tz"] == name
    assert (answer["start_ms"], answer["end_ms"]) == day_window(date(2026, 10, 10), zone)


def test_the_day_opens_to_a_paired_device_and_to_the_local_key_and_to_no_one_else(
    tmp_path: Path,
) -> None:
    """A remote peer needs a paired device's token; on loopback the local key, as for any route."""
    token = pair_device(tmp_path, "phone")
    client, _log = _client(tmp_path)
    key = local_key(tmp_path)
    local = _loopback(client)

    assert client.get(DAY_PATH).status_code == 401
    assert client.get(DAY_PATH, headers=_bearer("wrong")).status_code == 401
    assert client.get(DAY_PATH, headers=_bearer(key)).status_code == 401  # the key is not for peers
    assert client.get(DAY_PATH, headers={"Authorization": token}).status_code == 401
    # refused before the date is looked at
    assert client.get(DAY_PATH, params={"date": "bad"}).status_code == 401
    assert client.get(DAY_PATH, headers=_bearer(token)).status_code == 200

    assert local.get(DAY_PATH).status_code == 401
    assert local.get(DAY_PATH, headers=_bearer(token)).status_code == 401
    assert local.get(DAY_PATH, headers=_bearer(key)).status_code == 200


def test_without_a_day_reader_there_is_no_route(tmp_path: Path) -> None:
    """A daemon that does not wire it answers 404 to every credential."""
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1", broadcaster=InherentBroadcaster(),
    ))
    require_local_key(app, functools.partial(local_key_matches, local_key(tmp_path)))
    local = TestClient(app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000))
    assert local.get(DAY_PATH, headers=_bearer(local_key(tmp_path))).status_code == 404


def test_a_phones_batch_posted_to_the_brain_comes_back_as_the_day(tmp_path: Path) -> None:
    """The whole path: a phone's frames over HTTP, the log, the fold, the answer.

    The day is yesterday in the owner's zone, so every time in it is in the past and inside the
    week the log trusts a phone's clock for.
    """
    token = pair_device(tmp_path, "phone")
    client, _log = _client(tmp_path, phone=True)
    zone = ZoneInfo(VANCOUVER)
    now = datetime.now(zone)
    day = (now - timedelta(days=1)).date()
    start_ms, end_ms = day_window(day, zone)

    def frame(n: int, kind: str, payload: dict[str, Any], at: float) -> dict[str, Any]:
        return {
            "event_uid": f"{n:032x}", "event_type": kind, "ts_epoch_ms": _ms(start_ms, at),
            "payload": {k: _ms(start_ms, v) if k in _MINUTE_KEYS else v
                        for k, v in payload.items()},
        }

    here = {"lat": 48.4284, "lng": -123.3656, "accuracy_m": 65.0}
    frames = [
        frame(1, VISIT, {**here, "arrived_at_ms": 540, "place": "Cafe"}, 541),
        frame(
            2, VISIT, {**here, "arrived_at_ms": 540, "departed_at_ms": 600, "place": "Cafe"}, 601,
        ),
        frame(3, MOTION, {"activity": "walking", "confidence": "high", "started_at_ms": 600,
                          "ended_at_ms": 615}, 616),
        frame(4, MOTION, {"activity": "walking", "confidence": "high", "started_at_ms": 616,
                          "ended_at_ms": 630}, 631),
        frame(5, HEALTH, {"metric": "sleep_min", "start_ms": -60, "end_ms": 200, "value": 200},
              201),
        frame(6, HEALTH, {"metric": "sleep_min", "start_ms": 205, "end_ms": 400, "value": 190},
              401),
    ]
    posted = client.post(PHONE_EVENTS_PATH, json={"events": frames}, headers=_bearer(token))
    assert [ack["ok"] for ack in posted.json()["acks"]] == [True] * 6

    body = client.get(DAY_PATH, params={"date": day.isoformat()}, headers=_bearer(token)).json()
    assert body["missing"] == []
    shape = [(i["kind"], i["id"], i["start_ms"], i["end_ms"], i["state"]) for i in body["items"]]
    assert shape == [
        ("sleep", f"{5:032x}", _ms(start_ms, -60), _ms(start_ms, 400), "done"),
        ("stay", f"{1:032x}", _ms(start_ms, 540), _ms(start_ms, 600), "done"),
        ("move", f"{3:032x}", _ms(start_ms, 600), _ms(start_ms, 630), "done"),
    ]
    assert body["items"][0]["minutes"] == 390
    assert body["items"][1]["place"] == "Cafe"
    assert (body["start_ms"], body["end_ms"]) == (start_ms, end_ms)


def test_the_route_serves_his_mac_and_his_talks_and_names_what_is_not_connected(
    tmp_path: Path,
) -> None:
    """The stores the daemon is wired to answer; one it is not wired to is in ``missing``.

    The day is in the past so every time in it is, and the ``ledger:`` block is not involved: the
    route is wired from the stores alone.
    """
    token = pair_device(tmp_path, "phone")
    zone = ZoneInfo(VANCOUVER)
    start_ms, _end_ms = day_window(date(2026, 10, 8), zone)
    params = {"date": "2026-10-08"}
    spans = (Spun(540, 600, "Ghostty", "jarvis"), Spun(1400, 1480, "Chrome"))

    def ask(client: TestClient) -> dict[str, Any]:
        return client.get(DAY_PATH, params=params, headers=_bearer(token)).json()  # type: ignore[no-any-return]

    def stock(root: Path) -> None:
        _add_spans(root / "timesink.db", start_ms, spans)
        _add_calls(root / "timesink.db", start_ms, [Call(570, 590)])
        _add_talks(root / "memory.db", start_ms, zone, [Said(480), Said(485)])

    client, _log = _client(tmp_path)
    stock(tmp_path)
    body = ask(client)
    assert body["missing"] == []
    assert [(i["kind"], i["lane"], i["state"]) for i in body["items"]] == [
        ("talk", "her", "done"), ("work", "you", "done"), ("call", "you", "done"),
        ("work", "you", "done"),
    ]
    assert [i["id"] for i in body["items"] if i["kind"] != "work"] == [
        f"talk-{_ms(start_ms, 480)}", f"call-{_ms(start_ms, 570)}",
    ]
    jarvis = body["items"][1]
    assert (jarvis["active_s"], jarvis["projects"], jarvis["apps"]) == (
        3600, [{"name": "jarvis", "seconds": 3600}], [{"name": "Ghostty", "seconds": 3600}],
    )
    assert body["items"][0]["turns"] == 2
    assert body["items"][3]["end_ms"] == _ms(start_ms, 1480)  # past the day's end, whole

    # no memory store: the talks go, the Mac stays
    root = tmp_path / "no-memory"
    root.mkdir()
    client, _log = _client(root, memory=False)
    stock(root)
    token = pair_device(root, "phone")
    body = ask(client)
    assert (body["missing"], [i["kind"] for i in body["items"]]) == (
        ["talks"], ["work", "call", "work"],
    )

    # no TimeSink: the Mac goes, the talks stay
    root = tmp_path / "no-mac"
    root.mkdir()
    client, _log = _client(root, mac=False)
    stock(root)
    token = pair_device(root, "phone")
    body = ask(client)
    assert (body["missing"], [i["kind"] for i in body["items"]]) == (["mac"], ["talk"])

    # neither
    root = tmp_path / "neither"
    root.mkdir()
    client, _log = _client(root, memory=False, mac=False)
    token = pair_device(root, "phone")
    body = ask(client)
    assert (body["missing"], body["items"]) == (["mac", "talks"], [])


def test_today_is_where_the_owner_is_not_where_the_server_clock_is(tmp_path: Path) -> None:
    """06:30 UTC on 10 October is still the evening of 9 October in Vancouver."""
    log = tmp_path / "events.db"
    open_event_log(log).close()
    now_ms = int(datetime(2026, 10, 10, 6, 30, tzinfo=UTC).timestamp()) * 1000
    body = _read_day(log, ZoneInfo(VANCOUVER), None, None, None, now_ms)
    assert (body["date"], body["tz"], body["now_ms"]) == ("2026-10-09", VANCOUVER, now_ms)
    assert (body["start_ms"], body["end_ms"]) == day_window(date(2026, 10, 9), ZoneInfo(VANCOUVER))
    assert set(body) == {"date", "tz", "start_ms", "end_ms", "now_ms", "items", "missing"}
    assert (body["items"], body["missing"]) == ([], ["mac", "talks"])  # no store was given
