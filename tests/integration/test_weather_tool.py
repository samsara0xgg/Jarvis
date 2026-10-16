"""ADR 0279: the model reads the weather at any place and day in one call, over a faked HTTP layer.

The Open-Meteo answers are built from the query the tool sent (the days, the place), so a request
that asks for the wrong days or the wrong place reads wrong data here.
"""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime.home import weather, weather_lookup
from jarvis.shared import CallerPrincipal

RECORDED = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "open_meteo" / "saanich-2026-10-06.json").read_text()
)
PLACE = {"latitude": 48.48, "longitude": -123.38}
TORONTO = {
    "name": "Toronto", "latitude": 43.70, "longitude": -79.42, "admin1": "Ontario",
    "country": "Canada", "timezone": "America/Toronto",
}
TORONTO_OH = {
    "name": "Toronto", "latitude": 40.46, "longitude": -80.60, "admin1": "Ohio",
    "country": "United States",
}
TORONTO_KS = {
    "name": "Toronto", "latitude": 37.79, "longitude": -95.95, "admin1": "Kansas",
    "country": "United States",
}
TORONTO_VILLE = {"name": "Torontoville", "latitude": 1.0, "longitude": 2.0, "country": "Nowhere"}
GEOCODED = {
    ("Toronto", "en"): [TORONTO, TORONTO_OH, TORONTO_VILLE, TORONTO_KS],
    ("多伦多", "en"): [TORONTO],
    ("Nowhere Town", "en"): [],
}
NOW = datetime.now(UTC)


def _offset_s(longitude: float) -> int:
    return -25200 if longitude < -100 else -14400  # Pacific, otherwise Eastern


