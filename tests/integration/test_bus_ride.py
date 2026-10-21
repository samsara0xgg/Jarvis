"""ADR 0224 — a bus ride is state she follows from boarding to getting off.

A scripted world: a clock, a made-up line of eight stops, buses that drive along it, a phone that
reports a fix every 30 s, a hand-built BC Transit export and feeds, and the real daemon pieces
(`Rides`, `Push`, the tools through the registry, the routes, the day line) over a real event log.
Nothing is fetched. The checks:

- watch -> candidate -> exactly one push -> tap -> ride; a bus that does not move with him, one on
  an unwatched route, a "not" answer and a trip already asked about push nothing more;
- said start through the tool, ambiguity returning candidates, and no push once he said so;
- the progress line in every turn's state block, about his bus and not another of the route;
- the one-stop warning once (Live Activity priority 10, else a plain alert), and each ending rule;
- the day line's ride item replacing the generic automotive move;
- the Live Activity start, update and end payloads, the registration of its tokens;
- a failing feed falling back to the timetable and saying so, and a ride with no feeds at all;
- the real `vehicleupdates.pb` capture parsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from google.transit import gtfs_realtime_pb2

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.execution.transit_tool import TransitOffers
from jarvis.runtime.bus_live import BusLive
from jarvis.runtime.locate import HerePolicy
from jarvis.runtime.push import Push
from jarvis.runtime.rides import RIDE_LINE_PREFIX, Rides
from jarvis.runtime.turn.context import live_lines
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state import departures as pins
from jarvis.state import push_tokens
from jarvis.state import rides as ride_state
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.ride_progress import (
    Fix,
    StopTime,
    Vehicle,
    left_at,
    position_at,
    progress,
    rides_with,
)
from jarvis.surface.apns import (
    RIDE_ATTRIBUTES_TYPE,
    Outcome,
    PushMessage,
    alert_payload,
    ride_activity_payload,
)
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from tests.support import gtfs
from tests.support.clock import FakeClock
from tests.support.transit import TRANSIT_NOW

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jarvis.surface.apns import ApnsClient

ZONE = ZoneInfo("America/Vancouver")
DAY = datetime(2026, 10, 7, tzinfo=ZONE)
SERVICE_DATE = "20261007"
STEP_S = 15
START_TOKEN = "ab" * 20
ACTIVITY_TOKEN = "cd" * 20
DEVICE_TOKEN = "ef" * 20

# A made-up line: eight stops in a row, 445 m apart, a bus a minute from one to the next. Two
# share a street name, as stops do.
STOPS = [(f"S{i}", name, 48.5 + 0.004 * i, -123.4) for i, name in enumerate(
    ["Alder St", "Birch Rd", "Cedar Ave", "Dogwood Way", "Elm Cres", "Fir Lane", "Gum St",
     "Elm Rd"],
)]
LNG = -123.4


def at(hhmm: str, seconds: int = 0) -> int:
    """Epoch ms of a local clock time on the scripted day."""
    h, m = (int(p) for p in hhmm.split(":"))
    return int((DAY + timedelta(hours=h, minutes=m, seconds=seconds)).timestamp() * 1000)


def lat_of(position: float) -> float:
    """Latitude of ``position`` stops along the line (0 at the first stop)."""
    return 48.5 + 0.004 * position


def _listed(route: str, board: str, leave: str, departs: str, arrive: str) -> dict[str, Any]:
    """An option as a plan lists it (ADR 0343): its first bus, its times aware and local."""
    def local(hhmm: str) -> datetime:
        return datetime.fromtimestamp(at(hhmm) / 1000, ZONE)

    return {
        "legs": [{"route": route, "board_stop": board, "departs": local(departs)}],
        "leave": local(leave), "arrive": local(arrive),
    }


def _static(path: Path) -> None:
    """The export: route 7 with two trips a minute apart, route 9 with one, along the line."""
    stops = tuple(gtfs.StopSpec(s, n, la, lo) for s, n, la, lo in STOPS)

    def trip(trip_id: str, route: str, minute: int, headsign: str) -> gtfs.TripSpec:
        calls = tuple((s, f"17:{minute + i:02d}:00") for i, (s, *_rest) in enumerate(STOPS))
        return gtfs.TripSpec(trip_id, route, calls, headsign=headsign)

    gtfs.write(path, gtfs.Export(stops, (
        trip("T7a", "7", 5, "Harbour"), trip("T7b", "7", 6, "Harbour"), trip("T9a", "9", 5, "Hill"),
    )))


@dataclass
class Bus:
    """A bus driving the line from stop 0 at ``leaves_ms``, a stop a minute, ``dx`` degrees off."""

    vehicle_id: str
    trip_id: str
    route: str
    leaves_ms: int
    dx: float = 0.0
    delay_s: int = 0
    gone: bool = False

    def position(self, now_ms: int) -> float:
        """Stops along the line, clamped to the line."""
        return min(7.0, max(0.0, (now_ms - self.leaves_ms) / 60_000))

    def place(self, now_ms: int) -> tuple[float, float]:
        """Its latitude and longitude."""
        return lat_of(self.position(now_ms)), LNG + self.dx


class FakeApns:
    """Records what would go to Apple; every push is taken."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.alerts: list[tuple[str, PushMessage]] = []
        self.rides: list[SimpleNamespace] = []

    def send_alert(
        self, _device_token: str, _environment: str, message: PushMessage, *, label: str,
    ) -> Outcome:
        """Record a notification."""
        self.alerts.append((label, message))
        return Outcome.SENT

    def send_ride(
        self, token: str, _environment: str, payload: bytes, *, label: str, priority: int,
    ) -> Outcome:
        """Record a Live Activity push."""
        self.rides.append(SimpleNamespace(
            token=token, body=json.loads(payload)["aps"], priority=priority, label=label,
        ))
        return Outcome.SENT


