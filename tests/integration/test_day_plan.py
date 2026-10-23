"""ADR 0288: the day's plan state, the nightly draft, its parser and its schedule.

Data-driven checks over a real event log and memory.db with a fake model and a fake weather reader
(no key, no network, no provider). The examples are invented.

- the fold: a draft, a revision that wins, a failed draft, the revisions he made; the settled and
  opened rows of old logs are ignored;
- the writers refuse an item that could not be read back;
- the parser: bad JSON, a collision with a fixed item, an unknown source, a fixed item the model
  left out, an item that repeats a fixed item;
- the inputs: the ledger's things (ADR 0299) as fixed items, upcoming ones, all-day labels and
  open tasks, goals, missing sources noted, and his earlier changes to plans as preferences, in
  the prompt too; a past plan with the old sources still loads;
- the morning and the evening prompts and their parsers (ADR 0385), a private item masked;
- the draft: success, every failure writes ``plan.draft_failed``, a day he changed is kept;
- the schedule: once at ``at``, and a catch-up on start.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision import day_plan as decide
from jarvis.runtime import day_plan as runtime
from jarvis.shared.skills import load_skill
from jarvis.state import core_memory, plan, things
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log, open_runtime_event_log
from jarvis.state.memory_db import open_memory_db, remember_fact
from jarvis.state.things_late import late

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from sqlite3 import Connection

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 4, 0, tzinfo=ZONE)
SETTINGS = runtime.DayPlanSettings(at=time(4, 0))


def _item(item_id: str = "p1", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    base: dict[str, Any] = {
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "goal",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


class Env:
    """A real event log and memory.db in a temp directory."""

    def __init__(self, root: Path) -> None:
        """Create both stores under ``root``."""
        self.log = root / "events.db"
        self.memory = root / "memory.db"
        open_event_log(self.log).close()
        open_memory_db(self.memory).close()

    def conn(self) -> Connection:
        """A connection to the event log."""
        return open_runtime_event_log(self.log)

    def sources(self, **readers: Any) -> runtime.DayPlanSources:  # noqa: ANN401
        """The runtime's readers over these stores, plus any fake ``readers``."""
        return runtime.DayPlanSources(
            self.log, self.memory, ZONE, DaySources(ZONE, None, self.memory), **readers,
        )

    def add(self, title: str, kind: str = "go", **fields: Any) -> str:  # noqa: ANN401
        """Note one thing in the ledger; its id."""
        one = things.Thing(things.new_id(), title, kind, **fields)
        with closing(self.conn()) as conn:
            things.add(conn, one, by="her", evidence="said so", zone=ZONE, now=NOW)
        return one.thing_id

    def commit(self, title: str, at: datetime, *, place: str = "", minutes: int = 60) -> str:
        """Note one thing he goes to at ``at``; its id."""
        end = (at + timedelta(minutes=minutes)).isoformat()
        return self.add(title, start_at=at.isoformat(), end_at=end, place=place)


@pytest.fixture
def stores(tmp_path: Path) -> Env:
    """The stores of one test."""
    return Env(tmp_path)


def _answer(*items: Any) -> str:  # noqa: ANN401
    return json.dumps({"items": list(items)})


