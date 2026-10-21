"""ADR 0285: Apple WeatherKit and Open-Meteo, faked over ``urlopen``, and a throwaway signing key.

The WeatherKit answer is built from the request (the days, the hours, the time zone, the data
sets asked for), so a request that asks for the wrong place, days or zone reads wrong data. No
real call is made and no real key exists: the key is a P-256 key generated into a temp dir.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from jarvis.execution.tools import Tool, build_default_registry
from jarvis.runtime.home import weather_lookup
from jarvis.runtime.weatherkit import WeatherKit
from tests.support.clock import FakeClock
from tests.support.runtime_config import runtime_config
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.support.here import FixedHere

type HourProfile = Callable[[datetime], dict[str, Any]]
"""Fields of the hourly row that begins at a local time, laid over the default row."""

PLACE = {"latitude": 48.48, "longitude": -123.38, "country_code": "CA"}
"""The home the tests read the weather of: a public city, Victoria."""
ZONE = "America/Vancouver"
KEY_ID = "ABC123DEFG"
TEAM_ID = "3MEBVQ3N3U"
SERVICE_ID = "com.example.jarvis.weather"
NOW = datetime(2026, 10, 21, 16, 30, 30, tzinfo=UTC)  # Wednesday 09:30 in Victoria, mid-hour
GEOCODED: dict[tuple[str, str], list[dict[str, Any]]] = {}
CANADA, USA = ("Canada", "CA"), ("United States", "US")


def place_match(
    name: str, point: tuple[float, float], region: str, country: tuple[str, str], zone: str,
) -> dict[str, Any]:
    """One geocoder match, as Open-Meteo gives it."""
    return {
        "name": name, "latitude": point[0], "longitude": point[1], "admin1": region,
        "country": country[0], "country_code": country[1], "timezone": zone,
    }


BC = "British Columbia"
RICHMOND_VA = place_match("Richmond", (37.55, -77.46), "Virginia", USA, "America/New_York")
RICHMOND_BC = place_match("Richmond", (49.17, -123.14), BC, CANADA, "America/Vancouver")
RICHMOND_CA = place_match("Richmond", (37.94, -122.35), "California", USA, "America/Los_Angeles")
RICHMOND_HILL = place_match("Richmond Hill", (43.87, -79.44), "Ontario", CANADA, "America/Toronto")
VANCOUVER_BC = place_match("Vancouver", (49.25, -123.12), BC, CANADA, "America/Vancouver")
VANCOUVER_WA = place_match("Vancouver", (45.64, -122.66), "Washington", USA, "America/Los_Angeles")
SPRINGFIELDS = [
    place_match("Springfield", (39.8 + n, -89.6 - n), region, USA, "America/Chicago")
    for n, region in enumerate((
        "Illinois", "Missouri", "Massachusetts", "Oregon", "Ohio", "Tennessee", "Virginia",
    ))
]
GEOCODED.update({  # the geocoder lists the places of a name in its own order, not nearest first
    ("Richmond", "en"): [RICHMOND_VA, RICHMOND_HILL, RICHMOND_BC, RICHMOND_CA],
    ("Vancouver", "en"): [VANCOUVER_WA, VANCOUVER_BC],
    ("温哥华", "zh"): [place_match(
        "温哥华", (49.25, -123.12), "不列颠哥伦比亚省", ("加拿大", "CA"), "America/Vancouver",
    )],
    ("Springfield", "en"): SPRINGFIELDS,
})


def make_key(directory: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    """A new P-256 key written as a PKCS#8 PEM file (mode 0600) in ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    path = directory / "weatherkit.p8"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    path.chmod(0o600)
    return path, key


def config(path: Path) -> dict[str, Any]:
    """The ``weather`` block as the owner writes it."""
    return {"weather": {"weatherkit": {
        "key_id": KEY_ID, "team_id": TEAM_ID, "service_id": SERVICE_ID, "key_file": str(path),
    }}}


def kit_for(path: Path) -> WeatherKit:
    """The client over the key file, its clock fixed at :data:`NOW`."""
    kit = WeatherKit.from_settings(runtime_config(config(path)).weatherkit)
    assert kit is not None
    kit.clock = NOW.timestamp
    return kit


_TOOLS: dict[tuple[int, int, str, tuple[tuple[str, Any], ...]], Tool] = {}