class Net:
    """Open-Meteo, faked: records every URL, answers from the query, fails on request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, down: tuple[str, ...] = ()) -> None:
        """Replace ``urlopen``; ``down`` lists the hosts that time out."""
        self.asked: list[str] = []
        self.down = down
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, url: str, *, timeout: float) -> io.BytesIO:
        self.asked.append(f"{url} timeout={timeout}")
        host = urlparse(url).hostname or ""
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if host in self.down:
            raise TimeoutError
        if host.startswith("geocoding"):
            found = GEOCODED[query["name"], query["language"]]
            body: dict[str, Any] = {"results": found} if found else {"generationtime_ms": 0.1}
        elif host.startswith("air-quality"):
            body = _series(query, air=True)
        else:
            body = _series(query, air=False)
        return io.BytesIO(json.dumps(body).encode())

    def hosts(self) -> list[str]:
        """The host of each request, in order."""
        return [urlparse(one.split(" ")[0]).hostname or "" for one in self.asked]


def _series(query: dict[str, str], *, air: bool) -> dict[str, Any]:
    """The days and hours Open-Meteo would answer for ``query`` (synthetic values)."""
    offset = _offset_s(float(query["longitude"]))
    local = NOW + timedelta(seconds=offset)
    if "start_date" in query:
        first = date.fromisoformat(query["start_date"])
        last = date.fromisoformat(query["end_date"])
    else:
        first, last = local.date(), local.date() + timedelta(days=int(query["forecast_days"]) - 1)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]

    def midnight(day: date) -> int:
        return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) - offset

    hours = [midnight(day) + h * 3600 for day in days for h in range(24)]
    zone = {"timezone": "America/Toronto" if offset == -14400 else "America/Vancouver",
            "utc_offset_seconds": offset}
    now = int(NOW.timestamp()) // 900 * 900
    if air:
        return {
            **zone,
            "current": {"time": now, "us_aqi": 42, "pm2_5": 8.5, "pm10": 12.0},
            "hourly": {
                "time": hours,
                "us_aqi": [30 + h % 24 for h in range(len(hours))],
                "pm2_5": [5.0 + h % 24 for h in range(len(hours))],
                "pm10": [None] * len(hours),
            },
        }
    return {
        **zone,
        "current": {
            "time": now, "temperature_2m": 11.0, "apparent_temperature": 9.5,
            "relative_humidity_2m": 80, "weather_code": 3, "precipitation": 0.2,
            "wind_speed_10m": 14.0, "wind_gusts_10m": 30.0, "wind_direction_10m": 315,
        },
        "hourly": {
            "time": hours,
            "temperature_2m": [10.0 + h % 24 / 4 for h in range(len(hours))],
            "apparent_temperature": [9.0] * len(hours),
            "weather_code": [61] * len(hours),
            "precipitation_probability": [40] * len(hours),
            "precipitation": [0.1] * len(hours),
            "wind_speed_10m": [12.0] * len(hours),
        },
        "daily": {
            "time": [midnight(day) for day in days],
            "weather_code": [63] * len(days),
            "temperature_2m_max": [15.0 + n for n in range(len(days))],
            "temperature_2m_min": [8.0 + n for n in range(len(days))],
            "apparent_temperature_max": [14.0] * len(days),
            "apparent_temperature_min": [6.0] * len(days),
            "precipitation_probability_max": [None if d < local.date() else 70 for d in days],
            "precipitation_sum": [3.5] * len(days),
            "snowfall_sum": [0.0] * len(days),
            "wind_speed_10m_max": [25.0] * len(days),
            "wind_gusts_10m_max": [45.0] * len(days),
            "uv_index_max": [3.2] * len(days),
            "sunrise": [midnight(day) + 7 * 3600 + 30 * 60 for day in days],
            "sunset": [midnight(day) + 19 * 3600 for day in days],
        },
    }


def _call(
    args: dict[str, Any] | None = None, *, place: dict[str, Any] | None = PLACE,
    here: Any = None,  # noqa: ANN401 — a location reader or nothing.
) -> dict[str, Any]:
    registry = build_default_registry(weather_lookup=weather_lookup(place), here_location=here)
    (definition,) = (d for d in registry.get_definitions() if d.name == "weather")
    return dict(definition.handler(args or {}, None))  # type: ignore[arg-type,call-arg,misc]


def _day(offset: int) -> str:
    return (NOW.date() + timedelta(days=offset)).isoformat()


def test_no_arguments_is_one_forecast_request_for_home_in_the_old_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No arguments: one request for home; now, today, tomorrow and the next twelve hours."""
    net = Net(monkeypatch)
    report = _call()
    assert len(net.asked) == 1
    assert net.hosts() == ["api.open-meteo.com"]
    assert "latitude=48.48&longitude=-123.38" in net.asked[0]
    assert "forecast_days=2" in net.asked[0]
    assert "timeout=4.0" in net.asked[0]
    assert set(report) == {"place", "timezone", "now", "today", "tomorrow", "next_hours"}
    assert report["place"] == "home"
    assert report["timezone"] == "America/Vancouver"
    assert report["now"] == {
        "temp_c": 11.0, "feels_c": 9.5, "condition": "overcast", "humidity_pct": 80,
        "wind_kmh": 14.0, "gust_kmh": 30.0, "wind_from": "NW", "precip_mm": 0.2,
    }
    assert report["today"]["high_c"] == 15.0
    assert report["tomorrow"]["date"] == (
        datetime.fromisoformat(report["today"]["date"]) + timedelta(days=1)
    ).date().isoformat()
    assert report["today"] | {"date": "d"} == {
        "date": "d", "high_c": 15.0, "low_c": 8.0, "feels_high_c": 14.0, "feels_low_c": 6.0,
        "condition": "rain", "rain_chance_pct": 70, "precip_mm": 3.5, "snow_cm": 0.0,
        "wind_max_kmh": 25.0, "gust_max_kmh": 45.0, "uv_index_max": 3.2,
        "sunrise": "07:30", "sunset": "19:00",
    }
    hours = report["next_hours"]
    assert len(hours) == 12
    assert hours[0]["condition"] == "overcast"  # the first hour is the current reading
    assert hours[1]["condition"] == "light rain"
    assert set(hours[0]) == {
        "at", "temp_c", "feels_c", "condition", "rain_chance_pct", "precip_mm", "wind_kmh",
    }


