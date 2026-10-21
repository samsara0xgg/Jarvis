"""ADR 0189, 0246, 0342, 0343, 0346: the model reads the bus times in one `bus` call.

Google's answers and the place reader's Text Search are hand-built; BC Transit's timetable is a
real ``BusLive`` over a small synthetic export with a hand-built realtime feed. No network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import bus_tool, transit_tool
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.execution.transit_tool import TransitOffers
from jarvis.runtime.bootstrap.places import _here_location
from jarvis.shared import CallerPrincipal
from jarvis.shared.geo import metres
from jarvis.shared.here import HereUnavailable
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projections import CLARIFICATION_EVENT_TYPES, PendingClarification
from jarvis.surface import mac_location
from tests.support import gtfs
from tests.support.bus_world import bus_live, google_place, realtime, serve_text_search
from tests.support.clock import FakeClock
from tests.support.here import FixedHere
from tests.support.runtime_config import runtime_config
from tests.support.tool_context import tool_context
from tests.support.transit import (
    FAKE_KEY,
    FIX,
    PLACES,
    bus_route,
    fix_with,
    local_ms,
    payload,
    routes_with,
    sent_request,
    serve_routes,
    transit_clock,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.execution.transit_tool import BusTimes
    from jarvis.runtime.bus_live import BusLive

ZONE = ZoneInfo("America/Vancouver")


def _point(lat: float, lng: float) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


HERE_POINT = _point(48.46, -123.31)
HOME_POINT = _point(48.48, -123.38)
SCHOOL_POINT = _point(48.4634, -123.3117)
MALL_POINT = _point(48.4467, -123.3713)
WORK_POINT = _point(48.43, -123.37)
CARSA_POINT = _point(48.4665, -123.3093)
# What Text Search finds for each query (public landmarks, or invented).
FOUND = {
    PLACES["school"]: google_place(
        "University of Victoria", "3800 Finnerty Rd, Victoria, BC", 48.4634, -123.3117,
        "ChIJschool000000000000",
    ),
    "Mayfair Mall": google_place(
        "Mayfair Shopping Centre", "3147 Douglas St, Victoria, BC", 48.4467, -123.3713,
        "ChIJmayfair0000000000",
    ),
    "work": google_place(
        "Workshop Hall", "10 Example Ave, Victoria, BC", 48.43, -123.37, "ChIJworkshop000000000",
    ),
    "CARSA": google_place(
        "CARSA", "3964 Gordon Head Rd, Victoria, BC", 48.4665, -123.3093, "ChIJcarsa00000000000",
    ),
}

# Google's stops for the matched answers: bus_route's board point, and an alighting point.
BOARD = (48.46, -123.3)
ALIGHT = (48.47, -123.31)
BOARD_STOP = gtfs.StopSpec("B1", "Stop of the bus", BOARD[0] + 0.0001, BOARD[1], "10001")
ALIGHT_STOP = gtfs.StopSpec("A1", "UVic Exchange", ALIGHT[0] + 0.0001, ALIGHT[1], "10002")


@pytest.fixture(autouse=True)
def _event_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversation card is an event in the log the tool is handed."""
    monkeypatch.setitem(_LOG, "conn", open_event_log(tmp_path / "events.db"))
    # A direct call has no `action.running` event to link the card or the pin to.
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)
    monkeypatch.setattr(bus_tool, "_get_running_event_uid", lambda *_: None)


_LOG: dict[str, Any] = {}


def _call(  # noqa: PLR0913 - the arguments, places, Here, which tool, the timetable, the moment, the list.
    args: dict[str, Any],
    places: dict[str, str] | None = None,
    here: FixedHere | None = None,
    tool: str = "bus",
    bus_live: BusTimes | None = None,
    now: datetime | None = None,
    offers: TransitOffers | None = None,
) -> dict[str, Any]:
    clock = transit_clock() if now is None else FakeClock(now)
    registry = build_default_registry(
        bus_live=bus_live,
        transit_api_key=FAKE_KEY,
        transit_places=lambda: PLACES if places is None else places,
        transit_offers=offers,
        clock=clock,
        here=here,
    )
    (definition,) = (d for d in registry.get_definitions() if d.name == tool)
    ctx = tool_context(_LOG["conn"], clock=clock, here=None if here is None else here.bind(None))
    return dict(definition.handler(args, ctx))


def _serve(monkeypatch: pytest.MonkeyPatch, **routes: Any) -> list[Any]:  # noqa: ANN401
    """Google Routes as ``serve_routes`` serves it, and the reader's Text Search from FOUND."""
    seen = serve_routes(monkeypatch, **routes)
    serve_text_search(monkeypatch, FOUND)
    return seen


def _placed(route: dict[str, Any], alight: tuple[float, float] = ALIGHT) -> dict[str, Any]:
    """``route`` with its buses' alighting stop placed, as Google gives it, so a trip can match."""
    for leg in route["legs"]:
        for step in leg["steps"]:
            if detail := step.get("transitDetails"):
                detail["stopDetails"]["arrivalStop"]["location"] = {
                    "latLng": {"latitude": alight[0], "longitude": alight[1]},
                }
    return route


def _trip(
    trip_id: str, route: str, board_at: str, alight_at: str, *, direction: int = 0,
) -> gtfs.TripSpec:
    """A trip calling at the board stop, then the alight stop."""
    return gtfs.TripSpec(
        trip_id, route, (("B1", board_at), ("A1", alight_at)), direction=direction,
        headsign="Downtown",
    )


def _live(
    cache: Path, *trips: gtfs.TripSpec, feed: Callable[[], bytes] = lambda: b"",
    stops: tuple[gtfs.StopSpec, ...] = (BOARD_STOP, ALIGHT_STOP),
) -> BusLive:
    return bus_live(cache, gtfs.Export(stops, trips), feed)


def _card() -> dict[str, Any]:
    """The trip on the conversation's bus card."""
    found = PendingClarification.from_events(
        iter_events_of_types(_LOG["conn"], CLARIFICATION_EVENT_TYPES)
    )
    assert found is not None
    assert found.trip is not None
    return dict(found.trip)


def test_the_answer_is_plain_words_in_local_time_the_first_bus_and_the_one_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave time, walk, each bus with its source, arrival and total, said in Vancouver time.

    The first he can catch and the next one (ADR 0343); the two later buses are not listed.
    """
    _serve(monkeypatch)
    out = _call({"from": "school", "to": "home"})
    options = out["options"]
    assert len(options) == 2
    assert options[0] == {
        "option": 1,
        "leave_at": "5:06 pm",
        "walk_to_first_stop_min": 6,
        "legs": [
            {
                "route": "26",
                "headsign": "Downtown",
                "board_stop": "Shelbourne at McKenzie",
                "departs": "5:12 pm",
                "source": "scheduled",
                "alight_stop": "Douglas at Fort",
                "arrives": "5:41 pm",
            }
        ],
        "walk_at_end_min": 7,
        "arrive_at": "5:48 pm",
        "total_min": 42,
        "spare_min": 6,  # leaves 17:12, now 17:00, 6 min walk
        "verdict": "easy",
    }
    assert [leg["route"] for leg in options[1]["legs"]] == ["28", "6"]
    assert options[1]["arrive_at"] == "6:03 pm"
    assert [o["spare_min"] for o in options] == [6, 16]  # on every option


def test_one_post_with_the_key_a_tight_field_mask_and_the_saved_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request: headers, body, timeout; home is lat/lng, school's address one Text Search."""
    seen = serve_routes(monkeypatch)
    searches = serve_text_search(monkeypatch, FOUND)
    _call({"from": "Home", "to": "school", "at": "17:30"})
    assert len(seen) == 3  # the bus, and the other ways there at the same time
    request, timeout = sent_request(seen)
    assert request.full_url == "https://routes.googleapis.com/directions/v2:computeRoutes"
    assert request.get_method() == "POST"
    assert request.get_header("X-goog-api-key") == FAKE_KEY
    mask = request.get_header("X-goog-fieldmask")
    assert mask is not None
    assert mask.split(",") == [
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.transitDetails.stopDetails",
        "routes.legs.steps.transitDetails.headsign",
        "routes.legs.steps.transitDetails.transitLine.nameShort",
        "routes.legs.steps.transitDetails.transitLine.name",
        "routes.legs.endLocation",
    ]
    assert payload(request) == {
        "origin": HOME_POINT,
        "destination": SCHOOL_POINT,
        "travelMode": "TRANSIT",
        "computeAlternativeRoutes": True,
        "departureTime": "2026-10-07T00:30:00Z",
    }
    assert timeout == 5.0
    (search,) = searches
    assert search.get_header("X-goog-fieldmask") == (
        "places.id,places.displayName,places.formattedAddress,places.location,places.viewport"
    )
    # No Here to look near: no bias and no distance ranking.
    assert payload(search) == {
        "textQuery": PLACES["school"], "maxResultCount": 1, "languageCode": "en",
    }


