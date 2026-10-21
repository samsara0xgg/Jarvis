"""ADR 0299: Outlook events and To Do tasks are mirrored into the ledger of his things.

Data-driven checks over the real mirror and a real event log, with a fake ``microsoft`` server that
answers like ms-365-mcp-server (Graph's JSON in a text block, calendarView in UTC, To Do dates as
midnight timestamps). No key, no network. The examples are invented.

- new: an event is a ``go`` thing in his zone, a task a ``do`` due on its day, both written by the
  mirror and without rings; a second pass writes nothing;
- moved and renamed: one thing moved or changed, never a second;
- deleted: an event gone from the window (or cancelled) and a task gone from To Do are cancelled; an
  event the window moved past is not;
- all-day: an event with no hour is a day, and moves as a day;
- a task done is done in the ledger, one done before it was seen is never made, one opened again
  reopens what the mirror closed and leaves what he closed;
- Jarvis's own event (an interview's ``outlook_id``) is not read in;
- the connection down or a read failing writes nothing and says so once;
- the loop syncs at once and survives a failed pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.tools import ToolError
from jarvis.runtime import things_mirror
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.shared import lang
from jarvis.state import reminders as reminder_state
from jarvis.state import things
from tests.support.runtime_log import RuntimeLog

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

ZONE = ZoneInfo("America/Los_Angeles")  # UTC-7 until Nov 1 2026
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=ZONE)
LIST = "AQMkADAw-list="
_GRAPH = "%Y-%m-%dT%H:%M:%S.0000000"


def _utc(local: str) -> str:
    """A local ``YYYY-MM-DD HH:MM`` as Graph's UTC ``dateTime``."""
    at = datetime.fromisoformat(local).replace(tzinfo=ZONE)
    return at.astimezone(UTC).strftime(_GRAPH)


def _event(
    ref: str, subject: str, start: str, end: str, *, place: str = "",
) -> dict[str, Any]:
    """A timed calendarView event; ``start`` and ``end`` are local times."""
    return {
        "id": ref, "subject": subject, "isAllDay": False, "isCancelled": False,
        "location": {"displayName": place},
        "start": {"dateTime": _utc(start), "timeZone": "UTC"},
        "end": {"dateTime": _utc(end), "timeZone": "UTC"},
    }


def _day(ref: str, subject: str, day: str) -> dict[str, Any]:
    """An all-day event: Graph says its day as midnight UTC of that day and the next."""
    first = datetime.fromisoformat(day)
    return {
        "id": ref, "subject": subject, "isAllDay": True, "isCancelled": False,
        "start": {"dateTime": first.strftime(_GRAPH), "timeZone": "UTC"},
        "end": {"dateTime": (first + timedelta(days=1)).strftime(_GRAPH), "timeZone": "UTC"},
    }


def _task(ref: str, title: str, due: str = "", *, done: bool = False) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": ref, "title": title, "status": "completed" if done else "notStarted",
    }
    if due:
        task["dueDateTime"] = {"dateTime": f"{due}T00:00:00.0000000", "timeZone": "UTC"}
    return task


