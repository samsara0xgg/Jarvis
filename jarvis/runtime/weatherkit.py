"""ADR 0285: Apple WeatherKit, the one source of the weather, as the model and the home read it.

:class:`WeatherKit` signs the developer token (ES256, through the APNs provider token class),
asks ``weatherkit.apple.com`` for the current conditions, hourly and daily forecasts, the
next-hour minute precipitation and the official alerts of one place in one request, and holds an
answer for :data:`TTL_S` so the home and a turn's tool call do not pay twice. Two small requests
come with it (ADR 0357): which data sets WeatherKit offers at a place, held a day, and the full
text of an alert in force. The functions below it turn that answer into the words the model reads
(:func:`report`) and the home's three-hourly line (:func:`home_line`).

Nothing here logs or returns the key, the token or the request's headers: a failure is an
:class:`Unavailable`, a :class:`~jarvis.execution.tools.ToolError` that names what failed and
Apple's own ``reason`` word.
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
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from jarvis.execution.speakable import day_name
from jarvis.execution.tools import ToolError
from jarvis.execution.weather_tool import DAYS_AHEAD, DAYS_BACK
from jarvis.runtime.weather_day import (
    LIKELY,
    NIGHT_TO,
    first_line,
    horizon,
    hours_between,
    hours_of,
    split,
    stretch,
)
from jarvis.surface.apns import ProviderToken, load_signing_key

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)
ENDPOINT: Final = "https://weatherkit.apple.com/api/v1/weather"
AVAILABILITY: Final = "https://weatherkit.apple.com/api/v1/availability"
ALERT_DETAIL: Final = "https://weatherkit.apple.com/api/v1/weatherAlert"
ATTRIBUTION: Final = {
    "provider": "Apple Weather",
    "url": "https://weatherkit.apple.com/legal-attribution.html",
}
"""What Apple requires wherever its weather is shown: the trademark and the legal page link."""
NOT_SET_UP: Final = "weather is not set up: WeatherKit key missing"
DEFAULT_TEAM_ID: Final = "3MEBVQ3N3U"
TTL_S: Final = 300
"""How long an answer is reused: the minute precipitation is a few minutes old at most."""
OFFERS_TTL_S: Final = 86_400
"""How long the data sets WeatherKit offers at a place are kept (ADR 0357)."""
MAX_ALERTS: Final = 3
"""Alerts in force given in full; the others are counted."""
ALERT_TEXT_CHARS: Final = 300
"""An alert's text is cut to about this many characters, at a word."""
NO_SERVICE: Final = "no official warning service covers this place"
NONE_IN_FORCE: Final = "none in force"
NOT_ASKED: Final = "not asked: the country of this place is not known"
NOT_CHECKED: Final = "none shown; whether a warning service covers this place could not be checked"
TIMEOUT_S: Final = 6.0
TOKEN_LIFETIME_S: Final = 3600
_REFUSED: Final = 401
_NEXT_HOUR_MIN: Final = 60
_HOME_STEP_H: Final = 3
_HOME_HOURS: Final = 4
_FULL_ROWS: Final = 6
"""The next hours are given in full this far, then every second one (ADR 0355)."""
OUTING_ROWS: Final = 8
"""An outing's hours are given row by row up to this many; a longer one by its figures alone."""
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
_SEVERITY: Final = ("extreme", "severe", "moderate", "minor")
"""Alerts in force are given most severe first."""


def words(code: str) -> str:
    """``MostlyCloudy`` as ``mostly cloudy``."""
    return _CAMEL.sub(" ", code or "unknown").lower()


def kind(code: str) -> str:
    """The home's six icons: sun, cloud, fog, rain, snow, storm."""
    return next((one for keys, one in _KINDS if any(k in code for k in keys)), "rain")


