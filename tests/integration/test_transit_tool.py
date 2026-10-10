"""ADR 0189: the model reads the bus times in one call, from a hand-built Routes answer."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution import transit_tool
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime import _here_location, _transit_places
from jarvis.shared import CallerPrincipal
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.projections import CLARIFICATION_EVENT_TYPES, PendingClarification
from jarvis.surface import mac_location

if TYPE_CHECKING:
    from collections.abc import Callable

# Built by hand from Google's documented computeRoutes TRANSIT response (not a recording):
# Saanich to downtown Victoria on 2026-10-07, four routes, the fourth to be cut.
HANDBUILT = json.loads(
    (
        Path(__file__).parents[1]
        / "fixtures"
        / "google_routes"
        / "saanich-to-downtown-handbuilt.json"
    ).read_text()
)
FAKE_KEY = "test-key-not-real"
PLACES = {"home": "48.48,-123.38", "school": "University of Victoria, Victoria, BC"}


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Exception | None = None,
    answer: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] = HANDBUILT,
    modes: dict[str, Any] | None = None,
) -> list[Any]:
    """Serve ``answer`` (or what it makes of the request body) to TRANSIT.

    DRIVE and WALK get ``modes[mode]`` (none: no routes).
    """
    seen: list[Any] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        seen.append((request, timeout))
        sent = json.loads(request.data)
        travel = sent["travelMode"]
        if travel != "TRANSIT":
            reply = (modes or {}).get(travel, {})
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        if fail is not None:
            raise fail
        return io.BytesIO(json.dumps(answer(sent) if callable(answer) else answer).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    now = datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("America/Vancouver"))
    monkeypatch.setattr(transit_tool, "_now", lambda: now)
    return seen


def _sent(seen: list[Any], travel: str = "TRANSIT") -> tuple[urllib.request.Request, float]:
    """The one request made for ``travel``, with its timeout."""
    (found,) = (s for s in seen if json.loads(s[0].data)["travelMode"] == travel)
    return found  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _event_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversation card is an event in the log the tool is handed."""
    monkeypatch.setitem(_LOG, "conn", open_event_log(tmp_path / "events.db"))
    # A direct call has no `action.running` event to link the card to.
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)


_LOG: dict[str, Any] = {}
FIX = {"lat": 48.46, "lng": -123.31, "accuracy_m": 40.4, "place": "Ring Rd, Saanich"}


def _fix(**changes: object) -> Callable[[], dict[str, Any]]:
    return lambda: {**FIX, **changes}


def _call(
    args: dict[str, Any],
    places: dict[str, str] | None = None,
    here: Callable[[], dict[str, Any]] | None = None,
    tool: str = "transit",
    bus_live: Callable[[str, tuple[float, float], int], int | None] | None = None,
) -> dict[str, Any]:
    registry = build_default_registry(
        bus_live=bus_live,
        transit_api_key=FAKE_KEY,
        transit_places=PLACES if places is None else places,
        here_location=here,
    )
    (definition,) = (d for d in registry.get_definitions() if d.name == tool)
    ctx = SimpleNamespace(conn=_LOG["conn"], action_id="A1")
    return dict(definition.handler(args, ctx))  # type: ignore[arg-type,call-arg,misc]


def test_the_answer_is_plain_words_in_local_time_with_three_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave time, walk, each bus, arrival and total, in Vancouver time; far-fetched ones cut."""
    _serve(monkeypatch)
    out = _call({"origin": "school", "destination": "home"})
    options = out["options"]
    # The third arrives 20 min after the first and the fourth is past the cap: both cut.
    assert len(options) == 2
    assert options[0] == {
        "leave_at": "17:06",
        "walk_to_first_stop_min": 6,
        "legs": [
            {
                "route": "26",
                "headsign": "Downtown",
                "board_stop": "Shelbourne at McKenzie",
                "departs": "17:12",
                "alight_stop": "Douglas at Fort",
                "arrives": "17:41",
            }
        ],
        "walk_at_end_min": 7,
        "arrive_at": "17:48",
        "total_min": 42,
        "spare_min": 6,  # leaves 17:12, now 17:00, 6 min walk
    }
    assert [leg["route"] for leg in options[1]["legs"]] == ["28", "6"]
    assert options[1]["arrive_at"] == "18:03"
    assert [o["spare_min"] for o in options] == [6, 16]  # on every option


def test_one_post_with_the_key_a_tight_field_mask_and_the_saved_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request: headers, body, timeout; home is lat/lng, school an address, depart_at in UTC."""
    seen = _serve(monkeypatch)
    _call({"origin": "Home", "destination": "school", "depart_at": "17:30"})
    assert len(seen) == 3  # the bus, and the other ways there at the same time
    request, timeout = _sent(seen)
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
    ]
    assert json.loads(request.data) == {
        "origin": {"location": {"latLng": {"latitude": 48.48, "longitude": -123.38}}},
        "destination": {"address": "University of Victoria, Victoria, BC"},
        "travelMode": "TRANSIT",
        "computeAlternativeRoutes": True,
        "departureTime": "2026-10-07T00:30:00Z",
    }
    assert timeout == 5.0


