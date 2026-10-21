"""ADR 0230, 0299: the day line draws his Outlook events once, as things, and his pinned departures.

Acceptance checks, each against the real fold and a real event log:

- his Outlook events reach the day line only through the mirror: the real ``ThingsMirror`` reads a
  fake ``microsoft`` server that answers in Graph's calendarView shape (no network) and writes the
  ledger of his things, and the fold draws each event once, from the ledger, as an item in his lane
  with the phone's id ``commitment-<hex>@<day>`` and ``source`` ``outlook``: ``next`` until it
  starts, ``now`` while it runs and ``done`` once the clock passes its end; a cancelled event is
  not there, and one that Outlook drops later leaves the line;
- an all-day event is a label of the day, not an item, with the same fields as a thing's label;
- the fold has no reader of the calendar: a day whose Outlook was never mirrored (not connected, or
  a read that failed) is served as it is, with nothing in ``missing``;
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
from jarvis.runtime.daemon.routes import read_day, serve_day
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.state import departures, things
from jarvis.state.day_line import DayLine, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.support.day_line import (
    MINUTE,
    Row,
    fired,
    load_rows,
    make_stores,
    raw_row,
    remind,
    visit,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

VANCOUVER = "America/Vancouver"
ZONE = ZoneInfo(VANCOUVER)
DAY = "2026-10-10"
ORIGIN = day_window(parse_day(DAY), ZONE)[0]
LOGGER = "jarvis.state.day_line"
PHONE_ID = re.compile(r"commitment-[0-9a-f]{32}@2026-10-10")


def _at(minutes: float) -> int:
    return ORIGIN + round(minutes * MINUTE)


# --- Graph's answers, the server that gives them and the mirror that reads them -------------------


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

    def call(self, server: str, tool: str, _args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        answers = {
            "get-calendar-view": self.events, "list-todo-task-lists": [], "list-todo-tasks": [],
        }
        assert tool in answers, f"the mirror may only read: {tool}"
        if self.error is not None:
            raise self.error
        return {"text": json.dumps({"value": answers[tool]})}


class _Connections:
    def __init__(self, server: _Microsoft | None) -> None:
        self.server = server

    def client_for(self, server: str) -> _Microsoft:
        if self.server is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return self.server


def _mirror(log: Path, server: _Microsoft | None) -> ThingsMirror:
    """The real mirror over the log, at the local midnight that opens 2026-10-10."""
    mirror = ThingsMirror(cast("Any", _Connections(server)), log, ZONE)
    mirror.now = lambda: datetime.fromtimestamp(ORIGIN / 1000, UTC)
    return mirror


def _phone_id(log: sqlite3.Connection, graph: dict[str, Any], day: str = DAY) -> str:
    """The id the phone sees for the thing mirrored from ``graph`` on ``day``."""
    one = things.by_source(log, "outlook", graph["id"])
    assert one is not None
    return f"commitment-{one.thing_id.removeprefix(things.ID_PREFIX)}@{day}"


# --- the fold -------------------------------------------------------------------------------------


class Rig:
    """A real event log holding the rows, and the stores of a daemon, folded at a chosen time."""

    def __init__(
        self, tmp_path: Path, rows: list[Row] | None = None, old: tuple[Row, ...] = (),
    ) -> None:
        """Log ``rows`` (times in minutes) in ``tmp_path``, beside a TimeSink and a memory.db.

        ``old`` are rows the registry would refuse now, as an older or damaged log holds them.
        """
        self.path = tmp_path / "events.db"
        self.conn: sqlite3.Connection = open_event_log(self.path)
        load_rows(self.conn, rows or [], ORIGIN)
        for row in old:
            raw_row(self.conn, row.uid, row.type, json.dumps(row.payload), _at(row.ts))
        self.stores = make_stores(tmp_path, ZONE)

    def close(self) -> None:
        """Close the log."""
        self.conn.close()

    def mirror(self, server: _Microsoft | None) -> int:
        """One pass of the mirror over Outlook as ``server`` answers; the writes it made."""
        return _mirror(self.path, server).sync()

    def fold(self, now: float, *, day: str = DAY) -> DayLine:
        """The day as of ``now`` minutes after the midnight that opens 2026-10-10."""
        start_ms, end_ms = day_window(parse_day(day), ZONE)
        return fold_day(self.conn, start_ms, end_ms, _at(now), self.stores)


# --- his Outlook events, through the mirror -------------------------------------------------------

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
    """Graph's events become things, each drawn once; the cancelled one and the all-day one not."""
    server = _Microsoft([STANDUP, CALLED_OFF, READING_WEEK, DENTIST])
    with closing(Rig(tmp_path)) as rig:
        rig.mirror(server)
        line = rig.fold(now)
        standup_id = _phone_id(rig.conn, STANDUP)

    assert line.missing == []
    assert [item["title"] for item in line.items] == ["Standup", "Dentist"]
    assert {item["title"]: item["state"] for item in line.items} == states
    assert line.items[0] == {
        "id": standup_id, "lane": "you", "kind": "event", "start_ms": _at(540),
        "end_ms": _at(570), "state": states["Standup"], "title": "Standup", "source": "outlook",
        "confirmed": True, "private": False, "place": "",
    }


def test_an_events_id_is_the_things_and_stays_the_same(tmp_path: Path) -> None:
    """The phone's id for the thing, not Graph's long opaque one, the same on every request."""
    with closing(Rig(tmp_path)) as rig:
        assert rig.mirror(_Microsoft([STANDUP, DENTIST])) == 2
        first = rig.fold(480)
        assert rig.mirror(_Microsoft([DENTIST, STANDUP])) == 0  # nothing to follow: not twice
        later = rig.fold(700)
        expected = {
            "Standup": _phone_id(rig.conn, STANDUP), "Dentist": _phone_id(rig.conn, DENTIST),
        }

    ids = {item["title"]: item["id"] for item in first.items}
    assert ids == expected
    assert {item["title"]: item["id"] for item in later.items} == ids
    for title, graph in (("Standup", STANDUP), ("Dentist", DENTIST)):
        assert PHONE_ID.fullmatch(ids[title])
        assert graph["id"] not in ids[title]


