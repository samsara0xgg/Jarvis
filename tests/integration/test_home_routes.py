"""ADR 0055 — the companion home's routes over replayed Microsoft, Gmail and Open-Meteo answers.

The To Do list and task and the weather are the services' answers of
2026-09-25 (ids shortened, one forecast hour turned to rain so the icon
mapping shows); the calendar is empty on the real account, so its events are
written in Graph's calendarView shape, and the mail in the JSON text blocks the
Google Workspace server's gmail_search and gmail_get answer. Each check asserts
what a route serves (what the home renders) or what Jarvis sent to Microsoft or
Gmail.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import sqlite3
import urllib.request
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.execution.tools import ToolError
from jarvis.runtime.home import Home
from jarvis.state.daily_report import save_report
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

ZONE = "America/Vancouver"
LIST = {"displayName": "Tasks", "wellknownListName": "defaultList", "id": "AQMkADAw-list="}
TASKS = [
    {"status": "notStarted", "title": "repack jarvis", "id": "AQMkADAw-repack="},
    {"status": "completed", "title": "old one", "id": "AQMkADAw-old="},
    {
        "status": "notStarted", "title": "CSC370 A3", "id": "AQMkADAw-a3=",
        "dueDateTime": {"dateTime": "2026-09-26T00:00:00.0000000", "timeZone": "UTC"},
    },
]
EVENTS = [
    {
        "id": "ev-call", "subject": "Call with Mom", "isAllDay": False, "isCancelled": False,
        "start": {"dateTime": "2026-09-26T01:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-26T01:30:00.0000000", "timeZone": "UTC"},
    },
    {
        "id": "ev-day", "subject": "Reading week", "isAllDay": True, "isCancelled": False,
        "start": {"dateTime": "2026-09-25T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-26T00:00:00.0000000", "timeZone": "UTC"},
    },
    {
        "id": "ev-off", "subject": "Cancelled", "isAllDay": False, "isCancelled": True,
        "start": {"dateTime": "2026-09-25T20:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-25T21:00:00.0000000", "timeZone": "UTC"},
    },
]
# gmail_get's metadata answers; gmail_search lists them in this order, which is not
# newest first, so the route's own sort by Date shows.
LETTERS: list[dict[str, Any]] = [
    {
        "id": "199a1c0d4101", "threadId": "199a1c0d4101", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Office hours move to Thursday", "from": '"Prof. Lee" <lee@uvic.ca>',
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 14:40:00 -0700",
    },
    {
        "id": "199a1c0d4102", "threadId": "199a1c0d4102", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Your weekly digest", "from": "GitHub <noreply@github.com>",
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 13:00:00 -0700",
    },
    {
        "id": "199a1c0d4103", "threadId": "199a1c0d4103", "labelIds": ["UNREAD", "INBOX"],
        "subject": "今晚还来吗？", "from": "妈妈 <mom@example.com>",  # noqa: RUF001 — her words.
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 15:05:00 -0700",
    },
]
WEATHER: dict[str, Any] = {
    "current": {"time": 1790400600, "temperature_2m": 10.5, "weather_code": 0},
    "hourly": {
        "time": [1790391600 + 3600 * i for i in range(16)],
        "temperature_2m": [
            11.6, 11.3, 10.8, 10.2, 10.6, 10.3, 10.2, 10.0,
            9.8, 9.7, 9.7, 9.5, 9.9, 11.3, 12.1, 12.7,
        ],
        # Index 5 was 0 (clear); 61 (rain) there shows the mapping.
        "weather_code": [51, 61, 0, 0, 0, 61, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    },
    "daily": {"temperature_2m_max": [13.0, 13.8]},
}
ANSWERS = {
    "list-todo-task-lists": [LIST],
    "list-todo-tasks": TASKS,
    "get-calendar-view": EVENTS,
}


class _Microsoft:
    """The connected server: Graph's JSON as a text block, like ms-365-mcp-server answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, dict(args)))
        return {"text": json.dumps({"value": ANSWERS.get(tool, [])})}