def test_any_other_text_goes_to_google_as_an_address_and_now_sends_no_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free text is an address; without depart_at no departureTime is sent."""
    seen = _serve(monkeypatch)
    _call({"origin": "school", "destination": "Mayfair Mall"})
    body = json.loads(_sent(seen)[0].data)
    assert body["destination"] == {"address": "Mayfair Mall"}
    assert "departureTime" not in body


def test_the_tool_is_gated_on_the_key_and_is_a_quiet_up_front_read_only_model_tool() -> None:
    """No key, no tool; with one it is on the menu up front, L1, no confirmation, no wait line."""
    assert "transit" not in {d.name for d in build_default_registry().get_definitions()}
    assert "transit" not in {
        d.name
        for d in build_default_registry(transit_api_key="", transit_places=PLACES).get_definitions()
    }
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "transit")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    assert "school" in definition.description
    assert _dispatch_key("transit") is None


def test_an_unsaved_place_is_a_tool_error_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """School or home unset: the error says so; 'work' is no saved word, only address text."""
    seen = _serve(monkeypatch)
    with pytest.raises(ToolError, match="school address isn't saved yet"):
        _call({"origin": "home", "destination": "school"}, places={"home": "48.48,-123.38"})
    with pytest.raises(ToolError, match="home address isn't saved yet"):
        _call({"origin": "home", "destination": "school"}, places={"school": "UVic"})
    assert seen == []
    _call({"origin": "school", "destination": "work"})
    assert json.loads(_sent(seen)[0].data)["destination"] == {"address": "work"}


@pytest.mark.parametrize(
    ("fault", "code"),
    [(TimeoutError(), "network_error"), (OSError("HTTP 403"), "network_error")],
)
def test_a_failed_request_is_a_short_tool_error(
    monkeypatch: pytest.MonkeyPatch, fault: Exception, code: str
) -> None:
    """Timeout or HTTP error: one short reason."""
    _serve(monkeypatch, fail=fault)
    with pytest.raises(ToolError, match="transit unavailable") as caught:
        _call({"origin": "home", "destination": "school"})
    assert caught.value.code == code


def test_no_routes_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty answer is not an invented timetable."""
    _serve(monkeypatch, answer={})
    with pytest.raises(ToolError, match="no transit route") as caught:
        _call({"origin": "home", "destination": "school"})
    assert caught.value.code == "not_found"


def test_places_come_from_the_transit_section_and_home_falls_back_to_the_weather_location() -> None:
    """transit.home wins; otherwise home.weather lat/lng; school has no fallback."""
    weather = {"home": {"weather": {"latitude": 48.48, "longitude": -123.38}}}
    assert _transit_places(weather) == {"home": "48.48,-123.38"}
    both = {**weather, "transit": {"home": " 1 Example St ", "school": "UVic"}}
    assert _transit_places(both) == {"home": "1 Example St", "school": "UVic"}
    assert _transit_places({"transit": None}) == {}


