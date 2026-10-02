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
import contextlib
import functools
import io
import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from datetime import time as clock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.execution.tools import ToolError
from jarvis.runtime.home import Home
from jarvis.state.daily_report import save_report
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

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
    {
        "id": "199a1c0d4104", "threadId": "199a1c0d4104", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Your receipt", "from": "billing@shop.example",  # no display name
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 12:00:00 -0700",
    },
    {
        "id": "199a1c0d4105", "threadId": "199a1c0d4105", "labelIds": ["UNREAD", "INBOX"],
        "subject": "50% off everything this weekend", "from": "Shop Deals <deals@shop.example>",
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 11:00:00 -0700",
    },
    {
        "id": "199a1c0d4106", "threadId": "199a1c0d4106", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Our weekly newsletter", "from": "Weekly Brew <hello@brew.example>",
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 10:00:00 -0700",
    },
    {
        "id": "199a1c0d4107", "threadId": "199a1c0d4107", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Quick question about your listing", "from": "Sam <sam@example.com>",
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 09:00:00 -0700",
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
        if tool == "gmail_batchModify":
            body = {"modifiedCount": len(args["messageIds"]), "status": "success"}
            return {"text": json.dumps(body)}
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

    async def archive(ids: list[str], archive: bool) -> None:  # noqa: FBT001 — the route's body.
        await asyncio.to_thread(functools.partial(home.archive, ids, undo=not archive))

    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        today_read=functools.partial(asyncio.to_thread, home.today),
        todo_set=set_todo,
        mail_read=functools.partial(asyncio.to_thread, home.mail),
        mail_archive=archive,
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
    assert start.timetz() == clock(tzinfo=start.tzinfo)
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
    letters = reply.json()["unread"]
    assert [(one["id"], one["from"], one["reply"], one["junk"]) for one in letters] == [
        ("199a1c0d4103", "妈妈", None, False),
        ("199a1c0d4101", "Prof. Lee", None, False),
        ("199a1c0d4104", "billing@shop.example", None, False),
        ("199a1c0d4105", "Shop Deals", None, False),
        ("199a1c0d4106", "Weekly Brew", None, False),
        ("199a1c0d4107", "Sam", None, False),
    ]
    assert letters[0] == {
        "id": "199a1c0d4103", "from": "妈妈", "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
        "received": "2026-09-25T22:05:00+00:00", "reply": None, "junk": False,
    }
    assert gmail.calls == [
        ("gmail_search", {"query": "in:inbox category:primary is:unread", "maxResults": 20}),
        *[("gmail_get", {"messageId": one["id"], "format": "metadata"}) for one in LETTERS],
    ]


class _Jev:
    """A fake decisions endpoint (ADR 0123): answers P(needs a reply) by the subject line."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # subject -> (P(needs a reply), P(junk))
        self.odds = {
            "Office hours move to Thursday": (0.97, 0.02),
            "今晚还来吗？": (0.04, 0.01),  # noqa: RUF001 — her words.
            "Your receipt": (0.5, 0.5),
            "50% off everything this weekend": (0.03, 0.95),
            "Our weekly newsletter": (0.05, 0.89),  # just under the junk bar
            "Quick question about your listing": (0.95, 0.99),  # junk-looking, but may need a reply
        }
        self.status = 200
        self.delay_s = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                time.sleep(outer.delay_s)
                subject = request["state"].split("Subject: ", 1)[1]
                reply, junk = outer.odds[subject]
                body = json.dumps({
                    "answers": {"reply": {"noul": reply}, "junk": {"noul": junk}},
                    "usage": {"cost": 0.00001},
                }).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):  # the client gave up on a slow call
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, *, timeout_ms: int = 1500, log: JevLog | None = None) -> MailReply:
        route = SurrogateRoute(
            model="typesafe/jev-1.13", min_confidence=1.0, timeout_ms=timeout_ms,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions", log=log,
        )
        return MailReply(route, 0.9, 0.1, 0.9)


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-a-secret")
    fake = _Jev()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def _marks(client: TestClient) -> dict[str, str | None]:
    return {one["id"]: one["reply"] for one in client.get("/inherent/mail").json()["unread"]}


def _junk(client: TestClient) -> list[str]:
    return sorted(one["id"] for one in client.get("/inherent/mail").json()["unread"] if one["junk"])


def _with_jev(mail_reply: MailReply) -> TestClient:
    home = Home(
        _Connections(_Microsoft(), _Gmail()),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, mail_reply,
    )
    return _client(home)


def test_mail_reply_sends_only_name_and_subject_and_marks_by_threshold(
    jev: _Jev, caplog: pytest.LogCaptureFixture,
) -> None:
    """Each person's letter is asked once, name and subject only, zdr on; the bars map to marks."""
    client = _with_jev(jev.reply())
    caplog.set_level("INFO", logger="jarvis.decision.mail_reply")
    assert _marks(client) == {
        "199a1c0d4101": "yes", "199a1c0d4103": "fyi", "199a1c0d4104": None,
        "199a1c0d4105": "fyi", "199a1c0d4106": "fyi", "199a1c0d4107": "yes",
    }
    # Junk at the bar only: 0.95 is, 0.89 is not, and a letter that may need a reply never is.
    assert _junk(client) == ["199a1c0d4105"]
    sent = sorted(jev.requests, key=lambda one: one["state"])
    assert [one["state"] for one in sent] == [
        "From: Prof. Lee\nSubject: Office hours move to Thursday",
        "From: Sam\nSubject: Quick question about your listing",
        "From: Shop Deals\nSubject: 50% off everything this weekend",
        "From: Weekly Brew\nSubject: Our weekly newsletter",
        "From: unknown\nSubject: Your receipt",
        "From: 妈妈\nSubject: 今晚还来吗？",  # noqa: RUF001 — her words.
    ]
    for one in sent:
        assert set(one) == {"model", "state", "questions", "provider"}
        assert one["model"] == "typesafe/jev-1.13"
        assert one["provider"] == {"zdr": True}
        assert set(one["questions"]) == {"reply", "junk"}
        for question in one["questions"].values():
            assert question["type"] == "noul"
            assert question["instructions"]
    assert "@" not in json.dumps(jev.requests)  # no address, and the no-reply sender is not asked
    # The log line names the letter by Gmail id with Jev's probability and the mark, never the text.
    lines = [one.getMessage() for one in caplog.records if "one letter asked" in one.getMessage()]
    assert any("id 199a1c0d4101 reply p=0.970 mark=yes junk p=0.020 junk=False" in x for x in lines)
    assert any("id 199a1c0d4105 reply p=0.030 mark=fyi junk p=0.950 junk=True" in x for x in lines)
    assert not any(word in line for line in lines for word in ("Office", "Lee", "Prof", "off"))
    # A second poll asks nothing: the answers are cached per message id.
    assert _marks(client)["199a1c0d4101"] == "yes"
    assert len(jev.requests) == 6


def test_mail_reply_off_or_without_a_key_marks_nothing(
    jev: _Jev, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off: no call. On without OPENROUTER_API_KEY: no call. Either way, reply is null."""
    assert set(_marks(_client(_home(_Microsoft(), _Gmail()))).values()) == {None}
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert set(_marks(_with_jev(jev.reply())).values()) == {None}
    assert jev.requests == []


def test_mail_reply_errors_leave_letters_unmarked_and_are_retried(jev: _Jev) -> None:
    """An HTTP error is not cached: the poll shows no mark, and a later poll asks again."""
    client = _with_jev(jev.reply())
    jev.status = 500
    assert set(_marks(client).values()) == {None}
    assert len(jev.requests) == 6
    jev.status = 200
    assert _marks(client)["199a1c0d4101"] == "yes"
    assert len(jev.requests) == 12


def test_mail_reply_slow_answers_wait_only_the_deadline(jev: _Jev) -> None:
    """Past timeout_ms the route answers unmarked at once; a later poll asks again."""
    jev.delay_s = 0.6
    client = _with_jev(jev.reply(timeout_ms=150))
    began = time.monotonic()
    assert set(_marks(client).values()) == {None}
    assert time.monotonic() - began < 0.5
    jev.delay_s = 0.0
    time.sleep(0.8)  # the abandoned calls end on their own
    assert _marks(client)["199a1c0d4101"] == "yes"
    assert len(jev.requests) == 12


def test_junk_is_archived_only_when_offered_and_undone_only_when_archived(jev: _Jev) -> None:
    """Archive removes INBOX from exactly the offered ids, never deletes; unarchive adds it back."""
    gmail = _Gmail()
    home = Home(
        _Connections(_Microsoft(), gmail),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, jev.reply(),
    )
    client = _client(home)
    junk, other = "199a1c0d4105", "199a1c0d4101"

    def changes() -> list[dict[str, Any]]:
        return [args for tool, args in gmail.calls if tool == "gmail_batchModify"]

    # Nothing was offered yet, so nothing can be archived.
    assert client.post("/inherent/mail/archive", json={"ids": [junk]}).status_code == 400
    assert _junk(client) == [junk]
    for ids in ([other], [junk, other], [], ["nope"]):
        assert client.post("/inherent/mail/archive", json={"ids": ids}).status_code == 400
    assert client.post("/inherent/mail/unarchive", json={"ids": [junk]}).status_code == 400
    assert changes() == []
    assert client.post("/inherent/mail/archive", json={"ids": [junk]}).status_code == 200
    assert changes() == [{"messageIds": [junk], "removeLabelIds": ["INBOX"]}]
    # Once archived it is no longer on offer, and the other letters are untouched.
    assert client.post("/inherent/mail/archive", json={"ids": [junk]}).status_code == 400
    assert client.post("/inherent/mail/unarchive", json={"ids": [other]}).status_code == 400
    assert client.post("/inherent/mail/unarchive", json={"ids": [junk]}).status_code == 200
    assert changes()[-1] == {"messageIds": [junk], "addLabelIds": ["INBOX"]}
    assert len(changes()) == 2
    # Taken back: it stays in the list but is not suggested again.
    assert _junk(client) == []
    assert {tool for tool, _ in gmail.calls} == {"gmail_search", "gmail_get", "gmail_batchModify"}
    assert all("TRASH" not in json.dumps(args) for _, args in gmail.calls)
    # The request body of the questions is still name and subject only.
    assert all(set(one) == {"model", "state", "questions", "provider"} for one in jev.requests)


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


def test_brief_is_yesterdays_saved_report_read_for_a_person(tmp_path: Path) -> None:
    """GET /inherent/brief: 404 until yesterday's report is saved, then its reading view."""
    open_event_log(tmp_path / "events.db").close()
    # The daemon reads on its loop thread; TestClient serves from its own thread.
    conn = sqlite3.connect(tmp_path / "events.db", check_same_thread=False)
    client = _client(_home(_Microsoft()), conn)
    assert client.get("/inherent/brief").status_code == 404
    today = datetime.now(ZoneInfo(ZONE)).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    content = (
        f"# 工作日报 {yesterday}\n\n## 核心摘要\n写了首页的后端。\n有实证：1 首页后端（已提交）\n\n"  # noqa: RUF001
        "## 工作事项\n### 1. 首页后端 — 已提交（提交 abc1234，main）\n写好三条路由。\n引用：#1\n\n"  # noqa: RUF001
        "## 数据覆盖与不确定性\n- " + "长" * 9000 + "\n\n## 证据引用\n#1 event:abc\n"
    )
    save_report(
        conn, memory_path=None, timesink_path=None, day=yesterday, zone=ZONE,
        content=content, source_refs=[], coverage={}, expected_version=0, action_id="brief",
    )
    body = client.get("/inherent/brief").json()
    assert body == {
        "date": today.isoformat(),
        "summary": "写了首页的后端。",
        "lead": "写了首页的后端。",
        "items": 1,
        "sections": [
            {
                "key": "items",
                "title": "昨天做了什么",
                "rows": [
                    {
                        "text": "首页后端",
                        "tag": "done",
                        "label": "已完成",
                        "status": "已提交",
                        "note": "写好三条路由。",
                    }
                ],
            }
        ],
    }


def test_mail_calls_marks_and_archives_are_lines_in_the_local_dataset(
    jev: _Jev, tmp_path: Path,
) -> None:
    """ADR 0128: a line per letter asked, its mark, and an archive or undo that joins by id."""
    path = tmp_path / "jev" / "decisions.jsonl"
    client = _with_jev(jev.reply(log=JevLog(path)))
    junk = "199a1c0d4105"
    assert _junk(client) == [junk]
    assert client.post("/inherent/mail/archive", json={"ids": [junk]}).status_code == 200
    assert client.post("/inherent/mail/unarchive", json={"ids": [junk]}).status_code == 200
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    calls = [one for one in lines if one["kind"] == "call"]
    asked = {one["id"] for one in LETTERS if one["id"] != "199a1c0d4102"}  # the no-reply sender
    assert sorted(one["ref"] for one in calls) == sorted(asked)
    assert {one["use"] for one in lines} == {"mail"}
    (shop,) = [one for one in calls if one["ref"] == junk]
    assert shop["state"] == "From: Shop Deals\nSubject: 50% off everything this weekend"
    assert shop["model"] == "typesafe/jev-1.13"
    assert shop["answers"] == {"reply": {"noul": 0.03}, "junk": {"noul": 0.95}}
    assert set(shop["questions"]) == {"reply", "junk"}
    assert shop["questions"]["junk"]["type"] == "noul"
    assert (shop["error"], shop["cost_usd"]) == (None, 0.00001)
    assert shop["latency_ms"] >= 0
    assert shop["ts"].endswith("+00:00")
    decisions = {one["ref"]: one for one in lines if one["kind"] == "decision"}
    assert len(decisions) == 6
    assert (decisions[junk]["mark"], decisions[junk]["junk"]) == ("fyi", True)
    assert (decisions["199a1c0d4101"]["mark"], decisions["199a1c0d4101"]["junk"]) == ("yes", False)
    assert [(one["ref"], one["outcome"]) for one in lines if one["kind"] == "outcome"] == [
        (junk, "archive"), (junk, "unarchive"),
    ]
    text = path.read_text(encoding="utf-8").lower()
    assert "test-key-not-a-secret" not in text
    assert "authorization" not in text


def test_a_failed_mail_call_names_its_error_and_an_unwritable_dataset_changes_no_mark(
    jev: _Jev, tmp_path: Path,
) -> None:
    """HTTP 500: a call line with the error, no decision. A blocked path: the same marks as ever."""
    path = tmp_path / "jev" / "decisions.jsonl"
    jev.status = 500
    assert set(_marks(_with_jev(jev.reply(log=JevLog(path)))).values()) == {None}
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 6
    assert {(one["kind"], one["use"], one["error"], one["answers"]) for one in lines} == {
        ("call", "mail", "http", None),
    }
    jev.status = 200
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    client = _with_jev(jev.reply(log=JevLog(blocker / "decisions.jsonl")))
    assert _marks(client) == {
        "199a1c0d4101": "yes", "199a1c0d4103": "fyi", "199a1c0d4104": None,
        "199a1c0d4105": "fyi", "199a1c0d4106": "fyi", "199a1c0d4107": "yes",
    }
    assert _junk(client) == ["199a1c0d4105"]
    assert client.post("/inherent/mail/archive", json={"ids": ["199a1c0d4105"]}).status_code == 200
