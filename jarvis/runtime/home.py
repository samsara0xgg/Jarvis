"""The companion home's reads (ADR 0055): Today, unread mail and the morning brief.

Calendar and To Do come through the live ``microsoft`` connection's tools and mail
through the live ``gmail`` connection's, outside any model turn; the weather from
Open-Meteo. The writes are a to-do's status and a letter's INBOX label, each only
because Allen clicked it on the home (ADR 0124 for the letters).
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import urllib.request
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

from jarvis.decision.daily_report import brief_of
from jarvis.execution.tools import ToolError
from jarvis.runtime.daily_report import PLAN_SERVER, _graph
from jarvis.shared import lang
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_store import get_briefing
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.decision.mail_reply import MailReply
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.execution.weather_tool import WeatherRequest
    from jarvis.runtime.dashboard import ViewState
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.runtime.work_state import LLMAnalyst

LOGGER = logging.getLogger(__name__)
_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
_GEOCODE_MATCHES = 5
_ALTERNATIVES = 2
_LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW",
    "NNW",
)
_CURRENT = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,precipitation,"
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m"
)
_HOURLY = (
    "temperature_2m,apparent_temperature,weather_code,precipitation_probability,precipitation,"
    "wind_speed_10m"
)
_DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "apparent_temperature_min,precipitation_probability_max,precipitation_sum,snowfall_sum,"
    "wind_speed_10m_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset"
)
_WEATHER_TIMEOUT_S = 4.0
_FORECAST_STEP_H = 3
_REPORT_HOURS = 12
# WMO weather codes, as Open-Meteo documents them, folded into the home's six icons.
_WMO = (
    ((0, 1), "sun"),
    ((2, 3), "cloud"),
    ((45, 48), "fog"),
    ((71, 72, 73, 74, 75, 76, 77, 85, 86), "snow"),
    ((95, 96, 97, 98, 99), "storm"),
)
_WMO_WORDS = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
    57: "heavy freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}
"""WMO weather codes as Open-Meteo documents them, in the words the model says aloud."""
CALENDAR_TTL_TODAY_MS: Final = 5 * 60_000
"""How long the day line keeps today's Graph read (ADR 0230): his plans change during the day."""
CALENDAR_TTL_OTHER_MS: Final = 30 * 60_000
"""How long it keeps any other day's read: those days move slowly, and a phone scrubbing the ring
back and forth should not ask Graph for each swipe."""
_ID_SEP = "|"
"""A to-do's id is ``<list id>|<task id>``; Graph ids are base64 and never hold a bar."""
MAIL_SERVER = "gmail"
"""Google's Workspace MCP server with only Gmail switched on (ADR 0055)."""
_MAIL_LIMIT = 20
_INBOX = "INBOX"
_LETTERS_KEPT = 50
_MAIL_BODY_CHARS: Final = 4000
_HTML_HINT_RE: Final = re.compile(r"<(?:html|body|div|p|br|table|span)\b", re.IGNORECASE)
_HTML_DROP_RE: Final = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK_RE: Final = re.compile(r"<(?:br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE: Final = re.compile(r"<[^>]+>")
_HTML_LINK_RE: Final = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a\s*>""", re.IGNORECASE | re.DOTALL,
)


def _link_text(link: re.Match[str]) -> str:
    url, label = link.group(1), _HTML_TAG_RE.sub("", link.group(2)).strip()
    return url if not label or label == url else f"{label} ({url})"
_NOT_A_PERSON = re.compile(r"no-?reply|notification|mailer-daemon|bounce", re.IGNORECASE)


def mail_text(body: str, *, links: bool = False) -> str:
    """The message body as plain text, whole; an HTML-only message loses its markup.

    ``links`` keeps each anchor's address as ``text (url)`` for the page; the model's
    mail record (ADR 0063) leaves them out.
    """
    if _HTML_HINT_RE.search(body):
        # ponytail: tag stripping, not an HTML renderer; tables and quoted replies stay flat.
        if links:
            body = _HTML_LINK_RE.sub(_link_text, body)
        body = _HTML_TAG_RE.sub("", _HTML_BREAK_RE.sub("\n", _HTML_DROP_RE.sub("", body)))
        body = html.unescape(body)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body).strip()


def mail_body(body: str, *, links: bool = False) -> str:
    """:func:`mail_text` capped at the page's and the mail record's length."""
    text = mail_text(body, links=links)
    return text if len(text) <= _MAIL_BODY_CHARS else f"{text[:_MAIL_BODY_CHARS]}…"


_LAYOUT_RE: Final = re.compile(r"<(?:table|img)\b", re.IGNORECASE)
_URL_RE: Final = re.compile(r"https?://\S+")
_FEW_WORDS: Final = 200
_SUMMARY_PROMPT: Final = (
    "Say in one short sentence, in {language}, what this email is (who sends it and what it "
    "offers or asks). No greeting, no quotes."
)


def mail_layout(raw: str, text: str) -> str:
    """``html`` when the page should draw the letter's own HTML rather than ``text`` (ADR 0148).

    Only an HTML-only body qualifies (the server hands over the text part when there is one,
    so a body with markup is the HTML part). Then it draws as HTML when it lays out in tables
    or holds images, or when its text is mostly links: over half link characters, or links and
    under 200 other characters.
    """
    if not _HTML_HINT_RE.search(raw):
        return "text"
    if _LAYOUT_RE.search(raw):
        return "html"
    links = sum(len(one) for one in _URL_RE.findall(text))
    mostly_links = links > 0 and (links * 2 > len(text) or len(text) - links < _FEW_WORDS)
    return "html" if mostly_links else "text"


def mail_summarizer(
    analyst: LLMAnalyst | None, event_log: Path,
) -> Callable[[str, str], str] | None:
    """``(system, letter) -> sentence`` over the analyst's preset, accounted as ``mail_summary``."""
    if analyst is None:
        return None

    def summarize(system: str, letter: str) -> str:
        with closing(open_runtime_event_log(event_log)) as conn:
            result = analyst.analyze(
                conn, system=system, messages=[{"role": "user", "content": letter}],
                tools=[], tool_choice="none",
            )
        return (result.text or "").strip()

    return summarize


def _words(code: int) -> str:
    return _WMO_WORDS.get(code, "unknown")


def _kind(code: int) -> str:
    return next((kind for codes, kind in _WMO if code in codes), "rain")


def _get(url: str, query: Mapping[str, Any], what: str) -> dict[str, Any]:
    """One JSON GET; a ToolError that names ``what`` when it cannot be read."""
    try:
        with urllib.request.urlopen(  # noqa: S310 — a fixed https URL.
            f"{url}?{urlencode(query, safe=',')}", timeout=_WEATHER_TIMEOUT_S,
        ) as reply:
            body: dict[str, Any] = json.load(reply)
    except Exception as exc:  # timeout, refusal, bad JSON: all one short reason.
        msg = (
            f"weather: {what} could not be read ({type(exc).__name__}); "
            "use web_search, or tell him it is unavailable"
        )
        raise ToolError(msg, code="network_error") from exc
    return body


def _span(start: date | None, end: date | None) -> dict[str, Any]:
    """Open-Meteo's days: the two from today, or ``start`` to ``end``."""
    if start is None or end is None:
        return {"forecast_days": 2}
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def _forecast(
    latitude: float, longitude: float, start: date | None = None, end: date | None = None,
) -> dict[str, Any]:
    """Open-Meteo's forecast for the place, in its local time; raises when unreachable.

    Today and tomorrow unless ``start`` and ``end`` are given.
    """
    return _get(
        _OPEN_METEO,
        {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "timeformat": "unixtime", **_span(start, end),
            "current": _CURRENT, "hourly": _HOURLY, "daily": _DAILY,
        },
        "the forecast",
    )


def _first_hour(body: Mapping[str, Any]) -> int:
    """Index of the hourly row the current reading falls in."""
    return next(
        i for i, at in enumerate(body["hourly"]["time"]) if at + 3600 > body["current"]["time"]
    )


def weather(latitude: float, longitude: float) -> dict[str, Any]:
    """Now, today's high and a 3-hourly forecast from the current hour; raises when unreachable."""
    body = _forecast(latitude, longitude)
    current, hourly = body["current"], body["hourly"]
    first = _first_hour(body)
    hours = [
        {
            "at": datetime.fromtimestamp(hourly["time"][i], UTC).isoformat(),
            "kind": _kind(current["weather_code"] if i == first else hourly["weather_code"][i]),
            "temp_c": hourly["temperature_2m"][i],
        }
        for i in range(first, len(hourly["time"]), _FORECAST_STEP_H)
    ][:4]
    return {
        "now_c": current["temperature_2m"],
        "high_c": body["daily"]["temperature_2m_max"][0],
        "hours": hours,
    }


def _label(found: Mapping[str, Any]) -> str:
    """``Toronto, Ontario, Canada`` for a geocoding match."""
    return ", ".join(dict.fromkeys(
        str(one) for one in (found["name"], found.get("admin1"), found.get("country")) if one
    ))


def _geocode(name: str) -> tuple[float, float, str, list[dict[str, str]]]:
    """The best match for ``name`` (latitude, longitude, label) and the other places called that.

    Tries the name's own language, then English.

    Raises:
        ToolError: Open-Meteo did not answer, or knows no place of that name.
    """
    for language in dict.fromkeys((lang.text_language(name), "en")):
        query = {"name": name, "count": _GEOCODE_MATCHES, "language": language, "format": "json"}
        if found := _get(_GEOCODE, query, "the place lookup").get("results"):
            break
    else:
        msg = f"weather: no place found named {name!r}; ask him which place he means"
        raise ToolError(msg, code="not_found")
    first, *rest = found
    other = {
        _label(one): f"{one['latitude']},{one['longitude']}"
        for one in rest if one["name"].casefold() == first["name"].casefold()
    }
    other.pop(_label(first), None)
    others = [
        {"place": label, "use_place": coords}
        for label, coords in list(other.items())[:_ALTERNATIVES]
    ]
    return first["latitude"], first["longitude"], _label(first), others


def _spot(
    request: WeatherRequest, home: tuple[float, float, str],
) -> tuple[float, float, str, list[dict[str, str]]]:
    """Where the request is for: (latitude, longitude, label, same-name places)."""
    if request.here is not None:
        return (*request.here, [])
    if request.place is None:
        return (*home, [])
    if pair := _LAT_LNG.match(request.place):
        return float(pair[1]), float(pair[2]), request.place, []
    return _geocode(request.place)


def _now(current: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "temp_c": current["temperature_2m"],
        "feels_c": current["apparent_temperature"],
        "condition": _words(current["weather_code"]),
        "humidity_pct": current["relative_humidity_2m"],
        "wind_kmh": current["wind_speed_10m"],
        "gust_kmh": current["wind_gusts_10m"],
        "wind_from": _COMPASS[round(current["wind_direction_10m"] / 22.5) % len(_COMPASS)],
        "precip_mm": current["precipitation"],
    }


def _day(daily: Mapping[str, Any], i: int, zone: tzinfo) -> dict[str, Any]:
    return {
        "date": datetime.fromtimestamp(daily["time"][i], zone).date().isoformat(),
        "high_c": daily["temperature_2m_max"][i],
        "low_c": daily["temperature_2m_min"][i],
        "feels_high_c": daily["apparent_temperature_max"][i],
        "feels_low_c": daily["apparent_temperature_min"][i],
        "condition": _words(daily["weather_code"][i]),
        "rain_chance_pct": daily["precipitation_probability_max"][i],
        "precip_mm": daily["precipitation_sum"][i],
        "snow_cm": daily["snowfall_sum"][i],
        "wind_max_kmh": daily["wind_speed_10m_max"][i],
        "gust_max_kmh": daily["wind_gusts_10m_max"][i],
        "uv_index_max": daily["uv_index_max"][i],
        "sunrise": datetime.fromtimestamp(daily["sunrise"][i], zone).strftime("%H:%M"),
        "sunset": datetime.fromtimestamp(daily["sunset"][i], zone).strftime("%H:%M"),
    }


def _hour(
    hourly: Mapping[str, Any], i: int, zone: tzinfo, code: int | None = None,
) -> dict[str, Any]:
    return {
        "at": datetime.fromtimestamp(hourly["time"][i], zone).strftime("%Y-%m-%d %H:%M"),
        "temp_c": hourly["temperature_2m"][i],
        "feels_c": hourly["apparent_temperature"][i],
        "condition": _words(hourly["weather_code"][i] if code is None else code),
        "rain_chance_pct": hourly["precipitation_probability"][i],
        "precip_mm": hourly["precipitation"][i],
        "wind_kmh": hourly["wind_speed_10m"][i],
    }


def _report(body: Mapping[str, Any], label: str, request: WeatherRequest) -> dict[str, Any]:
    """What the model's ``weather`` tool answers with (ADR 0279): plain words, local times.

    With no days asked: ``today``, ``tomorrow`` and the next 12 hours; else ``days``. ``hourly``
    puts every hour of those days in place of the next 12.
    """
    zone = timezone(timedelta(seconds=body["utc_offset_seconds"]))
    daily, hourly = body["daily"], body["hourly"]
    days = [_day(daily, i, zone) for i in range(len(daily["time"]))]
    report: dict[str, Any] = {
        "place": label, "timezone": body["timezone"], "now": _now(body["current"]),
    }
    report |= {"today": days[0], "tomorrow": days[1]} if request.start is None else {"days": days}
    if request.hourly:
        report["hourly"] = [_hour(hourly, i, zone) for i in range(len(hourly["time"]))]
    elif request.start is None:
        first = _first_hour(body)
        report["next_hours"] = [
            _hour(hourly, i, zone, body["current"]["weather_code"] if i == first else None)
            for i in range(first, min(first + _REPORT_HOURS, len(hourly["time"])))
        ]
    return report


def _worst(values: list[float | None]) -> float | None:
    return max((one for one in values if one is not None), default=None)


def _air(latitude: float, longitude: float, request: WeatherRequest) -> dict[str, Any]:
    """US AQI, PM2.5 and PM10 now and the worst of each day, for the days of the request."""
    body = _get(
        _AIR_QUALITY,
        {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "timeformat": "unixtime", **_span(request.start, request.end),
            "current": "us_aqi,pm2_5,pm10", "hourly": "us_aqi,pm2_5,pm10",
        },
        "the air quality",
    )
    zone = timezone(timedelta(seconds=body["utc_offset_seconds"]))
    hourly = body["hourly"]
    by_day: dict[str, list[int]] = {}
    for i, at in enumerate(hourly["time"]):
        by_day.setdefault(datetime.fromtimestamp(at, zone).date().isoformat(), []).append(i)

    def pick(name: str, rows: list[int]) -> float | None:
        return _worst([hourly[name][i] for i in rows])

    return {
        "now": {
            "us_aqi": body["current"]["us_aqi"],
            "pm2_5_ugm3": body["current"]["pm2_5"],
            "pm10_ugm3": body["current"]["pm10"],
        },
        "days": [
            {
                "date": day, "us_aqi_max": pick("us_aqi", rows),
                "pm2_5_max_ugm3": pick("pm2_5", rows), "pm10_max_ugm3": pick("pm10", rows),
            }
            for day, rows in by_day.items()
        ],
    }


def weather_lookup(
    place: Mapping[str, Any] | None,
) -> Callable[[WeatherRequest], dict[str, Any]] | None:
    """The model's weather read (ADR 0279) for a :class:`WeatherRequest`.

    ``place`` is ``home.weather`` (``latitude``, ``longitude``, optional ``name``), the answer
    when the request names no place; None when the home has no location, which leaves the tool
    unregistered.
    """
    if place is None:
        return None
    home = (float(place["latitude"]), float(place["longitude"]), str(place.get("name") or "home"))

    def lookup(request: WeatherRequest) -> dict[str, Any]:
        latitude, longitude, label, others = _spot(request, home)
        report = _report(_forecast(latitude, longitude, request.start, request.end), label, request)
        if others:
            report["same_name_elsewhere"] = others
        if request.air_quality:
            report["air_quality"] = _air(latitude, longitude, request)
        return report

    return lookup


class Home:
    """What the home's Today, mail and brief blocks read; built once at boot."""

    def __init__(  # noqa: PLR0913 — the connections, the zone and the optional parts.
        self,
        connections: PluginConnections,
        zone: tuple[str, tzinfo],
        weather_at: Mapping[str, Any] | None,
        mail_reply: MailReply | None = None,
        view: ViewState | None = None,
        summarizer: Callable[[str, str], str] | None = None,
    ) -> None:
        """Bind the live connections, the local zone (name, zone) and the forecast's place.

        ``weather_at`` holds ``latitude`` and ``longitude``; None leaves the weather out.
        ``mail_reply`` marks letters that need a reply (ADR 0123); None leaves them all unmarked.
        ``view`` switches the Dashboard's mail page on (ADR 0147): its open letter may be read,
        archived and trashed, not only the junk offered. ``summarizer`` writes a letter's one-line
        summary (ADR 0148); None leaves :meth:`summary` a 404.
        """
        self._connections = connections
        self._zone_name, self._zone = zone
        self._weather_at = weather_at
        self._mail_reply = mail_reply
        self._view = view
        self._summarizer = summarizer
        self._summaries: dict[str, str] = {}
        # Gmail ids: what the last mail() listed, what it offered as junk, what Allen archived,
        # what he took back.
        self._listed: frozenset[str] = frozenset()
        self._junk: frozenset[str] = frozenset()
        self._archived: set[str] = set()
        self._archived_junk: set[str] = set()
        self._kept: set[str] = set()
        self._letters: dict[str, dict[str, str]] = {}
        self._calendar_lock = threading.Lock()
        self._calendar: dict[tuple[int, int], tuple[int, list[dict[str, Any]]]] = {}

    def _servers(self, server: str = PLAN_SERVER) -> McpServers:
        """The live client of ``server``; LookupError (the routes' 404) when it is not connected."""
        try:
            return self._connections.client_for(server)
        except ToolError as exc:
            raise LookupError(str(exc)) from exc

    def today(self) -> dict[str, Any]:
        """Today's calendar, every open to-do and the weather; a weather failure only drops it."""
        servers = self._servers()
        start = datetime.combine(datetime.now(self._zone).date(), time(), self._zone)
        events = [
            _event(one, self._zone)
            for one in _calendar_view(servers, start, start + timedelta(days=1))
        ]
        todos = []
        for one in _graph(servers, "list-todo-task-lists", {"fetchAllPages": True}):
            tasks = {"todoTaskListId": one["id"], "fetchAllPages": True}
            todos += [
                _todo(one["id"], task, self._zone)
                for task in _graph(servers, "list-todo-tasks", tasks)
                if task.get("status") != "completed"
            ]
        return {"events": events, "todos": todos, "weather": self._weather()}

    def calendar(self, start_ms: int, end_ms: int, now_ms: int) -> list[dict[str, Any]]:
        """The calendar's events of ``[start_ms, end_ms)`` as ``today()`` words them (ADR 0230).

        An all-day event also holds ``end``, the local midnight after its last day. The day line
        asks this on a worker thread. A read is kept per window: ``CALENDAR_TTL_TODAY_MS`` when
        ``now_ms`` is inside the window, ``CALENDAR_TTL_OTHER_MS`` otherwise. A failed read is not
        kept.

        Raises:
            LookupError: the ``microsoft`` server is not connected.
            ToolError: Graph or the server did not answer, or answered with an error.
        """
        key = (start_ms, end_ms)
        ttl = CALENDAR_TTL_TODAY_MS if start_ms <= now_ms < end_ms else CALENDAR_TTL_OTHER_MS
        with self._calendar_lock:
            held = self._calendar.get(key)
        if held is not None and 0 <= now_ms - held[0] < ttl:
            return held[1]
        events = []
        for one in _calendar_view(
            self._servers(),
            datetime.fromtimestamp(start_ms / 1000, self._zone),
            datetime.fromtimestamp(end_ms / 1000, self._zone),
        ):
            row = _event(one, self._zone)
            if row.get("all_day"):
                last = date.fromisoformat(str(one["end"]["dateTime"])[:10])
                row["end"] = datetime.combine(last, time(), self._zone).isoformat()
            events.append(row)
        with self._calendar_lock:
            self._calendar = {
                k: v for k, v in self._calendar.items()
                if 0 <= now_ms - v[0] < CALENDAR_TTL_OTHER_MS
            }
            self._calendar[key] = (now_ms, events)
        return events

    def graph(self, tool: str, args: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The ``value`` list of one read-only Microsoft tool: ``my_schedule``'s reads (ADR 0248).

        Raises:
            LookupError: the ``microsoft`` server is not connected.
            ToolError: the server did not answer, or answered with an error.
            ValueError: the answer is not Graph's JSON.
        """
        return _graph(self._servers(), tool, args)

    def _weather(self) -> dict[str, Any] | None:
        if self._weather_at is None:
            return None
        try:
            place = self._weather_at
            return weather(float(place["latitude"]), float(place["longitude"]))
        except Exception as exc:  # noqa: BLE001 — the calendar and to-dos still show without it.
            LOGGER.warning("home: weather unreadable: %s: %s", type(exc).__name__, exc)
            return None

    def set_todo(self, todo_id: str, *, done: bool) -> None:
        """Mark one To Do task completed or open again: Allen's own click, not a model proposal."""
        list_id, _, task_id = todo_id.partition(_ID_SEP)
        if not list_id or not task_id:
            msg = f"not a to-do id: {todo_id!r}"
            raise ValueError(msg)
        self._servers().call(
            PLAN_SERVER,
            "update-todo-task",
            {
                "todoTaskListId": list_id,
                "todoTaskId": task_id,
                "body": {"status": "completed" if done else "notStarted"},
            },
        )

    def mail(self) -> dict[str, Any]:
        """Unread Primary mail in Gmail, newest first, without no-reply and notification senders.

        Two read-only tools of the ``gmail`` server: a search in Gmail's own terms,
        then each hit's headers; nothing is marked read. Each letter's ``reply`` is "yes",
        "fyi" or None (ADR 0123) and its ``junk`` a bool (ADR 0124); a rated letter also
        carries ``importance`` (expected score 0-3, higher first) and ``category`` (ADR 0141);
        the junk ids are remembered as the only ones :meth:`archive` accepts.
        """
        servers = self._servers(MAIL_SERVER)
        # Gmail's own sort into Primary is the "from people" filter.
        query = {"query": "in:inbox category:primary is:unread", "maxResults": _MAIL_LIMIT}
        unread = [
            _letter(_gmail(servers, "gmail_get", {"messageId": hit["id"], "format": "metadata"}))
            for hit in _gmail(servers, "gmail_search", query).get("messages") or []
        ]
        people = [one for one in unread if not _NOT_A_PERSON.search(one["address"])]
        marks = {} if self._mail_reply is None else self._mail_reply.marks([
            # A sender with no display name shows its address; the address is never sent.
            (one["id"], "" if one["from"] == one["address"] else one["from"], one["subject"])
            for one in people
        ])
        letters: list[dict[str, str | bool | float | None]] = []
        for one in people:
            reply, junk = marks.get(one["id"], (None, False))
            rated = None if self._mail_reply is None else self._mail_reply.rating(one["id"])
            # A letter not rated yet has no importance or category: the UI keeps its order.
            rating = {} if rated is None else {
                "importance": rated["score"], "category": rated["category"],
            }
            letters.append({
                **one, "reply": reply, "junk": junk and one["id"] not in self._kept, **rating,
            })
        self._listed = frozenset(str(one["id"]) for one in letters)
        self._junk = frozenset(str(one["id"]) for one in letters if one["junk"])
        return {"unread": sorted(letters, key=lambda one: str(one["received"]), reverse=True)}

    def gmail(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """One call to the ``gmail`` server answered as JSON (ADR 0242: the model's ``mail``).

        A LookupError when Gmail is not connected; the server's own failure, sign-in
        included, is a ToolError.
        """
        return _gmail(self._servers(MAIL_SERVER), tool, args)

    def _actionable(self) -> frozenset[str]:
        """The ids a tap may change: the junk offered; with the mail page on, any listed or open."""
        if self._view is None:
            return self._junk
        return self._listed.union(filter(None, [self._view.mail_id()]))

    def _modify(
        self, ids: list[str], labels: Mapping[str, list[str]], *, allowed: AbstractSet[str],
    ) -> None:
        if not ids or not set(ids) <= allowed:
            msg = "not letters the home offered"
            raise ValueError(msg)
        _gmail(self._servers(MAIL_SERVER), "gmail_batchModify", {"messageIds": ids, **labels})

    def archive(self, ids: list[str], *, undo: bool = False) -> None:
        """Take letters out of the inbox, or put them back: Allen's own tap, never a proposal.

        Only ids the last :meth:`mail` offered as junk (with the mail page on, any it listed
        and the open letter, ADR 0148) are archived and only ids archived here (or, with the
        page on, those) are restored; anything else is a ValueError (the route's 400).
        Archiving is removing the INBOX label: the letter stays in All Mail, and nothing is
        deleted.
        """
        allowed: AbstractSet[str]
        if undo:
            allowed = self._archived | (self._actionable() if self._view else frozenset())
        else:
            allowed = self._actionable()
        labels = {"addLabelIds" if undo else "removeLabelIds": [_INBOX]}
        self._modify(ids, labels, allowed=allowed)
        LOGGER.info("mail %s: %s", "unarchive" if undo else "archive", " ".join(ids))
        # Only junk offers are the model's marks to learn from (ADR 0128).
        pairs: AbstractSet[str] = self._archived_junk if undo else self._junk
        outcome = [one for one in ids if one in pairs]
        if self._mail_reply is not None and outcome:
            self._mail_reply.outcome(outcome, "unarchive" if undo else "archive")
        if undo:
            self._archived.difference_update(ids)
            self._archived_junk.difference_update(ids)
            self._kept.update(ids)  # taken back: not suggested as junk again
        else:
            self._archived.update(ids)
            self._archived_junk.update(outcome)
            self._junk = self._junk.difference(ids)

    def letter(self, message_id: str) -> dict[str, str]:
        """One letter whole for the Dashboard's page (ADR 0147): headers and a plain-text body.

        Read like :meth:`mail` reads, outside any turn, and kept for the session; an id Gmail
        does not know is a LookupError (the route's 404). The text is shown to Allen only:
        it never goes to Jev or the model from here.
        """
        if message_id not in self._letters:
            args = {"messageId": message_id, "format": "full"}
            try:
                message = _gmail(self._servers(MAIL_SERVER), "gmail_get", args)
            except ToolError as exc:
                if "not found" in str(exc).lower() or "404" in str(exc):
                    raise LookupError(str(exc)) from exc
                raise
            head = _letter(message)
            raw = str(message.get("body") or "")
            text = mail_body(raw, links=True)
            self._letters[message_id] = {
                "id": head["id"],
                "thread_id": head["thread_id"],
                "from": head["from"],
                "address": head["address"],
                "to": str(message.get("to") or ""),
                "subject": head["subject"],
                "received": head["received"],
                "text": text,
                "layout": mail_layout(raw, text),
            }
            while len(self._letters) > _LETTERS_KEPT:
                del self._letters[next(iter(self._letters))]
        return self._letters[message_id]

    def summary(self, message_id: str) -> str:
        """One short sentence on what the letter is, in the UI language; kept per id (ADR 0148).

        A LookupError (404) when no model is configured or Gmail does not know the id; a failed
        call raises and is not kept. The model is the daemon's cheapest analysis preset, never Jev.
        """
        if self._summarizer is None:
            msg = "no summary model is configured"
            raise LookupError(msg)
        letter = self.letter(message_id)
        if message_id not in self._summaries:
            system = _SUMMARY_PROMPT.format(language=lang.language_name())
            said = self._summarizer(
                system,
                f"Subject: {letter['subject']}\nFrom: {letter['from']} <{letter['address']}>\n\n"
                f"{letter['text']}",
            )
            if not said:
                msg = "the model said nothing"
                raise RuntimeError(msg)
            self._summaries[message_id] = said
            while len(self._summaries) > _LETTERS_KEPT:
                del self._summaries[next(iter(self._summaries))]
        return self._summaries[message_id]

    def mark_read(self, ids: list[str], *, unread: bool = False) -> None:
        """Mark letters read (drop UNREAD) once Allen opened them, or unread again."""
        labels = {"addLabelIds" if unread else "removeLabelIds": ["UNREAD"]}
        self._modify(ids, labels, allowed=self._actionable())

    def trash(self, ids: list[str], *, undo: bool = False) -> None:
        """Move letters to Gmail's Trash (kept 30 days there, never deleted here), or back."""
        labels = {"removeLabelIds": ["TRASH"], "addLabelIds": [_INBOX]} if undo else {
            "addLabelIds": ["TRASH"],
        }
        self._modify(ids, labels, allowed=self._actionable())
        LOGGER.info("mail %s: %s", "untrash" if undo else "trash", " ".join(ids))
        self._junk = self._junk.difference(ids)

    def brief(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """This morning's brief: yesterday's report read for a person; None before it exists."""
        today = datetime.now(self._zone).date()
        args: dict[str, Any] = {
            "local_date": (today - timedelta(days=1)).isoformat(),
            "timezone": self._zone_name,
        }
        try:
            part = get_briefing(conn, args)
        except DailyError as exc:
            if exc.code == "not_found":
                return None
            raise
        chunks = [part["content"]]
        while part["next_cursor"]:
            part = get_briefing(conn, {**args, "cursor": part["next_cursor"]})
            chunks.append(part["content"])
        return {"date": today.isoformat(), **brief_of("".join(chunks))}


def _gmail(servers: McpServers, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    payload = servers.call(MAIL_SERVER, tool, args)
    # The server answers JSON in a text block and reports a failure as {"error": ...} there;
    # either kind of failure is the route's 502, never a ValueError's 400.
    try:
        body = json.loads(payload.get("text", ""))
    except ValueError as exc:
        msg = f"{tool}: answer is not JSON"
        raise ToolError(msg, code="mcp_tool_error") from exc
    if not isinstance(body, dict) or "error" in body:
        msg = f"{tool}: {body.get('error') if isinstance(body, dict) else body!r}"
        raise ToolError(msg, code="mcp_tool_error")
    return body


def _letter(message: Mapping[str, Any]) -> dict[str, str]:
    """One ``gmail_get`` answer in metadata format: ids, sender, subject and Date header."""
    name, address = parseaddr(str(message.get("from") or ""))
    try:
        received = parsedate_to_datetime(str(message["date"])).astimezone(UTC).isoformat()
    except (KeyError, TypeError, ValueError):
        received = ""  # no usable Date header: the letter still shows, sorted last
    return {
        "id": str(message["id"]),
        "thread_id": str(message.get("threadId") or message["id"]),
        "from": name or address,
        "address": address,
        "subject": str(message.get("subject") or ""),
        "received": received,
    }


def _calendar_view(servers: McpServers, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Graph's events of ``[start, end)``, the cancelled ones left out."""
    view = {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "select": "id,subject,start,end,isAllDay,isCancelled",
        "fetchAllPages": True,
    }
    return [one for one in _graph(servers, "get-calendar-view", view) if not one.get("isCancelled")]


def _event(event: Mapping[str, Any], zone: tzinfo) -> dict[str, Any]:
    """Graph answers calendarView in UTC; an all-day event starts at local midnight."""
    if event.get("isAllDay"):
        day = date.fromisoformat(str(event["start"]["dateTime"])[:10])
        return {
            "id": event["id"],
            "title": str(event.get("subject") or ""),
            "start": datetime.combine(day, time(), zone).isoformat(),
            "all_day": True,
        }
    stamp = [
        datetime.fromisoformat(str(event[key]["dateTime"])).replace(tzinfo=UTC).isoformat()
        for key in ("start", "end")
    ]
    return {
        "id": event["id"],
        "title": str(event.get("subject") or ""),
        "start": stamp[0],
        "end": stamp[1],
    }


def _todo(list_id: str, task: Mapping[str, Any], zone: tzinfo) -> dict[str, Any]:
    """To Do keeps a due date, not an instant: it is due by the end of that local day."""
    row = {"id": f"{list_id}{_ID_SEP}{task['id']}", "title": str(task.get("title") or "")}
    if task.get("dueDateTime"):
        day = date.fromisoformat(str(task["dueDateTime"]["dateTime"])[:10])
        row["due"] = datetime.combine(day, time(23, 59), zone).isoformat()
    return row
