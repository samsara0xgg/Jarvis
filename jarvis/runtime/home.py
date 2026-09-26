"""The companion home's reads (ADR 0050): Today, unread mail and the morning brief.

Calendar and To Do come through the live ``microsoft`` connection's tools outside
any model turn, mail from Gmail over IMAP, the weather from Open-Meteo. The one
write is a to-do's status, and only because Allen clicked its checkbox on the home.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import json
import logging
import os
import re
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.decision.daily_report import summary_of
from jarvis.execution.tools import ToolError
from jarvis.runtime.daily_report import PLAN_SERVER, _graph
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_store import get_briefing

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from datetime import tzinfo

    from jarvis.execution.mcp_tools import McpServers
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
_MAIL_LIMIT = 20
_MAIL_TIMEOUT_S = 8.0
_GMAIL_IMAP = "imap.gmail.com"
_MAIL_FIELDS = "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
_NOT_A_PERSON = re.compile(r"no-?reply|notification|mailer-daemon|bounce", re.IGNORECASE)


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
    ) -> None:
        """Bind the live connections, the local zone (name, zone) and the forecast's place.

        ``weather_at`` holds ``latitude`` and ``longitude``; None leaves the weather out.
        """
        self._connections = connections
        self._zone_name, self._zone = zone
        self._weather_at = weather_at

    def _servers(self) -> McpServers:
        """The live Microsoft client; LookupError (the routes' 404) when it is not connected."""
        try:
            return self._connections.client_for(PLAN_SERVER)
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

        IMAP with an app password (``GMAIL_ADDRESS``, ``GMAIL_APP_PASSWORD`` in the
        runtime env); the inbox is opened read-only, so nothing is marked read.
        """
        address, password = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
        if not address or not password:
            msg = "Gmail is not set up: GMAIL_ADDRESS and GMAIL_APP_PASSWORD"
            raise LookupError(msg)
        with imaplib.IMAP4_SSL(_GMAIL_IMAP, timeout=_MAIL_TIMEOUT_S) as box:
            box.login(address, password)
            box.select("INBOX", readonly=True)
            # Gmail's own sort into Primary is the "from people" filter.
            _, found = box.uid("SEARCH", "X-GM-RAW", '"category:primary is:unread"')
            uids = found[0].split()[-_MAIL_LIMIT:]
            if not uids:
                return {"unread": []}
            _, rows = box.uid("FETCH", b",".join(uids).decode(), _MAIL_FIELDS)
        unread = [_letter(*row) for row in rows if isinstance(row, tuple)]
        people = [one for one in unread if not _NOT_A_PERSON.search(one.pop("address"))]
        return {"unread": sorted(people, key=lambda one: one["received"], reverse=True)}

    def brief(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """This morning's brief: yesterday's saved daily report, whole; None before it exists."""
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
        body = "".join(chunks)
        return {"date": today.isoformat(), "summary": summary_of(body), "body": body}


def _letter(meta: bytes, header: bytes) -> dict[str, str]:
    """One FETCH answer: UID and INTERNALDATE from its line, From and Subject from its header."""
    uid = re.search(rb"UID (\d+)", meta)
    received = re.search(rb'INTERNALDATE "([^"]+)"', meta)
    if uid is None or received is None:
        msg = f"unexpected IMAP answer: {meta[:80]!r}"
        raise ValueError(msg)
    letter = email.message_from_bytes(header, policy=email.policy.default)
    sender = letter["From"].addresses[0] if letter["From"] and letter["From"].addresses else None
    return {
        "id": uid.group(1).decode(),
        "from": (sender.display_name or sender.addr_spec) if sender else "",
        "address": sender.addr_spec if sender else "",
        "subject": str(letter["Subject"] or ""),
        "received": datetime.strptime(received.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
        .astimezone(UTC)
        .isoformat(),
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
