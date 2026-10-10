"""ADR 0199: the brain serves each day as one timeline, folded from the log when asked.

Acceptance checks, each against the real code and a real event log:

- the fold, as a table of log rows to the items of a day: a visit reported twice, an open stay,
  an arrival that a later stay ends, motion merged, split, dropped and ended by its neighbour,
  a move inside a stay, sleep stages across midnight, reminders pending, fired and cancelled,
  the window across a clock change, an item that overlaps the window returned whole, and the order
  of items that start together;
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
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.inherent_loop import _read_day, _serve_day
from jarvis.state import day_line, reminders
from jarvis.state.daily_report import resolve_zone
from jarvis.state.day_line import day_window, fold_day, parse_day
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.phone_location import VISIT_EVENT
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import phone_events
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from tests.integration.test_terminal_voice import REMOTE, _bearer, _brain_log

if TYPE_CHECKING:
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
    return [
        {
            "id": item.uid, "lane": "her" if item.kind == "reminder" else "you", "kind": item.kind,
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
    line = fold_day(conn, start_ms, end_ms, _ms(start_ms, case.now))
    conn.close()

    assert line.items == _expected(case.items, start_ms)
    assert line.missing == []


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
    conn = open_event_log(tmp_path / "events.db")
    _load(conn, [
        visit("a1", 540), motion("m1", "walking", 400, 410), health("s1", 100, 200, 100),
        *remind("reminder-r", 800),
    ], start_ms)

    def ids() -> list[str]:
        return [item["id"] for item in fold_day(conn, start_ms, end_ms, now).items]

    before = ids()
    assert before == ["s1", "m1", "a1", "reminder-r"]
    assert ids() == before

    _load(conn, [
        visit("a2", 540, 570), motion("m0", "walking", 396, 399), health("s2", 210, 300, 80),
    ], start_ms)
    items = fold_day(conn, start_ms, end_ms, now).items
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

    def read(name: str, damage: list[tuple[str, str, str]]) -> day_line.DayLine:
        conn = open_event_log(tmp_path / f"{name}.db")
        _load(conn, good, start_ms)
        for uid, kind, payload in damage:
            _raw(conn, uid, kind, payload, _ms(start_ms, 650))
        try:
            return fold_day(conn, start_ms, end_ms, now)
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
    line = fold_day(conn, start_ms, end_ms, _ms(start_ms, 720))
    conn.close()
    assert ([item["id"] for item in line.items], line.missing) == (["a1"], ["reminders"])


def test_the_phone_types_the_fold_reads_are_the_phone_types_the_route_accepts() -> None:
    """The names the fold reads are the ones a phone may write, and the rows here use them."""
    read = (VISIT_EVENT, day_line.MOTION_EVENT, day_line.HEALTH_EVENT)
    assert read == (VISIT, MOTION, HEALTH)
    assert set(read) <= phone_events.PHONE_EVENT_TYPES


# --- the route --------------------------------------------------------------------------------


def _client(
    tmp_path: Path, *, zone: str | None = VANCOUVER, phone: bool = False,
) -> tuple[TestClient, Path]:
    """A brain's app as a peer on the private network reaches it, with the day route wired."""
    log = tmp_path / "events.db"
    conn = _brain_log(log)
    hub = TerminalHub(events=BrainEvents(conn)) if phone else None
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        terminals=hub,
        device_name=functools.partial(device_name_for_token, tmp_path) if phone else None,
        day_read=functools.partial(_serve_day, log, None if zone is None else ZoneInfo(zone)),
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


def test_today_is_where_the_owner_is_not_where_the_server_clock_is(tmp_path: Path) -> None:
    """06:30 UTC on 10 October is still the evening of 9 October in Vancouver."""
    log = tmp_path / "events.db"
    open_event_log(log).close()
    now_ms = int(datetime(2026, 10, 10, 6, 30, tzinfo=UTC).timestamp()) * 1000
    body = _read_day(log, ZoneInfo(VANCOUVER), None, now_ms)
    assert (body["date"], body["tz"], body["now_ms"]) == ("2026-10-09", VANCOUVER, now_ms)
    assert (body["start_ms"], body["end_ms"]) == day_window(date(2026, 10, 9), ZoneInfo(VANCOUVER))
    assert set(body) == {"date", "tz", "start_ms", "end_ms", "now_ms", "items", "missing"}