@dataclass
class World:
    """The clock, the buses, the phone, the daemon's pieces and the log they share."""

    root: Path
    clock_ms: int = field(default_factory=lambda: at("16:56"))
    buses: list[Bus] = field(default_factory=list)
    vehicle_error: Exception | None = None
    phone_fixes: list[tuple[int, float, float]] = field(default_factory=list)
    unpredicted: set[str] = field(default_factory=set)
    """Stop ids the TripUpdates feed leaves out, as BC Transit's often does."""

    def __post_init__(self) -> None:
        """Build the daemon's pieces over the scripted feeds."""
        self.paths = bootstrap_runtime(self.root)
        self.conn = open_event_log(self.paths.event_log)
        cache = self.root / "cache"
        cache.mkdir()
        _static(cache / "bctransit-gtfs.zip")
        self.live = BusLive(
            cache, feed=self._trip_updates, download=lambda _p: None, vehicles=self._vehicles,
        )
        self.offers = TransitOffers()
        self.rides = Rides(self.paths.event_log, self.live, offers=self.offers)
        self.rides.now = lambda: datetime.fromtimestamp(self.clock_ms / 1000, UTC)
        self.rides.defer = lambda call: call()
        self.apns = FakeApns()
        self.push = Push(self.paths.root, self.paths.event_log, cast("ApnsClient", self.apns))
        self.rides.push = self.push
        push_tokens.register(
            self.paths.root, "phone",
            push_tokens.Registration(DEVICE_TOKEN, "sandbox", ride_start_token=START_TOKEN),
        )

    # --- the feeds -----------------------------------------------------------------------

    def _vehicles(self) -> bytes:
        if self.vehicle_error is not None:
            raise self.vehicle_error
        message = gtfs_realtime_pb2.FeedMessage()
        message.header.gtfs_realtime_version = "2.0"
        message.header.timestamp = self.clock_ms // 1000
        for n, bus in enumerate(b for b in self.buses if not b.gone):
            one = message.entity.add(id=str(n)).vehicle
            one.trip.trip_id, one.trip.route_id = bus.trip_id, f"{bus.route}-VIC"
            one.trip.start_date = SERVICE_DATE
            one.vehicle.id = bus.vehicle_id
            place = bus.position(self.clock_ms)
            one.position.latitude, one.position.longitude = bus.place(self.clock_ms)
            one.timestamp = self.clock_ms // 1000
            if place == int(place):
                one.current_status = gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
                one.current_stop_sequence = int(place) + 1
            else:
                one.current_status = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO
                one.current_stop_sequence = int(place) + 2
            one.stop_id = STOPS[min(7, one.current_stop_sequence - 1)][0]
        return message.SerializeToString()

    def _trip_updates(self) -> bytes:
        message = gtfs_realtime_pb2.FeedMessage()
        message.header.gtfs_realtime_version = "2.0"
        for n, bus in enumerate(b for b in self.buses if not b.gone):
            update = message.entity.add(id=str(n)).trip_update
            update.trip.trip_id = bus.trip_id
            for i, (stop, *_rest) in enumerate(STOPS):
                if stop in self.unpredicted:
                    continue
                one = update.stop_time_update.add(stop_id=stop, stop_sequence=i + 1)
                one.arrival.time = (bus.leaves_ms // 1000) + 60 * i + bus.delay_s
        return message.SerializeToString()

    # --- the phone and the clock ----------------------------------------------------------

    def fix(self, lat: float, lng: float, *, accuracy: float = 15.0) -> None:
        """His phone reports a place now."""
        emit_event(
            self.conn, type="phone.location_observed", ts_epoch_ms=self.clock_ms,
            payload={"lat": lat, "lng": lng, "accuracy_m": accuracy}, ingestion_node="phone",
        )

    def motion(self, activity: str) -> None:
        """His phone reports an activity starting now."""
        emit_event(
            self.conn, type="phone.motion_observed", ts_epoch_ms=self.clock_ms,
            payload={"activity": activity, "confidence": "high", "started_at_ms": self.clock_ms},
            ingestion_node="phone",
        )

    def run(
        self, seconds: int, phone: Callable[[int], tuple[float, float] | None] | None = None,
    ) -> None:
        """Advance in 15 s steps: a fix from ``phone`` every 30 s, and a tick of the follower."""
        for _ in range(seconds // STEP_S):
            self.clock_ms += STEP_S * 1000
            where = None if phone is None else phone(self.clock_ms)
            if where is not None and (self.clock_ms // 1000) % 30 == 0:
                self.fix(*where)
            self.rides.tick()

    def with_bus(self, bus: Bus, aside: float = 0.0002) -> Callable[[int], tuple[float, float]]:
        """A phone riding ``bus``, ``aside`` degrees (about 22 m) off its line."""
        return lambda now: (bus.place(now)[0], bus.place(now)[1] + aside)

    # --- the log ------------------------------------------------------------------------

    def events(self, *types: str) -> list[dict[str, Any]]:
        """The payloads of the log's events of these types."""
        return [dict(e.payload) for e in iter_events_of_types(self.conn, types)]

    def pin(self, route: str = "7", departs: int | None = None) -> None:
        """Pin a trip of ``route`` whose bus leaves the first stop at ``departs``."""
        when = at("17:05") if departs is None else departs
        pins.pin(
            self.conn, reminder_id="reminder-none", ring="t1", offer_id=None, action_id="A1",
            trips=[pins.Trip(
                "t1", route, "Alder St", when - 180_000, when, "17:12", STOPS[0][2], LNG,
                "harbour", "Fir Lane", STOPS[5][2], LNG,
            )],
        )

    def ride(self) -> ride_state.Ride | None:
        """The ride he is on now."""
        return ride_state.active(self.conn, self.clock_ms)

    def app(self) -> TestClient:
        """The real routes over the follower."""
        async def act(body: dict[str, Any]) -> dict[str, Any]:
            return self.rides.act(body)

        async def read() -> dict[str, Any] | None:
            return self.rides.view()

        return TestClient(create_app(InherentDeps(
            submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
            ride_act=act, ride_read=read,
        )))

    def tool(
        self, args: dict[str, Any], places: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one `bus` call through the real registry, as the model would.

        ``places`` are his kept places (``home``, ``school``) as ``lat,lng``.
        """
        moment = datetime.fromtimestamp(self.clock_ms / 1000, UTC)
        clock = FakeClock(moment, zone=ZoneInfo("America/Vancouver"))
        registry = build_default_registry(
            ride_control=self.rides.control(), transit_offers=self.offers, clock=clock,
            here=HerePolicy(self.paths.event_log, clock),
            transit_places=None if places is None else lambda: places,
        )
        request = ActionRequest(
            action_id=f"A-bus-{self.clock_ms}", tool_name="bus", target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1", arguments=args,
            authorization_lease=None, run_id=None, turn_id=None,
        )
        lifecycle = ActionLifecycle()
        lifecycle.register(request.action_id)
        lifecycle.transition(request.action_id, "authorized")
        bundle = registry.dispatch(request, self.conn, self.paths, lifecycle)
        out = json.loads(bundle.tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """A world in English, at 16:56 on the scripted day."""
    before = lang.language()
    lang.set_language("en")
    made = World(tmp_path)
    yield made
    made.conn.close()
    lang.set_language(before)


def _board_said(world: World) -> Bus:
    """He is on bus T7a (past the second stop) and says so: a started ride, no pin."""
    world.clock_ms = at("17:07")
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.fix(*world.with_bus(bus)(world.clock_ms))
    # The last transit answer for the harbour said the 7 lets him off at Fir Lane.
    world.offers.to = "harbour"
    world.offers.alights[("7", "Alder St")] = ("Fir Lane", STOPS[5][2], LNG)
    assert world.tool({"ride": "start", "route": "7", "to": "Harbour"})["started"]
    return bus


# --- watch -> candidate -> one push -> tap -> ride ------------------------------------------


def test_a_pinned_trip_watch_asks_once_and_a_tap_starts_the_ride(world: World) -> None:
    """The pinned 7 leaves at 17:05: one question when his phone moves with it, then the ride."""
    world.pin()
    bus = Bus("v1", "T7a", "7", at("17:05"))
    other = Bus("v9", "T9a", "9", at("17:05"), dx=0.0003)  # a bus 30 m off, on an unwatched route
    world.buses = [bus, other]
    # Nothing is fetched before the watch has a reason: the trip is 9 minutes from leaving.
    world.clock_ms = at("16:50")
    world.rides.tick()
    assert world.rides._hist == {}  # noqa: SLF001 - nothing polled, nothing kept.
    world.clock_ms = at("16:56")
    # He waits at the stop (his phone at the first stop, the bus not yet moving): no question.
    world.run(450, lambda _now: (lat_of(0.0), LNG))  # to 17:03:30
    assert world.events("ride.candidate_offered") == []
    # The bus leaves at 17:05 and his phone goes with it: exactly one question.
    world.run(240, world.with_bus(bus))
    (offer,) = world.events("ride.candidate_offered")
    assert (offer["trip_id"], offer["route"], offer["vehicle_id"]) == ("T7a", "7", "v1")
    ((who, sent),) = world.apns.alerts
    assert who == "phone"
    assert (sent.title, sent.category, sent.thread) == (
        "Did you board the 7?", "ride_board", "ride",
    )
    body = json.loads(alert_payload(sent))
    assert body["ride"] == {"kind": "board", "candidate_id": offer["candidate_id"], "route": "7"}
    assert body["aps"]["category"] == "ride_board"
    # The same trip is never asked about again, however long he rides with it.
    world.run(120, world.with_bus(bus))
    assert len(world.events("ride.candidate_offered")) == 1
    assert len(world.apns.alerts) == 1
    # His tap starts the ride on that bus, matched to the pin, with its stop from the pin.
    reply = world.app().post(
        "/inherent/ride", json={"action": "board", "candidate_id": offer["candidate_id"]},
    )
    assert reply.status_code == 200
    (started,) = world.events("ride.started")
    assert (started["source"], started["trip_id"], started["vehicle_id"]) == ("tapped", "T7a", "v1")
    assert (started["route"], started["pin_tid"], started["device"]) == ("7", "t1", "phone")
    assert (started["board_stop"], started["alight_stop"]) == ("Alder St", "Fir Lane")
    assert started["alight_stop_id"] == "S5"
    assert world.events("ride.candidate_answered") == [
        {"candidate_id": offer["candidate_id"], "answer": "yes"},
    ]
    shown = reply.json()["ride"]
    assert (shown["route"], shown["alight_stop"], shown["source"]) == ("7", "Fir Lane", "tapped")
    # Tapping it again changes nothing.
    again = world.app().post(
        "/inherent/ride", json={"action": "board", "candidate_id": offer["candidate_id"]},
    )
    assert again.json()["reason"] == "answered"
    assert len(world.events("ride.started")) == 1


def test_not_boarded_ends_the_question_and_the_trip_is_not_asked_again(world: World) -> None:
    """Tapping 'not' records it, starts nothing, and that bus is never asked about again."""
    world.pin()
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.run(660, world.with_bus(bus))
    (offer,) = world.events("ride.candidate_offered")
    client = world.app()
    assert client.post(
        "/inherent/ride", json={"action": "not", "candidate_id": offer["candidate_id"]},
    ).status_code == 200
    assert world.events("ride.candidate_answered")[0]["answer"] == "no"
    assert world.ride() is None
    world.run(240, world.with_bus(bus))
    assert len(world.events("ride.candidate_offered")) == 1


def test_a_said_watch_asks_about_any_route_and_a_said_start_ends_the_asking(
    world: World,
) -> None:
    """ride=watch watches every route; a ride he starts himself gets no question."""
    bus = Bus("v9", "T9a", "9", at("17:05"))
    world.buses = [bus]
    world.clock_ms = at("17:02")
    assert world.tool({"ride": "watch", "to": "the harbour"})["watching"]
    (watched,) = world.events("ride.watched")
    assert watched["destination"] == "the harbour"
    assert watched["until_ms"] == at("17:02") + 45 * 60_000
    world.run(300, world.with_bus(bus))
    (offer,) = world.events("ride.candidate_offered")
    assert offer["route"] == "9"
    # He says he is on it before tapping: the ride is his, the watch is over, nothing more is sent.
    world.tool({"ride": "start", "route": "9", "to": "the harbour"})
    assert world.ride() is not None
    pushes = len(world.apns.alerts)
    world.run(300, world.with_bus(bus))
    assert len(world.apns.alerts) == pushes
    assert len(world.events("ride.candidate_offered")) == 1


def test_a_said_attempt_that_found_no_ride_quiets_the_question_for_five_minutes(
    world: World,
) -> None:
    """He said he boarded but she must ask which bus: no notification asks him the same thing."""
    world.clock_ms = at("17:07")
    a = Bus("v1", "T7a", "7", at("17:05"))
    b = Bus("v2", "T7b", "7", at("17:05") + 8_000, dx=0.0004)
    world.buses = [a, b]
    world.tool({"ride": "watch", "to": "campus"})
    world.fix(*world.with_bus(a)(world.clock_ms))
    assert world.tool({"ride": "start", "route": "7"})["ambiguous"] is True
    world.run(240, world.with_bus(a))
    assert world.events("ride.candidate_offered") == []
    assert world.ride() is None
    world.run(240, world.with_bus(a))  # five minutes after the attempt, asking is allowed again
    assert len(world.events("ride.candidate_offered")) == 1


def test_a_watch_runs_out_and_a_quiet_phone_is_not_asked(world: World) -> None:
    """The watch ends after 45 minutes; with 'no-pop' the question is written but not pushed."""
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.clock_ms = at("17:00")
    world.tool({"ride": "watch", "to": "campus"})
    world.push.quiet = lambda: "no-pop"
    world.run(420, world.with_bus(bus))
    assert len(world.events("ride.candidate_offered")) == 1
    assert world.apns.alerts == []
    # Out of watch: nothing is polled any more.
    world.clock_ms = at("17:46")
    world.rides.tick()
    assert world.rides._hist == {}  # noqa: SLF001 - idle forgets the buses.


# --- saying it -----------------------------------------------------------------------------


def test_a_said_start_takes_the_bus_nearest_his_last_fix(world: World) -> None:
    """Of two buses of the 7, the one at his fix; the ride has stops from the destination."""
    world.clock_ms = at("17:07")
    near = Bus("v1", "T7a", "7", at("17:05"))
    far = Bus("v2", "T7b", "7", at("17:06") + 120_000)  # 700 m behind
    world.buses = [near, far]
    world.fix(*world.with_bus(near)(world.clock_ms))
    done = world.tool({"ride": "start", "route": "7", "to": "Fir Lane"})
    assert (done["started"], done["route"], done["source"]) == (True, "7", "said")
    (started,) = world.events("ride.started")
    assert (started["vehicle_id"], started["trip_id"]) == ("v1", "T7a")
    assert started["board_stop"] == "Cedar Ave"  # the stop nearest where his phone was
    assert started["alight_stop"] is None or started["alight_stop"] == "Fir Lane"


def test_two_equally_near_buses_are_candidates_and_the_choice_starts_the_ride(
    world: World,
) -> None:
    """Ambiguity returns both buses (no ride); the answer is passed back as vehicle_id."""
    world.clock_ms = at("17:07")
    a = Bus("v1", "T7a", "7", at("17:05"))
    b = Bus("v2", "T7b", "7", at("17:05") + 8_000, dx=0.0004)  # 50 m aside, a few metres behind
    world.buses = [a, b]
    world.fix(*world.with_bus(a)(world.clock_ms))
    answer = world.tool({"ride": "start", "route": "7"})
    assert answer["started"] is False
    assert answer["ambiguous"] is True
    assert {c["vehicle_id"] for c in answer["candidates"]} == {"v1", "v2"}
    assert all({"route", "next_stop", "distance_m"} <= c.keys() for c in answer["candidates"])
    assert world.ride() is None
    chosen = world.tool({"ride": "start", "route": "7", "vehicle_id": "v2"})
    assert chosen["started"] is True
    assert world.events("ride.started")[0]["vehicle_id"] == "v2"


def test_a_said_start_with_no_bus_near_is_a_tool_error_that_says_to_ask(world: World) -> None:
    """No bus near his fix and no pin: no ride, and the error tells her to ask which bus."""
    world.clock_ms = at("17:30")
    world.buses = [Bus("v1", "T7a", "7", at("17:05"))]  # long gone to the end of the line
    world.fix(48.6, LNG)
    out = world.tool({"ride": "start", "route": "7"})
    assert out.get("code") == "no_vehicle"
    assert "ask him" in json.dumps(out)
    assert world.ride() is None


def test_a_said_start_with_a_pin_and_no_fix_uses_the_pinned_trip(world: World) -> None:
    """Without a phone position, the pinned bus is found by its stop and time: source 'pin'."""
    world.pin()
    world.clock_ms = at("17:06")
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    out = world.tool({"ride": "start"})
    assert (out["started"], out["source"]) == (True, "pin")
    (started,) = world.events("ride.started")
    assert (started["trip_id"], started["pin_tid"], started["alight_stop"]) == (
        "T7a", "t1", "Fir Lane",
    )


# --- during the ride -------------------------------------------------------------------------


def test_the_state_line_is_about_his_bus_and_not_another_of_the_route(world: World) -> None:
    """Next stop, stops left, arrival at his stop; a later bus's lateness does not leak in."""
    bus = _board_said(world)
    other = Bus("v2", "T7b", "7", at("17:06"), delay_s=600)
    world.buses = [bus, other]
    world.run(30, world.with_bus(bus))
    line = world.rides.line()
    assert line is not None
    assert line.startswith(RIDE_LINE_PREFIX)
    assert "route 7" in line
    assert "Next stop: Dogwood Way" in line or "Next stop: Elm Cres" in line
    assert "His stop: Fir Lane" in line
    assert "stops left" in line
    # On time at Fir Lane (stop 5) is 17:10; the other bus is ten minutes late there.
    assert "arrives about 17:10" in line
    assert "17:20" not in line
    # It is in the state block of a turn, whole (the live-line cap is not 200 characters).
    assert live_lines((world.rides.line,)) == (line,)
    assert len(line) > 200
    assert world.rides.view()["stops_left"] in {3, 2}  # type: ignore[index]


def test_a_late_bus_moves_the_arrival_from_the_trip_updates(world: World) -> None:
    """The arrival at his stop is the feed's prediction, not the timetable's."""
    bus = _board_said(world)
    bus.delay_s = 180
    world.run(75, world.with_bus(bus))
    view = world.rides.view()
    assert view is not None
    assert (view["position_live"], view["arrival_live"]) == (True, True)
    assert view["eta_ms"] == at("17:10") + 180_000


def test_the_unknown_stop_is_asked_once_and_set_by_a_tool(world: World) -> None:
    """Without a destination she is told to ask once; bus(ride="get_off", stop=...) sets it."""
    world.clock_ms = at("17:07")
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.fix(*world.with_bus(bus)(world.clock_ms))
    done = world.tool({"ride": "start", "route": "7"})
    assert done["alight_known"] is False
    first = world.rides.line()
    assert first is not None
    assert "ask him once where he gets off" in first
    second = world.rides.line()
    assert second is not None
    assert "ask him once" not in second  # asked once per ride
    ambiguous = world.tool({"ride": "get_off", "stop": "Elm"})  # Elm Cres and Elm Rd are ahead
    assert ambiguous["set"] is False
    assert ambiguous["candidates"] == ["Elm Cres", "Elm Rd"]
    set_it = world.tool({"ride": "get_off", "stop": "Gum"})
    assert (set_it["set"], set_it["alight_stop"]) == (True, "Gum St")
    assert world.events("ride.alight_set")[0]["alight_stop"] == "Gum St"
    # ADR 0263: the same stop from a later turn.
    again = world.tool({"ride": "get_off", "stop": "Gum"})
    assert (again["set"], again["already"]) == (True, True)
    assert again["summary"].startswith("Already set to this stop, nothing changed. ")
    assert len(world.events("ride.alight_set")) == 1
    nothing = world.tool({"ride": "get_off", "stop": "Zebra"})
    assert nothing.get("code") == "no_such_stop"


def test_bus_with_no_arguments_is_how_the_ride_is_going_and_without_one_the_times(
    world: World,
) -> None:
    """ADR 0246: on a ride the answer is the ride (next stop, his stop, arrival); off it, none."""
    out = world.tool({})
    assert out["code"] == "unavailable"  # no ride, and this world has no Google key
    bus = _board_said(world)
    world.run(30, world.with_bus(bus))
    out = world.tool({})
    assert out["summary"].startswith("On the 7 toward Harbour from Cedar Ave; next stop ")
    assert "He gets off at Fir Lane:" in out["summary"]
    assert "about 5:10 pm" in out["summary"]
    assert "No live bus position" not in out["summary"]
    assert {"ride_id", "eta_ms", "started_at_ms", "note"}.isdisjoint(out)
    assert out["credit"] == "BC Transit"
    world.tool({"ride": "end"})
    assert world.tool({})["code"] == "unavailable"


def test_a_times_question_for_where_the_ride_goes_is_answered_from_the_ride(
    world: World,
) -> None:
    """ADR 0344: "to Harbour", said at boarding, and a place at his stop are the ride's own.

    This world has no Google key, so a question that fell through to a new plan is `unavailable`.
    He gets off at Fir Lane (48.52, -123.4); home is 33 m from it, school over a kilometre.
    """
    _board_said(world)
    places = {"home": "48.5203,-123.4", "school": "48.53,-123.4"}
    said = world.tool({"to": "Harbour"}, places)
    assert said["summary"].startswith("On the 7 toward Harbour from Cedar Ave; next stop ")
    assert "He gets off at Fir Lane:" in said["summary"]
    assert {"destination", "alight_lat", "alight_lng"}.isdisjoint(said)
    home = world.tool({"to": "home"}, places)
    assert home["summary"].startswith("On the 7 toward Harbour from Cedar Ave; next stop ")
    assert home["summary"].endswith(
        " From there it is about 33 m on foot to home, in a straight line."
    )
    assert world.tool({"to": "school"}, places)["code"] == "unavailable"


def test_a_ride_starts_from_the_option_the_last_list_numbered(world: World) -> None:
    """`ride=start, option=1` is that option's first bus and its destination, nothing copied."""
    world.clock_ms = at("17:07")
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.fix(*world.with_bus(bus)(world.clock_ms))
    nothing = world.tool({"ride": "start", "option": 1})
    assert nothing["code"] == "no_plan"
    world.offers.keep([_listed("7", "Alder St", "17:02", "17:05", "17:20")], "harbour")
    world.offers.alights[("7", "Alder St")] = ("Fir Lane", STOPS[5][2], LNG)
    started = world.tool({"ride": "start", "option": 1})
    assert started["started"] is True
    assert started["alight_stop"] == "Fir Lane"
    (event,) = world.events("ride.started")
    assert (event["route"], event["trip_id"], event["alight_stop"]) == ("7", "T7a", "Fir Lane")
    again = world.tool({"ride": "start", "option": 1})
    assert again["already_riding"] is True
    assert again["summary"].startswith("Already following this ride; nothing changed.")
    assert len(world.events("ride.started")) == 1
    missing = world.tool({"ride": "end"})
    assert missing["ended"] is True
    assert world.tool({"ride": "start", "option": 4})["code"] == "not_found"


def test_the_ride_survives_a_restart(world: World) -> None:
    """A new follower on the same log knows the ride after one tick."""
    _board_said(world)
    again = Rides(world.paths.event_log, world.live)
    again.now = world.rides.now
    again.tick()
    assert again.line() is not None
    assert again.view()["route"] == "7"  # type: ignore[index]


def test_starting_a_second_ride_closes_the_first(world: World) -> None:
    """One ride at a time: the older ends as 'replaced'."""
    _board_said(world)
    first = world.ride()
    assert first is not None
    ride_state.write_start(world.conn, ride_state.Ride("ride-new", "9", "said", world.clock_ms))
    assert [r.ride_id for r in ride_state.fold(world.conn, 0) if r.active] == ["ride-new"]
    ended = world.events("ride.ended")
    assert ended == [{"ride_id": first.ride_id, "reason": "replaced"}]


# --- the one-stop warning ---------------------------------------------------------------------


def test_the_warning_goes_once_one_stop_before_his_and_buzzes_the_live_activity(
    world: World,
) -> None:
    """Priority 10 with an alert on the activity; priority 5 for every other update."""
    bus = _board_said(world)
    world.run(15, world.with_bus(bus))
    push_tokens.register_body(world.paths.root, "phone", {
        "device_token": DEVICE_TOKEN, "environment": "sandbox",
        "ride_activity_token": ACTIVITY_TOKEN,
    })
    world.run(90, world.with_bus(bus))  # 17:08:45, the bus is between Dogwood Way and Elm Cres
    assert world.events("ride.warned") == []
    world.run(30, world.with_bus(bus))  # 17:09:15, it has reached Elm Cres, the stop before his
    assert len(world.events("ride.warned")) == 1
    urgent = [r for r in world.apns.rides if r.priority == 10]
    assert len(urgent) == 1
    assert urgent[0].token == ACTIVITY_TOKEN
    assert urgent[0].body["alert"]["title"] == "Almost there"
    assert "Fir Lane" in urgent[0].body["alert"]["body"]
    assert urgent[0].body["content-state"]["phase"] == "arriving"
    assert all(r.priority == 5 for r in world.apns.rides if r is not urgent[0])
    assert world.apns.alerts == []  # the activity buzzed; no second notification
    world.run(60, world.with_bus(bus))
    assert len(world.events("ride.warned")) == 1
    assert len([r for r in world.apns.rides if r.priority == 10]) == 1


def test_without_a_live_activity_the_warning_is_a_time_sensitive_alert(world: World) -> None:
    """No activity token: a plain notification, with the ride id, once."""
    bus = _board_said(world)
    world.run(210, world.with_bus(bus))
    ((_, sent),) = world.apns.alerts
    assert (sent.category, sent.time_sensitive, sent.title) == (
        "ride_alight", True, "Almost there",
    )
    assert sent.data is not None
    assert sent.data["ride"]["kind"] == "alight"
    assert len(world.events("ride.warned")) == 1


# --- the endings ------------------------------------------------------------------------------


def test_a_live_bus_with_no_prediction_for_his_stop_is_live_with_a_timetable_arrival(
    world: World,
) -> None:
    """ADR 0261: the bus's place and his arrival are two facts; the warning needs only the first."""
    world.unpredicted = {"S5"}  # Fir Lane, his stop: the TripUpdates feed leaves it out
    bus = _board_said(world)
    world.run(105, world.with_bus(bus))  # 17:08:45, between Dogwood Way and Elm Cres
    view = world.rides.view()
    assert view is not None
    assert (view["position_live"], view["arrival_live"]) == (True, False)
    assert view["eta_ms"] == at("17:10")  # the timetable's, no lateness known
    line, said = world.rides.line(), world.tool({})["summary"]
    assert line is not None
    for text in (line, said):
        assert "bus position is live" in text
        assert "arrival at his stop is a timetable estimate" in text
    assert world.events("ride.warned") == []
    world.run(30, world.with_bus(bus))  # 17:09:15, at Elm Cres: one stop left
    assert len(world.events("ride.warned")) == 1


@pytest.mark.parametrize("unpredicted", [frozenset[str](), frozenset({"S5"})])
def test_a_live_vehicle_is_never_called_missing_by_the_line_or_the_tool(
    world: World, unpredicted: frozenset[str],
) -> None:
    """Every tick, with or without his stop predicted, and right after setting his stop."""
    world.unpredicted = set(unpredicted)
    world.clock_ms = at("17:07")
    bus = Bus("v1", "T7a", "7", at("17:05"))
    world.buses = [bus]
    world.fix(*world.with_bus(bus)(world.clock_ms))
    assert world.tool({"ride": "start", "route": "7"})["started"]
    world.rides.defer = lambda _call: None  # the answer comes before the next tick, as it can
    # Answered from the bus's last seen position.
    set_it = world.tool({"ride": "get_off", "stop": "Fir Lane"})
    assert set_it["set"] is True
    assert "no live bus position" not in set_it["summary"].casefold()
    for _ in range(8):
        line, said = world.rides.line(), world.tool({})["summary"]
        assert line is not None
        for text in (line, said):
            assert "no live bus position" not in text.casefold()
            assert "live from BC Transit" in text
        world.run(15, world.with_bus(bus))


def test_no_live_vehicle_makes_both_facts_the_timetable_and_warns_on_nothing(
    world: World,
) -> None:
    """The place and the arrival both say timetable; a timetable alone does not buzz him."""
    bus = _board_said(world)
    world.run(15, world.with_bus(bus))
    world.vehicle_error = OSError("feed down")
    world.run(150, world.with_bus(bus))  # 17:09:45, by the clock one stop before his
    view = world.rides.view()
    assert view is not None
    assert (view["position_live"], view["arrival_live"], view["stops_left"]) == (False, False, 1)
    line, said = world.rides.line(), world.tool({})["summary"]
    assert line is not None
    for text in (line, said):
        assert "no live bus position" in text.casefold()
        assert "timetable gives where the bus is and when he arrives" in text
    assert world.events("ride.warned") == []


def _ended(world: World) -> dict[str, Any]:
    (ended,) = world.events("ride.ended")
    return ended


def test_two_fixes_away_from_the_bus_end_the_ride_at_the_first_of_them(world: World) -> None:
    """He gets off and walks: the 2nd far fix confirms; the end is the time of the 1st."""
    bus = _board_said(world)
    world.run(60, world.with_bus(bus))
    off = bus.place(world.clock_ms)

    def walking(now: int) -> tuple[float, float]:
        return off[0] - 0.0001 * ((now - world.clock_ms) // 1000) / 5, LNG  # drifts south

    world.run(15, walking)
    far_from = world.clock_ms
    world.run(15, walking)
    assert world.ride() is not None  # a single far fix is not enough
    world.run(90, walking)
    assert world.ride() is None
    assert _ended(world)["reason"] == "left_vehicle"
    end_ms = next(e.ts_epoch_ms for e in iter_events_of_types(world.conn, ("ride.ended",)))
    assert end_ms >= far_from


def test_passing_his_stop_and_walking_ends_the_ride(world: World) -> None:
    """Past Fir Lane with his phone still, motion 'walking': ended; 'automotive' keeps it."""
    bus = _board_said(world)
    world.run(240, world.with_bus(bus))
    ride = world.ride()
    assert ride is not None
    assert world.rides.view()["stops_left"] in {1, 0}  # type: ignore[index]
    world.run(120, world.with_bus(bus))  # the bus is past Fir Lane
    world.motion("automotive")
    world.run(30, world.with_bus(bus))
    assert world.ride() is not None
    world.motion("walking")
    # His phone stays with the bus (a stale position); only the motion says he is off.
    world.run(30)
    assert world.ride() is None
    assert _ended(world)["reason"] == "passed_stop"


def test_he_says_so_through_the_tool_and_the_route(world: World) -> None:
    """ride=end ends it; so does POST action=end; both ways write the reason 'said'."""
    _board_said(world)
    assert world.tool({"ride": "end"})["ended"] is True
    assert _ended(world)["reason"] == "said"
    assert world.tool({"ride": "end"})["ended"] is False
    _board_said(world)
    ride = world.ride()
    assert ride is not None
    client = world.app()
    unknown = client.post("/inherent/ride", json={"action": "end", "ride_id": "nope"})
    assert unknown.status_code == 404
    ended = client.post("/inherent/ride", json={"action": "end", "ride_id": ride.ride_id})
    assert ended.status_code == 200
    assert [e["reason"] for e in world.events("ride.ended")] == ["said", "said"]


def test_fifteen_minutes_past_the_scheduled_end_ends_the_ride(world: World) -> None:
    """The trip ends at 17:12; the bus leaves the feed; the ride ends at 17:27, not before."""
    bus = _board_said(world)
    world.run(60, world.with_bus(bus))
    bus.gone = True
    world.clock_ms = at("17:26")
    world.rides.tick()
    assert world.ride() is not None
    world.clock_ms = at("17:28")
    world.rides.tick()
    assert world.ride() is None
    assert _ended(world)["reason"] == "schedule_end"


def test_the_end_closes_the_live_activity_and_forgets_its_token(world: World) -> None:
    """An 'end' event with a dismissal date goes to the activity's token, which is then dropped."""
    bus = _board_said(world)
    world.run(15, world.with_bus(bus))
    push_tokens.register_body(world.paths.root, "phone", {
        "device_token": DEVICE_TOKEN, "environment": "sandbox",
        "ride_activity_token": ACTIVITY_TOKEN,
    })
    world.tool({"ride": "end"})
    last = world.apns.rides[-1]
    assert (last.token, last.body["event"], last.body["content-state"]["phase"]) == (
        ACTIVITY_TOKEN, "end", "ended",
    )
    assert last.body["dismissal-date"] > last.body["timestamp"]
    assert push_tokens.registrations(world.paths.root)["phone"].ride_activity_token is None


# --- the Live Activity -------------------------------------------------------------------------


def test_the_ride_starts_a_live_activity_by_push_then_updates_it_on_change(world: World) -> None:
    """Start to the push-to-start token with attributes; updates to the activity's own token."""
    bus = _board_said(world)
    world.run(15, world.with_bus(bus))
    (start,) = [r for r in world.apns.rides if r.body["event"] == "start"]
    assert start.token == START_TOKEN
    assert start.priority == 5
    assert start.body["attributes-type"] == RIDE_ATTRIBUTES_TYPE == "RideAttributes"
    assert set(start.body["attributes"]) == {"ride_id", "route", "board_stop", "started_at_ms"}
    assert start.body["attributes"]["route"] == "7"
    state = start.body["content-state"]
    assert set(state) == {
        "route", "headsign", "next_stop", "stops_left", "minutes_to_alight", "alight_stop",
        "position_live", "arrival_live", "phase",
    }
    assert (state["route"], state["headsign"], state["alight_stop"], state["phase"]) == (
        "7", "Harbour", "Fir Lane", "riding",
    )
    assert (state["position_live"], state["arrival_live"]) == (True, True)
    # No update before the phone has registered the activity's token; none when nothing changed.
    sent = len(world.apns.rides)
    world.run(15, world.with_bus(bus))
    assert len(world.apns.rides) == sent
    push_tokens.register_body(world.paths.root, "phone", {
        "device_token": DEVICE_TOKEN, "environment": "sandbox",
        "ride_activity_token": ACTIVITY_TOKEN,
    })
    world.run(60, world.with_bus(bus))
    updates = [r for r in world.apns.rides if r.body["event"] == "update"]
    assert updates
    assert all(r.token == ACTIVITY_TOKEN and r.priority == 5 for r in updates)
    assert all("alert" not in r.body for r in updates)
    assert updates[-1].body["content-state"]["stops_left"] < state["stops_left"]


def test_activity_payloads_and_registration_of_the_new_tokens() -> None:
    """The three bodies, and the registration's keep/clear/set rule for the two new tokens."""
    content = {"route": "7", "next_stop": "A", "stops_left": 2, "minutes_to_alight": 3,
               "alight_stop": "B", "stale": False, "phase": "riding"}
    start = json.loads(ride_activity_payload("start", content, 100, attributes={"ride_id": "r"}))
    assert start == {"aps": {"timestamp": 100, "event": "start", "content-state": content,
                             "attributes-type": "RideAttributes", "attributes": {"ride_id": "r"}}}
    end = json.loads(ride_activity_payload("end", content, 100))["aps"]
    assert (end["event"], end["dismissal-date"]) == ("end", 220)
    with pytest.raises(ValueError, match="attributes"):
        ride_activity_payload("start", content, 100)
    with pytest.raises(ValueError, match="start, update or end"):
        ride_activity_payload("stop", content, 100)
    buzz = json.loads(ride_activity_payload("update", content, 100, alert=("t", "b")))["aps"]
    assert buzz["alert"] == {"title": "t", "body": "b"}
    with pytest.raises(ValueError, match="aps"):
        alert_payload(PushMessage("t", "b", data={"aps": {}}))


def test_ride_tokens_are_kept_cleared_and_set_by_the_registration(tmp_path: Path) -> None:
    """Absent keeps, null clears, a string sets; the older tokens are untouched by all of it."""
    base = {"device_token": DEVICE_TOKEN, "environment": "sandbox"}
    push_tokens.register_body(tmp_path, "phone", {**base, "live_activity_token": "11" * 20})
    push_tokens.register_body(tmp_path, "phone", {**base, "ride_start_token": START_TOKEN})
    one = push_tokens.registrations(tmp_path)["phone"]
    assert (one.live_activity_token, one.ride_start_token) == ("11" * 20, START_TOKEN)
    push_tokens.register_body(tmp_path, "phone", {**base, "ride_activity_token": ACTIVITY_TOKEN})
    push_tokens.register_body(tmp_path, "phone", base)
    one = push_tokens.registrations(tmp_path)["phone"]
    assert (one.ride_start_token, one.ride_activity_token) == (START_TOKEN, ACTIVITY_TOKEN)
    push_tokens.register_body(tmp_path, "phone", {**base, "ride_activity_token": None})
    assert push_tokens.registrations(tmp_path)["phone"].ride_activity_token is None
    with pytest.raises(push_tokens.RegistrationError):
        push_tokens.register_body(tmp_path, "phone", {**base, "ride_start_token": "nothex"})


# --- the day line -----------------------------------------------------------------------------


def test_the_day_line_has_the_ride_and_not_the_automotive_move_it_covers(world: World) -> None:
    """A finished ride is an item with route, stops and how it ended; its move is dropped."""
    for activity, start, end in (
        ("automotive", "17:05", "17:20"), ("walking", "17:25", "17:40"),
        ("automotive", "18:30", "18:50"),
    ):
        emit_event(
            world.conn, type="phone.motion_observed", ts_epoch_ms=at(start), ingestion_node="phone",
            payload={"activity": activity, "confidence": "high", "started_at_ms": at(start),
                     "ended_at_ms": at(end)},
        )
    ride = ride_state.Ride("ride-day", "7", "tapped", at("17:06"), board_stop="Alder St",
                           alight_stop="Fir Lane", trip_id="T7a")
    ride_state.write_start(world.conn, ride)
    ride_state.write_end(world.conn, "ride-day", "left_vehicle", at("17:18"))
    first, last = day_window(DAY.date(), ZONE)
    items = fold_day(world.conn, first, last, at("19:00"), DaySources(ZONE)).items
    kinds = [(i["kind"], i.get("activity")) for i in items]
    assert ("move", "automotive") in kinds  # the 18:30 drive has no ride: it stays
    assert kinds.count(("move", "automotive")) == 1
    assert ("move", "walking") in kinds
    (item,) = (i for i in items if i["kind"] == "ride")
    assert item == {
        "id": "ride-day", "lane": "you", "kind": "ride", "start_ms": at("17:06"),
        "end_ms": at("17:18"), "state": "done", "route": "7", "board_stop": "Alder St",
        "alight_stop": "Fir Lane", "source": "tapped", "ended_reason": "left_vehicle",
        "credit": "BC Transit",
    }
    # A ride still going has no end and is 'now'.
    ride_state.write_start(world.conn, ride_state.Ride("ride-live", "9", "said", at("18:55")))
    live = [i for i in fold_day(world.conn, first, last, at("19:00"), DaySources(ZONE)).items
            if i["id"] == "ride-live"]
    assert (live[0]["end_ms"], live[0]["state"], live[0]["board_stop"]) == (None, "now", None)


# --- the feeds failing -----------------------------------------------------------------------


def test_a_failing_vehicle_feed_falls_back_to_the_timetable_and_says_so(world: World) -> None:
    """The line, the view and the activity say there is no live position while the feed is down."""
    bus = _board_said(world)
    world.run(30, world.with_bus(bus))
    assert world.rides.view()["position_live"] is True  # type: ignore[index]
    world.vehicle_error = OSError("feed down")
    world.run(30, world.with_bus(bus))
    view = world.rides.view()
    assert view is not None
    assert (view["position_live"], view["arrival_live"]) == (False, False)
    assert view["next_stop"] is not None  # from the timetable
    line = world.rides.line()
    assert line is not None
    assert "timetable" in line
    assert world.ride() is not None  # a dead feed never ends the ride
    world.vehicle_error = None
    world.run(30, world.with_bus(bus))
    assert world.rides.view()["position_live"] is True  # type: ignore[index]


def test_no_feeds_at_all_is_a_declared_ride_on_the_timetable(world: World) -> None:
    """Outside Victoria: he says it, the ride exists, there are no times, and it ends by clock."""
    bare = Rides(world.paths.event_log, None)
    bare.now = world.rides.now
    bare.defer = lambda call: call()
    world.clock_ms = at("17:07")
    out = bare.start("12", "campus", None, None)
    assert (out["started"], out["route"], out["position_live"]) == (True, "12", False)
    line = bare.line()
    assert line is not None
    assert "no bus data" in line
    assert bare.watch("campus")["watching"] is False  # already riding
    world.clock_ms = at("19:30")
    bare.tick()
    assert world.ride() is None
    assert _ended(world)["reason"] == "schedule_end"
    with pytest.raises(Exception, match="route"):
        Rides(world.paths.event_log, None).control().start(None, None, None, None, lambda: None)


# --- the routes -------------------------------------------------------------------------------


def test_the_ride_routes_validate_and_report(world: World) -> None:
    """A board with no candidate is 422, an unknown one 404; GET is the ride or null."""
    client = world.app()
    assert client.get("/inherent/ride").json() == {"ride": None}
    assert client.post("/inherent/ride", json={"action": "board"}).status_code == 422
    assert client.post("/inherent/ride", json={"action": "fly"}).status_code == 422
    assert client.post(
        "/inherent/ride", json={"action": "board", "candidate_id": "cand-nope"},
    ).status_code == 404
    _board_said(world)
    shown = client.get("/inherent/ride").json()["ride"]
    assert shown["route"] == "7"
    assert {"ride_id", "alight_stop", "next_stop", "stops_left", "minutes_to_alight", "eta_ms",
            "position_live", "arrival_live", "warned", "started_at_ms", "source",
            "credit"} <= shown.keys()
    assert shown["credit"] == "BC Transit"


# --- pure rules and data ------------------------------------------------------------------------


def test_the_rules_on_positions_as_a_table() -> None:
    """Boarding, leaving and progress, as inputs and expected outputs."""
    line = [(t * 1000, lat_of(t / 60), LNG) for t in range(0, 601, 15)]  # a stop a minute
    def fix(sec: int, aside: float = 0.0, *, acc: float = 10.0, along: float = 0.0) -> Fix:
        return Fix(sec * 1000, lat_of(sec / 60 + along), LNG + aside, acc, "p")

    assert position_at(line, 30_000) == (lat_of(0.5), LNG)
    assert position_at(line, 700_000) is None
    assert position_at(line, 610_000) == (lat_of(10.0), LNG)
    assert rides_with(fix(30), fix(60), line) == pytest.approx(0, abs=1)
    assert rides_with(fix(30, 0.0003), fix(60, 0.0003), line) is not None  # 22 m aside
    assert rides_with(fix(30, 0.0013), fix(60, 0.0013), line) is None  # 96 m aside
    assert rides_with(fix(30), fix(60, acc=300), line) is None  # an inaccurate fix decides nothing
    still = Fix(30_000, lat_of(0.5), LNG, 10, "p")
    assert rides_with(still, Fix(60_000, lat_of(0.5), LNG, 10, "p"), line) is None  # not moving
    far = [fix(s, 0.003) for s in (30, 45, 60)]  # 220 m aside
    assert left_at(far, line) == 30_000
    assert left_at([far[0], fix(45), far[2]], line) is None  # a near fix ends the run
    stops = [StopTime(i + 1, s, n, la, lo, at("17:05") + 60_000 * i)
             for i, (s, n, la, lo) in enumerate(STOPS)]
    bus = Vehicle("v", "T", "7", lat_of(1.5), LNG, 0, "S2", 3, 2)
    got = progress(stops, "S5", bus, {}, at("17:06", 30))
    assert (got.next_stop, got.stops_left, got.minutes, got.passed, got.position_live) == (
        "Cedar Ave", 4, 4, False, True,
    )
    assert got.arrival_live is False  # no prediction for his stop: a timetable estimate
    stopped = Vehicle("v", "T", "7", lat_of(2), LNG, 0, "S2", 3, 1)
    assert progress(stops, "S5", stopped, {}, at("17:07")).next_stop == "Dogwood Way"
    gone = Vehicle("v", "T", "7", lat_of(6), LNG, 0, "S6", 7, 2)
    assert progress(stops, "S5", gone, {}, at("17:11")).passed is True
    table = progress(stops, "S5", None, {}, at("17:06", 30))
    assert (table.next_stop, table.position_live, table.stops_left) == ("Cedar Ave", False, 4)
    late = progress(stops, "S5", bus, {"S2": at("17:07") + 120_000}, at("17:06", 30))
    assert late.eta_ms == at("17:10") + 120_000
    assert late.arrival_live is False  # his stop is not in the prediction; the delay is carried
    said = progress(stops, "S5", bus, {"S5": at("17:10") + 60_000}, at("17:06", 30))
    assert (said.eta_ms, said.arrival_live) == (at("17:10") + 60_000, True)


def test_a_transit_answer_remembers_where_the_first_bus_lets_him_off() -> None:
    """Google's arrival stop (with its place) is kept per route and board stop."""
    answer = {"routes": [{"legs": [{"steps": [{"transitDetails": {
        "stopDetails": {
            "departureStop": {"name": "Alder St", "location": {"latLng": {
                "latitude": STOPS[0][2], "longitude": LNG}}},
            "arrivalStop": {"name": "Fir Lane", "location": {"latLng": {
                "latitude": STOPS[5][2], "longitude": LNG}}},
        },
        "transitLine": {"nameShort": "7"},
    }}]}]}]}
    offers = TransitOffers()
    offers.remember(answer)
    assert offers.stops[("7", "Alder St")] == (STOPS[0][2], LNG)
    assert offers.alights[("7", "Alder St")] == ("Fir Lane", STOPS[5][2], LNG)
    offers.put([_listed("7", "Alder St", "17:02", "17:05", "17:12")], "harbour", TRANSIT_NOW)
    assert offers.alight_for("7", "Harbour") == ("Fir Lane", STOPS[5][2], LNG)
    assert offers.alight_for("7", "elsewhere") is None
    assert offers.alight_for("12") is None


def test_the_captured_vehicle_feed_parses(tmp_path: Path) -> None:
    """The real vehicleupdates.pb capture: buses with trips, positions and stop sequences."""
    here = Path(__file__).parent.parent / "fixtures" / "bctransit"
    raw = (here / "vehicleupdates.pb").read_bytes()

    def no_export(_path: Path) -> None:
        offline = "offline"
        raise OSError(offline)

    live = BusLive(tmp_path, vehicles=lambda: raw, download=no_export)
    buses = live.vehicles()
    assert len(buses) > 40
    one = buses[0]
    assert all((one.trip_id, one.route, one.vehicle_id, one.start_date))
    assert 48 < one.lat < 49
    assert -124 < one.lng < -123
    assert one.route == next(b for b in buses if b.trip_id == one.trip_id).route
    assert any(b.stop_seq and b.stop_id and b.status is not None for b in buses)


def test_the_follower_does_nothing_without_a_reason(world: World) -> None:
    """No watch, no ride, no pin: the feed is not touched."""
    calls: list[int] = []
    world.live.vehicles = lambda: calls.append(1) or []  # type: ignore[method-assign,func-returns-value]
    world.run(60)
    assert calls == []
