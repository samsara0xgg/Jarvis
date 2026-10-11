"""ADR 0230: the day line shows what is planned, his Outlook events and his pinned departures.

Acceptance checks, each against the real fold, a real event log and the real ``Home`` reader over
a fake ``microsoft`` server that answers in Graph's calendarView shape (no network):

- an event is an item in his lane, ``next`` until it starts, ``now`` while it runs and ``done``
  once the clock passes its end; its id is a short hash, not Graph's long opaque id; a cancelled
  event is not there; the calendar is only ever read;
- an all-day event is a label of the day, not an item, and only on the days it covers (Graph also
  returns the all-day events of the neighbouring days when the window is cut in local midnights);
- a calendar that cannot be read (a tool error or a timeout, a garbled answer, a row without a
  start) puts ``calendar`` in ``missing`` and adds nothing while the rest of the day is served;
  one that is not connected does the same without a log line, and a day with no reader at all
  says nothing;
- the Graph read is kept per day: five minutes for today, thirty for any other day, and a failed
  read is not kept;
- the pinned trips of a pin are departure items; the reminder that rings for a trip on the line is
  not a second item, but a reminder whose trip is not on the line stays; a past day shows the pin
  that was held at its end, and a trip is only on the day it leaves;
- a pin that cannot be folded is ``departures`` missing and its reminders stay;
- ``GET /inherent/day`` serves ``labels`` beside the items.

Times in the table are minutes from the local midnight that opens 2026-10-10 (Vancouver, UTC-7).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import logging
import re
from contextlib import closing
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.execution.tools import ToolError
from jarvis.runtime.home import Home
from jarvis.runtime.inherent_loop import _read_day, _serve_day
from jarvis.state import departures
from jarvis.state.day_line import DayLine, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_day_line import MINUTE, Row, _load, _raw, _stores, fired, remind, visit

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from pathlib import Path

VANCOUVER = "America/Vancouver"
ZONE = ZoneInfo(VANCOUVER)
DAY = "2026-10-10"
ORIGIN = day_window(parse_day(DAY), ZONE)[0]
LOGGER = "jarvis.state.day_line"
EVENT_ID = re.compile(r"event-[0-9a-f]{12}")


def _at(minutes: float) -> int:
    return ORIGIN + round(minutes * MINUTE)


# --- Graph's answers and the server that gives them ----------------------------------------------


def _utc(minutes: float) -> str:
    """Graph's calendarView answers in UTC: the instant, as ``dateTime`` holds it."""
    moment = datetime.fromtimestamp(_at(minutes) / 1000, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000")


def timed(
    graph_id: str, subject: str, start: float, end: float, *, cancelled: bool = False,
) -> dict[str, Any]:
    """One timed event, as Graph words it."""
    return {
        "id": graph_id, "subject": subject, "isAllDay": False, "isCancelled": cancelled,
        "start": {"dateTime": _utc(start), "timeZone": "UTC"},
        "end": {"dateTime": _utc(end), "timeZone": "UTC"},
    }


def all_day(graph_id: str, subject: str, first: str, after: str) -> dict[str, Any]:
    """One all-day event: Graph holds the days as UTC midnights, ``after`` being the day it ends."""
    return {
        "id": graph_id, "subject": subject, "isAllDay": True, "isCancelled": False,
        "start": {"dateTime": f"{first}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{after}T00:00:00.0000000", "timeZone": "UTC"},
    }


STANDUP = timed(
    "AAMkAGI2NGVhZTU5LTRjNzItNDc2Zi04ODQ0LTQ2NWI0MDUxMTJhNgBGAAAAAAAMcHdGsrb4TLuREfq0WO0DBwDPLa7s"
    "r-7ZTYmd9vXC40UEAAAAAAENAADPLa7sr-7ZTYmd9vXC40UEAAAFf4avAAA=", "Standup", 540, 570,
)
DENTIST = timed(
    "AAMkAGI2NGVhZTU5LTRjNzItNDc2Zi04ODQ0LTQ2NWI0MDUxMTJhNgBGAAAAAAAMcHdGsrb4TLuREfq0WO0DBwDPLa7s"
    "r-7ZTYmd9vXC40UEAAAAAAENAADPLa7sr-7ZTYmd9vXC40UEAAAFf4avAAB=", "Dentist", 840, 900,
)
CALLED_OFF = timed("AAMk-called-off", "Called off", 600, 630, cancelled=True)
READING_WEEK = all_day("AAMk-reading-week", "Reading week", "2026-10-10", "2026-10-11")


class _Microsoft:
    """The connected ``microsoft`` server: Graph's JSON as a text block, or a failure."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.text: str | None = None  # an answer that is not Graph's JSON
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, dict(args)))
        if self.error is not None:
            raise self.error
        return {"text": self.text if self.text is not None else json.dumps({"value": self.events})}


class _Connections:
    def __init__(self, server: _Microsoft | None) -> None:
        self.server = server

    def client_for(self, server: str) -> _Microsoft:
        if self.server is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return self.server


def _home(server: _Microsoft | None) -> Home:
    return Home(cast("Any", _Connections(server)), (VANCOUVER, ZONE), None)


# --- the fold -------------------------------------------------------------------------------------


class Rig:
    """A real event log holding the rows, and the stores of a daemon, folded at a chosen time."""

    def __init__(
        self, tmp_path: Path, rows: list[Row] | None = None, old: tuple[Row, ...] = (),
    ) -> None:
        """Log ``rows`` (times in minutes) in ``tmp_path``, beside a TimeSink and a memory.db.

        ``old`` are rows the registry would refuse now, as an older or damaged log holds them.
        """
        self.conn: sqlite3.Connection = open_event_log(tmp_path / "events.db")
        _load(self.conn, rows or [], ORIGIN)
        for row in old:
            _raw(self.conn, row.uid, row.type, json.dumps(row.payload), _at(row.ts))
        self.stores = _stores(tmp_path, ZONE)

    def close(self) -> None:
        """Close the log."""
        self.conn.close()

    def fold(
        self, now: float, *,
        calendar: Callable[[int, int, int], list[dict[str, Any]]] | None = None, day: str = DAY,
    ) -> DayLine:
        """The day as of ``now`` minutes after the midnight that opens 2026-10-10."""
        start_ms, end_ms = day_window(parse_day(day), ZONE)
        sources = dataclasses.replace(self.stores, calendar=calendar)
        return fold_day(self.conn, start_ms, end_ms, _at(now), sources)


def _events(line: DayLine) -> list[dict[str, Any]]:
    return [item for item in line.items if item["kind"] == "event"]


# --- the calendar ---------------------------------------------------------------------------------

NOW_CASES: list[tuple[float, dict[str, str]]] = [
    (480, {"Standup": "next", "Dentist": "next"}),
    (539, {"Standup": "next", "Dentist": "next"}),
    (540, {"Standup": "now", "Dentist": "next"}),
    (569, {"Standup": "now", "Dentist": "next"}),
    (570, {"Standup": "done", "Dentist": "next"}),
    (840, {"Standup": "done", "Dentist": "now"}),
    (900, {"Standup": "done", "Dentist": "done"}),
]


@pytest.mark.parametrize(("now", "states"), NOW_CASES, ids=[f"minute {n}" for n, _ in NOW_CASES])
def test_events_are_next_until_they_start_and_past_when_the_clock_passes(
    tmp_path: Path, now: float, states: dict[str, str],
) -> None:
    """Graph's events become items in his lane; the cancelled one and the all-day one are not."""
    server = _Microsoft([STANDUP, CALLED_OFF, READING_WEEK, DENTIST])
    with closing(Rig(tmp_path)) as rig:
        line = rig.fold(now, calendar=_home(server).calendar)

    assert line.missing == []
    assert [item["title"] for item in line.items] == ["Standup", "Dentist"]
    assert {item["title"]: item["state"] for item in line.items} == states
    standup = line.items[0]
    assert EVENT_ID.fullmatch(standup.pop("id"))
    assert standup == {
        "lane": "you", "kind": "event", "start_ms": _at(540), "end_ms": _at(570),
        "state": states["Standup"], "title": "Standup",
    }


def test_an_events_id_is_a_short_hash_of_graphs_that_stays_the_same(tmp_path: Path) -> None:
    """Graph's id is a long opaque token; the item's is not it, and is the same each request."""
    server = _Microsoft([STANDUP, DENTIST])
    with closing(Rig(tmp_path)) as rig:
        first = rig.fold(480, calendar=_home(server).calendar)
        later = rig.fold(700, calendar=_home(_Microsoft([DENTIST, STANDUP])).calendar)

    ids = {item["title"]: item["id"] for item in first.items}
    assert {item["title"]: item["id"] for item in later.items} == ids
    assert len(set(ids.values())) == 2
    for title, graph in (("Standup", STANDUP), ("Dentist", DENTIST)):
        digest = hashlib.sha256(graph["id"].encode()).hexdigest()
        assert ids[title] == f"event-{digest[:12]}"
        assert graph["id"] not in ids[title]


def test_the_calendar_is_only_read_for_the_window_of_the_day(tmp_path: Path) -> None:
    """One read-only call, asking for the local midnights that bound the day."""
    server = _Microsoft([STANDUP])
    with closing(Rig(tmp_path)) as rig:
        rig.fold(480, calendar=_home(server).calendar)

    assert len(server.calls) == 1
    tool, args = server.calls[0]
    assert tool == "get-calendar-view"
    assert (args["startDateTime"], args["endDateTime"]) == (
        "2026-10-10T00:00:00-07:00", "2026-10-11T00:00:00-07:00",
    )
    assert args["fetchAllPages"] is True


ALL_DAY_CASES: list[tuple[str, list[dict[str, Any]], list[str]]] = [
    ("one day", [all_day("a", "Reading week", "2026-10-10", "2026-10-11")], ["Reading week"]),
    (
        "the day before is not a label of this day",
        [all_day("a", "Yesterday", "2026-10-09", "2026-10-10")], [],
    ),
    (
        "the day after is not either, though Graph returns it for this window",
        [all_day("a", "Tomorrow", "2026-10-11", "2026-10-12")], [],
    ),
    (
        "a stretch of days is a label on each day it covers",
        [all_day("a", "Conference", "2026-10-08", "2026-10-13")], ["Conference"],
    ),
    (
        "they are listed in the order Graph gave them",
        [
            all_day("b", "Birthday", "2026-10-10", "2026-10-11"),
            all_day("a", "Reading week", "2026-10-10", "2026-10-11"),
        ],
        ["Birthday", "Reading week"],
    ),
]


@pytest.mark.parametrize(
    ("graph", "labels"), [case[1:] for case in ALL_DAY_CASES],
    ids=[case[0] for case in ALL_DAY_CASES],
)
def test_an_all_day_event_is_a_label_of_the_day_and_not_an_item(
    tmp_path: Path, graph: list[dict[str, Any]], labels: list[str],
) -> None:
    """Only the days an all-day event covers carry it, as ``{title, source}`` in Graph's order."""
    server = _Microsoft([*graph, STANDUP])
    with closing(Rig(tmp_path)) as rig:
        line = rig.fold(480, calendar=_home(server).calendar)

    assert line.labels == [{"title": title, "source": "calendar"} for title in labels]
    assert [item["kind"] for item in line.items] == ["event"]
    assert line.missing == []


# --- a calendar that cannot be read ---------------------------------------------------------------


def _error(error: Exception) -> Callable[[_Microsoft], None]:
    """Make the server fail with ``error``."""

    def apply(server: _Microsoft) -> None:
        server.error = error

    return apply


def _garbled(server: _Microsoft) -> None:
    server.text = "<html>502 Bad Gateway</html>"


def _no_start(server: _Microsoft) -> None:
    server.events = [{"id": "x", "subject": "Broken", "isAllDay": False, "end": STANDUP["end"]}]


FAILURES: list[tuple[str, Callable[[_Microsoft], None]]] = [
    (
        "the server's tool error",
        _error(ToolError("microsoft: TimeoutError: ", code="mcp_server")),
    ),
    ("a timeout", _error(TimeoutError("graph"))),
    ("an answer that is not Graph's", _garbled),
    ("an event with no start", _no_start),
]


@pytest.mark.parametrize(
    "break_it", [case[1] for case in FAILURES], ids=[case[0] for case in FAILURES],
)
def test_a_calendar_that_cannot_be_read_is_missing_and_adds_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, break_it: Callable[[_Microsoft], None],
) -> None:
    """The failure is named and logged; the stay and the reminder of the day are still served."""
    server = _Microsoft([STANDUP, READING_WEEK])
    break_it(server)
    rows = [visit("home", 480, place="Home"), *remind("r1", 800, "water the plants")]
    caplog.set_level(logging.ERROR, logger=LOGGER)
    with closing(Rig(tmp_path, rows)) as rig:
        line = rig.fold(720, calendar=_home(server).calendar)

    assert line.missing == ["calendar"]
    assert line.labels == []
    assert [item["id"] for item in line.items] == ["home", "r1"]
    assert any("calendar" in record.getMessage() for record in caplog.records)


