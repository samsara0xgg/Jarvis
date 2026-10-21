"""ADR 0285: Apple WeatherKit, the one source of the weather, as the model and the home read it.

:class:`WeatherKit` signs the developer token (ES256, through the APNs provider token class),
asks ``weatherkit.apple.com`` for the current conditions, hourly and daily forecasts, the
next-hour minute precipitation and the official alerts of one place in one request, and holds an
answer for :data:`TTL_S` so the home and a turn's tool call do not pay twice. The functions below
it turn that answer into the words the model reads (:func:`report`) and the home's three-hourly
line (:func:`home_line`).

Nothing here logs or returns the key, the token or the request's headers: a failure is a
:class:`~jarvis.execution.tools.ToolError` that names what failed and Apple's own ``reason`` word.
"""

from __future__ import annotations

import json
import logging
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as day_start
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from jarvis.execution.tools import ToolError
from jarvis.execution.weather_tool import DAYS_AHEAD, DAYS_BACK
from jarvis.surface.apns import ProviderToken, load_signing_key

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)
ENDPOINT: Final = "https://weatherkit.apple.com/api/v1/weather"
ATTRIBUTION: Final = {
    "provider": "Apple Weather",
    "url": "https://weatherkit.apple.com/legal-attribution.html",
}
"""What Apple requires wherever its weather is shown: the trademark and the legal page link."""
NOT_SET_UP: Final = "weather is not set up: WeatherKit key missing"
DEFAULT_TEAM_ID: Final = "3MEBVQ3N3U"
TTL_S: Final = 300
"""How long an answer is reused: the minute precipitation is a few minutes old at most."""
TIMEOUT_S: Final = 6.0
TOKEN_LIFETIME_S: Final = 3600
_REFUSED: Final = 401
_NEXT_HOUR_MIN: Final = 60
_HOME_STEP_H: Final = 3
_HOME_HOURS: Final = 4
_REPORT_HOURS: Final = 12
_COMPASS: Final = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW",
    "NNW",
)
_CAMEL: Final = re.compile(r"(?<=[a-z])(?=[A-Z])")
_REASON: Final = re.compile(r"^[A-Za-z0-9_ .-]{1,40}$")
_KINDS: Final = (
    (("Thunder", "Hurricane", "Tornado", "TropicalStorm"), "storm"),
    (("Snow", "Flurries", "Sleet", "Blizzard", "Hail", "Frigid"), "snow"),
    (("Fog", "Haze", "Smoke", "Dust"), "fog"),
    (("Clear", "Sunny", "Hot"), "sun"),
    (("Cloud", "Breezy", "Windy"), "cloud"),
)
_RAIN_WORDS: Final = {"precipitation": "rain", "clear": "none"}


def words(code: str) -> str:
    """``MostlyCloudy`` as ``mostly cloudy``."""
    return _CAMEL.sub(" ", code or "unknown").lower()


def kind(code: str) -> str:
    """The home's six icons: sun, cloud, fog, rain, snow, storm."""
    return next((one for keys, one in _KINDS if any(k in code for k in keys)), "rain")


@dataclass(frozen=True)
class WeatherKitSettings:
    """The ``weather.weatherkit`` block: the Apple key's file and its ids, each blank where unset.

    ``team_id`` defaults to the owner's.
    """

    key_id: str
    service_id: str
    team_id: str
    key_file: str

    @classmethod
    def from_config(cls, weather: object) -> WeatherKitSettings:
        """The settings of the ``weather:`` block; one that is not a mapping is all blank."""
        block = weather.get("weatherkit") if isinstance(weather, dict) else None
        block = block if isinstance(block, dict) else {}
        key_id, service_id = (str(block.get(name) or "") for name in ("key_id", "service_id"))
        return cls(
            key_id=key_id, service_id=service_id,
            team_id=str(block.get("team_id") or DEFAULT_TEAM_ID),
            key_file=str(block.get("key_file") or ""),
        )