def test_here_is_the_macs_location_as_a_lat_lng_waypoint_and_is_said_with_its_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'here': the fix goes to Google as latLng; from says this Mac, the place and the metres."""
    seen = _serve(monkeypatch)
    out = _call({"origin": "Here", "destination": "home"}, here=_fix())
    assert json.loads(_sent(seen)[0].data)["origin"] == {
        "location": {"latLng": {"latitude": 48.46, "longitude": -123.31}}
    }
    assert out["from"] == "here (this Mac, Ring Rd, Saanich, ±40 m)"
    assert out["to"] == "home"
    out = _call({"origin": "school", "destination": "here"}, here=_fix(place=None))
    assert out["to"] == "here (this Mac, ±40 m)"


def test_here_without_a_reading_is_a_tool_error_that_says_to_ask_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denied, timed out, or no reader at all: the Mac's location could not be read; ask him."""
    seen = _serve(monkeypatch)

    def denied() -> dict[str, Any]:
        reason = "location access is denied"
        raise mac_location.LocationUnavailable(reason)

    for reader in (denied, None):
        with pytest.raises(ToolError, match=r"Mac's location could not be read.*ask the user") as e:
            _call({"origin": "here", "destination": "home"}, here=reader)
        assert e.value.code == "location_unavailable"
    assert seen == []


def test_the_description_makes_here_the_default_origin() -> None:
    """The model is told the origin is always 'here', never a guessed saved place."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "transit")
    assert "origin is always 'here'" in definition.description
    assert "never guess his start" in definition.description
    assert "this Mac" in definition.description


def test_where_am_i_reads_the_mac_with_or_without_a_place_and_is_a_quiet_read_only_tool() -> None:
    """No arguments, L1, no confirmation, no wait line; absent without a reader, key not needed."""
    names = {d.name for d in build_default_registry().get_definitions()}
    assert "where_am_i" not in names
    assert _call({}, here=_fix(), tool="where_am_i") == {
        "place": "Ring Rd, Saanich",
        "lat": 48.46,
        "lng": -123.31,
        "accuracy_m": 40.4,
        "source": "this Mac",
    }
    assert _call({}, here=_fix(place=None), tool="where_am_i")["place"] is None
    registry = build_default_registry(here_location=_fix())
    (definition,) = (d for d in registry.get_definitions() if d.name == "where_am_i")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    assert "transit" in definition.description
    assert _dispatch_key("where_am_i") is None
    with pytest.raises(ToolError, match="ask the user where he is"):
        _call({}, here=dict, tool="where_am_i")


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
    out = _call({"origin": "school", "destination": "home"})
    assert out["modes"] == {"drive": {"minutes": 12, "km": 9.3}}
    assert len(out["options"]) == 2  # the bus answer is as it was
    for travel in ("DRIVE", "WALK"):
        request, timeout = _sent(seen, travel)
        assert request.get_header("X-goog-fieldmask") == "routes.duration,routes.distanceMeters"
        assert json.loads(request.data) == {
            "origin": {"address": "University of Victoria, Victoria, BC"},
            "destination": {"location": {"latLng": {"latitude": 48.48, "longitude": -123.38}}},
            "travelMode": travel,
        }
        assert timeout == 5.0


def test_no_other_way_answering_leaves_no_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both fail or return nothing: the result is the bus options alone."""
    _serve(monkeypatch, modes={"DRIVE": OSError("down"), "WALK": {}})
    assert "modes" not in _call({"origin": "school", "destination": "home"})


def test_the_answer_puts_up_one_bus_card_that_a_newer_answer_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0205: the card is the ask card's slot with `trip` rows and modes; newer replaces."""
    _serve(monkeypatch, modes={"WALK": {"routes": [{"duration": "2400s", "distanceMeters": 3100}]}})

    def slot() -> PendingClarification:
        found = PendingClarification.from_events(
            iter_events_of_types(_LOG["conn"], CLARIFICATION_EVENT_TYPES)
        )
        assert found is not None
        return found

    _call({"origin": "school", "destination": "home"})
    first = slot()
    assert first.waiting
    assert first.fields == ()
    assert first.trip is not None
    assert first.trip["offer_id"] == first.clarification_id
    assert first.trip["modes"] == {"walk": {"minutes": 40, "km": 3.1}}
    assert [(r["index"], r["route"], r["leave_at"]) for r in first.trip["options"]] == [
        (0, "26", "17:06"),
        (1, "28 → 6", "17:16"),
    ]
    _call({"origin": "school", "destination": "home"})
    assert slot().clarification_id != first.clarification_id