class _Gmail:
    """The connected Workspace server: JSON in a text block, a failure as {"error": ...}."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        if self.error:
            return {"text": json.dumps({"error": self.error})}
        if tool == "gmail_search":
            hits = [{"id": one["id"], "threadId": one["threadId"]} for one in LETTERS]
            return {"text": json.dumps({"messages": hits, "resultSizeEstimate": len(hits)})}
        letter = next(one for one in LETTERS if one["id"] == args["messageId"])
        return {"text": json.dumps({**letter, "snippet": "", "body": "", "attachments": []})}


class _Connections:
    def __init__(self, server: _Microsoft | None, gmail: _Gmail | None = None) -> None:
        self.servers = {"microsoft": server, "gmail": gmail}

    def client_for(self, server: str) -> _Microsoft | _Gmail:
        found = self.servers.get(server)
        if found is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return found


def _client(home: Home, conn: sqlite3.Connection | None = None) -> TestClient:
    """The routes wired the way the daemon wires them (runtime/inherent_loop.py)."""

    async def set_todo(todo_id: str, done: bool) -> None:  # noqa: FBT001 — the route's body.
        await asyncio.to_thread(functools.partial(home.set_todo, todo_id, done=done))

    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        today_read=functools.partial(asyncio.to_thread, home.today),
        todo_set=set_todo,
        mail_read=functools.partial(asyncio.to_thread, home.mail),
        brief_read=None if conn is None else functools.partial(home.brief, conn),
    )))


def _weather_answers(monkeypatch: pytest.MonkeyPatch, *, reachable: bool = True) -> list[str]:
    asked: list[str] = []

    def urlopen(url: str, *, timeout: float) -> io.BytesIO:
        del timeout
        asked.append(url)
        if not reachable:
            raise TimeoutError
        return io.BytesIO(json.dumps(WEATHER).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def _home(server: _Microsoft | None, gmail: _Gmail | None = None) -> Home:
    return Home(
        _Connections(server, gmail),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)),
        {"latitude": 49.28, "longitude": -123.12},
    )


def test_today_serves_calendar_open_todos_and_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /inherent/today: timed and all-day events, open to-dos due by day's end, 3-hourly sky."""
    asked = _weather_answers(monkeypatch)
    server = _Microsoft()
    reply = _client(_home(server)).get("/inherent/today")
    assert reply.status_code == 200
    body = reply.json()
    assert body["events"] == [
        {
            "id": "ev-call", "title": "Call with Mom",
            "start": "2026-09-26T01:00:00+00:00", "end": "2026-09-26T01:30:00+00:00",
        },
        {
            "id": "ev-day", "title": "Reading week",
            "start": "2026-09-25T00:00:00-07:00", "all_day": True,
        },
    ]
    assert body["todos"] == [
        {"id": "AQMkADAw-list=|AQMkADAw-repack=", "title": "repack jarvis"},
        {
            "id": "AQMkADAw-list=|AQMkADAw-a3=", "title": "CSC370 A3",
            "due": "2026-09-26T23:59:00-07:00",
        },
    ]
    assert body["weather"] == {
        "now_c": 10.5,
        "high_c": 13.0,
        "hours": [
            {"at": "2026-09-26T05:00:00+00:00", "kind": "sun", "temp_c": 10.8},
            {"at": "2026-09-26T08:00:00+00:00", "kind": "rain", "temp_c": 10.3},
            {"at": "2026-09-26T11:00:00+00:00", "kind": "sun", "temp_c": 9.8},
            {"at": "2026-09-26T14:00:00+00:00", "kind": "sun", "temp_c": 9.5},
        ],
    }
    assert "latitude=49.28&longitude=-123.12" in asked[0]
    # The calendar is read for the local day the request falls on.
    view = next(args for tool, args in server.calls if tool == "get-calendar-view")
    start = datetime.fromisoformat(view["startDateTime"])
    assert start.timetz() == time(tzinfo=start.tzinfo)
    assert start.date() == datetime.now(ZoneInfo(ZONE)).date()
    assert datetime.fromisoformat(view["endDateTime"]) - start == timedelta(days=1)
    assert {"todoTaskListId": "AQMkADAw-list=", "fetchAllPages": True} in [
        args for tool, args in server.calls if tool == "list-todo-tasks"
    ]