def _model(title: str = "Write the report", **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    base = {
        "title": title, "start": "10:00", "minutes": 60, "firmness": "movable", "source": "goal",
        "why": "due soon", "prepare": [],
    }
    return base | fields


# --- the fold ------------------------------------------------------------------------------


def test_a_revision_wins_over_the_draft_and_a_failed_draft_folds_in(stores: Env) -> None:
    """The last items win; a failed draft folds in; old settled and opened rows are not read."""
    with closing(stores.conn()) as conn:
        assert plan.for_day(conn, DAY) is None
        plan.draft(conn, DAY, [_item("p1"), _item("p2", title="Walk", start="12:00")], "d1", "m")
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert (held.source, held.digest) == ("drafted", "d1")
        assert [i.item_id for i in held.items] == ["p1", "p2"]
        plan.revise(conn, DAY, [_item("p1", start="10:30")], "him", "later please")
        emit_event(conn, type="plan.settled", payload={"date": DAY.isoformat(), "by": "him"})
        emit_event(conn, type="plan.opened", payload={
            "date": DAY.isoformat(), "record_id": "plan-open-2026-10-16", "signal": "focus_off",
            "at_ms": 1_760_000_000_000,
        })
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert [(i.item_id, i.start) for i in held.items] == [("p1", "10:30")]
        assert held.source == "revised:him"
        other = DAY + timedelta(days=1)
        assert plan.for_day(conn, other) is None
        plan.draft_failed(conn, other, "TimeoutError: slow")
        assert plan.failed(conn, other) == "TimeoutError: slow"
        assert plan.failed(conn, DAY) is None
        assert plan.for_day(conn, other) is None


def test_a_later_draft_replaces_the_items(stores: Env) -> None:
    """A newer draft replaces the items."""
    with closing(stores.conn()) as conn:
        plan.draft(conn, DAY, [_item("p1")], "d1", "m")
        plan.draft(conn, DAY, [_item("p9", title="Other")], "d2", "m")
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert [i.item_id for i in held.items] == ["p9"]


def test_revisions_by_him_come_newest_first_with_what_changed(stores: Env) -> None:
    """His revisions inside the window, newest first, each with what it changed."""
    old, mid = DAY - timedelta(days=30), DAY - timedelta(days=2)
    with closing(stores.conn()) as conn:
        plan.draft(conn, mid, [_item("p1"), _item("p2", title="Walk", start="12:00")], "d", "m")
        plan.revise(conn, mid, [_item("p1", start="11:00")], "him", "I sleep in on Thursdays")
        plan.revise(conn, mid, [_item("p1", start="11:00"), _item("p3", title="Call")], "her", "x")
        plan.draft(conn, old, [_item("p1")], "d", "m")
        plan.revise(conn, old, [], "him", "long ago")
        plan.draft(conn, DAY, [_item("p1")], "d", "m")
        plan.revise(conn, DAY, [_item("p1", title="Write the plan")], "him", "better name")
        found = plan.revisions(conn, DAY - timedelta(days=14), DAY, by="him")
    assert [(r.day, r.why) for r in found] == [
        (DAY.isoformat(), "better name"), (mid.isoformat(), "I sleep in on Thursdays"),
    ]
    assert found[1].change == "- Walk 12:00; Write the report start: 09:00 -> 11:00"
    assert found[0].change == "Write the report -> Write the plan"


# --- the writers refuse what could not be read back ----------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"start": "9:00"}, {"start": "24:00"}, {"start": "09:60"}, {"window_from": "noon"},
        {"window_to": "25:00"}, {"firmness": "soft"}, {"minutes": 0}, {"minutes": 1441},
        {"title": ""}, {"title": "   "}, {"title": "x" * 121}, {"source": "mail"},
        {"source": "commitment:"}, {"source": "item:"}, {"item_id": ""},
    ],
)
def test_a_bad_item_is_refused_and_nothing_is_written(stores: Env, fields: dict[str, Any]) -> None:
    """An item that could not be read back is refused and leaves no event."""
    with closing(stores.conn()) as conn, pytest.raises(ValueError):  # noqa: PT011
        plan.draft(conn, DAY, [_item(**fields)], "d", "m")
    with closing(stores.conn()) as conn:
        assert plan.for_day(conn, DAY) is None


def test_a_valid_item_with_every_field_round_trips_and_other_writers_refuse(stores: Env) -> None:
    """Every field round-trips; the other writers refuse a bad value."""
    full = _item(
        "p1", start=None, window_from="14:00", window_to="17:00", place="Library", why="due",
        goal="read", prepare=("book", "charger"), source="item:thing-abc", private=True,
        route={"leave_at": "13:20", "line": "99"}, firmness="fixed", minutes=1440,
    )
    with closing(stores.conn()) as conn:
        plan.draft(conn, DAY, [full], "d", "m")
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert held.items == (full,)
        attempts: tuple[Callable[[], None], ...] = (
            lambda: plan.draft(conn, DAY, [_item("a"), _item("a")], "d", "m"),
            lambda: plan.revise(conn, DAY, [_item()], "someone", "x"),
            lambda: plan.say(conn, DAY, "morning", "noon", "Hi.", "plan-x", 1),
            lambda: plan.say(conn, DAY, "morning", "morning", " ", "plan-x", 1),
            lambda: plan.close_day(conn, DAY, [plan.Unfinished("p1", "Write", "never")]),
            lambda: plan.mute(conn, "noon", True),  # noqa: FBT003
        )
        for bad in attempts:
            with pytest.raises(ValueError):  # noqa: PT011
                bad()


# --- the parser ----------------------------------------------------------------------------

FIXED = (
    _item("c1", title="Dentist", firmness="fixed", source="item:thing-c1", start="14:00",
          minutes=60, place="Main St"),
)
KNOWN = {"goal", "his", "item:thing-c1"}


