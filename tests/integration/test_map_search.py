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
from tests.support.transit import HANDBUILT

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
            "currentOpeningHours": {"openNow": True, "weekdayDescriptions": WEEK},
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

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> io.BytesIO:  # noqa: ARG002
        """Answer one request."""
        self.seen.append(request)
        url = request.full_url
        if url == "https://places.googleapis.com/v1/places:searchText":
            reply = self.text
        elif url == "https://places.googleapis.com/v1/places:searchNearby":
            reply = self.nearby
        elif url.startswith("https://places.googleapis.com/v1/places/"):
            wide = "reviews" in (request.get_header("X-goog-fieldmask") or "")
            reply = self.details if wide else self.named
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
    """Google's hand-built bus answer, with the end point and the place it resolved the text to."""
    route = dict(HANDBUILT["routes"][0])
    route["legs"] = [{**route["legs"][0], "endLocation": {"latLng": DESTINATION}}]
    out: dict[str, Any] = {**HANDBUILT, "routes": [route, *HANDBUILT["routes"][1:]]}
    if place_id:
        out["geocodingResults"] = {"destination": {"placeId": place_id}}
    return out


def _tools(
    log: sqlite3.Connection,
    *,
    cap: float = 10.0,
    phone: FixedHere | None = PHONE_HERE,
) -> Callable[..., dict[str, Any]]:
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

    def call(tool: str, args: dict[str, Any], turn: str | None = None) -> dict[str, Any]:
        """Call."""
        action_id = f"A{next(count)}"
        if turn:
            register_live_action(turn_id=turn, action_id=action_id)
        try:
            ctx = tool_context(
                log, action_id, clock=CLOCK, here=None if phone is None else phone.bind(turn),
            )
            return dict(handlers[tool](args, ctx))
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
    maps_spend.PRICE_USD["test.spend"] = usd
    maps_spend.record(tool_context(log, "A-seed", clock=CLOCK), ["test.spend"])


@pytest.fixture(autouse=True)
def _clean_prices() -> Any:  # noqa: ANN401 - fixture
    """Clean prices."""
    yield
    maps_spend.PRICE_USD.pop("test.spend", None)


# --- map_search ----------------------------------------------------------------------------