def call(
    kit: WeatherKit | None, args: dict[str, Any] | None = None, here: FixedHere | None = None,
    *, home: dict[str, Any] | None = None, zone: str = ZONE,
) -> dict[str, Any]:
    """What the model's ``weather`` tool answers, at :data:`NOW`, for ``args``.

    One lookup per client and reader, as the daemon builds it: it remembers the places it listed.
    ``home`` and ``zone`` are the home's place (:data:`PLACE`) and the local time zone.
    """
    place = home or PLACE
    token = (id(kit), id(here), zone, tuple(sorted(place.items())))
    if token not in _TOOLS:
        registry = build_default_registry(
            weather_lookup=weather_lookup(place, kit, zone), here=here,
        )
        (_TOOLS[token],) = (d for d in registry.get_definitions() if d.name == "weather")
    ctx = tool_context(clock=FakeClock(NOW), here=None if here is None else here.bind(None))
    return dict(_TOOLS[token].handler(args or {}, ctx))


def b64(text: str) -> bytes:
    """Decode one base64url JWT segment."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def drizzle_before_dawn(at: datetime) -> dict[str, Any]:
    """Today's drizzle from 4:00 to 7:00 (1 mm in all), dry after, the low 6° at 6:00.

    The days after today are dry and cloudy; the daily rows still say rain, as a day row does.
    """
    today = at.date() == NOW.astimezone(at.tzinfo).date()
    amount = {4: 0.4, 5: 0.3, 6: 0.3}.get(at.hour, 0.0) if today else 0.0
    return {
        "conditionCode": "Drizzle" if amount else "Cloudy",
        "precipitationAmount": amount, "precipitationChance": 0.9 if amount else 0.0,
        "temperature": 6.0 + abs(at.hour - 6) / 2,
    }


def rain_all_day(at: datetime) -> dict[str, Any]:
    """Rain every hour, at its worst: every field of every row is there to be written."""
    return {
        "conditionCode": "HeavyRain", "precipitationAmount": 1.2, "precipitationChance": 0.9,
        "temperature": 8.0 + abs(at.hour - 6) / 3,
    }


ALERT_ID = "7c9a5f1e-2b3d-4e8a-9f60-a1b2c3d4e5f"
"""An alert's id, a UUID with a digit to end it for each alert in force."""
ALERT_TEXT = (
    "Heavy rain is expected.   Rainfall amounts of 50 to 70 mm are possible\nby Thursday morning."
    " Rain may cause flooding in low-lying areas and water pooling on roads. Be careful near"
    " creeks and storm drains, and leave early if a road is closed ahead. Updates will be issued"
    " as conditions change, so check the local road reports before you travel."
)
"""An alert's full text as WeatherKit gives it, spaces and line breaks included."""
_SEVERITIES = ("moderate", "minor", "extreme", "severe", "moderate")
_HEADLINES = (
    "Rainfall Warning", "Special Weather Statement", "Storm Surge Warning", "Wind Warning",
    "Freezing Rain Warning",
)

# For each pattern: the minutes that rain with their chance, and the summary periods
# (start minute, condition, chance) WeatherKit would give for the hour; None leaves no summary.
type _Periods = list[tuple[int, str, float | None]]
_MINUTES: dict[str, tuple[range, float, _Periods | None]] = {
    "dry-then-rain": (
        range(25, 45), 0.8, [(0, "clear", None), (25, "rain", 0.8), (45, "clear", None)],
    ),
    "raining": (range(20), 0.8, [(0, "rain", 0.8), (20, "clear", None)]),
    # Minutes with some intensity at a 20% chance, in a summary that says clear: no shower.
    "low-chance": (range(25, 45), 0.2, [(0, "clear", None)]),
    # The minutes begin at 09:54; the summary's period begins at 09:52 and is the one to follow.
    "summary-leads": (
        range(24, 44), 0.7, [(0, "clear", None), (22, "rain", 0.7), (44, "clear", None)],
    ),
    "none": (range(0), 0.0, None),
}


def _next_hour(minutes: str, *, summary: bool) -> dict[str, Any]:
    """WeatherKit's ``forecastNextHour`` for one of the patterns above, from the minute of NOW."""
    wet, chance, periods = _MINUTES[minutes]
    start = NOW.replace(second=0)
    rows = [
        {
            "startTime": _z(start + timedelta(minutes=n)),
            "precipitationIntensity": 1.5 if n in wet else 0.0,
            "precipitationChance": chance if n in wet else 0.0,
        }
        for n in range(60)
    ]
    body: dict[str, Any] = {"minutes": rows}
    if summary and periods is not None:
        body["summary"] = [
            {
                "startTime": _z(start + timedelta(minutes=at)), "condition": condition,
                "precipitationChance": odds, "precipitationIntensity": 1.5 if odds else 0.0,
                **(
                    {"endTime": _z(start + timedelta(minutes=periods[n + 1][0]))}
                    if n + 1 < len(periods) else {}
                ),
            }
            for n, (at, condition, odds) in enumerate(periods)
        ]
    return body


