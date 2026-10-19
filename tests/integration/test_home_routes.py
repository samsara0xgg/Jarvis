"""ADR 0055, 0299 — the companion home's routes over replayed Microsoft and Gmail answers.

The To Do list and task are the services' answers of 2026-09-25 (ids shortened); the weather
is a faked WeatherKit (ADR 0285); the mail is in the JSON text blocks the Google Workspace
server's gmail_search and gmail_get answer. Today's events and to-dos are his things (ADR 0299):
a real event log written through ``things`` and filled from To Do by the real mirror. Each check
asserts what a route serves (what the home renders), what Jarvis sent to Microsoft or Gmail, or
what the ledger holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.execution.tools import ToolError
from jarvis.runtime.dashboard import MailDrafts, ViewState
from jarvis.runtime.home import Home
from jarvis.runtime.inherent_loop import _serve_today, _set_todo
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.state import things
from jarvis.state.daily_report import save_report
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.weatherkit_world import NOW, Net, kit_for, make_key

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from jarvis.runtime.weatherkit import WeatherKit

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
# gmail_get's metadata answers; gmail_search lists them in this order, which is not
# newest first, so the route's own sort by Date shows.
LETTERS: list[dict[str, Any]] = [
    {
        "id": "199a1c0d4101", "threadId": "199a1c0d4101", "labelIds": ["UNREAD", "INBOX"],
        "subject": "Office hours move to Thursday", "from": '"Prof. Lee" <lee@uvic.ca>',
        "to": "allen@example.com", "date": "Thu, 25 Sep 2026 14:40:00 -0700",
        "body": "<div>Hi Allen,<br>Thursday 3pm.</div>",
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
# The calendar is empty on the real account: the mirror reads no event, so Today's events are
# the ones a test writes to the ledger.
ANSWERS = {
    "list-todo-task-lists": [LIST],
    "list-todo-tasks": TASKS,
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
        letter = next((one for one in LETTERS if one["id"] == args["messageId"]), None)
        if letter is None:
            return {"text": json.dumps({"error": "Requested entity was not found."})}
        body = letter.get("body", "")
        return {"text": json.dumps({**letter, "snippet": "", "body": body, "attachments": []})}


class _Connections:
    def __init__(self, server: _Microsoft | None, gmail: _Gmail | None = None) -> None:
        self.servers = {"microsoft": server, "gmail": gmail}

    def client_for(self, server: str) -> _Microsoft | _Gmail:
        found = self.servers.get(server)
        if found is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return found


def _flag(call: Any, name: str) -> Any:  # noqa: ANN401 — a route's (ids, flag) body.
    """The routes' (ids, do) callable over a Home method whose keyword says the opposite."""

    async def run(ids: list[str], do: bool) -> None:  # noqa: FBT001
        await asyncio.to_thread(functools.partial(call, ids, **{name: not do}))

    return run


LOCAL = ZoneInfo(ZONE)
WROTE = datetime(2026, 10, 14, 9, 0, tzinfo=LOCAL)  # a test's things are written from here
TODO = f"{LIST['id']}|AQMkADAw-repack="  # the mirrored to-do with no time
TODO_DUE = f"{LIST['id']}|AQMkADAw-a3="  # the mirrored to-do due 2026-09-26


class Ledger(NamedTuple):
    """His things in a real event log, and the real mirror that reads the fake To Do into it."""

    path: Path
    conn: sqlite3.Connection
    server: _Microsoft
    mirror: ThingsMirror


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    """An empty event log for his things, with the mirror between it and a fake Microsoft."""
    path = tmp_path / "events.db"
    conn = open_event_log(path)
    server = _Microsoft()
    mirror = ThingsMirror(_Connections(server), path, LOCAL)  # type: ignore[arg-type]
    mirror.now = lambda: (WROTE + timedelta(hours=1)).astimezone(UTC)  # after every _put
    yield Ledger(path, conn, server, mirror)
    conn.close()


