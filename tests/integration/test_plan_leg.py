"""ADR 0288: the bus planner plans any day and an arrive-by time, and a background job asks for it.

The requests are served by a fake transport that records each body; no network, no key.
"""

from __future__ import annotations

import io
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import maps_spend, places_api, transit_tool
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.state import places
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projections import CLARIFICATION_EVENT_TYPES
from tests.support.clock import FakeClock
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.tools import ToolContext

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 6, 17, 0, tzinfo=ZONE)
KEY = "test-key-not-real"
SAVED = {"home": "48.48,-123.38", "school": "University of Victoria, Victoria, BC"}
GYM = places.Place(
    "p1", "Gym", "other", 48.5, -123.4, 150, None, "user", "2026-10-01", "2026-10-01",
)
CAFE = places.Place(
    "p2", "Corner Cafe", "other", None, None, 100, "1 Main St, Victoria, BC", "user",
    "2026-10-01", "2026-10-01",
)
MODES = {
    "DRIVE": {"routes": [{"duration": "600s", "distanceMeters": 5000}]},
    "WALK": {"routes": [{"duration": "1800s", "distanceMeters": 2500}]},
}


def _bus(line: str, day: str, departs: str, *, walk: int = 4, ride: int = 20) -> dict[str, Any]:
    """One route: a walk, then bus ``line`` leaving at local ``departs`` on ``day``."""
    at = datetime.fromisoformat(f"{day}T{departs}").replace(tzinfo=ZONE)

    def utc(when: datetime) -> str:
        return when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "legs": [
            {
                "steps": [
                    {"staticDuration": f"{walk * 60}s", "travelMode": "WALK"},
                    {
                        "staticDuration": f"{ride * 60}s",
                        "travelMode": "TRANSIT",
                        "transitDetails": {
                            "stopDetails": {
                                "departureStop": {"name": f"Stop of {line}"},
                                "departureTime": utc(at),
                                "arrivalStop": {"name": "Exchange"},
                                "arrivalTime": utc(at + timedelta(minutes=ride)),
                            },
                            "headsign": "Out",
                            "transitLine": {"nameShort": line},
                        },
                    },
                ]
            }
        ]
    }


@dataclass
class World:
    """The fake Routes transport, the event log and the spend cap."""

    log: Any
    bodies: list[dict[str, Any]]
    urls: list[str]
    routes: list[dict[str, Any]]

    def transit(self) -> list[dict[str, Any]]:
        """The TRANSIT request bodies, in order."""
        return [b for b in self.bodies if b["travelMode"] == "TRANSIT"]

    def ctx(self) -> ToolContext:
        """The context a job hands the planner."""
        return tool_context(self.log, "plan-leg", clock=FakeClock(NOW))

    def leg_planner(self, cap: float = 10.0) -> Any:  # noqa: ANN401
        """``plan_leg`` over the kept Gym and Corner Cafe."""
        plan = transit_tool.build_leg_planner(
            KEY, lambda: SAVED, self.ctx(), kept=lambda: [GYM, CAFE], cap_usd=cap,
        )
        assert plan is not None
        return plan

    def spend(self) -> list[tuple[str, int]]:
        """The ``(SKU, requests)`` rows recorded."""
        rows = self.log.execute(
            "SELECT json_extract(payload_json, '$.sku'), json_extract(payload_json, '$.requests')"
            " FROM events WHERE type = 'maps.spend_recorded' ORDER BY id",
        ).fetchall()
        return [(sku, n) for sku, n in rows]


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routes: list[dict[str, Any]],
) -> World:
    world = World(open_event_log(tmp_path / "events.db"), [], [], routes)

    def urlopen(request: urllib.request.Request, **_: object) -> io.BytesIO:
        world.urls.append(request.full_url)
        sent = json.loads(request.data)  # type: ignore[arg-type]
        world.bodies.append(sent)
        travel = sent["travelMode"]
        reply = {"routes": world.routes} if travel == "TRANSIT" else MODES[travel]
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)
    return world


MORNING = "2026-10-08"
BUSES = [_bus("26", MORNING, "08:00"), _bus("28", MORNING, "08:20"), _bus("39", MORNING, "08:40")]