@pytest.mark.parametrize(
    ("text", "titles", "notes_with"),
    [
        pytest.param("I could not do it", [], ["not a JSON"], id="no-json"),
        pytest.param('{"items": "x"}', [], ["not a JSON"], id="items-not-a-list"),
        pytest.param("[1, 2]", [], ["not a JSON"], id="array"),
        pytest.param(
            f"```json\n{_answer(_model())}\n```", ["Write the report", "Dentist"], [], id="fenced",
        ),
        pytest.param(
            f"Here you go: {_answer(_model())} Enjoy.", ["Write the report", "Dentist"], [],
            id="prose-around",
        ),
        pytest.param(
            _answer(_model(start="14:30")), ["Dentist"], ["overlaps Dentist"], id="collision",
        ),
        pytest.param(
            _answer(_model(start="13:30", minutes=30)), ["Write the report", "Dentist"], [],
            id="touching-is-not-colliding",
        ),
        pytest.param(
            _answer(_model(source="routine:r9")), ["Dentist"], ["unknown source routine:r9"],
            id="source",
        ),
        pytest.param(
            _answer(_model(source="todo")), ["Dentist"], ["unknown source todo"],
            id="todo-is-no-source-any-more",
        ),
        pytest.param(
            _answer(_model(source="commitment:c1")), ["Dentist"], ["unknown source commitment:c1"],
            id="commitment-is-no-source-any-more",
        ),
        pytest.param(
            _answer(_model(start="9am"), _model("Walk"), {"title": "no firmness"}, "text"),
            ["Walk", "Dentist"], ["item 1", "item 3", "item 4"], id="unreadable-items",
        ),
        pytest.param(_answer(), ["Dentist"], [], id="fixed-item-added-back"),
        pytest.param(
            _answer(_model(title="Go to the dentist", source="item:thing-c1", start="14:00",
                           prepare=["insurance card"], why="six month check")),
            ["Dentist"], [], id="repeated-fixed-item-lends-prepare",
        ),
        pytest.param(
            _answer(_model(start=None, window_from="15:00", window_to="18:00")),
            ["Write the report", "Dentist"], [], id="window-without-start",
        ),
    ],
)
def test_the_parser_keeps_what_reads_and_adds_back_what_is_fixed(
    text: str, titles: list[str], notes_with: list[str],
) -> None:
    """Each shape of model answer gives the items it should, notes, and the fixed items back."""
    items, notes = decide.parse_items(text, known_sources=KNOWN, fixed=FIXED)
    assert [i.title for i in items] == titles
    assert [i.item_id for i in items] == [f"p{n}" for n in range(1, len(items) + 1)]
    for part in notes_with:
        assert any(part in note for note in notes), (part, notes)
    if "Dentist" in titles:
        dentist = next(i for i in items if i.source == "item:thing-c1")
        assert (dentist.firmness, dentist.start, dentist.place) == ("fixed", "14:00", "Main St")


def test_a_repeated_fixed_item_keeps_the_fixed_item_and_lends_why_and_prepare() -> None:
    """A model item for a thing already fixed keeps the fixed item and lends it why and prepare."""
    items, _ = decide.parse_items(
        _answer(_model(title="Other name", source="item:thing-c1", start="09:00",
                       prepare=["insurance card"], why="six month check")),
        known_sources=KNOWN, fixed=FIXED,
    )
    assert len(items) == 1
    assert (items[0].title, items[0].start, items[0].prepare, items[0].why) == (
        "Dentist", "14:00", ("insurance card",), "six month check",
    )


def test_a_model_item_that_says_his_does_not_take_a_calendar_items_place() -> None:
    """A model item sourced `his` is not mistaken for a fixed calendar item."""
    fixed = (_item("c", title="Standup", firmness="fixed", source="his", start="09:00"),)
    items, notes = decide.parse_items(
        _answer(_model("Invented", source="his", start="12:00")), known_sources=KNOWN, fixed=fixed,
    )
    assert [i.title for i in items] == ["Invented", "Standup"]  # the fixed one is added last
    assert notes == []


def test_the_models_order_is_the_plans_order_and_a_listed_fixed_item_keeps_its_place() -> None:
    """Nothing is sorted by time: tasks and timed items stay where the model listed them."""
    items, _ = decide.parse_items(
        _answer(
            _model("Last task", start=None),
            _model("Dentist", source="item:thing-c1", start="14:00"),
            _model("First task", start=None, source="goal"),
        ),
        known_sources=KNOWN, fixed=FIXED,
    )
    assert [(i.item_id, i.title) for i in items] == [
        ("p1", "Last task"), ("p2", "Dentist"), ("p3", "First task"),
    ]


def test_a_message_is_strict_json_and_short() -> None:
    """The morning's answer must be a JSON message of a sensible length."""
    assert decide.parse_message('{"message": " Morning. One thing first. "}') == (
        "Morning. One thing first."
    )
    for bad in ("Good morning", '{"message": ""}', '{"text": "x"}', '{"message": 3}',
                json.dumps({"message": "x" * (decide.MESSAGE_CHARS + 1)})):
        with pytest.raises(ValueError):  # noqa: PT011
            decide.parse_message(bad)


def _tasks_plan() -> plan.Plan:
    return plan.Plan(DAY, (
        _item("p1", title="Write the report", start=None),
        _item("p2", title="Email Sam", start=None, minutes=30),
        _item("p3", title="Fix the bike", start=None, done=True),
        _item("p4", title="Dentist", start="14:00", place="Clinic"),
    ))


def test_the_evening_keeps_the_destinations_of_tasks_left_undone_and_defaults_the_rest() -> None:
    """Each entry stays only for a task not done with a valid `to`; a task left out goes later."""
    answer = json.dumps({"message": "Good night.", "unfinished": [
        {"id": "p1", "to": "tomorrow"}, {"id": "p1", "to": "drop"},  # the first wins
        {"id": "p3", "to": "drop"},  # done already
        {"id": "p4", "to": "drop"},  # not a task
        {"id": "p9", "to": "drop"},  # not in the plan
        {"id": "p2", "to": "someday"},  # not a destination
        "p2",
    ]})
    found = decide.parse_evening(answer, _tasks_plan())
    assert (found.message, found.unfinished) == (
        "Good night.", (("p1", "tomorrow"), ("p2", "later")),
    )
    assert len(found.notes) == 6
    for bad in ("not json", '{"unfinished": []}', '{"message": ""}'):
        with pytest.raises(ValueError):  # noqa: PT011
            decide.parse_evening(bad, _tasks_plan())
    not_a_list = decide.parse_evening('{"message": "Done.", "unfinished": 3}', _tasks_plan())
    assert not_a_list.unfinished == (("p1", "later"), ("p2", "later"))


