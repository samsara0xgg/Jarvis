"""ADR 0288, 0343: the bus planner plans any day and an arrive-by time, for a background job too.

The requests are served by a fake transport that records each body, and the place reader's Text
Search by the shared fake (ADR 0346); no network, no key.
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
from tests.support.bus_world import TEXT_SEARCH_URL, google_place, serve_text_search
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
# What Text Search finds for each query: the school's address, the cafe's kept address, a
# business that shares the cafe's name elsewhere, and a mall.
FOUND = {
    SAVED["school"]: google_place(
        "University of Victoria", "3800 Finnerty Rd, Victoria, BC", 48.4634, -123.3117,
        "ChIJschool000000000000",
    ),
    "1 Main St, Victoria, BC": google_place(
        "1 Main St", "1 Main St, Victoria, BC", 48.43, -123.36, "ChIJmainst000000000000",
    ),
    "Corner Cafe": google_place(
        "Corner Cafe", "99 Other Rd, Langford, BC", 48.45, -123.5, "ChIJothercafe000000000",
    ),
    "Mayfair Mall": google_place(
        "Mayfair Shopping Centre", "3147 Douglas St, Victoria, BC", 48.4467, -123.3713,
        "ChIJmayfair0000000000",
    ),
}
MODES = {
    "DRIVE": {"routes": [{"duration": "600s", "distanceMeters": 5000}]},
    "WALK": {"routes": [{"duration": "1800s", "distanceMeters": 2500}]},
}


def _point(lat: float, lng: float) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


SCHOOL_POINT = _point(48.4634, -123.3117)


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
    """The fake Routes transport, the Text Searches, the event log and the spend cap."""

    log: Any
    bodies: list[dict[str, Any]]
    urls: list[str]
    routes: list[dict[str, Any]]
    searches: list[urllib.request.Request]

    def transit(self) -> list[dict[str, Any]]:
        """The TRANSIT request bodies, in order."""
        return [b for b in self.bodies if b["travelMode"] == "TRANSIT"]

    def queries(self) -> list[str]:
        """The words each Text Search asked for, in order."""
        return [json.loads(r.data)["textQuery"] for r in self.searches]  # type: ignore[arg-type]

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
    world = World(open_event_log(tmp_path / "events.db"), [], [], routes, [])

    def urlopen(request: urllib.request.Request, **_: object) -> io.BytesIO:
        world.urls.append(request.full_url)
        sent = json.loads(request.data)  # type: ignore[arg-type]
        world.bodies.append(sent)
        travel = sent["travelMode"]
        reply = {"routes": world.routes} if travel == "TRANSIT" else MODES[travel]
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    world.searches = serve_text_search(monkeypatch, FOUND)
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)
    return world


MORNING = "2026-10-08"
BUSES = [_bus("26", MORNING, "08:00"), _bus("28", MORNING, "08:20"), _bus("39", MORNING, "08:40")]


def test_arrive_by_sends_arrival_time_alone_and_picks_the_latest_bus_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arrival time goes in UTC, no departure time; DRIVE/WALK carry none; chosen, backup."""
    world = _world(tmp_path, monkeypatch, BUSES)
    out = world.leg_planner()("home", "school", arrive_by=f"{MORNING} 08:55")
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
    assert out["arrive_by"] == "Thu Oct 8, 8:55 am"
    assert out["modes"] == {"drive": {"minutes": 10, "km": 5.0}, "walk": {"minutes": 30, "km": 2.5}}