def test_a_named_place_is_geocoded_then_forecast_and_the_result_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The place goes to the geocoder as given; the forecast is for what it found."""
    net = Net(monkeypatch)
    report = _call({"place": "Toronto"})
    assert net.hosts() == ["geocoding-api.open-meteo.com", "api.open-meteo.com"]
    assert "name=Toronto" in net.asked[0]
    assert "latitude=43.7&longitude=-79.42" in net.asked[1]
    assert report["place"] == "Toronto, Ontario, Canada"
    assert report["timezone"] == "America/Toronto"
    assert report["today"]["sunrise"] == "07:30"


def test_a_name_in_the_users_language_is_tried_in_that_language_then_in_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CJK name asks in zh first and falls back to en when zh finds nothing."""
    net = Net(monkeypatch)
    GEOCODED["多伦多", "zh"] = []
    try:
        report = _call({"place": "多伦多"})
    finally:
        del GEOCODED["多伦多", "zh"]
    assert [one.split("language=")[1][:2] for one in net.asked[:2]] == ["zh", "en"]
    assert report["place"] == "Toronto, Ontario, Canada"


def test_same_name_places_are_listed_next_to_the_chosen_one_and_can_be_picked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two other places called Toronto come back, with coordinates that read the other one."""
    net = Net(monkeypatch)
    report = _call({"place": "Toronto"})
    assert report["place"] == "Toronto, Ontario, Canada"
    assert report["same_name_elsewhere"] == [
        {"place": "Toronto, Ohio, United States", "use_place": "40.46,-80.6"},
        {"place": "Toronto, Kansas, United States", "use_place": "37.79,-95.95"},
    ]
    net.asked.clear()
    picked = _call({"place": "40.46,-80.6"})
    assert net.hosts() == ["api.open-meteo.com"]  # no geocoding for coordinates
    assert picked["place"] == "40.46,-80.6"
    assert "same_name_elsewhere" not in picked


def test_an_unknown_place_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No match: an error that says so, after one lookup and no forecast request."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError, match="no place found named 'Nowhere Town'") as caught:
        _call({"place": "Nowhere Town"})
    assert caught.value.code == "not_found"
    assert net.hosts() == ["geocoding-api.open-meteo.com"]


def test_days_from_three_back_to_ten_ahead_are_read_and_others_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A range gets its days; past 16 ahead or 7 back, or backwards, is refused untouched."""
    net = Net(monkeypatch)
    report = _call({"place": "Toronto", "start_date": _day(-3), "end_date": _day(10)})
    assert f"start_date={_day(-3)}&end_date={_day(10)}" in net.asked[1]
    assert "forecast_days" not in net.asked[1]
    assert "today" not in report
    assert [d["date"] for d in report["days"]] == [_day(n) for n in range(-3, 11)]
    assert report["days"][0]["rain_chance_pct"] is None  # a past day has no chance of rain
    assert "next_hours" not in report
    assert _call({"start_date": _day(10)})["days"][0]["date"] == _day(10)  # one day alone
    net.asked.clear()
    for first, last in ((_day(30), _day(30)), (_day(-20), _day(-20)), (_day(5), _day(2))):
        with pytest.raises(ToolError, match="7 days back to 16 days ahead"):
            _call({"start_date": first, "end_date": last})
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        _call({"start_date": "next friday"})
    with pytest.raises(ToolError, match="needs a start_date"):
        _call({"end_date": _day(2)})
    assert net.asked == []