def _bus(
    line: str,
    departs: str,
    *,
    walk: int = 4,
    ride: int = 20,
    at: tuple[float, float] = (48.46, -123.3),
) -> dict[str, Any]:
    """One route: a walk, one bus ``line`` leaving at local ``departs`` (HH:MM, 2026-10-06)."""
    def utc(hhmm: str) -> str:
        hour, minute = map(int, hhmm.split(":"))
        total = hour * 60 + minute + 7 * 60  # PDT is UTC-7
        return f"2026-10-{6 + total // 1440:02d}T{total % 1440 // 60:02d}:{total % 60:02d}:00Z"

    end = f"{int(departs[:2]) * 60 + int(departs[3:]) + ride}"
    arrives = f"{int(end) // 60:02d}:{int(end) % 60:02d}"
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
                                "departureStop": {
                                    "name": f"Stop of {line}",
                                    "location": {
                                        "latLng": {"latitude": at[0], "longitude": at[1]}
                                    },
                                },
                                "departureTime": utc(departs),
                                "arrivalStop": {"name": "UVic Exchange"},
                                "arrivalTime": utc(arrives),
                            },
                            "headsign": "UVic",
                            "transitLine": {"nameShort": line},
                        },
                    },
                ]
            }
        ]
    }


def _ms(hour: int, minute: int) -> int:
    at = datetime(2026, 10, 6, hour, minute, tzinfo=ZoneInfo("America/Vancouver"))
    return int(at.timestamp() * 1000)


def _routes(*buses: dict[str, Any]) -> dict[str, Any]:
    return {"routes": list(buses)}


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
    seen = _serve(monkeypatch, answer=_routes(_bus("28", "17:20", ride=5), _bus("39", departs)))
    out = _call({"origin": "here", "destination": "UVic", "route": "39"}, here=_fix())
    assert _times(seen) == ["2026-10-06T23:50:00Z"]  # 16:50 PDT
    assert out["named_route"] == {
        "route": "39",
        "board_stop": "Stop of 39",
        "departs": departs,
        "walk_min": 4,
        "spare_min": spare,
        "verdict": verdict,
    }
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39", "28"]
    assert out["options"][0]["spare_min"] == spare


def test_the_named_route_is_matched_by_number_case_and_the_next_one_comes_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'26a路' or '26A' is route 26A; a gone bus is skipped; next is the following 26A."""
    _serve(
        monkeypatch,
        answer=_routes(
            _bus("26A", "16:58"), _bus("26A", "17:21"), _bus("26A", "17:08"), _bus("26", "17:03")
        ),
    )
    out = _call({"origin": "here", "destination": "home", "route": "26a路"}, here=_fix())
    named = out["named_route"]
    assert (named["route"], named["departs"], named["verdict"]) == ("26A", "17:08", "easy")
    assert named["next"] == {"board_stop": "Stop of 26A", "departs": "17:21", "leave_at": "17:17"}


def test_the_named_bus_stays_first_even_when_it_arrives_much_later_than_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The far-fetched cut never drops it: a 39 arriving 50 min after the 28 is still first."""
    _serve(monkeypatch, answer=_routes(_bus("28", "17:06", ride=10), _bus("39", "17:07", ride=60)))
    out = _call({"origin": "here", "destination": "home", "route": "39"}, here=_fix())
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39", "28"]


