"""ADR 0291: the plan watcher at run time, over a real event log and memory.db with stubs.

A fake clock (each ``tick`` is told the minute), a stub model for the opening and the decision,
and stub say and push hooks (no key, no network, no model). The examples are invented.

- ``woke`` opens the day once (her record and ``plan.opened``); a failed draft opens it too;
- ``left_late`` is recorded, spoken and pushed time-sensitively; ``outing_soon`` is asked with
  the leg read again and the weather of the outing's own hours at its place (kept place, else
  where the leg ends, else home's and said so; ADR 0355), as the opening gives each outing its own;
- a non-outing tell at dnd is only recorded, at quiet it is a silent push, past the hour's cap it
  is only recorded; a private item is named by its time on speakers and in a push;
- ``adjust`` revises the plan by her; an adjust that does not apply is silence; a ``progress``
  notice goes in two steps: a done-check that marks tasks done, then the decider only when the
  tasks left do not fit the time left;
- the first item's start settles the plan;
- the ``plan`` tool reads (in order, with the budget), changes (move, done) and settles;
  ``route_legs`` fills the legs a draft lacks.
"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.plan_tool import build_plan_tool
from jarvis.execution.tools import ToolContext, ToolError
from jarvis.runtime import day_plan as runtime
from jarvis.runtime.plan_run import Hooks, PlanWatch, make_route_legs
from jarvis.state import memory_db, places, plan, things
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log, open_runtime_event_log
from jarvis.state.memory_db import append_record, open_memory_db
from tests.support.clock import FakeClock

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
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "goal",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


DENTIST = _item(
    "p9", title="Dentist", firmness="fixed", source="item:thing-c1", start="14:00", place="Clinic",
    route=LEG,
)


OPENING = "Good morning."


class Rig:
    """The watcher over fresh stores, its stubs and what they were asked."""

    def __init__(self, root: Path, *, answers: list[str] | None = None,
                 settings: runtime.DayPlanSettings = SETTINGS) -> None:
        """Stores under ``root``; ``answers`` are the decider's replies in order (then TELL)."""
        root.mkdir(parents=True, exist_ok=True)
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
        self.at_mac = True
        self.hooks = Hooks(
            quiet=lambda: self.quiet, may_speak=lambda: True, at_mac=lambda: self.at_mac,
            say=self.said.append, push=lambda *a, **k: self.pushed.append((a, k)),
        )
        self.outing_asked: list[tuple[tuple[float, float] | None, datetime, datetime]] = []
        self.outing_down = False
        self.sources = runtime.DayPlanSources(
            self.log, self.memory, ZONE, DaySources(ZONE, None, self.memory),
            weather=lambda _day: {"summary": "rain"}, outing_weather=self._outing_weather,
        )
        self.watch = PlanWatch(
            settings=settings, sources=self.sources, ask_decide=self._decide, ask_open=self._open,
            hooks=self.hooks,
        )

    def _outing_weather(
        self, point: tuple[float, float] | None, start: datetime, end: datetime,
    ) -> dict[str, Any] | None:
        """What was asked of the outing's weather, and a stub answer (or a failure)."""
        self.outing_asked.append((point, start, end))
        if self.outing_down:
            msg = "WeatherKit is down"
            raise ToolError(msg)
        return {"from": f"{start:%H:%M}", "to": f"{end:%H:%M}", "max_chance_pct": 70}

    def keep(self, name: str, lat: float, lng: float) -> None:
        """He keeps the place ``name`` at a point."""
        places.save(self.memory, name=name, lat=lat, lng=lng, origin="user")

    def _decide(self, _system: str, user: str) -> str:
        self.asked.append(user)
        return self.answers.pop(0) if self.answers else TELL

    def _open(self, _system: str, user: str) -> str:
        self.opened.append(user)
        return json.dumps({"message": OPENING})

    @property
    def told(self) -> list[str]:
        """What was said aloud besides her opening."""
        return [line for line in self.said if line != OPENING]

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


def test_a_day_is_not_opened_after_it_ends(rig: Rig) -> None:
    """A watcher started late at night does not open the day he already lived."""
    rig.drafted(_item())
    assert "open" not in rig.watch.tick(_at("23:40"))
    assert rig.record(f"plan-open-{DAY}") is None