def _client(
    home: Home, conn: sqlite3.Connection | None = None, view: ViewState | None = None,
    *, ledger: Ledger | None = None,
) -> TestClient:
    """The routes wired the way the daemon wires them (runtime/inherent_loop.py).

    Today's two read and tick his things, so they are there only with a ``ledger``.
    """

    async def archive(ids: list[str], archive: bool) -> None:  # noqa: FBT001 — the route's body.
        await asyncio.to_thread(functools.partial(home.archive, ids, undo=not archive))

    today: dict[str, Any] = {} if ledger is None else {
        "today_read": functools.partial(_serve_today, ledger.path, LOCAL, home.weather),
        "todo_set": functools.partial(_set_todo, home, ledger.mirror, ledger.path, LOCAL),
    }
    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        mail_read=functools.partial(asyncio.to_thread, home.mail),
        mail_archive=archive,
        brief_read=None if conn is None else functools.partial(home.brief, conn),
        **today,
        # ADR 0148: the mail page's routes, wired as inherent_loop wires them.
        **({} if view is None else {
            "mail_letter": functools.partial(asyncio.to_thread, home.letter),
            "mail_mark_read": _flag(home.mark_read, "unread"),
            "mail_trash": _flag(home.trash, "undo"),
            "view_set": view.set,
        }),
    )))


def _weather_answers(
    monkeypatch: pytest.MonkeyPatch, *, reachable: bool = True,
) -> Net:
    return Net(monkeypatch, down=() if reachable else ("weatherkit.apple.com",))


def _home(
    server: _Microsoft | None, gmail: _Gmail | None = None, kit: WeatherKit | None = None,
) -> Home:
    return Home(
        _Connections(server, gmail),  # type: ignore[arg-type]
        (ZONE, LOCAL),
        {"latitude": 49.28, "longitude": -123.12, "country_code": "CA"},
        weatherkit=kit,
    )


def _day() -> date:
    """Today on his clock: the day the route reads."""
    return datetime.now(LOCAL).date()


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, clock(hour, minute), LOCAL)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _put(  # noqa: PLR0913 — a thing's words, and its one kind of time.
    ledger: Ledger, number: int, title: str, kind: str = "go", *, start_at: str = "",
    end_at: str = "", on_day: str = "", last_day: str = "", due: str = "",
) -> str:
    """One thing of his, written as her tool writes it; ``number`` makes its id and its minute."""
    thing_id = f"thing-{number:032x}"
    held = things.Thing(
        thing_id, title, kind, start_at=start_at, end_at=end_at, on_day=on_day,
        last_day=last_day, due=due,
    )
    things.add(
        ledger.conn, held, by="her", evidence="said", zone=LOCAL,
        now=WROTE + timedelta(minutes=number),
    )
    return thing_id


def _close(ledger: Ledger, thing_id: str, state: str = "done") -> None:
    things.close(
        ledger.conn, thing_id, state, by="her", evidence="said", zone=LOCAL,
        now=WROTE + timedelta(minutes=30),
    )


def _thing_events(ledger: Ledger) -> list[tuple[str, str]]:
    """Every write to a thing as (event type, actor), oldest first."""
    with contextlib.closing(sqlite3.connect(ledger.path)) as conn:
        return conn.execute(
            "SELECT type, actor FROM events WHERE type LIKE 'thing.%' ORDER BY id",
        ).fetchall()


def _todo_ids(client: TestClient) -> list[str]:
    return [one["id"] for one in client.get("/inherent/today").json()["todos"]]