def test_weather_failure_keeps_the_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open-Meteo down: Today still answers, without weather."""
    _weather_answers(monkeypatch, reachable=False)
    body = _client(_home(_Microsoft())).get("/inherent/today").json()
    assert body["weather"] is None
    assert [t["title"] for t in body["todos"]] == ["repack jarvis", "CSC370 A3"]


def test_the_checkbox_writes_the_task_status_to_todo() -> None:
    """POST /inherent/today/todo sends one update-todo-task per click; a bad id sends none."""
    server = _Microsoft()
    client = _client(_home(server))
    todo = "AQMkADAw-list=|AQMkADAw-repack="
    assert client.post("/inherent/today/todo", json={"id": todo, "done": True}).status_code == 200
    assert client.post("/inherent/today/todo", json={"id": todo, "done": False}).status_code == 200
    assert server.calls == [
        ("update-todo-task", {
            "todoTaskListId": "AQMkADAw-list=", "todoTaskId": "AQMkADAw-repack=",
            "body": {"status": "completed"},
        }),
        ("update-todo-task", {
            "todoTaskListId": "AQMkADAw-list=", "todoTaskId": "AQMkADAw-repack=",
            "body": {"status": "notStarted"},
        }),
    ]
    bad = client.post("/inherent/today/todo", json={"id": "no-bar", "done": True})
    assert bad.status_code == 400
    assert len(server.calls) == 2


def test_mail_is_unread_primary_gmail_from_people() -> None:
    """GET /inherent/mail: unread Primary Gmail by read-only tools, no-reply out, newest first."""
    gmail = _Gmail()
    reply = _client(_home(_Microsoft(), gmail)).get("/inherent/mail")
    assert reply.json() == {"unread": [
        {
            "id": "199a1c0d4103", "from": "妈妈", "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
            "received": "2026-09-25T22:05:00+00:00",
        },
        {
            "id": "199a1c0d4101", "from": "Prof. Lee", "subject": "Office hours move to Thursday",
            "received": "2026-09-25T21:40:00+00:00",
        },
    ]}
    assert gmail.calls == [
        ("gmail_search", {"query": "category:primary is:unread", "maxResults": 20}),
        *[("gmail_get", {"messageId": one["id"], "format": "metadata"}) for one in LETTERS],
    ]


def test_a_gmail_error_answer_is_the_homes_502() -> None:
    """Not logged in: the server answers {"error": ...} in its text, and the home gets a 502."""
    gmail = _Gmail(error="No browser available for authentication.")
    reply = _client(_home(_Microsoft(), gmail)).get("/inherent/mail")
    assert reply.status_code == 502
    assert "No browser available" in reply.json()["detail"]
    assert [tool for tool, _args in gmail.calls] == ["gmail_search"]


def test_without_microsoft_or_gmail_the_home_says_not_connected() -> None:
    """No Microsoft or Gmail connection: 404, which the home shows as not connected."""
    client = _client(_home(None))
    assert client.get("/inherent/today").status_code == 404
    assert client.get("/inherent/mail").status_code == 404
    assert client.post("/inherent/today/todo", json={"id": "a|b", "done": True}).status_code == 404


def test_brief_is_yesterdays_saved_report_whole(tmp_path: Path) -> None:
    """GET /inherent/brief: 404 until yesterday's report is saved, then all of it, dated today."""
    open_event_log(tmp_path / "events.db").close()
    # The daemon reads on its loop thread; TestClient serves from its own thread.
    conn = sqlite3.connect(tmp_path / "events.db", check_same_thread=False)
    client = _client(_home(_Microsoft()), conn)
    assert client.get("/inherent/brief").status_code == 404
    today = datetime.now(ZoneInfo(ZONE)).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    content = f"# 工作日报 {yesterday}\n\n## 核心摘要\n写了首页的后端。\n\n## 细节\n" + "长" * 9000
    save_report(
        conn, memory_path=None, timesink_path=None, day=yesterday, zone=ZONE,
        content=content, source_refs=[], coverage={}, expected_version=0, action_id="brief",
    )
    body = client.get("/inherent/brief").json()
    assert body == {"date": today.isoformat(), "summary": "写了首页的后端。", "body": content}