def test_the_morning_prompt_carries_the_plan_spoken_and_masks_a_private_item() -> None:
    """Task morning: clocks spoken, the route cut down, a private item named by its time."""
    held = plan.Plan(DAY, (
        _item("p1", title="Write the report", start=None),
        _item("p2", title="Dentist", start="14:00", place="Clinic", why="checkup",
              route={"from": "Home", "leave_at": "13:20", "line": "26", "board": "Main St",
                     "arrive_at": "13:55", "backup": None}),
        _item("p3", title="Therapy", start="16:00", place="Dr Lin's office", private=True,
              why="weekly", prepare=("notes",)),
        _item("p4", title="Call the bank", start=None, private=True),
    ))
    system, user = decide.build_morning_prompt(
        held, weather={"summary": "rain"}, goals=("learn Rust",), now=NOW, zone=ZONE,
        draft_failed=False,
    )
    body = json.loads(user)
    assert system == load_skill("day-plan").instructions
    assert (body["task"], body["day"], body["weekday"], body["language"]) == (
        "morning", "2026-10-16", "Friday", "en",
    )
    assert (body["now"], body["weather"], body["goals"], body["draft_failed"]) == (
        "4 AM", {"summary": "rain"}, ["learn Rust"], False,
    )
    dentist = body["plan"][1]
    assert (dentist["start"], dentist["place"]) == ("2 PM", "Clinic")
    assert dentist["route"] == {"leave_at": "1:20 PM", "line": "26", "board": "Main St"}
    therapy, bank = body["plan"][2], body["plan"][3]
    assert therapy["title"] == "The 4 PM thing."
    assert (therapy["place"], therapy["why"], therapy["prepare"]) == ("", "", [])
    assert bank["title"] == "Something on your plan."
    assert "Therapy" not in user
    assert "Lin" not in user
    assert "private" not in dentist
    failed = json.loads(decide.build_morning_prompt(
        plan.Plan(DAY, ()), weather=None, goals=(), now=NOW, zone=ZONE, draft_failed=True,
    )[1])
    assert (failed["plan"], failed["draft_failed"]) == ([], True)


def test_the_evening_prompt_sets_what_was_planned_against_what_was_lived() -> None:
    """Task evening: planned with done marks, lived lines, tomorrow's things; private masked."""
    held = plan.Plan(DAY, (
        _item("p1", title="写报告", start=None),
        _item("p2", title="看牙", start="14:00", place="诊所"),
        _item("p3", title="Therapy", start="16:00", private=True, done=True),
    ))
    tomorrow = [
        {"title": "Parent meeting", "start": "17:00", "place": "School", "private": False},
        {"title": "Visa interview", "start": "10:00", "place": "Consulate", "private": True},
    ]
    _system, user = decide.build_evening_prompt(
        held, lived=["09:00-10:00 on the Mac"], tomorrow=tomorrow,
        now=datetime(2026, 10, 16, 21, 5, tzinfo=ZONE), zone=ZONE,
    )
    body = json.loads(user)
    assert (body["task"], body["language"], body["lived"]) == (
        "evening", "zh", ["09:00-10:00 on the Mac"],
    )
    assert [(p["id"], p["start"], p["done"]) for p in body["planned"]] == [
        ("p1", None, False), ("p2", "下午2点整", False), ("p3", "下午4点整", True),
    ]
    assert body["planned"][2]["title"] == "下午4点整 那件事。"
    assert body["tomorrow"] == [
        {"title": "Parent meeting", "start": "下午5点整", "place": "School"},
        {"title": "上午10点整 那件事。", "start": "上午10点整", "place": ""},
    ]
    assert "Consulate" not in user
    assert "Therapy" not in user


def test_the_skill_loads_and_carries_the_principles_and_the_preferences() -> None:
    """The skill loads and tells the model to read his changes as preferences."""
    text = load_skill("day-plan").instructions
    for needle in ("his_changes", "carried", "droppable", "recent_days", "private", "morning",
                   "evening", "draft"):
        assert needle in text


def test_a_lived_item_reads_as_one_line_with_its_projects_and_place() -> None:
    """Work names its projects and apps; a stay names the place he keeps."""
    work = {
        "kind": "work", "active_s": 5400,
        "projects": [{"name": "jarvis", "seconds": 4200}],
        "apps": [{"name": "Ghostty", "seconds": 3000}],
    }
    said = runtime.lived_line(work)
    assert said == "on the Mac, 1h30 active; projects jarvis 1h10; apps Ghostty 50m"
    stay = {"kind": "stay", "lat": 49.2, "lng": -123.1, "kept_place": {"name": "家"}}
    assert runtime.lived_line(stay) == "at 家"


# --- the inputs ----------------------------------------------------------------------------


