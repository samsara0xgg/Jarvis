"""The companion home's reads (ADR 0055): the weather, unread mail and the morning brief.

Mail comes through the live ``gmail`` connection's tools, outside any model turn; the weather
from Apple WeatherKit (ADR 0285). Today's events and to-dos are the ledger of his things
(ADR 0299), read by the Today route in ``daemon/routes``, not here. The writes are a To Do task's
status and a letter's INBOX label, each only because Allen clicked it on the home (ADR 0124 for
the letters).
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time as _clock
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.decision.daily_report import brief_of
from jarvis.execution.speakable import resolve_when
from jarvis.execution.tools import ToolError
from jarvis.execution.weather_tool import DAYS_AHEAD, DAYS_BACK, HOURLY_MAX_DAYS
from jarvis.runtime.daily_report import PLAN_SERVER
from jarvis.runtime.weather_day import fit
from jarvis.runtime.weather_held import Held
from jarvis.runtime.weather_place import ALTERNATIVES, Spot, country_of, get_json, locate
from jarvis.runtime.weatherkit import NOT_SET_UP, Unavailable, day_rows, home_line
from jarvis.runtime.weatherkit import alerts as wk_alerts
from jarvis.runtime.weatherkit import report as wk_report
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
    from jarvis.runtime.weatherkit import WeatherKit
    from jarvis.runtime.work_state import LLMAnalyst

LOGGER = logging.getLogger(__name__)
_AIR_QUALITY = "https://air-quality-api.open-meteo.com/v1/air-quality"
WEATHER_CARD_TTL_S: Final = 6 * 3600
"""How long the day line keeps a day's weather (ADR 0285): one WeatherKit read per day, a few
times a day at most, however often a phone scrubs back and forth."""
WEATHER_CARD_RETRY_S: Final = 600
"""How long a day whose weather could not be read is left alone before it is asked for again."""
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


def _span(span: tuple[date, date] | None) -> dict[str, Any]:
    """Open-Meteo's days: the two from today, or ``first`` to ``last``."""
    if span is None:
        return {"forecast_days": 2}
    return {"start_date": span[0].isoformat(), "end_date": span[1].isoformat()}


def _worst(values: list[float | None]) -> float | None:
    return max((one for one in values if one is not None), default=None)


