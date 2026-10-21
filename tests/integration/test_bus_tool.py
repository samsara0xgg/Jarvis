"""ADR 0246: his bus is one tool; the server remembers the list and the ride.

A scripted story against the real registry: Google Routes served by hand (no network), a ride
follower that is a recorder with canned answers, a real event log for the pin and its reminder.
Each row of ``STORY`` is a call and what must come back; the rows run in order, so the list a plan
made is what the next row pins and starts. The follower's own behaviour (feeds, ending rules, the
Live Activity) is `test_bus_ride`, which calls the same tool over the real follower. The board
(ADR 0344) reads a real ``BusLive`` over a small synthetic export, and every call's receipt
(ADR 0319) is read from its ``action.result_observed`` row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.bus_tool import RideControl
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.execution.transit_tool import TransitOffers
from jarvis.runtime.bus_live import BusLive
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state import departures
from jarvis.state.event_log import iter_events_of_types, open_event_log
from tests.support import gtfs
from tests.support.bus_world import realtime
from tests.support.transit import (
    FAKE_KEY,
    PLACES,
    bus_route,
    fix_with,
    local_ms,
    routes_with,
    serve_routes,
    transit_clock,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from jarvis.shared.here import HereReading

    Where = Callable[[], tuple[float, float] | None]

ROUTES = routes_with(bus_route("26", "17:12"), bus_route("28", "17:20"))
"""Now is 17:00: the 26 leaves at 17:12 (walk 4: leave 17:08) and the 28 at 17:20 (leave 17:16)."""


RIDE_ID = "ride-3fa91c02"
"""The follower's ride; she sees it as ``ride:3fa91c02``."""


@dataclass
class Follower:
    """A ride follower that records its calls and answers like the runtime's, from one script.

    Each change it makes answers with the event that made it (``ev1``, ``ev2`` ...), as
    ``jarvis.runtime.rides`` does, so a receipt's undo can be checked against it.
    """

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    route: str | None = None
    stop: str | None = None
    events: int = 0

    def _event(self) -> str:
        self.events += 1
        return f"ev{self.events}"

    def ride(self) -> dict[str, Any]:
        """The runtime's description of the ride (ids and epoch times included)."""
        return {
            "ride_id": RIDE_ID, "route": self.route, "headsign": "Downtown",
            "board_stop": "Alder St",
            "alight_stop": self.stop, "alight_known": self.stop is not None,
            "next_stop": "Cedar Ave", "stops_left": 3 if self.stop else None,
            "minutes_to_alight": 12 if self.stop else None,
            "eta_ms": local_ms(17, 12) if self.stop else None, "position_live": True,
            "arrival_live": True,
            "started_at_ms": local_ms(17, 0), "source": "said", "warned": False,
            "credit": "BC Transit",
        }

    def watch(self, place: str) -> dict[str, Any]:
        """``bus(ride="watch")``."""
        self.calls.append(("watch", (place,)))
        return {"watching": True, "minutes": 45, "watch_id": "watch-5b2e7d10",
                "event_uid": self._event(), "note": "if his phone moves with a bus ..."}

    def start(
        self, route: str | None, place: str | None, vehicle: str | None,
        _here: HereReading | None, _where: Where,
    ) -> dict[str, Any]:
        """``bus(ride="start")``: begin, or say it already has."""
        self.calls.append(("start", (route, place, vehicle)))
        if self.route is not None:
            return {"started": False, "already_riding": True, **self.ride()}
        self.route, self.stop = route, "Fir Lane" if place else None
        return {"started": True, "event_uid": self._event(), **self.ride()}

    def set_stop(self, words: str, _here: HereReading | None, _where: Where) -> dict[str, Any]:
        """``bus(ride="get_off", stop=...)``: ambiguous for "e", else set."""
        self.calls.append(("set_stop", (words,)))
        if words == "e":
            return {"set": False, "ambiguous": True, "candidates": ["Elm Cres", "Fir Lane"],
                    "note": "ask which stop he means"}
        self.stop = "Gum St"
        return {"set": True, "event_uid": self._event(), **self.ride()}

    def end(self) -> dict[str, Any]:
        """``bus(ride="end")``."""
        self.calls.append(("end", ()))
        if self.route is None:
            return {"ended": False, "note": "no ride is active"}
        gone, self.route, self.stop = self.route, None, None
        return {"ended": True, "route": gone, "headsign": "Downtown", "ride_id": RIDE_ID,
                "event_uid": self._event()}

    def status(self) -> dict[str, Any] | None:
        """``bus()``: the ride, None when there is none."""
        self.calls.append(("status", ()))
        return None if self.route is None else self.ride()

    def control(self) -> RideControl:
        """The tool's view of this follower."""
        return RideControl(self.watch, self.start, self.set_stop, self.end, self.status)


