"""ADR 0188: the model reads the home's weather in one call, from a recorded Open-Meteo answer."""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime.home import weather, weather_lookup
from jarvis.shared import CallerPrincipal

# Recorded from api.open-meteo.com for 48.48, -123.38 at 2026-10-06 22:00 local (PDT).
RECORDED = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "open_meteo" / "saanich-2026-10-06.json").read_text()
)
PLACE = {"latitude": 48.48, "longitude": -123.38}


def _serve(monkeypatch: pytest.MonkeyPatch, *, reachable: bool = True) -> list[str]:
    asked: list[str] = []

    def urlopen(url: str, *, timeout: float) -> io.BytesIO:
        asked.append(f"{url} timeout={timeout}")
        if not reachable:
            raise TimeoutError
        return io.BytesIO(json.dumps(RECORDED).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def _call(place: dict[str, Any] | None) -> dict[str, Any]:
    registry = build_default_registry(weather_lookup=weather_lookup(place))
    (definition,) = (d for d in registry.get_definitions() if d.name == "weather")
    return dict(definition.handler({}, None))  # type: ignore[arg-type,call-arg,misc]


def test_the_report_is_plain_words_for_now_today_tomorrow_and_twelve_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call: now, today, tomorrow and the next twelve hours, in words and local time."""
    asked = _serve(monkeypatch)
    report = _call(PLACE)
    assert report["place"] == "home"
    assert report["now"] == {"temp_c": 11.0, "condition": "overcast"}
    assert report["today"] == {
        "date": "2026-10-06", "high_c": 16.1, "low_c": 10.9,
        "rain_chance_pct": 1, "condition": "fog",
    }
    assert report["tomorrow"] == {
        "date": "2026-10-07", "high_c": 20.1, "low_c": 9.7,
        "rain_chance_pct": 2, "condition": "overcast",
    }
    hours = report["next_hours"]
    assert len(hours) == 12
    assert hours[0] == {
        "at": "2026-10-06 22:00", "temp_c": 11.0, "condition": "overcast", "rain_chance_pct": 0,
    }
    assert hours[-1]["at"] == "2026-10-07 09:00"
    assert "latitude=48.48&longitude=-123.38" in asked[0]
    assert "timeout=4.0" in asked[0]


def test_a_named_place_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """``home.weather.name`` is what the report calls the place."""
    _serve(monkeypatch)
    assert _call({**PLACE, "name": "Saanich"})["place"] == "Saanich"


def test_the_tool_is_on_the_models_menu_up_front_read_only_and_only_with_a_location() -> None:
    """Registered up front for the model only, read-only, and absent without a location."""
    registry = build_default_registry(weather_lookup=weather_lookup(PLACE))
    (definition,) = (d for d in registry.get_definitions() if d.name == "weather")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    assert "web_search" in definition.description
    assert "weather" not in {d.name for d in build_default_registry().get_definitions()}
    assert weather_lookup(None) is None


def test_an_unreachable_forecast_is_a_tool_error_the_model_can_recover_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open-Meteo down: an error that says to use web_search."""
    _serve(monkeypatch, reachable=False)
    with pytest.raises(ToolError, match="use web_search") as caught:
        _call(PLACE)
    assert caught.value.code == "network_error"


def test_the_weather_call_says_no_wait_line() -> None:
    """A weather call is quiet at dispatch; a search still says its line."""
    assert _dispatch_key("weather") is None
    assert _dispatch_key("web_search") == "commentary.tool.web"


def test_the_home_view_still_reads_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The home's own forecast reads the same recorded answer."""
    _serve(monkeypatch)
    home = weather(48.48, -123.38)
    assert home["now_c"] == 11.0
    assert home["high_c"] == 16.1
    assert len(home["hours"]) == 4