def test_a_departure_on_another_day_is_sent_on_that_day_and_the_next_bus_is_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """depart_at on a date: departureTime on that day; chosen is the first, backup the one after."""
    buses = [_bus("26", MORNING, "08:10"), _bus("28", MORNING, "08:20")]
    world = _world(tmp_path, monkeypatch, buses)
    out = world.leg_planner()("home", "school", depart_at=f"{MORNING} 08:00")
    (body,) = world.transit()
    assert body["departureTime"] == "2026-10-08T15:00:00Z"
    assert "arrivalTime" not in body
    assert out["at"] == "Thu Oct 8, 8:00 am"
    assert out["chosen"]["legs"][0]["departs"] == "08:10"
    assert out["backup"]["legs"][0]["departs"] == "08:20"
    assert {"leave_at", "leave_ms", "arrive_at", "arrive_ms", "spare_min", "verdict", "legs"} <= (
        set(out["chosen"])
    )
    leave = datetime(2026, 10, 8, 8, 6, tzinfo=ZONE)
    assert out["chosen"]["leave_ms"] == int(leave.timestamp() * 1000)


@pytest.mark.parametrize(
    ("depart_at", "sent"),
    [
        ("17:30", "2026-10-07T00:30:00Z"),  # later today
        ("16:30", "2026-10-06T23:30:00Z"),  # half an hour gone: still today's, sent as it is
        ("15:30", "2026-10-07T22:30:00Z"),  # more than an hour gone: tomorrow's
    ],
)
def test_a_clock_time_alone_is_today_until_it_is_an_hour_gone_then_tomorrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depart_at: str, sent: str,
) -> None:
    """ADR 0343: 'HH:MM' alone is today's, or tomorrow's once today's is over an hour past."""
    world = _world(tmp_path, monkeypatch, BUSES)
    world.leg_planner()("home", "gym", depart_at=depart_at)
    assert world.transit()[0]["departureTime"] == sent


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("gym", _point(48.5, -123.4)),
        ("  GYM ", _point(48.5, -123.4)),
        ("Corner Cafe", _point(48.43, -123.36)),  # its kept address, not a namesake elsewhere
        ("Mayfair Mall", _point(48.4467, -123.3713)),
        ("home", _point(48.48, -123.38)),
    ],
)
def test_a_kept_place_is_found_by_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: str, expected: dict[str, Any],
) -> None:
    """Name (any case) of a kept place gives its point, else its address; others are searched."""
    world = _world(tmp_path, monkeypatch, BUSES)
    world.leg_planner()("school", place, arrive_by=f"{MORNING} 09:30")
    assert world.transit()[0]["destination"] == expected
    assert world.transit()[0]["origin"] == SCHOOL_POINT


def test_the_background_call_keeps_no_card_and_no_offer_but_records_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No card, no map announcement; the school's Text Search and three Routes requests billed."""
    world = _world(tmp_path, monkeypatch, [_bus("26", "2026-10-06", "17:30")])
    world.leg_planner()("home", "school", depart_at="2026-10-06 17:20")
    assert list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES)) == []
    assert not list(iter_events_of_types(world.log, (places_api.EVENT_TYPE,)))
    assert set(world.urls) == {"https://routes.googleapis.com/directions/v2:computeRoutes"}
    assert [r.full_url for r in world.searches] == [TEXT_SEARCH_URL]
    assert sorted(world.spend()) == [
        ("places.text_search.pro", 1), ("routes.basic.essentials", 2), ("routes.transit.pro", 1),
    ]


