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

from jarvis.runtime.weatherkit import WeatherKit
from tests.support.runtime_config import runtime_config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

KEY_ID = "ABC123DEFG"
TEAM_ID = "3MEBVQ3N3U"
SERVICE_ID = "com.example.jarvis.weather"
NOW = datetime.now(UTC).replace(minute=30, second=30, microsecond=0)  # mid-hour: +5 min, same hour
GEOCODED: dict[tuple[str, str], list[dict[str, Any]]] = {}


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


def b64(text: str) -> bytes:
    """Decode one base64url JWT segment."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def weatherkit_body(
    query: dict[str, str], *, minutes: str = "dry-then-rain", warned: bool = True,
) -> dict[str, Any]:
    """The WeatherKit answer for ``query``; ``minutes`` is one of dry-then-rain, raining, none.

    With ``warned`` False no alert is in force, and WeatherKit leaves the set out of its answer.
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
        hours: list[dict[str, Any]] = []
        at = h0
        while at < h1:
            hours.append({
                "forecastStart": _z(at), "conditionCode": "Drizzle",
                "precipitationAmount": 0.1, "precipitationChance": 0.4,
                "temperature": 10.0 + at.astimezone(zone).hour / 4, "temperatureApparent": 9.0,
                "windSpeed": 12.0,
            })
            at += timedelta(hours=1)
        body["forecastHourly"] = {"hours": hours}
    if "forecastNextHour" in sets and minutes != "none":
        start = NOW.replace(second=0)
        rows: list[dict[str, Any]] = []
        for n in range(60):
            wet = (minutes == "raining" and n < 20) or (minutes == "dry-then-rain" and 25 <= n < 45)
            rows.append({
                "startTime": _z(start + timedelta(minutes=n)),
                "precipitationIntensity": 1.5 if wet else 0.0, "precipitationChance": 0.8,
            })
        body["forecastNextHour"] = {
            "minutes": rows,
            "summary": [{"startTime": rows[0]["startTime"], "condition": "rain",
                         "precipitationChance": 0.8, "precipitationIntensity": 1.5}],
        }
    if "weatherAlerts" in sets and warned:
        body["weatherAlerts"] = {"alerts": [{
            "description": "Rainfall Warning", "severity": "moderate",
            "areaName": "Greater Victoria",
            "source": "Environment Canada", "effectiveTime": _z(NOW),
            "expireTime": _z(NOW + timedelta(hours=12)),
            "detailsUrl": "https://weather.gc.ca/warnings",
        }]}
    return body


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

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, down: tuple[str, ...] = (),
        refuse: int | None = None, minutes: str = "dry-then-rain", warned: bool = True,
    ) -> None:
        """Replace ``urlopen``; ``down`` lists hosts that time out, ``refuse`` an HTTP status."""
        self.asked: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.down, self.refuse, self.minutes, self.warned = down, refuse, minutes, warned
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
        if host == "weatherkit.apple.com":
            if self.refuse:
                raise urllib.error.HTTPError(
                    url, self.refuse, "refused", {}, io.BytesIO(b'{"reason":"NOT_ENABLED"}'),  # type: ignore[arg-type]
                )
            body = weatherkit_body(query, minutes=self.minutes, warned=self.warned)
        elif host.startswith("geocoding"):
            found = GEOCODED[query["name"], query["language"]]
            body = {"results": found} if found else {"generationtime_ms": 0.1}
        else:
            body = _series(query)
        return io.BytesIO(json.dumps(body).encode())

    def hosts(self) -> list[str]:
        """The host of each request, in order."""
        return [urlparse(one.split(" ")[0]).hostname or "" for one in self.asked]
