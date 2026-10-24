"""ADR 0283: `map_search`, the bus destination Google resolved, the monthly cap, places on answers.

Every Google request is a stub: `urllib.request.urlopen` is replaced for the whole module, so a
request to any host the stub does not know fails the test instead of going out.

- `map_search` asks Text Search near the phone's last report by default, Nearby Search for a bare
  type, Text Search "in <area>" for a named area and Place Details for a place_id; the field masks
  are the Enterprise (search) and Enterprise + Atmosphere (details) ones and each answer is shaped
  with distance, direction, today's hours and the rest; vague-name behaviour is the description's;
- Google's `SERVICE_DISABLED` 403 is a plain "Places API (New) is not enabled" reason;
- `bus` says the name and address Google resolved, takes a `map_search` place_id and says the walk;
- every request is priced and recorded, the month's sum stops paid calls near 90% of the cap, Routes
  requests count, and the sum survives reopening the log;
- the places a turn's tools returned ride on its `surface.response_emitted`.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import maps_spend, places_api, transit_tool
from jarvis.execution.tools import (
    ToolError,
    build_default_registry,
    register_live_action,
    release_turn_actions,
)
from jarvis.runtime.turn import start as start_module
from jarvis.state.event_log import open_event_log
from jarvis.state.phone_location import NoPhoneReport
from jarvis.surface.cli import emit_surface_user_intent
from tests.support.clock import FakeClock
from tests.support.here import FixedHere, reading
from tests.support.routine import drive_streaming_turn, logged_payloads
from tests.support.runtime_config import runtime_config
from tests.support.spoken import SpokenPeer, spoken_runtime
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

FAKE_KEY = "test-key-not-real"
NEVER = FixedHere(error=NoPhoneReport("no phone has reported a location"))
ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 12, 17, 0, tzinfo=ZONE)  # a Monday
CLOCK = FakeClock(NOW)
# The phone's last report, five minutes old, and a place 0.003 degrees (about 334 m) north of it.
PHONE = reading(
    48.4284, -123.3656, 30.0, age_s=300, device="iphone", place="Government St",
)
PHONE_HERE = FixedHere(PHONE)
WEEK = [
    f"{day}: 9:00 AM - 5:00 PM"
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
]


def _place(n: int, *, lat_off: float = 0.003, with_extras: bool = True) -> dict[str, Any]:
    """Place."""
    raw: dict[str, Any] = {
        "id": f"ChIJplace{n}",
        "displayName": {"text": f"Ramen Shop {n}", "languageCode": "en"},
        "formattedAddress": f"{n} Fort St, Victoria, BC V8W 1E5, Canada",
        "location": {"latitude": PHONE.lat + lat_off * n, "longitude": PHONE.lng},
        "googleMapsUri": f"https://maps.google.com/?cid={n}",
    }
    if with_extras:
        raw |= {
            "rating": 4.5, "userRatingCount": 312, "priceLevel": "PRICE_LEVEL_MODERATE",
            "nationalPhoneNumber": "(250) 555-0100",
            "currentOpeningHours": {
                "openNow": True, "weekdayDescriptions": WEEK,
                "nextCloseTime": "2026-10-13T00:00:00Z",  # 5:00 pm on the Monday, Pacific
            },
            "businessStatus": "OPERATIONAL",
        }
    return raw


DETAILS = {
    **_place(1),
    "websiteUri": "https://ramen.example",
    "reviews": [
        {"rating": 5, "text": {"text": "Best broth in town. " * 20},
         "relativePublishTimeDescription": "a week ago"},
        {"rating": 4, "text": {"text": "Good, slow."},
         "relativePublishTimeDescription": "a month ago"},
        {"rating": 3, "text": {"text": "Fine."}, "relativePublishTimeDescription": "2 months ago"},
        {"rating": 1, "text": {"text": "Fourth, cut."},
         "relativePublishTimeDescription": "a year ago"},
    ],
}
DESTINATION = {"latitude": 48.4300, "longitude": -123.3700}


class Google:
    """Stands in for every Google host; what was asked is in ``seen``."""

    def __init__(self) -> None:
        """Start with an answer for each host."""
        self.seen: list[urllib.request.Request] = []
        self.text: Any = {"places": [_place(1), _place(2)]}
        self.nearby: Any = {"places": [_place(1)]}
        self.details: Any = DETAILS
        self.named: Any = {
            "id": "ChIJdest", "displayName": {"text": "Starbucks"},
            "formattedAddress": "1234 Douglas St, Victoria, BC V8W 2E7, Canada",
            "location": DESTINATION,
        }
        self.routes: dict[str, Any] = {}
        self.text_replies: list[Any] = []
        """Answers to the next Text Searches, in order, before ``text`` answers the rest."""
        self.matrix_error: Exception | None = None

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> io.BytesIO:  # noqa: ARG002
        """Answer one request."""
        self.seen.append(request)
        url = request.full_url
        if url == "https://places.googleapis.com/v1/places:searchText":
            reply = self.text_replies.pop(0) if self.text_replies else self.text
        elif url == "https://places.googleapis.com/v1/places:searchNearby":
            reply = self.nearby
        elif url.startswith("https://places.googleapis.com/v1/places/"):
            wide = "reviews" in (request.get_header("X-goog-fieldmask") or "")
            reply = self.details if wide else self.named
        elif url == "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix":
            if self.matrix_error:
                raise self.matrix_error
            sent = _body(request)["destinations"]
            reply = [
                {"originIndex": 0, "destinationIndex": n, "duration": f"{(n + 1) * 250}s",
                 "condition": "ROUTE_EXISTS"}
                for n in range(len(sent))
            ]
        elif url == "https://routes.googleapis.com/directions/v2:computeRoutes":
            travel = _body(request)["travelMode"]
            reply = self.routes.get(travel, {})
        else:
            msg = f"a request to a host the test does not stub: {url}"
            raise AssertionError(msg)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())

    def to(self, host: str) -> list[urllib.request.Request]:
        """The requests made to ``host`` (a prefix of the URL)."""
        return [r for r in self.seen if r.full_url.startswith(host)]


def _body(request: urllib.request.Request) -> dict[str, Any]:
    """The JSON a request carried."""
    assert isinstance(request.data, bytes)
    body: dict[str, Any] = json.loads(request.data)
    return body


def _mask(request: urllib.request.Request) -> list[str]:
    """The field mask a request asked for, one path each."""
    return (request.get_header("X-goog-fieldmask") or "").split(",")


PLACES_HOST = "https://places.googleapis.com/"
ROUTES_HOST = "https://routes.googleapis.com/"


def _refused(code: int, status: str, reason: str) -> urllib.error.HTTPError:
    """Refused."""
    body = json.dumps({"error": {"code": code, "status": status,
                                 "details": [{"reason": reason}]}}).encode()
    url = "https://places.googleapis.com/"
    return urllib.error.HTTPError(url, code, status, {}, io.BytesIO(body))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    """Google."""
    stub = Google()
    monkeypatch.setattr(urllib.request, "urlopen", stub)
    return stub


@pytest.fixture
def log(tmp_path: Path) -> sqlite3.Connection:
    """Log."""
    return open_event_log(tmp_path / "events.db")


def _answer_with_end(*, place_id: str | None = "ChIJdest") -> dict[str, Any]:
    """Google's hand-built bus answer: a 3 min walk, one bus in 10 min, ending at DESTINATION."""
    at = (NOW + timedelta(minutes=10)).astimezone(ZoneInfo("UTC"))

    def stamp(moment: datetime) -> str:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    leg = {
        "steps": [
            {"staticDuration": "180s", "travelMode": "WALK"},
            {
                "staticDuration": "900s", "travelMode": "TRANSIT",
                "transitDetails": {
                    "stopDetails": {
                        "departureStop": {
                            "name": "Fort at Douglas",
                            "location": {"latLng": {"latitude": 48.4290, "longitude": -123.3660}},
                        },
                        "departureTime": stamp(at),
                        "arrivalStop": {"name": "Douglas at Yates"},
                        "arrivalTime": stamp(at + timedelta(minutes=15)),
                    },
                    "headsign": "Downtown",
                    "transitLine": {"nameShort": "6", "name": "Royal Oak"},
                },
            },
        ],
        "endLocation": {"latLng": DESTINATION},
    }
    out: dict[str, Any] = {"routes": [{"legs": [leg]}]}
    if place_id:
        out["geocodingResults"] = {"destination": {"placeId": place_id}}
    return out