class _Microsoft:
    """The connected server: reads only, and one tool can be made to fail."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.broken = ""

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, dict(args)))
        answers = {
            "get-calendar-view": self.events,
            "list-todo-task-lists": [{"id": LIST, "displayName": "Tasks"}],
            "list-todo-tasks": self.tasks,
        }
        assert tool in answers, f"the mirror may only read: {tool}"
        if tool == self.broken:
            msg = "microsoft: Graph is down"
            raise ToolError(msg, code="mcp_server")
        return {"text": json.dumps({"value": answers[tool]})}


class _Connections:
    def __init__(self, server: _Microsoft | None) -> None:
        self.server = server

    def client_for(self, server: str) -> _Microsoft:
        assert server == "microsoft"
        if self.server is None:
            msg = "mcp server 'microsoft' is not connected"
            raise ToolError(msg, code="mcp_server")
        return self.server


class _World:
    """The mirror over a real log, with its fake server."""

    def __init__(self, log: RuntimeLog, microsoft: _Microsoft | None) -> None:
        self.log, self.microsoft = log, microsoft
        self.mirror = ThingsMirror(_Connections(microsoft), log.paths.event_log, ZONE)  # type: ignore[arg-type]
        self.mirror.now = lambda: NOW.astimezone(UTC)

    def sync(self) -> int:
        return self.mirror.sync()

    def one(self, source: str, ref: str) -> things.Thing:
        found = things.by_source(self.log.conn, source, ref)
        assert found is not None
        return found

    def steps(self, source: str, ref: str) -> list[tuple[str, str]]:
        return [(s.what, s.by) for s in self.one(source, ref).history]


@pytest.fixture
def log(tmp_path: Path) -> Iterator[RuntimeLog]:
    """A runtime root with a real event log; the language is English."""
    before = lang.language()
    lang.set_language("en")
    one = RuntimeLog(tmp_path)
    yield one
    one.conn.close()
    lang.set_language(before)


@pytest.fixture
def world(log: RuntimeLog) -> _World:
    """The mirror and its fake server over the log."""
    return _World(log, _Microsoft())


def _server(world: _World) -> _Microsoft:
    assert world.microsoft is not None
    return world.microsoft


def test_an_event_and_a_task_are_things_made_by_the_mirror_and_not_made_twice(
    world: _World,
) -> None:
    """A ``go`` in his zone with its place, a ``do`` due on its day; confirmed, no rings."""
    server = _server(world)
    server.events = [
        _event("ev1", "Dentist", "2026-10-20 19:00", "2026-10-20 20:30", place="Clinic 5"),
    ]
    server.tasks = [_task("t1", "Repack jarvis", due="2026-10-16")]

    assert world.sync() == 2

    event = world.one("outlook", "ev1")
    assert (event.title, event.kind, event.place) == ("Dentist", "go", "Clinic 5")
    assert (event.start_at, event.end_at) == (
        "2026-10-20T19:00:00-07:00", "2026-10-20T20:30:00-07:00",
    )
    assert (event.state, event.confirmed, event.rings) == ("open", True, ())
    task = world.one("todo", f"{LIST}|t1")
    assert (task.title, task.kind, task.due, task.start_at) == (
        "Repack jarvis", "do", "2026-10-16", "",
    )
    assert (task.state, task.confirmed, task.rings) == ("open", True, ())
    assert world.steps("outlook", "ev1") == [("added", "mirror")]
    assert world.steps("todo", f"{LIST}|t1") == [("added", "mirror")]
    assert {tool for tool, _ in server.calls} == {
        "get-calendar-view", "list-todo-task-lists", "list-todo-tasks",
    }

    assert world.sync() == 0
    assert len(things.fold(world.log.conn)) == 2
    assert reminder_state.fold(world.log.conn) == {}  # a mirrored thing rings nothing


def test_the_window_is_yesterday_to_sixty_days_ahead_in_his_zone(world: _World) -> None:
    """The calendar is asked for ``[yesterday 00:00, today + 60 days)`` with every page."""
    world.sync()
    view = next(args for tool, args in _server(world).calls if tool == "get-calendar-view")
    assert view["startDateTime"] == "2026-10-13T00:00:00-07:00"
    assert view["endDateTime"] == "2026-12-13T00:00:00-08:00"
    assert view["fetchAllPages"] is True


def test_a_moved_or_renamed_event_is_the_same_thing_moved_or_changed(world: _World) -> None:
    """A new time is a move that remembers the old; a new subject or place is a change."""
    server = _server(world)
    server.events = [_event("ev1", "Dentist", "2026-10-20 19:00", "2026-10-20 20:00")]
    world.sync()
    thing_id = world.one("outlook", "ev1").thing_id

    server.events = [_event("ev1", "Dentist", "2026-10-21 10:00", "2026-10-21 11:00")]
    assert world.sync() == 1
    moved = world.one("outlook", "ev1")
    assert moved.thing_id == thing_id
    assert (moved.start_at, moved.end_at) == (
        "2026-10-21T10:00:00-07:00", "2026-10-21T11:00:00-07:00",
    )
    assert [(m.start_at, m.by) for m in moved.moves] == [("2026-10-20T19:00:00-07:00", "mirror")]

    server.events = [
        _event("ev1", "Dentist, checkup", "2026-10-21 10:00", "2026-10-21 11:00", place="Clinic 5"),
    ]
    assert world.sync() == 1
    renamed = world.one("outlook", "ev1")
    assert (renamed.title, renamed.place) == ("Dentist, checkup", "Clinic 5")
    assert world.steps("outlook", "ev1") == [
        ("added", "mirror"), ("moved", "mirror"), ("changed", "mirror"),
    ]
    assert world.sync() == 0
    assert len(things.fold(world.log.conn)) == 1


def test_an_all_day_event_is_a_day_and_moves_as_a_day(world: _World) -> None:
    """No hour: ``on_day`` is the date Graph gives, whatever zone it is read in."""
    server = _server(world)
    server.events = [_day("ev-day", "Reading week", "2026-10-26")]
    assert world.sync() == 1
    one = world.one("outlook", "ev-day")
    assert (one.on_day, one.start_at, one.end_at) == ("2026-10-26", "", "")

    server.events = [_day("ev-day", "Reading week", "2026-10-27")]
    assert world.sync() == 1
    one = world.one("outlook", "ev-day")
    assert one.on_day == "2026-10-27"
    assert [m.on_day for m in one.moves] == ["2026-10-26"]

    longer = _day("ev-day", "Reading week", "2026-10-27")
    longer["end"]["dateTime"] = "2026-10-31T00:00:00.0000000"
    server.events = [longer]
    assert world.sync() == 1
    one = world.one("outlook", "ev-day")
    assert (one.on_day, one.last_day) == ("2026-10-27", "2026-10-30")
    assert [(m.on_day, m.last_day) for m in one.moves] == [("2026-10-26", ""), ("2026-10-27", "")]
    noon = datetime(2026, 10, 29, 12, tzinfo=ZONE)
    days = things.occurrences(
        world.log.conn, int(noon.timestamp() * 1000),
        int((noon + timedelta(days=2)).timestamp() * 1000), zone=ZONE,
    )
    assert [(o.day, o.all_day) for o in days] == [("2026-10-27", True)]
    assert world.sync() == 0


def test_an_event_with_no_subject_is_untitled_and_one_that_cannot_be_read_waits(
    world: _World,
) -> None:
    """One bad event does not stop the others, and is not cancelled for being unread."""
    server = _server(world)
    broken = _event("ev-bad", "Broken", "2026-10-20 09:00", "2026-10-20 10:00")
    del broken["start"]
    good = _event("ev-none", "", "2026-10-20 12:00", "2026-10-20 13:00")
    server.events = [good, broken]

    assert world.sync() == 1

    assert world.one("outlook", "ev-none").title == "(untitled)"
    assert things.by_source(world.log.conn, "outlook", "ev-bad") is None
    # one made earlier that cannot be read now is left as it is, not taken for deleted
    server.events = [{**good, "start": None}, broken]
    assert world.sync() == 0
    assert world.one("outlook", "ev-none").state == "open"


def test_an_event_deleted_or_cancelled_in_outlook_is_cancelled_in_the_window_only(
    world: _World,
) -> None:
    """Gone from the answer or ``isCancelled``: cancelled; the window moving past one is not."""
    server = _server(world)
    server.events = [
        _event("gone", "Deleted", "2026-10-20 09:00", "2026-10-20 10:00"),
        _event("off", "Called off", "2026-10-21 09:00", "2026-10-21 10:00"),
        _event("kept", "Kept", "2026-10-22 09:00", "2026-10-22 10:00"),
    ]
    assert world.sync() == 3

    off = _event("off", "Called off", "2026-10-21 09:00", "2026-10-21 10:00")
    server.events = [
        {**off, "isCancelled": True},
        _event("kept", "Kept", "2026-10-22 09:00", "2026-10-22 10:00"),
    ]
    assert world.sync() == 2

    for ref in ("gone", "off"):
        one = world.one("outlook", ref)
        assert (one.state, one.history[-1].by, one.history[-1].why) == (
            "cancelled", "mirror", "gone from Outlook",
        )
    assert world.one("outlook", "kept").state == "open"
    assert world.sync() == 0

    # a month on, the window no longer reaches Oct 22: absent from the answer, it stays as it was
    server.events = []
    world.mirror.now = lambda: (NOW + timedelta(days=30)).astimezone(UTC)
    assert world.sync() == 0
    assert world.one("outlook", "kept").state == "open"


def test_a_task_gone_from_todo_is_cancelled(world: _World) -> None:
    """To Do has no window: a task that is not in any list is gone."""
    server = _server(world)
    server.tasks = [_task("t1", "Repack jarvis"), _task("t2", "Call bank", due="2026-10-16")]
    assert world.sync() == 2

    server.tasks = [_task("t2", "Call bank", due="2026-10-16")]
    assert world.sync() == 1

    one = world.one("todo", f"{LIST}|t1")
    assert (one.state, one.history[-1].by, one.history[-1].why) == (
        "cancelled", "mirror", "gone from To Do",
    )
    assert world.one("todo", f"{LIST}|t2").state == "open"


def test_a_completed_task_is_done_and_one_done_before_it_was_seen_is_never_made(
    world: _World,
) -> None:
    """``completed`` closes the thing as done; a task already done when first read makes nothing."""
    server = _server(world)
    server.tasks = [_task("t1", "Repack jarvis"), _task("t2", "Old one", done=True)]
    assert world.sync() == 1
    assert things.by_source(world.log.conn, "todo", f"{LIST}|t2") is None

    server.tasks = [_task("t1", "Repack jarvis", done=True), _task("t2", "Old one", done=True)]
    assert world.sync() == 1

    one = world.one("todo", f"{LIST}|t1")
    assert (one.state, one.history[-1].by) == ("done", "mirror")
    assert world.sync() == 0


def test_a_task_opened_again_reopens_what_the_mirror_closed_and_not_what_he_closed(
    world: _World,
) -> None:
    """Only the mirror's own close is undone by To Do; his or hers stays."""
    server = _server(world)
    server.tasks = [_task("t1", "Repack jarvis"), _task("t2", "Call bank")]
    world.sync()
    server.tasks = [_task("t1", "Repack jarvis", done=True), _task("t2", "Call bank")]
    world.sync()
    things.close(
        world.log.conn, world.one("todo", f"{LIST}|t2").thing_id, "cancelled", why="not needed",
        by="him", evidence="said so", zone=ZONE, now=NOW,
    )

    server.tasks = [_task("t1", "Repack jarvis"), _task("t2", "Call bank")]
    assert world.sync() == 1

    assert world.steps("todo", f"{LIST}|t1") == [
        ("added", "mirror"), ("done", "mirror"), ("reopened", "mirror"),
    ]
    assert world.one("todo", f"{LIST}|t1").state == "open"
    assert (world.one("todo", f"{LIST}|t2").state, world.steps("todo", f"{LIST}|t2")[-1]) == (
        "cancelled", ("cancelled", "him"),
    )
    assert world.sync() == 0