def test_a_route_google_does_not_have_says_none_and_the_plain_now_answer_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No such bus from the earlier start: a second request without a time is made.

    Buses already out of reach at a normal walk (the earlier start's) are not offered.
    """
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        if "departureTime" in body:
            return _routes(_bus("28", "16:55"), _bus("26", "17:08"))
        return _routes(_bus("28", "17:12"))

    seen = _serve(monkeypatch, answer=answer)
    out = _call({"origin": "here", "destination": "home", "route": "39"}, here=_fix())
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
            return _routes(_bus("28", "17:12"))
        return _routes(_bus("39", "17:09"))

    seen = _serve(monkeypatch, answer=answer)
    out = _call({"origin": "here", "destination": "home", "route": "39"}, here=_fix())
    assert out["named_route"]["departs"] == "17:09"
    assert [o["legs"][0]["route"] for o in out["options"]] == ["39"]
    seen.clear()
    out = _call(
        {"origin": "here", "destination": "home", "route": "39", "depart_at": "17:30"}, here=_fix()
    )
    assert _times(seen) == ["2026-10-07T00:30:00Z"]
    assert out["named_route"]["verdict"] == "none"


def test_without_a_route_the_request_and_the_answer_are_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No route: no departureTime, no named_route."""
    seen = _serve(monkeypatch)
    out = _call({"origin": "school", "destination": "home"})
    assert _times(seen) == [None]
    assert "named_route" not in out


def test_the_live_time_replaces_googles_for_the_named_bus_and_a_failure_keeps_googles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected BC Transit reader: called with the route, the stop's place and Google's time."""
    _serve(monkeypatch, answer=_routes(_bus("39", "17:06", at=(48.46, -123.3))))
    got: list[Any] = []

    def live(route: str, stop: tuple[float, float], around_ms: int) -> int | None:
        got.append((route, stop, around_ms))
        return _ms(17, 9)

    args = {"origin": "here", "destination": "home", "route": "39"}
    named = _call(args, here=_fix(), bus_live=live)["named_route"]
    assert got == [("39", (48.46, -123.3), _ms(17, 6))]
    assert (named["departs"], named["spare_min"], named["verdict"], named["live"]) == (
        "17:09", 5, "easy", True,
    )

    def broken(*_: object) -> int | None:
        down = OSError()
        raise down

    named = _call(args, here=_fix(), bus_live=broken)["named_route"]
    assert (named["departs"], named["spare_min"], "live" in named) == ("17:06", 2, False)
    named = _call(args, here=_fix(), bus_live=lambda *_: None)["named_route"]
    assert named["departs"] == "17:06"


@pytest.mark.parametrize("word", ["UVic", "u vic", " University  of Victoria ", "uvic"])
def test_uvic_is_the_saved_school(monkeypatch: pytest.MonkeyPatch, word: str) -> None:
    """The campus by name goes to Google as the saved school address."""
    seen = _serve(monkeypatch)
    _call({"origin": "here", "destination": word}, here=_fix())
    assert json.loads(_sent(seen)[0].data)["destination"] == {"address": PLACES["school"]}


def test_a_text_destination_not_found_is_tried_again_with_the_city(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'CARSA' alone is not found: once more with the home's city (default Victoria, BC)."""
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        found = body["destination"].get("address") == "CARSA, Victoria, BC"
        return HANDBUILT if found else {}

    seen = _serve(monkeypatch, answer=answer)
    out = _call({"origin": "here", "destination": "CARSA"}, here=_fix())
    assert out["to"] == "CARSA, Victoria, BC"
    asked = [json.loads(s[0].data) for s in seen]
    assert [b["destination"] for b in asked if b["travelMode"] == "TRANSIT"] == [
        {"address": "CARSA"},
        {"address": "CARSA, Victoria, BC"},
    ]

    def saanich(body: dict[str, Any]) -> dict[str, Any]:
        return HANDBUILT if body["destination"].get("address") == "CARSA, Saanich, BC" else {}

    _serve(monkeypatch, answer=saanich)
    places = {**PLACES, "home": "1 Example St, Saanich, BC V8X 1A1"}
    assert _call({"origin": "here", "destination": "CARSA"}, places=places, here=_fix())["to"] == (
        "CARSA, Saanich, BC"
    )


def test_a_saved_place_or_a_second_miss_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """School (a saved address) is not found: one request; text twice not found: the error."""
    seen = _serve(monkeypatch, answer={})
    with pytest.raises(ToolError, match="no transit route"):
        _call({"origin": "here", "destination": "school"}, here=_fix())
    assert len(_times(seen)) == 1
    seen.clear()
    with pytest.raises(ToolError, match="no transit route"):
        _call({"origin": "here", "destination": "Nowhere"}, here=_fix())
    assert len(_times(seen)) == 2


def test_the_description_teaches_the_named_bus_and_the_misheard_number() -> None:
    """route, the verdicts, 'one call', no invented target, and speech-recognition mishearing."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "transit")
    text = definition.description
    needles = (
        "pass it as route",
        "named_route",
        "39 degrees",
        "nonsense words",
        "never call transit again",
        "never invent a target arrival",
        "absent",
    )
    for needle in needles:
        assert needle in text
    assert "route" in definition.input_schema["properties"]
