"""ADR 0246: his bus is one tool; the server remembers the list and the ride.

A scripted story against the real registry: Google Routes served by hand (no network), a ride
follower that is a recorder with canned answers, a real event log for the pin and its reminder.
Each row of ``STORY`` is a call and what must come back; the rows run in order, so the list a plan
made is what the next row pins and starts. The follower's own behaviour (feeds, ending rules, the
Live Activity) is `test_bus_ride`, which calls the same tool over the real follower.
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
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state import departures
from jarvis.state.event_log import iter_events_of_types, open_event_log
from tests.integration.test_transit_tool import FAKE_KEY, PLACES, _bus, _fix, _ms, _routes, _serve

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ROUTES = _routes(_bus("26", "17:12"), _bus("28", "17:20"))
"""Now is 17:00: the 26 leaves at 17:12 (walk 4: leave 17:08) and the 28 at 17:20 (leave 17:16)."""


@dataclass
class Follower:
    """A ride follower that records its calls and answers like the runtime's, from one script."""

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    route: str | None = None
    stop: str | None = None

    def ride(self) -> dict[str, Any]:
        """The runtime's description of the ride (ids and epoch times included)."""
        return {
            "ride_id": "ride-1", "route": self.route, "board_stop": "Alder St",
            "alight_stop": self.stop, "alight_known": self.stop is not None,
            "next_stop": "Cedar Ave", "stops_left": 3 if self.stop else None,
            "minutes_to_alight": 12 if self.stop else None,
            "eta_ms": _ms(17, 12) if self.stop else None, "stale": False, "live": True,
            "started_at_ms": _ms(17, 0), "source": "said", "warned": False,
            "credit": "BC Transit",
        }

    def watch(self, place: str) -> dict[str, Any]:
        """``bus(ride="watch")``."""
        self.calls.append(("watch", (place,)))
        return {"watching": True, "minutes": 45, "note": "if his phone moves with a bus ..."}

    def start(self, route: str | None, place: str | None, vehicle: str | None) -> dict[str, Any]:
        """``bus(ride="start")``: begin, or say it already has."""
        self.calls.append(("start", (route, place, vehicle)))
        if self.route is not None:
            return {"started": False, "already_riding": True, **self.ride()}
        self.route, self.stop = route, "Fir Lane" if place else None
        return {"started": True, **self.ride()}

    def set_stop(self, words: str) -> dict[str, Any]:
        """``bus(get_off=...)``: ambiguous for "e", else set."""
        self.calls.append(("set_stop", (words,)))
        if words == "e":
            return {"set": False, "ambiguous": True, "candidates": ["Elm Cres", "Fir Lane"],
                    "note": "ask which stop he means"}
        self.stop = "Gum St"
        return {"set": True, **self.ride()}

    def end(self) -> dict[str, Any]:
        """``bus(ride="end")``."""
        self.calls.append(("end", ()))
        if self.route is None:
            return {"ended": False, "note": "no ride is active"}
        gone, self.route, self.stop = self.route, None, None
        return {"ended": True, "route": gone}

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
    n: int = 0

    def __post_init__(self) -> None:
        """Build the runtime pieces the registry dispatches over."""
        self.paths = bootstrap_runtime(self.tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.lifecycle = ActionLifecycle()
        self.registry = build_default_registry(
            transit_api_key=FAKE_KEY, transit_places=lambda: PLACES, transit_offers=self.offers,
            here_location=_fix(), ride_control=self.follower.control(),
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
        out = json.loads(bundle.slots[0].tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}

    def pins(self) -> list[dict[str, Any]]:
        """The ``departure.pinned`` payloads in the log."""
        return [dict(e.payload) for e in iter_events_of_types(self.conn, ("departure.pinned",))]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Google served by hand at 17:00; the follower idle."""
    made = World(tmp_path, seen=_serve(monkeypatch, answer=ROUTES))
    yield made
    made.conn.close()


# (what the row shows, arguments, the summary must start with, keys the answer must have,
#  the error code instead, calls the follower must have got since the last row)
Row = tuple[str, dict[str, Any], str, tuple[str, ...], str, list[tuple[str, tuple[Any, ...]]]]
STORY: list[Row] = [
    (
        "no ride and no arguments: the next buses home, numbered, the sentence first",
        {},
        "To home, option 1: leave 5:08 pm (in 8 min), take the 26 from Stop of 26 at 5:12 pm,"
        " arrive 5:32 pm (24 min). 2 options are listed.",
        ("options", "from", "to"), "", [("status", ())],
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
        "On the 26 from Alder St; next stop Cedar Ave. He gets off at Fir Lane: 3 stops left,"
        " about 5:12 pm (in 12 min).",
        ("started", "route", "alight_stop"), "", [("start", ("26", "home", None))],
    ),
    (
        "no arguments on a ride: how it is going, no ids",
        {},
        "On the 26 from Alder St; next stop Cedar Ave. He gets off at Fir Lane: 3 stops left,"
        " about 5:12 pm (in 12 min).",
        ("next_stop", "stops_left", "credit"), "", [("status", ())],
    ),
    (
        "starting again while riding changes nothing and says so",
        {"ride": "start", "route": "26"},
        "Already following this ride; nothing changed. On the 26 from Alder St;",
        ("already_riding",), "", [("start", ("26", None, None))],
    ),
    (
        "his stop is named: set, and he is warned before it",
        {"get_off": "Gum"},
        "Set. On the 26 from Alder St; next stop Cedar Ave. He gets off at Gum St:"
        " 3 stops left, about 5:12 pm (in 12 min). He is warned one stop before it.",
        ("set", "alight_stop"), "", [("set_stop", ("Gum",))],
    ),
    (
        "a name that fits two stops: ask which",
        {"get_off": "e"},
        "More than one stop matches: Elm Cres, Fir Lane. Ask him which, then call",
        ("candidates",), "", [("set_stop", ("e",))],
    ),
    (
        "he got off",
        {"ride": "end"},
        "Stopped following the 26.",
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
        ("options",), "", [("status", ())],
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
        assert not {"ride_id", "eta_ms", "started_at_ms", "pin_id", "offer_id"} & out.keys(), label
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
        ({"get_off": "Gum", "ride": "end"}, "invalid_arguments", "ride=end takes no other"),
        ({"ride": "fly"}, "invalid_arguments", "ride must be start, watch or end"),
        ({"ride": "watch"}, "invalid_arguments", "ride=watch needs to="),
        ({"ride": "start", "pin": [1]}, "invalid_arguments", "ride=start takes option="),
        ({"ride": "start", "option": 1, "route": "7"}, "invalid_arguments", "not both"),
        ({"ride": "start", "option": 1}, "no_plan", "ask for the times first with bus(to=...)"),
        ({"option": 1}, "invalid_arguments", "option goes with ride=start"),
        ({"vehicle_id": "v1"}, "invalid_arguments", "vehicle_id goes with ride=start"),
        ({"cursor": "x"}, "invalid_arguments", "cursor is not an argument"),
        ({"destination": "home"}, "invalid_arguments", "to=, at= and route="),
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
    _serve(monkeypatch, answer=ROUTES)
    made = World(tmp_path)
    registry = build_default_registry(
        transit_api_key=FAKE_KEY, transit_places=lambda: PLACES, transit_offers=made.offers,
        here_location=_fix(),
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
    assert out["summary"].startswith("On the 26 from Alder St; next stop Cedar Ave. Where he gets")
    assert out["summary"].endswith(
        "The last plan, to home, lets him off the 26 at UVic Exchange;"
        " set it with bus(get_off=...) if he takes that."
    )
    world.call({"get_off": "Gum"})
    assert "The last plan" not in world.call({})["summary"]


def test_a_newer_plan_renumbers_the_list_and_a_pin_takes_the_new_first(world: World) -> None:
    """Option 1 is the first of the latest list; a newer pin replaces the older."""
    world.call({"to": "home"})
    assert world.call({"pin": [1]})["pinned"] is True
    held = departures.active(world.conn, _ms(17, 0))
    assert held is not None
    assert [t.first_route for t in held.trips] == ["26"]
    world.call({"to": "UVic", "route": "28"})  # a new list: the 28 first, then the 26
    assert world.call({"pin": [1]})["summary"].startswith("Pinned on the notch: the 28 from")
    held = departures.active(world.conn, _ms(17, 0))
    assert held is not None
    assert [t.first_route for t in held.trips] == ["28"]


def test_the_menu_entry_is_short_first_sentence_says_when_and_nothing_pages(world: World) -> None:
    """Description about 600 characters, led by when to use it; no cursor anywhere."""
    (definition,) = (d for d in world.registry.get_definitions() if d.name == "bus")
    assert len(definition.description) <= 650
    assert definition.description.startswith("Use for anything about the bus")
    assert "not for where he is" in definition.description.split(".")[0]
    assert "cursor" not in json.dumps(definition.input_schema)
    assert definition.input_schema["additionalProperties"] is False
