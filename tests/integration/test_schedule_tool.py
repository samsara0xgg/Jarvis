"""ADR 0299: everything he has to go to or do is one ledger; ``schedule`` is her one tool for it.

The real tool runs through ``ToolRegistry.dispatch`` onto a real event log and the real ledger
(:mod:`jarvis.state.things`) under a fixed clock (Monday 2026-10-12, 12:40 at UTC-7). Each check is
a request and the exact words or fields the model reads back, the thing the ledger holds
afterwards, and the rings it armed. No network, no key, no model.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.tier0 import (
    load_tier0_table,
    match_tier0,
    render_tier0_response,
    validate_tier0_table,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.schedule_tool import build_schedule_tool
from jarvis.execution.tools import (
    ActionLifecycle,
    Tool,
    ToolContext,
    ToolRegistry,
    build_default_registry,
)
from jarvis.runtime import bootstrap_runtime_app
from jarvis.shared import ActionRequest, CallerPrincipal, lang, steps
from jarvis.state import commitments, departures, things
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from scripts import tool_exam, tool_exam_steps
from tests.support.clock import FakeClock

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Mapping

    type Maker = Callable[..., _World]

REPO = Path(__file__).resolve().parents[2]
ZONE = "America/Los_Angeles"
"""UTC-7 in October like Vancouver, with clock changes on Nov 1 2026 and Mar 14 2027 in every
tzdata release (Vancouver's are being dropped from it)."""
LOCAL = ZoneInfo(ZONE)
NOW = datetime(2026, 10, 12, 12, 40, tzinfo=LOCAL)  # Monday
ROUTER = CallerPrincipal.REGEX_ROUTER
OLD = {
    "create_memo", "list_memos", "set_reminder", "list_reminders", "cancel_reminder",
    "my_schedule", "remember",
}


def _dispatch(  # noqa: PLR0913 - the registry, its stores, who calls and what
    registry: ToolRegistry, conn: sqlite3.Connection, paths: Any,  # noqa: ANN401
    name: str, args: Mapping[str, Any], who: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
) -> dict[str, Any]:
    """One call through the dispatcher: the result the model reads, within 4000 characters."""
    action_id = uuid.uuid4().hex
    request = ActionRequest(
        action_id=action_id, tool_name=name, target_entity_ref=None, caller_principal=who,
        risk_level="L1", arguments=args, authorization_lease=None, run_id=None, turn_id=None,
    )
    lifecycle = ActionLifecycle()
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")
    slot = registry.dispatch(request, conn, paths, lifecycle)
    assert slot.tool_output is not None
    assert len(slot.tool_output) <= 4000
    out = json.loads(slot.tool_output)
    assert isinstance(out, dict)
    out["_action_id"] = action_id
    return out


class _World:
    """The tool over a real event log, called through the dispatcher under a moving clock."""

    def __init__(self, root: Path, before_min: int = 60) -> None:
        self.paths = bootstrap_runtime(root)
        self.conn = open_event_log(self.paths.event_log)
        self.clock = FakeClock(NOW, zone=LOCAL)
        self.registry = ToolRegistry(clock=self.clock)
        for tool in build_schedule_tool(before_min):
            self.registry.register(tool)

    @property
    def now(self) -> datetime:
        """The tool's clock; assigning moves it."""
        return self.clock.now()

    @now.setter
    def now(self, value: datetime) -> None:
        self.clock.set(value)

    def run(
        self, who: CallerPrincipal, args: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _dispatch(self.registry, self.conn, self.paths, "schedule", args, who)

    def call(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
        return self.run(CallerPrincipal.JARVIS_LLM, args)

    def error(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
        """The refusal the model reads for these arguments."""
        out = self.call(**args)
        assert "error" in out, out
        return out

    def thing(self, short: str) -> things.Thing:
        return things.find(self.conn, short)

    def count(self, event_type: str) -> int:
        return len(list(iter_events_of_types(self.conn, (event_type,))))

    def rings(self) -> list[str]:
        """When the pending rings the ledger armed ring, in his local time, soonest first."""
        return sorted(
            r.due_at_local for r in reminder_state.pending(self.conn)
            if r.reminder_id.startswith(things.RING_PREFIX)
        )

    def seed(
        self, title: str, kind: str = "do", *, by: str = "her", **fields: Any,  # noqa: ANN401
    ) -> str:
        """A thing written straight to the ledger (a past one, a mail's); its short id."""
        one = things.Thing(fields.pop("thing_id", things.new_id()), title, kind, **fields)
        made, created = things.add(
            self.conn, one, by=by, evidence="seeded", zone=LOCAL, now=self.now,
        )
        assert created
        return things.short_id(made)

    def nothing_written(self) -> bool:
        return self.count("thing.added") == 0 and self.rings() == []


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Maker]:
    """Builds worlds under ``tmp_path`` and closes their logs; the language is English."""
    before = lang.language()
    lang.set_language("en")
    worlds: list[_World] = []

    def build(before_min: int = 60) -> _World:
        world = _World(tmp_path / f"w{len(worlds)}", before_min)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.conn.close()
    lang.set_language(before)


def _rows(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The items of a read without their (random) ids."""
    return [{k: v for k, v in row.items() if k != "id"} for row in out["items"]]


# --- the entry -------------------------------------------------------------------------------


def test_the_tool_is_one_resident_menu_entry_that_reads_and_writes() -> None:
    """One L1 tool for the model and the router, on the menu from the first request."""
    (tool,) = build_schedule_tool(60)
    assert tool.name == "schedule"
    assert (tool.risk_level, tool.read_only, tool.deferred) == ("L1", False, False)
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM, ROUTER})
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert tool.description.startswith("Use for anything he has to go to or do")
    assert "(keep)" in tool.description
    assert "(my_day)" in tool.description
    props = tool.input_schema["properties"]
    assert set(props) == {
        "action", "what", "id", "at", "ends", "due", "to", "kind", "remind", "place",
        "weekly_until", "occurrence", "private", "whole_sender", "page",
    }
    assert all(one["description"] for one in props.values())
    assert props["action"]["enum"] == [
        "read", "add", "move", "done", "paid", "cancel", "confirm", "reopen",
    ]
    assert props["kind"]["enum"] == ["go", "do"]
    assert tool.input_schema["additionalProperties"] is False
    assert "required" not in tool.input_schema
    assert tool.supersedes == {
        "create_memo": 'schedule(action="add", what=...)',
        "set_reminder": 'schedule(action="add", what=..., at=...)',
        "cancel_reminder": 'schedule(action="cancel", id=...)',
        "list_memos": "schedule()",
        "list_reminders": "schedule()",
        "my_schedule": "schedule(at=..., to=...)",
        "mcp__microsoft__get-calendar-view": "schedule(at=..., to=...)",
        "mcp__microsoft__list-todo-task-lists": "schedule()",
        "mcp__microsoft__list-todo-tasks": "schedule(at=..., to=...)",
    }


# --- add ---------------------------------------------------------------------------------------

NOTHING_FIELDS: dict[str, Any] = {
    "kind": "do", "start_at": "", "on_day": "", "due": "", "rings": (),
}
ADD = [
    pytest.param(
        {"what": "fix the emoji feature"},
        'Kept "fix the emoji feature" with no time (id {id}).',
        NOTHING_FIELDS, [],
        id="words alone: a to-do with no time and no ring",
    ),
    pytest.param(
        {"what": "call mum", "at": "2026-10-13 09:00"},
        'Kept "call mum" for tomorrow (Tue) 9:00 am; rings at the time (id {id}).',
        {"kind": "do", "start_at": "2026-10-13T09:00:00-07:00", "rings": ("-0m",)},
        ["2026-10-13T09:00:00-07:00"],
        id="a do with a clock time rings at the time",
    ),
    pytest.param(
        {"what": "call mum", "at": "2026-10-13T16:00:00+00:00"},
        'Kept "call mum" for tomorrow (Tue) 9:00 am; rings at the time (id {id}).',
        {"start_at": "2026-10-13T09:00:00-07:00"}, ["2026-10-13T09:00:00-07:00"],
        id="a time with its own offset is read as that instant",
    ),
    pytest.param(
        {
            "what": "Dentist", "kind": "go", "at": "2026-10-13 15:30",
            "ends": "2026-10-13 16:30", "place": "Smile Clinic",
        },
        'Kept "Dentist" for tomorrow (Tue) 3:30 pm; rings 60 min before (id {id}).',
        {
            "kind": "go", "start_at": "2026-10-13T15:30:00-07:00",
            "end_at": "2026-10-13T16:30:00-07:00", "place": "Smile Clinic", "link": "",
            "rings": ("-60m",),
        },
        ["2026-10-13T14:30:00-07:00"],
        id="a go with a clock time rings the usual lead before",
    ),
    pytest.param(
        {"what": "Interview", "kind": "go", "at": "2026-10-13 13:00", "place": "https://m.example/a"},
        'Kept "Interview" for tomorrow (Tue) 1:00 pm; rings 60 min before (id {id}).',
        {"place": "", "link": "https://m.example/a"}, ["2026-10-13T12:00:00-07:00"],
        id="a place that is a link is the link",
    ),
    pytest.param(
        {"what": "Co-op info day", "kind": "go", "at": "2026-10-15"},
        'Kept "Co-op info day" for Thu Oct 15; rings the evening before (id {id}).',
        {"on_day": "2026-10-15", "start_at": "", "rings": ("-1d@20:00",)},
        ["2026-10-14T20:00:00-07:00"],
        id="a go on a day rings the evening before",
    ),
    pytest.param(
        {"what": "fix the sink", "at": "2026-10-14"},
        'Kept "fix the sink" for Wed Oct 14 (id {id}).',
        {"on_day": "2026-10-14", "rings": ()}, [],
        id="a do on a day has no ring",
    ),
    pytest.param(
        {"what": "Pay rent", "due": "2026-10-31"},
        'Kept "Pay rent", due Sat Oct 31 (id {id}).',
        {"due": "2026-10-31", "start_at": "", "rings": ()}, [],
        id="a due is a deadline, not a ring",
    ),
    pytest.param(
        {"what": "Send report", "due": "2026-10-13 17:00", "remind": [30]},
        'Kept "Send report", due tomorrow (Tue) 5:00 pm; rings 30 min before (id {id}).',
        {"due": "2026-10-13T17:00:00-07:00", "rings": ("-30m",)},
        ["2026-10-13T16:30:00-07:00"],
        id="a due with a time rings when asked",
    ),
    pytest.param(
        {"what": "Call Lee", "at": "2026-10-13 09:00", "remind": [30, 10]},
        'Kept "Call Lee" for tomorrow (Tue) 9:00 am; rings 30 min before and 10 min before'
        " (id {id}).",
        {"rings": ("-30m", "-10m")},
        ["2026-10-13T08:30:00-07:00", "2026-10-13T08:50:00-07:00"],
        id="remind lists the minutes before",
    ),
    pytest.param(
        {"what": "Call Lee", "at": "2026-10-13 09:00", "remind": [0, 0, 5]},
        'Kept "Call Lee" for tomorrow (Tue) 9:00 am; rings at the time and 5 min before'
        " (id {id}).",
        {"rings": ("-0m", "-5m")},
        ["2026-10-13T08:55:00-07:00", "2026-10-13T09:00:00-07:00"],
        id="0 rings at the time, and a repeat is one ring",
    ),
    pytest.param(
        {"what": "Walk", "at": "2026-10-13 09:00", "remind": []},
        'Kept "Walk" for tomorrow (Tue) 9:00 am (id {id}).',
        {"start_at": "2026-10-13T09:00:00-07:00", "rings": ()}, [],
        id="remind [] is no ring at all",
    ),
    pytest.param(
        {"what": "Call back", "kind": "go", "at": "2026-10-12 13:10"},
        'Kept "Call back" for today (Mon) 1:10 pm; its ring time has passed, so nothing will'
        " ring (id {id}).",
        {"rings": ("-60m",)}, [],
        id="a ring already past is said, never made late",
    ),
    pytest.param(
        {"what": "Passport photo", "kind": "go", "at": "2026-11-20 10:00"},
        'Kept "Passport photo" for Fri Nov 20, 10:00 am; rings 60 min before (id {id}).',
        {"rings": ("-60m",)}, [],
        id="a ring weeks off waits for its week",
    ),
    pytest.param(
        {
            "what": "Seminar", "kind": "go", "at": "2026-10-13 10:00",
            "weekly_until": "2026-11-03",
        },
        'Kept "Seminar" for tomorrow (Tue) 10:00 am, every week until 2026-11-03;'
        " rings 60 min before (id {id}).",
        {"weekly_until": "2026-11-03"}, ["2026-10-13T09:00:00-07:00"],
        id="weekly until a date: the ring is this week's",
    ),
]


@pytest.mark.parametrize(("args", "summary", "fields", "rings"), ADD)
def test_add_keeps_the_thing_says_so_and_arms_the_rings_it_should(
    make: Maker, args: dict[str, Any], summary: str, fields: dict[str, Any], rings: list[str],
) -> None:
    """The sentence, the thing as the ledger folds it, and the rings that are armed."""
    world = make()
    out = world.call(action="add", **args)
    one = world.thing(out["id"])
    assert out["summary"] == summary.format(id=out["id"])
    assert (out["state"], one.state, one.source, one.confirmed) == ("open", "open", "said", True)
    assert {name: getattr(one, name) for name in fields} == fields
    assert world.rings() == rings
    assert world.count("thing.added") == 1


def test_a_write_is_hers_with_his_words_as_the_evidence_and_the_running_event_as_the_ref(
    make: Maker,
) -> None:
    """Who wrote, on what proof and under which call: what the ledger's history keeps."""
    world = make()
    out = world.call(action="add", what="call mum", at="2026-10-13 09:00")
    (added,) = iter_events_of_types(world.conn, ("thing.added",))
    running = next(
        e for e in iter_events_of_types(world.conn, ("action.running",))
        if e.payload["action_id"] == out["_action_id"]
    )
    assert (added.payload["by"], added.payload["evidence"]) == ("her", "call mum")
    assert added.payload["ref"] == running.event_uid
    world.call(action="done", id=out["id"], what="rang her")
    (closed,) = iter_events_of_types(world.conn, ("thing.closed",))
    assert (closed.payload["by"], closed.payload["evidence"]) == ("her", "rang her")
    assert closed.payload["ref"] != added.payload["ref"]


def test_a_long_text_is_a_title_and_a_note(make: Maker) -> None:
    """The ledger's title is a line; the rest of what he said is kept as the note."""
    world = make()
    out = world.call(action="add", what="x" * 400)
    one = world.thing(out["id"])
    assert (len(one.title), one.note) == (things.TITLE_CHARS, "x" * 400)
    assert len(out["summary"]) < 250


def test_the_lead_before_a_go_is_the_one_the_runtime_gives(make: Maker) -> None:
    """``commitments.remind_before_min`` reaches the default ring of a go with a clock time."""
    world = make(before_min=15)
    out = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:30")
    assert world.thing(out["id"]).rings == ("-15m",)
    assert "rings 15 min before" in out["summary"]
    assert world.rings() == ["2026-10-13T15:15:00-07:00"]


@pytest.mark.parametrize(
    ("again", "same"),
    [
        ({"what": "call mum", "at": "2026-10-13 09:00"}, True),
        ({"what": "  Call   MUM ", "at": "2026-10-13 09:00"}, True),
        ({"what": "call mum", "at": "2026-10-13T16:00:00+00:00"}, True),
        ({"what": "call mum", "at": "2026-10-13 09:00", "kind": "go"}, True),
        ({"what": "call mum", "at": "2026-10-13 09:01"}, False),
        ({"what": "call dad", "at": "2026-10-13 09:00"}, False),
        ({"what": "call mum", "at": "2026-10-13"}, False),
        ({"what": "call mum"}, False),
    ],
)
def test_the_same_words_at_the_same_time_are_kept_once(
    make: Maker, again: dict[str, Any], *, same: bool,
) -> None:
    """Words and time decide: the repeat returns the first id; a changed one is a new thing."""
    world = make()
    first = world.call(action="add", what="call mum", at="2026-10-13 09:00")
    repeat = world.call(action="add", **again)
    assert (repeat["id"] == first["id"]) is same
    assert repeat["summary"].startswith("Already kept, nothing new: ") is same
    assert world.count("thing.added") == (1 if same else 2)
    assert len(world.rings()) == (1 if same else len(world.rings()))


def test_a_closed_thing_can_be_kept_again(make: Maker) -> None:
    """Only an open thing is the same one: after a cancel the same words are new."""
    world = make()
    first = world.call(action="add", what="call mum", at="2026-10-13 09:00")
    world.call(action="cancel", id=first["id"])
    again = world.call(action="add", what="call mum", at="2026-10-13 09:00")
    assert again["summary"].startswith("Kept ")
    assert again["id"] != first["id"]


REFUSED = [
    ({"at": "2026-10-11 18:00"}, "time_in_past",
     "yesterday (Sun) 6:00 pm is in the past (it is today (Mon) 12:40 pm now)"),
    ({"at": "2026-10-11"}, "time_in_past", "at 2026-10-11 is before today (2026-10-12)"),
    ({"due": "2026-10-01"}, "time_in_past", "due 2026-10-01 is before today (2026-10-12)"),
    ({"at": "2028-01-01 09:00"}, "too_far_ahead", "more than a year ahead"),
    ({"at": "2028-01-01"}, "too_far_ahead", "more than a year ahead"),
    ({"at": "tomorrow 9am"}, "invalid_time", "work the date out from the time in your state line"),
    ({"at": "2026-13-45"}, "invalid_time", 'at "2026-13-45" is not a date like 2026-10-13'),
    ({"at": "2026-10-13 09:00", "due": "2026-10-20"}, "invalid_arguments",
     "either at (when it starts) or due (by when)"),
    ({"ends": "2026-10-13 10:00"}, "invalid_arguments", "ends goes with a clock time in at"),
    ({"at": "2026-10-13", "ends": "2026-10-13 10:00"}, "invalid_arguments",
     "ends goes with a clock time in at"),
    ({"at": "2026-10-13 10:00", "ends": "2026-10-14"}, "invalid_time", "ends is a clock time"),
    ({"at": "2026-10-13 10:00", "ends": "2026-10-13 09:00"}, "invalid_arguments",
     "is before start_at"),
    ({"remind": [30]}, "invalid_arguments", "remind needs a time to ring from"),
    ({"at": "2026-10-13 10:00", "remind": [-1]}, "invalid_arguments",
     "remind is a list of whole minutes"),
    ({"at": "2026-10-13 10:00", "remind": "30"}, "invalid_arguments",
     "remind is a list of whole minutes"),
    ({"at": "2026-10-13 10:00", "remind": [True]}, "invalid_arguments",
     "remind is a list of whole minutes"),
    ({"at": "2026-10-13 10:00", "remind": [1.5]}, "invalid_arguments",
     "remind is a list of whole minutes"),
    ({"at": "2026-10-13 10:00", "remind": [5_000_000]}, "invalid_arguments", "ring '-5000000m'"),
    ({"kind": "stuff"}, "invalid_arguments", "kind must be go or do"),
    ({"weekly_until": "2026-11-03"}, "invalid_arguments", "need a start_at or an on_day"),
    ({"at": "2026-10-13 10:00", "weekly_until": "2026-10-01"}, "invalid_arguments",
     "before the first day"),
    ({"at": "2026-10-13 10:00", "weekly_until": "soon"}, "invalid_arguments", "is not a date"),
    ({"at": "2026-10-13 10:00", "private": "yes"}, "invalid_arguments",
     "private is true or false"),
    ({"what": "  "}, "empty_text", "add needs what"),
]


@pytest.mark.parametrize(("args", "code", "words"), REFUSED)
def test_a_bad_add_is_refused_with_the_fix_and_writes_nothing(
    make: Maker, args: dict[str, Any], code: str, words: str,
) -> None:
    """The error says what was wrong and what to pass instead; no thing, no ring."""
    world = make()
    refused = world.error(action="add", **{"what": "x", **args})
    assert refused["code"] == code
    assert words in refused["error"]
    assert refused["error"].startswith("schedule: ")
    assert world.nothing_written()


STRAY = [
    ("add", {"id": "abcd12"}, "id does not go with action=add; it goes with move or done"),
    ("add", {"occurrence": "2026-10-13"}, "occurrence does not go with action=add"),
    ("add", {"whole_sender": True}, "whole_sender does not go with action=add"),
    ("add", {"page": 2}, "page does not go with action=add; it goes with read"),
    ("read", {"kind": "go"}, "kind does not go with action=read; it goes with add"),
    ("read", {"remind": [5]}, "remind does not go with action=read"),
    ("done", {"at": "2026-10-13 09:00"}, "at does not go with action=done"),
    ("paid", {"due": "2026-10-13"}, "due does not go with action=paid"),
    ("cancel", {"remind": [5]}, "remind does not go with action=cancel"),
    ("move", {"whole_sender": True}, "whole_sender does not go with action=move"),
    ("move", {"private": True}, "private does not go with action=move; it goes with add"),
    ("reopen", {"whole_sender": True}, "whole_sender does not go with action=reopen"),
    ("confirm", {"occurrence": "2026-10-13"}, "occurrence does not go with action=confirm"),
]


@pytest.mark.parametrize(("action", "extra", "words"), STRAY)
def test_an_argument_that_does_not_go_with_the_action_is_refused_not_dropped(
    make: Maker, action: str, extra: dict[str, Any], words: str,
) -> None:
    """The model hears which argument was out of place and where it does go."""
    world = make()
    needs_id = {"id": "abcd12"} if action not in ("read", "add") else {}
    refused = world.error(action=action, what="x", **needs_id, **extra)
    assert refused["code"] == "invalid_arguments"
    assert words in refused["error"]
    assert world.nothing_written()


def test_empty_arguments_are_nothing_and_unknown_ones_are_named(make: Maker) -> None:
    """``""``, ``None`` and ``False`` are not given; an argument the tool lacks is named."""
    world = make()
    quiet = world.call(action="read", kind="", at="", id="", remind=None, private=False)
    assert quiet["summary"].startswith("Nothing on for today (Mon Oct 12) from 12:40 pm")
    unknown = world.error(bogus=1)
    assert unknown["code"] == "invalid_arguments"
    assert unknown["error"].startswith("schedule: no argument 'bogus'; use action, what, id,")
    wrong = world.error(action="remove", id="abcd12")
    assert "action must be one of read, add, move, done, paid, cancel, confirm, reopen" in (
        wrong["error"]
    )


# --- find, move, close -------------------------------------------------------------------------


def test_the_ids_a_read_gave_the_prefixes_and_the_day_lines_form_all_find_the_thing(
    make: Maker,
) -> None:
    """The short id, ``thing-``, the phone's ``commitment-<hex>@<day>`` (any case) are one thing."""
    world = make()
    key = world.call(action="add", what="call mum", at="2026-10-13 09:00")["id"]
    full = world.thing(key).thing_id
    for form in (key, key.upper(), full, f"commitment-{key}@2026-10-13", f"commitment-{key}"):
        out = world.call(action="reopen", id=form)
        assert out["summary"] == 'Already open, nothing changed: "call mum".'
        assert out["id"] == key


def test_an_id_that_names_nothing_or_too_much_is_refused_with_how_to_find_it(
    make: Maker,
) -> None:
    """Unknown, too short, absent and ambiguous ids each say what to do next; nothing changes."""
    world = make()
    world.seed("first", thing_id="thing-aaaa1111")
    world.seed("second", thing_id="thing-aaaa2222")
    cases = {
        "zzzz9": ("not_found", "no thing starts with 'zzzz9'; schedule() lists what is open"),
        "ab": ("not_found", "'ab' is too short: give at least 4 characters of the id"),
        "aaaa": ("not_found", "'aaaa' starts 2 things; give more of the id"),
        "": ("invalid_arguments", "done needs id, the short id a read or add gave"),
    }
    for given, (code, words) in cases.items():
        refused = world.error(action="done", id=given)
        assert refused["code"] == code
        assert words in refused["error"]
    assert world.count("thing.closed") == 0


def test_a_move_gives_the_new_time_and_keeps_the_one_it_had(make: Maker) -> None:
    """Time, length, rings, place and end each move as asked; the old time stays in its record."""
    world = make()
    made = world.call(
        action="add", what="Dentist", kind="go", at="2026-10-13 15:30",
        ends="2026-10-13 16:30", place="Smile Clinic",
    )
    key = made["id"]
    assert world.rings() == ["2026-10-13T14:30:00-07:00"]
    moved = world.call(action="move", id=key, at="2026-10-14 10:00")
    assert moved["summary"] == (
        'Moved "Dentist" for Wed Oct 14, 10:00 am (it was for tomorrow (Tue) 3:30 pm).'
    )
    one = world.thing(key)
    assert (one.start_at, one.end_at) == ("2026-10-14T10:00:00-07:00", "2026-10-14T11:00:00-07:00")
    assert [(m.start_at, m.end_at) for m in one.moves] == [
        ("2026-10-13T15:30:00-07:00", "2026-10-13T16:30:00-07:00"),
    ]
    assert world.rings() == ["2026-10-14T09:00:00-07:00"]  # the old ring is gone, the new stands

    ends = world.call(action="move", id=key, ends="2026-10-14 12:00")
    assert ends["summary"] == 'Changed "Dentist"; it now ends Wed Oct 14, 12:00 pm.'
    assert world.thing(key).end_at == "2026-10-14T12:00:00-07:00"
    link = world.call(action="move", id=key, place="https://maps.example/clinic")
    assert link["summary"] == 'Changed "Dentist".'
    assert (world.thing(key).place, world.thing(key).link) == ("", "https://maps.example/clinic")
    ring = world.call(action="move", id=key, remind=[15])
    assert ring["summary"] == 'Changed "Dentist"; rings 15 min before.'
    assert world.rings() == ["2026-10-14T09:45:00-07:00"]
    none = world.call(action="move", id=key, remind=[])
    assert none["summary"] == 'Changed "Dentist".'
    assert world.rings() == []


def test_a_move_to_a_new_kind_of_time_rings_the_way_that_time_does(make: Maker) -> None:
    """No time to a clock time rings; a clock time to a due stops ringing; a day to a clock time."""
    world = make()
    key = world.call(action="add", what="Renew library card")["id"]
    timed = world.call(action="move", id=key, at="2026-10-13 09:00")
    assert timed["summary"] == (
        'Moved "Renew library card" for tomorrow (Tue) 9:00 am (it was with no time);'
        " rings at the time."
    )
    assert world.rings() == ["2026-10-13T09:00:00-07:00"]
    due = world.call(action="move", id=key, due="2026-10-21")
    assert due["summary"] == (
        'Moved "Renew library card", due Wed Oct 21 (it was for tomorrow (Tue) 9:00 am).'
    )
    assert (world.thing(key).rings, world.rings()) == ((), [])
    day = world.call(action="add", what="Open day", kind="go", at="2026-10-15")["id"]
    world.call(action="move", id=day, at="2026-10-15 10:00")
    assert world.thing(day).rings == ("-60m",)
    assert world.rings() == ["2026-10-15T09:00:00-07:00"]


def test_a_move_that_cannot_be_made_is_refused_and_changes_nothing(make: Maker) -> None:
    """Nothing to change, a past time, a closed thing, an end with no start, a ring from nowhere."""
    world = make()
    key = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:30")["id"]
    plain = world.call(action="add", what="buy milk")["id"]
    refusals = [
        ({"id": key}, "invalid_arguments", "move needs the new time (at, ends or due)"),
        ({"id": key, "at": "2026-10-11 10:00"}, "time_in_past", "is in the past"),
        ({"id": plain, "ends": "2026-10-13 10:00"}, "invalid_arguments", "has no clock time"),
        ({"id": plain, "remind": [30]}, "invalid_arguments", "remind needs a time to ring from"),
        (
            {"id": key, "at": "2026-10-14 10:00", "due": "2026-10-15"}, "invalid_arguments",
            "not both",
        ),
    ]
    for args, code, words in refusals:
        refused = world.error(action="move", **args)
        assert (refused["code"], words in refused["error"]) == (code, True), refused
    assert world.thing(key).start_at == "2026-10-13T15:30:00-07:00"
    assert world.thing(key).moves == ()
    world.call(action="done", id=key)
    closed = world.error(action="move", id=key, at="2026-10-14 10:00")
    assert closed["code"] == "closed"
    assert 'is done, so it cannot be moved; schedule(action="reopen", id=...)' in closed["error"]


def test_done_paid_cancel_and_reopen_say_what_happened_and_change_nothing_twice(
    make: Maker,
) -> None:
    """Each close is one sentence; a repeat or another close of a closed thing changes nothing."""
    world = make()
    key = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:30")["id"]
    ring = ["2026-10-13T14:30:00-07:00"]
    assert world.rings() == ring
    steps_ = [
        ({"action": "done"}, 'Marked done: "Dentist".', "done", []),
        ({"action": "done"}, 'Already done, nothing changed: "Dentist".', "done", []),
        (
            {"action": "paid"},
            '"Dentist" is done already, nothing changed; reopen it first to mark it paid.',
            "done", [],
        ),
        ({"action": "reopen"}, 'Reopened "Dentist", it was done.', "open", ring),
        ({"action": "reopen"}, 'Already open, nothing changed: "Dentist".', "open", ring),
        ({"action": "paid"}, 'Marked paid: "Dentist".', "paid", []),
        ({"action": "reopen"}, 'Reopened "Dentist", it was paid.', "open", ring),
        (
            {"action": "cancel", "what": "the clinic phoned"}, 'Cancelled "Dentist".',
            "cancelled", [],
        ),
        ({"action": "cancel"}, 'Already cancelled, nothing changed: "Dentist".', "cancelled", []),
        ({"action": "reopen"}, 'Reopened "Dentist", it was cancelled.', "open", ring),
    ]
    for args, summary, state, rings in steps_:
        out = world.call(id=key, **args)
        assert (out["summary"], out["state"]) == (summary, state)
        assert (world.thing(key).state, world.rings()) == (state, rings)
    assert world.count("thing.closed") == 3
    assert [s.why for s in world.thing(key).history if s.what == "cancelled"] == [
        "the clinic phoned",
    ]


def test_a_weekly_thing_is_closed_moved_and_cancelled_one_date_at_a_time(make: Maker) -> None:
    """Done and paid take a date; a move or a cancel may take one; the whole series cancels once."""
    world = make()
    key = world.call(
        action="add", what="Seminar", kind="go", at="2026-10-13 10:00", ends="2026-10-13 11:30",
        weekly_until="2026-11-03",
    )["id"]
    assert world.rings() == ["2026-10-13T09:00:00-07:00"]
    whole = world.error(action="done", id=key)
    assert whole["code"] == "invalid_arguments"
    assert "repeats every week: say which date with occurrence, e.g. 2026-10-13" in whole["error"]
    done = world.call(action="done", id=key, occurrence="2026-10-13")
    assert done["summary"] == 'Marked done: "Seminar" on 2026-10-13.'
    assert world.rings() == []
    assert world.call(action="done", id=key, occurrence="2026-10-13")["summary"] == (
        'Already done, nothing changed: "Seminar" on 2026-10-13.'
    )
    assert world.call(action="paid", id=key, occurrence="2026-10-13")["summary"] == (
        '"Seminar" on 2026-10-13 is done already, nothing changed; reopen it first to mark it paid.'
    )
    day = world.call(at="2026-10-13")
    assert _rows(day) == [{
        "what": "Seminar", "when": "tomorrow (Tue) 10:00-11:30 am", "kind": "go",
        "state": "done", "occurrence": "2026-10-13",
    }]
    not_a_date = world.error(action="done", id=key, occurrence="2026-10-14")
    assert "'2026-10-14' is not one of the occurrences of 'Seminar'" in not_a_date["error"]

    moved = world.call(action="move", id=key, occurrence="2026-10-20", at="2026-10-21 13:00")
    assert moved["summary"] == 'Moved "Seminar" on 2026-10-20 for Wed Oct 21, 1:00 pm.'
    span = world.call(at="2026-10-20", to="2026-10-22")
    assert _rows(span) == [{
        "what": "Seminar", "when": "Wed Oct 21 1:00-2:30 pm", "kind": "go",
        "occurrence": "2026-10-20", "rings": "Wed Oct 21, 12:00 pm",
    }]
    assert world.call(action="cancel", id=key, occurrence="2026-10-27")["summary"] == (
        'Cancelled "Seminar" on 2026-10-27.'
    )
    assert world.call(at="2026-10-27")["items"] == []
    assert world.call(action="reopen", id=key, occurrence="2026-10-13")["summary"] == (
        'Reopened "Seminar" on 2026-10-13, it was done.'
    )
    assert world.rings() == ["2026-10-13T09:00:00-07:00"]
    series = world.call(action="cancel", id=key)
    assert series["summary"] == 'Cancelled "Seminar", the whole series.'
    assert (world.call(at="2026-10-13")["items"], world.rings()) == ([], [])


def test_the_day_lines_id_of_a_weekly_thing_names_the_date_it_is_on(make: Maker) -> None:
    """``commitment-<hex>@<day>`` closes or moves that date; an occurrence given wins."""
    world = make()
    key = world.call(
        action="add", what="Seminar", kind="go", at="2026-10-13 10:00", weekly_until="2026-11-03",
    )["id"]
    done = world.call(action="done", id=f"commitment-{key}@2026-10-20")
    assert done["summary"] == 'Marked done: "Seminar" on 2026-10-20.'
    moved = world.call(action="move", id=f"commitment-{key}@2026-10-27", at="2026-10-28 11:00")
    assert moved["summary"] == 'Moved "Seminar" on 2026-10-27 for Wed Oct 28, 11:00 am.'
    cancelled = world.call(
        action="cancel", id=f"commitment-{key}@2026-10-20", occurrence="2026-11-03",
    )
    assert cancelled["summary"] == 'Cancelled "Seminar" on 2026-11-03.'
    once = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:30")["id"]
    ended = world.call(action="done", id=f"commitment-{once}@2026-10-13")
    assert ended["summary"] == 'Marked done: "Dentist".'


# --- confirm and the sender's rule -------------------------------------------------------------


def test_confirm_makes_a_mails_thing_his_and_the_whole_sender_sets_the_rule(make: Maker) -> None:
    """A thing a mail made is marked until he says it is his; the rule keeps or drops its sender."""
    world = make()
    parcel = world.seed(
        "Parcel pickup", "go", by="mail", source="mail", confirmed=False,
        sender="shop@example.com", start_at="2026-10-13T11:00:00-07:00",
    )
    marked = world.call(at="2026-10-13")
    assert marked["summary"] == (
        "tomorrow (Tue Oct 13): 1 scheduled. Next: Parcel pickup (from a mail, not confirmed yet),"
        " tomorrow (Tue) 11:00 am. 1 from a mail, not confirmed yet."
    )
    assert _rows(marked) == [{
        "what": "Parcel pickup (from a mail, not confirmed yet)", "when": "tomorrow (Tue) 11:00 am",
        "kind": "go", "confirmed": False,
    }]
    assert world.call(action="confirm", id=parcel)["summary"] == 'Confirmed "Parcel pickup".'
    assert world.thing(parcel).confirmed
    assert world.call(action="confirm", id=parcel)["summary"] == (
        'Already confirmed, nothing changed: "Parcel pickup".'
    )
    assert _rows(world.call(at="2026-10-13"))[0]["what"] == "Parcel pickup"

    bill = world.seed(
        "Invoice 42", by="mail", source="mail", confirmed=False, sender="bill@example.com",
        due="2026-10-30",
    )
    always = world.call(action="confirm", id=bill, whole_sender=True)
    assert always["summary"] == (
        'Confirmed "Invoice 42". Later mail from bill@example.com will always count.'
    )
    spam = world.seed(
        "Spam meeting", "go", by="mail", source="mail", confirmed=False,
        sender="spam@example.com", start_at="2026-10-14T11:00:00-07:00",
    )
    never = world.call(action="cancel", id=spam, whole_sender=True)
    assert never["summary"] == (
        'Cancelled "Spam meeting" (from a mail, not confirmed). Later mail from'
        " spam@example.com will never count."
    )
    assert commitments.sender_rules(world.conn) == {
        "bill@example.com": "always", "spam@example.com": "never",
    }
    assert world.thing(spam).state == "cancelled"


def test_a_sender_rule_needs_a_mail_and_a_said_thing_is_already_confirmed(make: Maker) -> None:
    """``whole_sender`` on something he said is refused; confirming it changes nothing."""
    world = make()
    key = world.call(action="add", what="call mum")["id"]
    for action in ("cancel", "confirm"):
        refused = world.error(action=action, id=key, whole_sender=True)
        assert refused["code"] == "invalid_arguments"
        assert "did not come from a mail, so it has no sender to remember" in refused["error"]
    assert world.call(action="confirm", id=key)["summary"] == (
        'Already confirmed, nothing changed: "call mum".'
    )
    assert commitments.sender_rules(world.conn) == {}
    assert world.thing(key).state == "open"


def test_a_read_says_the_rings_the_add_set_and_which_of_them_rang(make: Maker) -> None:
    """10-19: an add said 12:00 would ring, the read did not say it, and she took it back.

    A read gives each time of a thing the rings the ledger holds for it: still to ring, rang,
    or passed unrung.
    """
    world = make()
    made = world.call(action="add", what="练车", kind="go", at="2026-10-13 13:00", remind=[60])
    assert "rings 60 min before" in made["summary"]
    weekly = world.call(
        action="add", what="Pottery", kind="go", at="2026-10-13 19:00",
        weekly_until="2026-10-20", remind=[30],
    )["id"]
    week = _rows(world.call(at="2026-10-13", to="2026-10-20"))
    assert [(row["what"], row.get("rings")) for row in week] == [
        ("练车", "tomorrow (Tue), 12:00 pm"),
        ("Pottery", "tomorrow (Tue), 6:30 pm"),
        ("Pottery", "Tue Oct 20, 6:30 pm"),
    ]
    (ring,) = [
        r for r in reminder_state.pending(world.conn)
        if r.due_at_local == "2026-10-13T12:00:00-07:00"
    ]
    fired = datetime(2026, 10, 13, 12, 0, 5, tzinfo=LOCAL)
    emit_event(
        world.conn, type="reminder.fired", ts_epoch_ms=int(fired.timestamp() * 1000),
        payload={
            "reminder_id": ring.reminder_id, "fired_at": int(fired.timestamp() * 1000),
            "late_ms": 5000, "delivered": "speak",
        },
    )
    world.conn.commit()
    world.now = datetime(2026, 10, 13, 18, 45, tzinfo=LOCAL)  # Pottery's 18:30 ring went by unrung
    day = _rows(world.call(at="2026-10-13"))
    assert [(row["what"], row.get("rings")) for row in day] == [
        ("练车", "today (Tue) 12:00 pm (rang)"),
        ("Pottery", "today (Tue) 6:30 pm (passed, did not ring)"),
    ]
    assert world.thing(weekly).rings == ("-30m",)


def test_a_private_thing_is_only_its_time_to_the_read_the_search_and_the_ring(
    make: Maker,
) -> None:
    """His words are kept; what comes back to the model, the search and the ring is its time."""
    world = make()
    made = world.call(
        action="add", what="Therapy with Dr Lee", kind="go", at="2026-10-13 18:00",
        place="Oak Street clinic", private=True,
    )
    assert world.thing(made["id"]).private
    assert _rows(world.call(at="2026-10-13")) == [{
        "what": "something private", "when": "tomorrow (Tue) 6:00 pm", "kind": "go",
        "rings": "tomorrow (Tue), 5:00 pm",
    }]
    assert world.call(what="therapy")["items"] == []
    assert world.call(what="oak")["items"] == []
    assert _rows(world.call(what="private")) == _rows(world.call(at="2026-10-13"))
    (ring,) = [
        r.text for r in reminder_state.pending(world.conn)
        if r.reminder_id.startswith(things.RING_PREFIX)
    ]
    assert ring == "18:00 you have something on"


# --- the read ----------------------------------------------------------------------------------


def _busy(world: _World) -> None:
    """A Monday at 12:40: late things, one under way, evening plans, to-dos, bills, a weekly."""
    world.seed("Missed call", start_at="2026-10-12T09:00:00-07:00")
    world.seed(
        "Ended meeting", "go", start_at="2026-10-12T10:00:00-07:00",
        end_at="2026-10-12T11:00:00-07:00",
    )
    world.seed(
        "Under way", "go", start_at="2026-10-12T12:00:00-07:00", end_at="2026-10-12T13:30:00-07:00",
    )
    world.seed("Yesterday chore", on_day="2026-10-11")
    world.seed("Rent late", due="2026-10-10")
    world.seed("Report due tonight", due="2026-10-12T23:00:00-07:00")
    world.seed("Tax", due="2026-10-13")
    world.seed("Far bill", due="2026-12-01")
    for n in range(3):
        world.seed(f"memo {n}")
    world.seed("Old memo with place", place="Library")
    world.seed("Dinner out", "go", start_at="2026-10-12T19:00:00-07:00", place="Pizza place")
    world.seed(
        "Weekly yoga", "go", start_at="2026-10-05T18:00:00-07:00", weekly_until="2026-11-30",
    )
    world.seed("Trip", "go", on_day="2026-11-20", note="bring passport")


def test_no_arguments_is_today_from_now_and_tomorrow_then_late_due_and_waiting(
    make: Maker,
) -> None:
    """Timed first, then what is overdue, due and without a time; eight to a page."""
    world = make()
    _busy(world)
    one = world.call()
    assert one["summary"] == (
        "today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13): 3 scheduled, 4 overdue,"
        " 2 due, 4 with no time. Next: Weekly yoga, today (Mon) 6:00 pm, in 5 h 20 min."
        " Showing 1-8; page=2 for the rest."
    )
    assert _rows(one) == [
        {"what": "Under way", "when": "today (Mon) 12:00-1:30 pm, now", "kind": "go"},
        {
            "what": "Weekly yoga", "when": "today (Mon) 6:00 pm, in 5 h 20 min", "kind": "go",
            "occurrence": "2026-10-12",
        },
        {"what": "Dinner out", "when": "today (Mon) 7:00 pm", "kind": "go", "place": "Pizza place"},
        {"what": "Rent late", "when": "due Sat Oct 10", "kind": "do", "overdue": True},
        {"what": "Yesterday chore", "when": "yesterday (Sun), all day", "kind": "do",
         "overdue": True},
        {"what": "Missed call", "when": "today (Mon) 9:00 am", "kind": "do", "overdue": True},
        {"what": "Ended meeting", "when": "today (Mon) 10:00-11:00 am", "kind": "go",
         "overdue": True},
        {"what": "Report due tonight", "when": "due today (Mon) 11:00 pm", "kind": "do"},
    ]
    assert one["more"] is True
    two = world.call(page=2)
    assert two["summary"] == (
        "today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13): items 9-13 of 13"
        " (3 scheduled, 4 overdue, 2 due, 4 with no time)."
    )
    assert _rows(two) == [
        {"what": "Tax", "when": "due tomorrow (Tue)", "kind": "do"},
        {"what": "memo 0", "kind": "do"},
        {"what": "memo 1", "kind": "do"},
        {"what": "memo 2", "kind": "do"},
        {"what": "Old memo with place", "kind": "do", "place": "Library"},
    ]
    assert two["more"] is False
    assert world.call(page=3)["summary"] == (
        "There is no page 3 of today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13);"
        " its items end at page 2."
    )
    assert all("id" in row for row in one["items"] + two["items"])


@pytest.mark.parametrize(
    ("args", "label"),
    [
        ({"at": "today"}, "today (Mon Oct 12)"),
        ({"at": "yesterday"}, "yesterday (Sun Oct 11)"),
        ({"at": "tomorrow"}, "tomorrow (Tue Oct 13)"),
        ({"at": "Fri"}, "Fri Oct 16"),
        ({"at": "next fri"}, "Fri Oct 16"),
        ({"at": "Mon"}, "Mon Oct 19"),  # the next Monday, not today
        ({"at": "2026-10-15"}, "Thu Oct 15"),
        ({"at": "2026-10-15T09:30:00"}, "Thu Oct 15"),
        ({"at": "Fri", "to": "Sun"}, "Fri Oct 16 to Sun Oct 18"),
        ({"at": "Sat", "to": "Mon"}, "Sat Oct 17 to Mon Oct 19"),
        ({"at": "2026-10-12", "to": "2026-10-25"}, "today (Mon Oct 12) to Sun Oct 25"),
        ({"to": "Wed"}, "today (Mon Oct 12) to Wed Oct 14"),
    ],
)
def test_a_day_or_a_span_is_named_the_way_he_says_it(
    make: Maker, args: dict[str, Any], label: str,
) -> None:
    """An empty ledger still says what was looked at, and how to look elsewhere."""
    world = make()
    out = world.call(**args)
    assert out["summary"] == (
        f"Nothing on for {label}, nothing overdue and nothing waiting without a time. Ask for"
        " another day with at (and to for a span of up to 14 days), or search words with what."
    )
    assert (out["items"], out["more"]) == ([], False)


def test_a_day_shows_what_is_on_it_what_is_done_and_leaves_out_what_was_cancelled(
    make: Maker,
) -> None:
    """The window decides the timed rows; late and waiting things come with every day asked."""
    world = make()
    world.seed("Dentist", "go", start_at="2026-10-14T08:00:00-07:00", place="Smile Clinic")
    done = world.seed("Standup", "go", start_at="2026-10-14T09:00:00-07:00")
    gone = world.seed("Cancelled call", start_at="2026-10-14T10:00:00-07:00")
    world.seed("Team lunch", "go", start_at="2026-10-15T12:00:00-07:00")
    world.seed("call mum")
    world.call(action="done", id=done)
    world.call(action="cancel", id=gone)
    out = world.call(at="Wed")
    assert out["summary"] == (
        "Wed Oct 14: 2 scheduled, 1 with no time. Next: Dentist, Wed Oct 14 8:00 am."
        ' No time set: "call mum".'
    )
    assert _rows(out) == [
        {"what": "Dentist", "when": "Wed Oct 14 8:00 am", "kind": "go", "place": "Smile Clinic"},
        {"what": "Standup", "when": "Wed Oct 14 9:00 am", "kind": "go", "state": "done"},
        {"what": "call mum", "kind": "do"},
    ]
    both = world.call(at="Wed", to="Thu")
    assert both["summary"].startswith("Wed Oct 14 to Thu Oct 15: 3 scheduled, 1 with no time.")
    assert [row["what"] for row in both["items"]] == [
        "Dentist", "Standup", "Team lunch", "call mum",
    ]


def test_only_the_day_shown_has_nothing_scheduled_when_only_late_or_waiting_things_remain(
    make: Maker,
) -> None:
    """A day with nothing on it says so before it lists what is late or waiting."""
    world = make()
    world.seed("Rent late", due="2026-10-10")
    world.seed("call mum")
    out = world.call(at="Fri")
    assert out["summary"] == (
        'Fri Oct 16: nothing scheduled, 1 overdue, 1 with no time. No time set: "call mum".'
    )


def test_a_search_reads_titles_places_and_notes_over_everything_open_and_sixty_days(
    make: Maker,
) -> None:
    """Every word must be in one thing; a weekly one shows its dates in the window."""
    world = make()
    _busy(world)
    memos = world.call(what="memo")
    assert memos["summary"] == (
        '"memo" among everything open and the next 60 days: 4 with no time.'
        ' No time set: "memo 0", "memo 1", "memo 2", "Old memo with place".'
    )
    assert [row["what"] for row in memos["items"]] == [
        "memo 0", "memo 1", "memo 2", "Old memo with place",
    ]
    assert _rows(world.call(what="PASSPORT")) == [
        {"what": "Trip", "when": "Fri Nov 20, all day", "kind": "go"},
    ]
    assert [row["what"] for row in world.call(what="pizza")["items"]] == ["Dinner out"]
    assert [row["what"] for row in world.call(what="library")["items"]] == ["Old memo with place"]
    yoga = world.call(what="yoga")
    assert [row["occurrence"] for row in yoga["items"]] == [
        "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09", "2026-11-16",
        "2026-11-23", "2026-11-30",
    ]
    assert world.call(what="far bill")["items"][0]["when"] == "due Tue Dec 1"
    assert world.call(what="library pizza")["items"] == []  # no one thing holds both words
    nothing = world.call(what="nothing like this")
    assert nothing["summary"] == (
        'Nothing matches "nothing like this" among everything open and the next 60 days.'
        " Try fewer words, or ask for a day with at (and to for up to 14 days)."
    )


def test_a_search_finds_a_thing_past_sixty_days_by_its_first_time(make: Maker) -> None:
    """What is open and still to come is found even beyond the window, once."""
    world = make()
    world.seed("Passport renewal", "go", start_at="2027-03-01T10:00:00-08:00")
    found = world.call(what="passport")
    assert _rows(found) == [
        {"what": "Passport renewal", "when": "Mon Mar 1 2027 10:00 am", "kind": "go"},
    ]


READ_REFUSED = [
    ({"at": "soon"}, "a day looks like today, tomorrow, Mon or 2026-10-15, not 'soon'"),
    ({"at": "2026-13-45"}, "not '2026-13-45'"),
    ({"at": "2026-10-20", "to": "2026-10-19"}, "to=2026-10-19 is before at=2026-10-20"),
    ({"at": "2026-10-12", "to": "2026-10-26"}, "a span is at most 14 days"),
    ({"what": "x", "at": "today"}, "what searches everything open and the next 60 days"),
    ({"what": "x", "to": "Fri"}, 'To keep a new thing, pass action="add"'),
    ({"page": 0}, "page must be a whole number, e.g. page=2 for items 9-16"),
    ({"page": "two"}, "page must be a whole number, e.g. page=2 for items 9-16"),
]


@pytest.mark.parametrize(("args", "words"), READ_REFUSED)
def test_a_bad_read_is_refused_with_what_to_pass_instead(
    make: Maker, args: dict[str, Any], words: str,
) -> None:
    """The error says what was wrong and the shape that works."""
    refused = make().error(**args)
    assert refused["code"] == "invalid_arguments"
    assert words in refused["error"]


def test_a_day_that_is_twenty_five_hours_long_holds_both_ends_of_it(make: Maker) -> None:
    """The clocks go back on Nov 1: a thing at 12:30 am and one at 11:30 pm are both that day."""
    world = make()
    early = world.call(action="add", what="Early", kind="go", at="2026-11-01 00:30")["id"]
    late = world.call(action="add", what="Late", kind="go", at="2026-11-01 23:30")["id"]
    assert (world.thing(early).start_at, world.thing(late).start_at) == (
        "2026-11-01T00:30:00-07:00", "2026-11-01T23:30:00-08:00",
    )
    day = world.call(at="2026-11-01")
    assert [(row["what"], row["when"]) for row in day["items"]] == [
        ("Early", "Sun Nov 1 12:30 am"), ("Late", "Sun Nov 1 11:30 pm"),
    ]
    assert world.call(at="2026-10-31")["items"] == []


def test_a_page_stays_under_four_thousand_characters_and_links_give_way_before_rows(
    make: Maker,
) -> None:
    """Eight long rows with long links pass 4000; the last rows lose their links first."""
    world = make()
    for n in range(10):
        world.seed(
            f"Meeting {n} " + "very long title " * 12, "go",
            start_at=f"2026-10-13T{9 + n:02d}:00:00-07:00", place="Room " + "a" * 100,
            link="https://example.com/join/" + "b" * 270,
        )
    out = world.call(at="2026-10-13")  # _dispatch asserts the 4000
    assert len(out["items"]) == 8
    assert out["more"] is True
    assert "link" in out["items"][0]
    assert "link" not in out["items"][-1]
    assert all(len(row["what"]) <= 120 for row in out["items"])


def test_a_pinned_bus_is_one_row_on_its_leave_time_among_the_things(make: Maker) -> None:
    """ADR 0246: the bus he pinned is listed once, in time order, without an id."""
    world = make()
    leave = datetime(2026, 10, 12, 13, 5, tzinfo=LOCAL)
    ring = reminder_state.schedule(
        world.conn, due=leave, text="Leave for the 28 now", action_id="R-ring",
    )
    trip = departures.Trip(
        "t1", "28 → 12", "Hillside Mall", int(leave.timestamp() * 1000),
        int((leave + timedelta(minutes=8)).timestamp() * 1000), "1:40 pm", None, None, "UVic",
    )
    departures.pin(
        world.conn, reminder_id=ring, ring="t1", trips=[trip], offer_id=None, action_id="P1",
    )
    world.seed("Water the plants", start_at="2026-10-12T18:00:00-07:00")
    out = world.call()
    assert _rows(out) == [
        {
            "what": "bus 28 → 12 from Hillside Mall to UVic",
            "when": "leave today (Mon) 1:05 pm, in 25 min", "kind": "bus",
        },
        {"what": "Water the plants", "when": "today (Mon) 6:00 pm, in 5 h 20 min", "kind": "do"},
    ]
    assert "id" not in out["items"][0]
    assert out["summary"].startswith(
        "today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13): 2 scheduled. Next: bus 28",
    )
    assert world.call(at="tomorrow")["items"] == []  # the bus leaves today
    assert [row["kind"] for row in world.call(what="bus")["items"]] == ["bus"]


# --- Tier 0 ------------------------------------------------------------------------------------


def test_note_and_notelist_run_the_one_tool(make: Maker) -> None:
    """`/note` adds a to-do with no time and `/notelist` reads what is open, with no model."""
    world = make()
    table = load_tier0_table(REPO / "config" / "tier0_patterns.yaml")
    regex_tools = build_default_registry().for_caller(ROUTER)  # what the boot validates against
    validate_tier0_table(
        tuple(row for row in table if row.pattern_id in {"note_capture", "note_list"}),
        allowed_tool_names=frozenset(t.name for t in regex_tools),
    )
    note = match_tier0("/note 买牛奶", table)
    assert note is not None
    assert (note.tool_name, note.tool_args) == ("schedule", {"action": "add", "what": "买牛奶"})
    saved = world.run(ROUTER, note.tool_args)
    assert saved["summary"] == f'Kept "买牛奶" with no time (id {saved["id"]}).'
    assert render_tier0_response(note, saved, "en") == "Noted."
    assert world.run(ROUTER, note.tool_args)["summary"].startswith("Already kept, nothing new")
    listing = match_tier0("/notelist", table)
    assert listing is not None
    assert (listing.tool_name, listing.tool_args) == ("schedule", {})
    payload = world.run(ROUTER, listing.tool_args)
    assert render_tier0_response(listing, payload, "en") == (
        'today (Mon Oct 12) from 12:40 pm and tomorrow (Tue Oct 13): nothing scheduled,'
        ' 1 with no time. No time set: "买牛奶".'
    )


# --- the menu ----------------------------------------------------------------------------------


def test_the_default_registry_has_the_three_and_none_of_the_names_they_replaced() -> None:
    """Menu and router reach ``schedule``, ``keep`` and ``kept``; the retired names are gone."""
    registry = build_default_registry()
    seen = {t.name for t in registry.get_definitions()}
    assert {"schedule", "keep", "kept"} <= seen
    assert not seen & OLD
    routed = {t.name for t in registry.for_caller(ROUTER)}
    assert {"schedule", "kept"} <= routed
    assert "keep" not in routed


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


def test_the_menu_offers_schedule_and_not_the_microsoft_reads_it_replaces(tmp_path: Path) -> None:
    """The shipped config: the Microsoft reads are deferred and tool_search points to the tool."""
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    recorded = _recorded(tmp_path)
    menu = tool_exam.offered_tools(config, recorded)
    names = [one["name"] for one in menu]
    assert {"schedule", "keep", "kept"} <= set(names)
    assert not set(names) & OLD
    assert not [n for n in names if n.startswith("mcp__microsoft__")]
    sizes = {
        one["name"]: len(one["description"]) + len(json.dumps(one["input_schema"]))
        for one in menu if one["name"] in {"schedule", "keep", "kept"}
    }
    assert sizes["schedule"] < 3200
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001 - the registry behind the menu
    held = {t.name: t for t in registry.get_definitions()}
    for name in ("get-calendar-view", "list-todo-task-lists", "list-todo-tasks"):
        assert held[f"mcp__microsoft__{name}"].deferred
    assert held["mcp__microsoft__create-todo-task"].deferred
    assert {k: v for k, v in registry.superseded().items() if v[0] == "schedule"} == {
        "create_memo": ("schedule", 'schedule(action="add", what=...)'),
        "set_reminder": ("schedule", 'schedule(action="add", what=..., at=...)'),
        "cancel_reminder": ("schedule", 'schedule(action="cancel", id=...)'),
        "list_memos": ("schedule", "schedule()"),
        "list_reminders": ("schedule", "schedule()"),
        "my_schedule": ("schedule", "schedule(at=..., to=...)"),
        "mcp__microsoft__get-calendar-view": ("schedule", "schedule(at=..., to=...)"),
        "mcp__microsoft__list-todo-task-lists": ("schedule", "schedule()"),
        "mcp__microsoft__list-todo-tasks": ("schedule", "schedule(at=..., to=...)"),
    }
    search = held["tool_search"]
    assert isinstance(search, Tool)
    found = search.handler({"query": "calendar events"}, cast("ToolContext", None))
    assert "mcp__microsoft__get-calendar-view" not in found["loaded_tools"]
    assert "mcp__microsoft__list-todo-tasks" not in found["loaded_tools"]
    assert "schedule(at=..., to=...)" in found["note"]
    writes = search.handler({"query": "create a to-do task"}, cast("ToolContext", None))
    assert writes["loaded_tools"][0] == "mcp__microsoft__create-todo-task"
    assert "mcp__microsoft__update-todo-task" in writes["loaded_tools"]


def test_the_step_label_and_the_dispatch_line_say_calendar() -> None:
    """The phone's step and the spoken line are the calendar's; a write says it changes it."""
    calendar = steps.kind("mcp__microsoft__get-calendar-view")
    assert steps.kind("schedule") == steps.kind("schedule", {"action": "read"}) == calendar
    assert steps.kind("schedule", {"action": ""}) == steps.kind("schedule", '{"at": "Fri"}') == (
        calendar
    )
    edit = steps.kind("mcp__microsoft__create-event")
    for action in ("add", "move", "done", "paid", "cancel", "confirm", "reopen"):
        assert steps.kind("schedule", {"action": action}) == edit
    assert steps.kind("kept") == "records"


def test_the_booted_runtime_has_the_tool_and_not_the_old_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired by the runtime: resident for the model, the ledger read through the dispatcher."""
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
        assert {"schedule", "keep", "kept"} <= set(held)
        assert not set(held) & OLD
        assert not held["schedule"].deferred
        out = _dispatch(
            runtime.tool_registry, runtime.conn, runtime.runtime_paths, "schedule",
            {"at": "tomorrow"},
        )
        assert out["summary"].startswith("Nothing on for tomorrow (")
        assert out["items"] == []
        # The multi-step exam runs it on a sandbox copy of the log, like the other writes.
        assert tool_exam_steps.classify(runtime.tool_registry)["schedule"] == "sandbox_write"
    finally:
        runtime.conn.close()


def test_the_default_registry_gives_schedule_the_ring_lead_and_his_zone(tmp_path: Path) -> None:
    """``build_default_registry(ring_before_min=, clock=)`` reaches the tool's clock."""
    registry = build_default_registry(clock=FakeClock(NOW, zone=LOCAL), ring_before_min=15)
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    try:
        soon = (NOW + timedelta(days=2)).strftime("%Y-%m-%d")
        out = _dispatch(
            registry, conn, paths, "schedule",
            {"action": "add", "what": "Dentist", "kind": "go", "at": f"{soon} 15:30"},
        )
        assert things.find(conn, out["id"]).rings == ("-15m",)
        assert things.find(conn, out["id"]).start_at.endswith(("-07:00", "-08:00"))
    finally:
        conn.close()


def _receipt(world: _World, out: Mapping[str, Any]) -> dict[str, Any]:
    """The receipt the dispatcher kept for the call that answered ``out`` (ADR 0319)."""
    (receipt,) = [
        event.payload["receipt"]
        for event in iter_events_of_types(world.conn, ("action.result_observed",))
        if (event.correlation or {}).get("action_id") == out["_action_id"]
    ]
    return cast("dict[str, Any]", receipt)


def test_each_call_leaves_its_own_receipt(make: Maker) -> None:
    """A read says what it covered; a write names the thing and the event an undo reverses."""
    world = make()
    made = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:00")
    added = _receipt(world, made)
    (one,) = things.fold(world.conn).values()
    assert added == {
        "status": "done", "reads_only": False, "line": made["summary"],
        "object": f"thing:{made['id']}", "undo": one.history[-1].event,
    }
    assert one.history[-1].event

    again = world.call(action="add", what="Dentist", kind="go", at="2026-10-13 15:00")
    assert "undo" not in _receipt(world, again)  # kept already: nothing written, nothing to undo

    moved = world.call(action="move", id=made["id"], at="2026-10-13 16:00")
    (one,) = things.fold(world.conn).values()
    assert _receipt(world, moved)["undo"] == one.history[-1].event != added["undo"]

    for n in range(9):
        world.call(action="add", what=f"Errand {n}")
    read = world.call()
    receipt = _receipt(world, read)
    assert (receipt["status"], receipt["reads_only"]) == ("done", True)
    assert "object" not in receipt
    assert "undo" not in receipt
    assert receipt["covered"] == {
        "sources": ["ledger of his things", "pinned bus"],
        "window": receipt["covered"]["window"], "shown": 8, "more": 2, "how_more": "page=2",
    }
    assert receipt["line"].endswith(": 8 shown, 2 more (page=2)")
