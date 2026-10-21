"""ADR 0285: the model reads the weather from Apple WeatherKit at any place and day in one call.

WeatherKit and Open-Meteo (place names, air quality) are faked over ``urlopen`` and the signing
key is a throwaway P-256 key, so a request for the wrong place, zone or days reads wrong data
here and no real service is called.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime.home import Home, weather_lookup
from jarvis.runtime.weatherkit import NOT_SET_UP, WeatherKit
from jarvis.shared import CallerPrincipal
from jarvis.shared.here import Coarse, HereUnavailable
from tests.integration.weatherkit_world import (
    GEOCODED,
    KEY_ID,
    NOW,
    PLACE,
    SERVICE_ID,
    TEAM_ID,
    ZONE,
    Net,
    b64,
    config,
    drizzle_before_dawn,
    kit_for,
    make_key,
    rain_all_day,
)
from tests.integration.weatherkit_world import call as _call
from tests.support.here import FixedHere, mac_reading
from tests.support.home_routes import Connections
from tests.support.runtime_config import runtime_config

TORONTO = {
    "name": "Toronto", "latitude": 43.70, "longitude": -79.42, "admin1": "Ontario",
    "country": "Canada", "country_code": "CA", "timezone": "America/Toronto",
}
TORONTO_OH = {
    "name": "Toronto", "latitude": 40.46, "longitude": -80.60, "admin1": "Ohio",
    "country": "United States", "country_code": "US", "timezone": "America/New_York",
}
TORONTO_KS = {
    "name": "Toronto", "latitude": 37.79, "longitude": -95.95, "admin1": "Kansas",
    "country": "United States", "country_code": "US", "timezone": "America/Chicago",
}
TORONTO_VILLE = {"name": "Torontoville", "latitude": 1.0, "longitude": 2.0, "country": "Nowhere"}
GEOCODED.update({
    ("Toronto", "en"): [TORONTO, TORONTO_OH, TORONTO_VILLE, TORONTO_KS],
    ("多伦多", "en"): [TORONTO],
    ("Nowhere Town", "en"): [],
})


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    """A throwaway signing key file and its key."""
    return make_key(tmp_path)


def _day(offset: int) -> str:
    return (NOW.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=offset)).isoformat()


def _day_in(zone: str, offset: int) -> str:
    return (NOW.astimezone(ZoneInfo(zone)).date() + timedelta(days=offset)).isoformat()


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url.split(" ", maxsplit=1)[0]).query).items()}


def test_no_arguments_is_one_weatherkit_request_for_home_and_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """One request: current, hours from midnight, daily, next hour and alerts; today in parts."""
    net = Net(monkeypatch)
    report = _call(kit_for(key[0]))
    assert net.paths() == [  # the forecast, then which data sets WeatherKit has at the place
        "/api/v1/weather/en/48.48/-123.38", "/api/v1/availability/48.48/-123.38",
    ]
    assert len(net.weather()) == 1
    query = _query(net.asked[0])
    assert query["dataSets"] == (
        "currentWeather,forecastHourly,forecastDaily,forecastNextHour,weatherAlerts"
    )
    assert query["countryCode"] == query["country"] == "CA"  # Apple documents one, forums the other
    assert query["timezone"] == ZONE
    assert "timeout=6.0" in net.asked[0]
    assert report["alerts"] == "none in force"
    assert list(report) == [
        "summary", "place", "timezone", "now", "earlier_today", "rest_of_today", "tonight",
        "whole_day", "tomorrow", "next_hours", "next_hour", "alerts", "attribution",
    ]
    assert report["place"] == "home"
    assert report["now"] == {
        "temp_c": 11.0, "feels_c": 9.5, "condition": "cloudy", "humidity_pct": 80,
        "wind_kmh": 14.0, "gust_kmh": 30.0, "wind_from": "NW", "precip_mm_h": 0.2,
        "uv_index": 1,
    }
    assert report["whole_day"] == {
        "day": "today (Wed)", "date": _day(0), "high_c": 15.0, "low_c": 8.0,
        "condition": "rain", "rain_chance_pct": 70, "precip_mm": 3.5, "snow_cm": 0.0,
        "wind_kmh": 25.0, "uv_index_max": 3, "sunrise": "07:30", "sunset": "19:00",
    }
    assert report["tomorrow"]["day"] == "tomorrow (Thu)"
    assert report["tomorrow"]["date"] == _day(1)
    hours = report["next_hours"]
    assert [row["at"] for row in hours[:7]] == [f"today {h:02d}:00" for h in range(9, 15)] + [
        "today 15:00",
    ]
    assert hours[0]["condition"] == "cloudy"  # the first hour is the current reading
    assert hours[1]["condition"] == "drizzle"
    assert set(hours[0]) == {"at", "temp_c", "condition", "rain_chance_pct", "precip_mm"}
    assert report["attribution"] == {
        "provider": "Apple Weather", "url": "https://weatherkit.apple.com/legal-attribution.html",
    }


def test_the_token_has_apples_header_and_claims_and_is_signed_with_the_key(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Header alg, kid, id; claims iss, iat, exp, sub; it verifies; one token serves both."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    _call(kit)
    _call(kit, {"when": _day(2)})
    bearers = [h["Authorization"] for h in net.headers]
    assert len(bearers) == 3  # the forecast, the data sets of the place, another forecast
    assert len(set(bearers)) == 1
    scheme, _, token = bearers[0].partition(" ")
    assert scheme == "Bearer"
    head, claims, signature = token.split(".")
    assert json.loads(b64(head)) == {
        "alg": "ES256", "kid": KEY_ID, "id": f"{TEAM_ID}.{SERVICE_ID}",
    }
    body = json.loads(b64(claims))
    assert set(body) == {"iss", "iat", "exp", "sub"}
    assert (body["iss"], body["sub"]) == (TEAM_ID, SERVICE_ID)
    assert body["exp"] - body["iat"] == 3600
    raw = b64(signature)
    assert len(raw) == 64
    key[1].public_key().verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()),
    )


def test_a_named_place_is_geocoded_then_read_in_its_own_zone_and_country(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The place goes to the geocoder as given; WeatherKit is asked for what it found."""
    net = Net(monkeypatch)
    report = _call(kit_for(key[0]), {"place": "Toronto"})
    assert net.hosts() == ["geocoding-api.open-meteo.com", *["weatherkit.apple.com"] * 2]
    assert "name=Toronto" in net.asked[0]
    assert urlparse(net.asked[1].split(" ")[0]).path == "/api/v1/weather/en/43.7/-79.42"
    assert _query(net.asked[1])["timezone"] == "America/Toronto"
    assert _query(net.asked[1])["countryCode"] == "CA"
    assert "/api/v1/availability/43.7/-79.42?country=CA" in net.asked[2]
    assert report["place"] == "Toronto, Ontario, Canada"
    assert report["timezone"] == "America/Toronto"


