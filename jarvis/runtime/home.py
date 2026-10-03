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
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.daily_report import brief_of
from jarvis.execution.tools import ToolError
from jarvis.runtime.daily_report import PLAN_SERVER, _graph
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_store import get_briefing

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet
    from datetime import tzinfo

    from jarvis.decision.mail_reply import MailReply
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.dashboard import FocusState
    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)
_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_WEATHER_TIMEOUT_S = 4.0
_FORECAST_STEP_H = 3
# WMO weather codes, as Open-Meteo documents them, folded into the home's six icons.
_WMO = (
    ((0, 1), "sun"),
    ((2, 3), "cloud"),
    ((45, 48), "fog"),
    ((71, 72, 73, 74, 75, 76, 77, 85, 86), "snow"),
    ((95, 96, 97, 98, 99), "storm"),
)
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


def mail_body(body: str, *, links: bool = False) -> str:
    """The message body as plain text, capped; an HTML-only message loses its markup.

    ``links`` keeps each anchor's address as ``text (url)`` for the page; the model's
    mail record (ADR 0063) leaves them out.
    """
    if _HTML_HINT_RE.search(body):
        # ponytail: tag stripping, not an HTML renderer; tables and quoted replies stay flat.
        if links:
            body = _HTML_LINK_RE.sub(_link_text, body)
        body = _HTML_TAG_RE.sub("", _HTML_BREAK_RE.sub("\n", _HTML_DROP_RE.sub("", body)))
        body = html.unescape(body)
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body).strip()
    return body if len(body) <= _MAIL_BODY_CHARS else f"{body[:_MAIL_BODY_CHARS]}…"


def _kind(code: int) -> str:
    return next((kind for codes, kind in _WMO if code in codes), "rain")


def weather(latitude: float, longitude: float) -> dict[str, Any]:
    """Now, today's high and a 3-hourly forecast from the current hour; raises when unreachable."""
    query = (
        f"latitude={latitude}&longitude={longitude}&timezone=auto&timeformat=unixtime"
        "&forecast_days=2&current=temperature_2m,weather_code"
        "&hourly=temperature_2m,weather_code&daily=temperature_2m_max"
    )
    with urllib.request.urlopen(f"{_OPEN_METEO}?{query}", timeout=_WEATHER_TIMEOUT_S) as reply:  # noqa: S310 — a fixed https URL.
        body = json.load(reply)
    current, hourly = body["current"], body["hourly"]
    first = next(i for i, at in enumerate(hourly["time"]) if at + 3600 > current["time"])
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


class Home:
    """What the home's Today, mail and brief blocks read; built once at boot."""

    def __init__(
        self,
        connections: PluginConnections,
        zone: tuple[str, tzinfo],
        weather_at: Mapping[str, Any] | None,
        mail_reply: MailReply | None = None,
        focus: FocusState | None = None,
    ) -> None:
        """Bind the live connections, the local zone (name, zone) and the forecast's place.

        ``weather_at`` holds ``latitude`` and ``longitude``; None leaves the weather out.
        ``mail_reply`` marks letters that need a reply (ADR 0123); None leaves them all unmarked.
        ``focus`` switches the Dashboard's mail page on (ADR 0147): its open letter may be read,
        archived and trashed, not only the junk offered.
        """
        self._connections = connections
        self._zone_name, self._zone = zone
        self._weather_at = weather_at
        self._mail_reply = mail_reply
        self._focus = focus
        # Gmail ids: what the last mail() listed, what it offered as junk, what Allen archived,
        # what he took back.
        self._listed: frozenset[str] = frozenset()
        self._junk: frozenset[str] = frozenset()
        self._archived: set[str] = set()
        self._archived_junk: set[str] = set()
        self._kept: set[str] = set()
        self._letters: dict[str, dict[str, str]] = {}

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
        view = {
            "startDateTime": start.isoformat(),
            "endDateTime": (start + timedelta(days=1)).isoformat(),
            "select": "id,subject,start,end,isAllDay,isCancelled",
            "fetchAllPages": True,
        }
        events = [
            _event(one, self._zone)
            for one in _graph(servers, "get-calendar-view", view)
            if not one.get("isCancelled")
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

    def _actionable(self) -> frozenset[str]:
        """The ids a tap may change: the junk offered; with the mail page on, any listed or open."""
        if self._focus is None:
            return self._junk
        return self._listed.union(filter(None, [self._focus.mail_id()]))

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
            allowed = self._archived | (self._actionable() if self._focus else frozenset())
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
            self._letters[message_id] = {
                "id": head["id"],
                "thread_id": head["thread_id"],
                "from": head["from"],
                "address": head["address"],
                "to": str(message.get("to") or ""),
                "subject": head["subject"],
                "received": head["received"],
                "text": mail_body(str(message.get("body") or ""), links=True),
            }
            while len(self._letters) > _LETTERS_KEPT:
                del self._letters[next(iter(self._letters))]
        return self._letters[message_id]

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