def test_a_day_whose_draft_failed_is_opened_with_that_fact_and_asked_once(rig: Rig) -> None:
    """No plan, a failed draft: the opening goes through the model with draft_failed, only once."""
    with closing(rig.conn()) as conn:
        plan.draft_failed(conn, DAY, "TimeoutError: slow")
    rig.woke("07:00")
    rig.watch.tick(_at("07:05"))
    rig.watch.tick(_at("07:06"))
    assert rig.record(f"plan-open-{DAY}") == "Good morning."
    assert [json.loads(u)["draft_failed"] for u in rig.opened] == [True]


def test_the_opening_lists_the_things_he_has_not_confirmed(rig: Rig) -> None:
    """ADR 0299: unconfirmed things of the next days, due ones and untimed ones; no others."""
    def add(title: str, kind: str = "go", *, confirmed: bool = False, **fields: Any) -> str:  # noqa: ANN401
        one = things.Thing(things.new_id(), title, kind, confirmed=confirmed, **fields)
        with closing(rig.conn()) as conn:
            return things.add(
                conn, one, by="mail", evidence="a mail", zone=ZONE, now=_at("06:00"),
            )[0].thing_id

    def on(days: int, clock: str) -> str:
        return (_at(clock) + timedelta(days=days)).isoformat()

    add("Visa interview", start_at=on(2, "10:00"), place="Consulate", private=True)
    add("Parent meeting", start_at=on(1, "17:00"))
    add("Send form", "do", due=(DAY + timedelta(days=1)).isoformat())
    add("Fix the bike", "do")
    add("Class", start_at=on(1, "09:00"), confirmed=True)  # he has said yes
    add("Reunion", start_at=on(9, "12:00"))  # past the horizon
    add("Tax return", "do", due=(DAY + timedelta(days=40)).isoformat())  # so is its date
    dropped = add("Dropped", start_at=on(1, "11:00"))
    with closing(rig.conn()) as conn:
        things.close(conn, dropped, "cancelled", by="her", evidence="said so", zone=ZONE)
    rig.drafted(_item())
    rig.watch.tick(_at("07:05"))
    rows = json.loads(rig.opened[0])["unconfirmed"]
    assert [(r["date"], r["title"], r["place"], r["private"]) for r in rows] == [
        ("2026-10-17", "Parent meeting", "", False),
        ("2026-10-18", "Visa interview", "Consulate", True),
        ("2026-10-17", "Send form", "", False),
        (None, "Fix the bike", "", False),
    ]


def test_the_opening_gets_today_at_home_and_each_outing_its_own_weather(rig: Rig) -> None:
    """Each timed item away from home, soonest first, with the hours of its own stretch."""
    rig.keep("clinic", 48.46, -123.31)
    rig.drafted(
        _item("p1", title="Write the report"),  # no place: not an outing
        _item("p2", title="Lunch", place="Mayfair Mall", start="12:00", minutes=45),
        _item("p3", title="Standup", place="Home", start="08:00", minutes=15),  # at home
        DENTIST,  # 14:00-15:00 at the kept clinic, leaving 13:20
        _item("p4", title="Errands", place="Downtown", start=None),  # no time: no stretch to give
    )
    rig.watch.tick(_at("07:05"))
    body = json.loads(rig.opened[0])
    assert body["weather"] == {"summary": "rain"}  # what the source gave for the day, as it is
    assert [(o["item_id"], o["place"]) for o in body["outings"]] == [
        ("p2", "Mayfair Mall"), ("p9", "Clinic"),
    ]
    assert body["outings"][0]["weather"] == {
        "at": "home", "from": "12 PM", "to": "12:45 PM", "max_chance_pct": 70,
        "why_home": "the position of Mayfair Mall is not known",
    }
    assert body["outings"][1]["weather"]["at"] == "Clinic"
    assert rig.outing_asked == [
        (None, _at("12:00"), _at("12:45")), ((48.46, -123.31), _at("13:20"), _at("15:00")),
    ]