def test_any_other_text_is_searched_for_and_now_looks_back_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free text is one Text Search; its point goes to Routes; no at= asks from ten minutes ago."""
    seen = serve_routes(monkeypatch)
    searches = serve_text_search(monkeypatch, FOUND)
    out = _call({"from": "school", "to": "Mayfair Mall"})
    body = payload(sent_request(seen)[0])
    assert body["destination"] == MALL_POINT
    assert body["departureTime"] == "2026-10-06T23:50:00Z"
    assert [payload(r)["textQuery"] for r in searches] == [PLACES["school"], "Mayfair Mall"]
    assert "note" in out  # searched without Here: the answer says it was not near him


def test_the_tool_is_gated_on_the_key_and_is_a_quiet_up_front_model_tool() -> None:
    """No key and no ride, no tool; with one it is on the menu up front, L1, no confirmation.

    It writes (a pin, a ride), so it is not read-only: the turn guards count it as a write.
    """
    assert "bus" not in {d.name for d in build_default_registry().get_definitions()}
    keyless = build_default_registry(transit_api_key="", transit_places=lambda: PLACES)
    assert "bus" not in {d.name for d in keyless.get_definitions()}
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=lambda: PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "bus")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.risk_level == "L1"
    assert not definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    assert "school" in definition.input_schema["properties"]["to"]["description"]


def test_an_unsaved_place_is_a_tool_error_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """School or home unset: the error says so; 'work' is no saved word, only words to search."""
    seen = serve_routes(monkeypatch)
    searches = serve_text_search(monkeypatch, FOUND)
    with pytest.raises(ToolError, match="His school address isn't saved yet; ask him"):
        _call({"from": "home", "to": "school"}, places={"home": "48.48,-123.38"})
    with pytest.raises(ToolError, match="His home address isn't saved yet"):
        _call({"from": "home", "to": "school"}, places={"school": "UVic"})
    assert seen == []
    assert searches == []
    _call({"from": "school", "to": "work"})
    assert payload(searches[-1])["textQuery"] == "work"
    assert payload(sent_request(seen)[0])["destination"] == WORK_POINT


@pytest.mark.parametrize(
    ("fault", "code"),
    [(TimeoutError(), "network_error"), (OSError("HTTP 403"), "network_error")],
)
def test_a_failed_request_is_a_short_tool_error(
    monkeypatch: pytest.MonkeyPatch, fault: Exception, code: str
) -> None:
    """Timeout or HTTP error: one short reason."""
    _serve(monkeypatch, fail=fault)
    with pytest.raises(ToolError, match="Bus times could not be read just now") as caught:
        _call({"from": "home", "to": "school"})
    assert caught.value.code == code


def test_no_routes_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty answer, and no other way there either, is not an invented timetable."""
    _serve(monkeypatch, answer={})
    with pytest.raises(ToolError, match=r"No bus route found.*try another time") as caught:
        _call({"from": "home", "to": "school"})
    assert caught.value.code == "not_found"


def test_a_trip_google_finds_only_on_foot_answers_with_the_walk_and_the_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: no bus route but the other ways answered: those times, not `not_found`."""
    _serve(
        monkeypatch, answer={},
        modes={
            "DRIVE": {"routes": [{"duration": "725s", "distanceMeters": 9340}]},
            "WALK": {"routes": [{"duration": "2400s", "distanceMeters": 3100}]},
        },
    )
    out = _call({"to": "home"}, here=fix_with())
    assert out["options"] == []
    assert out["modes"] == {"drive": {"minutes": 12, "km": 9.3}, "walk": {"minutes": 40, "km": 3.1}}
    assert out["summary"] == (
        "No bus route to home at that time. Driving takes 12 min. Walking all the way is 40 min."
    )


def test_places_come_from_the_transit_section_and_home_falls_back_to_the_weather_location() -> None:
    """transit.home wins; otherwise home.weather lat/lng; school has no fallback."""
    weather = {"home": {"weather": {"latitude": 48.48, "longitude": -123.38}}}
    assert runtime_config(weather).transit_places == {"home": "48.48,-123.38"}
    both = {**weather, "transit": {"home": " 1 Example St ", "school": "UVic"}}
    assert runtime_config(both).transit_places == {"home": "1 Example St", "school": "UVic"}
    assert runtime_config({"transit": None}).transit_places == {}


def test_here_is_the_macs_location_as_a_lat_lng_waypoint_and_is_said_with_its_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'here': the fix goes to Google as latLng; from says this Mac, the place and the metres."""
    seen = _serve(monkeypatch)
    out = _call({"from": "Here", "to": "home"}, here=fix_with())
    assert payload(sent_request(seen)[0])["origin"] == HERE_POINT
    assert out["from"] == "here (this Mac, Ring Rd, Saanich, ±40 m)"
    assert out["to"] == "home"
    seen.clear()
    out = _call({"from": "school", "to": "here"}, here=fix_with(place=None))
    assert payload(sent_request(seen)[0])["destination"] == HERE_POINT
    assert out["to"] == "here"


def test_here_without_a_reading_is_a_tool_error_that_says_to_ask_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denied, timed out, or no reader at all: the Mac's location could not be read; ask him."""
    seen = serve_routes(monkeypatch)

    denied = FixedHere(error=HereUnavailable("this Mac's location could not be read (denied)"))
    for reader in (denied, None):
        with pytest.raises(ToolError, match=r"location.*ask the user") as e:
            _call({"from": "here", "to": "home"}, here=reader)
        assert e.value.code == "location_unavailable"
    assert seen == []


def test_here_is_the_default_start_when_he_names_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`from` is optional; left out, the trip starts at Here and is said from there."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=lambda: PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "bus")
    assert "from" in definition.input_schema["properties"]
    assert "from" not in definition.input_schema.get("required", ())
    seen = _serve(monkeypatch)
    out = _call({"to": "home"}, here=fix_with())
    assert payload(sent_request(seen)[0])["origin"] == HERE_POINT
    assert out["from"] == "here (this Mac, Ring Rd, Saanich, ±40 m)"