def test_the_inputs_hold_the_days_things_goals_and_say_what_is_missing(stores: Env) -> None:
    """ADR 0299: fixed, upcoming, all-day and open tasks come from the ledger; goals; the gaps."""
    cid = stores.commit("Dentist", datetime(2026, 10, 16, 14, 0, tzinfo=ZONE), place="Main St")
    later = stores.commit("Passport office", datetime(2026, 10, 18, 9, 30, tzinfo=ZONE))
    stores.commit("Far away", datetime(2026, 10, 30, 9, 30, tzinfo=ZONE))
    stores.add("Holiday", on_day="2026-10-16")
    rent = stores.add("Pay rent", "do", due="2026-10-17")
    stores.add("Later", "do", due="2026-12-01")
    remember_fact(stores.memory, "Spanish", "twenty minutes a day", core_memory.GOALS_SECTION)
    remember_fact(stores.memory, "Visa", "expires 2026-12-01", core_memory.ID_FREE_SECTION)
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert [(i.title, i.start, i.minutes, i.place, i.source) for i in inputs.fixed] == [
        ("Dentist", "14:00", 60, "Main St", f"item:{cid}"),
    ]
    assert [(u["title"], u["date"], u["start"]) for u in inputs.upcoming] == [
        ("Passport office", "2026-10-18", "09:30"),
    ]
    assert inputs.calendar_all_day == ("Holiday",)
    assert [(t["title"], t["due"], t["source"]) for t in inputs.things] == [
        ("Pay rent", "2026-10-17", f"item:{rent}"),
    ]
    assert any("Spanish" in g for g in inputs.goals)
    assert any("Visa" in d for d in inputs.deadlines)
    assert inputs.notes == ("weather",)
    assert {f"item:{cid}", f"item:{later}", f"item:{rent}", "goal", "his"} <= set(inputs.sources)
    assert "todo" not in inputs.sources
    assert len(inputs.recent_days) == runtime.RECENT_DAYS


def test_the_open_things_offered_to_the_draft_are_late_due_by_the_horizon_or_untimed(
    stores: Env,
) -> None:
    """Late, due by the horizon, to do today, or with no time, in that order; no more."""
    stores.add("Old bill", "do", due="2026-10-01")
    stores.add("Call mum", "do", start_at="2026-10-12T10:00:00-07:00")
    stores.add("Cleaning", "do", on_day="2026-10-16")
    stores.add("Private form", "do", due="2026-10-16T17:00:00-07:00", private=True)
    stores.add("Pay rent", "do", due="2026-10-17")
    stores.add("Fix the bike", "do")
    stores.add("Seminar", "go", start_at="2026-10-12T10:00:00-07:00")  # he went, or he did not
    stores.add("Far off", "do", due="2026-12-01")  # past the horizon
    stores.add(
        "Water plants", "do", start_at="2026-10-02T08:00:00-07:00", weekly_until="2026-12-01",
    )
    paid = stores.add("Paid already", "do", due="2026-10-16")
    with closing(stores.conn()) as conn:
        things.close(conn, paid, "paid", by="her", evidence="said so", zone=ZONE, now=NOW)
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert [(t["title"], t["due"], t["late"], t["private"]) for t in inputs.things] == [
        ("Old bill", "2026-10-01", True, False),
        ("Call mum", "2026-10-12", True, False),
        ("Cleaning", "2026-10-16", False, False),
        ("Private form", "2026-10-16", False, True),
        ("Pay rent", "2026-10-17", False, False),
        ("Fix the bike", None, False, False),
    ]
    # The week's repeat that falls today is a fixed item, not a task.
    assert [(i.title, i.start) for i in inputs.fixed] == [("Water plants", "08:00")]


def test_the_open_things_offered_to_the_draft_are_capped(stores: Env) -> None:
    """More open things than fit are cut at the cap, oldest first."""
    for n in range(runtime.MAX_THINGS + 5):
        stores.add(f"Task {n}", "do")
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert len(inputs.things) == runtime.MAX_THINGS
    assert inputs.things[0]["title"] == "Task 0"