def test_today_serves_his_events_and_to_dos_from_the_ledger_and_the_weather(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ledger: Ledger,
) -> None:
    """GET /inherent/today: his things on the local day, those with no hour, and 3-hourly sky."""
    day = _day()
    yesterday, tomorrow = day - timedelta(days=1), day + timedelta(days=1)
    put = functools.partial(_put, ledger)
    # On the card, soonest first: one that began last night, an all-day one, a timed one with no
    # end (an hour) and one with an end.
    night = put(1, "Night shift", start_at=_iso(_at(yesterday, 23)), end_at=_iso(_at(day, 0, 30)))
    reading = put(2, "Reading week", on_day=day.isoformat())
    call = put(3, "Call with Mom", start_at=_iso(_at(day, 8, 30)))
    dentist = put(4, "Dentist", start_at=_iso(_at(day, 15)), end_at=_iso(_at(day, 16)))
    # Left out: tomorrow's, one that starts as tomorrow does, one that ended as today began, one
    # done and one cancelled.
    put(5, "Lunch with Sam", start_at=_iso(_at(tomorrow, 12)))
    put(6, "Midnight start", start_at=_iso(_at(tomorrow, 0)))
    put(7, "Late class", start_at=_iso(_at(yesterday, 22)), end_at=_iso(_at(day, 0)))
    _close(ledger, put(8, "Standup", start_at=_iso(_at(day, 10))))
    _close(ledger, put(9, "Cancelled meeting", start_at=_iso(_at(day, 11))), "cancelled")
    # The to-dos: no time, a date, an instant (kept in UTC, shown in his zone), one done, and
    # the two the mirror makes from To Do (its completed task is not made).
    buy = put(10, "Buy milk", "do")
    rent = put(11, "Pay rent", "do", due=(day + timedelta(days=3)).isoformat())
    form = put(12, "Send form", "do", due=_at(tomorrow, 20).astimezone(UTC).isoformat())
    _close(ledger, put(13, "Old chore", "do"))
    assert ledger.mirror.sync() == 2
    ledger.server.calls.clear()

    net = _weather_answers(monkeypatch)
    kit = kit_for(make_key(tmp_path)[0])
    reply = _client(_home(ledger.server, kit=kit), ledger=ledger).get("/inherent/today")
    assert reply.status_code == 200
    body = reply.json()
    assert set(body) == {"events", "todos", "weather"}
    assert body["events"] == [
        {
            "id": night, "title": "Night shift",
            "start": _iso(_at(yesterday, 23)), "end": _iso(_at(day, 0, 30)),
        },
        {"id": reading, "title": "Reading week", "start": _iso(_at(day, 0)), "all_day": True},
        {
            "id": call, "title": "Call with Mom",
            "start": _iso(_at(day, 8, 30)), "end": _iso(_at(day, 9, 30)),
        },
        {
            "id": dentist, "title": "Dentist",
            "start": _iso(_at(day, 15)), "end": _iso(_at(day, 16)),
        },
    ]
    # In the ledger's order: the ones with a date or an instant, soonest first, then the ones with
    # none, oldest first. A to-do made from To Do keeps To Do's id, so a tick on it reaches To Do.
    assert body["todos"] == [
        {"id": TODO_DUE, "title": "CSC370 A3", "due": "2026-09-26T23:59:00-07:00"},
        {"id": form, "title": "Send form", "due": _iso(_at(tomorrow, 20))},
        {"id": rent, "title": "Pay rent", "due": _iso(_at(day + timedelta(days=3), 23, 59))},
        {"id": buy, "title": "Buy milk"},
        {"id": TODO, "title": "repack jarvis"},
    ]
    first = NOW.replace(minute=0, second=0).astimezone(UTC)
    assert body["weather"] == {
        "now_c": 11.0,
        "high_c": 15.0,
        "hours": [
            {
                "at": (first + timedelta(hours=3 * n)).isoformat(),
                "kind": "cloud" if n == 0 else "rain",
                "temp_c": round(
                    10.0 + (first + timedelta(hours=3 * n)).astimezone(LOCAL).hour / 4,
                    1,
                ),
            }
            for n in range(4)
        ],
    }
    # One request serves the home; the tool's default request for the same place shares it.
    assert net.hosts() == ["weatherkit.apple.com"]
    assert "/49.28/-123.12" in net.asked[0]
    assert "countryCode=CA" in net.asked[0]
    # The card is the ledger's: Microsoft is not asked.
    assert ledger.server.calls == []