def test_where_am_i_reads_the_mac_with_or_without_a_place_and_is_a_quiet_read_only_tool() -> None:
    """No arguments, L1, no confirmation, no wait line; absent without a reader, key not needed."""
    names = {d.name for d in build_default_registry().get_definitions()}
    assert "where_am_i" not in names
    assert _call({}, here=fix_with(), tool="where_am_i") == {
        "summary": "He is at Ring Rd, Saanich (this Mac).",
        "place": "Ring Rd, Saanich",
        "lat": 48.46,
        "lng": -123.31,
        "accuracy_m": 40.4,
        "source": "this Mac",
    }
    assert _call({}, here=fix_with(place=None), tool="where_am_i")["place"] is None
    registry = build_default_registry(here=fix_with())
    (definition,) = (d for d in registry.get_definitions() if d.name == "where_am_i")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    assert "Not for a bus trip (bus)" in definition.description
    with pytest.raises(ToolError, match="ask the user where he is"):
        _call({}, here=FixedHere(error=HereUnavailable("no location")), tool="where_am_i")


def test_the_helper_output_parses_with_and_without_a_place_and_each_failure_has_a_reason() -> None:
    """The one JSON line: a fix (place optional), or denied / a CoreLocation error / junk."""
    full = '{"lat":48.46,"lng":-123.31,"accuracy_m":40,"age_s":1,"place":"Ring Rd, Saanich"}'
    assert mac_location.parse_helper_output(full) == mac_location.MacLocation(
        48.46, -123.31, 40.0, "Ring Rd, Saanich"
    )
    bare = '{"lat":48.46,"lng":-123.31,"accuracy_m":40,"age_s":1}'
    assert mac_location.parse_helper_output(bare).place is None
    for text, reason in (
        ('{"error":"denied"}', "denied"),
        ('{"error":"timeout"}', "timeout"),
        ("", "no usable answer"),
        ('{"lat":1}', "no usable answer"),
    ):
        with pytest.raises(mac_location.LocationUnavailable, match=reason):
            mac_location.parse_helper_output(text)


def test_the_reader_opens_the_app_with_a_temp_file_and_reads_what_it_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run open -W -n <app> --args <file>; a fake app writes it; a timeout has its own reason."""
    monkeypatch.setattr(sys, "platform", "darwin")
    app = Path("/nonexistent/JarvisWhere.app")
    monkeypatch.setattr(mac_location, "ensure_where_app", lambda: app)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: float) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert kwargs["timeout"] == 12.0
        Path(argv[-1]).write_text(json.dumps({**FIX, "age_s": 2}))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    got = mac_location.read_mac_location()
    assert got == mac_location.MacLocation(48.46, -123.31, 40.4, "Ring Rd, Saanich")
    assert calls[0][:5] == ["/usr/bin/open", "-W", "-n", str(app), "--args"]

    def hang(argv: list[str], **_kwargs: float) -> None:
        raise subprocess.TimeoutExpired(argv, 12.0)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(mac_location.LocationUnavailable, match="timed out"):
        mac_location.read_mac_location()
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(mac_location.LocationUnavailable, match="cannot read its location"):
        mac_location.read_mac_location()


def test_the_runtime_offers_this_macs_reader_only_on_a_mac_and_never_on_a_brain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role all on a Mac: a reader; on Linux: none; a brain: none, whatever the machine."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _here_location("all") is not None
    assert _here_location("brain") is None  # ADR 0198: its phone, not a Mac
    monkeypatch.setattr(sys, "platform", "linux")
    assert _here_location("all") is None
    assert _here_location("brain") is None


def test_the_other_ways_there_come_back_beside_the_bus_and_a_failed_one_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0205: DRIVE and WALK ask only for time and distance; a failure leaves that out."""
    seen = _serve(
        monkeypatch,
        modes={
            "DRIVE": {"routes": [{"duration": "725s", "distanceMeters": 9340}]},
            "WALK": OSError("HTTP 500"),
        },
    )
    out = _call({"from": "school", "to": "home"})
    assert out["modes"] == {"drive": {"minutes": 12, "km": 9.3}}
    assert len(out["options"]) == 2  # the bus answer is as it was
    for travel in ("DRIVE", "WALK"):
        request, timeout = sent_request(seen, travel)
        assert request.get_header("X-goog-fieldmask") == "routes.duration,routes.distanceMeters"
        assert payload(request) == {
            "origin": SCHOOL_POINT, "destination": HOME_POINT, "travelMode": travel,
        }
        assert timeout == 5.0


def test_no_other_way_answering_leaves_no_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both fail or return nothing: the result is the bus options alone."""
    _serve(monkeypatch, modes={"DRIVE": OSError("down"), "WALK": {}})
    assert "modes" not in _call({"from": "school", "to": "home"})


def test_the_answer_puts_up_one_bus_card_that_a_newer_answer_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0205, 0343: the card is the ask card's slot with `trip` rows and modes; newer replaces.

    Each row keeps its HH:MM for the phone and carries the same times as epoch ms.
    """
    _serve(monkeypatch, modes={"WALK": {"routes": [{"duration": "2400s", "distanceMeters": 3100}]}})

    def slot() -> PendingClarification:
        found = PendingClarification.from_events(
            iter_events_of_types(_LOG["conn"], CLARIFICATION_EVENT_TYPES)
        )
        assert found is not None
        return found

    _call({"from": "school", "to": "home"})
    first = slot()
    assert first.waiting
    assert first.fields == ()
    assert first.trip is not None
    assert first.trip["offer_id"] == first.clarification_id
    assert first.trip["modes"] == {"walk": {"minutes": 40, "km": 3.1}}
    assert [
        (r["index"], r["route"], r["leave_at"], r["leave_ms"]) for r in first.trip["options"]
    ] == [(0, "26", "17:06", local_ms(17, 6)), (1, "28 → 6", "17:16", local_ms(17, 16))]
    _call({"from": "school", "to": "home"})
    assert slot().clarification_id != first.clarification_id


def test_an_answer_for_another_day_leaves_nothing_to_pin_until_one_for_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: tomorrow's times are said, not offered; `pin` says so and takes no older list."""

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        tomorrow = body["departureTime"] > "2026-10-07"  # 17:00 on the 7th is 00:00Z on the 8th
        return routes_with(_placed(bus_route("26", "41:12" if tomorrow else "17:12")))

    _serve(monkeypatch, answer=answer)
    offers = TransitOffers()
    _call({"to": "home"}, here=fix_with(), offers=offers)
    out = _call({"to": "home", "at": "2026-10-07 17:00"}, here=fix_with(), offers=offers)
    assert out["options"][0]["legs"][0]["departs"] == "tomorrow (Wed), 5:12 pm"
    with pytest.raises(ToolError, match="The last list was for tomorrow") as caught:
        _call({"pin": [1]}, offers=offers)
    assert caught.value.code == "no_plan"
    assert "cannot be pinned" in str(caught.value)
    assert list(iter_events_of_types(_LOG["conn"], ("departure.pinned",))) == []
    _call({"to": "home"}, here=fix_with(), offers=offers)  # today's list is pinnable again
    assert _call({"pin": [1]}, offers=offers)["pinned"] is True


def _times(seen: list[Any]) -> list[str | None]:
    """The departureTime of each TRANSIT request made, in order."""
    bodies = [json.loads(s[0].data) for s in seen]
    return [b.get("departureTime") for b in bodies if b["travelMode"] == "TRANSIT"]


