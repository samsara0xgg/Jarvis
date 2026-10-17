"""ADR 0288: the day's plan state, the nightly draft, its parser and its schedule.

Data-driven checks over a real event log and memory.db with a fake model, a fake calendar, fake
To Do and weather readers (no key, no network, no provider). The examples are invented.

- the fold: a draft, a revision that wins, settle, open, a failed draft, the revisions he made;
- the writers refuse an item that could not be read back;
- the parser: bad JSON, a collision with a fixed item, an unknown source, a commitment the model
  left out, an item that repeats a commitment;
- the inputs: commitments, calendar, goals, missing sources noted, and his earlier changes to
  plans (and how each plan settled) as preferences, in the prompt too;
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
from jarvis.state import commitments, core_memory, plan
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.memory_db import open_memory_db, remember_fact

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
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "todo",
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

    def commit(self, title: str, at: datetime, *, place: str = "", minutes: int = 60) -> str:
        """Note one confirmed commitment; its id."""
        record = commitments.Commitment(
            commitments.new_id(), title, at.isoformat(), "said", confirmed=True, private=False,
            end_at=(at + timedelta(minutes=minutes)).isoformat(), place=place,
        )
        with closing(self.conn()) as conn:
            commitments.note(conn, record)
        return record.commitment_id


@pytest.fixture
def stores(tmp_path: Path) -> Env:
    """The stores of one test."""
    return Env(tmp_path)


def _answer(*items: Any) -> str:  # noqa: ANN401
    return json.dumps({"items": list(items)})


def _model(title: str = "Write the report", **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    base = {
        "title": title, "start": "10:00", "minutes": 60, "firmness": "movable", "source": "todo",
        "why": "due soon", "prepare": [],
    }
    return base | fields


# --- the fold ------------------------------------------------------------------------------


def test_a_revision_wins_over_the_draft_and_settle_and_open_fold_in(stores: Env) -> None:
    """The last items win; settled, opened and failed fold in per date."""
    with closing(stores.conn()) as conn:
        assert plan.for_day(conn, DAY) is None
        plan.draft(conn, DAY, [_item("p1"), _item("p2", title="Walk", start="12:00")], "d1", "m")
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert (held.status, held.source, held.opened) == ("draft", "drafted", False)
        assert held.digest == "d1"
        assert [i.item_id for i in held.items] == ["p1", "p2"]
        plan.revise(conn, DAY, [_item("p1", start="10:30")], "him", "later please")
        plan.settle(conn, DAY, "him")
        plan.opened(conn, DAY, "plan-open-2026-10-16", "focus_off", 1_760_000_000_000)
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert [(i.item_id, i.start) for i in held.items] == [("p1", "10:30")]
        assert (held.status, held.settled_by, held.source) == ("settled", "him", "revised:him")
        assert (held.opened, held.opened_record_id, held.opened_signal) == (
            True, "plan-open-2026-10-16", "focus_off",
        )
        other = DAY + timedelta(days=1)
        assert plan.for_day(conn, other) is None
        plan.draft_failed(conn, other, "TimeoutError: slow")
        assert plan.failed(conn, other) == "TimeoutError: slow"
        assert plan.failed(conn, DAY) is None
        assert plan.for_day(conn, other) is None


def test_a_later_draft_replaces_the_items_but_a_settle_before_a_plan_marks_nothing(
    stores: Env,
) -> None:
    """A newer draft replaces the items; a settle with no plan to hold it marks nothing."""
    with closing(stores.conn()) as conn:
        plan.settle(conn, DAY, "first_item")  # no plan yet: nothing to mark
        assert plan.for_day(conn, DAY) is None
        plan.draft(conn, DAY, [_item("p1")], "d1", "m")
        plan.draft(conn, DAY, [_item("p9", title="Other")], "d2", "m")
        held = plan.for_day(conn, DAY)
        assert held is not None
        assert [i.item_id for i in held.items] == ["p9"]
        assert held.status == "draft"


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
        {"source": "commitment:"}, {"item_id": ""},
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
        goal="read", prepare=("book", "charger"), source="commitment:abc", private=True,
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
            lambda: plan.settle(conn, DAY, "nobody"),
            lambda: plan.opened(conn, DAY, "", "focus_off", 1),
        )
        for bad in attempts:
            with pytest.raises(ValueError):  # noqa: PT011
                bad()


# --- the parser ----------------------------------------------------------------------------

FIXED = (
    _item("c1", title="Dentist", firmness="fixed", source="commitment:c1", start="14:00",
          minutes=60, place="Main St"),
)
KNOWN = {"todo", "goal", "his", "commitment:c1"}


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
            _answer(_model(start="9am"), _model("Walk"), {"title": "no firmness"}, "text"),
            ["Walk", "Dentist"], ["item 1", "item 3", "item 4"], id="unreadable-items",
        ),
        pytest.param(_answer(), ["Dentist"], [], id="commitment-added-back"),
        pytest.param(
            _answer(_model(title="Go to the dentist", source="commitment:c1", start="14:00",
                           prepare=["insurance card"], why="six month check")),
            ["Dentist"], [], id="repeated-commitment-lends-prepare",
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
        dentist = next(i for i in items if i.source == "commitment:c1")
        assert (dentist.firmness, dentist.start, dentist.place) == ("fixed", "14:00", "Main St")


def test_a_repeated_commitment_keeps_the_fixed_item_and_lends_why_and_prepare() -> None:
    """A model item for a commitment keeps the fixed item and lends it why and prepare."""
    items, _ = decide.parse_items(
        _answer(_model(title="Other name", source="commitment:c1", start="09:00",
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
            _model("Dentist", source="commitment:c1", start="14:00"),
            _model("First task", start=None, source="goal"),
        ),
        known_sources=KNOWN, fixed=FIXED,
    )
    assert [(i.item_id, i.title) for i in items] == [
        ("p1", "Last task"), ("p2", "Dentist"), ("p3", "First task"),
    ]


def test_the_opening_is_strict_json_and_short() -> None:
    """The opening must be a JSON message of a sensible length."""
    assert decide.parse_opening('{"message": " Morning. One thing first. "}') == (
        "Morning. One thing first."
    )
    for bad in ("Good morning", '{"message": ""}', '{"text": "x"}', '{"message": 3}',
                json.dumps({"message": "x" * (decide.OPENING_CHARS + 1)})):
        with pytest.raises(ValueError):  # noqa: PT011
            decide.parse_opening(bad)


def test_the_skill_loads_and_carries_the_principles_and_the_preferences() -> None:
    """The skill loads and tells the model to read his changes as preferences."""
    text = load_skill("day-plan").instructions
    for needle in ("his_changes", "past_plans", "droppable", "recent_days", "private", "opening"):
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


def _calendar(first: int, last: int, now: int) -> list[dict[str, Any]]:
    del first, last, now
    return [
        {"id": "e1", "title": "Team sync", "start": "2026-10-16T17:00:00+00:00",
         "end": "2026-10-16T17:30:00+00:00"},
        {"id": "e2", "title": "Holiday", "start": "2026-10-16T07:00:00-07:00", "all_day": True},
    ]


def test_the_inputs_hold_the_days_commitments_calendar_goals_and_say_what_is_missing(
    stores: Env,
) -> None:
    """Commitments, calendar, To Do, goals and deadlines are in; the missing source is noted."""
    cid = stores.commit("Dentist", datetime(2026, 10, 16, 14, 0, tzinfo=ZONE), place="Main St")
    later = stores.commit("Passport office", datetime(2026, 10, 18, 9, 30, tzinfo=ZONE))
    stores.commit("Far away", datetime(2026, 10, 30, 9, 30, tzinfo=ZONE))
    remember_fact(stores.memory, "Spanish", "twenty minutes a day", core_memory.GOALS_SECTION)
    remember_fact(stores.memory, "Visa", "expires 2026-12-01", core_memory.ID_FREE_SECTION)
    todos = [{"title": "Pay rent", "due": "2026-10-17T23:59:00-07:00"}, {"title": "Someday"},
             {"title": "Later", "due": "2026-12-01T23:59:00-07:00"}]
    inputs = runtime.gather(
        stores.sources(calendar=_calendar, todos=lambda: todos), SETTINGS, DAY, NOW,
    )
    assert [(i.title, i.start, i.source) for i in inputs.fixed] == [
        ("Dentist", "14:00", f"commitment:{cid}"), ("Team sync", "10:00", "his"),
    ]
    assert [(u["title"], u["date"], u["start"]) for u in inputs.upcoming] == [
        ("Passport office", "2026-10-18", "09:30"),
    ]
    assert inputs.calendar_all_day == ("Holiday",)
    assert [t["title"] for t in inputs.todo] == ["Pay rent"]
    assert any("Spanish" in g for g in inputs.goals)
    assert any("Visa" in d for d in inputs.deadlines)
    assert inputs.notes == ("weather",)
    assert f"commitment:{later}" in inputs.sources
    assert {"todo", "goal", "his"} <= set(inputs.sources)
    assert len(inputs.recent_days) == runtime.RECENT_DAYS


def test_a_reader_that_fails_or_is_absent_is_noted_and_the_inputs_still_come(stores: Env) -> None:
    """A failing or absent reader is noted and the inputs still come."""
    def broken(*_: Any) -> Any:  # noqa: ANN401
        msg = "not connected"
        raise LookupError(msg)

    inputs = runtime.gather(stores.sources(calendar=broken, todos=broken), SETTINGS, DAY, NOW)
    assert inputs.notes == ("calendar", "todo", "weather")
    assert inputs.fixed == ()


def test_his_changes_to_past_plans_and_how_they_settled_are_in_the_inputs_and_the_prompt(
    stores: Env,
) -> None:
    """His revisions of 14 days and how each plan settled reach the inputs and the prompt."""
    days = [DAY - timedelta(days=n) for n in (1, 2, 3, 20)]
    with closing(stores.conn()) as conn:
        for day in days:
            plan.draft(conn, day, [_item("p1"), _item("p2", title="Run", start="07:00")], "d", "m")
        plan.revise(conn, days[0], [_item("p1")], "him", "no early runs")
        plan.settle(conn, days[0], "him")
        plan.revise(conn, days[1], [_item("p1", start="10:00")], "her", "she moved it")
        plan.settle(conn, days[1], "first_item")
        plan.revise(conn, days[3], [], "him", "too old to count")
        plan.settle(conn, days[3], "him")
        # days[2] was never settled
    inputs = runtime.gather(stores.sources(), SETTINGS, DAY, NOW)
    assert [(c["date"], c["change"], c["why"]) for c in inputs.his_changes] == [
        (days[0].isoformat(), "- Run 07:00", "no early runs"),
    ]
    assert [(p["date"], p["settled"]) for p in inputs.past_plans] == [
        (days[0].isoformat(), "him"), (days[1].isoformat(), "first_item"),
        (days[2].isoformat(), "no"),
    ]
    system, user = decide.build_prompt(inputs, zone=ZONE, today=DAY)
    body = json.loads(user)
    assert body["his_changes"] == list(inputs.his_changes)
    assert body["past_plans"] == list(inputs.past_plans)
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
        ({"enabled": True, "at": "04:30", "preset": "p", "horizon_days": 5, "morning_push": True},
         runtime.DayPlanSettings(time(4, 30), "p", 5, morning_push=True)),
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
    assert dry[0]["missing"] == ["calendar", "todo", "weather"]
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
