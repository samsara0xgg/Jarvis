"""ADR 0289: the plan watcher at run time, over a real event log and memory.db with stubs.

A fake clock (each ``tick`` is told the minute), a stub model for the opening and the decision,
and stub say and push hooks (no key, no network, no model). The examples are invented.

- ``woke`` opens the day once (her record and ``plan.opened``); a failed draft opens it too;
- ``left_late`` is recorded, spoken and pushed time-sensitively; ``outing_soon`` is asked with
  the weather and the leg read again;
- a non-outing tell at dnd is only recorded, at quiet it is a silent push, past the hour's cap it
  is only recorded; a private item is named by its time on speakers and in a push;
- ``adjust`` revises the plan by her; an adjust that does not apply is silence;
- the first item's start settles the plan;
- the ``plan`` tool reads, changes and settles; ``route_legs`` fills the legs a draft lacks.
"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, datetime, time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.plan_tool import build_plan_tool
from jarvis.execution.tools import ToolContext, ToolError
from jarvis.runtime import day_plan as runtime
from jarvis.runtime.plan_run import Hooks, PlanWatch, make_route_legs
from jarvis.state import plan
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log, open_runtime_event_log
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)
SETTINGS = runtime.DayPlanSettings(at=time(4, 0), watch=True)
LEG = {"from": "Home", "leave_at": "13:20", "line": "26", "board": "Main St",
       "arrive_at": "13:55", "backup": None}
TELL = '{"act": "tell", "say": "Heads up.", "why": "late"}'


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"{DAY.isoformat()}T{clock}:00").replace(tzinfo=ZONE)


def _item(item_id: str = "p1", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    base: dict[str, Any] = {
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "todo",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


DENTIST = _item(
    "p9", title="Dentist", firmness="fixed", source="commitment:c1", start="14:00", place="Clinic",
    route=LEG,
)


class Rig:
    """The watcher over fresh stores, its stubs and what they were asked."""

    def __init__(self, root: Path, *, answers: list[str] | None = None,
                 settings: runtime.DayPlanSettings = SETTINGS) -> None:
        """Stores under ``root``; ``answers`` are the decider's replies in order (then TELL)."""
        self.log = root / "events.db"
        self.memory = root / "memory.db"
        open_event_log(self.log).close()
        open_memory_db(self.memory).close()
        self.answers = list(answers or [])
        self.asked: list[str] = []
        self.opened: list[str] = []
        self.said: list[str] = []
        self.pushed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.quiet = "off"
        self.hooks = Hooks(
            quiet=lambda: self.quiet, may_speak=lambda: True,
            say=self.said.append, push=lambda *a, **k: self.pushed.append((a, k)),
        )
        self.sources = runtime.DayPlanSources(
            self.log, self.memory, ZONE, DaySources(ZONE, None, self.memory),
            weather=lambda _day: {"summary": "rain"},
        )
        self.watch = PlanWatch(
            settings=settings, sources=self.sources, ask_decide=self._decide, ask_open=self._open,
            hooks=self.hooks,
        )

    def _decide(self, _system: str, user: str) -> str:
        self.asked.append(user)
        return self.answers.pop(0) if self.answers else TELL

    def _open(self, _system: str, user: str) -> str:
        self.opened.append(user)
        return '{"message": "Good morning."}'

    def conn(self) -> Any:  # noqa: ANN401
        """A connection to the event log."""
        return open_runtime_event_log(self.log)

    def drafted(self, *items: plan.PlanItem, wake: str | None = "07:00") -> None:
        """Draft today with ``items``; he woke at ``wake`` (focus off) unless it is None."""
        with closing(self.conn()) as conn:
            plan.draft(conn, DAY, items, "d", "m")
        if wake:
            self.woke(wake)

    def woke(self, at: str) -> None:
        """The phone says focus went off at ``at``."""
        with closing(self.conn()) as conn:
            emit_event(
                conn, type="phone.state_observed", ts_epoch_ms=int(_at(at).timestamp() * 1000),
                ingestion_node="phone", payload={"signal": "focus", "value": "off"},
            )

    def record(self, record_id: str) -> str | None:
        """The text of conversation record ``record_id``, or None."""
        with closing(open_memory_db(self.memory)) as conn:
            row = conn.execute(
                "SELECT text FROM records WHERE id = ? AND source = 'jarvis'", (record_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def today(self) -> plan.Plan:
        """Today's plan as the log leaves it."""
        with closing(self.conn()) as conn:
            held = plan.for_day(conn, DAY)
        assert held is not None
        return held


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A watcher over empty stores."""
    return Rig(tmp_path)


def test_waking_opens_the_day_once_and_the_decider_is_not_asked(rig: Rig) -> None:
    """Her opening is her record plus plan.opened, written once; woke is noticed, not judged."""
    rig.drafted(_item())
    assert rig.watch.tick(_at("07:05")) == ["woke", "open"]
    assert rig.record(f"plan-open-{DAY}") == "Good morning."
    held = rig.today()
    assert (held.opened, held.opened_signal, held.opened_record_id) == (
        True, "focus_off", f"plan-open-{DAY}",
    )
    assert rig.watch.tick(_at("07:06")) == []
    assert (len(rig.opened), rig.asked) == (1, [])
    with closing(rig.conn()) as conn:
        assert list(plan.notices(conn, DAY)) == ["woke"]


def test_a_day_whose_draft_failed_is_opened_with_that_fact_and_asked_once(rig: Rig) -> None:
    """No plan, a failed draft: the opening goes through the model with draft_failed, only once."""
    with closing(rig.conn()) as conn:
        plan.draft_failed(conn, DAY, "TimeoutError: slow")
    rig.woke("07:00")
    rig.watch.tick(_at("07:05"))
    rig.watch.tick(_at("07:06"))
    assert rig.record(f"plan-open-{DAY}") == "Good morning."
    assert [json.loads(u)["draft_failed"] for u in rig.opened] == [True]


def test_a_day_with_no_plan_and_no_failure_is_left_alone(rig: Rig) -> None:
    """Nothing to compare with: no notice, no call."""
    assert rig.watch.tick(_at("07:05")) == []
    assert (rig.opened, rig.asked) == ([], [])


def test_left_late_is_recorded_spoken_and_pushed_time_sensitively(rig: Rig) -> None:
    """3 minutes past the leave time: her words in the conversation, on speakers, on the phone."""
    rig.drafted(DENTIST)
    rig.watch.tick(_at("07:05"))
    assert "left_late:p9" in rig.watch.tick(_at("13:24"))
    assert rig.record(f"plan-{DAY}-left_late:p9") == "Heads up."
    assert rig.said == ["Heads up."]
    (args, kwargs), = rig.pushed
    assert args == ("Jarvis", "Heads up.")
    assert (kwargs["thread"], kwargs["time_sensitive"], kwargs["sound"]) == ("talk", True, True)
    with closing(rig.conn()) as conn:
        (told,) = plan.decisions(conn, DAY)
    assert (told.key, told.act, told.say) == ("left_late:p9", "tell", "Heads up.")


def test_outing_soon_is_asked_with_the_weather_and_the_leg_read_again(tmp_path: Path) -> None:
    """Before she is asked, the forecast and the leg are fetched and ride in the notice."""
    rig = Rig(tmp_path)
    asked: list[tuple[Any, ...]] = []

    @contextmanager
    def open_leg(action_id: str) -> Iterator[Any]:
        def plan_leg(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            asked.append((action_id, args, kwargs))
            option = {"leave_at": "13:25", "arrive_at": "13:58",
                      "legs": [{"route": "28", "board_stop": "Main St"}]}
            return {"chosen": option, "backup": None}

        yield plan_leg

    rig.watch._open_leg = open_leg  # noqa: SLF001
    rig.drafted(DENTIST)
    rig.watch.tick(_at("07:05"))
    rig.answers = ['{"act": "silent", "why": "fine"}']
    assert "outing_soon:p9" in rig.watch.tick(_at("13:00"))
    facts = json.loads(rig.asked[0])["notice"]["facts"]
    assert facts["weather"] == {"summary": "rain"}
    assert facts["leg_now"]["leave_at"] == "1:25 PM"  # said as she says it
    assert facts["leg_now"]["line"] == "28"
    (action_id, args, kwargs), = asked
    assert (action_id, args) == (f"plan-{DAY}-outing_soon:p9", ("Home", "Clinic"))
    assert kwargs == {"day": DAY.isoformat(), "arrive_by": "14:00"}
    assert rig.said == []  # silent: nothing said, nothing pushed
    assert rig.pushed == []


@pytest.mark.parametrize(
    ("level", "spoken", "pushed", "sound"),
    [("off", 1, 1, True), ("quiet", 0, 1, False), ("no-pop", 0, 0, None), ("dnd", 0, 0, None)],
)
def test_a_non_outing_tell_obeys_the_quiet_level_and_is_always_recorded(
    tmp_path: Path, level: str, spoken: int, pushed: int, sound: object,
) -> None:
    """quiet: a push without sound; no-pop and dnd: the conversation record only."""
    rig = Rig(tmp_path)
    rig.quiet = level
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Heads up."
    assert (len(rig.said), len(rig.pushed)) == (spoken, pushed)
    if pushed:
        assert rig.pushed[0][1]["sound"] is sound
        assert rig.pushed[0][1]["time_sensitive"] is False


def test_past_the_hours_cap_a_tell_is_only_recorded(tmp_path: Path) -> None:
    """Two notices in one minute with 1 allowed an hour: the second stays in the conversation."""
    rig = Rig(tmp_path, settings=replace(SETTINGS, told_per_hour=1))
    rig.drafted(_item("p1", title="First"), _item("p2", title="Second"))
    rig.watch.tick(_at("09:05"))
    assert rig.said == ["Heads up."]
    assert len(rig.pushed) == 1
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Heads up."
    assert rig.record(f"plan-{DAY}-item_due:p2") == "Heads up."


def test_a_private_item_is_named_by_its_time_on_speakers_and_in_a_push(rig: Rig) -> None:
    """Her record keeps her words; the speakers and the push say the time only."""
    rig.answers = ['{"act": "tell", "say": "Your therapy starts."}']
    rig.drafted(_item(title="Therapy", private=True))
    rig.watch.tick(_at("09:05"))
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Your therapy starts."
    assert rig.said == ["The 09:00 thing."]
    assert rig.pushed[0][0][1] == "The 09:00 thing."


def test_adjust_revises_the_plan_by_her(rig: Rig) -> None:
    """An applying adjust is plan.revised by her with her reason; the item moved."""
    move = {"op": "edit", "id": "p1", "start": "10:00"}
    rig.answers = [json.dumps({"act": "adjust", "say": "Moved to 10.", "changes": [move],
                               "why": "he is late"})]
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    assert rig.today().items[0].start == "10:00"
    with closing(rig.conn()) as conn:
        (revision,) = plan.revisions(conn, DAY, DAY, by="her")
    assert revision.why == "he is late"
    assert revision.change == "Write the report start: 09:00 -> 10:00"
    assert rig.said == ["Moved to 10."]


def test_an_adjust_with_a_change_that_does_not_apply_is_silence(rig: Rig) -> None:
    """One op naming no item turns the whole answer to silence: nothing moves, nothing is said."""
    rig.answers = ['{"act": "adjust", "say": "Moved.", "changes": [{"op": "drop", "id": "p7"}]}']
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    assert [i.item_id for i in rig.today().items] == ["p1"]
    with closing(rig.conn()) as conn:
        (decision,) = plan.decisions(conn, DAY)
        assert plan.revisions(conn, DAY, DAY) == []
    assert (decision.act, rig.said) == ("silent", [])


def test_the_first_items_start_settles_the_plan(rig: Rig) -> None:
    """Before 09:00 the plan stays a draft; at 09:00 the program settles it."""
    rig.drafted(_item(start="09:00"), _item("p2", start="11:00", firmness="droppable"))
    rig.watch.tick(_at("08:59"))
    assert rig.today().status == "draft"
    rig.watch.tick(_at("09:00"))
    held = rig.today()
    assert (held.status, held.settled_by) == ("settled", "first_item")


def test_a_model_that_fails_is_silence_on_record(tmp_path: Path) -> None:
    """A decider that raises writes a silent decision; nothing is told."""
    rig = Rig(tmp_path)

    def boom(*_: str) -> str:
        msg = "down"
        raise TimeoutError(msg)

    rig.watch._ask_decide = boom  # noqa: SLF001
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    with closing(rig.conn()) as conn:
        (decision,) = plan.decisions(conn, DAY)
    assert (decision.act, decision.why) == ("silent", "TimeoutError")
    assert rig.said == []


def test_the_settings_read_the_watch_keys() -> None:
    """Watch is on only for true; the caps need whole numbers of 1 or more."""
    block = {"enabled": True, "watch": True, "decide_preset": "sonnet", "told_per_day": 3,
             "told_per_hour": 0}
    parsed = runtime.DayPlanSettings.from_config(block)
    assert parsed is not None
    assert (parsed.watch, parsed.decide_preset, parsed.told_per_day, parsed.told_per_hour) == (
        True, "sonnet", 3, 2,
    )
    off = runtime.DayPlanSettings.from_config({"enabled": True})
    assert off is not None
    assert (off.watch, off.decide_preset, off.told_per_day) == (False, "haiku", 8)


# --- the plan tool -------------------------------------------------------------------------


def _tool(rig: Rig) -> Callable[[dict[str, Any]], dict[str, Any]]:
    (tool,) = build_plan_tool(ZONE, lambda: _at("09:30"))

    def call(args: dict[str, Any]) -> dict[str, Any]:
        with closing(rig.conn()) as conn:
            ctx = ToolContext(conn, SimpleNamespace(), "a1")
            return dict(tool.handler(args, ctx))

    return call


def test_the_plan_tool_reads_changes_and_settles(rig: Rig) -> None:
    """Read gives compact rows; change is one revision by him; settle is his."""
    rig.drafted(_item(place="Library"), DENTIST, wake=None)
    call = _tool(rig)
    read = call({"action": "read"})
    assert read["settled"] is False
    assert [r["id"] for r in read["items"]] == ["p1", "p9"]
    assert read["items"][0] == {
        "id": "p1", "title": "Write the report", "start": "09:00", "minutes": 60,
        "place": "Library", "firmness": "movable",
    }
    assert read["items"][1]["route"]["leave_at"] == "13:20"
    changed = call({"action": "change", "why": "he asked", "changes": [
        {"op": "edit", "id": "p1", "start": "10:30"}, {"op": "drop", "id": "nope"},
    ]})
    assert changed["changed"] is True
    assert len(changed["notes"]) == 1
    assert [r["start"] for r in changed["items"]] == ["10:30", "14:00"]
    with closing(rig.conn()) as conn:
        (revision,) = plan.revisions(conn, DAY, DAY, by="him")
    assert revision.why == "he asked"
    assert call({"action": "change", "changes": [{"op": "drop", "id": "nope"}]})["changed"] is False
    call({"action": "settle"})
    assert (rig.today().status, rig.today().settled_by) == ("settled", "him")


def test_the_plan_tool_refuses_a_day_with_no_plan_and_a_bad_date(rig: Rig) -> None:
    """Read says there is no plan; change and settle refuse; a bad date is an argument error."""
    call = _tool(rig)
    assert call({"action": "read", "day": "2026-10-17"})["plan"] is False
    with pytest.raises(ToolError, match="no plan yet"):
        call({"action": "settle", "day": "2026-10-17"})
    with pytest.raises(ToolError, match="ISO date"):
        call({"action": "read", "day": "tomorrow"})


# --- the legs of the draft -----------------------------------------------------------------


def test_route_legs_plans_a_leg_for_each_move_to_another_place(rig: Rig) -> None:
    """The day starts at home; a second item at the same place gets none; a failure is None."""
    calls: list[tuple[Any, ...]] = []

    @contextmanager
    def open_leg(_action: str) -> Iterator[Any]:
        def plan_leg(origin: str, dest: str, *, day: str, arrive_by: str) -> dict[str, Any]:
            calls.append((origin, dest, day, arrive_by))
            if dest == "Nowhere":
                msg = "not_found"
                raise ToolError(msg)
            ride = {"route": "26", "board_stop": f"stop for {dest}"}
            chosen = {"leave_at": "08:10", "arrive_at": "08:50", "legs": [ride]}
            backup = {"leave_at": "08:00", "arrive_at": "08:40", "legs": [ride]}
            return {"chosen": chosen, "backup": backup}

        yield plan_leg

    items = [
        _item("p1", place="Gym", start="09:00"),
        _item("p2", place="gym", start="10:30"),
        _item("p3", place="Library", start="12:00"),
        _item("p4", place="Nowhere", start="15:00"),
        _item("p5", title="Think", start="16:00"),
    ]
    out = make_route_legs(open_leg, rig.memory)(items, DAY)
    assert calls == [
        ("home", "Gym", "2026-10-16", "09:00"),
        ("gym", "Library", "2026-10-16", "12:00"),
        ("Library", "Nowhere", "2026-10-16", "15:00"),
    ]
    assert out[0].route == {
        "from": "home", "leave_at": "08:10", "line": "26", "board": "stop for Gym",
        "arrive_at": "08:50",
        "backup": {"leave_at": "08:00", "line": "26", "board": "stop for Gym",
                   "arrive_at": "08:40"},
    }
    assert [i.route is None for i in out] == [False, True, False, True, True]
    assert out[2].route is not None
    assert out[2].route["from"] == "gym"