@pytest.mark.parametrize(
    ("departs", "spare", "verdict"),
    [
        ("17:06", 2, "easy"),
        ("17:05", 1, "tight"),
        ("17:04", 0, "tight"),
        ("17:03", -1, "tight"),
        ("17:02", -2, "missed"),
        ("17:00", -4, "missed"),
    ],
)
def test_a_named_route_is_asked_from_ten_minutes_ago_and_judged_by_its_spare_minutes(
    monkeypatch: pytest.MonkeyPatch, departs: str, spare: int, verdict: str
) -> None:
    """Now 17:00, walk 4: spare = minutes to the bus - 4; easy >= 2, missed <= -2; 28 not first."""
    seen = _serve(
        monkeypatch, answer=routes_with(bus_route("28", "17:20", ride=5), bus_route("39", departs))
    )
    out = _call({"from": "here", "to": "UVic", "route": "39"}, here=fix_with())
    assert _times(seen) == ["2026-10-06T23:50:00Z"]  # 16:50 PDT
    assert out["named_route"] == {
        "route": "39",
        "board_stop": "Stop of 39",
        "departs": f"5:{departs[3:]} pm",
        "source": "scheduled",
        "walk_min": 4,
        "spare_min": spare,
        "verdict": verdict,
    }
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39", "28"]
    assert out["options"][0]["spare_min"] == spare
    # The card offers only what he can still catch: a missed 39 is said, not offered.
    assert [r["route"] for r in _card()["options"]] == (
        ["28"] if verdict == "missed" else ["39", "28"]
    )


def test_the_named_route_is_matched_by_number_case_and_the_next_one_comes_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'26a路' or '26A' is route 26A; a gone bus is skipped; next is the following 26A."""
    _serve(
        monkeypatch,
        answer=routes_with(
            bus_route("26A", "16:58"),
            bus_route("26A", "17:21"),
            bus_route("26A", "17:08"),
            bus_route("26", "17:03"),
        ),
    )
    out = _call({"from": "here", "to": "home", "route": "26a路"}, here=fix_with())
    named = out["named_route"]
    assert (named["route"], named["departs"], named["verdict"]) == ("26A", "5:08 pm", "easy")
    assert named["next"] == {
        "board_stop": "Stop of 26A", "departs": "5:21 pm", "source": "scheduled",
        "leave_at": "5:17 pm",
    }


def test_the_named_bus_stays_first_even_when_it_arrives_much_later_than_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The far-fetched cut never drops it: a 39 arriving 50 min after the 28 is still first."""
    _serve(
        monkeypatch,
        answer=routes_with(bus_route("28", "17:06", ride=10), bus_route("39", "17:07", ride=60)),
    )
    out = _call({"from": "here", "to": "home", "route": "39"}, here=fix_with())
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39", "28"]