def test_a_memo_like_untimed_thing_reaches_the_draft_and_can_be_planned(stores: Env) -> None:
    """A thing with no time (what a memo was) is in the prompt, a known source and a plan item."""
    bike = stores.add("Fix the bike", "do", note="the chain slips")
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert [(t["title"], t["due"], t["late"]) for t in inputs.things] == [
        ("Fix the bike", None, False),
    ]
    _, user = decide.build_prompt(inputs, zone=ZONE, today=DAY)
    body = json.loads(user)
    assert body["things"] == list(inputs.things)
    assert "todo" not in body

    def ask(*_: str) -> str:
        return _answer(_model(
            "Fix the bike", source=f"item:{bike}", start=None, window_from="15:00",
            window_to="17:00",
        ))

    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW) == "drafted"
    held = _drafted(stores)
    assert held is not None
    assert [(i.title, i.source, i.start) for i in held.items] == [
        ("Fix the bike", f"item:{bike}", None),
    ]


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        pytest.param("do", {"due": "2026-10-15"}, True, id="due-a-day-before"),
        pytest.param("do", {"due": "2026-10-15T23:00:00-07:00"}, True, id="due-an-hour-before"),
        pytest.param("do", {"due": "2026-10-16"}, False, id="due-the-day-itself"),
        pytest.param("do", {"start_at": "2026-10-15T10:00:00-07:00"}, True, id="to-start-before"),
        pytest.param("do", {"on_day": "2026-10-15"}, True, id="to-do-on-a-day-before"),
        pytest.param("do", {"on_day": "2026-10-16"}, False, id="to-do-on-the-day"),
        pytest.param("do", {}, False, id="no-time-to-miss"),
        pytest.param("go", {"start_at": "2026-10-15T10:00:00-07:00"}, False, id="a-go-is-not-late"),
        pytest.param(
            "do", {"start_at": "2026-10-01T10:00:00-07:00", "weekly_until": "2026-12-01"}, False,
            id="a-week-closes-a-date-at-a-time",
        ),
        pytest.param("do", {"due": "2026-10-15", "state": "done"}, False, id="closed"),
    ],
)
def test_late_means_an_open_do_whose_time_went_by_before_the_day(
    kind: str, fields: dict[str, Any], *, expected: bool,
) -> None:
    """ADR 0299: the one rule the ledger block and the draft share for what is behind."""
    one = things.Thing("thing-a", "Task", kind, **fields)
    first = int(datetime.combine(DAY, time.min, ZONE).timestamp() * 1000)
    assert bool(late([one], first, ZONE)) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("item:thing-0a1b", "thing-0a1b", id="item"),
        pytest.param("commitment:commitment-0a1b", "thing-0a1b", id="old-commitment"),
        pytest.param("commitment:", "", id="old-commitment-without-id"),
        pytest.param("todo", "", id="old-todo"),
        pytest.param("goal", "", id="goal"),
        pytest.param("his", "", id="his"),
        pytest.param("routine:r1", "", id="routine"),
    ],
)
def test_the_thing_a_plan_item_stands_for_is_named_by_its_source(
    source: str, expected: str,
) -> None:
    """``item:<id>`` names its thing; an old ``commitment:`` source maps to the carried-in id."""
    assert plan.thing_of(source) == expected


def test_a_past_plan_with_the_old_sources_still_loads_and_can_be_revised(stores: Env) -> None:
    """Plans drafted before ADR 0299 name `todo` and `commitment:<id>`; they fold and revise."""
    day = DAY - timedelta(days=30)
    old = (
        _item("p1", source="todo"),
        _item("p2", title="Dentist", firmness="fixed", start="14:00",
              source="commitment:commitment-0a1b2c"),
    )
    with closing(stores.conn()) as conn:
        plan.draft(conn, day, old, "d", "m")
        held = plan.for_day(conn, day)
        assert held is not None
        assert held.items == old
        plan.revise(conn, day, [replace(old[0], start="10:30"), old[1]], "him", "later please")
        revised = plan.for_day(conn, day)
    assert revised is not None
    assert [(i.source, i.start) for i in revised.items] == [
        ("todo", "10:30"), ("commitment:commitment-0a1b2c", "14:00"),
    ]
    assert [plan.thing_of(i.source) for i in revised.items] == ["", "thing-0a1b2c"]
    assert plan.SOURCE_NAMES == ("goal", "his")


def test_a_reader_that_fails_or_is_absent_is_noted_and_the_inputs_still_come(stores: Env) -> None:
    """A failing, absent or empty weather reader is noted and the inputs still come."""
    def broken(*_: Any) -> Any:  # noqa: ANN401
        msg = "not connected"
        raise LookupError(msg)

    for weather, notes in (
        (broken, ("weather",)), (None, ("weather",)), (lambda _day: None, ("weather",)),
        (lambda _day: {"summary": "rain"}, ()),
    ):
        inputs = runtime.gather(stores.sources(weather=weather), SETTINGS, DAY, NOW)
        assert inputs.notes == notes
        assert inputs.fixed == ()
        assert inputs.things == ()


def test_his_changes_to_past_plans_are_in_the_inputs_and_the_prompt(stores: Env) -> None:
    """His revisions of 14 days reach the inputs and the prompt."""
    days = [DAY - timedelta(days=n) for n in (1, 2, 3, 20)]
    with closing(stores.conn()) as conn:
        for day in days:
            plan.draft(conn, day, [_item("p1"), _item("p2", title="Run", start="07:00")], "d", "m")
        plan.revise(conn, days[0], [_item("p1")], "him", "no early runs")
        plan.revise(conn, days[1], [_item("p1", start="10:00")], "her", "she moved it")
        plan.revise(conn, days[3], [], "him", "too old to count")
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert [(c["date"], c["change"], c["why"]) for c in inputs.his_changes] == [
        (days[0].isoformat(), "- Run 07:00", "no early runs"),
    ]
    system, user = decide.build_prompt(inputs, zone=ZONE, today=DAY)
    body = json.loads(user)
    assert body["his_changes"] == list(inputs.his_changes)
    assert "past_plans" not in body
    assert body["carried"] == []
    assert body["task"] == "draft"
    assert "his_changes" in system


def test_the_count_of_his_changes_is_capped_newest_first(stores: Env) -> None:
    """His changes are capped, newest first."""
    with closing(stores.conn()) as conn:
        for n in range(runtime.MAX_CHANGES + 5):
            day = DAY - timedelta(days=1 + n % 10)
            plan.revise(conn, day, [_item(title=f"v{n}")], "him", f"why {n}")
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert len(inputs.his_changes) == runtime.MAX_CHANGES
    assert inputs.his_changes[0]["why"] == f"why {runtime.MAX_CHANGES + 4}"