def test_arrive_by_sends_arrival_time_alone_and_picks_the_latest_bus_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arrival time goes in UTC, no departure time; DRIVE/WALK carry none; chosen, backup."""
    world = _world(tmp_path, monkeypatch, BUSES)
    out = world.leg_planner()("home", "school", day=MORNING, arrive_by="08:55")
    (body,) = world.transit()
    assert body["arrivalTime"] == "2026-10-08T15:55:00Z"
    assert "departureTime" not in body
    assert len(world.bodies) == 3
    for other in (b for b in world.bodies if b["travelMode"] != "TRANSIT"):
        assert not {"arrivalTime", "departureTime"} & set(other)
    assert [o["legs"][0]["route"] for o in out["options"]] == ["26", "28", "39"]
    # The 39 arrives 09:00, after 08:55: the 28 (arrives 08:40) is the latest in time.
    assert out["chosen"]["legs"][0]["route"] == "28"
    assert out["backup"]["legs"][0]["route"] == "26"
    assert out["day"] == MORNING
    assert out["modes"] == {"drive": {"minutes": 10, "km": 5.0}, "walk": {"minutes": 30, "km": 2.5}}


def test_a_departure_on_another_day_is_sent_on_that_day_and_the_next_bus_is_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """depart_at on day: departureTime on that day; chosen is the first, backup the one after."""
    # The far-fetched cut (ADR 0203) still applies: a bus 15+ minutes later is not offered.
    buses = [_bus("26", MORNING, "08:10"), _bus("28", MORNING, "08:20")]
    world = _world(tmp_path, monkeypatch, buses)
    out = world.leg_planner()("home", "school", day=MORNING, depart_at="08:00")
    (body,) = world.transit()
    assert body["departureTime"] == "2026-10-08T15:00:00Z"
    assert "arrivalTime" not in body
    assert out["chosen"]["legs"][0]["departs"] == "08:10"
    assert out["backup"]["legs"][0]["departs"] == "08:20"
    assert {"leave_at", "arrive_at", "spare_min", "verdict", "legs"} <= set(out["chosen"])


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("gym", {"location": {"latLng": {"latitude": 48.5, "longitude": -123.4}}}),
        ("  GYM ", {"location": {"latLng": {"latitude": 48.5, "longitude": -123.4}}}),
        ("Corner Cafe", {"address": "1 Main St, Victoria, BC"}),
        ("Mayfair Mall", {"address": "Mayfair Mall"}),
        ("home", {"location": {"latLng": {"latitude": 48.48, "longitude": -123.38}}}),
    ],
)
def test_a_kept_place_is_found_by_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str, expected: dict[str, Any],
) -> None:
    """Name (any case) of a kept place gives its point, else its address; others are text."""
    world = _world(tmp_path, monkeypatch, BUSES)
    world.leg_planner()("school", place, day=MORNING, arrive_by="09:30")
    assert world.transit()[0]["destination"] == expected


def test_the_background_call_keeps_no_card_and_no_offer_but_records_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No clarification card, no Place Details request; the three Routes requests are billed."""
    world = _world(tmp_path, monkeypatch, [_bus("26", "2026-10-06", "17:30")])
    world.leg_planner()("home", "school", day="2026-10-06", depart_at="17:20")
    assert list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES)) == []
    assert not list(iter_events_of_types(world.log, (places_api.EVENT_TYPE,)))
    assert set(world.urls) == {"https://routes.googleapis.com/directions/v2:computeRoutes"}
    assert sorted(world.spend()) == [("routes.basic.essentials", 2), ("routes.transit.pro", 1)]


def test_the_chat_call_on_today_still_shows_its_card_and_another_day_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bus` with arrive_by today keeps the card; with day=<later> no card and no pin list."""
    world = _world(tmp_path, monkeypatch, [_bus("26", "2026-10-06", "17:30")])
    registry = build_default_registry(
        transit_api_key=KEY, transit_places=lambda: SAVED, clock=FakeClock(NOW),
    )
    (bus,) = (d for d in registry.get_definitions() if d.name == "bus")

    def chat(args: dict[str, str]) -> dict[str, Any]:
        return dict(bus.handler(args, world.ctx()))

    out = chat({"to": "school", "from": "home", "arrive_by": "18:30"})
    assert len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES))) == 1
    assert out["arrive_by"] == "18:30"
    assert "arrive" in out["summary"]
    assert world.transit()[0]["arrivalTime"] == "2026-10-07T01:30:00Z"
    world.bodies.clear()
    world.routes = BUSES
    before = len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES)))
    out = chat({"to": "school", "from": "home", "day": MORNING, "at": "08:10"})
    assert len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES))) == before
    assert out["day"] == MORNING
    assert "Thu Oct 8" in out["summary"]
    assert world.transit()[0]["departureTime"] == "2026-10-08T15:10:00Z"


def test_the_cap_stops_the_background_call_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9.5 of a 10 month: ToolError spend_cap, no request made."""
    world = _world(tmp_path, monkeypatch, BUSES)
    maps_spend.PRICE_USD["test.spend"] = 9.5
    try:
        maps_spend.record(world.ctx(), ["test.spend"])
    finally:
        maps_spend.PRICE_USD.pop("test.spend", None)
    with pytest.raises(ToolError) as caught:
        world.leg_planner(cap=10.0)("home", "school", day=MORNING, arrive_by="09:00")
    assert caught.value.code == "spend_cap"
    assert world.bodies == []


@pytest.mark.parametrize(
    "ask",
    [
        {"day": "2026-10-06", "arrive_by": "16:00"},  # today, already passed
        {"day": "2026-10-05", "depart_at": "09:00"},  # a day gone
        {"day": "2026-10-06", "depart_at": "16:00"},  # a departure already gone, day given
        {"day": "2026-11-06", "arrive_by": "09:00"},  # 31 days ahead
        {"day": "next thursday", "arrive_by": "09:00"},  # not an ISO date
        {"day": MORNING, "arrive_by": "09:00", "depart_at": "08:00"},  # both times
        {"day": MORNING},  # another day with no time
        {"day": MORNING, "arrive_by": "9 o'clock"},  # not HH:MM
    ],
)
def test_a_time_gone_a_far_day_or_a_muddled_ask_is_a_clear_error_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ask: dict[str, str],
) -> None:
    """Each is ToolError invalid_arguments, raised before Google is asked."""
    world = _world(tmp_path, monkeypatch, BUSES)
    plan = transit_tool.build_planner(
        KEY, lambda: SAVED, offers=transit_tool.TransitOffers(), bus_live=None, quiet=True,
    )
    assert plan is not None
    with pytest.raises(ToolError) as caught:
        plan({"origin": "home", "destination": "school", **ask}, world.ctx())
    assert caught.value.code == "invalid_arguments"
    assert world.bodies == []


def test_the_thirtieth_day_is_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge of the window: 30 days ahead plans."""
    day = (date(2026, 10, 6) + timedelta(days=30)).isoformat()
    world = _world(tmp_path, monkeypatch, [_bus("26", day, "08:00")])
    out = world.leg_planner()("home", "school", day=day, arrive_by="09:00")
    assert out["chosen"]["legs"][0]["route"] == "26"