def test_a_route_google_does_not_have_says_none_and_the_plain_now_answer_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No such bus from the earlier start: a second request without a time is made.

    Buses already out of reach at a normal walk (the earlier start's) are not offered.
    """

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        if "departureTime" in body:
            return routes_with(bus_route("28", "16:55"), bus_route("26", "17:08"))
        return routes_with(bus_route("28", "17:12"))

    seen = _serve(monkeypatch, answer=answer)
    out = _call({"from": "here", "to": "home", "route": "39"}, here=fix_with())
    assert _times(seen) == ["2026-10-06T23:50:00Z", None]
    assert out["named_route"] == {
        "route": "39",
        "verdict": "none",
        "note": "no route 39 bus found then",
    }
    assert [o["legs"][0]["route"] for o in out["options"]] == ["28"]


def test_the_plain_answer_may_hold_the_named_bus_and_depart_at_is_not_moved_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second request finds the 39: it is the named route; depart_at: one request, as is."""

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        if "departureTime" in body:
            return routes_with(bus_route("28", "17:12"))
        return routes_with(bus_route("39", "17:09"))

    seen = _serve(monkeypatch, answer=answer)
    out = _call({"from": "here", "to": "home", "route": "39"}, here=fix_with())
    assert out["named_route"]["departs"] == "5:09 pm"
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39"]
    seen.clear()
    out = _call({"from": "here", "to": "home", "route": "39", "at": "17:30"}, here=fix_with())
    assert _times(seen) == ["2026-10-07T00:30:00Z"]
    assert out["named_route"]["verdict"] == "none"


def test_without_a_route_the_request_looks_back_and_there_is_no_named_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No route: asked from ten minutes ago too (ADR 0265), one bus request, no named_route."""
    seen = _serve(monkeypatch)
    out = _call({"from": "school", "to": "home"})
    assert _times(seen) == ["2026-10-06T23:50:00Z"]
    assert "named_route" not in out


def test_without_a_route_the_bus_he_can_run_for_comes_first_marked_then_the_two_he_can_catch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Now 09:29 (the live miss): Google plans from now and loses the 09:31; the look-back keeps it.

    Walk is 4 min, so the 09:31 has spare -2: missed at a normal walk, marked as the one he can
    run for (ADR 0343); a bus that left 4 min ago is dropped, and of two near misses only the
    later stays. Then the first he can catch and the one after, said in that order. Three
    requests in all: one bus, one drive, one walk.
    """
    everything = routes_with(
        bus_route("10", "09:25"),
        bus_route("12", "09:30"),
        bus_route("31", "09:31"),
        bus_route("26", "09:54"),
        bus_route("27", "09:44"),
    )

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        return everything if "departureTime" in body else routes_with(bus_route("26", "09:54"))

    seen = _serve(monkeypatch, answer=answer)
    out = _call(
        {"from": "here", "to": "UVic"}, here=fix_with(),
        now=datetime(2026, 10, 6, 9, 29, tzinfo=ZONE),
    )
    assert _times(seen) == ["2026-10-06T16:19:00Z"]  # 09:19 PDT
    assert len(seen) == 3
    assert [
        (o["legs"][0]["route"], o["spare_min"], o["verdict"], o.get("run_for", False))
        for o in out["options"]
    ] == [("31", -2, "missed", True), ("27", 11, "easy", False), ("26", 21, "easy", False)]
    assert "named_route" not in out
    assert out["summary"] == (
        "Option 1: the 31 from Stop of 31 at 9:31 am (in 2 min) (scheduled), only by"
        " running. To school (3800 Finnerty Rd,"
        " Victoria, BC), option 2: leave 9:40 am (in 11 min), take the 27 from Stop of 27 at"
        " 9:44 am (in 15 min) (scheduled), arrive 10:04 am (24 min). Then option 3: the"
        " 26 from Stop of 26 at 9:54 am (in 25 min) (scheduled), arrive 10:14 am."
    )


def test_the_named_bus_takes_its_matched_trips_live_time_and_the_next_from_the_timetable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0342: a named bus is matched like any other; its next comes from the store.

    The 39 matched at Google's stop and time leaves at its live 17:09; with the feed down it
    keeps its scheduled 17:07; with no trip of route 39 in the store, Google's 17:06 stands.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("39", "17:06"))))
    args = {"from": "here", "to": "home", "route": "39"}
    feed = realtime(("T39", "B1", local_ms(17, 9) + 20_000))
    live = _live(
        tmp_path / "live", _trip("T39", "39", "17:06:00", "17:26:00"),
        _trip("T39b", "39", "17:36:00", "17:56:00"), feed=lambda: feed,
    )
    named = _call(args, here=fix_with(), bus_live=live)["named_route"]
    assert (named["departs"], named["source"], named["spare_min"], named["verdict"]) == (
        "5:09 pm", "live", 5, "easy",
    )
    assert named["next"] == {
        "board_stop": "Stop of 39", "departs": "5:36 pm", "source": "scheduled",
        "leave_at": "5:32 pm",
    }

    def down() -> bytes:
        raise OSError

    dead = _live(tmp_path / "dead", _trip("T39", "39", "17:07:00", "17:27:00"), feed=down)
    named = _call(args, here=fix_with(), bus_live=dead)["named_route"]
    assert (named["departs"], named["source"], named["spare_min"]) == ("5:07 pm", "scheduled", 3)
    other = _live(tmp_path / "other", _trip("T7", "7", "17:06:00", "17:26:00"))
    named = _call(args, here=fix_with(), bus_live=other)["named_route"]
    assert (named["departs"], named["source"], named["spare_min"]) == ("5:06 pm", "scheduled", 2)


def test_each_bus_leg_is_one_matched_trip_and_one_feed_snapshot_times_the_whole_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0342: each leg takes its own trip's time and source; the feed is read once per answer.

    The card's rows carry the matched trip's id and the same minute as epoch ms.
    """
    _serve(
        monkeypatch,
        answer=routes_with(_placed(bus_route("26", "17:12")), _placed(bus_route("28", "17:20"))),
    )
    reads: list[int] = []
    feed = realtime(("T28", "B1", local_ms(17, 22) + 30_000))

    def serve() -> bytes:
        reads.append(1)
        return feed

    live = _live(
        tmp_path, _trip("T26", "26", "17:13:00", "17:33:00"),
        _trip("T28", "28", "17:20:00", "17:40:00"), feed=serve,
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert [
        (leg["route"], leg["departs"], leg["source"])
        for option in out["options"] for leg in option["legs"]
    ] == [("26", "5:13 pm", "scheduled"), ("28", "5:22 pm", "live")]
    assert len(reads) == 1
    assert [
        (r["route"], r["departs"], r["departs_ms"], r["bus_trip_id"]) for r in _card()["options"]
    ] == [("26", "17:13", local_ms(17, 13), "T26"), ("28", "17:22", local_ms(17, 22), "T28")]


@pytest.mark.parametrize(("north_m", "matched"), [(35, True), (45, False)])
def test_a_timetable_stop_is_googles_stop_within_40_m_and_not_beyond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, north_m: int, *, matched: bool,
) -> None:
    """The export's stop 35 m from Google's: its trip's 17:13; 45 m: no match, Google's 17:12."""
    board = gtfs.StopSpec("B1", "Stop of 26", BOARD[0] + north_m / 111_195, BOARD[1])
    assert (metres(BOARD, (board.lat, board.lng)) <= 40) is matched  # the radius
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:12"))))
    live = _live(tmp_path, _trip("T26", "26", "17:13:00", "17:33:00"), stops=(board, ALIGHT_STOP))
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    leg = out["options"][0]["legs"][0]
    assert (leg["departs"], leg["source"]) == ("5:13 pm" if matched else "5:12 pm", "scheduled")
    assert _card()["options"][0]["bus_trip_id"] == ("T26" if matched else None)


@pytest.mark.parametrize(
    ("scheduled", "live_at", "departs", "source", "trip"),
    [
        ("17:21:00", None, "5:21 pm", "scheduled", "T26"),  # 9 min from Google's 17:12
        ("17:23:00", None, "5:12 pm", "scheduled", None),  # 11 min: no match, Google's time
        ("17:23:00", (17, 21), "5:21 pm", "live", "T26"),  # its live time is within ten minutes
        ("17:23:00", (17, 24), "5:12 pm", "scheduled", None),  # neither time is
    ],
)
def test_a_trip_more_than_ten_minutes_from_googles_time_by_schedule_and_live_is_no_match(  # noqa: PLR0913 - the case's columns.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheduled: str,
    live_at: tuple[int, int] | None, departs: str, source: str, trip: str | None,
) -> None:
    """ADR 0342: a trip matches within ten minutes by its scheduled or its live time, else none.

    With no match the bus keeps Google's time and is said as scheduled, whatever the feed says.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:12"))))
    feed = realtime(("T26", "B1", local_ms(*live_at))) if live_at else b""
    live = _live(tmp_path, _trip("T26", "26", scheduled, "17:40:00"), feed=lambda: feed)
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    leg = out["options"][0]["legs"][0]
    assert (leg["departs"], leg["source"]) == (departs, source)
    assert _card()["options"][0]["bus_trip_id"] == trip


def test_one_live_time_per_bus_in_the_answer_the_card_row_and_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google 17:13, scheduled 17:13, live 17:12:40: 5:12 pm (live) everywhere, rounded down."""
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:13"))))
    feed = realtime(("T26", "B1", local_ms(17, 12) + 40_000))
    live = _live(tmp_path, _trip("T26", "26", "17:13:00", "17:33:00"), feed=lambda: feed)
    offers = TransitOffers()
    out = _call({"to": "home"}, here=fix_with(), bus_live=live, offers=offers)
    leg = out["options"][0]["legs"][0]
    assert (leg["departs"], leg["source"]) == ("5:12 pm", "live")
    assert "the 26 from Stop of 26 at 5:12 pm (in 12 min) (live)" in out["summary"]
    row = _card()["options"][0]
    assert (row["leave_at"], row["departs"], row["departs_ms"], row["bus_trip_id"]) == (
        "17:08", "17:12", local_ms(17, 12), "T26",
    )
    pinned = _call({"pin": [1]}, here=fix_with(), bus_live=live, offers=offers)
    assert pinned["trips"] == [{
        "route": "26", "board_stop": "Stop of 26", "leave": "5:08 pm (in 8 min)",
        "departs": "5:12 pm",
    }]
    (event,) = iter_events_of_types(_LOG["conn"], ("departure.pinned",))
    (trip,) = event.payload["trips"]
    assert (trip["departs_at_ms"], trip["bus_trip_id"]) == (local_ms(17, 12), "T26")


def test_a_pinned_bus_after_midnight_is_said_with_its_day_and_no_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 23:50 the 00:10 is on today's list but tomorrow's by the clock: no "at" before its day."""
    late = datetime(2026, 10, 6, 23, 50, tzinfo=ZONE)
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "24:10"))))
    live = _live(tmp_path, _trip("T26", "26", "24:10:00", "24:30:00"))
    offers = TransitOffers()
    _call({"to": "home"}, here=fix_with(), bus_live=live, offers=offers, now=late)
    pinned = _call({"pin": [1]}, here=fix_with(), bus_live=live, offers=offers, now=late)
    assert pinned["summary"] == (
        "Pinned on the notch: the 26 from Stop of 26: leave tomorrow (Wed), 12:06 am, bus"
        " tomorrow (Wed), 12:10 am. It rings at the leave time."
    )


DAY_MS = 24 * 3_600_000