def test_an_opening_with_no_outing_weather_still_comes_without_it(tmp_path: Path) -> None:
    """A reader that fails leaves the outing out; the day is still opened."""
    rig = Rig(tmp_path)
    rig.outing_down = True
    rig.drafted(DENTIST)
    assert rig.watch.tick(_at("07:05")) == ["woke", "open"]
    assert json.loads(rig.opened[0])["outings"] == []


def test_a_day_with_no_plan_and_no_failure_is_left_alone(rig: Rig) -> None:
    """Nothing to compare with: no notice, no call."""
    assert rig.watch.tick(_at("07:05")) == []
    assert (rig.opened, rig.asked) == ([], [])


def test_left_late_is_recorded_spoken_and_pushed_time_sensitively(rig: Rig) -> None:
    """3 minutes past the leave time: in the conversation, and on the phone while he is away."""
    rig.at_mac = False
    rig.drafted(DENTIST)
    rig.watch.tick(_at("07:05"))
    assert "left_late:p9" in rig.watch.tick(_at("13:24"))
    assert rig.record(f"plan-{DAY}-left_late:p9") == "Heads up."
    assert rig.told == []  # nobody at the Mac to hear it
    (args, kwargs), = rig.pushed
    assert args == ("Jarvis", "Heads up.")
    assert (kwargs["thread"], kwargs["time_sensitive"], kwargs["sound"]) == ("talk", True, True)
    with closing(rig.conn()) as conn:
        (told,) = plan.decisions(conn, DAY)
    assert (told.key, told.act, told.say) == ("left_late:p9", "tell", "Heads up.")