def _tools(
    log: sqlite3.Connection,
    *,
    cap: float = 10.0,
    phone: FixedHere | None = PHONE_HERE,
) -> Callable[..., Any]:
    """A registry as the brain builds it; ``call(tool, args, turn=...)`` runs one handler."""
    registry = build_default_registry(
        transit_api_key=FAKE_KEY,
        transit_places=lambda: {"home": "48.48,-123.38"},
        clock=CLOCK,
        here=phone,
        maps_monthly_usd=cap,
    )
    handlers = {d.name: d.handler for d in registry.get_definitions()}
    count = iter(range(1, 1000))

    def call(tool: str, args: dict[str, Any], turn: str | None = None) -> Any:  # noqa: ANN401
        """Call."""
        action_id = f"A{next(count)}"
        if turn:
            register_live_action(turn_id=turn, action_id=action_id)
        try:
            ctx = tool_context(
                log, action_id, clock=CLOCK, here=None if phone is None else phone.bind(turn),
            )
            return handlers[tool](args, ctx)
        finally:
            if turn:
                release_turn_actions(turn)

    return call


@pytest.fixture(autouse=True)
def _no_card_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct call has no `action.running` event to link the bus card to."""
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)


def _costs(log: sqlite3.Connection) -> list[tuple[str, float]]:
    """The (SKU, USD) rows the calls recorded, oldest first."""
    rows = log.execute(
        "SELECT json_extract(payload_json, '$.sku'), json_extract(payload_json, '$.cost_usd') "
        "FROM events WHERE type = 'maps.spend_recorded' ORDER BY id",
    ).fetchall()
    return [(m, c) for m, c in rows]


def _spend(log: sqlite3.Connection, usd: float) -> None:
    """Earlier map spend in the log: one row."""
    maps_spend.SKUS["test.spend"] = maps_spend.Sku(usd, 0)
    maps_spend.record(tool_context(log, "A-seed", clock=CLOCK), ["test.spend"])


@pytest.fixture(autouse=True)
def _clean_prices() -> Any:  # noqa: ANN401 - fixture
    """Clean prices."""
    yield
    maps_spend.SKUS.pop("test.spend", None)


# --- map_search ----------------------------------------------------------------------------


def _box(request: urllib.request.Request) -> dict[str, Any]:
    """The rectangle a request restricted its search to."""
    box: dict[str, Any] = _body(request)["locationRestriction"]["rectangle"]
    return box


SEARCH_MASK = [
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.googleMapsUri", "places.rating", "places.userRatingCount", "places.priceLevel",
    "places.nationalPhoneNumber", "places.currentOpeningHours.openNow",
    "places.currentOpeningHours.weekdayDescriptions", "places.businessStatus",
    "places.currentOpeningHours.nextOpenTime", "places.currentOpeningHours.nextCloseTime",
]


def test_near_him_is_a_text_search_in_the_box_round_his_radius(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Headers, body, field mask; each place with distance, walking time, open state, the rest."""
    out = _tools(log)("map_search", {"query": "ramen"})

    [request, matrix] = google.seen
    assert request.get_method() == "POST"
    assert request.full_url == "https://places.googleapis.com/v1/places:searchText"
    assert request.get_header("X-goog-api-key") == FAKE_KEY
    assert "key=" not in request.full_url, "the key goes in a header, not the URL"
    assert _mask(request) == SEARCH_MASK
    body = _body(request)
    assert body["textQuery"] == "ramen"
    assert body["maxResultCount"] == 20, "the box has corners the circle drops"
    assert "locationBias" not in body
    box = _box(request)
    assert box["low"]["latitude"] == pytest.approx(PHONE.lat - 2000 / 111_320, abs=1e-4)
    assert box["high"]["latitude"] == pytest.approx(PHONE.lat + 2000 / 111_320, abs=1e-4)
    assert box["low"]["longitude"] < PHONE.lng < box["high"]["longitude"]

    first = out["places"][0]
    assert first == {
        "name": "Ramen Shop 1", "address": "1 Fort St, Victoria, BC V8W 1E5, Canada",
        "distance_m": 334, "direction": "north", "walk_min": 5,
        "open": "open now, closes 5:00 pm",
        "hours_today": "Monday: 9:00 AM - 5:00 PM", "rating": 4.5, "rating_count": 312,
        "price_level": "$$", "phone": "(250) 555-0100",
        "maps_url": "https://maps.google.com/?cid=1", "place_id": "ChIJplace1",
    }
    assert out["places"][1]["distance_m"] == 667
    assert out["summary"].startswith(
        "5 places for 'ramen' within 2000 m of his position (the phone's last report 5 min ago): "
        "Ramen Shop 1, 334 m north, 5 min on foot, open now, closes 5:00 pm;",
    ) or out["summary"].startswith("2 places for 'ramen' within 2000 m of his position")
    assert FAKE_KEY not in json.dumps(out)
    assert matrix.full_url.endswith("distanceMatrix/v2:computeRouteMatrix")
    assert _body(matrix)["travelMode"] == "WALK"
    assert len(_body(matrix)["destinations"]) == 2, "one request for every place within 3 km"
    assert _mask(matrix) == ["originIndex", "destinationIndex", "duration", "condition"]