@pytest.mark.parametrize(
    ("predicted_ms", "start_date", "departs", "source"),
    [
        (local_ms(17, 15), "", "tomorrow (Wed), 5:12 pm", "scheduled"),
        (local_ms(17, 15), "20261006", "tomorrow (Wed), 5:12 pm", "scheduled"),
        (local_ms(17, 15) + DAY_MS, "", "tomorrow (Wed), 5:15 pm", "live"),
        (local_ms(17, 15) + DAY_MS, "20261007", "tomorrow (Wed), 5:15 pm", "live"),
    ],
    ids=["today's, undated", "today's, dated", "tomorrow's, undated", "tomorrow's, dated"],
)
def test_a_prediction_counts_for_its_own_service_day_not_for_the_same_trip_on_another(  # noqa: PLR0913 - the case's columns.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    predicted_ms: int, start_date: str, departs: str, source: str,
) -> None:
    """ADR 0342: a trip runs every day under one id; asked at 4:30 pm for tomorrow's 5:12 pm bus.

    The feed's prediction for today's run of that trip is not tomorrow's bus's time. A prediction
    dated with its start_date counts on that day; an undated one only near the bus's scheduled time.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "41:12"))))
    feed = realtime(("T26", "B1", predicted_ms), start_date=start_date)
    live = _live(tmp_path, _trip("T26", "26", "17:12:00", "17:32:00"), feed=lambda: feed)
    out = _call(
        {"to": "home", "at": "2026-10-07 17:00"}, here=fix_with(), bus_live=live,
        now=datetime(2026, 10, 6, 16, 30, tzinfo=ZONE),
    )
    leg = out["options"][0]["legs"][0]
    assert (leg["departs"], leg["source"]) == (departs, source)


def test_the_next_bus_of_tomorrow_does_not_take_todays_run_of_its_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the last 26 today the timetable's next is tomorrow's first, the 6:00 am trip.

    The feed still holds this morning's run of it, three minutes late: not tomorrow's time.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:12"))))
    feed = realtime(("T26first", "B1", local_ms(6, 3)))
    live = _live(
        tmp_path, _trip("T26", "26", "17:12:00", "17:32:00"),
        _trip("T26first", "26", "06:00:00", "06:20:00"), feed=lambda: feed,
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert [(o["legs"][0]["departs"], o["legs"][0]["source"]) for o in out["options"]] == [
        ("5:12 pm", "scheduled"), ("tomorrow (Wed), 6:00 am", "scheduled"),
    ]
    assert [r["bus_trip_id"] for r in _card()["options"]] == ["T26", "T26first"]


# A stop of the 26 before the board stop, a kilometre off; two stops 22 m apart where he changes.
BEFORE_STOP = gtfs.StopSpec("X0", "Before the stop", BOARD[0] - 0.01, BOARD[1], "10009")
CHANGE = (48.47, -123.29)
CHANGE_STOPS = (
    gtfs.StopSpec("X1", "Change A", *CHANGE, "10003"),
    gtfs.StopSpec("X2", "Change B", CHANGE[0] + 0.0002, CHANGE[1], "10004"),
)


def _feed_of(*updates: tuple[str, str, tuple[int, int]]) -> bytes:
    """A feed predicting each ``(trip, stop, (hour, minute))`` departure, today."""
    return realtime(*((trip, stop, local_ms(*at)) for trip, stop, at in updates))


@pytest.mark.parametrize(
    ("updates", "departs", "source", "arrives"),
    [
        ([("T26", "B1", (17, 20))], "5:20 pm", "live", "5:40 pm"),
        ([("T26", "X0", (17, 9))], "5:16 pm", "live", "5:36 pm"),
        ([("T26", "A1", (17, 40))], "5:12 pm", "scheduled", "5:40 pm"),
        ([("T26", "B1", (17, 20)), ("T26", "A1", (17, 38))], "5:20 pm", "live", "5:38 pm"),
        ([("T26", "X0", (17, 9)), ("T26", "B1", (17, 20))], "5:20 pm", "live", "5:40 pm"),
    ],
    ids=[
        "late at his stop, nothing after", "late at an earlier stop", "only a later stop",
        "his stop and the last", "an earlier stop and his",
    ],
)
def test_a_bus_late_at_one_stop_is_late_at_the_stops_after_it_unless_the_feed_says_otherwise(  # noqa: PLR0913 - the case's columns.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    updates: list[tuple[str, str, tuple[int, int]]], departs: str, source: str, arrives: str,
) -> None:
    """The feed's delay at a stop holds for the stops after it until its next prediction.

    The 26 is scheduled 5:05 pm at an earlier stop, 5:12 at his stop and 5:32 at the last; a
    stop the feed does not predict takes the delay of the nearest earlier stop it does, a
    prediction at the stop itself is kept, and nothing is carried back to an earlier stop.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:12"))))
    trip = gtfs.TripSpec(
        "T26", "26", (("X0", "17:05:00"), ("B1", "17:12:00"), ("A1", "17:32:00")),
        headsign="Downtown",
    )
    feed = _feed_of(*updates)
    live = _live(
        tmp_path, trip, feed=lambda: feed, stops=(BEFORE_STOP, BOARD_STOP, ALIGHT_STOP),
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    leg = out["options"][0]["legs"][0]
    assert (leg["departs"], leg["source"], leg["arrives"]) == (departs, source, arrives)
    assert out["options"][0]["arrive_at"] == arrives


def _with_a_change(*, later_39: bool) -> tuple[dict[str, Any], tuple[gtfs.TripSpec, ...]]:
    """Google's 26 then 39 with a change, a plain 28 beside it, and the trips of the store."""
    first = _placed(bus_route("26", "17:12", ride=13), alight=CHANGE)
    second = _placed(bus_route("39", "17:30", ride=20, at=(CHANGE[0] + 0.0002, CHANGE[1])))
    steps = [
        *first["legs"][0]["steps"], {"staticDuration": "120s", "travelMode": "WALK"},
        *second["legs"][0]["steps"][1:],
    ]
    answer = routes_with(
        {"legs": [{"steps": steps}]}, _placed(bus_route("28", "17:20", ride=40)),
    )
    trips = (
        gtfs.TripSpec("T26", "26", (("B1", "17:12:00"), ("X1", "17:25:00")), headsign="Downtown"),
        gtfs.TripSpec("T39", "39", (("X2", "17:30:00"), ("A1", "17:50:00")), headsign="UVic"),
        gtfs.TripSpec("T28", "28", (("B1", "17:20:00"), ("A1", "18:00:00")), headsign="Downtown"),
        *((gtfs.TripSpec(
            "T39b", "39", (("X2", "17:50:00"), ("A1", "18:10:00")), headsign="UVic",
        ),) if later_39 else ()),
    )
    return answer, trips


@pytest.mark.parametrize(
    ("updates", "later_39", "options"),
    [
        ([], True, [[("26", "5:12 pm", "scheduled"), ("39", "5:30 pm", "scheduled")],
                    [("28", "5:20 pm", "scheduled")]]),
        ([("T26", "B1", (17, 22)), ("T26", "X1", (17, 35))], True,
         [[("28", "5:20 pm", "scheduled")],
          [("26", "5:22 pm", "live"), ("39", "5:50 pm", "scheduled")]]),
        ([("T26", "B1", (17, 22)), ("T26", "X1", (17, 35))], False,
         [[("28", "5:20 pm", "scheduled")]]),
    ],
    ids=["on time", "late: the next 39 from the change", "late: no later 39, the option goes"],
)
def test_a_change_the_late_first_bus_makes_miss_takes_the_next_bus_of_the_second_route_or_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    updates: list[tuple[str, str, tuple[int, int]]], *, later_39: bool,
    options: list[list[tuple[str, str, str]]],
) -> None:
    """The 26 reaches the change at 5:25 pm and the 39 leaves it at 5:30 pm; the 26 is 10 min late.

    Reaching the change at 5:35 pm, the 39 of 5:30 pm is gone: the option holds the next 39 from
    that stop (5:50 pm), and with none later it is dropped, leaving the plain 28 in the answer.
    A change that still works is left as it is.
    """
    answer, trips = _with_a_change(later_39=later_39)
    _serve(monkeypatch, answer=answer)
    feed = _feed_of(*updates)
    live = _live(
        tmp_path, *trips, feed=lambda: feed, stops=(BOARD_STOP, *CHANGE_STOPS, ALIGHT_STOP),
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert [
        [(leg["route"], leg["departs"], leg["source"]) for leg in option["legs"]]
        for option in out["options"]
    ] == options


def test_with_no_later_bus_from_google_the_next_is_the_timetables_on_the_same_stop_and_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: Google has one 26; the one after is the store's next 26 the same way.

    A 26 the other way calls at the same stop at the same minute, its alight stop before the
    board stop: it is neither the match nor the next.
    """
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:12"))))
    back = gtfs.TripSpec(
        "T26back", "26", (("A1", "17:02:00"), ("B1", "17:12:00")), direction=1, headsign="Hill",
    )
    live = _live(
        tmp_path, back, _trip("T26", "26", "17:12:00", "17:32:00"),
        _trip("T26n", "26", "17:42:00", "18:02:00"),
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert [(o["legs"][0]["departs"], o["legs"][0]["source"]) for o in out["options"]] == [
        ("5:12 pm", "scheduled"), ("5:42 pm", "scheduled"),
    ]
    assert [r["bus_trip_id"] for r in _card()["options"]] == ["T26", "T26n"]
    assert out["summary"].endswith(
        "Then option 2: the 26 from Stop of 26 at 5:42 pm (in 42 min) (scheduled), arrive"
        " 6:02 pm."
    )


def _runs(*trips: tuple[str, str, str]) -> list[gtfs.TripSpec]:
    """Trips ``(id, route, HH:MM at the board stop)``, each 20 min on to the alight stop."""
    runs = []
    for trip_id, route, at in trips:
        end = int(at[:2]) * 60 + int(at[3:]) + 20
        runs.append(_trip(trip_id, route, f"{at}:00", f"{end // 60:02d}:{end % 60:02d}:00"))
    return runs


_MISSED_BY_MORE_THAN_FIVE = [
    pytest.param(
        [("26", "17:03")],
        [("T26", "26", "17:03"), ("T26n", "26", "17:33")],
        [("26", "5:33 pm", "easy", False)],
        id="the-only-bus-missed-by-nine-takes-its-routes-next-trip",
    ),
    pytest.param(
        [("26", "17:03")],
        [("T26", "26", "17:03"), ("T26b", "26", "17:08"), ("T26c", "26", "17:10"),
         ("T26d", "26", "17:11")],
        [("26", "5:11 pm", "tight", False)],
        id="next-trips-still-missed-are-passed-over-for-the-first-he-can-catch",
    ),
    pytest.param(
        [("26", "17:03"), ("39", "17:04")],
        [("T26", "26", "17:03"), ("T26n", "26", "17:33"),
         ("T39", "39", "17:04"), ("T39n", "39", "17:23")],
        [("39", "5:23 pm", "easy", False), ("26", "5:33 pm", "easy", False)],
        id="each-missed-route-gives-its-next-trip-in-departure-order",
    ),
    pytest.param(
        [("26", "17:08"), ("39", "17:03")],
        [("T26", "26", "17:08"), ("T26n", "26", "17:38"),
         ("T39", "39", "17:03"), ("T39n", "39", "17:20")],
        [("26", "5:08 pm", "missed", True), ("39", "5:20 pm", "easy", False),
         ("26", "5:38 pm", "easy", False)],
        id="the-run-for-bus-then-the-next-trips-of-every-missed-route",
    ),
]


@pytest.mark.parametrize(("buses", "trips", "expected"), _MISSED_BY_MORE_THAN_FIVE)
def test_when_every_bus_google_gave_is_missed_the_timetables_next_trips_are_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    buses: list[tuple[str, str]], trips: list[tuple[str, str, str]],
    expected: list[tuple[str, str, str, bool]],
) -> None:
    """ADR 0343: a 12 min walk misses each bus at 17:00; the next trip comes from the store.

    Whatever the miss, each missed route's next trip is looked up (the first one he can still
    catch), so "no bus route" is only for a route the timetable has nothing later for.
    """
    _serve(monkeypatch, answer=routes_with(*(
        _placed(bus_route(line, at, walk=12)) for line, at in buses
    )))
    live = _live(tmp_path, *_runs(*trips))
    out = _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert [
        (o["legs"][0]["route"], o["legs"][0]["departs"], o["verdict"], o.get("run_for", False))
        for o in out["options"]
    ] == expected


def test_every_bus_missed_and_none_later_in_the_timetable_is_no_bus_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: the only bus is missed by nine and the store has no later trip: nothing to give."""
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "17:03", walk=12))))
    live = _live(tmp_path, *_runs(("T26", "26", "17:03")))
    with pytest.raises(ToolError, match="No bus route found") as caught:
        _call({"to": "home"}, here=fix_with(), bus_live=live)
    assert caught.value.code == "not_found"


