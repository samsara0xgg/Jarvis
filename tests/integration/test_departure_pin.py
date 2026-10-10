"""ADR 0200, 0203 — a bus trip pinned on the notch: tool, card, ring, served pin, live refresh.

The real tools run through ``ToolRegistry.dispatch`` onto a real event log; the real ``Reminders``
tick and ``Departures`` fold that log under a fake clock, and the real ``/inherent/notices`` routes
serve and unpin it. Google's answer is hand-built; BC Transit's feed and static export are tiny
hand-built bytes. No network.
"""

# ruff: noqa: RUF001 - the reminder text is Chinese, as the daemon says it.
from __future__ import annotations

import dataclasses
import io
import json
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from google.transit import gtfs_realtime_pb2

from jarvis.decision.commentary import _dispatch_key
from jarvis.decision.packet import assemble_packet, format_pending_clarification_note
from jarvis.deployment import bootstrap_runtime
from jarvis.execution import transit_tool
from jarvis.execution.tools import ActionLifecycle, ToolError, build_default_registry
from jarvis.execution.transit_tool import TransitOffers, live_card
from jarvis.runtime.bus_live import BusLive
from jarvis.runtime.departures import REFRESH_S, Departures
from jarvis.runtime.inherent_loop import _notice_deps
from jarvis.runtime.reminders import Reminders
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state import departures as departures_state
from jarvis.state import event_log
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projections import CLARIFICATION_EVENT_TYPES, PendingClarification
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 7, 17, 0, tzinfo=ZONE)
STOP = (48.4600, -123.3100)
LAT_LNG = {"latitude": STOP[0], "longitude": STOP[1]}


def _local(hhmm: str) -> datetime:
    clock = datetime.strptime(hhmm, "%H:%M").time()  # noqa: DTZ007
    return datetime.combine(NOW.date(), clock, tzinfo=ZONE)