def _with_leg(rig: Rig, end_at: dict[str, float] | None) -> list[tuple[Any, ...]]:
    """Give the watcher a leg planner that answers as the real one does; what it was asked."""
    asked: list[tuple[Any, ...]] = []

    @contextmanager
    def open_leg(action_id: str) -> Iterator[Any]:
        def plan_leg(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            asked.append((action_id, args, kwargs))
            option = {"leave_at": "13:25", "arrive_at": "13:58",
                      "legs": [{"route": "28", "board_stop": "Main St"}]}
            return {"chosen": option, "backup": None, **({"end_at": end_at} if end_at else {})}

        yield plan_leg

    rig.watch._open_leg = open_leg  # noqa: SLF001
    return asked


def _outing_soon(rig: Rig) -> dict[str, Any]:
    """The facts she is asked with at 13:00, when the dentist's leg leaves at 13:20."""
    rig.drafted(DENTIST)
    rig.watch.tick(_at("07:05"))  # the opening reads the outing's weather too: not what is asked
    rig.outing_asked.clear()
    rig.answers = ['{"act": "silent", "why": "fine"}']
    assert "outing_soon:p9" in rig.watch.tick(_at("13:00"))
    facts: dict[str, Any] = json.loads(rig.asked[0])["notice"]["facts"]
    return facts


def test_outing_soon_is_asked_with_the_leg_read_again_and_the_weather_where_the_leg_ends(
    tmp_path: Path,
) -> None:
    """The place is not a kept one: its weather is for the end of the leg read just now."""
    rig = Rig(tmp_path)
    asked = _with_leg(rig, {"lat": 48.43, "lng": -123.36})
    facts = _outing_soon(rig)
    assert facts["weather"] == {  # the hours from the leave time to the end, said as she says it
        "at": "Clinic", "from": "1:20 PM", "to": "3 PM", "max_chance_pct": 70,
    }
    assert rig.outing_asked == [((48.43, -123.36), _at("13:20"), _at("15:00"))]
    assert facts["leg_now"]["leave_at"] == "1:25 PM"  # said as she says it
    assert facts["leg_now"]["line"] == "28"
    assert "notes" not in facts
    (action_id, args, kwargs), = asked
    assert (action_id, args) == (f"plan-{DAY}-outing_soon:p9", ("Home", "Clinic"))
    assert kwargs == {"arrive_by": f"{DAY.isoformat()} 14:00"}
    assert rig.told == []  # silent: nothing said, nothing pushed
    assert rig.pushed == []


def test_outing_soon_at_a_kept_place_reads_the_hours_at_its_position(tmp_path: Path) -> None:
    """A kept place has a position of its own: it is used, whatever the leg's end says."""
    rig = Rig(tmp_path)
    rig.keep("clinic", 48.46, -123.31)
    _with_leg(rig, {"lat": 1.0, "lng": 2.0})
    facts = _outing_soon(rig)
    assert [point for point, _, _ in rig.outing_asked] == [(48.46, -123.31)]
    assert facts["weather"]["at"] == "Clinic"


def test_outing_soon_with_no_position_known_is_home_s_hours_and_says_so(tmp_path: Path) -> None:
    """Not kept, and the leg gave no end: home's hours, with a note that they are home's."""
    rig = Rig(tmp_path)
    _with_leg(rig, None)
    facts = _outing_soon(rig)
    assert [point for point, _, _ in rig.outing_asked] == [None]
    assert facts["weather"]["at"] == "home"
    assert facts["weather"]["why_home"] == "the position of Clinic is not known"
    assert facts["weather"]["max_chance_pct"] == 70


def test_outing_soon_goes_on_without_weather_the_reader_cannot_give(tmp_path: Path) -> None:
    """A reader that fails: she is asked with the leg, and the facts say it is unreadable."""
    rig = Rig(tmp_path)
    rig.outing_down = True
    _with_leg(rig, {"lat": 48.43, "lng": -123.36})
    facts = _outing_soon(rig)
    assert facts["weather"] is None
    assert facts["notes"] == ["weather unreadable"]
    assert facts["leg_now"]["line"] == "28"


@pytest.mark.parametrize(
    ("level", "spoken", "pushed", "sound"),
    [("off", 0, 1, True), ("quiet", 0, 1, False), ("no-pop", 0, 0, None), ("dnd", 0, 0, None)],
)
def test_a_non_outing_tell_obeys_the_quiet_level_and_is_always_recorded(
    tmp_path: Path, level: str, spoken: int, pushed: int, sound: object,
) -> None:
    """Away from the Mac. quiet: a push without sound; no-pop and dnd: the record only."""
    rig = Rig(tmp_path)
    rig.at_mac = False
    rig.quiet = level
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Heads up."
    assert (len(rig.told), len(rig.pushed)) == (spoken, pushed)
    if pushed:
        assert rig.pushed[0][1]["sound"] is sound
        assert rig.pushed[0][1]["time_sensitive"] is False


def test_past_the_hours_cap_a_tell_is_only_recorded(tmp_path: Path) -> None:
    """Two notices in one minute with 1 allowed an hour: the second stays in the conversation."""
    rig = Rig(tmp_path, settings=replace(SETTINGS, told_per_hour=1))
    rig.drafted(_item("p1", title="First"), _item("p2", title="Second"))
    rig.watch.tick(_at("09:05"))
    assert rig.told == ["Heads up."]  # at the Mac: said there, not pushed
    assert rig.pushed == []
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Heads up."
    assert rig.record(f"plan-{DAY}-item_due:p2") == "Heads up."


def test_a_private_item_is_named_by_its_time_on_speakers_and_in_a_push(
    rig: Rig, tmp_path: Path,
) -> None:
    """Her record keeps her words; the speakers and the push say the time only."""
    rig.answers = ['{"act": "tell", "say": "Your therapy starts."}']
    rig.drafted(_item(title="Therapy", private=True))
    rig.watch.tick(_at("09:05"))
    assert rig.record(f"plan-{DAY}-item_due:p1") == "Your therapy starts."
    assert rig.told == ["The 09:00 thing."]
    (tmp_path / "away").mkdir()
    away = Rig(tmp_path / "away", answers=['{"act": "tell", "say": "Your therapy starts."}'])
    away.at_mac = False
    away.drafted(_item(title="Therapy", private=True))
    away.watch.tick(_at("09:05"))
    assert away.pushed[0][0][1] == "The 09:00 thing."


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
    assert rig.told == ["Moved to 10."]


def test_an_adjust_with_a_change_that_does_not_apply_is_silence(rig: Rig) -> None:
    """One op naming no item turns the whole answer to silence: nothing moves, nothing is said."""
    rig.answers = ['{"act": "adjust", "say": "Moved.", "changes": [{"op": "drop", "id": "p7"}]}']
    rig.drafted(_item())
    rig.watch.tick(_at("09:05"))
    assert [i.item_id for i in rig.today().items] == ["p1"]
    with closing(rig.conn()) as conn:
        (decision,) = plan.decisions(conn, DAY)
        assert plan.revisions(conn, DAY, DAY) == []
    assert (decision.act, rig.told) == ("silent", [])


def _two_tasks(rig: Rig) -> None:
    rig.drafted(_item("p1", start=None), _item("p2", start=None, title="Email Sam", minutes=30))


def _early(root: Path, *answers: str) -> Rig:
    """A rig whose day ends at 10:00: at 09:00 it has 60 minutes for 90 of tasks."""
    return Rig(root, answers=list(answers), settings=replace(SETTINGS, day_ends=time(10, 0)))


def test_a_progress_check_that_fits_after_marking_is_silent_with_no_second_call(
    tmp_path: Path,
) -> None:
    """The done-check marks p1 done (and p9, which is no task); 30 of 60 minutes fit: silence."""
    rig = _early(tmp_path, json.dumps({"done": ["p1", "p9"], "why": "he sent the report"}))
    rig.quiet = "off"
    _two_tasks(rig)
    assert rig.watch.tick(_at("09:00")) == ["woke", "progress:1", "open"]
    [asked] = rig.asked
    body = json.loads(asked)
    assert (body["task"], [t["id"] for t in body["tasks"]]) == ("progress", ["p1", "p2"])
    assert body["evidence"]["since"] == "07:00"
    assert [i.done for i in rig.today().items] == [True, False]
    with closing(rig.conn()) as conn:
        by_key = {d.key: d for d in plan.decisions(conn, DAY)}
        [revision] = plan.revisions(conn, DAY, DAY)
    assert (by_key["progress:1:done"].act, by_key["progress:1:done"].say) == ("adjust", "")
    assert "p9" in by_key["progress:1:done"].why  # what was dropped is on record
    assert (by_key["progress:1"].act, by_key["progress:1"].why) == ("silent", "fits")
    assert revision.by == "her"
    assert (rig.told, rig.pushed, rig.record(f"plan-{DAY}-progress:1")) == ([], [], None)
    assert rig.watch.tick(_at("09:30")) == []  # each checkpoint once


def test_a_progress_check_that_still_does_not_fit_asks_the_decider_with_the_fresh_budget(
    tmp_path: Path,
) -> None:
    """p1 has 45 minutes left: 75 against 60. The second call sees that, not the evidence."""
    rig = _early(tmp_path, json.dumps({"left": {"p1": 45}}), TELL)
    _two_tasks(rig)
    rig.watch.tick(_at("09:00"))
    assert [json.loads(a)["task"] for a in rig.asked] == ["progress", "notice"]
    second = json.loads(rig.asked[1])
    facts = second["notice"]["facts"]
    assert (facts["remaining_min"], facts["free_min"], facts["fits"]) == (75, 60, False)
    assert facts["marked"] == {"done": [], "left": {"p1": 45}}
    assert "evidence" not in facts
    assert second["told_today"] == []  # the done-check is not something she told him
    assert [(i.item_id, i.minutes) for i in rig.today().items] == [("p1", 45), ("p2", 30)]
    assert rig.told == ["Heads up."]
    with closing(rig.conn()) as conn:
        assert [(d.key, d.act) for d in plan.decisions(conn, DAY)] == [
            ("progress:1:done", "adjust"), ("progress:1", "tell"),
        ]
    # the day is over for checks at 10:00, whatever is left
    assert rig.watch.tick(_at("11:00")) == []
    assert len(rig.asked) == 2


def test_a_done_check_that_fails_leaves_the_plan_and_the_budget_decides(tmp_path: Path) -> None:
    """Not JSON: nothing marked, silent on record; 90 of 60 does not fit, so she is asked (dnd)."""
    rig = _early(tmp_path, "not json", TELL)
    rig.quiet = "dnd"
    _two_tasks(rig)
    rig.watch.tick(_at("09:00"))
    assert (rig.told, rig.pushed, rig.record(f"plan-{DAY}-progress:1")) == ([], [], "Heads up.")
    assert [i.done for i in rig.today().items] == [False, False]
    with closing(rig.conn()) as conn:
        by_key = {d.key: d for d in plan.decisions(conn, DAY)}
    assert (by_key["progress:1:done"].act, "JSON" in by_key["progress:1:done"].why) == (
        "silent", True,
    )
    assert by_key["progress:1"].act == "tell"


def _work(rig: Rig, at: str, *texts: str) -> None:
    with closing(rig.conn()) as conn:
        emit_event(conn, type="work_state.revised", ts_epoch_ms=int(_at(at).timestamp() * 1000),
                   payload={"item": {"activities": [{"text": t} for t in texts]},
                            "trigger": "auto"})


def test_each_progress_check_sees_the_evidence_since_the_previous_one(tmp_path: Path) -> None:
    """The first looks back to waking; the second to when the first looked, not to waking."""
    rig = Rig(tmp_path, answers=["{}", "{}"])
    _two_tasks(rig)
    _work(rig, "06:30", "before he woke")
    _work(rig, "08:00", "editing the report")
    _work(rig, "09:30", "writing tests")
    rig.watch.tick(_at("09:00"))
    rig.watch.tick(_at("11:00"))
    with closing(rig.conn()) as conn:
        seen = plan.notices(conn, DAY)
    first, second = (seen[k].facts["evidence"] for k in ("progress:1", "progress:2"))
    assert (first["since"], [a["text"] for a in first["screen"]]) == (
        "07:00", ["editing the report"],
    )
    assert (second["since"], [a["text"] for a in second["screen"]]) == ("09:00", ["writing tests"])
    assert [json.loads(a)["evidence"]["since"] for a in rig.asked] == ["07:00", "09:00"]


def test_the_first_items_start_settles_a_plan_nobody_opened(rig: Rig) -> None:
    """No sign of him, so no opening: at 09:00 the program settles it. An opened one is his."""
    rig.drafted(_item(start="09:00"), _item("p2", start="11:00", firmness="droppable"), wake=None)
    rig.watch.tick(_at("08:59"))
    assert rig.today().status == "draft"
    rig.watch.tick(_at("09:00"))
    held = rig.today()
    assert (held.status, held.settled_by) == ("settled", "first_item")


def test_an_opened_plan_is_not_settled_by_the_program(rig: Rig) -> None:
    """He was up and she opened the day: the first item's start leaves it to him."""
    rig.drafted(_item(start="09:00"))
    rig.watch.tick(_at("08:00"))
    rig.watch.tick(_at("09:00"))
    assert (rig.today().opened, rig.today().status) == (True, "draft")


def test_the_opening_is_said_at_the_mac_and_pushed_when_he_is_away(tmp_path: Path) -> None:
    """Said where he is; away from the Mac the phone gets it with a sound; dnd keeps it only."""
    home = Rig(tmp_path / "home", settings=replace(SETTINGS, morning_push=True))
    home.drafted(_item())
    home.watch.tick(_at("07:05"))
    assert (home.said, home.pushed) == ([OPENING], [])
    away = Rig(tmp_path / "away", settings=replace(SETTINGS, morning_push=True))
    away.at_mac = False
    away.drafted(_item())
    away.watch.tick(_at("07:05"))
    (args, kwargs), = away.pushed
    assert (away.said, args[1], kwargs["sound"], kwargs["thread"]) == ([], OPENING, True, "talk")
    held = Rig(tmp_path / "dnd", settings=replace(SETTINGS, morning_push=True))
    held.quiet, held.at_mac = "dnd", False
    held.drafted(_item())
    held.watch.tick(_at("07:05"))
    assert (held.said, held.pushed, held.record(f"plan-open-{DAY}")) == ([], [], OPENING)


def _clocked(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Records get the watcher's time, not the wall clock; set ``[0]`` before writing one."""
    now = [_at("07:00")]
    monkeypatch.setattr(memory_db, "local_now", lambda: now[0])
    return now


def _his(rig: Rig, record_id: str, text: str) -> None:
    append_record(rig.memory, record_id=record_id, source="allen", text=text)


def test_an_unanswered_opening_is_followed_up_until_he_answers(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten minutes of no answer and she follows up with the talk so far.

    His answer stops it; her next unanswered words start the wait again (15 min for the second).
    """
    now = _clocked(monkeypatch)
    rig.drafted(_item())
    now[0] = _at("07:05")
    rig.watch.tick(now[0])  # the opening
    assert rig.watch.tick(_at("07:14")) == []
    now[0] = _at("07:15")
    assert rig.watch.tick(now[0]) == ["unsettled:1"]
    body = json.loads(rig.opened[-1])
    assert (body["task"], body["asked"], body["minutes"]) == ("follow-up", 0, 10)
    assert [m["who"] for m in body["conversation"]] == ["you"]
    assert rig.record(f"plan-{DAY}-unsettled:1") == OPENING
    now[0] = _at("07:20")
    _his(rig, "r-him", "I'll go out at ten")
    assert rig.watch.tick(_at("08:00")) == []  # his words are the last
    now[0] = _at("08:01")
    append_record(rig.memory, record_id="r-her", source="jarvis", text="The 9:40 bus then?")
    assert rig.watch.tick(_at("08:15")) == []
    assert rig.watch.tick(_at("08:16")) == ["unsettled:2"]
    assert [m["who"] for m in json.loads(rig.opened[-1])["conversation"]] == [
        "you", "you", "him", "you",
    ]


def test_follow_ups_stop_once_settled_and_after_six(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six follow-ups at most in a day, and none once the plan is settled."""
    now = _clocked(monkeypatch)
    rig.drafted(_item())
    rig.watch.tick(_at("07:05"))
    keys = []
    for minute in range(10, 400, 5):
        now[0] = _at("07:05") + timedelta(minutes=minute)
        keys += [k for k in rig.watch.tick(now[0]) if k.startswith("unsettled")]
    assert keys == [f"unsettled:{n}" for n in range(1, 7)]
    settled = Rig(rig.log.parent / "settled")
    now2 = _clocked(monkeypatch)
    settled.drafted(_item())
    settled.watch.tick(_at("07:05"))
    with closing(settled.conn()) as conn:
        plan.settle(conn, DAY, "him")
    now2[0] = _at("08:00")
    assert settled.watch.tick(now2[0]) == []


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
    assert rig.told == []


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
    assert off.day_ends == time(23, 30)
    ends = runtime.DayPlanSettings.from_config({"enabled": True, "day_ends": "22:15"})
    assert ends is not None
    assert ends.day_ends == time(22, 15)
    assert runtime.day_ends_of({"day_ends": "late"}) == time(23, 30)


# --- the plan tool -------------------------------------------------------------------------


def _tool(rig: Rig) -> Callable[[dict[str, Any]], dict[str, Any]]:
    (tool,) = build_plan_tool()

    def call(args: dict[str, Any]) -> dict[str, Any]:
        with closing(rig.conn()) as conn:
            ctx = ToolContext(conn, SimpleNamespace(), "a1", FakeClock(_at("09:30")))
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


def test_the_plan_tool_reads_tasks_in_order_with_the_budget_and_moves_and_marks_done(
    rig: Rig,
) -> None:
    """Read: order, done marks, and the time left today (09:30 to 23:30 less dentist and bus)."""
    rig.drafted(_item("p1", start=None), _item("p2", start=None, title="Email Sam", minutes=30),
                DENTIST, wake=None)
    call = _tool(rig)
    read = call({"action": "read"})
    assert read["budget"] == {
        "tasks_left": 2, "remaining_min": 90, "free_min": 745, "fits": True,
        "next_task": "Write the report",
    }  # 840 minutes less 60 for the dentist and 35 for the bus
    changed = call({"action": "change", "changes": [
        {"op": "move", "id": "p2", "after": None}, {"op": "edit", "id": "p1", "done": True},
    ]})
    assert [(r["id"], r.get("done", False)) for r in changed["items"]] == [
        ("p2", False), ("p1", True), ("p9", False),
    ]
    assert changed["budget"]["tasks_left"] == 1
    assert (changed["budget"]["remaining_min"], changed["budget"]["next_task"]) == (30, "Email Sam")
    assert "order changed" in changed["summary"]
    assert "budget" not in call({"action": "read", "day": "2026-10-17"})


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
        def plan_leg(origin: str, dest: str, *, arrive_by: str) -> dict[str, Any]:
            day, _, start = arrive_by.partition(" ")
            calls.append((origin, dest, day, start))
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
