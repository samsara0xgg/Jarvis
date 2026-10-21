"""ADR 0285: the model reads the weather from Apple WeatherKit at any place and day in one call.

WeatherKit and Open-Meteo (place names, air quality) are faked over ``urlopen`` and the signing
key is a throwaway P-256 key, so a request for the wrong place, zone or days reads wrong data
here and no real service is called.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime.home import weather_lookup
from jarvis.runtime.weatherkit import NOT_SET_UP, WeatherKit
from jarvis.shared import CallerPrincipal
from tests.integration.weatherkit_world import (
    GEOCODED,
    KEY_ID,
    NOW,
    SERVICE_ID,
    TEAM_ID,
    Net,
    b64,
    config,
    kit_for,
    make_key,
)

PLACE = {"latitude": 48.48, "longitude": -123.38, "country_code": "CA"}
ZONE = "America/Vancouver"
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


_TOOLS: dict[tuple[int, int], Any] = {}


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    """A throwaway signing key file and its key."""
    return make_key(tmp_path)


def _call(
    kit: WeatherKit | None, args: dict[str, Any] | None = None,
    here: Any = None,  # noqa: ANN401 - a location reader or nothing.
) -> dict[str, Any]:
    # One lookup per client and reader, as the daemon builds it: it remembers the places it listed.
    token = (id(kit), id(here))
    if token not in _TOOLS:
        registry = build_default_registry(
            weather_lookup=weather_lookup(PLACE, kit, ZONE), here_location=here,
        )
        (_TOOLS[token],) = (d for d in registry.get_definitions() if d.name == "weather")
    return dict(_TOOLS[token].handler(args or {}, None))


def _day(offset: int) -> str:
    return (NOW.astimezone(ZoneInfo(ZONE)).date() + timedelta(days=offset)).isoformat()


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url.split(" ", maxsplit=1)[0]).query).items()}


def test_no_arguments_is_one_weatherkit_request_for_home_and_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """One request: current, hourly, daily, next hour and alerts; the model's answer from it."""
    net = Net(monkeypatch)
    report = _call(kit_for(key[0]))
    assert net.hosts() == ["weatherkit.apple.com"]
    url = urlparse(net.asked[0].split(" ")[0])
    assert url.path == "/api/v1/weather/en/48.48/-123.38"
    query = _query(net.asked[0])
    assert query["dataSets"] == (
        "currentWeather,forecastHourly,forecastDaily,forecastNextHour,weatherAlerts"
    )
    assert query["countryCode"] == "CA"
    assert query["timezone"] == ZONE
    assert "timeout=6.0" in net.asked[0]
    assert set(report) == {
        "place", "timezone", "now", "today", "tomorrow", "next_hours", "next_hour", "alerts",
        "attribution",
    }
    assert report["place"] == "home"
    assert report["now"] == {
        "temp_c": 11.0, "feels_c": 9.5, "condition": "cloudy", "humidity_pct": 80,
        "wind_kmh": 14.0, "gust_kmh": 30.0, "wind_from": "NW", "precip_mm_h": 0.2,
        "uv_index": 1,
    }
    assert report["today"] | {"date": "d"} == {
        "date": "d", "high_c": 15.0, "low_c": 8.0, "condition": "rain", "rain_chance_pct": 70,
        "precip_mm": 3.5, "snow_cm": 0.0, "wind_kmh": 25.0, "uv_index_max": 3,
        "sunrise": "07:30", "sunset": "19:00",
    }
    assert report["tomorrow"]["date"] == _day(1)
    hours = report["next_hours"]
    assert len(hours) == 12
    assert hours[0]["condition"] == "cloudy"  # the first hour is the current reading
    assert hours[1]["condition"] == "drizzle"
    assert set(hours[0]) == {
        "at", "temp_c", "feels_c", "condition", "rain_chance_pct", "precip_mm", "wind_kmh",
    }
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
    _call(kit, {"start_date": _day(2)})
    bearers = [h["Authorization"] for h in net.headers]
    assert len(bearers) == 2
    assert bearers[0] == bearers[1]
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