def _google(depart: str = "17:05", route: str = "28") -> dict[str, Any]:
    """One Google route: a 3 min walk, one bus whose first stop carries its position."""
    at = _local(depart).astimezone(UTC)
    return {
        "routes": [
            {
                "legs": [
                    {
                        "steps": [
                            {"staticDuration": "180s", "travelMode": "WALK"},
                            {
                                "staticDuration": "900s",
                                "travelMode": "TRANSIT",
                                "transitDetails": {
                                    "stopDetails": {
                                        "departureStop": {
                                            "name": "Shelbourne at Pear",
                                            "location": {"latLng": LAT_LNG},
                                        },
                                        "departureTime": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "arrivalStop": {"name": "Home stop"},
                                        "arrivalTime": (at + timedelta(minutes=15)).strftime(
                                            "%Y-%m-%dT%H:%M:%SZ"
                                        ),
                                    },
                                    "headsign": "Downtown",
                                    "transitLine": {"nameShort": route, "name": "Long"},
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    }


def _static(path: Path) -> None:
    """A tiny static export: BOMs as BC Transit ships them, two routes, one stop here, one far."""
    files = {
        "routes.txt": "route_id,route_short_name\n1,28\n2,12\n",
        "trips.txt": "route_id,trip_id\n1,T28a\n1,T28b\n2,T12a\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        f"S1,Shelbourne Pear,{STOP[0] + .0002},{STOP[1]}\nS2,Far,48.5,-123.4\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, "﻿" + text)


def _feed(*trips: tuple[str, str, str, int]) -> bytes:
    """A FeedMessage of ``(trip_id, stop_id, scheduled HH:MM, delay_s)``."""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    for number, (trip, stop, scheduled, delay) in enumerate(trips):
        update = message.entity.add(id=str(number)).trip_update
        update.trip.trip_id = trip
        one = update.stop_time_update.add(stop_id=stop)
        one.departure.time = int(_local(scheduled).timestamp()) + delay
        one.departure.delay = delay
    return message.SerializeToString()


class _World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.offers = TransitOffers()
        self.local_now = NOW
        self.registry = build_default_registry(
            transit_api_key="not-a-key",
            transit_places={"home": "48.48,-123.38"},
            transit_offers=self.offers,
        )
        self.lifecycle = ActionLifecycle()
        self.n = 0
        self.clock = NOW.astimezone(UTC)
        self.google: dict[str, Any] = _google()
        self.feed = b""
        self.feed_calls = 0
        self.feed_error: Exception | None = None
        cache = tmp_path / "cache"
        cache.mkdir()
        _static(cache / "bctransit-gtfs.zip")
        self.live = BusLive(cache, feed=self._serve_feed, download=lambda _p: None)
        self.said: list[str] = []
        self.reminders = Reminders(
            self.paths.event_log,
            moment=SimpleNamespace(hold=lambda: None),  # type: ignore[arg-type]
            departures=Departures(self.paths.event_log, self.live, self.offers),
        )
        self.reminders.now = lambda: self.clock
        self.reminders.may_speak = lambda: True
        self.reminders.say = self.said.append
        self.departures = self.reminders.departures
        monkeypatch.setattr(transit_tool, "_now", lambda: self.local_now)
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *_a, **_k: io.BytesIO(json.dumps(self.google).encode()),
        )

    def _serve_feed(self) -> bytes:
        self.feed_calls += 1
        if self.feed_error:
            raise self.feed_error
        return self.feed

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        action_id = f"A{self.n}"
        request = ActionRequest(
            action_id=action_id,
            tool_name=name,
            target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L1",
            arguments=args,
            authorization_lease=None,
            run_id=None,
            turn_id=None,
        )
        self.lifecycle.register(action_id)
        self.lifecycle.transition(action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)
        out = json.loads(bundle.slots[0].tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}

    def at(self, hhmm: str) -> None:
        """Both clocks (the tool's and the daemon's) to that time today."""
        self.local_now = _local(hhmm)
        self.clock = self.local_now.astimezone(UTC)

    def look_up(self) -> None:
        self.call("transit", {"origin": "home", "destination": "Mayfair Mall"})

    def pin(self, leave: str = "17:02", departs: str = "17:05", **more: str) -> dict[str, Any]:
        args = {
            "leave_at": leave, "route": "28", "board_stop": "Shelbourne at Pear",
            "departs": departs, "arrive_at": "17:20", "destination": "Home", **more,
        }
        return self.call("pin_departure", args)

    def pending(self) -> list[reminder_state.Reminder]:
        return reminder_state.pending(self.conn)

    def events(self, *types: str) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in iter_events_of_types(self.conn, types)]

    def app(self) -> TestClient:
        deps = _notice_deps(None, self.reminders, None)
        return TestClient(
            create_app(
                InherentDeps(
                    submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), **deps
                )
            )
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """A world after one bus lookup, with Chinese fixed text as the daemon runs."""
    before = lang.language()
    lang.set_language("zh")
    made = _World(tmp_path, monkeypatch)
    made.look_up()
    yield made
    made.conn.close()
    lang.set_language(before)


def test_a_pin_is_an_event_and_a_reminder_at_the_leave_time(world: _World) -> None:
    """The pin writes departure.pinned with the stop's position, and rings at leave_at in words."""
    made = world.pin()
    assert made["pinned"]
    (pinned,) = world.events("departure.pinned")
    (trip,) = pinned["trips"]
    assert (trip["route"], trip["board_stop"], trip["arrive_at"]) == (
        "28", "Shelbourne at Pear", "17:20",
    )
    assert (trip["stop_lat"], trip["stop_lng"], trip["to"]) == (*STOP, "home")
    assert trip["leave_at_ms"] == int(_local("17:02").timestamp() * 1000)
    (ring,) = world.pending()
    assert ring.reminder_id == pinned["reminder_id"]
    assert ring.due_at_ms == trip["leave_at_ms"]
    assert ring.text == "该出门了，28 路 17:05 在 Shelbourne at Pear 上车"
    world.clock = _local("17:02").astimezone(UTC) + timedelta(seconds=5)
    assert world.reminders.tick() == 1
    assert world.said == ["提醒：该出门了，28 路 17:05 在 Shelbourne at Pear 上车"]


def test_the_notices_route_serves_the_pin_until_the_bus_has_gone(world: _World) -> None:
    """GET /inherent/notices carries the pin; it is gone once departs has passed."""
    world.pin()
    client = world.app()
    pin = client.get("/inherent/notices").json()["departure"]
    assert (pin["route"], pin["leave_at"], pin["departs"], pin["arrive_at"], pin["to"]) == (
        "28", "17:02", "17:05", "17:20", "home",
    )
    assert pin["stale"] is False
    assert pin["leave_at_ms"] == int(_local("17:02").timestamp() * 1000)
    world.clock = _local("17:04").astimezone(UTC)
    assert client.get("/inherent/notices").json()["departure"] is not None
    world.clock = _local("17:05").astimezone(UTC)
    assert client.get("/inherent/notices").json()["departure"] is None


def test_a_second_pin_replaces_the_first_and_cancels_its_reminder(world: _World) -> None:
    """A second pin replaces the first and cancels its reminder."""
    first = world.pin()
    (before,) = world.pending()
    world.pin(leave="17:10", departs="17:13")
    (after,) = world.pending()
    assert after.reminder_id != before.reminder_id
    assert after.due_at_ms == int(_local("17:10").timestamp() * 1000)
    assert [e["reminder_id"] for e in world.events("reminder.cancelled")] == [before.reminder_id]
    pin = world.app().get("/inherent/notices").json()["departure"]
    assert pin["id"] != first["pin_id"]
    assert pin["leave_at"] == "17:10"


def test_unpinning_from_the_notch_cancels_the_ring(world: _World) -> None:
    """Unpinning from the notch cancels the ring."""
    made = world.pin()
    client = world.app()
    reply = client.post(f"/inherent/notices/{made['pin_id']}", json={"action": "dismissed"})
    assert reply.status_code == 200
    assert world.pending() == []
    assert [e["pin_id"] for e in world.events("departure.unpinned")] == [made["pin_id"]]
    assert client.get("/inherent/notices").json()["departure"] is None
    # Pressing the x twice is harmless.
    again = client.post(f"/inherent/notices/{made['pin_id']}", json={"action": "dismissed"})
    assert again.status_code == 200


def test_the_tool_refuses_a_past_leave_time_and_a_bus_before_it(world: _World) -> None:
    """The tool refuses a past leave time and a bus before it."""
    assert world.pin(leave="16:58")["code"] == "time_in_past"
    assert world.pin(leave="17:06", departs="17:05")["code"] == "invalid_argument"
    assert world.pin(leave="soon")["code"] == "invalid_argument"
    assert world.events("departure.pinned") == []
    assert world.pending() == []
    # A minute late still rings at the next tick.
    assert world.pin(leave="16:59", departs="17:05")["pinned"]


def test_the_tool_is_quiet_and_model_only() -> None:
    """The tool is quiet and model only."""
    registry = build_default_registry(transit_api_key="not-a-key", transit_places={})
    (definition,) = (d for d in registry.get_definitions() if d.name == "pin_departure")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.risk_level == "L1"
    assert not definition.requires_confirmation
    assert _dispatch_key("pin_departure") is None
    assert "ONLY when he asks" in definition.description
    assert "pin_departure" not in {
        d.name for d in build_default_registry().get_definitions()
    }


def _late(world: _World, minutes: int = 3) -> None:
    """The 28 at the pinned stop is running ``minutes`` behind in the feed."""
    world.feed = _feed(("T28a", "S1", "17:05", minutes * 60), ("T12a", "S1", "17:05", 0))


def test_a_late_bus_moves_the_times_and_the_reminder(world: _World) -> None:
    """A late bus moves the times and the reminder."""
    world.pin()
    (before,) = world.pending()
    _late(world)
    world.reminders.tick()  # the first tick of a pin only starts its clock
    assert world.feed_calls == 0
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.feed_calls == 1
    (moved,) = world.events("departure.updated")
    assert moved["departs_at_ms"] == int(_local("17:08").timestamp() * 1000)
    assert moved["leave_at_ms"] == int(_local("17:05").timestamp() * 1000)
    assert moved["arrive_at"] == "17:23"
    (after,) = world.pending()
    assert after.reminder_id == moved["reminder_id"] != before.reminder_id
    assert after.due_at_ms == moved["leave_at_ms"]
    assert after.text == "该出门了，28 路 17:08 在 Shelbourne at Pear 上车"
    assert [e["reminder_id"] for e in world.events("reminder.cancelled")] == [before.reminder_id]
    pin = world.app().get("/inherent/notices").json()["departure"]
    assert (pin["leave_at"], pin["departs"], pin["arrive_at"], pin["stale"]) == (
        "17:05", "17:08", "17:23", False,
    )
    # The same delay again changes nothing, and the next bus of the route is not mistaken for it.
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert len(world.events("departure.updated")) == 1


def test_refresh_runs_every_30_seconds_not_every_tick(world: _World) -> None:
    """Refresh runs every 30 seconds not every tick."""
    world.pin()
    _late(world, 0)
    for _ in range(5):
        world.clock += timedelta(seconds=15)
        world.reminders.tick()
    assert world.feed_calls == 2  # ticks 2 and 4 (tick 1 only started the clock)


def test_a_bus_the_feed_does_not_show_marks_the_pin_stale_and_keeps_its_times(
    world: _World,
) -> None:
    """A bus the feed does not show marks the pin stale and keeps its times."""
    world.pin()
    world.reminders.tick()
    # The wrong route at the stop, and the right route at the wrong stop.
    world.feed = _feed(("T12a", "S1", "17:05", 0), ("T28a", "S2", "17:05", 0))
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.events("departure.updated") == []
    pin = world.app().get("/inherent/notices").json()["departure"]
    assert (pin["stale"], pin["leave_at"], pin["departs"]) == (True, "17:02", "17:05")
    # The feed comes back: the mark clears with no event if nothing moved.
    world.feed = _feed(("T28a", "S1", "17:05", 0))
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.app().get("/inherent/notices").json()["departure"]["stale"] is False


def test_a_dead_feed_marks_stale_and_never_drops_the_pin(world: _World) -> None:
    """A dead feed marks stale and never drops the pin."""
    world.pin()
    world.reminders.tick()
    world.feed_error = TimeoutError("feed down")
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    pin = world.app().get("/inherent/notices").json()["departure"]
    assert pin["stale"] is True
    assert len(world.pending()) == 1


def test_a_pin_without_the_stops_position_is_stale_not_wrong(world: _World) -> None:
    """A pin without the stops position is stale not wrong."""
    world.google["routes"][0]["legs"][0]["steps"][1]["transitDetails"]["stopDetails"][
        "departureStop"
    ].pop("location")
    world.look_up()
    world.pin()
    world.reminders.tick()
    _late(world)
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.feed_calls == 0
    assert world.app().get("/inherent/notices").json()["departure"]["stale"] is True


def test_refresh_stops_once_the_bus_has_left(world: _World) -> None:
    """Refresh stops once the bus has left."""
    world.pin()
    world.reminders.tick()
    _late(world, 0)
    world.clock = _local("17:05").astimezone(UTC)
    world.reminders.tick()
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.feed_calls == 0
    assert world.app().get("/inherent/notices").json()["departure"] is None


def test_the_static_export_is_downloaded_once_a_day(tmp_path: Path) -> None:
    """The static export is downloaded once a day."""
    cache = tmp_path / "cache"
    calls: list[Path] = []

    def download(part: Path) -> None:
        calls.append(part)
        _static(part)

    live = BusLive(cache, feed=lambda: _feed(("T28a", "S1", "17:05", 120)), download=download)
    around = int(_local("17:05").timestamp() * 1000)
    got = live.departure("28", STOP, around)
    assert got == ("T28a", around + 120_000)
    assert live.departure("28", STOP, around, "T28a") == got
    assert live.departure("28", STOP, around + 15 * 60_000) is None  # beyond ten minutes
    assert live.departure("12", STOP, around) is None  # that trip is not in the feed
    assert len(calls) == 1


# --- ADR 0203: the card under the notch ------------------------------------------------


def _answer(world: _World, *legs: tuple[str, str]) -> None:
    """The next ``transit`` answer has one option per ``(first bus departs, route)``."""
    world.google = {"routes": [r for at, route in legs for r in _google(at, route)["routes"]]}
    world.look_up()


def _slot(world: _World) -> PendingClarification:
    """The conversation's card (ADR 0205): the ask-card slot, its `trip` the bus rows."""
    found = PendingClarification.from_events(
        iter_events_of_types(world.conn, CLARIFICATION_EVENT_TYPES)
    )
    assert found is not None
    assert found.trip is not None
    return found


def _offer(world: _World) -> dict[str, Any]:
    """The card as the companion is served it now: the offer's id and the rows still catchable."""
    shown = live_card(_slot(world).trip or {})
    assert shown is not None
    return {"id": shown["offer_id"], "options": shown["options"]}


def _take(world: _World, action: str, **more: str | int) -> Any:  # noqa: ANN401
    return world.app().post("/inherent/departure", json={"action": action, **more})


def test_an_answer_serves_one_row_per_option_and_drops_the_past(world: _World) -> None:
    """Each option is a row; a row whose leave time has passed goes, the rest keep their index."""
    _answer(world, ("17:05", "28"), ("17:15", "28"), ("17:18", "12"))
    offer = _offer(world)
    assert offer["id"].startswith("offer-")
    rows = [
        (o["index"], o["route"], o["leave_at"], o["departs"], o["arrive_at"])
        for o in offer["options"]
    ]
    assert rows == [
        (0, "28", "17:02", "17:05", "17:20"),
        (1, "28", "17:12", "17:15", "17:30"),
        (2, "12", "17:15", "17:18", "17:33"),
    ]
    assert offer["options"][0]["board_stop"] == "Shelbourne at Pear"
    assert offer["options"][0]["to"] == "mayfair mall"
    world.local_now = _local("17:10")
    assert [o["index"] for o in _offer(world)["options"]] == [1, 2]
    world.local_now = _local("17:40")
    assert live_card(_slot(world).trip or {}) is None


def test_the_bus_card_asks_the_model_nothing(world: _World) -> None:
    """ADR 0205: words said over the card get no "your card closed" note, as a question would."""
    _answer(world, ("17:05", "28"))
    said = event_log.emit_event(
        world.conn, type="utterance.received", payload={"transcript": "ok", "turn_id": "T-next"},
        correlation={"turn_id": "T-next"},
    )
    assert format_pending_clarification_note(assemble_packet(said, world.conn)) is None


def test_the_card_pins_the_chosen_option_through_the_tools_path(world: _World) -> None:
    """A click pins that option: event, ring and stop position as the tool writes them."""
    _answer(world, ("17:05", "28"), ("17:15", "28"))
    offer = _offer(world)
    reply = _take(world, "pin", offer_id=offer["id"], index=1)
    assert reply.status_code == 200
    (pinned,) = world.events("departure.pinned")
    (trip,) = pinned["trips"]
    assert (trip["route"], trip["board_stop"], trip["arrive_at"], trip["to"]) == (
        "28", "Shelbourne at Pear", "17:30", "mayfair mall",
    )
    assert (trip["stop_lat"], trip["stop_lng"]) == STOP
    assert trip["leave_at_ms"] == int(_local("17:12").timestamp() * 1000)
    assert (pinned["offer_id"], trip["tid"]) == (offer["id"], f"{offer['id']}-1")
    (ring,) = world.pending()
    assert ring.reminder_id == pinned["reminder_id"]
    assert ring.text == "该出门了，28 路 17:15 在 Shelbourne at Pear 上车"
    served = reply.json()["departure"]
    assert (served["id"], served["leave_at"], served["departs"]) == (
        pinned["pin_id"], "17:12", "17:15",
    )
    assert world.app().get("/inherent/notices").json()["departure"]["id"] == pinned["pin_id"]


def test_a_stale_offer_or_row_pins_nothing(world: _World) -> None:
    """Unknown id, unknown index, a past row and a replaced offer are all 404."""
    _answer(world, ("17:05", "28"), ("17:15", "28"))
    old = _offer(world)["id"]
    assert _take(world, "pin", offer_id="offer-nope", index=0).status_code == 404
    assert _take(world, "pin", offer_id=old, index=5).status_code == 404
    world.local_now = _local("17:10")  # row 0 has gone
    assert _take(world, "pin", offer_id=old, index=0).status_code == 404
    world.local_now = NOW
    _answer(world, ("17:20", "12"))  # a newer answer replaces the offer
    assert _offer(world)["id"] != old
    assert _take(world, "pin", offer_id=old, index=0).status_code == 404
    assert world.events("departure.pinned") == []
    assert world.pending() == []


def test_the_description_no_longer_asks_to_pin() -> None:
    """The card offers the pin; the model pins only when he asks by voice."""
    registry = build_default_registry(transit_api_key="not-a-key", transit_places={})
    descriptions = {d.name: d.description for d in registry.get_definitions()}
    assert "ask him" not in descriptions["transit"]
    assert "Do not ask whether to pin" in descriptions["transit"]
    assert "by voice" in descriptions["pin_departure"]
    assert "says yes" not in descriptions["pin_departure"]


def test_next_bus_takes_a_later_option_of_the_last_answer(world: _World) -> None:
    """The next bus of the same route and stop comes from the last answer when it has one."""
    _answer(world, ("17:05", "28"), ("17:15", "28"), ("17:30", "12"))
    world.pin()
    (before,) = world.pending()
    reply = _take(world, "next").json()
    assert reply["reason"] is None
    assert (reply["departure"]["leave_at"], reply["departure"]["departs"]) == ("17:12", "17:15")
    assert reply["departure"]["arrive_at"] == "17:30"
    (after,) = world.pending()
    assert after.reminder_id != before.reminder_id
    assert after.due_at_ms == int(_local("17:12").timestamp() * 1000)
    # Again: the 12 is another route, so there is no later 28.
    again = _take(world, "next").json()
    assert again["reason"] == "no_later"
    assert again["departure"]["departs"] == "17:15"


def test_next_bus_falls_back_to_the_live_feed(world: _World) -> None:
    """Without a later option the feed's next trip at the matched stop is pinned, times shifted."""
    world.pin()
    world.feed = _feed(
        ("T28a", "S1", "17:05", 0), ("T28b", "S1", "17:15", 0), ("T12a", "S1", "17:08", 0),
    )
    reply = _take(world, "next").json()
    assert reply["reason"] is None
    assert (reply["departure"]["leave_at"], reply["departure"]["departs"]) == ("17:12", "17:15")
    assert reply["departure"]["arrive_at"] == "17:30"
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:12").timestamp() * 1000)
    assert len(world.events("departure.pinned")) == 1
    assert world.events("departure.updated")[-1]["departs_at_ms"] == int(
        _local("17:15").timestamp() * 1000
    )
    world.feed = _feed(("T28a", "S1", "17:05", 0), ("T28b", "S1", "17:15", 0))
    assert _take(world, "next").json()["reason"] == "no_later"
    world.feed = b""
    world.feed_error = OSError("down")
    assert _take(world, "next").json()["reason"] == "no_later"
    assert len(world.events("departure.updated")) == 1


def test_cancel_then_undo_pins_the_same_trip_again(world: _World) -> None:
    """Undo re-pins the unpinned trip through the shared path, unless its leave time has passed."""
    made = world.pin()
    client = world.app()
    client.post(f"/inherent/notices/{made['pin_id']}", json={"action": "dismissed"})
    assert world.pending() == []
    assert _take(world, "undo", pin_id="departure-nope").status_code == 404
    reply = _take(world, "undo", pin_id=made["pin_id"]).json()
    assert reply["reason"] is None
    back = reply["departure"]
    assert back["id"] != made["pin_id"]
    assert (back["route"], back["leave_at"], back["departs"], back["arrive_at"], back["to"]) == (
        "28", "17:02", "17:05", "17:20", "home",
    )
    (pinned,) = world.events("departure.pinned")[-1:]
    assert (pinned["trips"][0]["stop_lat"], pinned["trips"][0]["stop_lng"]) == STOP
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:02").timestamp() * 1000)
    # It was used up; and a trip whose leave time has gone cannot come back.
    assert _take(world, "undo", pin_id=made["pin_id"]).status_code == 404
    again = world.app().post(f"/inherent/notices/{back['id']}", json={"action": "dismissed"})
    assert again.status_code == 200
    world.at("17:10")
    late = _take(world, "undo", pin_id=back["id"]).json()
    assert late["reason"] == "past"
    assert late["departure"] is None
    assert world.pending() == []


def test_the_served_pin_says_what_the_refresh_knows(world: _World) -> None:
    """delay_min and checked_at_ms follow the feed; an unchecked pin has no check time."""
    world.pin()
    client = world.app()
    first = client.get("/inherent/notices").json()["departure"]
    assert (first["delay_min"], first["checked_at_ms"]) == (0, None)
    _late(world)
    world.reminders.tick()
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    seen = client.get("/inherent/notices").json()["departure"]
    assert seen["delay_min"] == 3
    assert seen["checked_at_ms"] == int(world.clock.timestamp() * 1000)


# --- ADR 0204: a set of trips -----------------------------------------------------------


def _option(leave: str, departs: str, route: str = "28") -> dict[str, str]:
    return {
        "leave_at": leave, "route": route, "board_stop": "Shelbourne at Pear",
        "departs": departs, "arrive_at": "17:30", "destination": "Home",
    }


def _pin_two(world: _World) -> dict[str, Any]:
    """Two buses by voice: the 28 at 17:05 (leave 17:02) and at 17:15 (leave 17:12)."""
    options = [_option("17:12", "17:15"), _option("17:02", "17:05")]
    return world.call("pin_departure", {"options": options})


def _served(world: _World) -> dict[str, Any] | None:
    got = world.app().get("/inherent/notices").json()["departure"]
    return None if got is None else dict(got)


def test_a_card_click_adds_a_trip_and_a_second_click_takes_it_off(world: _World) -> None:
    """Rows are additive on one offer; the ring names the earliest and the other; a row toggles."""
    _answer(world, ("17:05", "28"), ("17:15", "28"), ("17:18", "12"))
    offer = _offer(world)
    first = _take(world, "add", offer_id=offer["id"], index=0).json()["departure"]
    assert (first["more"], len(first["trips"])) == (0, 1)
    both = _take(world, "add", offer_id=offer["id"], index=1).json()["departure"]
    assert both["id"] == first["id"]
    assert (both["leave_at"], both["departs"], both["more"]) == ("17:02", "17:05", 1)
    assert [(t["id"], t["departs"]) for t in both["trips"]] == [
        (f"{offer['id']}-0", "17:05"), (f"{offer['id']}-1", "17:15"),
    ]
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:02").timestamp() * 1000)
    assert ring.text == "该出门了，28 路 17:05 在 Shelbourne at Pear 上车；也可以坐 28 路 17:15"
    assert len(world.events("departure.pinned")) == 1
    # The same row again (a double click) adds nothing.
    again = _take(world, "add", offer_id=offer["id"], index=1).json()["departure"]
    assert len(again["trips"]) == 2
    # A click on a pinned row takes just that trip off, and the ring follows the one left.
    left = _take(world, "remove", trip_id=f"{offer['id']}-0").json()["departure"]
    assert (left["id"], left["departs"], left["more"]) == (first["id"], "17:15", 0)
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:12").timestamp() * 1000)
    assert ring.text == "该出门了，28 路 17:15 在 Shelbourne at Pear 上车"
    assert _take(world, "remove", trip_id="nope").status_code == 404


def test_a_newer_answer_starts_a_new_set(world: _World) -> None:
    """The first click on a new offer replaces the set; later clicks on it add to the new one."""
    _answer(world, ("17:05", "28"), ("17:15", "28"))
    old = _offer(world)
    first = _take(world, "add", offer_id=old["id"], index=0).json()["departure"]
    _answer(world, ("17:20", "12"), ("17:25", "12"))
    new = _offer(world)
    assert new["id"] != old["id"]
    started = _take(world, "add", offer_id=new["id"], index=1).json()["departure"]
    assert started["id"] != first["id"]
    assert [t["id"] for t in started["trips"]] == [f"{new['id']}-1"]
    assert len(world.pending()) == 1
    grown = _take(world, "add", offer_id=new["id"], index=0).json()["departure"]
    assert (grown["id"], len(grown["trips"])) == (started["id"], 2)
    # "pin" always starts a set with that row, even on the offer that started this one.
    fresh = _take(world, "pin", offer_id=new["id"], index=0).json()["departure"]
    assert (fresh["id"] != grown["id"], len(fresh["trips"])) == (True, 1)


def test_three_trips_is_the_most(world: _World) -> None:
    """A fourth catchable trip is refused with ToolError full; the card says so in `reason`."""
    world.call("pin_departure", {"options": [
        _option("17:02", "17:05"), _option("17:12", "17:15"), _option("17:22", "17:25"),
    ]})
    assert world.call("pin_departure", {"options": [_option("17:02", "17:05")] * 4}).get("error")
    one = departures_state.current(world.conn)
    assert one is not None
    extra = transit_tool.trip_of(
        tid="x", route="28", board_stop="Shelbourne at Pear", leave=_local("17:32"),
        departs=_local("17:35"), arrive_at="17:50", to="home", stop=STOP,
    )
    with pytest.raises(ToolError) as full:
        transit_tool.add_trip(world.conn, one, extra, action_id="A")
    assert full.value.code == "full"


def test_the_current_trip_is_the_earliest_catchable_and_the_ring_moves_on(world: _World) -> None:
    """Missed trips drop off, the ring moves to the next, and the pin ends with the last."""
    _pin_two(world)
    (ring,) = world.pending()
    assert ring.text == "该出门了，28 路 17:05 在 Shelbourne at Pear 上车；也可以坐 28 路 17:15"
    pin = _served(world)
    assert pin is not None
    assert (pin["leave_at"], pin["more"], [t["departs"] for t in pin["trips"]]) == (
        "17:02", 1, ["17:05", "17:15"],
    )
    world.at("17:02")
    world.clock += timedelta(seconds=5)
    assert world.reminders.tick() == 1
    assert world.said == [
        "提醒：该出门了，28 路 17:05 在 Shelbourne at Pear 上车；也可以坐 28 路 17:15",
    ]
    # Past its leave time but before the bus: still the current trip, and no second ring.
    world.at("17:04")
    world.reminders.tick()
    assert (_served(world) or {})["departs"] == "17:05"
    assert world.pending() == []
    # The bus has gone: the other trip is current, alone, and the ring is set for it.
    world.at("17:05")
    world.reminders.tick()
    pin = _served(world)
    assert pin is not None
    assert (pin["leave_at"], pin["departs"], pin["more"], len(pin["trips"])) == (
        "17:12", "17:15", 0, 1,
    )
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:12").timestamp() * 1000)
    assert ring.text == "该出门了，28 路 17:15 在 Shelbourne at Pear 上车"
    world.reminders.tick()
    assert len(world.pending()) == 1  # settled: nothing is rescheduled again
    world.at("17:12")
    world.clock += timedelta(seconds=5)
    assert world.reminders.tick() == 1
    world.at("17:15")
    world.reminders.tick()
    assert _served(world) is None


def test_every_trip_is_refreshed_on_its_own(world: _World) -> None:
    """Each trip keeps its matched bus and delay; the ring moves with the earliest trip's time."""
    _pin_two(world)
    world.feed = _feed(("T28a", "S1", "17:05", 180), ("T28b", "S1", "17:15", 120))
    world.reminders.tick()
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    assert world.feed_calls == 1  # one download for both trips
    moved = {e["tid"]: e["departs_at_ms"] for e in world.events("departure.updated")}
    assert sorted(moved.values()) == [
        int(_local("17:08").timestamp() * 1000), int(_local("17:17").timestamp() * 1000),
    ]
    pin = _served(world)
    assert pin is not None
    delays = [(t["departs"], t["delay_min"]) for t in pin["trips"]]
    assert delays == [("17:08", 3), ("17:17", 2)]
    assert pin["leave_at"] == "17:05"
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:05").timestamp() * 1000)
    assert ring.text == "该出门了，28 路 17:08 在 Shelbourne at Pear 上车；也可以坐 28 路 17:17"
    # A trip whose stop has no position is stale alone.
    world.call("pin_departure", {"options": [
        _option("17:02", "17:05"), {**_option("17:12", "17:15"), "board_stop": "Nowhere"},
    ]})
    world.reminders.tick()
    world.feed = _feed(("T28a", "S1", "17:05", 0))
    world.clock += timedelta(seconds=REFRESH_S)
    world.reminders.tick()
    pin = _served(world)
    assert pin is not None
    assert [t["stale"] for t in pin["trips"]] == [False, True]
    assert pin["stale"] is False


def test_undo_brings_back_what_the_last_removal_took(world: _World) -> None:
    """One trip removed comes back into the set; the whole set removed comes back as a set."""
    pin_id = _pin_two(world)["pin_id"]
    gone = [t["id"] for t in (_served(world) or {})["trips"]]
    client = world.app()
    left = _take(world, "remove", trip_id=gone[0]).json()["departure"]
    assert [t["id"] for t in left["trips"]] == [gone[1]]
    back = _take(world, "undo", pin_id=pin_id).json()
    assert back["reason"] is None
    assert [t["id"] for t in back["departure"]["trips"]] == gone
    assert back["departure"]["id"] == pin_id
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:02").timestamp() * 1000)
    # The whole set: x on the pill, then undo.
    reply = client.post(f"/inherent/notices/{pin_id}", json={"action": "dismissed"})
    assert reply.status_code == 200
    assert world.pending() == []
    set_back = _take(world, "undo", pin_id=pin_id).json()["departure"]
    assert set_back["id"] != pin_id
    assert [t["id"] for t in set_back["trips"]] == gone
    assert len(world.pending()) == 1
    # Removing the last trip of a set is removing the set.
    new_id = set_back["id"]
    _take(world, "remove", trip_id=gone[0])
    assert _take(world, "remove", trip_id=gone[1]).json()["departure"] is None
    assert world.pending() == []
    again = _take(world, "undo", pin_id=new_id).json()["departure"]
    assert [t["id"] for t in again["trips"]] == [gone[1]]


def test_an_old_single_trip_pin_still_folds(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A departure.pinned and departure.updated from before ADR 0204 are a set of one."""
    legacy = {
        "departure.pinned": (
            "pin_id", "reminder_id", "route", "board_stop", "leave_at_ms", "departs_at_ms",
            "arrive_at", "stop_lat", "stop_lng", "to", "action_id",
        ),
        "departure.updated": (
            "pin_id", "reminder_id", "leave_at_ms", "departs_at_ms", "arrive_at",
        ),
    }
    schemas = dict(event_log._REGISTRY_MAP)  # noqa: SLF001 - the old shapes are not registered.
    for kind, required in legacy.items():
        schemas[kind] = dataclasses.replace(schemas[kind], required_payload=required)
    monkeypatch.setattr(event_log, "_REGISTRY_MAP", MappingProxyType(schemas))
    ring = reminder_state.schedule(
        world.conn, due=_local("17:02"), action_id="old",
        text="该出门了，28 路 17:05 在 Shelbourne at Pear 上车",
    )
    event_log.emit_event(world.conn, type="departure.pinned", payload={
        "pin_id": "departure-old", "reminder_id": ring, "route": "28",
        "board_stop": "Shelbourne at Pear", "leave_at_ms": int(_local("17:02").timestamp() * 1000),
        "departs_at_ms": int(_local("17:05").timestamp() * 1000), "arrive_at": "17:20",
        "stop_lat": STOP[0], "stop_lng": STOP[1], "to": "home", "action_id": "old",
    })
    pin = _served(world)
    assert pin is not None
    assert (pin["id"], pin["route"], pin["departs"], pin["more"], len(pin["trips"])) == (
        "departure-old", "28", "17:05", 0, 1,
    )
    event_log.emit_event(world.conn, type="departure.updated", payload={
        "pin_id": "departure-old", "reminder_id": ring,
        "leave_at_ms": int(_local("17:03").timestamp() * 1000),
        "departs_at_ms": int(_local("17:06").timestamp() * 1000), "arrive_at": "17:21",
    })
    pin = _served(world)
    assert pin is not None
    assert (pin["leave_at"], pin["departs"], pin["arrive_at"]) == ("17:03", "17:06", "17:21")
    # It can be added to and refreshed like any set.
    world.look_up()
    world.reminders.tick()
    assert world.reminders.departures.view() is not None
    one = departures_state.current(world.conn)
    assert one is not None
    extra = transit_tool.trip_of(
        tid="n", route="28", board_stop="Shelbourne at Pear", leave=_local("17:12"),
        departs=_local("17:15"), arrive_at="17:30", to="home", stop=STOP,
    )
    transit_tool.add_trip(world.conn, one, extra, action_id="A")
    assert (_served(world) or {})["more"] == 1


def test_voice_can_pin_several_options_and_one_set_of_fields_still_works(world: _World) -> None:
    """`options` pins a set (one ring, all trips); the plain fields pin one trip as before."""
    made = _pin_two(world)
    assert made["pinned"]
    assert (made["leave_at"], made["departs"]) == ("17:02", "17:05")  # the earliest
    assert [t["departs"] for t in made["trips"]] == ["17:15", "17:05"]
    (pinned,) = world.events("departure.pinned")
    assert pinned["offer_id"] is None
    assert len(pinned["trips"]) == 2
    assert len(world.pending()) == 1
    single = world.pin(leave="17:10", departs="17:13")
    assert single["pinned"]
    assert len((_served(world) or {})["trips"]) == 1
    (ring,) = world.pending()
    assert ring.due_at_ms == int(_local("17:10").timestamp() * 1000)
    # One bad option refuses the lot.
    options = [_option("17:12", "17:15"), _option("16:00", "17:05")]
    bad = world.call("pin_departure", {"options": options})
    assert bad["code"] == "time_in_past"
    assert (_served(world) or {})["departs"] == "17:13"