def weatherkit_body(
    query: dict[str, str], *, minutes: str = "dry-then-rain", warned: int = 1,
    hours: HourProfile | None = None, summary: bool = True,
) -> dict[str, Any]:
    """The WeatherKit answer for ``query``; ``minutes`` is a pattern of :data:`_MINUTES`.

    ``warned`` is how many alerts are in force; with none WeatherKit leaves the set out of its
    answer. ``hours`` sets the fields of each hourly row from its local start; ``summary`` False
    leaves the next hour's summary out.
    """
    zone = ZoneInfo(query["timezone"])
    sets = query["dataSets"].split(",")
    d0 = _parse(query["dailyStart"]).astimezone(zone)
    d1 = _parse(query["dailyEnd"]).astimezone(zone)
    h0, h1 = (_parse(query[k]) for k in ("hourlyStart", "hourlyEnd")) if "hourlyStart" in query \
        else (d0, d0)
    body: dict[str, Any] = {}
    if "currentWeather" in sets:
        body["currentWeather"] = {
            "asOf": _z(NOW), "conditionCode": "Cloudy", "humidity": 0.8,
            "precipitationIntensity": 0.2, "temperature": 11.04, "temperatureApparent": 9.5,
            "uvIndex": 1, "windDirection": 315, "windGust": 30.0, "windSpeed": 14.0,
        }
    if "forecastDaily" in sets:
        days: list[dict[str, Any]] = []
        day = d0
        while day < d1:
            nxt = day + timedelta(days=1)
            days.append({
                "forecastStart": _z(day), "forecastEnd": _z(nxt), "conditionCode": "Rain",
                "maxUvIndex": 3, "precipitationAmount": 3.5, "precipitationChance": 0.7,
                "snowfallAmount": 0.0, "sunrise": _z(day + timedelta(hours=7, minutes=30)),
                "sunset": _z(day + timedelta(hours=19)), "temperatureMax": 15.0 + len(days),
                "temperatureMin": 8.0 + len(days), "daytimeForecast": {"windSpeed": 25.0},
            })
            day = nxt
        body["forecastDaily"] = {"days": days}
    if "forecastHourly" in sets:
        hourly_rows: list[dict[str, Any]] = []
        at = h0
        while at < h1:
            row = {
                "forecastStart": _z(at), "conditionCode": "Drizzle",
                "precipitationAmount": 0.1, "precipitationChance": 0.4,
                "temperature": 10.0 + at.astimezone(zone).hour / 4, "temperatureApparent": 9.0,
                "windSpeed": 12.0,
            }
            hourly_rows.append(row | (hours(at.astimezone(zone)) if hours else {}))
            at += timedelta(hours=1)
        body["forecastHourly"] = {"hours": hourly_rows}
    if "forecastNextHour" in sets and minutes != "none":
        body["forecastNextHour"] = _next_hour(minutes, summary=summary)
    if "weatherAlerts" in sets and warned:
        body["weatherAlerts"] = {"alerts": [
            {
                "id": f"{ALERT_ID}{n}",
                "description": _HEADLINES[n],
                "severity": _SEVERITIES[n], "areaName": "Greater Victoria",
                "source": "Environment Canada", "effectiveTime": _z(NOW),
                "expireTime": _z(NOW + timedelta(hours=12)),
                "detailsUrl": "https://weather.gc.ca/warnings",
            }
            for n in range(warned)
        ]}
    return body


def alert_detail() -> dict[str, Any]:
    """What WeatherKit gives for one alert by its id: the words in each language and its times."""
    return {
        "messages": [
            {"language": "fr", "text": "Des pluies abondantes sont prévues."},
            {"language": "en", "text": ALERT_TEXT},
        ],
        "eventOnsetTime": _z(NOW + timedelta(hours=2)),
        "eventEndTime": _z(NOW + timedelta(hours=14)),
    }