def test_a_task_renamed_or_given_a_new_due_day_is_changed_or_moved(world: _World) -> None:
    """A task has a title and a due day; both follow To Do."""
    server = _server(world)
    server.tasks = [_task("t1", "Repack jarvis", due="2026-10-16")]
    world.sync()

    server.tasks = [_task("t1", "Repack jarvis and ship", due="2026-10-18")]
    assert world.sync() == 2

    one = world.one("todo", f"{LIST}|t1")
    assert (one.title, one.due) == ("Repack jarvis and ship", "2026-10-18")
    assert [(m.due, m.by) for m in one.moves] == [("2026-10-16", "mirror")]


def test_jarvis_own_event_is_not_read_in_and_one_read_before_it_was_owned_is_cancelled(
    world: _World,
) -> None:
    """An event whose id is a thing's ``outlook_id`` is Jarvis's: ADR 0186's, not a second thing."""
    server = _server(world)
    interview = things.Thing(
        things.new_id(), "Interview: Acme", "go", start_at="2026-10-20T13:00:00-07:00",
        end_at="2026-10-20T14:00:00-07:00", outlook_id="ev-own", source="mail",
        source_ref="job:acme",
    )
    things.add(world.log.conn, interview, by="mail", evidence="invited", zone=ZONE, now=NOW)
    server.events = [_event("ev-own", "Interview: Acme", "2026-10-20 13:00", "2026-10-20 14:00")]

    assert world.sync() == 0
    assert things.by_source(world.log.conn, "outlook", "ev-own") is None

    # an event read before the thing that owns it was written: the thing wins, the copy is cancelled
    server.events = [
        _event("ev-late", "Interview Beta (Teams)", "2026-10-21 13:00", "2026-10-21 14:00"),
    ]
    assert world.sync() == 1
    assert world.one("outlook", "ev-late").state == "open"
    beta = things.Thing(
        things.new_id(), "Interview: Beta", "go", start_at="2026-10-21T13:00:00-07:00",
        source="mail", source_ref="job:beta",
    )
    added, _ = things.add(world.log.conn, beta, by="mail", evidence="invited", zone=ZONE, now=NOW)
    things.change(
        world.log.conn, added.thing_id, {"outlook_id": "ev-late"}, by="mail", evidence="carried",
        zone=ZONE, now=NOW,
    )

    assert world.sync() == 1

    copy = world.one("outlook", "ev-late")
    why = "Jarvis's own event for another thing"
    assert (copy.state, copy.history[-1].why) == ("cancelled", why)
    assert world.one("mail", "job:beta").state == "open"
    assert world.sync() == 0