def test_what_is_outside_the_circle_is_dropped_and_the_rest_is_nearest_first(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The box's corner is not the circle; the answer is by distance, not Google's order."""
    google.text = {"places": [
        _place(4),  # 1336 m
        _place(7),  # 2338 m: inside the box, outside the 2000 m circle
        _place(2),  # 667 m
    ]}
    out = _tools(log)("map_search", {"query": "ramen"})
    assert [p["name"] for p in out["places"]] == ["Ramen Shop 2", "Ramen Shop 4"]
    receipt = out.receipt
    assert receipt.reads_only
    assert receipt.covered.shown == 2


def test_closed_for_now_and_for_good_are_said_with_when_it_changes(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Open, closed for now and closed for good, and the next change in his time with the day."""
    shut = _place(1)
    shut["currentOpeningHours"] = {
        "openNow": False, "weekdayDescriptions": WEEK,
        "nextOpenTime": "2026-10-13T16:00:00Z",  # 9:00 am Tuesday, Pacific
    }
    gone = _place(2)
    gone["businessStatus"] = "CLOSED_PERMANENTLY"
    paused = _place(3)
    paused["businessStatus"] = "CLOSED_TEMPORARILY"
    google.text = {"places": [shut, gone, paused]}
    got = _tools(log)("map_search", {"query": "ramen"})["places"]
    assert got[0]["open"].startswith("closed now, opens tomorrow (Tue)")
    assert got[0]["open"].endswith("9:00 am")
    assert got[1]["open"] == "closed for good"
    assert got[2]["open"] == "closed for now"


def test_a_place_without_extras_leaves_them_out_instead_of_guessing(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A place without extras leaves them out instead of guessing."""
    google.text = {"places": [_place(1, with_extras=False)]}
    [one] = _tools(log)("map_search", {"query": "ramen"})["places"]
    assert set(one) == {
        "name", "address", "distance_m", "direction", "walk_min", "maps_url", "place_id",
    }


def test_no_result_is_said_not_an_error(google: Google, log: sqlite3.Connection) -> None:
    """Nothing inside and nothing anywhere: no place is said, and no walking is asked."""
    google.text = {}
    out = _tools(log)("map_search", {"query": "zzzz"})
    assert out["places"] == []
    assert "no place" in out["summary"]
    assert len(google.to(PLACES_HOST)) == 2, "the search, then the one that looks further"
    assert not google.to(ROUTES_HOST)


def test_nothing_inside_the_radius_asks_once_more_for_the_nearest(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The second request is biased to Here, ranked by distance, and says how far the nearest is."""
    far = _place(30)  # about 10 km north
    google.text_replies = [{}, {"places": [far]}]
    out = _tools(log)("map_search", {"query": "ramen", "radius_m": 1000})

    first, second = google.to(PLACES_HOST)
    assert "locationRestriction" in _body(first)
    again = _body(second)
    assert again["rankPreference"] == "DISTANCE"
    assert again["maxResultCount"] == 3
    assert again["locationBias"]["circle"]["radius"] == 50_000
    assert again["locationBias"]["circle"]["center"] == {
        "latitude": PHONE.lat, "longitude": PHONE.lng,
    }
    assert out["summary"].startswith("Nothing within 1000 m for 'ramen'; the nearest is Ramen Shop")
    assert out["places"][0]["outside_radius"] is True
    assert out["places"][0]["distance_m"] == 10_008
    assert not google.to(ROUTES_HOST), "10 km is not walked"


def test_a_bare_type_is_a_nearby_search_nearest_first(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A bare type is a nearby search nearest first."""
    out = _tools(log)("map_search", {"kind": "pharmacy", "radius_m": 800, "limit": 3})
    [request, _matrix] = google.seen
    assert request.full_url == "https://places.googleapis.com/v1/places:searchNearby"
    assert _body(request) == {
        "includedTypes": ["pharmacy"], "maxResultCount": 8, "languageCode": "en",
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {"center": {"latitude": PHONE.lat,
                                                       "longitude": PHONE.lng}, "radius": 800}},
    }
    assert out["places"][0]["name"] == "Ramen Shop 1"


def test_a_named_area_is_looked_up_then_searched_inside_its_bounds(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The reader finds the area with its bounds; the search is restricted to them."""
    area = {
        "id": "ChIJfairfield", "displayName": {"text": "Fairfield"},
        "formattedAddress": "Fairfield, Victoria, BC, Canada",
        "location": {"latitude": 48.41, "longitude": -123.34},
        "viewport": {"low": {"latitude": 48.40, "longitude": -123.36},
                     "high": {"latitude": 48.42, "longitude": -123.32}},
    }
    google.text_replies = [{"places": [area]}]
    out = _tools(log)("map_search", {"query": "ramen", "area": "Fairfield, Victoria"})

    reader_request, search = google.to(PLACES_HOST)
    assert _body(reader_request)["textQuery"] == "Fairfield, Victoria"
    assert _body(reader_request)["maxResultCount"] == 1
    assert _mask(reader_request) == [
        "places.id", "places.displayName", "places.formattedAddress", "places.location",
        "places.viewport",
    ]
    assert _box(search) == {
        "low": {"latitude": 48.40, "longitude": -123.36},
        "high": {"latitude": 48.42, "longitude": -123.32},
    }
    assert _body(search)["textQuery"] == "ramen"
    assert "locationBias" not in _body(search)
    # distances are from the area's point (48.41, -123.34), said as from there
    assert out["places"][0]["distance_m"] > 1500
    assert "in Fairfield, Victoria (distances from Fairfield)" in out["summary"]
    assert not google.to(ROUTES_HOST), "no walking times from somewhere he is not"
    assert _costs(log) == [
        ("places.text_search.pro", 0.032), ("places.text_search.enterprise", 0.035),
    ]


def test_an_area_search_works_when_no_phone_has_reported(log: sqlite3.Connection) -> None:
    """The area is read without a bias and the places are measured from the area."""
    out = _tools(log, phone=NEVER)("map_search", {"query": "ramen", "area": "Fairfield"})
    assert out["places"][0]["distance_m"] >= 0
    assert "(distances from " in out["summary"]


def test_near_him_with_no_position_asks_instead_of_searching(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Near him with no position asks instead of searching."""
    with pytest.raises(ToolError) as caught:
        _tools(log, phone=NEVER)("map_search", {"query": "ramen"})
    assert caught.value.code == "location_unavailable"
    assert not google.seen
    assert _costs(log) == []


def test_an_old_phone_report_comes_first_in_the_summary(
    log: sqlite3.Connection,
) -> None:
    """The summary opens with the position being old, before any distance is trusted."""
    stale = replace(PHONE, age_s=3 * 3600, why="the phone did not answer")
    out = _tools(log, phone=FixedHere(stale))("map_search", {"query": "ramen"})
    assert out["summary"].startswith("His phone's report is old, so he may have moved. ")
    assert "warning" not in out


def test_a_walking_time_that_fails_leaves_the_answer_without_it(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The matrix is extra: a refusal costs nothing and the places still come."""
    google.matrix_error = _refused(403, "PERMISSION_DENIED", "SERVICE_DISABLED")
    out = _tools(log)("map_search", {"query": "ramen"})
    assert "walk_min" not in out["places"][0]
    assert all(sku != "routes.matrix.essentials" for sku, _ in _costs(log))


def test_each_walking_time_is_metered_as_a_matrix_element(
    log: sqlite3.Connection,
) -> None:
    """Two places walked: two elements under their own SKU."""
    _tools(log)("map_search", {"query": "ramen"})
    assert _costs(log) == [
        ("places.text_search.enterprise", 0.035),
        ("routes.matrix.essentials", 0.01),
    ]


def test_a_place_id_gives_that_place_in_detail(google: Google, log: sqlite3.Connection) -> None:
    """A place id gives that place in detail."""
    out = _tools(log)("map_search", {"place_id": "ChIJplace1"})
    [request] = google.to(PLACES_HOST)
    assert request.get_method() == "GET"
    assert request.full_url == "https://places.googleapis.com/v1/places/ChIJplace1"
    assert _mask(request) == [
        "id", "displayName", "formattedAddress", "location", "googleMapsUri", "rating",
        "userRatingCount", "priceLevel", "nationalPhoneNumber", "currentOpeningHours.openNow",
        "currentOpeningHours.weekdayDescriptions", "businessStatus",
        "currentOpeningHours.nextOpenTime", "currentOpeningHours.nextCloseTime", "websiteUri",
        "reviews.rating", "reviews.text", "reviews.relativePublishTimeDescription",
    ]
    place = out["place"]
    assert place["hours_week"] == WEEK
    assert place["website"] == "https://ramen.example"
    assert [r["rating"] for r in place["reviews"]] == [5, 4, 3], "three highlights"
    assert len(place["reviews"][0]["text"]) == 240
    assert place["reviews"][0]["when"] == "a week ago"
    assert place["distance_m"] == 334
    assert place["open"] == "open now, closes 5:00 pm"


def test_arguments_that_do_not_make_one_question_are_refused_before_any_request(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Arguments that do not make one question are refused before any request."""
    call = _tools(log)
    for args in ({}, {"query": "a", "kind": "cafe"}, {"place_id": "x", "query": "a"},
                 {"query": "a", "bogus": 1}):
        with pytest.raises(ToolError) as caught:
            call("map_search", args)
        assert caught.value.code == "invalid_arguments"
    assert not google.seen


def test_the_description_sends_stops_buses_and_his_places_elsewhere() -> None:
    """The second sentence says what not to use it for, and what to use instead."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, here=FixedHere(PHONE))
    (tool,) = (d for d in registry.get_definitions() if d.name == "map_search")
    assert tool.read_only
    assert "Not for bus stops or bus times (bus)" in tool.description
    assert "(where_am_i)" in tool.description
    assert len(tool.description) <= 600


def test_no_key_no_tool() -> None:
    """No key no tool."""
    registry = build_default_registry(transit_api_key="")
    assert "map_search" not in {d.name for d in registry.get_definitions()}


def test_a_project_without_places_enabled_gets_a_plain_reason_and_no_charge(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A project without places enabled gets a plain reason and no charge."""
    google.text = _refused(403, "PERMISSION_DENIED", "SERVICE_DISABLED")
    with pytest.raises(ToolError) as caught:
        _tools(log)("map_search", {"query": "ramen"})
    assert caught.value.code == "places_not_enabled"
    assert "Places API (New) is not enabled" in str(caught.value)
    assert FAKE_KEY not in str(caught.value)
    assert _costs(log) == [], "a refused request is not billed"


def test_another_failure_is_a_short_error_that_says_not_to_guess(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Another failure is a short error that says not to guess."""
    google.text = _refused(500, "INTERNAL", "")
    with pytest.raises(ToolError) as caught:
        _tools(log)("map_search", {"query": "ramen"})
    assert caught.value.code == "network_error"
    assert "do not guess" in str(caught.value)


# --- bus ------------------------------------------------------------------------------------


STARBUCKS = {
    "id": "ChIJdestination0123456789", "displayName": {"text": "Starbucks"},
    "formattedAddress": "1234 Douglas St, Victoria, BC V8W 2E7, Canada",
    "location": DESTINATION,
    "viewport": {"low": {"latitude": 48.4295, "longitude": -123.3705},
                 "high": {"latitude": 48.4305, "longitude": -123.3695}},
}


def test_the_bus_reads_a_name_with_one_search_ranked_by_distance(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The reader asks Pro-tier Text Search near Here, nearest first; the bus uses its point."""
    google.text = {"places": [STARBUCKS]}
    google.routes["TRANSIT"] = _answer_with_end()
    google.routes["WALK"] = {"routes": [{"duration": "1500s", "distanceMeters": 2000}]}
    google.routes["DRIVE"] = {"routes": [{"duration": "420s", "distanceMeters": 3000}]}
    out = _tools(log)("bus", {"to": "starbucks douglas", "from": "here"})

    [read] = google.to(PLACES_HOST)
    assert read.full_url == "https://places.googleapis.com/v1/places:searchText"
    assert _mask(read) == [
        "places.id", "places.displayName", "places.formattedAddress", "places.location",
        "places.viewport",
    ]
    assert _body(read) == {
        "textQuery": "starbucks douglas", "maxResultCount": 1, "languageCode": "en",
        "rankPreference": "DISTANCE",
        "locationBias": {"circle": {"center": {"latitude": PHONE.lat, "longitude": PHONE.lng},
                                    "radius": 50_000}},
    }
    transit = next(r for r in google.to(ROUTES_HOST) if _body(r)["travelMode"] == "TRANSIT")
    assert _body(transit)["destination"] == {
        "location": {"latLng": {"latitude": 48.43, "longitude": -123.37}},
    }
    assert out["destination"] == {
        "name": "Starbucks", "lat": DESTINATION["latitude"], "lng": DESTINATION["longitude"],
        "address": "1234 Douglas St, Victoria, BC V8W 2E7, Canada",
        "place_id": "ChIJdestination0123456789", "kind": "place",
    }
    assert "To Starbucks (1234 Douglas St, Victoria, BC V8W 2E7" in out["summary"]
    # the DRIVE and WALK requests are one SKU; the order the threads finish in is not fixed.
    assert sorted(_costs(log)) == [
        ("places.text_search.pro", 0.032), ("routes.basic.essentials", 0.01),
        ("routes.transit.pro", 0.01),
    ]


def test_a_kept_place_is_said_as_he_named_it_and_costs_no_place_lookup(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A kept place is said as he named it and costs no place lookup."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    out = _tools(log)("bus", {"to": "home"})
    assert out["destination"]["name"] == "home"
    assert not google.to(PLACES_HOST)


def test_a_map_search_place_id_is_the_destination_and_its_name_is_remembered(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A place_id from map_search goes in `to`; the name comes from the search, not a lookup."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    call = _tools(log)
    call("map_search", {"query": "ramen"})
    before = len(google.to(PLACES_HOST))
    out = call("bus", {"to": "ChIJplace2"})

    sent = _body(next(r for r in google.to(ROUTES_HOST) if _body(r)["travelMode"] == "TRANSIT"))
    assert sent["destination"] == {
        "location": {"latLng": {"latitude": PHONE.lat + 0.006, "longitude": PHONE.lng}},
    }
    assert len(google.to(PLACES_HOST)) == before
    assert out["destination"]["name"] == "Ramen Shop 2"
    assert out["destination"]["place_id"] == "ChIJplace2"
    assert "To Ramen Shop 2 (2 Fort St" in out["summary"]


def test_a_place_id_this_process_does_not_know_is_looked_up_once_for_its_name(
    google: Google, log: sqlite3.Connection,
) -> None:
    """An unknown place id costs one Pro-tier Place Details request."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    google.named = {**STARBUCKS}
    out = _tools(log)("bus", {"to": "ChIJdestination0123456789"})
    [named] = google.to(PLACES_HOST)
    assert named.full_url == "https://places.googleapis.com/v1/places/ChIJdestination0123456789"
    assert named.get_header("X-goog-fieldmask") == (
        "id,displayName,formattedAddress,location,viewport"
    )
    assert out["destination"]["name"] == "Starbucks"
    assert ("places.details.pro", 0.017) in _costs(log)


def test_place_id_is_no_longer_an_argument_of_the_bus(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The old argument is refused with the call that took its place, before any request."""
    with pytest.raises(ToolError) as caught:
        _tools(log)("bus", {"place_id": "ChIJplace2"})
    assert caught.value.code == "invalid_arguments"
    assert "place_id is to=" in str(caught.value)
    assert not google.seen


# --- the monthly cap ---------------------------------------------------------------------


def test_each_request_is_priced_and_recorded_under_its_sku(
    log: sqlite3.Connection,
) -> None:
    """Every request is recorded at list price; Google bills none of them within the free use."""
    call = _tools(log)
    call("map_search", {"query": "ramen"})
    call("map_search", {"kind": "cafe"})
    call("map_search", {"place_id": "ChIJplace1"})
    assert _costs(log) == [
        ("places.text_search.enterprise", 0.035), ("routes.matrix.essentials", 0.01),
        ("places.nearby_search.enterprise", 0.035), ("routes.matrix.essentials", 0.005),
        ("places.details.enterprise_atmosphere", 0.025),
    ]
    assert maps_spend.month_spent(log, NOW) == 0


def test_1200_enterprise_searches_in_a_month_bill_only_the_200_past_the_free_use(
    log: sqlite3.Connection,
) -> None:
    """The month's sum is what Google bills: (requests - 1000 free) x price, per SKU."""
    ctx = tool_context(log, "A-bulk", clock=CLOCK)
    maps_spend.record(ctx, ["places.text_search.enterprise"] * 1200)
    assert maps_spend.month_spent(log, NOW) == pytest.approx(200 * 0.035)
    maps_spend.record(ctx, ["places.details.pro"] * 100)
    assert maps_spend.month_spent(log, NOW) == pytest.approx(200 * 0.035), "Pro has 5,000 free"


def test_paid_calls_stop_near_the_cap_for_places_and_routes_alike(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Paid calls stop near the cap for places and routes alike, by the billed sum."""
    call = _tools(log, cap=10.0)
    _spend(log, 9.2)  # past 90% of 10
    for tool, args in (("map_search", {"query": "ramen"}), ("bus", {"to": "home"})):
        with pytest.raises(ToolError) as caught:
            call(tool, args)
        assert caught.value.code == "spend_cap"
        assert "billed about $9.2" in str(caught.value)
        assert "$10.00 monthly limit" in str(caught.value)
    assert not google.seen, "a refused call makes no request"


def test_a_cap_of_zero_is_no_cap(log: sqlite3.Connection) -> None:
    """A cap of zero is no cap."""
    _spend(log, 500.0)
    _tools(log, cap=0)("map_search", {"query": "ramen"})


def test_last_months_spend_is_not_this_months(log: sqlite3.Connection) -> None:
    """Last months spend is not this months."""
    maps_spend.SKUS["test.spend"] = maps_spend.Sku(50.0, 0)
    last_month = datetime.now(ZoneInfo("America/Los_Angeles")).replace(day=1) - timedelta(days=2)
    maps_spend.record(tool_context(log, "A-old", clock=CLOCK), ["test.spend"], last_month)
    assert maps_spend.month_spent(log, NOW) == 0
    _tools(log)("map_search", {"query": "ramen"})


def test_the_months_total_survives_a_restart(tmp_path: Path) -> None:
    """The months total survives a restart."""
    path = tmp_path / "restart.db"
    first = open_event_log(path)
    _spend(first, 9.0)
    first.close()

    second = open_event_log(path)  # the brain restarted: a new connection, a new registry
    assert maps_spend.month_spent(second, NOW) == pytest.approx(9.0)
    with pytest.raises(ToolError) as caught:
        _tools(second)("map_search", {"query": "ramen"})
    assert caught.value.code == "spend_cap"


def test_the_runtime_reads_the_cap_from_the_maps_block() -> None:
    """The runtime reads the cap from the maps block."""
    def read(raw: dict[str, Any]) -> float:
        return runtime_config(raw).maps_monthly_usd

    assert read({}) == 10.0
    assert read({"maps": {"monthly_usd": 25}}) == 25.0
    assert read({"maps": {"monthly_usd": 0}}) == 0.0
    assert read({"maps": {"monthly_usd": -1}}) == 10.0
    assert read({"maps": {"monthly_usd": "ten"}}) == 10.0


# --- places on the answer ------------------------------------------------------------------


def test_the_places_a_turn_returned_are_what_its_answer_carries(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The places a turn returned are what its answer carries."""
    google.routes["TRANSIT"] = _answer_with_end()
    google.text_replies = [{"places": [_place(1), _place(2)]}, {"places": [STARBUCKS]}]
    call = _tools(log)
    call("map_search", {"query": "ramen"}, turn="T1")
    call("bus", {"to": "starbucks douglas"}, turn="T1")
    call("map_search", {"query": "other"}, turn="T-other")

    got = places_api.turn_places(log, "T1")
    assert got[:2] == [
        {"name": "Ramen Shop 1", "lat": PHONE.lat + 0.003, "lng": PHONE.lng,
         "address": "1 Fort St, Victoria, BC V8W 1E5, Canada", "place_id": "ChIJplace1",
         "kind": "place"},
        {"name": "Ramen Shop 2", "lat": PHONE.lat + 0.006, "lng": PHONE.lng,
         "address": "2 Fort St, Victoria, BC V8W 1E5, Canada", "place_id": "ChIJplace2",
         "kind": "place"},
    ]
    assert got[2] == {
        "name": "Starbucks", "lat": DESTINATION["latitude"], "lng": DESTINATION["longitude"],
        "address": "1234 Douglas St, Victoria, BC V8W 2E7, Canada",
        "place_id": "ChIJdestination0123456789", "kind": "place",
    }
    assert len(got) == 3
    assert places_api.turn_places(log, "T-none") == []


def test_at_most_eight_places_the_latest_and_none_twice(
    google: Google, log: sqlite3.Connection,
) -> None:
    """At most eight places the latest and none twice."""
    call = _tools(log)
    google.text = {"places": [_place(n) for n in range(1, 9)]}
    call("map_search", {"query": "a", "limit": 8, "radius_m": 5000}, turn="T1")
    google.text = {"places": [_place(n) for n in (8, 9, 10)]}
    call("map_search", {"query": "b", "radius_m": 5000}, turn="T1")
    got = places_api.turn_places(log, "T1")
    assert [p["place_id"] for p in got] == [f"ChIJplace{n}" for n in (3, 4, 5, 6, 7, 8, 9, 10)]


@pytest.fixture
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture key."""
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(start_module, "labels_phases", lambda _base_url: True)


@pytest.mark.usefixtures("_fixture_key")
def test_the_final_row_carries_places_and_leaves_the_key_out_when_there_are_none(
    tmp_path: Path,
) -> None:
    """Through ``drive_turn``: the answer's ``surface.response_emitted`` is what the phone reads."""
    with SpokenPeer([[("final_answer", "Two ramen places.")], [("final_answer", "Fine.")]]) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        _tools(runtime.conn)("map_search", {"query": "ramen"}, turn="T-with")
        for turn in ("T-with", "T-without"):
            intent = emit_surface_user_intent(
                runtime.conn, transcript="ramen near me", turn_id=turn,
                channel="inherent_ptt", ingestion_node="iphone",
            )
            drive_streaming_turn(runtime, intent)
    with_places, without = (
        [
            p
            for p in logged_payloads(runtime.conn, "surface.response_emitted")
            if p["turn_id"] == turn
        ]
        for turn in ("T-with", "T-without")
    )
    assert [p["name"] for p in with_places[-1]["places"]] == ["Ramen Shop 1", "Ramen Shop 2"]
    assert set(with_places[-1]["places"][0]) == {
        "name",
        "lat",
        "lng",
        "address",
        "place_id",
        "kind",
    }
    assert "places" not in without[-1]