def _air(latitude: float, longitude: float, span: tuple[date, date] | None) -> dict[str, Any]:
    """US AQI, PM2.5 and PM10 now and the worst of each day, for ``span`` (None: two days)."""
    body = get_json(
        _AIR_QUALITY,
        {
            "latitude": latitude, "longitude": longitude, "timezone": "auto",
            "timeformat": "unixtime", **_span(span),
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


def _days(request: WeatherRequest, label: str, today: date) -> tuple[date, date]:
    """The first and last day ``request`` asks for, in the place's own calendar.

    With no ``when`` it is today and tomorrow. His words are resolved as of ``today``.

    Raises:
        ToolError: His words are not a day or days, or the days are out of WeatherKit's range, or
            are not ones that hourly rows exist for.
    """
    if request.when is None:
        return today, today + timedelta(days=1)
    first, last = resolve_when(request.when, today, back=False, tool="weather")
    oldest, newest = today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_AHEAD)
    if first < oldest or last > newest:
        msg = (
            f"weather: {label}'s forecast runs from {DAYS_BACK} days back"
            f" ({oldest.isoformat()}) to {DAYS_AHEAD} days ahead ({newest.isoformat()});"
            " earlier or later days are not available (use web_search)"
        )
        raise ToolError(msg, code="invalid_arguments")
    if request.hourly and first < today:
        msg = (
            f"weather: hour-by-hour rows exist from now on, not for {first.isoformat()};"
            " ask for today or later, or drop hourly for the daily figures"
        )
        raise ToolError(msg, code="invalid_arguments")
    if request.hourly and (last - first).days >= HOURLY_MAX_DAYS:
        msg = f"weather: hourly rows are for at most {HOURLY_MAX_DAYS} days; ask for fewer days"
        raise ToolError(msg, code="invalid_arguments")
    return first, last


def _elsewhere(others: list[Spot]) -> dict[str, Any]:
    """The other places of the name, each with the ``place`` that reads it (ADR 0356)."""
    out: dict[str, Any] = {
        "same_name_elsewhere": [
            {"place": one.label, "use_place": f"{one.latitude},{one.longitude}"}
            for one in others[:ALTERNATIVES]
        ],
    }
    if len(others) > ALTERNATIVES:
        out["same_name_left_out"] = len(others) - ALTERNATIVES
    return out


def weather_lookup(
    place: Mapping[str, Any] | None, kit: WeatherKit | None, zone: str,
) -> Callable[[WeatherRequest], dict[str, Any]] | None:
    """The model's weather read (ADR 0285) for a :class:`WeatherRequest`, from WeatherKit.

    ``place`` is ``home.weather`` (``latitude``, ``longitude``, optional ``name`` and
    ``country_code``) in the local time zone ``zone``, the answer when the request names no
    place; None when the home has no location, which leaves the tool unregistered. Without
    ``kit`` (no WeatherKit key) every call is an error that says the weather is not set up.
    His words for the days (``request.when``) are resolved in the place's own zone by the client's
    clock, the runtime's.
    """
    if place is None:
        return None
    home = Spot(
        float(place["latitude"]), float(place["longitude"]), str(place.get("name") or "home"),
        zone, place.get("country_code") or country_of(zone),
    )
    known: dict[tuple[float, float], Spot] = {}
    held = Held()

    def lookup(request: WeatherRequest) -> dict[str, Any]:
        if kit is None:
            raise ToolError(NOT_SET_UP, code="not_configured")
        spot, others = locate(request, home, known)
        zone_name = str(spot.zone or home.zone)
        zone = ZoneInfo(zone_name)
        today = datetime.fromtimestamp(kit.clock(), zone).date()
        first, last = _days(request, spot.label, today)
        asked = (
            round(spot.latitude, 4), round(spot.longitude, 4), first, last, request.hourly,
            request.air_quality, request.when is None,
        )
        try:
            body = kit.read(
                spot.latitude, spot.longitude, zone=zone_name, country=spot.country,
                first=first, last=last,
            )
        except Unavailable as exc:  # the last good read of this request, if there is one (ADR 0357)
            last_good = held.last(asked, kit.clock(), zone_name, exc.why)
            if last_good is None:
                raise
            return last_good
        now = kit.clock()
        result = wk_report(
            body, spot.label, zone_name, first=first, last=last,
            whole=request.when is None, hourly=request.hourly, now=now,
            alerts=None if last < today else wk_alerts(
                kit, body, spot.latitude, spot.longitude, spot.country, zone, now,
            ),
        )
        if others:
            result |= _elsewhere(others)
        if request.air_quality:
            try:
                result["air_quality"] = _air(
                    spot.latitude, spot.longitude, None if request.when is None else (first, last),
                )
            except ToolError:  # the forecast stands without it
                result["air_quality"] = {
                    "unavailable": "not read for these days; its forecast reaches only a few days",
                }
        done = fit(result)
        held.keep(asked, now, done)
        return done

    return lookup


class Home:
    """What the home's weather, mail and brief blocks read; built once at boot."""

    def __init__(  # noqa: PLR0913 — the connections, the zone and the optional parts.
        self,
        connections: PluginConnections,
        zone: tuple[str, tzinfo],
        weather_at: Mapping[str, Any] | None,
        mail_reply: MailReply | None = None,
        view: ViewState | None = None,
        summarizer: Callable[[str, str], str] | None = None,
        weatherkit: WeatherKit | None = None,
    ) -> None:
        """Bind the live connections, the local zone (name, zone) and the forecast's place.

        ``weather_at`` holds ``latitude`` and ``longitude``; None leaves the weather out, as does
        a missing ``weatherkit`` (no key).
        ``mail_reply`` marks letters that need a reply (ADR 0123); None leaves them all unmarked.
        ``view`` switches the Dashboard's mail page on (ADR 0147): its open letter may be read,
        archived and trashed, not only the junk offered. ``summarizer`` writes a letter's one-line
        summary (ADR 0148); None leaves :meth:`summary` a 404.
        """
        self._connections = connections
        self._zone_name, self._zone = zone
        self._weather_at = weather_at
        self._weatherkit = weatherkit
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
        self._cards_lock = threading.Lock()
        self._cards: dict[date, tuple[float, dict[str, Any] | None]] = {}

    def _servers(self, server: str = PLAN_SERVER) -> McpServers:
        """The live client of ``server``; LookupError (the routes' 404) when it is not connected."""
        try:
            return self._connections.client_for(server)
        except ToolError as exc:
            raise LookupError(str(exc)) from exc

    def day_weather(self, day: date) -> dict[str, Any] | None:
        """WeatherKit's daily row for ``day`` at home (ADR 0285); None without a place or key.

        Raises:
            ToolError: WeatherKit did not answer or holds no forecast for the day.
        """
        if self._weather_at is None or self._weatherkit is None:
            return None
        place, kit = self._weather_at, self._weatherkit
        body = kit.read(
            float(place["latitude"]), float(place["longitude"]), zone=self._zone_name,
            country=place.get("country_code") or country_of(self._zone_name), first=day, last=day,
        )
        today = datetime.fromtimestamp(kit.clock(), self._zone).date()
        return day_rows(body, ZoneInfo(self._zone_name), first=day, last=day, today=today)[0]

    def day_weather_card(self, day: date) -> dict[str, Any] | None:
        """The weather of ``day`` for the day line.

        ``{condition, high, low, sunrise_ms, sunset_ms}``.

        ``high`` and ``low`` are degrees Celsius and the two times epoch milliseconds (to the
        minute, as WeatherKit's daily row words them); any of them is None where it has none.
        Built from :meth:`day_weather`, and kept per day (:data:`WEATHER_CARD_TTL_S`, or
        :data:`WEATHER_CARD_RETRY_S` after a failure), so the day route waits on WeatherKit about
        once a day. A day WeatherKit does not cover, no key, no place and any failure are all None:
        never an error.
        """
        now = _clock.time()
        with self._cards_lock:
            held = self._cards.get(day)
        if held is not None and now - held[0] < (
            WEATHER_CARD_TTL_S if held[1] is not None else WEATHER_CARD_RETRY_S
        ):
            return held[1]
        today = datetime.fromtimestamp(now, self._zone).date()
        card: dict[str, Any] | None = None
        if today - timedelta(days=DAYS_BACK) <= day <= today + timedelta(days=DAYS_AHEAD):
            try:
                row = self.day_weather(day)
            except Exception as exc:  # noqa: BLE001 - WeatherKit or the key failed: no weather
                LOGGER.warning("day weather: %s not read (%s)", day, type(exc).__name__)
                row = None
            if row is not None:
                card = {
                    "condition": row["condition"], "high": row["high_c"], "low": row["low_c"],
                    "sunrise_ms": self._clock_ms(day, row["sunrise"]),
                    "sunset_ms": self._clock_ms(day, row["sunset"]),
                }
        with self._cards_lock:
            self._cards = {d: h for d, h in self._cards.items() if now - h[0] < WEATHER_CARD_TTL_S}
            self._cards[day] = (now, card)
        return card

    def _clock_ms(self, day: date, text: str | None) -> int | None:
        """``HH:MM`` on ``day`` in the home's zone as epoch milliseconds."""
        if not text:
            return None
        moment = datetime.combine(day, time.fromisoformat(text), self._zone)
        return int(moment.timestamp() * 1000)

    def weather(self) -> dict[str, Any] | None:
        """Today's weather line; None without a place or key, or when it cannot be read."""
        if self._weather_at is None or self._weatherkit is None:
            return None
        try:
            place, kit = self._weather_at, self._weatherkit
            today = datetime.fromtimestamp(kit.clock(), self._zone).date()
            body = kit.read(  # the same request as the tool's default one, so one call serves both
                float(place["latitude"]), float(place["longitude"]), zone=self._zone_name,
                country=place.get("country_code") or country_of(self._zone_name),
                first=today, last=today + timedelta(days=1),
            )
            return home_line(body, self._zone_name, kit.clock())
        except Exception as exc:  # noqa: BLE001 — Today's events and to-dos still show without it.
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