def test_with_no_connection_or_a_failed_read_nothing_is_written_and_it_is_said_once(
    log: RuntimeLog, caplog: pytest.LogCaptureFixture,
) -> None:
    """Everything is read before anything is written; a read that fails leaves the ledger be."""
    caplog.set_level(logging.WARNING, logger=things_mirror.__name__)
    bare = _World(log, None)
    assert (bare.sync(), bare.sync()) == (0, 0)
    assert len([r for r in caplog.records if "not connected" in r.getMessage()]) == 1

    server = _Microsoft()
    server.events = [_event("ev1", "Dentist", "2026-10-20 19:00", "2026-10-20 20:00")]
    server.tasks = [_task("t1", "Repack jarvis")]
    server.broken = "list-todo-tasks"
    world = _World(log, server)
    assert (world.sync(), world.sync()) == (0, 0)
    assert things.fold(log.conn) == {}  # not even the event, which was read before the tasks failed
    assert len([r for r in caplog.records if "could not be read" in r.getMessage()]) == 1

    server.broken = ""
    assert world.sync() == 2
    server.broken = "get-calendar-view"
    assert world.sync() == 0  # a failed read cancels nothing
    assert [one.state for one in things.fold(log.conn).values()] == ["open", "open"]


def test_the_loop_syncs_at_once_and_a_failed_pass_does_not_end_it(
    world: _World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first pass raises; the loop logs it and the next one mirrors."""
    monkeypatch.setattr(things_mirror, "PASS_S", 0.05)
    _server(world).tasks = [_task("t1", "Repack jarvis")]
    real, passes = world.mirror.sync, []

    def flaky() -> int:
        passes.append(1)
        if len(passes) == 1:
            msg = "the log is busy"
            raise RuntimeError(msg)
        return real()

    monkeypatch.setattr(world.mirror, "sync", flaky)

    async def run() -> None:
        task = asyncio.create_task(world.mirror.run())
        for _ in range(100):
            if things.by_source(world.log.conn, "todo", f"{LIST}|t1") is not None:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(passes) >= 2
    assert world.one("todo", f"{LIST}|t1").state == "open"
