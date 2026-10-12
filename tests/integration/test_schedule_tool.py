"""ADR 0248: what is on for him is one read-only tool, ``my_schedule``.

A scripted Microsoft server answers like ms-365-mcp-server does (Graph's JSON in a text block,
calendarView by overlap with the bounds it was sent, in UTC) behind the real ``Home``, and the
tool runs through ``ToolRegistry.dispatch`` onto a real event log under a fixed clock (Monday
2026-10-12, 12:40 at UTC-7). Each check is a request and the exact words or fields
the model reads back, or the bounds the server was asked for. No network, no key, no model.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.commentary import _dispatch_key
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.schedule_tool import build_schedule_tool
from jarvis.execution.tools import ActionLifecycle, Tool, ToolContext, ToolError, ToolRegistry
from jarvis.runtime import bootstrap_runtime_app
from jarvis.runtime.home import Home
from jarvis.shared import ActionRequest, CallerPrincipal, steps
from jarvis.state import departures, reminders
from jarvis.state.event_log import open_event_log
from scripts import tool_exam, tool_exam_steps
from tests.integration.test_home_routes import _Connections

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Mapping, Sequence

    type Maker = Callable[..., _World]

REPO = Path(__file__).resolve().parents[2]
ZONE = "America/Los_Angeles"
"""UTC-7 in October like Vancouver, with clock changes on Nov 1 2026 and Mar 14 2027 in every
tzdata release (Vancouver's are being dropped from it)."""
LOCAL = ZoneInfo(ZONE)
NOW = datetime(2026, 10, 12, 12, 40, tzinfo=LOCAL)  # Monday
FIELDS = "id,subject,start,end,isAllDay,isCancelled,location,isOnlineMeeting,webLink"
LINK = "https://outlook.example/calendar/item/interview"


def _stamp(at: datetime) -> dict[str, str]:
    return {
        "dateTime": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC",
    }


def _at(day: int, hour: int, minute: int = 0, month: int = 10, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LOCAL)


def _timed(
    uid: str, subject: str, start: datetime, end: datetime, **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    return {
        "id": uid, "subject": subject, "isAllDay": False, "isCancelled": False,
        "start": _stamp(start), "end": _stamp(end), **extra,
    }


def _all_day(uid: str, subject: str, first: date, last: date) -> dict[str, Any]:
    """Graph's all-day event: midnight to midnight, labelled UTC; ``end`` is the day after."""
    midnight = {"timeZone": "UTC"}
    return {
        "id": uid, "subject": subject, "isAllDay": True, "isCancelled": False,
        "start": {"dateTime": f"{first.isoformat()}T00:00:00.0000000", **midnight},
        "end": {"dateTime": f"{last + timedelta(days=1)}T00:00:00.0000000", **midnight},
    }


def _task(title: str, due: str | None, status: str = "notStarted") -> dict[str, Any]:
    task: dict[str, Any] = {"id": f"t-{title}", "title": title, "status": status}
    if due:
        task["dueDateTime"] = {"dateTime": f"{due}T00:00:00.0000000", "timeZone": "UTC"}
    return task


class _Microsoft:
    """The ``microsoft`` server: calendarView by overlap with the bounds, To Do lists and tasks.

    Only the three reads ``my_schedule`` may use are answered. All-day events overlap by their
    UTC midnights, as Graph answers them, so a window that ends at local midnight can still
    return the next day's all-day event. ``fail`` makes a tool raise as the MCP client does.
    """

    def __init__(
        self, events: Sequence[dict[str, Any]] = (), tasks: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.events = list(events)
        self.tasks = list(tasks)
        self.fail: dict[str, Exception] = {}
        self.raw: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        assert tool in {"get-calendar-view", "list-todo-task-lists", "list-todo-tasks"}, tool
        self.calls.append((tool, dict(args)))
        assert args["fetchAllPages"] is True  # the server follows Graph's pages
        if tool in self.fail:
            raise self.fail[tool]
        if tool in self.raw:
            return {"text": self.raw[tool]}
        if tool == "list-todo-task-lists":
            return {"text": json.dumps({"value": [{"id": "L1", "displayName": "Tasks"}]})}
        if tool == "list-todo-tasks":
            assert args["todoTaskListId"] == "L1"
            return {"text": json.dumps({"value": self.tasks})}
        assert args["select"] == FIELDS
        lo = datetime.fromisoformat(args["startDateTime"])
        hi = datetime.fromisoformat(args["endDateTime"])
        assert lo.tzinfo is not None
        assert hi.tzinfo is not None
        found = [one for one in self.events if self._overlaps(one, lo, hi)]
        return {"text": json.dumps({"value": found})}

    @staticmethod
    def _overlaps(one: dict[str, Any], lo: datetime, hi: datetime) -> bool:
        start = datetime.fromisoformat(one["start"]["dateTime"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(one["end"]["dateTime"]).replace(tzinfo=UTC)
        return start < hi and end > lo

    def views(self) -> list[tuple[str, str]]:
        return [
            (a["startDateTime"], a["endDateTime"])
            for name, a in self.calls if name == "get-calendar-view"
        ]


def _dispatch(
    registry: ToolRegistry, conn: sqlite3.Connection, paths: Any, args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    """One ``my_schedule`` call through the dispatcher: the result the model reads, within 4000."""
    action_id = uuid.uuid4().hex
    request = ActionRequest(
        action_id=action_id, tool_name="my_schedule", target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L0", arguments=args,
        authorization_lease=None, run_id=None, turn_id=None,
    )
    lifecycle = ActionLifecycle()
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")
    slot = registry.dispatch(request, conn, paths, lifecycle).slots[0]
    assert slot.tool_output is not None
    assert len(slot.tool_output) <= 4000
    out = json.loads(slot.tool_output)
    assert isinstance(out, dict)
    return out


class _World:
    """The tool over the real ``Home`` and a real event log, called through the dispatcher."""

    def __init__(self, root: Path, server: _Microsoft | None, now: datetime = NOW) -> None:
        self.paths = bootstrap_runtime(root)
        self.conn = open_event_log(self.paths.event_log)
        self.server = server
        self.now = now
        home = Home(_Connections(server), (ZONE, LOCAL), None)  # type: ignore[arg-type]
        (self.tool,) = build_schedule_tool(home.graph, LOCAL, lambda: self.now)
        self.registry = ToolRegistry()
        self.registry.register(self.tool)

    def call(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
        return _dispatch(self.registry, self.conn, self.paths, args)

    def error(self, **args: Any) -> str:  # noqa: ANN401 - the model's arguments.
        """The text of the error the model reads for these arguments."""
        out = self.call(**args)
        assert out.get("code") == "invalid_arguments", out
        return str(out["error"])


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Maker]:
    """Builds worlds under ``tmp_path`` and closes their logs."""
    worlds: list[_World] = []

    def build(server: _Microsoft | None, now: datetime = NOW) -> _World:
        world = _World(tmp_path / f"w{len(worlds)}", server, now)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.conn.close()


def _week() -> _Microsoft:
    """Monday's interview, Tuesday's dentist and all-day session, a Wednesday meeting, to-dos."""
    return _Microsoft(
        [
            _timed("e0", "Standup", _at(12, 9), _at(12, 9, 30)),
            _timed(
                "e1", "Interview: Reliable Controls", _at(12, 13, 30), _at(12, 14, 30),
                location={"displayName": "Microsoft Teams Meeting"}, isOnlineMeeting=True,
                webLink=LINK,
            ),
            _timed(
                "e2", "Dentist", _at(13, 8), _at(13, 9),
                location={"displayName": "Smile Clinic, 2nd Ave"}, isOnlineMeeting=False,
            ),
            _all_day("e3", "Co-op info day", date(2026, 10, 13), date(2026, 10, 13)),
            _timed("e4", "Team lunch", _at(14, 12), _at(14, 13)),
            {**_timed("e5", "Cancelled call", _at(13, 10), _at(13, 11)), "isCancelled": True},
        ],
        [
            _task("Submit timesheet", "2026-10-13"),
            _task("Already done", "2026-10-13", "completed"),
            _task("Someday", None),
            _task("Renew library card", "2026-10-14"),
        ],
    )


def _remind(world: _World, text: str, due: datetime) -> str:
    return reminders.schedule(world.conn, due=due, text=text, action_id=f"R-{text}")


# --- the entry -------------------------------------------------------------------------------


def test_the_tool_is_a_small_read_only_native_menu_entry() -> None:
    """One L0 read for the model, on the menu from the first request, with a short brief."""
    (tool,) = build_schedule_tool(dict, LOCAL)  # type: ignore[arg-type]
    assert tool.name == "my_schedule"
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert tool.description.startswith("Use for anything on his schedule")
    assert set(tool.input_schema["properties"]) == {"date", "to", "query", "page"}
    assert tool.input_schema["additionalProperties"] is False
    assert tool.supersedes == {
        "mcp__microsoft__get-calendar-view": "my_schedule(date=..., to=...)",
        "mcp__microsoft__list-todo-task-lists": "my_schedule()",
        "mcp__microsoft__list-todo-tasks": "my_schedule(date=..., to=...)",
    }


# --- what is on ------------------------------------------------------------------------------


def test_no_arguments_is_today_from_now_and_tomorrow_across_all_sources(make: Maker) -> None:
    """Events, to-dos and a reminder, merged in time order, with local words and the source."""
    server = _week()
    world = make(server)
    _remind(world, "Call Lee", _at(12, 20))
    _remind(world, "Next week", _at(20, 9))
    out = world.call()
    assert server.views() == [("2026-10-12T12:40:00-07:00", "2026-10-14T00:00:00-07:00")]
    assert out["summary"] == (
        "today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13): 3 calendar events, 1 to-do,"
        " 1 reminder, read just now. Next: Interview: Reliable Controls,"
        " today (Mon) 1:30-2:30 pm, in 50 min."
    )
    assert list(out) == ["summary", "items", "more", "read"]
    assert out["more"] is False
    assert out["read"] == ["calendar", "to-dos", "reminders"]
    reminder = out["items"][1]
    assert reminder.pop("id").startswith("reminder-")
    assert out["items"] == [
        {
            "when": "today (Mon) 1:30-2:30 pm, in 50 min", "what": "Interview: Reliable Controls",
            "where": "Microsoft Teams Meeting", "link": LINK, "source": "calendar",
        },
        {"when": "today (Mon) 8:00 pm", "what": "Call Lee", "source": "reminder"},
        {"when": "tomorrow (Tue), all day", "what": "Co-op info day", "source": "calendar"},
        {
            "when": "tomorrow (Tue) 8:00-9:00 am", "what": "Dentist",
            "where": "Smile Clinic, 2nd Ave", "source": "calendar",
        },
        {"when": "due tomorrow (Tue)", "what": "Submit timesheet", "source": "to-do"},
    ]


def test_an_event_under_way_says_now_and_an_ended_one_of_the_day_says_done(make: Maker) -> None:
    """date=today is the whole day, so its earlier events show, and say they are over."""
    world = make(_week(), _at(12, 14))
    out = world.call(date="today")
    assert [(i["what"], i["when"]) for i in out["items"][:2]] == [
        ("Standup", "today (Mon) 9:00-9:30 am, done"),
        ("Interview: Reliable Controls", "today (Mon) 1:30-2:30 pm, now"),
    ]
    assert out["summary"].endswith(
        "read just now. Next: Interview: Reliable Controls, today (Mon) 1:30-2:30 pm, now.",
    )


@pytest.mark.parametrize(
    ("args", "start", "end", "label"),
    [
        ({"date": "today"}, "2026-10-12T00:00:00-07:00", "2026-10-13T00:00:00-07:00",
         "today (Mon Oct 12)"),
        ({"date": "tomorrow"}, "2026-10-13T00:00:00-07:00", "2026-10-14T00:00:00-07:00",
         "tomorrow (Tue Oct 13)"),
        ({"date": "yesterday"}, "2026-10-11T00:00:00-07:00", "2026-10-12T00:00:00-07:00",
         "yesterday (Sun Oct 11)"),
        ({"date": "2026-10-15"}, "2026-10-15T00:00:00-07:00", "2026-10-16T00:00:00-07:00",
         "Thu Oct 15"),
        ({"date": "2026-10-15T09:00:00-07:00"}, "2026-10-15T00:00:00-07:00",
         "2026-10-16T00:00:00-07:00", "Thu Oct 15"),
        ({"date": "Mon"}, "2026-10-19T00:00:00-07:00", "2026-10-20T00:00:00-07:00", "Mon Oct 19"),
        ({"date": "wednesday"}, "2026-10-14T00:00:00-07:00", "2026-10-15T00:00:00-07:00",
         "Wed Oct 14"),
        ({"date": "next Sun."}, "2026-10-18T00:00:00-07:00", "2026-10-19T00:00:00-07:00",
         "Sun Oct 18"),
        ({"date": "2026-12-25"}, "2026-12-25T00:00:00-08:00", "2026-12-26T00:00:00-08:00",
         "Fri Dec 25"),
        ({"date": "2027-01-04"}, "2027-01-04T00:00:00-08:00", "2027-01-05T00:00:00-08:00",
         "Mon Jan 4 2027"),
        ({"date": "today", "to": "Sun"}, "2026-10-12T00:00:00-07:00", "2026-10-19T00:00:00-07:00",
         "today (Mon Oct 12) to Sun Oct 18"),
        ({"to": "Fri"}, "2026-10-12T00:00:00-07:00", "2026-10-17T00:00:00-07:00",
         "today (Mon Oct 12) to Fri Oct 16"),
        ({"date": "Wed", "to": "Fri"}, "2026-10-14T00:00:00-07:00", "2026-10-17T00:00:00-07:00",
         "Wed Oct 14 to Fri Oct 16"),
        ({"date": "2026-10-12", "to": "2026-10-25"}, "2026-10-12T00:00:00-07:00",
         "2026-10-26T00:00:00-07:00", "today (Mon Oct 12) to Sun Oct 25"),
    ],
)
def test_a_day_or_span_asks_graph_for_its_own_local_midnights(
    make: Maker, args: dict[str, str], start: str, end: str, label: str,
) -> None:
    """The bounds sent are the local midnights of the first and the day after the last."""
    server = _Microsoft()
    out = make(server).call(**args)
    assert server.views() == [(start, end)]
    assert out["summary"] == (
        f"Nothing on your calendar, to-dos or reminders for {label} (read just now)."
    )


# --- the local day ---------------------------------------------------------------------------


def test_an_event_at_23_30_local_belongs_to_its_local_day_not_the_utc_one(make: Maker) -> None:
    """23:30 on the 20th is 06:30 UTC on the 21st; the 21st's window must not return it."""
    late = _timed("late", "Night shift handover", _at(20, 23, 30), _at(20, 23, 50))
    early = _timed("early", "Early run", _at(21, 0, 30), _at(21, 1))
    world = make(_Microsoft([late, early]))
    on_20 = world.call(date="2026-10-20")
    on_21 = world.call(date="2026-10-21")
    assert [i["what"] for i in on_20["items"]] == ["Night shift handover"]
    assert on_20["items"][0]["when"] == "Tue Oct 20 11:30-11:50 pm"
    assert [i["what"] for i in on_21["items"]] == ["Early run"]
    assert on_21["items"][0]["when"] == "Wed Oct 21 12:30-1:00 am"


def test_an_event_that_crosses_midnight_is_on_both_days_and_says_when_it_ends(make: Maker) -> None:
    """Graph returns it for both windows; it keeps its start and names its end."""
    world = make(_Microsoft([_timed("x", "Hackathon", _at(20, 22), _at(21, 2))]))
    for day in ("2026-10-20", "2026-10-21"):
        item = world.call(date=day)["items"][0]
        assert item["when"] == "Tue Oct 20 10:00 pm to Wed Oct 21 2:00 am"


def test_a_long_day_keeps_its_bounds_across_the_autumn_clock_change(make: Maker) -> None:
    """Nov 1 2026 has 25 hours: it ends at -08:00, so 23:30 PST is still Nov 1."""
    server = _Microsoft([
        _timed("a", "Evening class", _at(1, 23, 30, month=11), _at(1, 23, 50, month=11)),
        _timed("b", "After midnight", _at(2, 0, 30, month=11), _at(2, 1, month=11)),
    ])
    world = make(server)
    nov_1 = world.call(date="2026-11-01")
    nov_2 = world.call(date="2026-11-02")
    assert server.views() == [
        ("2026-11-01T00:00:00-07:00", "2026-11-02T00:00:00-08:00"),
        ("2026-11-02T00:00:00-08:00", "2026-11-03T00:00:00-08:00"),
    ]
    assert [i["what"] for i in nov_1["items"]] == ["Evening class"]
    assert [i["what"] for i in nov_2["items"]] == ["After midnight"]
    world.call(date="2026-10-31", to="2026-11-02")
    assert server.views()[-1] == ("2026-10-31T00:00:00-07:00", "2026-11-03T00:00:00-08:00")


def test_a_short_day_keeps_its_bounds_across_the_spring_clock_change(make: Maker) -> None:
    """Mar 14 2027 has 23 hours: it starts at -08:00 and ends at -07:00."""
    server = _Microsoft()
    make(server).call(date="2027-03-14")
    assert server.views() == [("2027-03-14T00:00:00-08:00", "2027-03-15T00:00:00-07:00")]


def test_an_all_day_event_is_on_its_dates_not_on_the_day_graphs_utc_window_returned_it_for(
    make: Maker,
) -> None:
    """The window ending at local midnight overlaps the next UTC day: the date decides."""
    fair = _all_day("f", "Career fair", date(2026, 10, 13), date(2026, 10, 14))
    world = make(_Microsoft([fair]))
    assert world.server is not None
    assert world.server.events[0]["start"]["dateTime"].startswith("2026-10-13")
    on_12 = world.call(date="2026-10-12")
    assert on_12["items"] == []
    on_13 = world.call(date="2026-10-13")
    assert on_13["items"] == [
        {"when": "tomorrow (Tue) to Wed Oct 14, all day", "what": "Career fair",
         "source": "calendar"},
    ]
    assert world.call(date="2026-10-14")["items"] == on_13["items"]
    assert world.call(date="2026-10-15")["items"] == []


# --- the find --------------------------------------------------------------------------------


def test_a_query_finds_an_event_by_its_words_in_the_next_sixty_days(make: Maker) -> None:
    """Every word must be in the title or place, in any order; the other sources are searched."""
    server = _Microsoft(
        [
            _timed(
                "i", "Interview: Reliable Controls — Yilun Shi", _at(13, 13, 30),
                _at(13, 14, 30), location={"displayName": "Teams"},
            ),
            _timed("d", "Dentist", _at(14, 8), _at(14, 9)),
            _timed(
                "far", "Reliable Controls onboarding", _at(11, 9, month=12),
                _at(11, 10, month=12),
            ),
        ],
        [_task("Reliable Controls thank-you note", "2026-10-14")],
    )
    world = make(server)
    _remind(world, "Prep for the Reliable Controls interview", _at(13, 12))
    out = world.call(query="reliable controls INTERVIEW")
    assert server.views() == [("2026-10-12T00:00:00-07:00", "2026-12-11T00:00:00-08:00")]
    assert [(i["source"], i["what"]) for i in out["items"]] == [
        ("reminder", "Prep for the Reliable Controls interview"),
        ("calendar", "Interview: Reliable Controls — Yilun Shi"),
    ]
    assert out["summary"] == (
        '"reliable controls INTERVIEW" in the next 60 days: 1 calendar event, 1 reminder,'
        " read just now. Next: Prep for the Reliable Controls interview, tomorrow (Tue) 12:00 pm."
    )
    nothing = world.call(query="dentist visa")
    assert nothing["summary"] == (
        'Nothing on your calendar, to-dos or reminders matches "dentist visa" in the next 60 days'
        " (read just now)."
    )
    place = world.call(query="teams")
    assert [i["what"] for i in place["items"]] == ["Interview: Reliable Controls — Yilun Shi"]


# --- read, and not read ----------------------------------------------------------------------


def test_nothing_on_a_day_that_was_read_says_so_and_names_what_was_read(make: Maker) -> None:
    """An empty answer is a sentence with the day and 'read just now', never an empty list alone."""
    out = make(_Microsoft()).call(date="tomorrow")
    assert out == {
        "summary": "Nothing on your calendar, to-dos or reminders for tomorrow (Tue Oct 13)"
        " (read just now).",
        "items": [], "more": False, "read": ["calendar", "to-dos", "reminders"],
    }


def test_microsoft_not_connected_is_named_and_the_calendar_is_never_called_clear(
    make: Maker,
) -> None:
    """The reminders still read; the calendar and to-dos are said to be unread, with what to do."""
    world = make(None)
    _remind(world, "Call Lee", _at(13, 9))
    out = world.call(date="tomorrow")
    why = "Microsoft is not connected; tell him it needs connecting"
    assert out["summary"] == (
        "tomorrow (Tue Oct 13): 1 reminder, read just now."
        " Next: Call Lee, tomorrow (Tue) 9:00 am."
        f" Could not read his calendar and his to-dos ({why})."
    )
    assert out["read"] == ["reminders"]
    assert out["missing"] == [f"his calendar: {why}", f"his to-dos: {why}"]
    empty = make(None).call(date="tomorrow")
    assert empty["summary"] == (
        "Nothing on your reminders for tomorrow (Tue Oct 13) (read just now)."
        f" Could not read his calendar and his to-dos ({why})."
    )
    assert empty["items"] == []


@pytest.mark.parametrize(
    ("failure", "why"),
    [
        (ToolError("microsoft: ConnectionError: boom", code="mcp_server"),
         "Microsoft did not answer (microsoft: ConnectionError: boom);"
         " tell him it could not be read just now"),
        (ToolError("AADSTS70043: the refresh token has expired", code="mcp_tool_error"),
         "Microsoft needs reconnecting (AADSTS70043: the refresh token has expired);"
         " tell him, it cannot be read until he does"),
    ],
)
def test_a_failing_calendar_is_named_with_what_happened_and_the_to_dos_still_show(
    make: Maker, failure: ToolError, why: str,
) -> None:
    """One source failing does not hide the others, and its words are the server's."""
    server = _week()
    server.fail["get-calendar-view"] = failure
    out = make(server).call(date="tomorrow")
    assert out["summary"] == (
        "tomorrow (Tue Oct 13): 1 to-do, read just now. Next: Submit timesheet, due tomorrow"
        f" (Tue). Could not read his calendar ({why})."
    )
    assert out["read"] == ["to-dos", "reminders"]
    assert out["missing"] == [f"his calendar: {why}"]
    assert "Nothing" not in out["summary"]


def test_an_answer_that_is_not_json_is_unreadable_not_empty(make: Maker) -> None:
    """A server that answers rubbish is reported, and the day is not called empty."""
    server = _week()
    server.raw["get-calendar-view"] = "<html>bad gateway</html>"
    server.raw["list-todo-task-lists"] = '{"value": "none"}'
    out = make(server).call(date="tomorrow")
    why = "Microsoft answered something unreadable; tell him it could not be read just now"
    assert out["summary"] == (
        "Nothing on your reminders for tomorrow (Tue Oct 13) (read just now)."
        f" Could not read his calendar and his to-dos ({why})."
    )


# --- pages -----------------------------------------------------------------------------------


def test_eleven_items_come_eight_then_three_and_a_page_past_the_end_says_where_it_ends(
    make: Maker,
) -> None:
    """``page`` counts items 9-16; each page reads the sources again, so nothing is held."""
    events = [
        _timed(f"e{n}", f"Meeting {n:02d}", _at(13, 8 + n % 12, 10 * (n % 5)), _at(13, 13, 59))
        for n in range(11)
    ]
    server = _Microsoft(events)
    world = make(server)
    first = world.call(date="tomorrow")
    assert first["more"] is True
    assert len(first["items"]) == 8
    assert first["summary"].startswith(
        "tomorrow (Tue Oct 13): 11 items: 11 calendar events, read just now. Next: Meeting",
    )
    assert first["summary"].endswith(" Showing 1-8; page=2 for the rest.")
    second = world.call(date="tomorrow", page=2)
    assert second["more"] is False
    assert len(second["items"]) == 3
    assert second["summary"] == (
        "tomorrow (Tue Oct 13): items 9-11 of 11 (11 calendar events), read just now."
    )
    names = [i["what"] for i in first["items"] + second["items"]]
    assert sorted(names) == [f"Meeting {n:02d}" for n in range(11)]
    assert len(set(names)) == 11
    past = world.call(date="tomorrow", page=3)
    assert past == {
        "summary": "There is no page 3 of tomorrow (Tue Oct 13); its items end at page 2.",
        "items": [], "more": False, "read": ["calendar", "to-dos", "reminders"],
    }
    assert len(server.views()) == 3


def test_online_meeting_links_give_way_before_rows_when_a_page_would_pass_four_thousand(
    make: Maker,
) -> None:
    """Eight long rows with links stay inside the 4000 characters the dispatcher checks."""
    long_link = "https://outlook.example/" + "x" * 270
    events = [
        _timed(
            f"e{n}", "T" * 120, _at(13, 8 + n), _at(13, 9 + n),
            location={"displayName": "L" * 80}, isOnlineMeeting=True, webLink=long_link,
        )
        for n in range(8)
    ]
    out = make(_Microsoft(events)).call(date="tomorrow")
    assert len(out["items"]) == 8
    assert any("link" in row for row in out["items"])
    assert not all("link" in row for row in out["items"])


# --- the pinned bus --------------------------------------------------------------------------


def test_a_pinned_bus_is_listed_once_and_its_ring_is_not_a_second_reminder(make: Maker) -> None:
    """The bus is a row on its leave time; the reminder that rings it is the same thing."""
    world = make(_Microsoft())
    leave = _at(12, 13, 5)
    ring = _remind(world, "Leave for the 28 now", leave)
    other = _remind(world, "Water the plants", _at(12, 18))
    trip = departures.Trip(
        "t1", "28 → 12", "Hillside Mall", int(leave.timestamp() * 1000),
        int((leave + timedelta(minutes=8)).timestamp() * 1000), "1:40 pm", None, None, "UVic",
    )
    departures.pin(
        world.conn, reminder_id=ring, ring="t1", trips=[trip], offer_id=None, action_id="P1",
    )
    out = world.call()
    assert [(i["source"], i["when"], i["what"]) for i in out["items"]] == [
        ("bus", "leave today (Mon) 1:05 pm, in 25 min", "bus 28 → 12 from Hillside Mall to UVic"),
        ("reminder", "today (Mon) 6:00 pm, in 5 h 20 min", "Water the plants"),
    ]
    assert out["items"][1]["id"] == other
    assert "1 reminder, 1 bus to catch, read just now." in out["summary"]


# --- the arguments ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"bogus": 1}, "my_schedule: no argument 'bogus'; use date, to, query or page"),
        ({"date": "soon"},
         "my_schedule: a day looks like today, tomorrow, Mon or 2026-10-15, not 'soon'"),
        ({"date": "2026-13-45"},
         "my_schedule: a day looks like today, tomorrow, Mon or 2026-10-15, not '2026-13-45'"),
        ({"date": "2026-10-20", "to": "2026-10-19"},
         "my_schedule: to=2026-10-19 is before date=2026-10-20; to is the last day"),
        ({"date": "2026-10-12", "to": "2026-10-26"},
         "my_schedule: a span is at most 14 days; ask in parts for longer"),
        ({"query": "x", "date": "today"},
         "my_schedule: query searches the next 60 days; pass it without date or to"),
        ({"query": "x", "to": "Fri"},
         "my_schedule: query searches the next 60 days; pass it without date or to"),
        ({"page": 0}, "my_schedule: page must be a whole number, e.g. page=2 for items 9-16"),
        ({"page": "two"}, "my_schedule: page must be a whole number, e.g. page=2 for items 9-16"),
    ],
)
def test_a_wrong_argument_is_refused_with_what_to_pass_instead(
    make: Maker, args: dict[str, Any], message: str,
) -> None:
    """The error says what was wrong and the shape that works; nothing is read."""
    server = _Microsoft()
    assert make(server).error(**args) == message
    assert server.calls == []


# --- the menu --------------------------------------------------------------------------------


def _recorded(tmp_path: Path) -> Path:
    recorded = tmp_path / "mcp-tools.json"
    names = (
        "get-calendar-view", "list-todo-task-lists", "list-todo-tasks", "list-calendars",
        "create-todo-task", "update-todo-task",
    )
    recorded.write_text(json.dumps({
        f"mcp__microsoft__{name}": {
            "name": name, "description": f"{name} for the calendar and to-do lists",
            "input_schema": {"type": "object"},
        }
        for name in names
    }), encoding="utf-8")
    return recorded


def test_the_menu_offers_my_schedule_and_not_the_three_reads_it_replaces(tmp_path: Path) -> None:
    """The shipped config: the Microsoft reads are deferred and tool_search points to the tool."""
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    recorded = _recorded(tmp_path)
    names = [one["name"] for one in tool_exam.offered_tools(config, recorded)]
    assert "my_schedule" in names
    assert not [n for n in names if n.startswith("mcp__microsoft__")]
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001 - the registry behind the menu
    held = {t.name: t for t in registry.get_definitions()}
    for name in ("get-calendar-view", "list-todo-task-lists", "list-todo-tasks"):
        assert held[f"mcp__microsoft__{name}"].deferred
    assert held["mcp__microsoft__create-todo-task"].deferred
    assert {k: v for k, v in registry.superseded().items() if v[0] == "my_schedule"} == {
        "mcp__microsoft__get-calendar-view": ("my_schedule", "my_schedule(date=..., to=...)"),
        "mcp__microsoft__list-todo-task-lists": ("my_schedule", "my_schedule()"),
        "mcp__microsoft__list-todo-tasks": ("my_schedule", "my_schedule(date=..., to=...)"),
    }
    search = held["tool_search"]
    assert isinstance(search, Tool)
    found = search.handler({"query": "calendar events"}, cast("ToolContext", None))
    loaded = found["loaded_tools"]
    assert "mcp__microsoft__get-calendar-view" not in loaded
    assert "mcp__microsoft__list-todo-tasks" not in loaded
    assert "my_schedule(date=..., to=...)" in found["note"]
    writes = search.handler({"query": "create a to-do task"}, cast("ToolContext", None))
    assert writes["loaded_tools"][0] == "mcp__microsoft__create-todo-task"
    assert "mcp__microsoft__update-todo-task" in writes["loaded_tools"]


def test_the_step_label_and_the_dispatch_line_say_calendar() -> None:
    """The phone's step and the spoken line are those of the calendar read it replaced."""
    assert steps.kind("my_schedule", {}) == steps.kind("mcp__microsoft__get-calendar-view")
    assert _dispatch_key("my_schedule") == _dispatch_key("mcp__microsoft__get-calendar-view")


def test_the_booted_runtime_has_the_tool_and_reports_microsoft_not_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired by the runtime over its own ``Home``: no Microsoft connection is said, not hidden."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        "runtime:\n  role: brain\nobserver:\n  claude_sessions:\n    enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = bootstrap_runtime_app(runtime_root=root)
    try:
        held = {t.name: t for t in runtime.tool_registry.for_caller(CallerPrincipal.JARVIS_LLM)}
        assert "my_schedule" in held
        assert not held["my_schedule"].deferred
        out = _dispatch(
            runtime.tool_registry, runtime.conn, runtime.runtime_paths, {"date": "tomorrow"},
        )
        assert "Could not read his calendar and his to-dos (Microsoft is not connected" in (
            out["summary"]
        )
        assert out["read"] == ["reminders"]
        # The multi-step exam stubs it with the same honesty: no calendar was read.
        classes = tool_exam_steps.classify(runtime.tool_registry)
        assert classes["my_schedule"] == "stub"
        tool_exam_steps._apply_stubs(runtime.tool_registry, classes)  # noqa: SLF001
        stubbed = _dispatch(
            runtime.tool_registry, runtime.conn, runtime.runtime_paths, {"date": "tomorrow"},
        )
        assert stubbed["summary"].startswith("Could not read his calendar and his to-dos (sandbox")
        assert stubbed["read"] == []
    finally:
        runtime.conn.close()