class WeatherKit:
    """The signed client; built once at boot, safe to call from any thread."""

    def __init__(
        self, token: ProviderToken, clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the token signer; ``clock`` is the time the cache and the minutes are read by."""
        self._token = token
        self.clock = clock
        self._lock = threading.Lock()
        self._held: dict[str, tuple[float, dict[str, Any]]] = {}

    @classmethod
    def from_settings(cls, settings: WeatherKitSettings) -> WeatherKit | None:
        """The client for ``weather.weatherkit``, or None; logs exactly one line saying which.

        Needs ``key_id``, ``service_id`` and a readable ``key_file`` (mode 0600; ``~`` allowed);
        ``team_id`` defaults to the owner's. Never raises: a missing or broken key leaves the
        weather off (the tool then says so), it does not stop the boot.
        """
        key_id, service_id = settings.key_id, settings.service_id
        team_id, named = settings.team_id, settings.key_file
        why = ""
        if not (key_id and service_id and named):
            why = "weather.weatherkit needs key_id, service_id and key_file"
        else:
            path = Path(named).expanduser()
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    why = "weather.weatherkit.key_file must be a regular file, mode 0600"
                else:
                    pem = path.read_bytes()
            except OSError:
                why = "weather.weatherkit.key_file cannot be read"
        if not why:
            try:
                token = ProviderToken(
                    load_signing_key(pem), key_id, team_id,
                    header={"id": f"{team_id}.{service_id}"}, claims={"sub": service_id},
                    lifetime_s=TOKEN_LIFETIME_S,
                )
            except ValueError as exc:
                why = f"the WeatherKit key is unusable: {exc}"
        if why:
            LOGGER.warning("weather: off (%s)", why)
            return None
        LOGGER.info("weather: Apple WeatherKit")
        return cls(token)

    def read(  # noqa: PLR0913 - one request: the point, its zone and country, the days.
        self, latitude: float, longitude: float, *, zone: str, country: str | None,
        first: date, last: date,
    ) -> dict[str, Any]:
        """WeatherKit's answer for ``first`` to ``last`` (the place's own days); raises ToolError.

        Daily rows for those days, hourly rows from now (or ``first``) to the end of ``last``,
        the current conditions, the next hour's minutes and, when ``country`` is known, alerts.
        Days all before today get the daily rows alone: nothing live applies to them.
        """
        tz = ZoneInfo(zone)
        now = self.clock()
        begin = datetime.combine(first, day_start(), tz)
        end = datetime.combine(last + timedelta(days=1), day_start(), tz)
        query = {"timezone": zone, "dailyStart": _utc(begin), "dailyEnd": _utc(end)}
        if last < datetime.fromtimestamp(now, tz).date():
            query["dataSets"] = "forecastDaily"
        else:
            hourly = max(begin, datetime.fromtimestamp(now // 3600 * 3600, tz))
            sets = ["currentWeather", "forecastHourly", "forecastDaily", "forecastNextHour"]
            query["dataSets"] = ",".join([*sets, "weatherAlerts"] if country else sets)
            query["hourlyStart"] = _utc(hourly)
            query["hourlyEnd"] = _utc(max(end, hourly + timedelta(hours=1)))
            if country:
                query["countryCode"] = country
        url = f"{ENDPOINT}/en/{latitude}/{longitude}?{urlencode(query, safe=',:/')}"
        with self._lock:
            held = self._held.get(url)
            if held is not None and now - held[0] < TTL_S:
                return held[1]
            body = self._get(url)
            self._held = {u: h for u, h in self._held.items() if now - h[0] < TTL_S}
            self._held[url] = (now, body)
            return body

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL.
            url, headers={"Authorization": f"Bearer {self._token.current()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:  # noqa: S310
                body: dict[str, Any] = json.load(reply)
        except urllib.error.HTTPError as exc:
            reason = _reason(exc)
            if exc.code == _REFUSED:
                self._token.discard()
                msg = (
                    f"weather: Apple WeatherKit refused the key (401 {reason}); tell him the"
                    " weather key needs checking, and use web_search meanwhile"
                )
                raise ToolError(msg, code="weatherkit_refused") from None
            msg = (
                f"weather: Apple WeatherKit answered {exc.code} {reason}; use web_search, or"
                " tell him it is unavailable"
            )
            raise ToolError(msg, code="network_error") from None
        except Exception as exc:  # timeout, refusal, bad JSON: all one short reason.
            msg = (
                f"weather: the forecast could not be read ({type(exc).__name__}); "
                "use web_search, or tell him it is unavailable"
            )
            raise ToolError(msg, code="network_error") from exc
        return body


def _reason(exc: urllib.error.HTTPError) -> str:
    """Apple's ``reason`` word from an error body, else ``""``; never the body itself."""
    try:
        reason = json.load(exc).get("reason", "")
    except Exception:  # noqa: BLE001 - an unreadable body only loses the word.
        return ""
    return reason if isinstance(reason, str) and _REASON.match(reason) else ""


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text: str, zone: tzinfo) -> datetime:
    return datetime.fromisoformat(text).astimezone(zone)