class Unavailable(ToolError):  # noqa: N818 - reads as the fact it states
    """WeatherKit could not be read; ``why`` says so in a few words (never Apple's own body).

    It is a ``dont_retry`` failure and sends nobody to a web search: a search finds pages, not a
    forecast (ADR 0357).
    """

    def __init__(self, why: str, *, code: str = "network_error", then: str = "") -> None:
        """Say that the weather is unavailable, why, and what follows from it."""
        super().__init__(f"weather is unavailable: {why}{then}", code=code, kind="dont_retry")
        self.why = why


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
        self._held: dict[str, tuple[float, Any]] = {}
        self._offers: dict[tuple[float, float], tuple[float, frozenset[str]]] = {}

    @classmethod
    def from_settings(
        cls, settings: WeatherKitSettings, clock: Callable[[], float] = time.time,
    ) -> WeatherKit | None:
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
        return cls(token, clock)

    def read(  # noqa: PLR0913 - one request: the point, its zone and country, the days.
        self, latitude: float, longitude: float, *, zone: str, country: str | None,
        first: date, last: date,
    ) -> dict[str, Any]:
        """WeatherKit's answer for ``first`` to ``last`` (the place's own days); raises ToolError.

        Daily rows for those days, the current conditions, the next hour's minutes and, when
        ``country`` is known, alerts. The hourly rows start at the place's midnight when the days
        include today (ADR 0355: what fell earlier today is added up from them) and run to the
        end of ``last``, or to the end of tonight if that is later; for days all ahead they cover
        those days. Days all before today get the daily rows alone: nothing live applies to them.
        """
        tz = ZoneInfo(zone)
        now = self.clock()
        today = datetime.fromtimestamp(now, tz)
        begin = datetime.combine(first, day_start(), tz)
        end = datetime.combine(last + timedelta(days=1), day_start(), tz)
        query = {"timezone": zone, "dailyStart": _utc(begin), "dailyEnd": _utc(end)}
        if last < today.date():
            query["dataSets"] = "forecastDaily"
        else:
            hourly, through = begin, end
            if first <= today.date():
                hourly = datetime.combine(today.date(), day_start(), tz)
                through = max(end, datetime.combine(today.date() + timedelta(days=1), NIGHT_TO, tz))
            sets = ["currentWeather", "forecastHourly", "forecastDaily", "forecastNextHour"]
            query["dataSets"] = ",".join([*sets, "weatherAlerts"] if country else sets)
            query["hourlyStart"] = _utc(hourly)
            query["hourlyEnd"] = _utc(max(through, hourly + timedelta(hours=1)))
            if country:  # Apple documents countryCode; its forum examples send country
                query["countryCode"] = country
                query["country"] = country
        url = f"{ENDPOINT}/en/{latitude}/{longitude}?{urlencode(query, safe=',:/')}"
        body = self._cached(url, now)
        if not isinstance(body, dict):
            why = "the forecast could not be read (an unexpected answer)"
            raise Unavailable(why)
        return body

    def offers(self, latitude: float, longitude: float, country: str) -> frozenset[str]:
        """The data sets WeatherKit offers at a place (ADR 0357), held a day per place.

        A place is its point to two decimals. ``weatherAlerts`` among them means a warning
        service covers it.

        Raises:
            Unavailable: WeatherKit did not answer.
        """
        point = (round(latitude, 2), round(longitude, 2))
        now = self.clock()
        with self._lock:
            held = self._offers.get(point)
            if held is not None and now - held[0] < OFFERS_TTL_S:
                return held[1]
            url = f"{AVAILABILITY}/{point[0]}/{point[1]}?{urlencode({'country': country})}"
            body = self._get(url)
            if not isinstance(body, list):
                why = "the data sets WeatherKit offers could not be read"
                raise Unavailable(why)
            found = frozenset(str(name) for name in body)
            self._offers = {
                at: h for at, h in self._offers.items() if now - h[0] < OFFERS_TTL_S
            } | {point: (now, found)}
            return found

    def alert_detail(self, alert_id: str) -> dict[str, Any]:
        """The full text and times of one alert in force, by its id; held like a forecast.

        Raises:
            Unavailable: WeatherKit did not answer.
        """
        body = self._cached(f"{ALERT_DETAIL}/en/{quote(alert_id, safe='')}", self.clock())
        if not isinstance(body, dict):
            why = "the alert could not be read (an unexpected answer)"
            raise Unavailable(why)
        return body

    def _cached(self, url: str, now: float) -> Any:  # noqa: ANN401 - decoded JSON
        """The JSON at ``url``, as asked within :data:`TTL_S` or fresh."""
        with self._lock:
            held = self._held.get(url)
            if held is not None and now - held[0] < TTL_S:
                return held[1]
            body = self._get(url)
            self._held = {u: h for u, h in self._held.items() if now - h[0] < TTL_S}
            self._held[url] = (now, body)
            return body

    def _get(self, url: str) -> Any:  # noqa: ANN401 - decoded JSON
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL.
            url, headers={"Authorization": f"Bearer {self._token.current()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:  # noqa: S310
                return json.load(reply)
        except urllib.error.HTTPError as exc:
            reason = " ".join(filter(None, (str(exc.code), _reason(exc))))
            if exc.code == _REFUSED:
                self._token.discard()
                why = f"Apple WeatherKit refused the key ({reason})"
                raise Unavailable(
                    why, code="weatherkit_refused", then="; the key needs checking",
                ) from None
            why = f"Apple WeatherKit answered {reason}"
            raise Unavailable(why) from None
        except Exception as exc:  # timeout, refusal, bad JSON: all one short reason.
            why = f"the forecast could not be read ({type(exc).__name__})"
            raise Unavailable(why) from exc


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


def _day(row: Mapping[str, Any], zone: ZoneInfo, today: date) -> dict[str, Any]:
    daytime = row.get("daytimeForecast") or {}
    day = _when(row["forecastStart"], zone).date()
    return {
        "day": day_name(day, today),
        "date": day.isoformat(),
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


def day_rows(
    body: Mapping[str, Any], zone: ZoneInfo, *, first: date, last: date, today: date,
) -> list[dict[str, Any]]:
    """The daily rows for ``first`` to ``last``, each named by its day (``today (Wed)``).

    Raises:
        ToolError: WeatherKit holds none of the days.
    """
    found = (_day(row, zone, today) for row in body.get("forecastDaily", {}).get("days", ()))
    rows = [one for one in found if first <= date.fromisoformat(one["date"]) <= last]
    if not rows:
        msg = (
            f"weather: WeatherKit has no forecast for {first.isoformat()} to {last.isoformat()}"
            f" (it covers {DAYS_BACK} days before today, {today.isoformat()}, to {DAYS_AHEAD}"
            " days after it); use web_search for other dates"
        )
        raise ToolError(msg, code="invalid_arguments")
    return rows


def _at(at: datetime, today: date) -> str:
    """``today 14:00``, ``tomorrow 01:00``, else ``Sat 14:00``."""
    name = {0: "today", 1: "tomorrow", -1: "yesterday"}.get((at.date() - today).days)
    return f"{name or at.strftime('%a')} {at:%H:%M}"


def _hour(
    hour: tuple[datetime, Mapping[str, Any]], today: date, code: str | None = None,
) -> dict[str, Any]:
    at, row = hour
    return {
        "at": _at(at, today),
        "temp_c": _num(row.get("temperature")),
        "condition": words(row.get("conditionCode", "") if code is None else code),
        "rain_chance_pct": _pct(row.get("precipitationChance")),
        "precip_mm": _num(row.get("precipitationAmount")),
    }


def _periods(
    summary: Sequence[Mapping[str, Any]], zone: ZoneInfo,
) -> list[tuple[datetime, datetime | None, str]]:
    """The rain periods of WeatherKit's summary of the next hour: ``(start, end, kind)``.

    A period counts when it is not ``clear`` and its chance, if it names one, is at least half;
    one that touches the one before it joins it. A period with no end ends where the next one
    starts, or at the end of the data (None).
    """
    rows = sorted(((_when(one["startTime"], zone), one) for one in summary), key=lambda r: r[0])
    out: list[tuple[datetime, datetime | None, str]] = []
    for n, (start, one) in enumerate(rows):
        chance = one.get("precipitationChance")
        if one.get("condition") == "clear" or (chance is not None and chance < LIKELY):
            continue
        end = (
            _when(one["endTime"], zone) if one.get("endTime")
            else (rows[n + 1][0] if n + 1 < len(rows) else None)
        )
        kind = _RAIN_WORDS.get(str(one.get("condition")), str(one.get("condition")))
        if out and out[-1][1] == start:
            out[-1] = (out[-1][0], end, out[-1][2])
        else:
            out.append((start, end, kind))
    return out


def next_hour(body: Mapping[str, Any], zone: ZoneInfo, now: float) -> dict[str, Any]:
    """The coming hour's rain, as when it starts and stops (ADR 0357).

    It follows WeatherKit's own summary of the hour, period by period. Without one, a minute is
    rain when its chance is at least half and its intensity above zero; a minute at a lower
    chance is dry however much it would rain.
    """
    data = body.get("forecastNextHour")
    if not data or not data.get("minutes"):
        return {"unavailable": "no minute-by-minute precipitation exists for this place"}
    rows = [(_when(one["startTime"], zone), one) for one in data["minutes"]]
    rows = [(at, one) for at, one in rows if at.timestamp() + _NEXT_HOUR_MIN > now]
    if not rows:
        return {"unavailable": "the minute-by-minute precipitation is out of date"}
    stamp = datetime.fromtimestamp(now, zone)
    summary = [one for one in data.get("summary") or () if one.get("startTime")]
    if summary:
        periods = _periods(summary, zone)
        current = next(
            (p for p in periods if p[0] <= stamp and (p[1] is None or stamp < p[1])), None,
        )
        coming = next((p for p in periods if p[0] > stamp), None)
        raining = current is not None
        flip = (current[1] if current else coming[0] if coming else None)
        kind = (current or coming or (None, None, "rain"))[2]
        wet = [
            one for at, one in rows
            if any(start <= at and (end is None or at < end) for start, end, _ in periods)
        ]
    else:
        marks = [
            (at, (one.get("precipitationIntensity") or 0) > 0
             and (one.get("precipitationChance") or 0) >= LIKELY)
            for at, one in rows
        ]
        raining = marks[0][1]
        flip = next((at for at, is_wet in marks if is_wet != raining), None)
        wet = [one for (_, is_wet), (_, one) in zip(marks, rows, strict=True) if is_wet]
        kind = "rain"
    peak = max((one.get("precipitationIntensity") or 0 for one in wet), default=0)
    out: dict[str, Any] = {
        "now": kind if raining else "dry",
        "starts_at": None if raining or flip is None else flip.strftime("%H:%M"),
        "stops_at": flip.strftime("%H:%M") if raining and flip is not None else None,
        "peak_mm_h": _num(peak),
        "data_until": (rows[-1][0] + timedelta(minutes=1)).strftime("%H:%M"),
    }
    if not raining and flip is not None:
        out["kind"] = kind
    return out


def _severity(row: Mapping[str, Any]) -> int:
    named = str(row.get("severity") or "").lower()
    return _SEVERITY.index(named) if named in _SEVERITY else len(_SEVERITY)


def _trimmed(text: str) -> str:
    """``text`` on one line, cut to about :data:`ALERT_TEXT_CHARS` at a word."""
    one = " ".join(text.split())
    if len(one) <= ALERT_TEXT_CHARS:
        return one
    return one[:ALERT_TEXT_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:.") + "\u2026"


def _text(detail: Mapping[str, Any]) -> str | None:
    """The English text of an alert (else its first), trimmed; None when it has none."""
    messages = [m for m in detail.get("messages", ()) if str(m.get("text") or "").strip()]
    chosen = next(
        (m for m in messages if m.get("language") == "en"), messages[0] if messages else None,
    )
    return None if chosen is None else _trimmed(str(chosen["text"]))


def _alert(
    row: Mapping[str, Any], detail: Mapping[str, Any], zone: ZoneInfo, today: date,
) -> dict[str, Any]:
    """One alert in force: what it is, where, who says so, when it runs, and its own words."""

    def when(*names: str) -> str | None:
        found = next((v for n in names if (v := detail.get(n) or row.get(n))), None)
        return None if not found else _at(_when(str(found), zone), today)

    text = _text(detail)
    out: dict[str, Any] = {
        "headline": row.get("description"), "severity": row.get("severity"),
        "area": row.get("areaName"), "source": row.get("source"),
        "starts": when("eventOnsetTime", "effectiveTime"),
        "ends": when("eventEndTime", "expireTime"),
    }
    if text is not None:
        out["text"] = text
    elif row.get("detailsUrl"):
        out["details_url"] = row["detailsUrl"]
    return out


def alerts(  # noqa: PLR0913 - the client, the answer, the point and its country, the clock.
    kit: WeatherKit, body: Mapping[str, Any], latitude: float, longitude: float,
    country: str | None, zone: ZoneInfo, now: float,
) -> dict[str, Any]:
    """The ``alerts`` an answer carries, as a fragment of it (ADR 0357).

    A list when some are in force: at most :data:`MAX_ALERTS`, the most severe first, each with
    its own text and when it starts and ends (one more request each), and ``more_alerts`` for
    the rest. WeatherKit leaves ``weatherAlerts`` out when none are in force, and it is the same
    where no service exists; so with none, it is asked which data sets it offers at the place and
    ``alerts`` says which of the two it is, never a bare empty list.
    """
    if not country:
        return {"alerts": NOT_ASKED}
    rows = sorted(
        (body.get("weatherAlerts") or {}).get("alerts", ()), key=_severity,
    )
    if not rows:
        try:
            offered = kit.offers(latitude, longitude, country)
        except ToolError:
            return {"alerts": NOT_CHECKED}
        return {"alerts": NONE_IN_FORCE if "weatherAlerts" in offered else NO_SERVICE}
    today = datetime.fromtimestamp(now, zone).date()
    shown: list[dict[str, Any]] = []
    for row in rows[:MAX_ALERTS]:
        try:
            detail = kit.alert_detail(str(row["id"])) if row.get("id") else {}
        except ToolError:  # the headline stands without its text
            detail = {}
        shown.append(_alert(row, detail, zone, today))
    more = len(rows) - len(shown)
    return {"alerts": shown} | ({"more_alerts": more} if more else {})


def _hour_rows(
    rows: list[tuple[datetime, Mapping[str, Any]]], stamp: datetime, current: Mapping[str, Any],
    *, thin: bool,
) -> list[dict[str, Any]]:
    """Hour rows, the one of this hour with the current conditions.

    ``thin`` leaves out every second hour after the first :data:`_FULL_ROWS`.
    """
    this_hour = stamp.replace(minute=0, second=0, microsecond=0).timestamp()
    kept = [*rows[:_FULL_ROWS], *rows[_FULL_ROWS::2]] if thin else rows
    now_code = current.get("conditionCode")
    return [
        _hour(hour, stamp.date(), now_code if hour[0].timestamp() == this_hour else None)
        for hour in kept
    ]


def report(  # noqa: PLR0913 - what the tool answers with: the answer and what was asked of it.
    body: Mapping[str, Any], label: str, zone_name: str, *,
    first: date, last: date, whole: bool, hourly: bool, now: float,
    alerts: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """What the model's ``weather`` tool answers with (ADR 0285, 0355): plain words, local times.

    It opens with ``summary``, a line written from the figures. When the days include today they
    come with today in parts (``earlier_today``, ``rest_of_today``, ``tonight``) added up from the
    hourly rows, and the daily row of today as ``whole_day``. ``whole`` is a request with no
    days: today, ``tomorrow`` and ``next_hours`` (from this hour to the end of tonight, thinned);
    else the other days are ``days`` (``first`` to ``last``). ``hourly`` gives every hour of the
    days asked (from this hour on, for today) as ``hourly`` instead. The next hour's minutes come
    with the current day; alerts and attribution always, except that days all before today give
    ``days`` and the attribution alone (no ``now``). ``alerts`` is the part of the answer about
    official warnings (:func:`alerts`), which a request for days all before today has none of.

    Raises:
        ToolError: WeatherKit holds none of the days asked.
    """
    zone = ZoneInfo(zone_name)
    stamp = datetime.fromtimestamp(now, zone)
    today = stamp.date()
    rows = day_rows(body, zone, first=first, last=last, today=today)
    hours = hours_of(body, zone)
    current = body.get("currentWeather") or {}
    past = last < today
    out: dict[str, Any] = {"place": label, "timezone": zone_name}
    if not past:
        out["now"] = _now(current)
    parts: dict[str, Any] = {}
    if first <= today <= last:
        parts = split(hours, stamp, words)
        out |= parts
        out["whole_day"] = next(row for row in rows if row["date"] == today.isoformat())
    others = [row for row in rows if row["date"] != today.isoformat()]
    if whole:
        if others:
            out["tomorrow"] = others[0]
    elif others:
        out["days"] = others
    this_hour = stamp.replace(minute=0, second=0, microsecond=0).timestamp()
    coming = [h for h in hours if h[0].timestamp() >= this_hour]
    if hourly:
        shown = [h for h in coming if first <= h[0].date() <= last]
        out["hourly"] = _hour_rows(shown, stamp, current, thin=False)
    elif whole:
        ahead = [h for h in coming if h[0] < horizon(stamp)]
        out["next_hours"] = _hour_rows(ahead, stamp, current, thin=True)
    if first <= today <= last:
        out["next_hour"] = next_hour(body, zone, now)
    if not past and alerts is not None:
        out |= alerts
    out["attribution"] = ATTRIBUTION
    gone = [(row, date.fromisoformat(row["date"]) < today) for row in others]
    asked_now = out.get("now") if first <= today <= last else None
    in_force = out["alerts"] if isinstance(out.get("alerts"), list) else []
    warned = [str(one.get("headline") or "unnamed warning") for one in in_force]
    line = first_line(label, asked_now, parts, gone, warned, int(out.get("more_alerts", 0)))
    return {"summary": line, **out}


def outing(
    body: Mapping[str, Any], zone_name: str, *, start: datetime, end: datetime,
) -> dict[str, Any] | None:
    """The weather of ``start`` to ``end`` at the place ``body`` is for, as a plan reads it.

    The figures of :func:`~jarvis.runtime.weather_day.stretch` and, when they are few, the hours
    themselves as rows (ADR 0355). Clocks are ``HH:MM`` in the zone of ``start``: the plan's own.
    None when the answer holds none of those hours.
    """
    hours = hours_of(body, ZoneInfo(zone_name))
    found = stretch(hours, start, end)
    if found is None:
        return None
    rows = hours_between(hours, start, end)
    if len(rows) > OUTING_ROWS:
        return found
    return found | {
        "hours": [
            {
                "at": f"{at.astimezone(start.tzinfo):%H:%M}",
                "temp_c": _num(row.get("temperature")),
                "condition": words(row.get("conditionCode", "")),
                "rain_chance_pct": _pct(row.get("precipitationChance")),
                "precip_mm": _num(row.get("precipitationAmount")),
            }
            for at, row in rows
        ],
    }


def said_at(moment: float, zone_name: str, now: float) -> str:
    """When ``moment`` was, in the place's own time: ``today 07:42``, ``yesterday 23:10``."""
    zone = ZoneInfo(zone_name)
    return _at(datetime.fromtimestamp(moment, zone), datetime.fromtimestamp(now, zone).date())


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