@pytest.mark.parametrize(
    ("first", "last", "shown"),
    [(0, 0, True), (-1, 1, True), (0, 2, True), (-3, 0, True), (-3, -1, False), (1, 3, False)],
)
def test_an_all_day_thing_over_several_days_is_on_each_day_from_that_days_midnight(
    ledger: Ledger, first: int, last: int, shown: bool,  # noqa: FBT001 — a table row.
) -> None:
    """On any of its days the card starts it at that day's midnight, not where it began."""
    day = _day()
    begin, end = day + timedelta(days=first), day + timedelta(days=last)
    conference = _put(
        ledger, 1, "Conference", on_day=begin.isoformat(),
        last_day=end.isoformat() if last > first else "",
    )
    body = _client(_home(None), ledger=ledger).get("/inherent/today").json()
    assert body["events"] == (
        [{"id": conference, "title": "Conference", "start": _iso(_at(day, 0)), "all_day": True}]
        if shown else []
    )


def test_a_thing_a_mail_made_is_on_the_card_only_once_he_confirms_it(ledger: Ledger) -> None:
    """The card cannot mark one as his to confirm, so an unconfirmed thing is not on it."""
    day = _day()
    made = [
        things.Thing(
            f"thing-{number:032x}", title, kind, start_at=start_at, due=due, source="mail",
            confirmed=False,
        )
        for number, title, kind, start_at, due in (
            (1, "Visa interview", "go", _iso(_at(day, 15)), ""),
            (2, "Pay invoice", "do", "", (day + timedelta(days=2)).isoformat()),
        )
    ]
    for one in made:
        things.add(ledger.conn, one, by="mail", evidence="mail", zone=LOCAL, now=WROTE)
    client = _client(_home(None), ledger=ledger)
    body = client.get("/inherent/today").json()
    assert (body["events"], body["todos"]) == ([], [])

    for one in made:
        things.confirm(
            ledger.conn, one.thing_id, by="her", evidence="said", zone=LOCAL, now=WROTE,
        )
    body = client.get("/inherent/today").json()
    assert [one["id"] for one in body["events"]] == [made[0].thing_id]
    assert _todo_ids(client) == [made[1].thing_id]


def test_today_does_not_need_microsoft(ledger: Ledger) -> None:
    """Microsoft not connected: the card still has the ledger's rows, and a To Do tick is a 404."""
    day = _day()
    visit = _put(ledger, 1, "Dentist", start_at=_iso(_at(day, 15)), end_at=_iso(_at(day, 16)))
    assert ledger.mirror.sync() == 2  # while it was connected
    client = _client(_home(None), ledger=ledger)
    reply = client.get("/inherent/today")
    assert reply.status_code == 200
    assert reply.json() == {
        "events": [{
            "id": visit, "title": "Dentist",
            "start": _iso(_at(day, 15)), "end": _iso(_at(day, 16)),
        }],
        "todos": [
            {"id": TODO_DUE, "title": "CSC370 A3", "due": "2026-09-26T23:59:00-07:00"},
            {"id": TODO, "title": "repack jarvis"},
        ],
        "weather": None,
    }
    assert client.post("/inherent/today/todo", json={"id": TODO, "done": True}).status_code == 404
    one = things.by_source(ledger.conn, "todo", TODO)
    assert one is not None
    assert one.state == "open"


def test_weather_failure_keeps_the_ledgers_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ledger: Ledger,
) -> None:
    """WeatherKit down: Today still answers, without weather."""
    _weather_answers(monkeypatch, reachable=False)
    kit = kit_for(make_key(tmp_path)[0])
    _put(ledger, 1, "Dentist", start_at=_iso(_at(_day(), 15)))
    assert ledger.mirror.sync() == 2
    body = _client(_home(ledger.server, kit=kit), ledger=ledger).get("/inherent/today").json()
    assert body["weather"] is None
    assert [e["title"] for e in body["events"]] == ["Dentist"]
    assert [t["title"] for t in body["todos"]] == ["CSC370 A3", "repack jarvis"]


