"""ADR 0051 — the companion home's routes over replayed Microsoft, Gmail and Open-Meteo answers.

The To Do list and task and the weather are the services' answers of
2026-09-25 (ids shortened, one forecast hour turned to rain so the icon
mapping shows); the calendar is empty on the real account, so its events are
written in Graph's calendarView shape, and the mail in the shape imaplib
returns for a Gmail FETCH. Each check asserts what a route serves (what the
home renders) or what Jarvis sent to Microsoft or Gmail.
"""

from __future__ import annotations

import asyncio
import functools
import imaplib
import io
import json
import sqlite3
import urllib.request
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Self
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
FIELDS = b"BODY[HEADER.FIELDS (FROM SUBJECT)]"
MAIL_ROWS: list[Any] = [
    (
        b'1 (UID 4101 INTERNALDATE "25-Sep-2026 14:40:00 -0700" ' + FIELDS + b" {70}",
        b'From: "Prof. Lee" <lee@uvic.ca>\r\nSubject: Office hours move to Thursday\r\n\r\n',
    ),
    b")",
    (
        b'2 (UID 4102 INTERNALDATE "25-Sep-2026 13:00:00 -0700" ' + FIELDS + b" {62}",
        b"From: GitHub <noreply@github.com>\r\nSubject: Your weekly digest\r\n\r\n",
    ),
    b")",
    (
        b'3 (UID 4103 INTERNALDATE "25-Sep-2026 15:05:00 -0700" ' + FIELDS + b" {90}",
        b"From: =?utf-8?b?5aaI5aaI?= <mom@example.com>\r\n"
        b"Subject: =?utf-8?b?5LuK5pma6L+Y5p2l5ZCX77yf?=\r\n\r\n",
    ),
    b")",
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


class _Connections:
    def __init__(self, server: _Microsoft | None) -> None:
        self.server = server

    def client_for(self, server: str) -> _Microsoft:
        if self.server is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return self.server


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


def _home(server: _Microsoft | None) -> Home:
    return Home(
        _Connections(server),  # type: ignore[arg-type]
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


class _Gmail:
    """imaplib.IMAP4_SSL's surface as the mail read uses it; records every command."""

    sent: ClassVar[list[tuple[Any, ...]]] = []

    def __init__(self, host: str, *, timeout: float) -> None:
        self.sent.append(("connect", host, timeout > 0))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.sent.append(("logout",))

    def login(self, user: str, password: str) -> None:
        self.sent.append(("login", user, password))

    def select(self, mailbox: str, *, readonly: bool) -> None:
        self.sent.append(("select", mailbox, readonly))

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.sent.append((command, *args))
        return ("OK", [b"4101 4102 4103"]) if command == "SEARCH" else ("OK", MAIL_ROWS)


def test_mail_is_unread_primary_gmail_from_people(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /inherent/mail: unread Primary Gmail, read-only, no-reply dropped, newest first."""
    monkeypatch.setenv("GMAIL_ADDRESS", "allen@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _Gmail)
    _Gmail.sent.clear()
    reply = _client(_home(_Microsoft())).get("/inherent/mail")
    assert reply.json() == {"unread": [
        {
            "id": "4103", "from": "妈妈", "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
            "received": "2026-09-25T22:05:00+00:00",
        },
        {
            "id": "4101", "from": "Prof. Lee", "subject": "Office hours move to Thursday",
            "received": "2026-09-25T21:40:00+00:00",
        },
    ]}
    assert _Gmail.sent == [
        ("connect", "imap.gmail.com", True),
        ("login", "allen@example.com", "app-password"),
        ("select", "INBOX", True),
        ("SEARCH", "X-GM-RAW", '"category:primary is:unread"'),
        ("FETCH", "4101,4102,4103", "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"),
        ("logout",),
    ]


def test_without_microsoft_or_gmail_the_home_says_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Microsoft connection, no Gmail password: 404, which the home shows as not connected."""
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
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
