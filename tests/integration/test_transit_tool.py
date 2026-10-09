"""ADR 0189: the model reads the bus times in one call, from a hand-built Routes answer."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution import transit_tool
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime import _here_location, _transit_places
from jarvis.shared import CallerPrincipal
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
    answer: dict[str, Any] = HANDBUILT,
) -> list[Any]:
    seen: list[Any] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        seen.append((request, timeout))
        if fail is not None:
            raise fail
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    now = datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("America/Vancouver"))
    monkeypatch.setattr(transit_tool, "_now", lambda: now)
    return seen


FIX = {"lat": 48.46, "lng": -123.31, "accuracy_m": 40.4, "place": "Ring Rd, Saanich"}


def _fix(**changes: object) -> Callable[[], dict[str, Any]]:
    return lambda: {**FIX, **changes}


def _call(
    args: dict[str, Any],
    places: dict[str, str] | None = None,
    here: Callable[[], dict[str, Any]] | None = None,
    tool: str = "transit",
) -> dict[str, Any]:
    registry = build_default_registry(
        transit_api_key=FAKE_KEY,
        transit_places=PLACES if places is None else places,
        here_location=here,
    )
    (definition,) = (d for d in registry.get_definitions() if d.name == tool)
    return dict(definition.handler(args, None))  # type: ignore[arg-type,call-arg,misc]


def test_the_answer_is_plain_words_in_local_time_with_three_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave time, walk, each bus, arrival and total, in Vancouver time; the fourth route is cut."""
    _serve(monkeypatch)
    out = _call({"origin": "school", "destination": "home"})
    options = out["options"]
    assert len(options) == 3
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
    }
    assert [leg["route"] for leg in options[1]["legs"]] == ["28", "6"]
    assert options[1]["arrive_at"] == "18:03"


def test_one_post_with_the_key_a_tight_field_mask_and_the_saved_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request: headers, body, timeout; home is lat/lng, school an address, depart_at in UTC."""
    seen = _serve(monkeypatch)
    _call({"origin": "Home", "destination": "school", "depart_at": "17:30"})
    ((request, timeout),) = seen
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
    body = json.loads(seen[0][0].data)
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
    assert json.loads(seen[0][0].data)["destination"] == {"address": "work"}


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
    assert json.loads(seen[0][0].data)["origin"] == {
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
    """The model is told an unstated origin is 'here'."""
    registry = build_default_registry(transit_api_key=FAKE_KEY, transit_places=PLACES)
    (definition,) = (d for d in registry.get_definitions() if d.name == "transit")
    assert "origin is 'here'" in definition.description
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


def test_the_runtime_offers_this_macs_reader_only_on_a_mac_and_a_brain_reads_its_phone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Role all on a Mac: a reader; on Linux: none. A brain: the phone's reader on any machine."""
    log = tmp_path / "events.db"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _here_location("all", log) is not None
    assert _here_location("brain", log) is not None
    monkeypatch.setattr(sys, "platform", "linux")
    assert _here_location("all", log) is None
    assert _here_location("brain", log) is not None  # ADR 0198: its phone, not a Mac