def test_a_time_today_is_said_as_a_clock_and_another_days_with_its_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: at 23:50 the 00:10 is tomorrow's, said so; the card keeps 00:10 and its epoch.

    The 00:10 is the 24:10 of today's service day, still to come, so no last 26 is said to have
    left.
    """
    late = datetime(2026, 10, 6, 23, 50, tzinfo=ZONE)
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "24:10"))))
    live = _live(
        tmp_path, _trip("T26a", "26", "23:20:00", "23:40:00"),
        _trip("T26", "26", "24:10:00", "24:30:00"),
    )
    out = _call({"to": "home"}, here=fix_with(), bus_live=live, now=late)
    first = out["options"][0]
    assert (first["leave_at"], first["legs"][0]["departs"], first["arrive_at"]) == (
        "tomorrow (Wed), 12:06 am", "tomorrow (Wed), 12:10 am", "tomorrow (Wed), 12:30 am",
    )
    assert "last_today" not in out
    assert out["summary"] == (
        "To home, option 1: leave tomorrow (Wed), 12:06 am, take the 26 from Stop of 26"
        " tomorrow (Wed), 12:10 am (scheduled), arrive tomorrow (Wed), 12:30 am (24 min). Then"
        " option 2: the 26 from Stop of 26 tomorrow (Wed), 11:20 pm (scheduled), arrive"
        " tomorrow (Wed), 11:40 pm."
    )
    row = _card()["options"][0]
    wednesday = datetime(2026, 10, 7, 0, 10, tzinfo=ZONE)
    assert (row["departs"], row["departs_ms"]) == ("00:10", int(wednesday.timestamp() * 1000))


def test_the_last_bus_had_left_is_said_when_the_next_one_is_of_a_later_service_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0343: at 23:50 the last 26 left at 11:20 pm and the next is the first of tomorrow's."""
    late = datetime(2026, 10, 6, 23, 50, tzinfo=ZONE)
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "29:30"))))
    live = _live(tmp_path, *_runs(("T26a", "26", "23:20"), ("T26", "26", "05:30")))
    out = _call({"to": "home"}, here=fix_with(), bus_live=live, now=late)
    assert out["options"][0]["legs"][0]["departs"] == "tomorrow (Wed), 5:30 am"
    assert out["last_today"] == {"route": "26", "left_at": "11:20 pm"}
    assert out["summary"].endswith("The last 26 had left at 11:20 pm.")


def test_the_last_bus_that_left_yesterday_is_said_with_its_day_and_no_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line with no Wednesday service, at 00:20 on Wednesday: the 26 last left yesterday."""
    wednesday = datetime(2026, 10, 7, 0, 20, tzinfo=ZONE)
    _serve(monkeypatch, answer=routes_with(_placed(bus_route("26", "53:30"))))
    export = gtfs.Export(
        (BOARD_STOP, ALIGHT_STOP), tuple(_runs(("T26a", "26", "23:20"), ("T26", "26", "05:30"))),
        services=(gtfs.ServiceSpec("WK", "1101111"),),
    )
    live = bus_live(tmp_path, export, lambda: b"")
    out = _call({"to": "home"}, here=fix_with(), bus_live=live, now=wednesday)
    assert out["options"][0]["legs"][0]["departs"] == "tomorrow (Thu), 5:30 am"
    assert out["last_today"] == {"route": "26", "left_at": "yesterday (Tue) 11:20 pm"}
    assert out["summary"] == (
        "To home, option 1: leave tomorrow (Thu), 5:26 am, take the 26 from Stop of 26"
        " tomorrow (Thu), 5:30 am (scheduled), arrive tomorrow (Thu), 5:50 am (24 min). Then"
        " option 2: the 26 from Stop of 26 tomorrow (Thu), 11:20 pm (scheduled), arrive tomorrow"
        " (Thu), 11:40 pm. The last 26 had left yesterday (Tue) 11:20 pm."
    )


def test_arrive_by_keeps_the_last_three_and_says_the_latest_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0288: five buses arrive by 18:30; the last three by leave time are listed."""
    buses = [bus_route("26", at) for at in ("17:20", "17:35", "17:50", "18:00", "18:05")]
    seen = _serve(monkeypatch, answer=routes_with(*buses))
    out = _call({"to": "home", "arrive_by": "18:30"}, here=fix_with())
    assert payload(sent_request(seen)[0])["arrivalTime"] == "2026-10-07T01:30:00Z"
    assert [o["legs"][0]["departs"] for o in out["options"]] == ["5:50 pm", "6:00 pm", "6:05 pm"]
    assert out["arrive_by"] == "6:30 pm"
    assert out["summary"].startswith(
        "To home, option 3: leave 6:01 pm (in 61 min), take the 26 from Stop of 26 at 6:05 pm"
    )
    assert out["summary"].endswith("3 options are listed.")