def test_an_event_moved_in_outlook_is_still_one_item_and_one_dropped_is_gone(
    tmp_path: Path,
) -> None:
    """The mirror follows Outlook; the line draws the ledger, so it never draws a stale copy."""
    with closing(Rig(tmp_path)) as rig:
        rig.mirror(_Microsoft([STANDUP, DENTIST]))
        standup_id = _phone_id(rig.conn, STANDUP)
        rig.mirror(_Microsoft([timed(STANDUP["id"], "Standup", 550, 580)]))  # Dentist is deleted
        line = rig.fold(720)

    assert [(i["id"], i["title"], i["start_ms"], i["end_ms"]) for i in line.items] == [
        (standup_id, "Standup", _at(550), _at(580)),
    ]


ALL_DAY_CASES: list[tuple[str, list[dict[str, Any]], list[str]]] = [
    ("one day", [all_day("a", "Reading week", "2026-10-10", "2026-10-11")], ["Reading week"]),
    (
        "the day before is not a label of this day",
        [all_day("a", "Yesterday", "2026-10-09", "2026-10-10")], [],
    ),
    (
        "the day after is not either",
        [all_day("a", "Tomorrow", "2026-10-11", "2026-10-12")], [],
    ),
    (
        "two on one day are two labels",
        [
            all_day("b", "Birthday", "2026-10-10", "2026-10-11"),
            all_day("a", "Reading week", "2026-10-10", "2026-10-11"),
        ],
        ["Birthday", "Reading week"],
    ),
    (
        "one over several days is a label of each of them",
        [all_day("a", "Conference", "2026-10-09", "2026-10-12")], ["Conference"],
    ),
    (
        "one over several days that ended the day before is not",
        [all_day("a", "Trip", "2026-10-07", "2026-10-10")], [],
    ),
]


@pytest.mark.parametrize(
    ("graph", "labels"), [case[1:] for case in ALL_DAY_CASES],
    ids=[case[0] for case in ALL_DAY_CASES],
)
def test_an_all_day_event_is_a_label_of_the_day_and_not_an_item(
    tmp_path: Path, graph: list[dict[str, Any]], labels: list[str],
) -> None:
    """Only its own days carry it, with a thing's label fields and the phone's id (first day)."""
    with closing(Rig(tmp_path)) as rig:
        rig.mirror(_Microsoft([*graph, STANDUP]))
        line = rig.fold(480)
        held = {
            one["subject"]: _phone_id(rig.conn, one, one["start"]["dateTime"][:10])
            for one in graph
        }

    assert sorted(line.labels, key=lambda label: label["title"]) == [
        {
            "title": title, "source": "outlook", "confirmed": True, "private": False,
            "place": "", "id": held[title],
        }
        for title in labels
    ]
    assert [item["title"] for item in line.items] == ["Standup"]
    assert line.missing == []


# --- an Outlook that was not read -----------------------------------------------------------------


@pytest.mark.parametrize("connected", [False, True], ids=["not connected", "a read that fails"])
def test_a_day_whose_outlook_was_not_mirrored_serves_everything_else(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, *, connected: bool,
) -> None:
    """The line has no calendar to read: it adds nothing and names nothing in ``missing``."""
    server = _Microsoft([STANDUP]) if connected else None
    if server is not None:
        server.error = ToolError("microsoft: TimeoutError: ", code="mcp_server")
    rows = [visit("home", 480, place="Home"), *remind("r1", 800, "water the plants")]
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with closing(Rig(tmp_path, rows)) as rig:
        assert rig.mirror(server) == 0
        line = rig.fold(720)

    assert line.missing == []
    assert line.labels == []
    assert [item["id"] for item in line.items] == ["home", "r1"]
    assert [r for r in caplog.records if r.name == LOGGER] == []


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
    load_rows(conn, pin, ORIGIN)
    _mirror(log, _Microsoft([STANDUP, READING_WEEK, DENTIST])).sync()
    reading_week = _phone_id(conn, READING_WEEK)
    conn.close()
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        day_read=functools.partial(serve_day, log, ZONE, None, None),
    )))

    reply = client.get("/inherent/day", params={"date": DAY})

    assert reply.status_code == 200
    body = reply.json()
    assert [(item["kind"], item.get("title")) for item in body["items"]] == [
        ("departure", f"28 · {STOP}"), ("event", "Standup"), ("event", "Dentist"),
    ]
    assert all(PHONE_ID.fullmatch(item["id"]) for item in body["items"] if item["kind"] == "event")
    assert body["labels"] == [{
        "title": "Reading week", "source": "outlook", "confirmed": True, "private": False,
        "place": "", "id": reading_week,
    }]
    assert body["missing"] == ["mac", "talks"]


def test_the_day_route_without_microsoft_serves_the_rest_and_never_names_the_calendar(
    tmp_path: Path,
) -> None:
    """Outlook never mirrored: no labels, nothing of the calendar in ``missing``."""
    log = tmp_path / "events.db"
    conn = open_event_log(log)
    load_rows(conn, [visit("home", 480, place="Home")], ORIGIN)
    conn.close()
    body = read_day(log, ZONE, None, None, date(2026, 10, 10), _at(720))

    assert [item["id"] for item in body["items"]] == ["home"]
    assert (body["labels"], body["missing"]) == ([], ["mac", "talks"])