def test_the_default_is_a_text_search_near_the_phones_last_report(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Headers, body, field mask; each place with distance, direction, hours and the rest."""
    out = _tools(log)("map_search", {"query": "ramen"})

    [request] = google.to(PLACES_HOST)
    assert request.get_method() == "POST"
    assert request.full_url == "https://places.googleapis.com/v1/places:searchText"
    assert request.get_header("X-goog-api-key") == FAKE_KEY
    assert "key=" not in request.full_url, "the key goes in a header, not the URL"
    assert _mask(request) == [
        "places.id", "places.displayName", "places.formattedAddress", "places.location",
        "places.googleMapsUri", "places.rating", "places.userRatingCount", "places.priceLevel",
        "places.nationalPhoneNumber", "places.currentOpeningHours.openNow",
        "places.currentOpeningHours.weekdayDescriptions",
    ]
    assert _body(request) == {
        "textQuery": "ramen", "maxResultCount": 5, "languageCode": "en",
        "locationBias": {"circle": {"center": {"latitude": PHONE.lat,
                                                "longitude": PHONE.lng}, "radius": 2000}},
    }
    first = out["places"][0]
    assert first == {
        "name": "Ramen Shop 1", "address": "1 Fort St, Victoria, BC V8W 1E5, Canada",
        "distance_m": 334, "direction": "north", "open_now": True,
        "hours_today": "Monday: 9:00 AM - 5:00 PM", "rating": 4.5, "rating_count": 312,
        "price_level": "$$", "phone": "(250) 555-0100",
        "maps_url": "https://maps.google.com/?cid=1", "place_id": "ChIJplace1",
    }
    assert out["places"][1]["distance_m"] == 667
    assert "the phone's last report 5 min ago" in out["near"]
    assert "warning" not in out
    assert FAKE_KEY not in json.dumps(out)


def test_a_place_without_extras_leaves_them_out_instead_of_guessing(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A place without extras leaves them out instead of guessing."""
    google.text = {"places": [_place(1, with_extras=False)]}
    [one] = _tools(log)("map_search", {"query": "ramen"})["places"]
    assert set(one) == {"name", "address", "distance_m", "direction", "maps_url", "place_id"}


def test_no_result_is_said_not_an_error(google: Google, log: sqlite3.Connection) -> None:
    """No result is said not an error."""
    google.text = {}
    out = _tools(log)("map_search", {"query": "zzzz"})
    assert out["places"] == []
    assert "no place" in out["summary"]


def test_a_bare_type_is_a_nearby_search_nearest_first(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A bare type is a nearby search nearest first."""
    out = _tools(log)("map_search", {"kind": "pharmacy", "radius_m": 800, "limit": 3})
    [request] = google.to(PLACES_HOST)
    assert request.full_url == "https://places.googleapis.com/v1/places:searchNearby"
    assert _body(request) == {
        "includedTypes": ["pharmacy"], "maxResultCount": 3, "languageCode": "en",
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {"center": {"latitude": PHONE.lat,
                                                       "longitude": PHONE.lng}, "radius": 800}},
    }
    assert out["places"][0]["name"] == "Ramen Shop 1"


def test_a_named_area_is_searched_there_with_no_bias_and_still_measured_from_him(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A named area is searched there with no bias and still measured from him."""
    out = _tools(log)("map_search", {"query": "ramen", "area": "Fairfield, Victoria"})
    [request] = google.to(PLACES_HOST)
    assert _body(request) == {
        "textQuery": "ramen in Fairfield, Victoria", "maxResultCount": 5, "languageCode": "en",
    }
    assert out["places"][0]["distance_m"] == 334


def test_an_area_search_works_when_no_phone_has_reported_but_has_no_distances(
    log: sqlite3.Connection,
) -> None:
    """An area search works when no phone has reported but has no distances."""
    out = _tools(log, phone=NEVER)("map_search", {"query": "ramen", "area": "Fairfield"})
    assert "distance_m" not in out["places"][0]
    assert "near" not in out


def test_near_him_with_no_position_asks_instead_of_searching(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Near him with no position asks instead of searching."""
    with pytest.raises(ToolError) as caught:
        _tools(log, phone=NEVER)("map_search", {"query": "ramen"})
    assert caught.value.code == "location_unavailable"
    assert not google.seen
    assert _costs(log) == []


def test_an_old_phone_report_comes_with_a_warning(
    log: sqlite3.Connection,
) -> None:
    """An old phone report comes with a warning."""
    stale = replace(PHONE, age_s=3 * 3600, why="the phone did not answer")
    out = _tools(log, phone=FixedHere(stale))("map_search", {"query": "ramen"})
    assert "may have moved" in out["warning"]


def test_a_place_id_gives_that_place_in_detail(google: Google, log: sqlite3.Connection) -> None:
    """A place id gives that place in detail."""
    out = _tools(log)("map_search", {"place_id": "ChIJplace1"})
    [request] = google.to(PLACES_HOST)
    assert request.get_method() == "GET"
    assert request.full_url == "https://places.googleapis.com/v1/places/ChIJplace1"
    assert _mask(request) == [
        "id", "displayName", "formattedAddress", "location", "googleMapsUri", "rating",
        "userRatingCount", "priceLevel", "nationalPhoneNumber", "currentOpeningHours.openNow",
        "currentOpeningHours.weekdayDescriptions", "websiteUri", "reviews.rating",
        "reviews.text", "reviews.relativePublishTimeDescription",
    ]
    place = out["place"]
    assert place["hours_week"] == WEEK
    assert place["website"] == "https://ramen.example"
    assert [r["rating"] for r in place["reviews"]] == [5, 4, 3], "three highlights"
    assert len(place["reviews"][0]["text"]) == 240
    assert place["reviews"][0]["when"] == "a week ago"
    assert place["distance_m"] == 334


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


def test_vague_or_misheard_names_are_the_descriptions_rule_not_code() -> None:
    """Vague or misheard names are the descriptions rule not code."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, here=FixedHere(PHONE))
    (tool,) = (d for d in registry.get_definitions() if d.name == "map_search")
    text = tool.description
    assert tool.read_only
    assert "vague or may be misheard, search first, name the specific match" in text
    assert "let him confirm" in text
    assert "never invent a place" in text


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


def test_the_bus_says_the_place_and_address_google_resolved_and_the_walk(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The bus says the place and address google resolved and the walk."""
    google.routes["TRANSIT"] = _answer_with_end()
    google.routes["WALK"] = {"routes": [{"duration": "1500s", "distanceMeters": 2000}]}
    google.routes["DRIVE"] = {"routes": [{"duration": "420s", "distanceMeters": 3000}]}
    out = _tools(log)("bus", {"to": "starbucks douglas", "from": "here"})

    assert out["destination"] == {
        "name": "Starbucks", "lat": DESTINATION["latitude"], "lng": DESTINATION["longitude"],
        "address": "1234 Douglas St, Victoria, BC V8W 2E7, Canada", "place_id": "ChIJdest",
        "kind": "place",
    }
    assert out["summary"].startswith("To Starbucks (1234 Douglas St, Victoria, BC V8W 2E7")
    assert "Walking all the way is 25 min." in out["summary"]
    transit_request = next(r for r in google.to(ROUTES_HOST)
                           if _body(r)["travelMode"] == "TRANSIT")
    mask = _mask(transit_request)
    assert mask[-2:] == ["routes.legs.endLocation", "geocodingResults"]
    [named] = google.to(PLACES_HOST)
    assert named.full_url == "https://places.googleapis.com/v1/places/ChIJdest"
    assert named.get_header("X-goog-fieldmask") == "id,displayName,formattedAddress,location"
    # the DRIVE and WALK requests are one SKU; the order the three threads finish in is not fixed.
    assert sorted(_costs(log)) == [
        ("places.details.pro", 0.017), ("routes.basic.essentials", 0.01),
        ("routes.transit.pro", 0.01),
    ]


def test_a_kept_place_is_said_as_he_named_it_and_costs_no_place_lookup(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A kept place is said as he named it and costs no place lookup."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    out = _tools(log)("bus", {"to": "home"})
    assert out["destination"]["name"] == "home"
    assert out["destination"]["address"] is None
    assert not google.to(PLACES_HOST)


def test_a_map_search_place_id_is_the_destination_and_its_name_is_remembered(
    google: Google, log: sqlite3.Connection,
) -> None:
    """A map search place id is the destination and its name is remembered."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    call = _tools(log)
    call("map_search", {"query": "ramen"})
    before = len(google.to(PLACES_HOST))
    out = call("bus", {"place_id": "ChIJplace2"})

    sent = _body(next(r for r in google.to(ROUTES_HOST)
                           if _body(r)["travelMode"] == "TRANSIT"))
    assert sent["destination"] == {"placeId": "ChIJplace2"}
    assert len(google.to(PLACES_HOST)) == before, "the name came from the search, not a lookup"
    assert out["to"] == "Ramen Shop 2"
    assert out["destination"]["name"] == "Ramen Shop 2"
    assert out["destination"]["place_id"] == "ChIJplace2"
    assert out["summary"].startswith("To Ramen Shop 2 (2 Fort St")


def test_an_unknown_place_id_is_looked_up_once_for_its_name(
    google: Google, log: sqlite3.Connection,
) -> None:
    """An unknown place id is looked up once for its name."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    out = _tools(log)("bus", {"place_id": "ChIJdest"})
    assert out["to"] == "that place"
    assert out["destination"]["name"] == "Starbucks"
    assert len(google.to(PLACES_HOST)) == 1


def test_to_and_place_id_together_are_refused(google: Google, log: sqlite3.Connection) -> None:
    """To and place id together are refused."""
    with pytest.raises(ToolError) as caught:
        _tools(log)("bus", {"to": "home", "place_id": "x"})
    assert caught.value.code == "invalid_arguments"
    assert not google.seen


# --- the monthly cap ---------------------------------------------------------------------


def test_each_request_is_priced_and_recorded_under_its_sku(
    log: sqlite3.Connection,
) -> None:
    """Each request is priced and recorded under its sku."""
    call = _tools(log)
    call("map_search", {"query": "ramen"})
    call("map_search", {"kind": "cafe"})
    call("map_search", {"place_id": "ChIJplace1"})
    assert _costs(log) == [
        ("places.text_search.enterprise", 0.035),
        ("places.nearby_search.enterprise", 0.035),
        ("places.details.enterprise_atmosphere", 0.025),
    ]
    assert maps_spend.month_spent(log, NOW) == pytest.approx(0.095)


def test_paid_calls_stop_near_the_cap_for_places_and_routes_alike(
    google: Google, log: sqlite3.Connection,
) -> None:
    """Paid calls stop near the cap for places and routes alike."""
    call = _tools(log, cap=10.0)
    _spend(log, 8.99)
    call("map_search", {"query": "ramen"})  # 8.99 is under 90% of 10: allowed; now 9.025
    google.seen.clear()
    for tool, args in (("map_search", {"query": "ramen"}), ("bus", {"to": "home"})):
        with pytest.raises(ToolError) as caught:
            call(tool, args)
        assert caught.value.code == "spend_cap"
        assert "$9.0" in str(caught.value)
        assert "$10.00 monthly limit" in str(caught.value)
    assert not google.seen, "a refused call makes no request"


def test_a_bus_search_counts_against_the_cap_too(google: Google, log: sqlite3.Connection) -> None:
    """A bus search counts against the cap too."""
    google.routes["TRANSIT"] = _answer_with_end(place_id=None)
    call = _tools(log, cap=0.035)  # one bus search costs 0.01 + 2 x 0.005 = 0.02
    call("bus", {"to": "home"})
    assert maps_spend.month_spent(log, NOW) == pytest.approx(0.02)
    call("bus", {"to": "home"})  # 0.02 < 90% of 0.035 = 0.0315: still allowed
    with pytest.raises(ToolError) as caught:
        call("bus", {"to": "home"})  # 0.04 is past it
    assert caught.value.code == "spend_cap"


def test_a_cap_of_zero_is_no_cap(log: sqlite3.Connection) -> None:
    """A cap of zero is no cap."""
    _spend(log, 500.0)
    _tools(log, cap=0)("map_search", {"query": "ramen"})


def test_last_months_spend_is_not_this_months(log: sqlite3.Connection) -> None:
    """Last months spend is not this months."""
    maps_spend.PRICE_USD["test.spend"] = 50.0
    last_month = datetime.now(ZoneInfo("America/Los_Angeles")).replace(day=1) - timedelta(days=2)
    maps_spend.record(tool_context(log, "A-old", clock=CLOCK), ["test.spend"], last_month)
    assert maps_spend.month_spent(log, NOW) == 0
    _tools(log)("map_search", {"query": "ramen"})


def test_the_months_total_survives_a_restart(tmp_path: Path) -> None:
    """The months total survives a restart."""
    path = tmp_path / "restart.db"
    first = open_event_log(path)
    _tools(first)("map_search", {"query": "ramen"})
    first.close()

    second = open_event_log(path)  # the brain restarted: a new connection, a new registry
    assert maps_spend.month_spent(second, NOW) == pytest.approx(0.035)
    _spend(second, 9.0)
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
        "address": "1234 Douglas St, Victoria, BC V8W 2E7, Canada", "place_id": "ChIJdest",
        "kind": "place",
    }
    assert len(got) == 3
    assert places_api.turn_places(log, "T-none") == []


def test_at_most_eight_places_the_latest_and_none_twice(
    google: Google, log: sqlite3.Connection,
) -> None:
    """At most eight places the latest and none twice."""
    call = _tools(log)
    google.text = {"places": [_place(n) for n in range(1, 9)]}
    call("map_search", {"query": "a", "limit": 8}, turn="T1")
    google.text = {"places": [_place(n) for n in (8, 9, 10)]}
    call("map_search", {"query": "b"}, turn="T1")
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