@pytest.mark.parametrize("word", ["UVic", "u vic", " University  of Victoria ", "uvic"])
def test_uvic_is_the_saved_school(monkeypatch: pytest.MonkeyPatch, word: str) -> None:
    """The campus by name is the saved school address, looked up and sent as its point."""
    seen = serve_routes(monkeypatch)
    searches = serve_text_search(monkeypatch, FOUND)
    _call({"from": "here", "to": word}, here=fix_with())
    assert [payload(r)["textQuery"] for r in searches] == [PLACES["school"]]
    assert payload(sent_request(seen)[0])["destination"] == SCHOOL_POINT


def test_words_for_a_place_are_one_search_near_here_by_distance_said_back_by_name_and_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0346: one Text Search biased to Here (50 km), nearest first; its place is the trip's end.

    The destination is said back by its name and address, and no second search is made.
    """
    seen = serve_routes(monkeypatch)
    searches = serve_text_search(monkeypatch, FOUND)
    out = _call({"to": "CARSA"}, here=fix_with())
    (search,) = searches
    assert payload(search) == {
        "textQuery": "CARSA",
        "maxResultCount": 1,
        "languageCode": "en",
        "rankPreference": "DISTANCE",
        "locationBias": {
            "circle": {"center": {"latitude": 48.46, "longitude": -123.31}, "radius": 50000.0},
        },
    }
    assert payload(sent_request(seen)[0])["destination"] == CARSA_POINT
    assert out["summary"].startswith("To CARSA (3964 Gordon Head Rd, Victoria, BC), option 1:")
    assert out["destination"] == {
        "name": "CARSA", "lat": 48.4665, "lng": -123.3093,
        "address": "3964 Gordon Head Rd, Victoria, BC", "place_id": "ChIJcarsa00000000000",
        "kind": "place",
    }
    assert "note" not in out


def test_a_routes_miss_is_asked_once_and_a_place_google_cannot_find_asks_routes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """School found but no route: one bus request, the error; words with no place: not_found."""
    seen = _serve(monkeypatch, answer={})
    with pytest.raises(ToolError, match="No bus route found"):
        _call({"from": "here", "to": "school"}, here=fix_with())
    assert len(_times(seen)) == 1
    seen.clear()
    with pytest.raises(ToolError, match="Google found no place for 'Nowhere'") as e:
        _call({"from": "here", "to": "Nowhere"}, here=fix_with())
    assert e.value.code == "not_found"
    assert seen == []


def test_the_schema_names_the_bus_number_and_drops_day_and_place_id() -> None:
    """route: a bus he names, a number by example; `day` and `place_id` are not arguments."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=lambda: PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "bus")
    properties = definition.input_schema["properties"]
    assert properties["route"]["type"] == "string"
    assert "'39'" in properties["route"]["description"]
    assert {"day", "place_id"}.isdisjoint(properties)


def test_a_plain_ask_defaults_to_home_from_here_and_numbers_the_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bus()` with no ride is the buses home from here; the sentence leads, options are 1..N."""
    seen = _serve(monkeypatch)
    out = _call({}, here=fix_with())
    body = payload(sent_request(seen)[0])
    assert body["destination"] == HOME_POINT
    assert body["origin"] == HERE_POINT
    assert next(iter(out)) == "summary"
    assert out["summary"] == (
        "To home, option 1: leave 5:06 pm (in 6 min), take the 26 from Shelbourne at McKenzie at"
        " 5:12 pm (in 12 min) (scheduled), arrive 5:48 pm (42 min). Then option 2: the 28,"
        " then the 6 from Shelbourne at Cedar Hill X at 5:20 pm (in 20 min) (scheduled), arrive"
        " 6:03 pm."
    )
    assert [o["option"] for o in out["options"]] == [1, 2]


def test_a_named_bus_is_answered_in_the_sentence_with_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first sentence is about THAT bus: how tight, its source, and the next when it is gone."""
    _serve(monkeypatch, answer=routes_with(bus_route("39", "17:03"), bus_route("39", "17:21")))
    out = _call({"to": "home", "route": "39"}, here=fix_with())
    assert out["summary"].startswith(
        "The 39 leaves Stop of 39 at 5:03 pm (in 3 min) (scheduled); it is tight, he must walk"
        " fast"
    )
    _serve(monkeypatch, answer=routes_with(bus_route("39", "17:00"), bus_route("39", "17:21")))
    out = _call({"to": "home", "route": "39"}, here=fix_with())
    assert out["summary"].startswith(
        "The 39 leaves Stop of 39 at 5:00 pm (scheduled); he has missed it."
    )
    assert "The next one is at 5:21 pm (scheduled)." in out["summary"]
    _serve(monkeypatch, answer=routes_with(bus_route("28", "17:20")))
    assert _call({"to": "home", "route": "39"}, here=fix_with())["summary"].startswith(
        "Google has no route 39 bus then."
    )


def test_a_named_bus_and_the_next_one_of_another_day_are_said_with_their_day_and_no_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A time that carries its day is said without "at": the clock keeps it, the day does not."""
    late = datetime(2026, 10, 6, 23, 50, tzinfo=ZONE)
    _serve(monkeypatch, answer=routes_with(bus_route("39", "23:50"), bus_route("39", "24:21")))
    out = _call({"to": "home", "route": "39"}, here=fix_with(), now=late)
    assert out["summary"].startswith(
        "The 39 leaves Stop of 39 at 11:50 pm (scheduled); he has missed it. The next one is"
        " tomorrow (Wed), 12:21 am (scheduled)."
    )
    _serve(monkeypatch, answer=routes_with(bus_route("39", "24:21")))
    out = _call({"to": "home", "route": "39"}, here=fix_with(), now=late)
    assert out["summary"].startswith(
        "The 39 leaves Stop of 39 tomorrow (Wed), 12:21 am (scheduled); he has room"
    )


def test_an_argument_from_the_old_tools_is_refused_with_the_names_to_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`destination` was transit's, `day` and `place_id` the old bus's: each error names the new."""
    seen = _serve(monkeypatch)
    refused = (
        r"destination is not an argument\. The arguments are arrive_by, at, from, option, pin,"
        r" ride, route, stop, to, vehicle_id"
    )
    with pytest.raises(ToolError, match=refused) as e:
        _call({"destination": "home"})
    assert e.value.code == "invalid_arguments"
    with pytest.raises(ToolError, match=r"day is at=, which takes a local date and time"):
        _call({"to": "home", "day": "2026-10-08"})
    with pytest.raises(ToolError, match=r"place_id is to=, which takes a place_id from map_search"):
        _call({"place_id": "ChIJcarsa00000000000"})
    assert seen == []