def test_with_no_weatherkit_key_the_home_shows_no_weather_and_asks_nobody(
    monkeypatch: pytest.MonkeyPatch, ledger: Ledger,
) -> None:
    """No key: Today answers without weather and no request is made."""
    net = _weather_answers(monkeypatch)
    body = _client(_home(ledger.server), ledger=ledger).get("/inherent/today").json()
    assert body == {"events": [], "todos": [], "weather": None}
    assert net.asked == []


def test_the_checkbox_on_a_to_do_id_writes_the_task_to_todo_and_the_card_follows(
    ledger: Ledger,
) -> None:
    """One update-todo-task per click, To Do first; the mirrored thing and the card follow."""
    assert ledger.mirror.sync() == 2
    ledger.server.calls.clear()
    client = _client(_home(ledger.server), ledger=ledger)
    assert TODO in _todo_ids(client)
    assert client.post("/inherent/today/todo", json={"id": TODO, "done": True}).status_code == 200
    assert TODO not in _todo_ids(client)
    assert client.post("/inherent/today/todo", json={"id": TODO, "done": False}).status_code == 200
    assert TODO in _todo_ids(client)
    assert ledger.server.calls == [
        ("update-todo-task", {
            "todoTaskListId": LIST["id"], "todoTaskId": "AQMkADAw-repack=",
            "body": {"status": "completed"},
        }),
        ("update-todo-task", {
            "todoTaskListId": LIST["id"], "todoTaskId": "AQMkADAw-repack=",
            "body": {"status": "notStarted"},
        }),
    ]
    one = things.by_source(ledger.conn, "todo", TODO)
    assert one is not None
    assert [(s.what, s.by) for s in one.history] == [
        ("added", "mirror"), ("done", "him"), ("reopened", "him"),
    ]


def test_the_checkbox_on_a_thing_closes_and_reopens_it_in_the_ledger_alone(
    ledger: Ledger,
) -> None:
    """Done closes it and open reopens it, by him; a click that changes nothing writes nothing."""
    buy = _put(ledger, 1, "Buy milk", "do")
    client = _client(_home(ledger.server), ledger=ledger)
    # (done, the state after it, the writes to the ledger so far)
    for done, state, writes in [
        (True, "done", 2), (True, "done", 2), (False, "open", 3), (False, "open", 3),
    ]:
        reply = client.post("/inherent/today/todo", json={"id": buy, "done": done})
        assert reply.status_code == 200
        assert reply.json() == {"ok": True}
        assert things.get(ledger.conn, buy).state == state
        assert len(_thing_events(ledger)) == writes
        assert (buy in _todo_ids(client)) is (state == "open")
    clicked = things.get(ledger.conn, buy).history[1:]
    assert [(s.what, s.by, s.evidence, s.ref) for s in clicked] == [
        ("done", "him", "ticked on the desktop", buy),
        ("reopened", "him", "ticked on the desktop", buy),
    ]
    assert [actor for _type, actor in _thing_events(ledger)[1:]] == ["user", "user"]
    assert ledger.server.calls == []