def test_a_name_in_the_users_language_is_tried_in_that_language_then_in_english(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A CJK name asks in zh first and falls back to en when zh finds nothing."""
    net = Net(monkeypatch)
    GEOCODED["多伦多", "zh"] = []
    try:
        report = _call(kit_for(key[0]), {"place": "多伦多"})
    finally:
        del GEOCODED["多伦多", "zh"]
    assert [one.split("language=")[1][:2] for one in net.asked[:2]] == ["zh", "en"]
    assert report["place"] == "Toronto, Ontario, Canada"


def test_same_name_places_are_listed_and_picked_by_coordinates_in_their_own_zone(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Two other places called Toronto come back, with coordinates that read the other one."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"place": "Toronto"})
    assert report["same_name_elsewhere"] == [
        {"place": "Toronto, Ohio, United States", "use_place": "40.46,-80.6"},
        {"place": "Toronto, Kansas, United States", "use_place": "37.79,-95.95"},
    ]
    net.asked.clear()
    picked = _call(kit, {"place": "40.46,-80.6"})
    assert set(net.hosts()) == {"weatherkit.apple.com"}  # no geocoding for coordinates
    assert _query(net.asked[0])["timezone"] == "America/New_York"
    assert _query(net.asked[0])["countryCode"] == "US"
    assert picked["place"] == "40.46,-80.6"
    assert "same_name_elsewhere" not in picked


def test_a_point_far_from_home_takes_its_zone_from_the_offline_map(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Coordinates the lookup never listed, far from home: their own zone, no country, no lookup."""
    net = Net(monkeypatch)
    far = _call(kit_for(key[0]), {"place": "35.69,139.69"})
    assert far["timezone"] == "Asia/Tokyo"
    assert set(net.hosts()) == {"weatherkit.apple.com"}
    assert parse_qs(urlparse(net.asked[0].split(" ")[0]).query)["countryCode"] == ["JP"]
    net.asked.clear()
    near = _call(kit_for(key[0]), {"place": "48.5,-123.4"})  # inside 100 km of home: its zone
    assert near["timezone"] == ZONE
    assert set(net.hosts()) == {"weatherkit.apple.com"}


def test_an_unknown_place_is_a_tool_error(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """No match: an error that says so, after one lookup and no forecast request."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError, match="no place found named 'Nowhere Town'") as caught:
        _call(kit_for(key[0]), {"place": "Nowhere Town"})
    assert caught.value.code == "not_found"
    assert net.hosts() == ["geocoding-api.open-meteo.com"]


def test_days_from_seven_back_to_nine_ahead_are_read_and_others_are_refused_untouched(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A range gets its days; 8 back, past nine ahead, or backwards, is refused clearly."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"when": f"{_day(0)} to {_day(5)}"})
    query = _query(net.asked[0])
    assert query["dailyStart"].endswith(("T07:00:00Z", "T08:00:00Z"))
    assert report["whole_day"]["date"] == _day(0)  # today is in parts, the others are days
    assert [d["date"] for d in report["days"]] == [_day(n) for n in range(1, 6)]
    assert "next_hours" not in report
    assert "next_hour" in report  # the range holds today
    later = _call(kit, {"when": _day(3)})
    assert [d["date"] for d in later["days"]] == [_day(3)]
    assert "next_hour" not in later  # minute rain is for now only
    net.asked.clear()
    for words in (_day(12), _day(-8), f"{_day(5)} to {_day(2)}"):
        with pytest.raises(ToolError, match=r"forecast runs from|ends before it starts"):
            _call(kit, {"when": words})
    assert net.asked == []


def test_yesterday_and_seven_days_back_are_read_and_eight_back_is_refused_naming_the_range(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The place's own date judges the range and names it in full, at any hour of the UTC day."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    assert [d["date"] for d in _call(kit, {"when": "yesterday"})["days"]] == [_day(-1)]
    assert [d["date"] for d in _call(kit, {"when": _day(-7)})["days"]] == [_day(-7)]
    net.asked.clear()
    with pytest.raises(ToolError) as caught:
        _call(kit, {"when": _day(-8)})
    assert _day(-7) in str(caught.value)
    assert _day(9) in str(caught.value)
    assert net.asked == []


def test_a_request_of_days_all_past_asks_for_the_daily_forecast_alone_and_answers_without_now(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Past days: one daily-only request, ``days`` and attribution; into today it is whole."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"when": f"{_day(-3)} to {_day(-1)}"})
    assert len(net.asked) == 1
    query = _query(net.asked[0])
    assert query["dataSets"] == "forecastDaily"
    assert not {"hourlyStart", "hourlyEnd", "countryCode"} & set(query)
    assert set(report) == {"summary", "place", "timezone", "days", "attribution"}
    assert [d["date"] for d in report["days"]] == [_day(-3), _day(-2), _day(-1)]
    assert report["days"][0]["high_c"] == 15.0
    assert report["days"][0]["precip_mm"] == 3.5
    mixed = _call(kit, {"when": f"{_day(-1)} to {_day(1)}"})
    assert [d["date"] for d in mixed["days"]] == [_day(-1), _day(1)]
    assert mixed["whole_day"]["date"] == _day(0)
    assert {"now", "next_hour", "alerts"} <= set(mixed)
    assert "dailyStart" in _query(net.weather()[-1])
    assert "forecastHourly" in _query(net.weather()[-1])["dataSets"]


def test_hourly_rows_for_a_past_day_are_refused_untouched(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Hour rows run from now on: hourly with a day before today is an error, no request made."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError, match="hour-by-hour rows exist from now on"):
        _call(kit_for(key[0]), {"when": "yesterday", "hourly": True})
    assert net.asked == []


def test_hourly_rows_cover_the_days_asked_and_air_quality_adds_aqi_rows(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """hourly: every hour of the day asked; air_quality: now and each day's worst."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"when": "tomorrow", "hourly": True, "air_quality": True})
    rows = report["hourly"]
    assert len(rows) == 24
    assert {row["at"].split(" ")[0] for row in rows} == {"tomorrow"}
    assert net.hosts() == ["weatherkit.apple.com"] * 2 + ["air-quality-api.open-meteo.com"]
    assert f"start_date={_day(1)}&end_date={_day(1)}" in net.asked[2]
    assert report["air_quality"]["now"] == {"us_aqi": 42, "pm2_5_ugm3": 8.5, "pm10_ugm3": 12.0}
    assert [d["date"] for d in report["air_quality"]["days"]] == [_day(1)]
    default = _call(kit, {"hourly": True})  # no dates: the rest of today and then tomorrow
    assert default["hourly"][0]["at"] == "today 09:00"
    assert default["hourly"][-1]["at"].startswith("tomorrow ")
    assert "next_hours" not in default
    assert "air_quality" not in default
    with pytest.raises(ToolError, match="at most 3 days"):
        _call(kit, {"when": f"{_day(0)} to {_day(3)}", "hourly": True})


def test_here_reads_the_location_seam_and_says_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """``here`` reads the fix without geocoding; far away its own zone; no reader: error."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    fix = mac_reading(lat=48.45, lng=-123.37, accuracy_m=30.0, place="Government St, Victoria")
    report = _call(kit, {"place": "Here"}, here=FixedHere(fix))
    assert net.paths()[0] == "/api/v1/weather/en/48.45/-123.37"
    assert len(net.weather()) == 1
    assert report["place"] == "Government St, Victoria"
    assert report["location_from"] == "this Mac"
    away = mac_reading(lat=43.65, lng=-79.38, accuracy_m=30.0, place="King St, Toronto")
    assert _call(kit, {"place": "here"}, here=FixedHere(away))["timezone"] == "America/Toronto"
    denied = FixedHere(error=HereUnavailable("this Mac's location could not be read (denied)"))

    net.asked.clear()
    with pytest.raises(ToolError, match="location could not be read") as caught:
        _call(kit, {"place": "here"}, here=denied)
    assert caught.value.code == "location_unavailable"
    with pytest.raises(ToolError, match="no location reader"):
        _call(kit, {"place": "here"})
    assert net.asked == []


@pytest.mark.parametrize(
    ("down", "args", "failed"),
    [
        ("geocoding-api.open-meteo.com", {"place": "Toronto"}, "weather: the place lookup"),
        ("weatherkit.apple.com", {"place": "Toronto"}, "weather is unavailable: the forecast"),
    ],
)
def test_each_failed_read_is_a_tool_error_that_names_it(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    down: str, args: dict[str, Any], failed: str,
) -> None:
    """The geocoder or WeatherKit down: the error says which, and sends no one to a web search."""
    Net(monkeypatch, down=(down,))
    with pytest.raises(ToolError, match=f"{failed} could not be read") as caught:
        _call(kit_for(key[0]), args)
    assert (caught.value.code, caught.value.kind) == ("network_error", "dont_retry")
    assert "web_search" not in str(caught.value)


def test_a_401_from_apple_gives_a_plain_reason_without_the_token(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Apple refuses the key: the error names the status and Apple's reason, nothing secret."""
    net = Net(monkeypatch, refuse=401)
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolError) as caught:
        _call(kit_for(key[0]))
    assert (caught.value.code, caught.value.kind) == ("weatherkit_refused", "dont_retry")
    assert str(caught.value) == (
        "weather is unavailable: Apple WeatherKit refused the key (401 NOT_ENABLED);"
        " the key needs checking"
    )
    token = net.headers[0]["Authorization"].split(" ")[1]
    assert token not in str(caught.value)
    assert token not in caplog.text


def test_the_forecast_stands_when_the_air_quality_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Air quality down or out of its reach: the weather comes back, the air marked unavailable."""
    Net(monkeypatch, down=("air-quality-api.open-meteo.com",))
    result = _call(kit_for(key[0]), {"air_quality": True})
    assert "whole_day" in result
    assert "unavailable" in result["air_quality"]


def test_with_no_key_the_tool_says_the_weather_is_not_set_up_and_asks_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No WeatherKit: every call is the plain reason, with no request to any host."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError) as caught:
        _call(None, {"place": "Toronto"})
    assert str(caught.value) == "weather is not set up: WeatherKit key missing" == NOT_SET_UP
    assert net.asked == []


def test_boot_logs_one_line_for_the_source_and_never_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing config, a missing file, a loose file and a broken key all leave it off, once."""
    path, _ = make_key(tmp_path)
    pem = path.read_text()
    secret = pem.splitlines()[1]
    cases: list[dict[str, Any]] = [
        {}, {"weather": {"weatherkit": {"key_id": "", "service_id": "x", "key_file": str(path)}}},
        {"weather": {"weatherkit": {"key_id": KEY_ID, "service_id": SERVICE_ID,
                                    "key_file": str(tmp_path / "missing.p8")}}},
    ]
    broken = tmp_path / "broken.p8"
    broken.write_text(pem.replace(secret, "AAAA" + secret[4:]))
    broken.chmod(0o600)
    loose = tmp_path / "loose.p8"
    loose.write_text(pem)
    loose.chmod(0o644)
    cases.extend(
        {"weather": {"weatherkit": {
            "key_id": KEY_ID, "service_id": SERVICE_ID, "key_file": str(named),
        }}}
        for named in (broken, loose)
    )
    for block in cases:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert WeatherKit.from_settings(runtime_config(block).weatherkit) is None
        assert [r.getMessage().startswith("weather: off (") for r in caplog.records] == [True]
        assert secret not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert WeatherKit.from_settings(runtime_config(config(path)).weatherkit) is not None
    assert [r.getMessage() for r in caplog.records] == ["weather: Apple WeatherKit"]


@pytest.mark.parametrize(
    ("words", "dates"),
    [
        ("this weekend", ["2026-10-24", "2026-10-25"]),
        ("这周末", ["2026-10-24", "2026-10-25"]),
        ("下周五", ["2026-10-30"]),
        ("Friday", ["2026-10-23"]),
        ("周五到周日", ["2026-10-23", "2026-10-24", "2026-10-25"]),
        ("the 25th", ["2026-10-25"]),
        ("Oct 25", ["2026-10-25"]),
        ("后天", ["2026-10-23"]),
        ("yesterday", ["2026-10-20"]),
    ],
)
def test_his_words_for_the_days_are_worked_out_in_code_and_ask_for_those_days(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    words: str, dates: list[str],
) -> None:
    """The model passes what he said; the days read, and asked of Apple, are the right ones."""
    net = Net(monkeypatch)
    report = _call(kit_for(key[0]), {"when": words})
    assert [d["date"] for d in report["days"]] == dates
    query = _query(net.asked[0])
    first = datetime.fromisoformat(dates[0]).replace(tzinfo=ZoneInfo(ZONE))
    last = datetime.fromisoformat(dates[-1]).replace(tzinfo=ZoneInfo(ZONE)) + timedelta(days=1)
    assert query["dailyStart"] == first.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert query["dailyEnd"] == last.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_his_words_are_read_in_the_places_own_calendar_by_the_runtime_clock(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """It is Wednesday morning in Victoria and already Thursday in Tokyo: today is each one's."""
    Net(monkeypatch)
    kit = kit_for(key[0])
    assert _day_in(ZONE, 0) == "2026-10-21"
    assert _day_in("Asia/Tokyo", 0) == "2026-10-22"
    home = _call(kit, {"when": "today"})
    tokyo = _call(kit, {"place": "35.69,139.69", "when": "today"})
    assert home["whole_day"]["date"] == "2026-10-21"
    assert tokyo["whole_day"]["date"] == "2026-10-22"
    assert tokyo["whole_day"]["day"] == "today (Thu)"
    assert [d["date"] for d in _call(kit, {"place": "35.69,139.69", "when": "tomorrow"})["days"]] \
        == ["2026-10-23"]
    assert _call(kit, {"place": "35.69,139.69"})["whole_day"]["date"] == "2026-10-22"


def test_words_the_resolver_cannot_read_and_the_old_date_arguments_are_refused_untouched(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The error lists what is read; start_date and end_date say what replaced them."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    with pytest.raises(ToolError) as unread:
        _call(kit, {"when": "sometime soon"})
    assert unread.value.code == "invalid_arguments"
    assert "weather: I read today, tonight, tomorrow" in str(unread.value)
    assert "not 'sometime soon'" in str(unread.value)
    for old in ({"start_date": _day(2)}, {"end_date": _day(2)}, {"when": "x", "start_date": "y"}):
        with pytest.raises(ToolError, match="start_date and end_date are gone; pass when") as gone:
            _call(kit, old)
        assert gone.value.code == "invalid_arguments"
    assert net.asked == []


def test_the_geocoder_is_asked_for_ten_matches(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """One lookup per name, for up to ten places."""
    net = Net(monkeypatch)
    _call(kit_for(key[0]), {"place": "Richmond"})
    assert "count=10" in net.asked[0]
    assert len([one for one in net.asked if "geocoding" in one]) == 1


def test_the_place_of_that_name_nearest_him_is_the_one_read_and_the_others_are_listed(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Richmond with home in Victoria is Richmond, BC, though the geocoder lists Virginia first."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"place": "Richmond"})
    assert report["place"] == "Richmond, British Columbia, Canada"
    assert urlparse(net.asked[1].split(" ")[0]).path == "/api/v1/weather/en/49.17/-123.14"
    assert report["same_name_elsewhere"] == [
        {"place": "Richmond, Virginia, United States", "use_place": "37.55,-77.46"},
        {"place": "Richmond, California, United States", "use_place": "37.94,-122.35"},
    ]
    assert "same_name_left_out" not in report


def test_where_he_is_roughly_picks_among_the_places_of_a_name_and_asks_no_fix(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The coarse read puts him near Virginia, so that Richmond wins; no fix is asked of Here."""
    Net(monkeypatch)
    kit = kit_for(key[0])
    here = FixedHere(coarse=Coarse(37.6, -77.5))
    report = _call(kit, {"place": "Richmond"}, here=here)
    assert report["place"] == "Richmond, Virginia, United States"
    assert [one["place"] for one in report["same_name_elsewhere"]] == [
        "Richmond, British Columbia, Canada", "Richmond, California, United States",
    ]
    assert here.asked == []
    unknown = FixedHere(coarse=None)
    assert _call(kit, {"place": "Richmond"}, here=unknown)["place"] == (
        "Richmond, British Columbia, Canada"  # no report: the home's own point
    )


def test_a_place_none_is_near_takes_the_geocoders_first_match_and_caps_the_others(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Seven Springfields, all far: the first stands, four others are listed, the rest counted."""
    Net(monkeypatch)
    report = _call(kit_for(key[0]), {"place": "Springfield"})
    assert report["place"] == "Springfield, Illinois, United States"
    assert [one["place"] for one in report["same_name_elsewhere"]] == [
        f"Springfield, {region}, United States"
        for region in ("Missouri", "Massachusetts", "Oregon", "Ohio")
    ]
    assert report["same_name_left_out"] == 2


@pytest.mark.parametrize(
    ("named", "label"),
    [
        ("Vancouver, BC", "Vancouver, British Columbia, Canada"),
        ("Vancouver, B.C.", "Vancouver, British Columbia, Canada"),
        ("Vancouver, British Columbia", "Vancouver, British Columbia, Canada"),
        ("Vancouver\uff0c Canada", "Vancouver, British Columbia, Canada"),
        ("Vancouver, CA", "Vancouver, British Columbia, Canada"),
        ("vancouver , bc , canada", "Vancouver, British Columbia, Canada"),
        ("Vancouver, WA", "Vancouver, Washington, United States"),
        ("Vancouver, Washington", "Vancouver, Washington, United States"),
        ("Vancouver, USA", "Vancouver, Washington, United States"),
        ("Vancouver, US", "Vancouver, Washington, United States"),
        ("温哥华\uff0c不列颠哥伦比亚", "温哥华, 不列颠哥伦比亚省, 加拿大"),
    ],
)
def test_a_region_or_country_after_a_comma_picks_the_match_by_name_or_short_code(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    named: str, label: str,
) -> None:
    """Matched on the region or the country, in either language, with BC and WA as codes."""
    net = Net(monkeypatch)
    here = FixedHere(coarse=Coarse(48.0, -123.0))  # nearer to Vancouver, BC than to Washington
    report = _call(kit_for(key[0]), {"place": named}, here=here)
    assert report["place"] == label
    assert "same_name_elsewhere" not in report  # the other Vancouver was named away
    assert net.hosts()[0] == "geocoding-api.open-meteo.com"


def test_a_region_the_geocoder_has_no_match_in_is_an_error_that_lists_what_it_knows(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Never another place under his name: Richmond, Texas is not read as Richmond, BC."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError) as caught:
        _call(kit_for(key[0]), {"place": "Richmond, Texas"})
    assert caught.value.code == "not_found"
    assert "no place named 'Richmond' found in Texas" in str(caught.value)
    assert "Richmond, Virginia, United States" in str(caught.value)
    assert net.hosts() == ["geocoding-api.open-meteo.com"]


@pytest.mark.parametrize("named", [",", "\uff0c", " , "])
def test_a_name_that_is_only_a_comma_is_not_found(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey], named: str,
) -> None:
    """No place in it: the plain not-found error, no lookup."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError) as caught:
        _call(kit_for(key[0]), {"place": named})
    assert caught.value.code == "not_found"
    assert net.asked == []


@pytest.mark.parametrize(
    ("args", "start", "end"),
    [
        pytest.param(
            {}, "2026-10-21T07:00:00Z", "2026-10-23T07:00:00Z",
            id="no days: today's midnight to the end of tomorrow",
        ),
        pytest.param(
            {"when": "today"}, "2026-10-21T07:00:00Z", "2026-10-22T14:00:00Z",
            id="today: on to tomorrow 7:00, so that tonight is whole",
        ),
        pytest.param(
            {"when": "today to Friday"}, "2026-10-21T07:00:00Z", "2026-10-24T07:00:00Z",
            id="a range from today: today's midnight to the end of the last day",
        ),
        pytest.param(
            {"when": "yesterday to tomorrow"}, "2026-10-21T07:00:00Z", "2026-10-23T07:00:00Z",
            id="a range into the past: today's midnight, not yesterday's",
        ),
        pytest.param(
            {"when": "Saturday"}, "2026-10-24T07:00:00Z", "2026-10-25T07:00:00Z",
            id="a later day: that day's own hours",
        ),
        pytest.param(
            {"when": "tomorrow", "hourly": True}, "2026-10-22T07:00:00Z", "2026-10-23T07:00:00Z",
            id="tomorrow by the hour",
        ),
        pytest.param(
            {"place": "35.69,139.69"}, "2026-10-21T15:00:00Z", "2026-10-23T15:00:00Z",
            id="Tokyo: its own midnight, which is Thursday's",
        ),
    ],
)
def test_the_hours_start_at_the_places_midnight_when_the_days_include_today(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    args: dict[str, Any], start: str, end: str,
) -> None:
    """What fell earlier is added up from the hours, so they begin at the day's own midnight."""
    net = Net(monkeypatch)
    _call(kit_for(key[0]), args)
    query = _query(net.asked[0])
    assert (query["hourlyStart"], query["hourlyEnd"]) == (start, end)


def test_the_homes_line_and_the_tool_without_days_are_one_request(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Both ask for today and tomorrow of the same place: the held answer serves the second."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    home = Home(Connections(None), (ZONE, ZoneInfo(ZONE)), PLACE, weatherkit=kit)  # type: ignore[arg-type]
    assert home.weather() is not None
    _call(kit)
    assert len(net.weather()) == 1
    assert _query(net.weather()[0])["hourlyStart"] == "2026-10-21T07:00:00Z"


def test_drizzle_before_dawn_is_over_and_the_answer_says_no_more_rain_today(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The morning of the audit: the day row still says rain, the parts and the line say not."""
    Net(monkeypatch, hours=drizzle_before_dawn)
    report = _call(kit_for(key[0]))
    assert report["summary"] == (
        "home: 11° and cloudy now; about 1 mm fell before 7:00; no more rain today;"
        " tonight's low 6° at 6:00, dry; tomorrow (Thu): rain, 16°/9°, rain 70% (about 3.5 mm)."
    )
    assert report["earlier_today"] == {
        "rain_mm": 1.0, "from": "4:00", "until": "7:00", "condition": "drizzle",
    }
    assert report["rest_of_today"] == {
        "max_chance_pct": 0, "rain_mm": 0.0, "note": "no more rain expected today",
    }
    assert report["tonight"]["low_c"] == 6.0
    assert report["tonight"]["low_at"] == "6:00"
    assert report["whole_day"]["rain_chance_pct"] == 70  # the whole day's row, named as such


@pytest.mark.parametrize(
    ("hour", "day", "begins"),
    [
        pytest.param(9, 21, "19:00", id="in the morning tonight is 19:00 to 7:00"),
        pytest.param(21, 21, "now", id="at 21:10 it has begun"),
        pytest.param(3, 22, "now", id="at 3:10 it is the night that began yesterday"),
    ],
)
def test_tonight_is_asked_in_the_morning_and_in_the_night_alike(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    hour: int, day: int, begins: str,
) -> None:
    """The low and its hour come from the hours of 19:00 to 7:00, or those left of them."""
    Net(monkeypatch, hours=drizzle_before_dawn)
    kit = kit_for(key[0])
    kit.clock = datetime(2026, 10, day, hour, 10, tzinfo=ZoneInfo(ZONE)).timestamp
    report = _call(kit)
    assert report["tonight"]["from"] == begins
    assert (report["tonight"]["to"], report["tonight"]["low_at"]) == ("7:00", "6:00")


def test_every_day_row_names_its_day_and_the_hours_name_theirs(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """``today (Wed)``, ``tomorrow (Thu)``, ``yesterday (Tue)``, else ``Sat Oct 24``."""
    Net(monkeypatch)
    kit = kit_for(key[0])
    assert [d["day"] for d in _call(kit, {"when": "this weekend"})["days"]] == [
        "Sat Oct 24", "Sun Oct 25",
    ]
    assert _call(kit, {"when": "yesterday"})["days"][0]["day"] == "yesterday (Tue)"
    assert _call(kit, {"when": "next Friday"})["days"][0]["day"] == "Fri Oct 30"
    hourly = _call(kit, {"when": "tomorrow to Saturday", "hourly": True})["hourly"]
    assert {row["at"].split(" ")[0] for row in hourly} <= {"tomorrow", "Fri", "Sat"}


def test_a_full_day_of_rain_still_answers_in_under_four_thousand_characters(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Every field present, rain every hour, rain now: nothing is left out."""
    Net(monkeypatch, hours=rain_all_day, minutes="raining")
    here = FixedHere(mac_reading(lat=48.45, lng=-123.37, place="Government St, Victoria"))
    kit = kit_for(key[0])
    for args in ({}, {"place": "here"}):
        report = _call(kit, args, here=here)
        assert len(json.dumps(report, ensure_ascii=False)) <= 4000
        assert "left_out" not in report


def test_a_range_too_long_to_say_is_cut_at_whole_days_and_says_how_to_get_the_rest(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Today and nine days, three warnings in force: the later days go whole, counted."""
    Net(monkeypatch, hours=rain_all_day, warned=3)
    report = _call(kit_for(key[0]), {"when": f"{_day(0)} to {_day(9)}", "air_quality": True})
    assert len(json.dumps(report, ensure_ascii=False)) <= 4000
    shown = [d["date"] for d in report["days"]]
    assert shown == [_day(n) for n in range(1, len(shown) + 1)]
    assert len(shown) + report["left_out"]["days"] == 9
    assert "narrower `when`" in report["see_more"]


def test_hours_without_days_are_cut_at_the_end_and_say_so(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Today's rest and tomorrow by the hour is more than fits: the last hours go, counted."""
    Net(monkeypatch, hours=rain_all_day)
    report = _call(kit_for(key[0]), {"hourly": True})
    assert len(json.dumps(report, ensure_ascii=False)) <= 4000
    rows = report["hourly"]
    assert rows[0]["at"] == "today 09:00"
    assert len(rows) + report["left_out"]["hourly"] == 39  # 15 hours left today and 24 tomorrow
    assert "narrower `when`" in report["see_more"]


def test_the_key_is_never_in_a_result_or_a_log_line(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the key's bytes nor the token appear in what the model reads or in the logs."""
    net = Net(monkeypatch)
    secret = key[0].read_text().splitlines()[1]
    with caplog.at_level(logging.DEBUG):
        report = _call(kit_for(key[0]), {"place": "Toronto", "hourly": True, "air_quality": True})
    text = json.dumps(report)
    token = next(h for h in net.headers if h)["Authorization"].split(" ")[1]
    assert secret not in text
    assert token not in text
    assert secret not in caplog.text
    assert token not in caplog.text


def test_one_answer_serves_the_same_request_within_five_minutes(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A repeated request is read from the held answer, until it is five minutes old."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    now = [NOW.timestamp()]
    kit.clock = lambda: now[0]
    _call(kit)
    _call(kit)
    assert len(net.weather()) == 1
    now[0] += 299
    _call(kit)
    assert len(net.weather()) == 1
    now[0] += 2
    _call(kit)
    assert len(net.weather()) == 2


def test_the_tool_is_on_the_models_menu_up_front_read_only_and_only_with_a_location(
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Registered up front for the model only, read-only, and absent without a location."""
    registry = build_default_registry(weather_lookup=weather_lookup(PLACE, kit_for(key[0]), ZONE))
    (definition,) = (d for d in registry.get_definitions() if d.name == "weather")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert definition.read_only
    assert not definition.requires_confirmation
    assert not definition.deferred
    for word in ("web_search", "where_am_i", "warnings", "9 ahead", "7 days back", "summary line"):
        assert word in definition.description
    assert "weather" not in {d.name for d in build_default_registry().get_definitions()}
    assert weather_lookup(None, None, ZONE) is None
    # Without a key the tool is still offered: it answers with the reason.
    unkeyed = build_default_registry(weather_lookup=weather_lookup(PLACE, None, ZONE))
    assert "weather" in {d.name for d in unkeyed.get_definitions()}


def test_the_description_offers_here_only_where_a_location_can_be_read(
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """``here`` is in the place's text only with a Mac or phone location reader."""
    lookup = weather_lookup(PLACE, kit_for(key[0]), ZONE)
    plain = build_default_registry(weather_lookup=lookup)
    located = build_default_registry(weather_lookup=lookup, here=FixedHere())
    (without,) = (d for d in plain.get_definitions() if d.name == "weather")
    (with_here,) = (d for d in located.get_definitions() if d.name == "weather")
    assert '"here"' not in without.input_schema["properties"]["place"]["description"]
    assert '"here"' in with_here.input_schema["properties"]["place"]["description"]
    assert set(with_here.input_schema["properties"]) == {
        "place", "when", "hourly", "air_quality",
    }
    assert with_here.input_schema["required"] == []


def test_the_shipped_config_names_the_owners_key_team_and_services_ids_and_the_key_path() -> None:
    """The defaults are the owner's non-secret ids; only the key file is left to put in place."""
    import yaml  # noqa: PLC0415 - only this check reads the shipped file.

    shipped = yaml.safe_load(
        (Path(__file__).parents[2] / "config" / "jarvis.yaml").read_text(encoding="utf-8"),
    )
    assert shipped["weather"]["weatherkit"] == {
        "key_id": "5YTD6WQL62", "team_id": "3MEBVQ3N3U", "service_id": "com.allen.jarvis.weather",
        "key_file": "~/.jarvis/weatherkit.p8",
    }