@dataclass
class World:
    """The registry the model would have, a recorded follower, and the log they write."""

    tmp_path: Path
    follower: Follower = field(default_factory=Follower)
    offers: TransitOffers = field(default_factory=TransitOffers)
    seen: list[Any] = field(default_factory=list)
    bus_live: BusLive | None = None
    n: int = 0

    def __post_init__(self) -> None:
        """Build the runtime pieces the registry dispatches over."""
        self.paths = bootstrap_runtime(self.tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.lifecycle = ActionLifecycle()
        self.registry = build_default_registry(
            transit_api_key=FAKE_KEY, transit_places=lambda: PLACES, transit_offers=self.offers,
            clock=transit_clock(), here=fix_with(), ride_control=self.follower.control(),
            bus_live=self.bus_live,
        )

    def call(self, args: dict[str, Any]) -> dict[str, Any]:
        """One model call of ``bus`` through the real dispatcher."""
        self.n += 1
        request = ActionRequest(
            action_id=f"A{self.n}", tool_name="bus", target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1", arguments=args,
            authorization_lease=None, run_id=None, turn_id=None,
        )
        self.lifecycle.register(request.action_id)
        self.lifecycle.transition(request.action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)
        out = json.loads(bundle.tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}

    def pins(self) -> list[dict[str, Any]]:
        """The ``departure.pinned`` payloads in the log."""
        return [dict(e.payload) for e in iter_events_of_types(self.conn, ("departure.pinned",))]

    def receipt(self) -> dict[str, Any]:
        """The last call's receipt, as the dispatcher logged it in ``action.result_observed``."""
        *_, last = iter_events_of_types(self.conn, ("action.result_observed",))
        return dict(last.payload["receipt"])


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Google served by hand at 17:00; the follower idle."""
    made = World(tmp_path, seen=serve_routes(monkeypatch, answer=ROUTES))
    yield made
    made.conn.close()


# (what the row shows, arguments, the summary must start with, keys the answer must have,
#  the error code instead, calls the follower must have got since the last row)
Row = tuple[str, dict[str, Any], str, tuple[str, ...], str, list[tuple[str, tuple[Any, ...]]]]
NO_RIDE: list[tuple[str, tuple[Any, ...]]] = [("status", ()), ("status", ())]
"""``bus()`` off a ride reads the ride twice: for the no-argument answer, then whether the times
asked are the ride's own (ADR 0344)."""
STORY: list[Row] = [
    (
        "no ride and no arguments: the next buses home, numbered, the sentence first",
        {},
        "To home, option 1: leave 5:08 pm (in 8 min), take the 26 from Stop of 26 at 5:12 pm"
        " (in 12 min) (scheduled), arrive 5:32 pm (24 min). Then option 2: the 28 from"
        " Stop of 28 at 5:20 pm (in 20 min) (scheduled), arrive 5:40 pm.",
        ("options", "from", "to"), "", NO_RIDE,
    ),
    (
        "pin an option that is not on the list: what happened and what to do",
        {"pin": [3]},
        "", (), "not_found", [],
    ),
    (
        "pin option 2 by number: nothing is copied from the list",
        {"pin": [2]},
        "Pinned on the notch: the 28 from Stop of 28: leave 5:16 pm (in 16 min), bus at 5:20 pm.",
        ("trips",), "", [],
    ),
    (
        "the same pin again: answered as already pinned",
        {"pin": [2]},
        "Already pinned, nothing changed: the 28 from Stop of 28:",
        ("already_pinned",), "", [],
    ),
    (
        "he boarded the bus he was told about: option 1 is route 26 to where it was planned",
        {"ride": "start", "option": 1},
        "On the 26 toward Downtown from Alder St; next stop Cedar Ave. He gets off at Fir Lane:"
        " 3 stops left, about 5:12 pm (in 12 min).",
        ("started", "route", "alight_stop"), "", [("start", ("26", "home", None))],
    ),
    (
        "no arguments on a ride: how it is going, no ids",
        {},
        "On the 26 toward Downtown from Alder St; next stop Cedar Ave. He gets off at Fir Lane:"
        " 3 stops left, about 5:12 pm (in 12 min).",
        ("next_stop", "stops_left", "credit"), "", [("status", ())],
    ),
    (
        "starting again while riding changes nothing and says so",
        {"ride": "start", "route": "26"},
        "Already following this ride; nothing changed. On the 26 toward Downtown from Alder St;",
        ("already_riding",), "", [("start", ("26", None, None))],
    ),
    (
        "his stop is named: set, and he is warned before it",
        {"ride": "get_off", "stop": "Gum"},
        "Set. On the 26 toward Downtown from Alder St; next stop Cedar Ave. He gets off at Gum"
        " St: 3 stops left, about 5:12 pm (in 12 min). The bus position and the arrival at his"
        " stop are live from BC Transit. He is warned one stop before it.",
        ("set", "alight_stop"), "", [("set_stop", ("Gum",))],
    ),
    (
        "a name that fits two stops: ask which",
        {"ride": "get_off", "stop": "e"},
        "More than one stop matches: Elm Cres, Fir Lane. Ask him which, then call",
        ("candidates",), "", [("set_stop", ("e",))],
    ),
    (
        "he got off",
        {"ride": "end"},
        "Stopped following the 26 toward Downtown.",
        ("ended",), "", [("end", ())],
    ),
    (
        "he got off again: nothing to end",
        {"ride": "end"},
        "He was not on a followed ride, so nothing was ended.",
        ("ended",), "", [("end", ())],
    ),
    (
        "heading out: a quiet watch, told in words",
        {"ride": "watch", "to": "campus"},
        "Watching for him to board a bus to campus for the next 45 minutes.",
        ("watching",), "", [("watch", ("campus",))],
    ),
    (
        "no ride again: no arguments is the buses home once more",
        {},
        "To home, option 1:",
        ("options",), "", NO_RIDE,
    ),
]


def test_the_story_runs_row_by_row(world: World) -> None:
    """Each call: the sentence first and spoken, no ids, small, and the follower asked right."""
    for label, args, summary, keys, code, calls in STORY:
        before = len(world.follower.calls)
        out = world.call(args)
        assert out.get("code", "") == code, (label, out)
        if code:
            assert "error" in out, label
            continue
        assert next(iter(out)) == "summary", label
        assert out["summary"].startswith(summary), (label, out["summary"])
        for key in keys:
            assert key in out, (label, key)
        assert not {
            "ride_id", "eta_ms", "started_at_ms", "pin_id", "offer_id", "event_uid", "watch_id",
        } & out.keys(), label
        assert "cursor" not in json.dumps(out), label
        assert len(json.dumps(out)) <= 4000, label
        assert world.follower.calls[before:] == calls, label
    assert len(world.pins()) == 1  # the repeat pinned nothing more
    assert len(world.seen) == 2 * 3  # two plans: the bus, drive, walk


@pytest.mark.parametrize(
    ("args", "code", "says"),
    [
        ({"pin": [1], "to": "home"}, "invalid_arguments", "pin stands alone"),
        ({"pin": []}, "invalid_arguments", "pin takes one to 3"),
        ({"pin": [1, 2, 3, 4]}, "invalid_arguments", "pin takes one to 3"),
        ({"pin": [0]}, "invalid_arguments", "pin must be a whole number from 1"),
        ({"stop": "Gum", "ride": "end"}, "invalid_arguments", "ride=end takes no other"),
        ({"ride": "fly"}, "invalid_arguments", "ride must be start, watch, end or get_off"),
        ({"ride": "get_off"}, "invalid_arguments", "ride=get_off takes stop="),
        ({"ride": "get_off", "to": "home"}, "invalid_arguments", "ride=get_off takes stop="),
        ({"stop": "Gum", "to": "home"}, "invalid_arguments", "so not both"),
        ({"ride": "watch"}, "invalid_arguments", "ride=watch needs to="),
        ({"ride": "start", "pin": [1]}, "invalid_arguments", "ride=start takes option="),
        ({"ride": "start", "option": 1, "route": "7"}, "invalid_arguments", "not both"),
        ({"ride": "start", "option": 1}, "no_plan", "ask for the times first with bus(to=...)"),
        ({"option": 1}, "invalid_arguments", "option goes with ride=start"),
        ({"vehicle_id": "v1"}, "invalid_arguments", "vehicle_id goes with ride=start"),
        ({"cursor": "x"}, "invalid_arguments", "cursor is not an argument"),
        ({"destination": "home"}, "invalid_arguments", "The arguments are arrive_by, at, from,"),
        # The arguments that left with ADR 0343-0346 say where they went.
        ({"get_off": "Gum"}, "invalid_arguments", 'get_off is ride="get_off", stop=<stop>'),
        ({"day": "tomorrow"}, "invalid_arguments", "day is at=, which takes a local date"),
        ({"place_id": "ChIJx"}, "invalid_arguments", "place_id is to=, which takes a place_id"),
    ],
)
def test_a_mistake_says_what_it_was_and_the_way_out(
    world: World, args: dict[str, Any], code: str, says: str,
) -> None:
    """Mixed intents, bad numbers and arguments from the old tools are refused before any work."""
    out = world.call(args)
    assert out["code"] == code
    assert says in out["error"]
    assert world.follower.calls == []
    assert world.seen == []
    assert world.pins() == []


def test_without_the_follower_the_tool_still_plans_and_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `ride_control`: times and pins work; a ride is an error that says it is not set up."""
    serve_routes(monkeypatch, answer=ROUTES)
    made = World(tmp_path)
    registry = build_default_registry(
        transit_api_key=FAKE_KEY, transit_places=lambda: PLACES, transit_offers=made.offers,
        clock=transit_clock(), here=fix_with(),
    )
    made.registry = registry
    assert made.call({"to": "home"})["options"]
    assert made.call({"pin": [1]})["pinned"] is True
    out = made.call({"ride": "end"})
    assert out["code"] == "unavailable"
    assert "not set up on this machine" in out["error"]
    made.conn.close()


def test_without_a_key_it_still_follows_rides_and_says_times_cannot_be_read(
    tmp_path: Path,
) -> None:
    """Only a follower: `bus()` off a ride is an error that names the missing key's effect."""
    made = World(tmp_path)
    made.registry = build_default_registry(ride_control=made.follower.control())
    out = made.call({})
    assert out["code"] == "unavailable"
    assert "no Google Routes key" in out["error"]
    assert made.call({"ride": "watch", "to": "campus"})["watching"] is True
    made.conn.close()


def test_with_no_stop_set_the_ride_offers_where_the_last_plan_lets_him_off(world: World) -> None:
    """'Which stop do I get off at': the plan's drop-off for this bus, to set if he takes it."""
    world.call({"to": "home"})
    world.offers.alights[("26", "Stop of 26")] = ("UVic Exchange", 48.47, -123.31)
    world.call({"ride": "start", "route": "26"})  # no destination: his stop is not set
    out = world.call({})
    assert out["summary"].startswith(
        "On the 26 toward Downtown from Alder St; next stop Cedar Ave. Where he gets",
    )
    assert out["summary"].endswith(
        "The last plan, to home, lets him off the 26 at UVic Exchange;"
        " set it with bus(ride='get_off', stop=...) if he takes that."
    )
    world.call({"ride": "get_off", "stop": "Gum"})
    assert "The last plan" not in world.call({})["summary"]


def test_a_newer_plan_renumbers_the_list_and_a_pin_takes_the_new_first(world: World) -> None:
    """Option 1 is the first of the latest list; a newer pin replaces the older."""
    world.call({"to": "home"})
    assert world.call({"pin": [1]})["pinned"] is True
    held = departures.active(world.conn, local_ms(17, 0))
    assert held is not None
    assert [t.first_route for t in held.trips] == ["26"]
    # A new list: the 28 first, then the 26. Home is a point, so no place search is needed.
    world.call({"to": "home", "route": "28"})
    assert world.call({"pin": [1]})["summary"].startswith("Pinned on the notch: the 28 from")
    held = departures.active(world.conn, local_ms(17, 0))
    assert held is not None
    assert [t.first_route for t in held.trips] == ["28"]


def test_the_menu_entry_is_short_first_sentence_says_when_and_nothing_pages(world: World) -> None:
    """Description about 600 characters: when to use it, then when not; no cursor anywhere."""
    (definition,) = (d for d in world.registry.get_definitions() if d.name == "bus")
    assert len(definition.description) <= 650
    when, not_for, *_ = definition.description.split(". ")
    assert when.startswith("Use for anything about the bus")
    assert not_for.startswith("Not for where he is")
    assert "cursor" not in json.dumps(definition.input_schema)
    assert definition.input_schema["additionalProperties"] is False


# --- receipts (ADR 0319) ---------------------------------------------------------------------


def test_each_ride_change_names_the_ride_and_the_event_an_undo_reverses(world: World) -> None:
    """A ride write is ``ride:<short id>`` (a watch ``watch:``) with the follower's event as undo.

    A repeat changes nothing, so it has no undo; a question back and a refusal touch no object.
    """
    rows: list[tuple[dict[str, Any], str, str | None, str | None]] = [
        ({"ride": "start", "route": "26", "to": "home"}, "done", "ride:3fa91c02", "ev1"),
        ({"ride": "start", "route": "26"}, "done", "ride:3fa91c02", None),  # already riding
        ({"ride": "get_off", "stop": "e"}, "needs_choice", None, None),
        ({"ride": "get_off"}, "failed", None, None),
        ({"ride": "get_off", "stop": "Gum"}, "done", "ride:3fa91c02", "ev2"),
        ({"ride": "end"}, "done", "ride:3fa91c02", "ev3"),
        ({"ride": "end"}, "done", None, None),  # nothing was ended
        ({"ride": "watch", "to": "campus"}, "done", "watch:5b2e7d10", "ev4"),
    ]
    for args, status, touched, undo in rows:
        world.call(args)
        receipt = world.receipt()
        assert (receipt["status"], receipt["reads_only"]) == (status, False), args
        assert (receipt.get("object"), receipt.get("undo")) == (touched, undo), args


def test_a_pin_names_itself_and_undoes_by_its_event_and_a_repeat_has_no_undo(
    world: World,
) -> None:
    """``pin:<short id>`` with the ``departure.pinned`` event as undo; pinning it again has none."""
    world.call({"to": "home"})
    world.call({"pin": [1]})
    first = world.receipt()
    (pinned,) = iter_events_of_types(world.conn, ("departure.pinned",))
    short = str(pinned.payload["pin_id"]).removeprefix(departures.ID_PREFIX)
    assert (first["status"], first["reads_only"]) == ("done", False)
    assert (first["object"], first["undo"]) == (f"pin:{short}", pinned.event_uid)
    world.call({"pin": [1]})
    again = world.receipt()
    assert again["object"] == f"pin:{short}"
    assert "undo" not in again


def test_the_times_and_the_ride_are_reads_that_say_what_they_covered(world: World) -> None:
    """The times name Google and BC Transit, the window and the options shown; the ride its feed."""
    world.call({"to": "home"})
    times = world.receipt()
    assert times["reads_only"] is True
    assert not {"object", "undo"} & times.keys()
    assert times["covered"] == {
        "sources": ["Google Routes", "BC Transit timetable", "BC Transit live"],
        "window": "buses from now", "shown": 2, "more": 0, "how_more": "",
    }
    world.call({"ride": "start", "route": "26"})
    world.call({})
    ride = world.receipt()
    assert ride["reads_only"] is True
    assert ride["covered"]["sources"] == ["BC Transit live", "BC Transit timetable"]
    assert (ride["covered"]["window"], ride["covered"]["shown"]) == ("the ride now", 1)


# --- the next buses at a stop (ADR 0344) -----------------------------------------------------

# Here is the Mac's fix, 48.46, -123.31. Two stops of the 7 are within 300 m of it, one is 2 km
# away; the 7 runs both ways, the 9 calls at the nearer stop only.
BOARD = gtfs.Export(
    stops=(
        gtfs.StopSpec("S1", "Ring Rd at Oak St", 48.4609, -123.31, "10001"),  # 100 m
        gtfs.StopSpec("S2", "Oak St at Elm Ave", 48.46, -123.3125),  # 184 m
        gtfs.StopSpec("S3", "Ring Rd at Pine St", 48.48, -123.31),  # 2.2 km
        gtfs.StopSpec("S4", "Alder Ave at Fern St", 48.45, -123.31),  # 1.1 km
    ),
    trips=(
        gtfs.TripSpec("T7a", "7", (("S1", "17:10:00"), ("S3", "17:20:00")), headsign="Downtown"),
        gtfs.TripSpec("T7c", "7", (("S1", "17:40:00"), ("S3", "17:50:00")), headsign="Downtown"),
        gtfs.TripSpec(
            "T7b", "7",
            (("S3", "17:05:00"), ("S2", "17:12:00"), ("S1", "17:15:00"), ("S4", "17:20:00")),
            direction=1, headsign="Harbour",
        ),
        gtfs.TripSpec(
            "T7d", "7",
            (("S3", "17:35:00"), ("S2", "17:42:00"), ("S1", "17:45:00"), ("S4", "17:50:00")),
            direction=1, headsign="Harbour",
        ),
        gtfs.TripSpec("T9a", "9", (("S1", "17:20:00"), ("S4", "17:25:00")), headsign="Uplands"),
    ),
)


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The world at 17:00 with a real ``BusLive`` over ``BOARD``; the live feed is empty."""
    cache = tmp_path / "cache"
    cache.mkdir()
    gtfs.write(cache / "bctransit-gtfs.zip", BOARD)
    live = BusLive(cache, feed=lambda: b"", download=lambda _p: None)
    made = World(tmp_path, seen=serve_routes(monkeypatch, answer=ROUTES), bus_live=live)
    yield made
    made.conn.close()


def test_a_route_alone_is_its_next_buses_both_ways_at_his_stops_within_300_m(
    board: World,
) -> None:
    """``route=`` with no destination: the next buses of that route at the stops near Here.

    Stops within 300 m, nearest first; two buses per direction with their headsigns; from the
    timetable alone, with no Google request.
    """
    out = board.call({"route": "7"})
    assert out["summary"].startswith(
        "Next at Ring Rd at Oak St (100 m away): the 7 toward Downtown at 5:10 pm (scheduled);"
    )
    assert [(s["stop"], s["metres"]) for s in out["stops"]] == [
        ("Ring Rd at Oak St", 100), ("Oak St at Elm Ave", 184),
    ]
    near, other = out["stops"]
    assert [(b["headsign"], b["departs"]) for b in near["buses"]] == [
        ("Downtown", "5:10 pm"), ("Harbour", "5:15 pm"), ("Downtown", "5:40 pm"),
        ("Harbour", "5:45 pm"),
    ]
    assert [b["headsign"] for b in other["buses"]] == ["Harbour", "Harbour"]
    assert {b["route"] for s in out["stops"] for b in s["buses"]} == {"7"}
    assert {b["source"] for s in out["stops"] for b in s["buses"]} == {"scheduled"}
    assert board.seen == []
    receipt = board.receipt()
    assert (receipt["status"], receipt["reads_only"]) == ("done", True)
    assert receipt["covered"]["sources"] == ["BC Transit timetable", "BC Transit live"]
    assert receipt["covered"]["shown"] == 6


def test_a_stop_name_that_fits_several_stops_asks_which_nearest_first(board: World) -> None:
    """``stop=`` naming two stops reads no buses: it asks which, each candidate with its distance.

    The call only reads (``bus(stop=...)`` is a read, ADR 0319), and its receipt says so.
    """
    out = board.call({"stop": "Ring Road"})
    assert out["summary"].startswith(
        "More than one stop matches: Ring Rd at Oak St, Ring Rd at Pine St. Ask him which",
    )
    assert out["candidates"] == [
        {"stop": "Ring Rd at Oak St", "metres": 100},
        {"stop": "Ring Rd at Pine St", "metres": 2224},
    ]
    assert "stops" not in out
    assert board.seen == []
    receipt = board.receipt()
    assert receipt["status"] == "needs_choice"
    assert receipt["reads_only"] is True


@pytest.fixture
def board_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., World]]:
    """A world at 17:00 over a chosen export and realtime feed (an empty one by default)."""
    made: list[World] = []

    def make(export: gtfs.Export, feed: bytes = b"") -> World:
        cache = tmp_path / "cache"
        cache.mkdir(exist_ok=True)
        gtfs.write(cache / "bctransit-gtfs.zip", export)
        live = BusLive(cache, feed=lambda: feed, download=lambda _p: None)
        world = World(tmp_path, seen=serve_routes(monkeypatch, answer=ROUTES), bus_live=live)
        made.append(world)
        return world

    yield make
    for world in made:
        world.conn.close()


def test_a_board_for_tomorrow_does_not_take_todays_prediction_of_the_same_trip(
    board_of: Callable[..., World],
) -> None:
    """ADR 0342: the 7's trip T7a runs every day under one id; today's run is 2 minutes late.

    That prediction is today's 5:10 pm bus's, live; tomorrow's 5:10 pm stays on the timetable.
    """
    world = board_of(BOARD, realtime(("T7a", "S1", local_ms(17, 12))))

    def first_downtown(args: dict[str, Any]) -> tuple[str, str]:
        buses = world.call({"stop": "10001", **args})["stops"][0]["buses"]
        return next((b["departs"], b["source"]) for b in buses if b["headsign"] == "Downtown")

    assert first_downtown({}) == ("5:12 pm", "live")
    assert first_downtown({"at": "2026-10-07 17:00"}) == ("tomorrow (Wed), 5:10 pm", "scheduled")


def test_a_trip_that_ends_at_the_stop_is_not_a_bus_to_board_and_takes_no_slot(
    board_of: Callable[..., World],
) -> None:
    """Nobody boards at a trip's last stop: it is an arrival, not a bus on the board.

    Two 7s end at Ring Rd at Oak St at 5:05 and 5:10 pm. They are not listed, and they do not
    use up the two places of their direction: the 7 that goes on to the next stop at 5:25 pm is.
    """
    stops = (
        gtfs.StopSpec("S1", "Ring Rd at Oak St", 48.4609, -123.31, "10001"),
        gtfs.StopSpec("S2", "Ring Rd at Pine St", 48.48, -123.31),
        gtfs.StopSpec("S3", "Alder Ave at Fern St", 48.45, -123.31),
    )
    export = gtfs.Export(stops, (
        gtfs.TripSpec("T7go", "7", (("S1", "17:20:00"), ("S2", "17:30:00")), headsign="Pine"),
        gtfs.TripSpec(
            "T7e1", "7", (("S2", "16:55:00"), ("S1", "17:05:00")), direction=1, headsign="Oak",
        ),
        gtfs.TripSpec(
            "T7e2", "7", (("S2", "17:00:00"), ("S1", "17:10:00")), direction=1, headsign="Oak",
        ),
        gtfs.TripSpec(
            "T7c", "7", (("S2", "17:15:00"), ("S1", "17:25:00"), ("S3", "17:30:00")),
            direction=1, headsign="Harbour",
        ),
    ))
    out = board_of(export).call({"stop": "10001", "at": "17:00"})
    (stop,) = out["stops"]
    assert [(b["headsign"], b["departs"]) for b in stop["buses"]] == [
        ("Pine", "5:20 pm"), ("Harbour", "5:25 pm"),
    ]