@pytest.mark.parametrize(
    ("todo_id", "status"),
    [(f"thing-{0xF:032x}", 404), ("no-bar", 400), ("|task", 400), ("list|", 400)],
)
def test_a_tick_on_a_thing_that_is_not_there_is_a_404_and_on_a_bad_id_a_400_and_writes_nothing(
    ledger: Ledger, todo_id: str, status: int,
) -> None:
    """The ledger and Microsoft are left as they were."""
    client = _client(_home(ledger.server), ledger=ledger)
    reply = client.post("/inherent/today/todo", json={"id": todo_id, "done": True})
    assert reply.status_code == status
    assert _thing_events(ledger) == []
    assert ledger.server.calls == []


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
        "id": "199a1c0d4103", "thread_id": "199a1c0d4103", "from": "妈妈",
        "address": "mom@example.com", "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
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
        # subject -> probabilities of the importance choice; unlisted subjects are "normal"
        self.importance = {
            "Office hours move to Thursday": {
                "urgent": 0.5, "important": 0.3, "normal": 0.1, "low": 0.1,
            },
            "50% off everything this weekend": {
                "urgent": 0.0, "important": 0.0, "normal": 0.1, "low": 0.9,
            },
        }
        self.category = "school"
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
                answers: dict[str, Any] = {"reply": {"noul": reply}, "junk": {"noul": junk}}
                if "importance" in request["questions"]:
                    answers["importance"] = {
                        "type": "choice", "choice": "normal", "confidence": 0.5,
                        "probabilities": outer.importance.get(
                            subject, {"urgent": 0.0, "important": 0.0, "normal": 1.0, "low": 0.0},
                        ),
                    }
                    answers["category"] = {
                        "type": "choice", "choice": outer.category, "confidence": 0.9,
                        "probabilities": {outer.category: 0.9},
                    }
                body = json.dumps({"answers": answers, "usage": {"cost": 0.00001}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):  # the client gave up on a slow call
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(
        self, *, timeout_ms: int = 1500, log: JevLog | None = None, importance: bool = False,
    ) -> MailReply:
        route = SurrogateRoute(
            model="typesafe/jev-1.13", min_confidence=1.0, timeout_ms=timeout_ms,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions", log=log,
        )
        return MailReply(route, 0.9, 0.1, 0.9, importance=importance)


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


def test_importance_rides_the_same_request_and_scores_by_expected_value(jev: _Jev) -> None:
    """On: one request per letter also asks importance and category; the payload has the score."""
    client = _with_jev(jev.reply(importance=True))
    letters = {one["id"]: one for one in client.get("/inherent/mail").json()["unread"]}
    assert len(jev.requests) == 6  # still one request per letter
    for one in jev.requests:
        assert set(one["questions"]) == {"reply", "junk", "importance", "category"}
        assert one["questions"]["importance"]["type"] == "choice"
        assert set(one["questions"]["importance"]["criteria"]) == {
            "urgent", "important", "normal", "low",
        }
        assert len(one["questions"]["category"]["criteria"]) == 8
        assert set(one) == {"model", "state", "questions", "provider"}
    assert "@" not in json.dumps(jev.requests)
    # 3*0.5 + 2*0.3 + 0.1 = 2.2; 3*0 + 2*0 + 0.1 = 0.1; "normal" alone is 1.
    assert letters["199a1c0d4101"]["importance"] == pytest.approx(2.2)
    assert letters["199a1c0d4105"]["importance"] == pytest.approx(0.1)
    assert letters["199a1c0d4103"]["importance"] == pytest.approx(1.0)
    assert letters["199a1c0d4101"]["category"] == "school"
    # The reply and junk marks are what they were.
    assert (letters["199a1c0d4101"]["reply"], letters["199a1c0d4105"]["junk"]) == ("yes", True)


def test_importance_off_adds_no_question_and_no_field(jev: _Jev) -> None:
    """Off (the default): today's request and payload, no importance or category."""
    letters = _with_jev(jev.reply()).get("/inherent/mail").json()["unread"]
    assert all("importance" not in one and "category" not in one for one in letters)
    assert all(set(one["questions"]) == {"reply", "junk"} for one in jev.requests)


def test_importance_missing_for_unrated_letters_and_bad_answers(jev: _Jev) -> None:
    """A late or failed letter has no field; an unusable importance answer keeps the marks."""
    jev.category = "not-a-category"
    letters = _with_jev(jev.reply(importance=True)).get("/inherent/mail").json()["unread"]
    assert all("importance" not in one and "category" not in one for one in letters)
    assert {one["id"]: one["reply"] for one in letters}["199a1c0d4101"] == "yes"
    jev.delay_s = 0.6
    slow = _with_jev(jev.reply(timeout_ms=150, importance=True))
    assert all("importance" not in one for one in slow.get("/inherent/mail").json()["unread"])


def test_importance_is_kept_in_the_jev_log(jev: _Jev, tmp_path: Path) -> None:
    """The decision line holds the score, the probabilities and the category (ADR 0128)."""
    path = tmp_path / "decisions.jsonl"
    _with_jev(jev.reply(importance=True, log=JevLog(path))).get("/inherent/mail")
    lines = [json.loads(one) for one in path.read_text().splitlines()]
    (office,) = [
        one for one in lines if one["kind"] == "decision" and one["ref"] == "199a1c0d4101"
    ]
    assert office["importance"]["score"] == pytest.approx(2.2)
    assert office["importance"]["category"] == "school"
    assert office["importance"]["probabilities"]["urgent"] == 0.5


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


def test_without_microsoft_or_gmail_the_home_says_not_connected(ledger: Ledger) -> None:
    """No Microsoft or Gmail: mail and a To Do tick are 404 (not connected); Today answers."""
    client = _client(_home(None), ledger=ledger)
    assert client.get("/inherent/today").json() == {"events": [], "todos": [], "weather": None}
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


def test_the_open_letter_is_read_whole_marked_read_archived_and_trashed() -> None:
    """ADR 0148: GET letter caches by id; taps act only on listed or open ids, each with an undo."""
    gmail = _Gmail()
    view = ViewState()
    home = Home(
        _Connections(_Microsoft(), gmail),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, None, view,
    )
    client = _client(home, view=view)
    listed, unlisted = "199a1c0d4101", "199a1c0d9999"

    got = client.get(f"/inherent/mail/{listed}")
    assert got.json() == {
        "id": listed, "thread_id": listed, "from": "Prof. Lee", "address": "lee@uvic.ca",
        "to": "allen@example.com", "subject": "Office hours move to Thursday",
        "received": "2026-09-25T21:40:00+00:00", "text": "Hi Allen,\nThursday 3pm.",
        "layout": "text",
    }
    assert client.get("/inherent/mail/nope").status_code == 404
    client.get(f"/inherent/mail/{listed}")
    assert [a["messageId"] for t, a in gmail.calls if t == "gmail_get"] == [listed, "nope"]

    def changes() -> list[dict[str, Any]]:
        return [a for t, a in gmail.calls if t == "gmail_batchModify"]

    for route in ("read", "unread", "archive", "unarchive", "trash", "untrash"):  # nothing yet
        assert client.post(f"/inherent/mail/{route}", json={"ids": [listed]}).status_code == 400
    assert changes() == []
    client.get("/inherent/mail")
    assert client.post("/inherent/mail/read", json={"ids": [listed]}).status_code == 200
    assert client.post("/inherent/mail/archive", json={"ids": [listed]}).status_code == 200
    assert client.post("/inherent/mail/unarchive", json={"ids": [listed]}).status_code == 200
    assert client.post("/inherent/mail/unread", json={"ids": [listed]}).status_code == 200
    assert client.post("/inherent/mail/trash", json={"ids": [listed]}).status_code == 200
    assert client.post("/inherent/mail/untrash", json={"ids": [listed]}).status_code == 200
    assert changes() == [
        {"messageIds": [listed], "removeLabelIds": ["UNREAD"]},
        {"messageIds": [listed], "removeLabelIds": ["INBOX"]},
        {"messageIds": [listed], "addLabelIds": ["INBOX"]},
        {"messageIds": [listed], "addLabelIds": ["UNREAD"]},
        {"messageIds": [listed], "addLabelIds": ["TRASH"]},
        {"messageIds": [listed], "removeLabelIds": ["TRASH"], "addLabelIds": ["INBOX"]},
    ]
    # An id the list never showed is refused until it is the open letter.
    assert client.post("/inherent/mail/trash", json={"ids": [unlisted]}).status_code == 400
    opened = {"page": "mail", "item": {"kind": "mail", "id": unlisted, "title": "Sam — Lunch"}}
    assert client.post("/inherent/view", json=opened).status_code == 200
    assert client.post("/inherent/mail/trash", json={"ids": [unlisted]}).status_code == 200
    assert all("DELETE" not in json.dumps(args) for _, args in gmail.calls)


def test_with_the_mail_page_off_its_routes_are_404_and_archive_stays_junk_only() -> None:
    """No ViewState: the page's routes are not registered; a listed non-junk letter is a 400."""
    client = _client(_home(_Microsoft(), _Gmail()))
    client.get("/inherent/mail")
    assert client.get("/inherent/mail/199a1c0d4101").status_code == 404
    for route in ("read", "unread", "trash", "untrash", "../view"):
        reply = client.post(f"/inherent/mail/{route}", json={"ids": ["199a1c0d4101"]})
        assert reply.status_code == 404
    assert client.post("/inherent/mail/archive", json={"ids": ["199a1c0d4101"]}).status_code == 400


def test_the_page_text_keeps_links_as_urls_and_the_models_record_does_not() -> None:
    """ADR 0148: an anchor shows as ``text (url)`` on the page only; the cap is 4000."""
    from jarvis.runtime.home import mail_body  # noqa: PLC0415

    html_body = '<p>Join <a href="https://x.example/a">the call</a> or <a href="https://y.example">https://y.example</a></p>'
    assert mail_body(html_body, links=True) == "Join the call (https://x.example/a) or https://y.example"
    assert mail_body(html_body) == "Join the call or https://y.example"
    assert len(mail_body("x" * 5000, links=True)) == 4001


def _drafts_client(gmail: _Gmail | None = None) -> tuple[TestClient, ViewState, MailDrafts]:
    """The mail page's draft routes wired over the real stores, as inherent_loop wires them."""
    from jarvis.runtime.inherent_loop import _draft_view, _save_draft  # noqa: PLC0415

    view = ViewState()
    drafts = MailDrafts(view)
    home = Home(
        _Connections(_Microsoft(), gmail or _Gmail()),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, None, view,
    )

    async def read(letter_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(_draft_view, home, drafts, letter_id)

    async def save(letter_id: str, subject: str, body: str) -> dict[str, Any]:
        return await asyncio.to_thread(_save_draft, home, drafts, letter_id, subject, body)

    async def discard(letter_id: str) -> None:
        drafts.discard(letter_id)

    app = create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
        view_set=view.set, mail_draft_read=read, mail_draft_save=save, mail_draft_discard=discard,
    ))
    return TestClient(app), view, drafts