def _series(query: dict[str, str]) -> dict[str, Any]:
    """Open-Meteo's air quality (synthetic values) over the days of ``query``."""
    offset = -25200 if float(query["longitude"]) < -100 else -14400
    local = NOW + timedelta(seconds=offset)
    if "start_date" in query:
        first = datetime.fromisoformat(query["start_date"]).date()
        last = datetime.fromisoformat(query["end_date"]).date()
    else:
        first, last = local.date(), local.date() + timedelta(days=int(query["forecast_days"]) - 1)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    hours = [
        int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()) - offset + h * 3600
        for d in days for h in range(24)
    ]
    return {
        "timezone": "America/Vancouver", "utc_offset_seconds": offset,
        "current": {"time": int(NOW.timestamp()), "us_aqi": 42, "pm2_5": 8.5, "pm10": 12.0},
        "hourly": {
            "time": hours, "us_aqi": [30 + h % 24 for h in range(len(hours))],
            "pm2_5": [5.0 + h % 24 for h in range(len(hours))], "pm10": [None] * len(hours),
        },
    }


class Net:
    """The network, faked: records every request, answers from the query, fails on request."""

    def __init__(  # noqa: PLR0913 - one switch for each thing the fake network can do
        self, monkeypatch: pytest.MonkeyPatch, *, down: tuple[str, ...] = (),
        refuse: int | None = None, minutes: str = "dry-then-rain", warned: int = 0,
        hours: HourProfile | None = None, service: bool = True, summary: bool = True,
        fail: tuple[str, ...] = (), hours_at: dict[str, HourProfile] | None = None,
    ) -> None:
        """Replace ``urlopen``; ``down`` lists hosts that time out, ``refuse`` an HTTP status.

        ``warned`` is how many alerts are in force; ``service`` whether WeatherKit offers alerts
        at the place; ``summary`` whether the next hour has one; ``fail`` the path prefixes of
        WeatherKit that answer 503 while the rest answer; ``hours_at`` the hourly profile of a
        place by the ``lat,lng`` in the request's path, over ``hours``. Every switch is an
        attribute a test may set again between two calls.
        """
        self.asked: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.down, self.refuse, self.minutes, self.warned = down, refuse, minutes, warned
        self.hours, self.service = hours, service
        self.hours_at = hours_at or {}
        self.summary, self.fail = summary, fail
        self.reason = "NOT_ENABLED"
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, target: Any, *, timeout: float) -> io.BytesIO:  # noqa: ANN401
        url = target if isinstance(target, str) else target.full_url
        self.asked.append(f"{url} timeout={timeout}")
        self.headers.append({} if isinstance(target, str) else dict(target.header_items()))
        parts = urlparse(url)
        host = parts.hostname or ""
        query = {k: unquote(v[0]) for k, v in parse_qs(parts.query).items()}
        if host in self.down:
            raise TimeoutError
        body: Any
        if host == "weatherkit.apple.com":
            if self.refuse or parts.path.startswith(self.fail or ("\0",)):
                raise urllib.error.HTTPError(
                    url, self.refuse or 503, "refused", {},  # type: ignore[arg-type]
                    io.BytesIO(json.dumps({"reason": self.reason}).encode()),
                )
            if parts.path.startswith("/api/v1/availability/"):
                body = [
                    "currentWeather", "forecastDaily", "forecastHourly", "forecastNextHour",
                    *(["weatherAlerts"] if self.service else []),
                ]
            elif parts.path.startswith("/api/v1/weatherAlert/"):
                body = alert_detail()
            else:
                point = ",".join(parts.path.split("/")[-2:])
                body = weatherkit_body(
                    query, minutes=self.minutes, warned=self.warned,
                    hours=self.hours_at.get(point, self.hours), summary=self.summary,
                )
        elif host.startswith("geocoding"):
            found = next(  # the geocoder does not mind the case of a name
                one for (name, language), one in GEOCODED.items()
                if name.casefold() == query["name"].casefold() and language == query["language"]
            )
            body = {"results": found} if found else {"generationtime_ms": 0.1}
        else:
            body = _series(query)
        return io.BytesIO(json.dumps(body).encode())

    def hosts(self) -> list[str]:
        """The host of each request, in order."""
        return [urlparse(one.split(" ")[0]).hostname or "" for one in self.asked]

    def weather(self) -> list[str]:
        """The forecast requests, in order: not availability, and not an alert's text."""
        return [one for one in self.asked if "/api/v1/weather/" in one]

    def paths(self) -> list[str]:
        """The path of each request to WeatherKit, in order."""
        return [
            urlparse(one.split(" ")[0]).path for one in self.asked
            if urlparse(one.split(" ")[0]).hostname == "weatherkit.apple.com"
        ]