def test_a_day_with_no_microsoft_connection_serves_everything_else(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Not connected is a state, not a fault: ``calendar`` is missing and nothing is logged."""
    rows = [visit("home", 480, place="Home"), *remind("r1", 800, "water the plants")]
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with closing(Rig(tmp_path, rows)) as rig:
        line = rig.fold(720, calendar=_home(None).calendar)

    assert line.missing == ["calendar"]
    assert [item["id"] for item in line.items] == ["home", "r1"]
    assert [r for r in caplog.records if r.name == LOGGER] == []


def test_a_day_with_no_calendar_reader_says_nothing_of_it(tmp_path: Path) -> None:
    """A fold that was given none has nothing to read: not an error, not in ``missing``."""
    with closing(Rig(tmp_path, [visit("home", 480)])) as rig:
        line = rig.fold(720)

    assert (line.missing, line.labels, [item["id"] for item in line.items]) == ([], [], ["home"])


# --- the cache ------------------------------------------------------------------------------------


def _moved(server: _Microsoft) -> None:
    """The standup moves ten minutes later in Outlook."""
    server.events = [timed(STANDUP["id"], "Standup", 550, 580)]


def test_todays_read_is_kept_for_five_minutes(tmp_path: Path) -> None:
    """A second request within the time does not ask Graph; one after it does and sees the move."""
    server = _Microsoft([STANDUP])
    calendar = _home(server).calendar
    with closing(Rig(tmp_path)) as rig:
        cold = rig.fold(720, calendar=calendar)
        _moved(server)
        warm = rig.fold(724, calendar=calendar)
        assert len(server.calls) == 1
        assert warm.items == cold.items
        stale = rig.fold(726, calendar=calendar)

    assert len(server.calls) == 2
    assert [(item["start_ms"], item["end_ms"]) for item in _events(stale)] == [
        (_at(550), _at(580)),
    ]


def test_another_days_read_is_kept_for_thirty_minutes_and_each_day_has_its_own(
    tmp_path: Path,
) -> None:
    """Days other than today move slowly; two days are two reads."""
    server = _Microsoft([STANDUP])
    calendar = _home(server).calendar
    now = 3000  # the morning of the 12th: the 10th is past, the 11th is gone
    with closing(Rig(tmp_path)) as rig:
        rig.fold(now, calendar=calendar)
        _moved(server)
        rig.fold(now + 29, calendar=calendar)
        assert len(server.calls) == 1
        rig.fold(now + 29, calendar=calendar, day="2026-10-11")
        assert len(server.calls) == 2
        rig.fold(now + 31, calendar=calendar)

    assert len(server.calls) == 3


def test_a_failed_read_is_not_kept(tmp_path: Path) -> None:
    """The next request asks again and gets the day once Graph answers."""
    server = _Microsoft([STANDUP])
    server.error = ToolError("microsoft: TimeoutError: ", code="mcp_server")
    calendar = _home(server).calendar
    with closing(Rig(tmp_path)) as rig:
        down = rig.fold(720, calendar=calendar)
        server.error = None
        up = rig.fold(721, calendar=calendar)

    assert (down.missing, _events(down)) == (["calendar"], [])
    assert (up.missing, [item["title"] for item in _events(up)]) == ([], ["Standup"])
    assert len(server.calls) == 2


# --- the pinned departures ------------------------------------------------------------------------

STOP = "Shelbourne at Pear"


def trip(tid: str, departs: float, leave: float, route: str = "28") -> dict[str, Any]:
    """A pinned trip's fields, as ``departure.pinned`` holds them."""
    return dataclasses.asdict(departures.Trip(
        tid=tid, route=route, board_stop=STOP, leave_at_ms=_at(leave), departs_at_ms=_at(departs),
        arrive_at="09:15", stop_lat=48.46, stop_lng=-123.31, to="Home stop",
    ))


def pinned(pin: str, trips: list[dict[str, Any]], reminder: str, ring: str, ts: float) -> Row:
    """A ``departure.pinned`` row: the set, and the reminder that rings for ``ring``."""
    payload = {
        "pin_id": pin, "reminder_id": reminder, "ring": ring, "trips": trips, "offer_id": None,
        "action_id": "A1",
    }
    return Row("departure.pinned", f"{pin}-pinned", payload, ts)


def rang_for(pin: str, one: dict[str, Any], reminder: str, ts: float) -> Row:
    """A ``departure.updated`` row: the ring now for trip ``one`` with a new reminder."""
    payload = {
        "pin_id": pin, "tid": one["tid"], "reminder_id": reminder, "ring": one["tid"],
        "leave_at_ms": one["leave_at_ms"], "departs_at_ms": one["departs_at_ms"],
        "arrive_at": "09:15",
    }
    return Row("departure.updated", f"{pin}-{reminder}", payload, ts)


def dropped(pin: str, tid: str, reminder: str, ring: str, ts: float) -> Row:
    """A ``departure.trip_removed`` row."""
    payload = {"pin_id": pin, "tid": tid, "reminder_id": reminder, "ring": ring}
    return Row("departure.trip_removed", f"{pin}-no-{tid}", payload, ts)


def unpinned(pin: str, ts: float) -> Row:
    """A ``departure.unpinned`` row."""
    return Row("departure.unpinned", f"{pin}-unpinned", {"pin_id": pin}, ts)


def cancelled(rid: str, ts: float) -> Row:
    """A ``reminder.cancelled`` row."""
    payload = {"reminder_id": rid, "action_id": "A1"}
    return Row("reminder.cancelled", f"{rid}-cancel", payload, ts)


class Plan(NamedTuple):
    """Log rows, a day and a time, and the ``(kind, id, state)`` of what the day then holds."""

    name: str
    rows: list[Row]
    now: float
    expected: list[tuple[str, str, str]]
    day: str = DAY
    old: tuple[Row, ...] = ()


PLANS: list[Plan] = [
    Plan(
        "a pinned trip is one departure and the pin's ring is not a second item",
        [
            pinned("P1", [trip("a", 520, 515)], "r1", "a", 0),
            *remind("r1", 515, "go"), *remind("r9", 600, "water the plants"),
        ],
        500,
        [("departure", "departure-a", "next"), ("reminder", "r9", "next")],
    ),
    Plan(
        "once the bus has left it is done, and its ring that rang is still not a second item",
        [
            pinned("P1", [trip("a", 520, 515)], "r1", "a", 0),
            *remind("r1", 515, "go"), fired("r1", 515), *remind("r9", 600, "water the plants"),
        ],
        530,
        [("departure", "departure-a", "done"), ("reminder", "r9", "next")],
    ),
    Plan(
        "a ring moved on to the next trip: both of its reminders are the departures'",
        [
            pinned("P1", [trip("a", 520, 515), trip("b", 530, 525)], "r1", "a", 0),
            *remind("r1", 515, "go"), fired("r1", 515),
            *remind("r2", 525, "go"), rang_for("P1", trip("b", 530, 525), "r2", 520),
        ],
        522,
        [("departure", "departure-a", "done"), ("departure", "departure-b", "next")],
    ),
    Plan(
        "a reminder whose trip has left the pin stays on the line",
        [
            pinned("P1", [trip("a", 520, 515), trip("b", 530, 525)], "r1", "a", 0),
            *remind("r1", 515, "go"), fired("r1", 515),
            *remind("r2", 525, "go"), dropped("P1", "a", "r2", "b", 516),
        ],
        518,
        [("reminder", "r1", "done"), ("departure", "departure-b", "next")],
    ),
    Plan(
        "a pin that was unpinned has no departure and its cancelled ring is not there either",
        [
            pinned("P1", [trip("a", 520, 515)], "r1", "a", 0),
            *remind("r1", 515, "go"), cancelled("r1", 510), unpinned("P1", 510),
        ],
        500,
        [],
    ),
    Plan(
        "a pin written before ADR 0204, its one trip's fields at the top, is a set of one",
        remind("r1", 515, "go"),
        500,
        [("departure", "departure-t0", "next")],
        old=(Row("departure.pinned", "old-pin", {
            "pin_id": "P0", "reminder_id": "r1", "route": "28", "board_stop": STOP,
            "leave_at_ms": _at(515), "departs_at_ms": _at(520), "arrive_at": "09:15",
            "stop_lat": 48.46, "stop_lng": -123.31, "to": "Home stop",
        }, 0),),
    ),
    Plan(
        "a trip that leaves tomorrow is not on today's line",
        [pinned("P1", [trip("a", 1500, 1495)], "r1", "a", 0), *remind("r1", 1495, "go")],
        500,
        [],
    ),
    Plan(
        "and is on tomorrow's, as the next thing, with its ring folded into it",
        [pinned("P1", [trip("a", 1500, 1495)], "r1", "a", 0), *remind("r1", 1495, "go")],
        500,
        [("departure", "departure-a", "next")],
        "2026-10-11",
    ),
    Plan(
        "a past day shows the pin held at its end, not today's: the 9th",
        [
            pinned("P1", [trip("a", -900, -905), trip("b", -890, -895)], "r1", "a", -1000),
            pinned("P2", [trip("c", 520, 515)], "r2", "c", 500),
        ],
        1500,
        [("departure", "departure-a", "done"), ("departure", "departure-b", "done")],
        "2026-10-09",
    ),
    Plan(
        "a past day shows the pin held at its end, not today's: the 10th",
        [
            pinned("P1", [trip("a", -900, -905), trip("b", -890, -895)], "r1", "a", -1000),
            pinned("P2", [trip("c", 520, 515)], "r2", "c", 500),
        ],
        1500,
        [("departure", "departure-c", "done")],
    ),
    Plan(
        "a pin dropped the day after still shows on the day it was held",
        [pinned("P1", [trip("a", -900, -905)], "r1", "a", -1000), unpinned("P1", 500)],
        1500,
        [("departure", "departure-a", "done")],
        "2026-10-09",
    ),
    Plan(
        "and is gone from the day it was dropped",
        [pinned("P1", [trip("a", -900, -905)], "r1", "a", -1000), unpinned("P1", 500)],
        1500,
        [],
    ),
]


@pytest.mark.parametrize("plan", PLANS, ids=[plan.name for plan in PLANS])
def test_pinned_departures_are_on_the_line_once(tmp_path: Path, plan: Plan) -> None:
    """Each case: the log folded at its own day and time gives exactly these items."""
    with closing(Rig(tmp_path, plan.rows, plan.old)) as rig:
        line = rig.fold(plan.now, day=plan.day)

    assert [(item["kind"], item["id"], item["state"]) for item in line.items] == plan.expected
    assert line.missing == []
    assert all("_rings" not in item for item in line.items)


def test_a_departure_item_names_the_trip_and_leaves_in_his_lane(tmp_path: Path) -> None:
    """A point at the bus's departure, titled by its route and stop, with the time to leave."""
    rows = [pinned("P1", [trip("a", 520, 515)], "r1", "a", 0), *remind("r1", 515, "go")]
    with closing(Rig(tmp_path, rows)) as rig:
        line = rig.fold(500)

    assert line.items == [{
        "id": "departure-a", "lane": "you", "kind": "departure",
        "start_ms": _at(520), "end_ms": _at(520), "state": "next",
        "title": f"28 · {STOP}", "route": "28", "board_stop": STOP, "to": "Home stop",
        "leave_at_ms": _at(515),
    }]


def test_a_pin_that_cannot_be_folded_is_missing_and_its_reminder_stays(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """With no departure to stand for it, the pin's reminder is an ordinary one."""
    bad = Row("departure.pinned", "bad-pin", {"pin_id": "P1", "reminder_id": "r1"}, 0)
    caplog.set_level(logging.ERROR, logger=LOGGER)
    with closing(Rig(tmp_path, [*remind("r1", 515, "go"), visit("home", 480)], (bad,))) as rig:
        line = rig.fold(500)

    assert line.missing == ["departures"]
    assert [item["id"] for item in line.items] == ["home", "r1"]
    assert any("departures" in record.getMessage() for record in caplog.records)


# --- the route ------------------------------------------------------------------------------------


def test_the_day_route_serves_events_labels_and_departures(tmp_path: Path) -> None:
    """``GET /inherent/day``: the items, the day's labels, and the sources that were not there."""
    log = tmp_path / "events.db"
    conn = open_event_log(log)
    pin = [pinned("P1", [trip("a", 520, 515)], "r1", "a", 0), *remind("r1", 515, "go")]
    _load(conn, pin, ORIGIN)
    conn.close()
    home = _home(_Microsoft([STANDUP, READING_WEEK, DENTIST]))
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        day_read=functools.partial(_serve_day, log, ZONE, None, None, calendar=home.calendar),
    )))

    reply = client.get("/inherent/day", params={"date": DAY})

    assert reply.status_code == 200
    body = reply.json()
    assert [(item["kind"], item.get("title")) for item in body["items"]] == [
        ("departure", f"28 · {STOP}"), ("event", "Standup"), ("event", "Dentist"),
    ]
    assert body["labels"] == [{"title": "Reading week", "source": "calendar"}]
    assert body["missing"] == ["mac", "talks"]


def test_the_day_route_without_microsoft_names_the_calendar_and_serves_the_rest(
    tmp_path: Path,
) -> None:
    """Not connected: ``calendar`` is missing, no labels, and the day is otherwise served."""
    log = tmp_path / "events.db"
    conn = open_event_log(log)
    _load(conn, [visit("home", 480, place="Home")], ORIGIN)
    conn.close()
    body = _read_day(
        log, ZONE, None, None, date(2026, 10, 10), _at(720), calendar=_home(None).calendar,
    )

    assert [item["id"] for item in body["items"]] == ["home"]
    assert (body["labels"], body["missing"]) == ([], ["mac", "talks", "calendar"])