def _hm(text: str | None, zone: ZoneInfo) -> str | None:
    return None if not text else _when(text, zone).strftime("%H:%M")


def _num(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _pct(value: float | None) -> int | None:
    return None if value is None else round(value * 100)


def _now(current: Mapping[str, Any]) -> dict[str, Any]:
    direction = current.get("windDirection")
    return {
        "temp_c": _num(current.get("temperature")),
        "feels_c": _num(current.get("temperatureApparent")),
        "condition": words(current.get("conditionCode", "")),
        "humidity_pct": _pct(current.get("humidity")),
        "wind_kmh": _num(current.get("windSpeed")),
        "gust_kmh": _num(current.get("windGust")),
        "wind_from": None if direction is None else _COMPASS[round(direction / 22.5) % 16],
        "precip_mm_h": _num(current.get("precipitationIntensity")),
        "uv_index": current.get("uvIndex"),
    }


def _day(row: Mapping[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    daytime = row.get("daytimeForecast") or {}
    return {
        "date": _when(row["forecastStart"], zone).date().isoformat(),
        "high_c": _num(row.get("temperatureMax")),
        "low_c": _num(row.get("temperatureMin")),
        "condition": words(row.get("conditionCode", "")),
        "rain_chance_pct": _pct(row.get("precipitationChance")),
        "precip_mm": _num(row.get("precipitationAmount")),
        "snow_cm": _num((row.get("snowfallAmount") or 0) / 10),
        "wind_kmh": _num(daytime.get("windSpeed")),
        "uv_index_max": row.get("maxUvIndex"),
        "sunrise": _hm(row.get("sunrise"), zone),
        "sunset": _hm(row.get("sunset"), zone),
    }


def _hour(row: Mapping[str, Any], zone: ZoneInfo, code: str | None = None) -> dict[str, Any]:
    return {
        "at": _when(row["forecastStart"], zone).strftime("%Y-%m-%d %H:%M"),
        "temp_c": _num(row.get("temperature")),
        "feels_c": _num(row.get("temperatureApparent")),
        "condition": words(row.get("conditionCode", "") if code is None else code),
        "rain_chance_pct": _pct(row.get("precipitationChance")),
        "precip_mm": _num(row.get("precipitationAmount")),
        "wind_kmh": _num(row.get("windSpeed")),
    }


def next_hour(body: Mapping[str, Any], zone: ZoneInfo, now: float) -> dict[str, Any]:
    """The coming hour's precipitation by the minute, as when it starts and stops."""
    data = body.get("forecastNextHour")
    if not data or not data.get("minutes"):
        return {"unavailable": "no minute-by-minute precipitation exists for this place"}
    rows = [(_when(one["startTime"], zone), one) for one in data["minutes"]]
    rows = [(at, one) for at, one in rows if at.timestamp() + _NEXT_HOUR_MIN > now]
    if not rows:
        return {"unavailable": "the minute-by-minute precipitation is out of date"}
    wet = [(at, (one.get("precipitationIntensity") or 0) > 0) for at, one in rows]
    raining = wet[0][1]
    flip = next((at for at, is_wet in wet if is_wet != raining), None)
    precip = [s for s in data.get("summary", ()) if s.get("condition") != "clear"]
    label = _RAIN_WORDS.get(str(precip[0]["condition"]), str(precip[0]["condition"])) \
        if precip else "rain"
    peak = max((one.get("precipitationIntensity") or 0 for _, one in rows), default=0)
    out: dict[str, Any] = {
        "now": label if raining else "dry",
        "starts_at": None if raining or flip is None else flip.strftime("%H:%M"),
        "stops_at": flip.strftime("%H:%M") if raining and flip is not None else None,
        "peak_mm_h": _num(peak),
        "data_until": (rows[-1][0] + timedelta(minutes=1)).strftime("%H:%M"),
    }
    if not raining and flip is not None:
        out["kind"] = label
    return out


def alerts(
    body: Mapping[str, Any], zone: ZoneInfo, *, asked: bool,
) -> list[dict[str, Any]] | dict[str, str]:
    """The official alerts in force or coming; ``{"unavailable": why}`` when not ``asked`` for.

    WeatherKit leaves ``weatherAlerts`` out of its answer when none are in force, so an asked
    for set that is missing is no alerts, not an unknown.
    """
    if not asked:
        return {"unavailable": "alerts are read only where the country is known"}
    data = body.get("weatherAlerts") or {}
    return [
        {
            "headline": one.get("description"), "severity": one.get("severity"),
            "area": one.get("areaName"), "source": one.get("source"),
            "effective": _when(one["effectiveTime"], zone).isoformat(timespec="minutes")
            if one.get("effectiveTime") else None,
            "expires": _when(one["expireTime"], zone).isoformat(timespec="minutes")
            if one.get("expireTime") else None,
            "details_url": one.get("detailsUrl"),
        }
        for one in data.get("alerts", ())
    ]


def report(  # noqa: PLR0913 - what the tool answers with: the answer and what was asked of it.
    body: Mapping[str, Any], label: str, zone_name: str, *,
    first: date, last: date, whole: bool, hourly: bool, now: float, alerts_asked: bool,
) -> dict[str, Any]:
    """What the model's ``weather`` tool answers with (ADR 0285): plain words, local times.

    ``whole`` is a request with no days: ``today``, ``tomorrow`` and the next 12 hours; else
    ``days`` (``first`` to ``last``). ``hourly`` puts every hour of those days in place of the
    next 12. The next hour's minutes come with the current day; alerts and attribution always,
    except that days all before today give ``days`` and the attribution alone (no ``now``).
    ``alerts_asked`` is whether the request carried a country, and so asked for alerts.

    Raises:
        ToolError: WeatherKit holds none of the days asked.
    """
    zone = ZoneInfo(zone_name)
    today = datetime.fromtimestamp(now, zone).date()
    days = [
        d for d in (_day(row, zone) for row in body.get("forecastDaily", {}).get("days", ()))
        if first <= date.fromisoformat(d["date"]) <= last
    ]
    if not days:
        msg = (
            f"weather: WeatherKit has no forecast for {first.isoformat()} to {last.isoformat()}"
            f" (it covers {DAYS_BACK} days before today, {today.isoformat()}, to {DAYS_AHEAD}"
            " days after it); use web_search for other dates"
        )
        raise ToolError(msg, code="invalid_arguments")
    current = body.get("currentWeather") or {}
    past = last < today
    out: dict[str, Any] = {"place": label, "timezone": zone_name}
    if not past:
        out["now"] = _now(current)
    out |= {"today": days[0], "tomorrow": days[1] if len(days) > 1 else None} if whole \
        else {"days": days}
    rows = body.get("forecastHourly", {}).get("hours", ())
    if hourly:
        out["hourly"] = [_hour(row, zone) for row in rows]
    elif whole:
        live = [row for row in rows if _when(row["forecastStart"], zone).timestamp() + 3600 > now]
        out["next_hours"] = [
            _hour(row, zone, current.get("conditionCode") if i == 0 else None)
            for i, row in enumerate(live[:_REPORT_HOURS])
        ]
    if first <= today <= last:
        out["next_hour"] = next_hour(body, zone, now)
    if not past:
        out["alerts"] = alerts(body, zone, asked=alerts_asked)
    out["attribution"] = ATTRIBUTION
    return out


def home_line(body: Mapping[str, Any], zone_name: str, now: float) -> dict[str, Any]:
    """The home's line: ``now_c``, today's ``high_c`` and four three-hourly kinds from now."""
    zone = ZoneInfo(zone_name)
    current = body["currentWeather"]
    live = [
        row for row in body["forecastHourly"]["hours"]
        if _when(row["forecastStart"], zone).timestamp() + 3600 > now
    ]
    hours = [
        {
            "at": _when(row["forecastStart"], UTC).isoformat(),
            "kind": kind(current["conditionCode"] if i == 0 else row["conditionCode"]),
            "temp_c": _num(row["temperature"]),
        }
        for i, row in enumerate(live)
        if i % _HOME_STEP_H == 0
    ][:_HOME_HOURS]
    return {
        "now_c": _num(current["temperature"]),
        "high_c": _num(body["forecastDaily"]["days"][0]["temperatureMax"]),
        "hours": hours,
    }