def test_the_next_hour_says_when_rain_starts_or_stops_or_that_there_is_no_minute_data(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Dry then rain at minute 25; raining now stops at minute 20; none: a plain reason."""
    local = NOW.astimezone(ZoneInfo(ZONE)).replace(second=0)
    Net(monkeypatch)
    soon = _call(kit_for(key[0]))["next_hour"]
    assert soon == {
        "now": "dry", "starts_at": (local + timedelta(minutes=25)).strftime("%H:%M"),
        "stops_at": None, "peak_mm_h": 1.5, "kind": "rain",
        "data_until": (local + timedelta(minutes=60)).strftime("%H:%M"),
    }
    Net(monkeypatch, minutes="raining")
    raining = _call(kit_for(key[0]))["next_hour"]
    assert raining["now"] == "rain"
    assert raining["stops_at"] == (local + timedelta(minutes=20)).strftime("%H:%M")
    Net(monkeypatch, minutes="none")
    none = _call(kit_for(key[0]))["next_hour"]
    assert "no minute-by-minute precipitation" in none["unavailable"]


def test_alerts_come_with_headline_severity_times_and_source_and_need_the_country(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The alert is read in the place's time; a place whose zone has no country is not asked."""
    net = Net(monkeypatch)
    (alert,) = _call(kit_for(key[0]))["alerts"]
    assert alert["headline"] == "Rainfall Warning"
    assert alert["severity"] == "moderate"
    assert alert["source"] == "Environment Canada"
    assert alert["area"] == "Greater Victoria"
    assert alert["effective"].endswith(("-07:00", "-08:00"))
    assert alert["expires"] > alert["effective"]
    net.asked.clear()
    registry = build_default_registry(
        weather_lookup=weather_lookup(
            {"latitude": 48.48, "longitude": -123.38}, kit_for(key[0]), "Etc/UTC",
        ),
    )
    (tool,) = (d for d in registry.get_definitions() if d.name == "weather")
    report = dict(tool.handler({}, None))  # type: ignore[arg-type]
    assert "weatherAlerts" not in net.asked[0]
    assert "countryCode" not in net.asked[0]
    assert "unavailable" in report["alerts"]


def test_a_named_place_is_geocoded_then_read_in_its_own_zone_and_country(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The place goes to the geocoder as given; WeatherKit is asked for what it found."""
    net = Net(monkeypatch)
    report = _call(kit_for(key[0]), {"place": "Toronto"})
    assert net.hosts() == ["geocoding-api.open-meteo.com", "weatherkit.apple.com"]
    assert "name=Toronto" in net.asked[0]
    assert urlparse(net.asked[1].split(" ")[0]).path == "/api/v1/weather/en/43.7/-79.42"
    assert _query(net.asked[1])["timezone"] == "America/Toronto"
    assert _query(net.asked[1])["countryCode"] == "CA"
    assert report["place"] == "Toronto, Ontario, Canada"
    assert report["timezone"] == "America/Toronto"


def test_a_country_with_no_alert_in_force_reads_as_no_alerts(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """WeatherKit omits the alerts set when none are in force: that is [], not unavailable."""
    net = Net(monkeypatch, warned=False)
    report = _call(kit_for(key[0]), {"place": "Toronto"})
    assert "weatherAlerts" in _query(net.asked[-1])["dataSets"]
    assert report["place"] == "Toronto, Ontario, Canada"
    assert report["alerts"] == []


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
    assert net.hosts() == ["weatherkit.apple.com"]  # no geocoding for coordinates
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
    assert net.hosts() == ["weatherkit.apple.com"]
    assert parse_qs(urlparse(net.asked[0].split(" ")[0]).query)["countryCode"] == ["JP"]
    net.asked.clear()
    near = _call(kit_for(key[0]), {"place": "48.5,-123.4"})  # inside 100 km of home: its zone
    assert near["timezone"] == ZONE
    assert net.hosts() == ["weatherkit.apple.com"]


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
    report = _call(kit, {"start_date": _day(0), "end_date": _day(9)})
    query = _query(net.asked[0])
    assert query["dailyStart"].endswith(("T07:00:00Z", "T08:00:00Z"))
    assert "today" not in report
    assert [d["date"] for d in report["days"]] == [_day(n) for n in range(10)]
    assert "next_hours" not in report
    assert "next_hour" in report  # the range holds today
    later = _call(kit, {"start_date": _day(3)})
    assert [d["date"] for d in later["days"]] == [_day(3)]
    assert "next_hour" not in later  # minute rain is for now only
    net.asked.clear()
    for first, last in ((_day(12), _day(12)), (_day(-8), _day(-8)), (_day(5), _day(2))):
        with pytest.raises(ToolError, match=r"forecast runs from|end_date is before start_date"):
            _call(kit, {"start_date": first, "end_date": last})
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        _call(kit, {"start_date": "next friday"})
    with pytest.raises(ToolError, match="needs a start_date"):
        _call(kit, {"end_date": _day(2)})
    assert net.asked == []


def test_yesterday_and_seven_days_back_are_read_and_eight_back_is_refused_naming_the_range(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The place's own date judges the range and names it in full, at any hour of the UTC day."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    assert [d["date"] for d in _call(kit, {"start_date": _day(-1)})["days"]] == [_day(-1)]
    assert [d["date"] for d in _call(kit, {"start_date": _day(-7)})["days"]] == [_day(-7)]
    net.asked.clear()
    with pytest.raises(ToolError) as caught:
        _call(kit, {"start_date": _day(-8)})
    assert _day(-7) in str(caught.value)
    assert _day(9) in str(caught.value)
    assert net.asked == []


def test_a_request_of_days_all_past_asks_for_the_daily_forecast_alone_and_answers_without_now(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Past days: one daily-only request, ``days`` and attribution; into today it is whole."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"start_date": _day(-3), "end_date": _day(-1)})
    assert len(net.asked) == 1
    query = _query(net.asked[0])
    assert query["dataSets"] == "forecastDaily"
    assert not {"hourlyStart", "hourlyEnd", "countryCode"} & set(query)
    assert set(report) == {"place", "timezone", "days", "attribution"}
    assert [d["date"] for d in report["days"]] == [_day(-3), _day(-2), _day(-1)]
    assert report["days"][0]["high_c"] == 15.0
    assert report["days"][0]["precip_mm"] == 3.5
    mixed = _call(kit, {"start_date": _day(-1), "end_date": _day(1)})
    assert [d["date"] for d in mixed["days"]] == [_day(-1), _day(0), _day(1)]
    assert {"now", "next_hour", "alerts"} <= set(mixed)
    assert "dailyStart" in _query(net.asked[-1])
    assert "forecastHourly" in _query(net.asked[-1])["dataSets"]


def test_hourly_rows_for_a_past_day_are_refused_untouched(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Hour rows run from now on: hourly with a day before today is an error, no request made."""
    net = Net(monkeypatch)
    with pytest.raises(ToolError, match="hour-by-hour rows exist from now on"):
        _call(kit_for(key[0]), {"start_date": _day(-1), "hourly": True})
    assert net.asked == []


def test_hourly_rows_cover_the_days_asked_and_air_quality_adds_aqi_rows(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """hourly: every hour of the day asked; air_quality: now and each day's worst."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    report = _call(kit, {"start_date": _day(1), "hourly": True, "air_quality": True})
    rows = report["hourly"]
    assert len(rows) == 24
    assert {row["at"][:10] for row in rows} == {_day(1)}
    assert net.hosts() == ["weatherkit.apple.com", "air-quality-api.open-meteo.com"]
    assert f"start_date={_day(1)}&end_date={_day(1)}" in net.asked[1]
    assert report["air_quality"]["now"] == {"us_aqi": 42, "pm2_5_ugm3": 8.5, "pm10_ugm3": 12.0}
    assert [d["date"] for d in report["air_quality"]["days"]] == [_day(1)]
    default = _call(kit, {"hourly": True})  # no dates: the rest of today and all of tomorrow
    assert default["hourly"][-1]["at"].startswith(_day(1))
    assert "next_hours" not in default
    assert "air_quality" not in default
    with pytest.raises(ToolError, match="at most 3 days"):
        _call(kit, {"start_date": _day(0), "end_date": _day(3), "hourly": True})


def test_here_reads_the_location_seam_and_says_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """``here`` reads the fix without geocoding; far away its own zone; no reader: error."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    fix = {"lat": 48.45, "lng": -123.37, "accuracy_m": 30.0, "place": "Government St, Victoria"}
    report = _call(kit, {"place": "Here"}, here=lambda: fix)
    assert net.hosts() == ["weatherkit.apple.com"]
    assert urlparse(net.asked[0].split(" ")[0]).path == "/api/v1/weather/en/48.45/-123.37"
    assert report["place"] == "Government St, Victoria"
    assert report["location_from"] == "this Mac"
    away = {"lat": 43.65, "lng": -79.38, "accuracy_m": 30.0, "place": "King St, Toronto"}
    assert _call(kit, {"place": "here"}, here=lambda: away)["timezone"] == "America/Toronto"

    def denied() -> dict[str, Any]:
        msg = "denied"
        raise PermissionError(msg)

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
        ("weatherkit.apple.com", {"place": "Toronto"}, "weather: the forecast"),
    ],
)
def test_each_failed_read_is_a_tool_error_that_names_it(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    down: str, args: dict[str, Any], failed: str,
) -> None:
    """The geocoder or WeatherKit down: the error says which."""
    Net(monkeypatch, down=(down,))
    with pytest.raises(ToolError, match=f"{failed} could not be read") as caught:
        _call(kit_for(key[0]), args)
    assert caught.value.code == "network_error"


def test_a_401_from_apple_gives_a_plain_reason_without_the_token(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Apple refuses the key: the error names the status and Apple's reason, nothing secret."""
    net = Net(monkeypatch, refuse=401)
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolError) as caught:
        _call(kit_for(key[0]))
    assert caught.value.code == "weatherkit_refused"
    assert str(caught.value) == (
        "weather: Apple WeatherKit refused the key (401 NOT_ENABLED); tell him the weather key"
        " needs checking, and use web_search meanwhile"
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
    assert "today" in result
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
            assert WeatherKit.from_config(block) is None
        assert [r.getMessage().startswith("weather: off (") for r in caplog.records] == [True]
        assert secret not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert WeatherKit.from_config(config(path)) is not None
    assert [r.getMessage() for r in caplog.records] == ["weather: Apple WeatherKit"]


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
    assert len(net.asked) == 1
    now[0] += 299
    _call(kit)
    assert len(net.asked) == 1
    now[0] += 2
    _call(kit)
    assert len(net.asked) == 2


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
    for word in ("web_search", "next_hour", "alerts", "Apple Weather", "9 days", "7 days"):
        assert word in definition.description
    assert "weather" not in {d.name for d in build_default_registry().get_definitions()}
    assert weather_lookup(None, None, ZONE) is None
    # Without a key the tool is still offered: it answers with the reason.
    unkeyed = build_default_registry(weather_lookup=weather_lookup(PLACE, None, ZONE))
    assert "weather" in {d.name for d in unkeyed.get_definitions()}


def test_the_description_offers_here_only_where_a_location_can_be_read(
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """``here`` is in the text and the schema only with a Mac or phone location reader."""
    lookup = weather_lookup(PLACE, kit_for(key[0]), ZONE)
    plain = build_default_registry(weather_lookup=lookup)
    located = build_default_registry(weather_lookup=lookup, here_location=dict)
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