# --- the draft -----------------------------------------------------------------------------


def _drafted(stores: Env, day: date = DAY) -> plan.Plan | None:
    with closing(stores.conn()) as conn:
        return plan.for_day(conn, day)


def test_a_good_answer_is_written_as_the_draft_with_the_commitment_added_back(stores: Env) -> None:
    """A good answer is written as plan.drafted, legs applied, the commitment added back."""
    stores.commit("Dentist", datetime(2026, 10, 16, 14, 0, tzinfo=ZONE))
    asked: list[tuple[str, str]] = []

    def ask(system: str, user: str) -> str:
        asked.append((system, user))
        return _answer(_model("Write the report"), _model("Nap", start="14:15"))

    legs: list[date] = []

    def route(items: list[plan.PlanItem], day: date) -> list[plan.PlanItem]:
        legs.append(day)
        return [replace(i, route={"leave_at": "08:40"}) for i in items]

    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW, route) == "drafted"
    held = _drafted(stores)
    assert held is not None
    assert [(i.title, i.start, i.firmness) for i in held.items] == [
        ("Write the report", "10:00", "movable"), ("Dentist", "14:00", "fixed"),
    ]
    assert all(i.route == {"leave_at": "08:40"} for i in held.items)
    assert legs == [DAY]
    assert len(asked) == 1
    assert held.digest
    with closing(stores.conn()) as conn:
        assert plan.failed(conn, DAY) is None


@pytest.mark.parametrize(
    ("ask", "why"),
    [
        pytest.param(None, "no model", id="no-model"),
        pytest.param(lambda *_: "sorry", "no usable plan", id="not-json"),
        pytest.param(lambda *_: _answer(), "no usable plan", id="empty-day"),
        pytest.param(
            lambda *_: _answer(_model(source="routine:r9")), "unknown source", id="all-dropped",
        ),
    ],
)
def test_a_draft_that_cannot_be_made_writes_a_failure_and_never_an_empty_day(
    stores: Env, ask: Callable[[str, str], str] | None, why: str,
) -> None:
    """Every way a draft can fail writes plan.draft_failed and no plan."""
    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW) == "failed"
    assert _drafted(stores) is None
    with closing(stores.conn()) as conn:
        error = plan.failed(conn, DAY)
    assert error is not None
    assert why in error


def test_a_model_that_raises_writes_the_failure_with_its_class(stores: Env) -> None:
    """A raising model leaves a failure that names the exception class."""
    def ask(*_: str) -> str:
        msg = "slow"
        raise TimeoutError(msg)

    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW) == "failed"
    with closing(stores.conn()) as conn:
        assert plan.failed(conn, DAY) == "TimeoutError: slow"


def test_a_route_planner_that_raises_keeps_the_plan_without_legs(stores: Env) -> None:
    """A failing route planner keeps the plan without legs."""
    def route(items: list[plan.PlanItem], day: date) -> list[plan.PlanItem]:
        msg = f"{day}: {len(items)}"
        raise RuntimeError(msg)

    ask = lambda *_: _answer(_model())  # noqa: E731
    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW, route) == "drafted"
    held = _drafted(stores)
    assert held is not None
    assert held.items[0].route is None


def test_a_day_he_already_changed_is_not_redrafted(stores: Env) -> None:
    """A day whose last write is his revision is kept as it is."""
    with closing(stores.conn()) as conn:
        plan.draft(conn, DAY, [_item()], "d", "m")
        plan.revise(conn, DAY, [_item(title="His version")], "him", "mine")
    ask = lambda *_: _answer(_model())  # noqa: E731
    assert runtime.draft_day(stores.sources(), SETTINGS, ask, DAY, NOW) == "kept"
    held = _drafted(stores)
    assert held is not None
    assert held.items[0].title == "His version"


# --- the schedule --------------------------------------------------------------------------


class Clock:
    """A schedule over fakes: which dates it drafted, and which dates have state."""

    def __init__(self, *, state: set[date] | None = None) -> None:
        """Start with ``state`` dates already holding a draft or a failure."""
        self.state = state or set()
        self.drafted: list[date] = []
        self.schedule = runtime.DayPlanSchedule(
            settings=SETTINGS, zone=ZONE, draft=self._draft, has_state=self.state.__contains__,
        )

    def _draft(self, day: date, now: datetime) -> str:
        del now
        self.drafted.append(day)
        self.state.add(day)
        return "drafted"

    def tick(self, moment: datetime) -> None:
        """One poll at ``moment``."""
        day = self.schedule.due(moment)
        if day is not None:
            self._draft(day, moment)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ZONE).astimezone(UTC)


def test_a_start_after_at_with_nothing_on_record_drafts_once_then_waits_for_tomorrow() -> None:
    """A late start drafts once; the next date drafts at `at`."""
    clock = Clock()
    for hour in (9, 10, 22):
        clock.tick(_at(DAY, hour))
    tomorrow = DAY + timedelta(days=1)
    clock.tick(_at(tomorrow, 3, 59))
    clock.tick(_at(tomorrow, 4, 0))
    clock.tick(_at(tomorrow, 4, 1))
    assert clock.drafted == [DAY, tomorrow]