def test_hourly_rows_cover_the_days_asked_and_air_quality_adds_aqi_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hourly: every hour of the day asked; air_quality: now and each day's worst."""
    net = Net(monkeypatch)
    report = _call({"start_date": _day(1), "hourly": True, "air_quality": True})
    rows = report["hourly"]
    assert len(rows) == 24
    assert {row["at"][:10] for row in rows} == {_day(1)}
    assert "next_hours" not in report
    assert net.hosts() == ["api.open-meteo.com", "air-quality-api.open-meteo.com"]
    assert f"start_date={_day(1)}&end_date={_day(1)}" in net.asked[1]
    assert report["air_quality"] == {
        "now": {"us_aqi": 42, "pm2_5_ugm3": 8.5, "pm10_ugm3": 12.0},
        "days": [
            {"date": _day(1), "us_aqi_max": 53, "pm2_5_max_ugm3": 28.0, "pm10_max_ugm3": None},
        ],
    }
    default = _call({"hourly": True})  # no dates: every hour of today and tomorrow
    assert len(default["hourly"]) == 48
    assert "next_hours" not in default
    assert "air_quality" not in default
    with pytest.raises(ToolError, match="at most 3 days"):
        _call({"start_date": _day(0), "end_date": _day(3), "hourly": True})


def test_here_reads_the_location_seam_and_says_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``here`` forecasts for the fix without geocoding; with no reader it is an error."""
    net = Net(monkeypatch)
    fix = {"lat": 43.65, "lng": -79.38, "accuracy_m": 30.0, "place": "King St, Toronto"}
    report = _call({"place": "Here"}, here=lambda: fix)
    assert net.hosts() == ["api.open-meteo.com"]
    assert "latitude=43.65&longitude=-79.38" in net.asked[0]
    assert report["place"] == "King St, Toronto"
    assert report["location_from"] == "this Mac"

    def denied() -> dict[str, Any]:
        msg = "denied"
        raise PermissionError(msg)

    net.asked.clear()
    with pytest.raises(ToolError, match="location could not be read") as caught:
        _call({"place": "here"}, here=denied)
    assert caught.value.code == "location_unavailable"
    with pytest.raises(ToolError, match="no location reader"):
        _call({"place": "here"})
    assert net.asked == []


@pytest.mark.parametrize(
    ("down", "args", "failed"),
    [
        ("geocoding-api.open-meteo.com", {"place": "Toronto"}, "the place lookup"),
        ("api.open-meteo.com", {"place": "Toronto"}, "the forecast"),
        ("air-quality-api.open-meteo.com", {"air_quality": True}, "the air quality"),
    ],
)
def test_each_failed_read_is_a_tool_error_that_names_it(
    monkeypatch: pytest.MonkeyPatch, down: str, args: dict[str, Any], failed: str,
) -> None:
    """The geocoder, the forecast or the air quality down: the error says which."""
    Net(monkeypatch, down=(down,))
    with pytest.raises(ToolError, match=f"weather: {failed} could not be read") as caught:
        _call(args)
    assert caught.value.code == "network_error"


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


def test_the_description_offers_here_only_where_a_location_can_be_read() -> None:
    """``here`` is in the text and the schema only with a Mac or phone location reader."""
    plain = build_default_registry(weather_lookup=weather_lookup(PLACE))
    located = build_default_registry(
        weather_lookup=weather_lookup(PLACE), here_location=dict,
    )
    (without,) = (d for d in plain.get_definitions() if d.name == "weather")
    (with_here,) = (d for d in located.get_definitions() if d.name == "weather")
    assert '"here"' not in without.description
    assert '"here"' in with_here.description
    assert set(with_here.input_schema["properties"]) == {
        "place", "start_date", "end_date", "hourly", "air_quality",
    }
    assert with_here.input_schema["required"] == []


def test_the_weather_call_says_no_wait_line() -> None:
    """A weather call is quiet at dispatch; a search still says its line."""
    assert _dispatch_key("weather") is None
    assert _dispatch_key("web_search") == "commentary.tool.web"


def test_the_home_view_still_reads_the_same_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The home's own forecast reads the same recorded answer."""

    def urlopen(_url: str, *, timeout: float) -> io.BytesIO:
        assert timeout == 4.0
        return io.BytesIO(json.dumps(RECORDED).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    home = weather(48.48, -123.38)
    assert home["now_c"] == 11.0
    assert home["high_c"] == 16.1
    assert len(home["hours"]) == 4