def test_the_draft_is_jarvis_then_allens_edit_with_a_revision_each_and_can_be_discarded() -> None:
    """ADR 0148: GET {draft: null} first; Jarvis's body, then his hand edit, bump the revision."""
    client, view, drafts = _drafts_client()
    letter = "199a1c0d4101"
    assert client.get(f"/inherent/mail/{letter}/draft").json() == {"draft": None}
    view.set("mail", item=("mail", letter, "Prof. Lee — Office hours move to Thursday"))
    first = drafts.write(letter, "Thanks, Thursday works.")
    assert client.get(f"/inherent/mail/{letter}/draft").json() == {"draft": {
        "revision": first, "to": "lee@uvic.ca", "subject": "Re: Office hours move to Thursday",
        "body": "Thanks, Thursday works.", "by": "jarvis",
    }}
    saved = client.post(
        f"/inherent/mail/{letter}/draft", json={"subject": "Thursday it is", "body": "Yes, 3pm."},
    ).json()["draft"]
    assert saved == {
        "revision": first + 1, "to": "lee@uvic.ca", "subject": "Thursday it is",
        "body": "Yes, 3pm.", "by": "owner",
    }
    assert drafts.line() == (
        f"Draft reply under it (revision {first + 1}, last edited by Allen): Yes, 3pm."
    )
    drafts.write(letter, "Yes, 3pm works for me.")  # Jarvis revises; the subject Allen set stays
    got = client.get(f"/inherent/mail/{letter}/draft").json()["draft"]
    assert (got["by"], got["subject"], got["revision"]) == ("jarvis", "Thursday it is", first + 2)
    drafts.discard(letter)
    assert client.get(f"/inherent/mail/{letter}/draft").json() == {"draft": None}
    assert drafts.line() is None