def test_the_chat_call_on_today_still_shows_its_card_and_another_day_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bus` with arrive_by today keeps the card; with at=<a later date> no card and no pin list."""
    world = _world(tmp_path, monkeypatch, [_bus("26", "2026-10-06", "17:30")])
    registry = build_default_registry(
        transit_api_key=KEY, transit_places=lambda: SAVED, clock=FakeClock(NOW),
    )
    (bus,) = (d for d in registry.get_definitions() if d.name == "bus")

    def chat(args: dict[str, str]) -> dict[str, Any]:
        return dict(bus.handler(args, world.ctx()))

    out = chat({"to": "school", "from": "home", "arrive_by": "18:30"})
    assert len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES))) == 1
    assert out["arrive_by"] == "6:30 pm"
    assert "arrive" in out["summary"]
    assert world.transit()[0]["arrivalTime"] == "2026-10-07T01:30:00Z"
    world.bodies.clear()
    world.routes = BUSES
    before = len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES)))
    out = chat({"to": "school", "from": "home", "at": f"{MORNING} 08:10"})
    assert len(list(iter_events_of_types(world.log, CLARIFICATION_EVENT_TYPES))) == before
    assert out["at"] == "Thu Oct 8, 8:10 am"
    assert "Thu Oct 8" in out["summary"]
    assert world.transit()[0]["departureTime"] == "2026-10-08T15:10:00Z"
    with pytest.raises(ToolError, match="day is at=, which takes a local date and time") as e:
        chat({"to": "school", "day": MORNING, "at": "08:10"})
    assert e.value.code == "invalid_arguments"


def test_the_cap_stops_the_background_call_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9.5 billed of a 10 month: ToolError spend_cap, no Routes or Places request made."""
    world = _world(tmp_path, monkeypatch, BUSES)
    monkeypatch.setitem(maps_spend.SKUS, "test.spend", maps_spend.Sku(9.5, 0))
    maps_spend.record(world.ctx(), ["test.spend"])
    with pytest.raises(ToolError) as caught:
        world.leg_planner(cap=10.0)("home", "school", arrive_by=f"{MORNING} 09:00")
    assert caught.value.code == "spend_cap"
    assert world.bodies == []
    assert world.searches == []


@pytest.mark.parametrize(
    "ask",
    [
        {"arrive_by": "2026-10-06 16:00"},  # today, already passed
        {"arrive_by": "16:00"},  # a clock time alone, today's, already passed
        {"depart_at": "2026-10-05 09:00"},  # a day gone
        {"depart_at": "2026-10-06 16:00"},  # a departure already gone, its date given
        {"arrive_by": "2026-11-06 09:00"},  # 31 days ahead
        {"arrive_by": "next thursday 09:00"},  # not a date
        {"arrive_by": f"{MORNING} 09:00", "depart_at": f"{MORNING} 08:00"},  # both times
        {"arrive_by": f"{MORNING} 9 o'clock"},  # not HH:MM
    ],
)
def test_a_time_gone_a_far_day_or_a_muddled_ask_is_a_clear_error_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ask: dict[str, str],
) -> None:
    """Each is ToolError invalid_arguments, raised before Google Routes is asked."""
    world = _world(tmp_path, monkeypatch, BUSES)
    plan = transit_tool.build_planner(
        KEY, lambda: SAVED, offers=transit_tool.TransitOffers(), bus_live=None,
        kept=lambda: [GYM], quiet=True,
    )
    assert plan is not None
    with pytest.raises(ToolError) as caught:
        plan({"origin": "home", "destination": "gym", **ask}, world.ctx())
    assert caught.value.code == "invalid_arguments"
    assert world.bodies == []


def test_a_bad_time_asks_places_nothing_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A time that cannot be planned is refused before the destination's words are searched."""
    world = _world(tmp_path, monkeypatch, BUSES)
    with pytest.raises(ToolError) as caught:
        world.leg_planner()("home", "Mayfair Mall", arrive_by="next thursday 09:00")
    assert caught.value.code == "invalid_arguments"
    assert world.searches == []
    assert world.bodies == []


def test_plan_leg_takes_exactly_one_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither or both of arrive_by and depart_at: invalid_arguments, nothing asked."""
    world = _world(tmp_path, monkeypatch, BUSES)
    for times in ({}, {"arrive_by": f"{MORNING} 09:00", "depart_at": f"{MORNING} 08:00"}):
        with pytest.raises(ToolError) as caught:
            world.leg_planner()("home", "gym", **times)
        assert caught.value.code == "invalid_arguments"
    assert world.bodies == []


def test_the_thirtieth_day_is_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge of the window: 30 days ahead plans."""
    day = (date(2026, 10, 6) + timedelta(days=30)).isoformat()
    world = _world(tmp_path, monkeypatch, [_bus("26", day, "08:00")])
    out = world.leg_planner()("home", "school", arrive_by=f"{day} 09:00")
    assert out["chosen"]["legs"][0]["route"] == "26"