def test_a_start_after_the_day_ends_drafts_nothing_until_tomorrows_at() -> None:
    """A day that is over is not planned: a 23:40 start waits for tomorrow's `at`."""
    clock = Clock()
    clock.tick(_at(DAY, 23, 40))
    clock.tick(_at(DAY + timedelta(days=1), 3, 59))
    assert clock.drafted == []
    clock.tick(_at(DAY + timedelta(days=1), 4, 0))
    assert clock.drafted == [DAY + timedelta(days=1)]


def test_a_start_after_at_with_a_draft_or_a_failure_on_record_does_nothing_today() -> None:
    """A start with a draft or failure on record waits for tomorrow's `at`."""
    clock = Clock(state={DAY})
    clock.tick(_at(DAY, 9))
    clock.tick(_at(DAY, 15))
    assert clock.drafted == []
    clock.tick(_at(DAY + timedelta(days=1), 4, 5))
    assert clock.drafted == [DAY + timedelta(days=1)]


def test_a_start_before_at_waits_for_at_and_drafts_once() -> None:
    """A start before `at` with nothing on record waits for `at`: one draft a day."""
    clock = Clock()
    clock.tick(_at(DAY, 2, 0))
    clock.tick(_at(DAY, 3, 30))
    assert clock.drafted == []
    clock.tick(_at(DAY, 4, 0))
    clock.tick(_at(DAY, 4, 1))
    assert clock.drafted == [DAY]


def test_a_start_before_at_with_state_waits_for_at() -> None:
    """A start before `at` with state on record waits for `at`."""
    clock = Clock(state={DAY})
    clock.tick(_at(DAY, 2, 0))
    clock.tick(_at(DAY, 3, 59))
    assert clock.drafted == []
    clock.tick(_at(DAY, 4, 0))
    clock.tick(_at(DAY, 5, 0))
    assert clock.drafted == [DAY]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (None, None), ({}, None), ({"enabled": False}, None), ({"at": "04:00"}, None),
        ({"enabled": True}, runtime.DayPlanSettings()),
        ({"enabled": True, "at": "04:30", "preset": "p", "horizon_days": 5, "line_preset": "q"},
         runtime.DayPlanSettings(time(4, 30), "p", 5, line_preset="q")),
        # the keys of ADR 0291 an old settings overlay may still hold are ignored
        ({"enabled": True, "morning_push": True, "decide_preset": "x", "told_per_day": 3,
          "told_per_hour": 1}, runtime.DayPlanSettings()),
        ({"enabled": True, "line_preset": " "}, runtime.DayPlanSettings()),
        ({"enabled": True, "at": "soon"}, None),
        ({"enabled": True, "horizon_days": 0, "preset": " "}, runtime.DayPlanSettings()),
    ],
)
def test_the_config_block_is_off_unless_enabled(
    block: dict[str, Any] | None, expected: runtime.DayPlanSettings | None,
) -> None:
    """The block is off unless `enabled` is true; bad values take the defaults."""
    assert runtime.DayPlanSettings.from_config(block) == expected


# --- the dry-run script --------------------------------------------------------------------


def test_the_replay_prints_sizes_without_a_model_and_writes_files_only_with_one(
    stores: Env, tmp_path: Path,
) -> None:
    """Without a model nothing is called or written; with one the day's files appear."""
    from scripts import day_plan_replay as script  # noqa: PLC0415 - the script is not a package

    stores.commit("Dentist", datetime(2026, 10, 16, 14, 0, tzinfo=ZONE))
    table = {"m": {"input": 3.0, "output": 15.0}}
    out = tmp_path / "out"
    dry = script.replay([DAY], stores.sources(), SETTINGS, "m", table, None, out)
    assert not out.exists()
    assert dry[0]["asked"] is False
    assert dry[0]["fixed"] == 1
    assert dry[0]["missing"] == ["weather"]
    assert dry[0]["est_cost_usd"] == pytest.approx(
        (dry[0]["est_input_tokens"] * 3.0 + script.OUTPUT_TOKENS * 15.0) / 1_000_000,
    )
    unpriced = script.replay([DAY], stores.sources(), SETTINGS, "other", table, None, out)
    assert unpriced[0]["est_cost_usd"] is None

    def ask(*_: str) -> str:
        return _answer(_model("Write the report", prepare=["laptop"], place="Library"))

    live = script.replay([DAY], stores.sources(), SETTINGS, "m", table, ask, out)
    assert (live[0]["asked"], live[0]["items"], live[0]["dropped"]) == (True, 2, 0)
    body = json.loads((out / f"{DAY.isoformat()}.json").read_text(encoding="utf-8"))
    assert [i["title"] for i in body["items"]] == ["Write the report", "Dentist"]
    text = (out / f"{DAY.isoformat()}.md").read_text(encoding="utf-8")
    assert "- 10:00-11:00 Write the report @ Library (movable)" in text
    assert "  - prepare: laptop" in text
    assert _drafted(stores) is None  # a replay never writes to the log
