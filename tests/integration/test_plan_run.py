"""ADR 0383: the plan watcher at run time, over a real event log and memory.db with stubs.

A fake clock (each ``tick`` is told the minute), a stub model for the two lines the model writes,
and stub push hooks (no key, no network, no model). The examples are invented.

- the morning line goes once at the first sign he is up: her record, ``plan.said``, a silent push
  when he is away from the Mac, the conversation alone at ``no-pop`` and ``dnd``; a call holds it
  to the next minute; a model that fails is tried three times; he never gets a second line or a
  follow-up when he does not answer;
- a thing that mail or the mirror added or moved after the morning is one fixed ``new`` line; a
  thing from his own words is not; a third after two is not;
- the evening line on the way home, when the Mac stopped and at the fallback hour writes
  ``plan.closed``, and the next day's draft carries the ``tomorrow`` tasks;
- he mutes a kind through the ``plan`` tool, which has no settle; the day reply shows her latest
  line; ``route_legs`` fills the legs a draft lacks;
- the morning gets today at home and each outing the weather of its own hours at its place (kept
  place, else home's and said so; ADR 0355), a private one left out;
- the facts: the Mac's stretches, and a thing moved today by mail.
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
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.plan_run import Hooks, PlanWatch, make_route_legs
from jarvis.shared import lang
from jarvis.state import places, plan, things
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log, open_runtime_event_log
from jarvis.state.memory_db import open_memory_db
from jarvis.state.plan_facts import facts_at
from tests.support.clock import FakeClock
from tests.support.places import stay

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)
SETTINGS = runtime.DayPlanSettings(at=time(4, 0), watch=True)
LEG = {"from": "Home", "leave_at": "13:20", "line": "26", "board": "Main St",
       "arrive_at": "13:55", "backup": None}
MORNING = "Good morning. The dentist is at two; leave at 1:20."
EVENING = "Good night. The report is done."


def _at(clock: str, day: date = DAY) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{clock}:00").replace(tzinfo=ZONE)


def _ms(clock: str, day: date = DAY) -> int:
    return int(_at(clock, day).timestamp() * 1000)


def _item(item_id: str = "p1", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    base: dict[str, Any] = {
        "item_id": item_id, "title": "Write the report", "firmness": "movable", "source": "goal",
        "start": "09:00", "minutes": 60,
    }
    return plan.PlanItem(**(base | fields))


def _task(item_id: str = "p1", title: str = "Write the report", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    return _item(item_id, title=title, start=None, **fields)


DENTIST = _item(
    "p9", title="Dentist", firmness="fixed", source="item:thing-c1", start="14:00", place="Clinic",
    route=LEG,
)


class Rig:
    """The watcher over fresh stores, its stubs and what they were asked."""

    def __init__(self, root: Path, *, open_leg: Any = None) -> None:  # noqa: ANN401
        """Stores under ``root``; the model answers the morning and the evening from the rig."""
        root.mkdir(parents=True, exist_ok=True)
        self.log = root / "events.db"
        self.memory = root / "memory.db"
        open_event_log(self.log).close()
        open_memory_db(self.memory).close()
        self.asked: list[dict[str, Any]] = []
        self.failures = 0
        self.unfinished: list[dict[str, str]] = []
        self.pushed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.quiet = "off"
        self.at_mac = True
        self.on_call = False
        self.hooks = Hooks(
            quiet=lambda: self.quiet, at_mac=lambda: self.at_mac, on_call=lambda: self.on_call,
            push=lambda *a, **k: self.pushed.append((a, k)),
        )
        self.outing_asked: list[tuple[tuple[float, float] | None, datetime, datetime]] = []
        self.outing_down = False
        self.sources = runtime.DayPlanSources(
            self.log, self.memory, ZONE, DaySources(ZONE, None, self.memory),
            weather=lambda _day: {"summary": "rain"}, outing_weather=self._outing_weather,
        )
        self.watch = PlanWatch(
            settings=SETTINGS, sources=self.sources, ask=self._ask, open_leg=open_leg,
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

    def _ask(self, _system: str, user: str) -> str:
        body = json.loads(user)
        self.asked.append(body)
        if self.failures:
            self.failures -= 1
            msg = "down"
            raise TimeoutError(msg)
        if body["task"] == "morning":
            return json.dumps({"message": MORNING})
        return json.dumps({"message": EVENING, "unfinished": self.unfinished})

    def tasks_asked(self) -> list[str]:
        """The task of each model call, in order."""
        return [body["task"] for body in self.asked]

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
                conn, type="phone.state_observed", ts_epoch_ms=_ms(at),
                ingestion_node="phone", payload={"signal": "focus", "value": "off"},
            )

    def mac(self, at: str, kind: str) -> None:
        """TimeSink says the Mac's latest state at ``at`` is ``kind``."""
        head = {"status": "ok", "identity": "i", "span_high": 1, "span_latest_end": None,
                "capture_high": 0, "capture_latest_seen": None, "state_high": 1,
                "state_latest": {"at": _at(at).isoformat(), "kind": kind},
                "observed_at_ms": _ms(at), "actor": "observer"}
        with closing(self.conn()) as conn:
            emit_event(conn, type="timesink.state_observed", ts_epoch_ms=_ms(at), payload=head)

    def thing(
        self, title: str, *, by: str = "mail", start: str = "15:00", at: str = "10:00",
        days: int = 0, **fields: Any,  # noqa: ANN401
    ) -> str:
        """Note a thing with an hour at ``at`` as ``by``; its id."""
        when = (_at(start) + timedelta(days=days)).isoformat()
        one = things.Thing(things.new_id(), title, "go", start_at=when, **fields)
        with closing(self.conn()) as conn:
            return things.add(
                conn, one, by=by, evidence="a mail", zone=ZONE, now=_at(at),
            )[0].thing_id

    def said(self) -> dict[str, plan.Said]:
        """Today's lines by key, as the log holds them."""
        with closing(self.conn()) as conn:
            return plan.said(conn, DAY)

    def record(self, record_id: str) -> str | None:
        """The text of conversation record ``record_id`` by her, or None."""
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


def _new_line(title: str, when: str, day: str, place: str = "") -> str:
    held = lang.t("plan.new_place", place=place) if place else ""
    return lang.t(
        "plan.new_thing", when=f"{lang.t('plan.new_' + day)} {when}", title=title, place=held,
    )


# --- the morning ----------------------------------------------------------------------------


def test_waking_says_the_morning_line_once_and_never_follows_it_up(rig: Rig) -> None:
    """Her record, plan.said, one model call, nothing pushed at the Mac; then silence all day."""
    rig.drafted(_item())
    assert rig.watch.tick(_at("07:05")) == ["morning"]
    assert rig.record(f"plan-{DAY}-morning") == MORNING
    (said,) = rig.said().values()
    assert (said.key, said.kind, said.text, said.record_id) == (
        "morning", "morning", MORNING, f"plan-{DAY}-morning",
    )
    assert rig.pushed == []
    minutes = [_at("07:06") + timedelta(minutes=5 * n) for n in range(190)]
    assert [k for m in minutes if m.time() < time(22, 0) for k in rig.watch.tick(m)] == []
    assert rig.tasks_asked() == ["morning"]
    with closing(open_memory_db(rig.memory)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM records WHERE source = 'jarvis'").fetchone()
    assert count[0] == 1


def test_the_morning_prompt_is_the_plan_the_weather_and_the_goals(rig: Rig) -> None:
    """The model is asked task morning with the day's plan, spoken, and the forecast."""
    rig.drafted(_item("p1", title="Dentist", start="14:00", place="Clinic", route=LEG),
                _task("p2", "Email Sam"))
    rig.watch.tick(_at("07:05"))
    (body,) = rig.asked
    assert (body["task"], body["day"], body["draft_failed"], body["weather"]) == (
        "morning", "2026-10-16", False, {"summary": "rain"},
    )
    assert body["plan"][0]["route"] == {"leave_at": "1:20 PM", "line": "26", "board": "Main St"}
    assert [p["title"] for p in body["plan"]] == ["Dentist", "Email Sam"]


def test_the_morning_gets_today_at_home_and_each_outing_its_own_weather(rig: Rig) -> None:
    """Each timed item away from home, soonest first, with the hours of its own stretch."""
    rig.keep("clinic", 48.46, -123.31)
    rig.drafted(
        _item("p1", title="Write the report", start=None),  # no place: not an outing
        _item("p2", title="Lunch", place="Mayfair Mall", start="12:00", minutes=45),
        _item("p3", title="Standup", place="Home", start="08:00", minutes=15),  # at home
        DENTIST,  # 14:00-15:00 at the kept clinic, leaving 13:20
        _item("p4", title="Errands", place="Downtown", start=None),  # no time: no stretch
        _item("p5", title="Therapy", place="Oak Bay", start="16:00", private=True),  # private
    )
    rig.watch.tick(_at("07:05"))
    (body,) = rig.asked
    assert body["weather"] == {"summary": "rain"}
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


def test_a_morning_with_no_outing_weather_still_comes_without_it(tmp_path: Path) -> None:
    """A reader that fails leaves the outing out; the morning line still goes."""
    rig = Rig(tmp_path)
    rig.outing_down = True
    rig.drafted(DENTIST)
    assert rig.watch.tick(_at("07:05")) == ["morning"]
    assert rig.asked[0]["outings"] == []


def test_a_day_whose_draft_failed_still_has_a_morning_line_with_that_fact(rig: Rig) -> None:
    """No plan, a failed draft: the model is asked with draft_failed, once."""
    with closing(rig.conn()) as conn:
        plan.draft_failed(conn, DAY, "TimeoutError: slow")
    rig.woke("07:00")
    assert rig.watch.tick(_at("07:05")) == ["morning"]
    assert rig.watch.tick(_at("07:06")) == []
    assert [(b["draft_failed"], b["plan"]) for b in rig.asked] == [(True, [])]


def test_a_day_with_no_plan_and_no_failure_is_left_alone(rig: Rig) -> None:
    """Nothing to speak of: no line, no call."""
    rig.woke("07:00")
    assert rig.watch.tick(_at("07:05")) == []
    assert rig.asked == []


def test_a_private_item_reaches_the_model_by_its_time_alone(rig: Rig) -> None:
    """The program masks: the title and place never leave the log."""
    rig.drafted(_item(title="Therapy", place="Dr Lin's office", private=True))
    rig.watch.tick(_at("07:05"))
    (user,) = [json.dumps(b) for b in rig.asked]
    assert "Therapy" not in user
    assert "Lin" not in user


@pytest.mark.parametrize(
    ("level", "at_mac", "pushed"),
    [("off", False, 1), ("quiet", False, 1), ("off", True, 0), ("no-pop", False, 0),
     ("dnd", False, 0), ("dnd", True, 0)],
)
def test_the_line_is_always_her_record_and_a_silent_push_only_away_from_the_mac(
    rig: Rig, level: str, *, at_mac: bool, pushed: int,
) -> None:
    """no-pop and dnd keep it in the conversation; at the Mac nothing is spoken or pushed."""
    rig.quiet, rig.at_mac = level, at_mac
    rig.drafted(_item())
    assert rig.watch.tick(_at("07:05")) == ["morning"]
    assert rig.record(f"plan-{DAY}-morning") == MORNING
    assert len(rig.pushed) == pushed
    if pushed:
        (args, kwargs), = rig.pushed
        assert args == ("Jarvis", MORNING)
        assert kwargs == {"thread": "talk", "time_sensitive": False, "sound": False}


def test_a_call_holds_the_line_to_the_next_minute_and_nothing_is_written_meanwhile(
    rig: Rig,
) -> None:
    """During the call: no model call, no record, no plan.said. After it: all, once."""
    rig.at_mac = False
    rig.drafted(_item())
    rig.on_call = True
    assert rig.watch.tick(_at("07:05")) == []
    assert (rig.asked, rig.said(), rig.record(f"plan-{DAY}-morning")) == ([], {}, None)
    rig.on_call = False
    assert rig.watch.tick(_at("07:06")) == ["morning"]
    assert rig.tasks_asked() == ["morning"]
    assert len(rig.pushed) == 1
    assert rig.watch.tick(_at("07:07")) == []


def test_a_model_that_fails_is_tried_three_times_and_then_the_day_goes_without(rig: Rig) -> None:
    """Three failures, nothing written; a fourth minute is not even tried."""
    rig.failures = 10
    rig.drafted(_item())
    assert [rig.watch.tick(_at(f"07:0{n}")) for n in range(5, 9)] == [[], [], [], []]
    assert rig.tasks_asked() == ["morning"] * 3
    assert (rig.said(), rig.record(f"plan-{DAY}-morning")) == ({}, None)


def test_a_model_that_answers_badly_once_is_tried_again(rig: Rig) -> None:
    """A failure is not a day without: the next minute asks again."""
    rig.failures = 1
    rig.drafted(_item())
    assert rig.watch.tick(_at("07:05")) == []
    assert rig.watch.tick(_at("07:06")) == ["morning"]
    assert rig.record(f"plan-{DAY}-morning") == MORNING


# --- a new hard thing -----------------------------------------------------------------------


def test_a_mail_thing_for_today_after_the_morning_is_one_fixed_line_with_no_model(
    rig: Rig,
) -> None:
    """Her record, plan.said and a silent push away; the model is not asked again."""
    rig.at_mac = False
    rig.drafted(_item())
    rig.watch.tick(_at("08:00"))
    thing_id = rig.thing("Parent meeting", start="15:00", at="10:00", place="School")
    assert rig.watch.tick(_at("10:01")) == [f"new:{thing_id}"]
    expected = _new_line("Parent meeting", "15:00", "today", "School")
    assert rig.record(f"plan-{DAY}-new:{thing_id}") == expected
    assert rig.said()[f"new:{thing_id}"].kind == "new"
    assert [a[1] for a, _ in rig.pushed] == [MORNING, expected]
    assert rig.tasks_asked() == ["morning"]
    assert rig.watch.tick(_at("10:02")) == []


def test_a_thing_for_tomorrow_a_mirror_move_and_a_private_one_are_told(rig: Rig) -> None:
    """The mirror counts; a move of yesterday's thing counts; a private one is its time only."""
    rig.drafted(_item())
    rig.watch.tick(_at("08:00"))
    visa = rig.thing("Visa interview", by="mirror", start="10:00", at="09:00", days=1,
                     private=True)
    moved = rig.thing("Class", by="mail", start="13:00", at="07:30", place="Room 2")
    with closing(rig.conn()) as conn:
        earlier = things.Thing(things.new_id(), "Budget review", "go",
                               start_at=_at("11:00", DAY - timedelta(days=1)).isoformat())
        old_id = things.add(conn, earlier, by="her", evidence="said so", zone=ZONE,
                            now=_at("09:00", DAY - timedelta(days=1)))[0].thing_id
        things.move(conn, old_id, start_at=_at("16:00").isoformat(), by="mirror",
                    evidence="the calendar", zone=ZONE, now=_at("10:30"))
    keys = rig.watch.tick(_at("10:45"))
    assert keys == [f"new:{visa}", f"new:{old_id}"]
    assert f"new:{moved}" not in rig.said()  # noted before the morning line went out
    assert rig.record(f"plan-{DAY}-new:{visa}") == lang.t("plan.private", time="10:00")
    assert rig.record(f"plan-{DAY}-new:{old_id}") == _new_line("Budget review", "16:00", "today")


def test_things_from_his_own_words_are_not_told_back_to_him(rig: Rig) -> None:
    """her, pass and him wrote them: no new line, whatever the hour."""
    rig.drafted(_item())
    rig.watch.tick(_at("08:00"))
    for by in ("her", "pass", "him"):
        rig.thing(f"Mine {by}", by=by, at="10:00")
    assert rig.watch.tick(_at("10:05")) == []
    assert list(rig.said()) == ["morning"]


def test_a_third_new_thing_after_two_is_not_told(rig: Rig) -> None:
    """Two new lines fill the day; the third waits for nothing."""
    rig.drafted(_item())
    rig.watch.tick(_at("08:00"))
    ids = [rig.thing(f"Meeting {n}", start=f"1{n}:00", at=f"09:0{n}") for n in range(2, 6)]
    assert rig.watch.tick(_at("10:00")) == [f"new:{i}" for i in ids[:2]]
    assert rig.watch.tick(_at("10:01")) == []
    assert rig.watch.tick(_at("11:00")) == []


def test_a_thing_noted_before_the_morning_line_is_left_to_it(rig: Rig) -> None:
    """The morning line holds what was there when it was written."""
    rig.drafted(_item(), wake="07:00")
    rig.thing("Early mail", at="06:30")
    rig.watch.tick(_at("08:00"))
    assert rig.watch.tick(_at("08:05")) == []
    assert list(rig.said()) == ["morning"]


# --- the evening ----------------------------------------------------------------------------


def _evening_rig(rig: Rig, *unfinished: dict[str, str]) -> None:
    rig.unfinished = list(unfinished)
    rig.drafted(
        _task("p1", "Write the report"), _task("p2", "Email Sam", minutes=30),
        _task("p3", "Fix the bike", done=True), DENTIST,
    )
    rig.watch.tick(_at("08:00"))


def test_the_evening_line_goes_when_he_is_home_from_elsewhere_and_closes_the_day(
    rig: Rig,
) -> None:
    """Library until 20:25, home at 20:30: ten minutes later the evening, plan.closed, no more."""
    rig.at_mac = False
    _evening_rig(
        rig, {"id": "p1", "to": "tomorrow"}, {"id": "p2", "to": "drop"},
    )
    with closing(rig.conn()) as conn:
        stay(conn, "v1", 49.2, -123.1, _ms("15:00"), _ms("20:25"), "Library")
        stay(conn, "v2", 49.3, -123.2, _ms("20:30"), None, "Home")
    assert rig.watch.tick(_at("20:39")) == []
    assert rig.watch.tick(_at("20:40")) == ["evening"]
    assert rig.record(f"plan-{DAY}-evening") == EVENING
    assert rig.said()["evening"].kind == "evening"
    assert rig.pushed[-1][0] == ("Jarvis", EVENING)
    body = rig.asked[-1]
    assert (body["task"], [p["id"] for p in body["planned"]]) == (
        "evening", ["p1", "p2", "p3", "p9"],
    )
    assert [p["done"] for p in body["planned"]] == [False, False, True, False]
    with closing(rig.conn()) as conn:
        closed = plan.closed(conn, DAY)
    assert closed == (
        plan.Unfinished("p1", "Write the report", "tomorrow"),
        plan.Unfinished("p2", "Email Sam", "drop"),
    )
    assert rig.watch.tick(_at("20:41")) == []
    assert rig.watch.tick(_at("23:00")) == []


def test_the_evening_line_goes_when_the_mac_is_locked_for_twenty_minutes(rig: Rig) -> None:
    """Locked at 21:10: at 21:29 not yet; at 21:30 the evening, and tomorrow's things ride along."""
    _evening_rig(rig)
    rig.thing("Parent meeting", by="her", start="17:00", at="09:00", days=1, place="School")
    rig.thing("Gym", by="her", start="07:30", at="09:00", days=1)
    rig.mac("21:10", "lock")
    assert rig.watch.tick(_at("21:29")) == []
    assert rig.watch.tick(_at("21:30")) == ["evening"]
    body = rig.asked[-1]
    assert [t["title"] for t in body["tomorrow"]] == ["Gym", "Parent meeting"]
    with closing(rig.conn()) as conn:
        closed = plan.closed(conn, DAY)
    assert closed == (  # she left both tasks out: later is her default
        plan.Unfinished("p1", "Write the report", "later"),
        plan.Unfinished("p2", "Email Sam", "later"),
    )


def test_a_dinner_break_at_the_mac_is_not_the_end_of_the_day(rig: Rig) -> None:
    """Idle at 19:00, locked 18:00 to 19:40, and back: nothing until the fallback hour."""
    _evening_rig(rig)
    rig.mac("18:00", "lock")
    rig.mac("18:45", "unlock")
    rig.mac("19:00", "idle")
    rig.mac("19:40", "active")
    quiet = [rig.watch.tick(_at(clock)) for clock in ("19:50", "21:30", "22:29")]
    assert quiet == [[], [], []]
    assert rig.watch.tick(_at("22:30")) == ["evening"]


def test_the_evening_line_goes_at_the_fallback_hour_without_any_signal(rig: Rig) -> None:
    """An hour before day_ends (23:30): the evening, once."""
    _evening_rig(rig)
    assert rig.watch.tick(_at("22:29")) == []
    assert rig.watch.tick(_at("22:30")) == ["evening"]
    assert rig.watch.tick(_at("22:31")) == []
    assert rig.tasks_asked() == ["morning", "evening"]


def test_a_destination_the_model_invents_is_dropped_and_a_failed_evening_is_not_closed(
    rig: Rig,
) -> None:
    """Bad `to` becomes later; a model that fails writes neither the line nor plan.closed."""
    _evening_rig(rig, {"id": "p1", "to": "someday"})
    rig.failures = 3
    assert [rig.watch.tick(_at(f"22:3{n}")) for n in range(3)] == [[], [], []]
    with closing(rig.conn()) as conn:
        assert plan.closed(conn, DAY) is None
    assert "evening" not in rig.said()
    assert rig.watch.tick(_at("22:40")) == []  # the day goes without it
    assert rig.tasks_asked() == ["morning", "evening", "evening", "evening"]


def test_the_next_days_draft_carries_the_tomorrow_tasks(rig: Rig) -> None:
    """Only `tomorrow` rides on, with the source of the task; its source is a known one."""
    rig.unfinished = [{"id": "p1", "to": "tomorrow"}, {"id": "p2", "to": "later"}]
    rig.drafted(
        _task("p1", "Renew the lease", source="item:thing-zz9", minutes=25),
        _task("p2", "Email Sam"), wake="07:00",
    )
    assert rig.watch.tick(_at("22:30")) == ["evening"]
    inputs = runtime.gather(
        rig.sources, SETTINGS, DAY + timedelta(days=1), _at("04:00", DAY + timedelta(days=1)),
    )
    assert inputs.carried == (
        {"title": "Renew the lease", "source": "item:thing-zz9", "minutes": 25},
    )
    assert "item:thing-zz9" in inputs.sources
    payload = inputs.payload(zone_name="America/Vancouver", today=DAY + timedelta(days=1))
    assert payload["carried"] == list(inputs.carried)
    same_day = runtime.gather(rig.sources, SETTINGS, DAY, _at("04:00"))
    assert same_day.carried == ()


# --- muting through the plan tool -------------------------------------------------------------


def _tool(rig: Rig) -> Callable[[dict[str, Any]], dict[str, Any]]:
    (tool,) = build_plan_tool()

    def call(args: dict[str, Any]) -> dict[str, Any]:
        with closing(rig.conn()) as conn:
            ctx = ToolContext(conn, SimpleNamespace(), "a1", FakeClock(_at("09:30")))
            return dict(tool.handler(args, ctx))

    return call


def test_a_kind_he_mutes_is_not_said_until_he_asks_for_it_back(rig: Rig) -> None:
    """Muting morning: no line; on false it speaks at the next minute. Other kinds go on."""
    call = _tool(rig)
    rig.drafted(_item())
    assert call({"action": "mute", "kind": "morning"})["muted"] == ["morning"]
    assert call({"action": "read"})["muted"] == ["morning"]
    assert rig.watch.tick(_at("07:05")) == []
    assert rig.asked == []
    call({"action": "mute", "kind": "new"})
    rig.thing("Parent meeting", at="08:30")
    assert rig.watch.tick(_at("08:31")) == []  # the new line is off too
    assert call({"action": "mute", "kind": "morning", "on": False})["muted"] == ["new"]
    assert rig.watch.tick(_at("07:06")) == ["morning"]


def test_with_the_morning_muted_a_new_thing_is_still_told_after_he_woke(rig: Rig) -> None:
    """No morning line will hold it, so the first sign of waking stands in."""
    call = _tool(rig)
    rig.drafted(_item())
    call({"action": "mute", "kind": "morning"})
    thing_id = rig.thing("Parent meeting", at="08:30")
    assert rig.watch.tick(_at("08:31")) == [f"new:{thing_id}"]


def test_the_plan_tool_mutes_by_kind_and_has_no_settle(rig: Rig) -> None:
    """The kinds are the four; anything else is an argument error; settle is gone."""
    (tool,) = build_plan_tool()
    schema = tool.input_schema["properties"]
    assert schema["action"]["enum"] == ["read", "change", "mute"]
    assert schema["kind"]["enum"] == ["morning", "new", "leave", "evening"]
    assert "settl" not in tool.description
    call = _tool(rig)
    for bad in ({"action": "settle"}, {"action": "mute"}, {"action": "mute", "kind": "noon"},
                {"action": "mute", "kind": "new", "on": "yes"}):
        with pytest.raises(ToolError):
            call(bad)
    with closing(rig.conn()) as conn:
        assert plan.muted(conn) == frozenset()


# --- the day reply ------------------------------------------------------------------------------


def test_the_day_reply_shows_her_latest_line_of_the_day(rig: Rig) -> None:
    """None before she says anything; then the latest plan.said, beside the day prose's line."""
    now_ms = _ms("23:00")
    assert read_day(rig.log, ZONE, rig.memory, None, DAY, now_ms)["plan_said"] is None
    rig.drafted(_item())
    rig.watch.tick(_at("08:00"))
    thing_id = rig.thing("Parent meeting", at="10:00")
    rig.watch.tick(_at("10:01"))
    said = rig.said()
    page = read_day(rig.log, ZONE, rig.memory, None, DAY, now_ms)
    assert page["plan_said"] == {
        "kind": "new", "text": said[f"new:{thing_id}"].text, "at_ms": said[f"new:{thing_id}"].at_ms,
    }
    assert "line" in page


# --- the facts ----------------------------------------------------------------------------------


def test_the_macs_idle_locked_and_asleep_stretches_are_folded_up_to_now(rig: Rig) -> None:
    """A resume ends its own stretch; one not ended runs on; marks after now are unseen."""
    for at, kind in (("12:00", "idle"), ("12:10", "active"), ("13:00", "lock"),
                     ("13:30", "sleep"), ("13:45", "wake"), ("14:00", "unlock"),
                     ("15:00", "sleep"), ("16:00", "wake")):
        rig.mac(at, kind)
    with closing(rig.conn()) as conn:
        facts = facts_at(conn, DAY, _ms("15:30"), ZONE, [])
        later = facts_at(conn, DAY, _ms("17:00"), ZONE, [])
    assert [(a.kind, a.start_ms, a.end_ms) for a in facts.mac_away] == [
        ("idle", _ms("12:00"), _ms("12:10")), ("lock", _ms("13:00"), _ms("14:00")),
        ("sleep", _ms("13:30"), _ms("13:45")), ("sleep", _ms("15:00"), None),
    ]
    assert later.mac_away[-1].end_ms == _ms("16:00")


def test_a_thing_moved_today_by_mail_counts_at_the_time_of_the_move_and_a_close_removes_it(
    rig: Rig,
) -> None:
    """The writer of the last add or move is kept; closed things stand no more."""
    with closing(rig.conn()) as conn:
        one = things.Thing(things.new_id(), "Checkup", "go", start_at=_at("11:00").isoformat())
        thing_id = things.add(conn, one, by="her", evidence="said so", zone=ZONE,
                              now=_at("07:00", DAY - timedelta(days=2)))[0].thing_id
        gone = things.add(conn, things.Thing(
            things.new_id(), "Cancelled", "go", start_at=_at("12:00").isoformat(),
        ), by="mail", evidence="a mail", zone=ZONE, now=_at("08:00"))[0].thing_id
        things.move(conn, thing_id, start_at=_at("15:00").isoformat(), by="mail",
                    evidence="a mail", zone=ZONE, now=_at("09:00"))
        things.close(conn, gone, "cancelled", by="mail", evidence="a mail", zone=ZONE,
                     now=_at("09:30"))
        facts = facts_at(conn, DAY, _ms("10:00"), ZONE, [])
    (noted,) = facts.things
    assert (noted.title, noted.start, noted.day, noted.by, noted.noted_at_ms) == (
        "Checkup", "15:00", DAY, "mail", _ms("09:00"),
    )


# --- the legs of the plan -----------------------------------------------------------------------


def test_the_watcher_computes_the_legs_a_plan_lacks_once(tmp_path: Path) -> None:
    """A placed item with no leg gets one, written as a revision by the program; not again."""
    calls: list[tuple[Any, ...]] = []

    @contextmanager
    def open_leg(action_id: str) -> Iterator[Any]:
        def plan_leg(origin: str, dest: str, *, arrive_by: str) -> dict[str, Any]:
            calls.append((action_id, origin, dest, arrive_by))
            option = {"leave_at": "13:25", "arrive_at": "13:58",
                      "legs": [{"route": "28", "board_stop": "Main St"}]}
            return {"chosen": option, "backup": None}

        yield plan_leg

    rig = Rig(tmp_path, open_leg=open_leg)
    rig.drafted(replace(DENTIST, route=None), wake=None)
    rig.watch.tick(_at("07:05"))
    rig.watch.tick(_at("07:06"))
    assert calls == [(f"plan-{DAY}-routes", "home", "Clinic", f"{DAY.isoformat()} 14:00")]
    assert rig.today().items[0].route is not None
    assert rig.today().items[0].route["line"] == "28"  # type: ignore[index]
    with closing(rig.conn()) as conn:
        (revision,) = plan.revisions(conn, DAY, DAY, by="program")
    assert revision.why == "legs"


def test_the_settings_read_the_watch_keys_and_ignore_the_old_ones() -> None:
    """Watch is on only for true; line_preset is a name; the ADR 0291 keys are ignored."""
    parsed = runtime.DayPlanSettings.from_config({
        "enabled": True, "watch": True, "line_preset": "sonnet", "decide_preset": "x",
        "told_per_day": 3, "morning_push": True,
    })
    assert parsed is not None
    assert (parsed.watch, parsed.line_preset) == (True, "sonnet")
    off = runtime.DayPlanSettings.from_config({"enabled": True})
    assert off is not None
    assert (off.watch, off.line_preset, off.day_ends) == (False, "haiku", time(23, 30))
    ends = runtime.DayPlanSettings.from_config({"enabled": True, "day_ends": "22:15"})
    assert ends is not None
    assert ends.day_ends == time(22, 15)
    assert runtime.day_ends_of({"day_ends": "late"}) == time(23, 30)


# --- the plan tool -------------------------------------------------------------------------


def test_the_plan_tool_reads_and_changes(rig: Rig) -> None:
    """Read gives compact rows; change is one revision by him."""
    rig.drafted(_item(place="Library"), DENTIST, wake=None)
    call = _tool(rig)
    read = call({"action": "read"})
    assert (read["source"], read["muted"]) == ("drafted", [])
    assert "settled" not in read
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
    """Read says there is no plan; change refuses; a bad date is an argument error."""
    call = _tool(rig)
    assert call({"action": "read", "day": "2026-10-17"})["plan"] is False
    with pytest.raises(ToolError, match="no plan yet"):
        call({"action": "change", "day": "2026-10-17", "changes": [{"op": "drop", "id": "p1"}]})
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


# --- the replay script --------------------------------------------------------------------------


def test_the_replay_runs_the_watcher_over_copies_and_writes_nothing_to_the_given_files(
    rig: Rig,
) -> None:
    """A day replays to its morning, new thing and evening; the originals are untouched."""
    from scripts import plan_day_replay as script  # noqa: PLC0415 - the script is not a package

    rig.drafted(_item())
    rig.thing("Parent meeting", at="10:00", place="School")
    rig.mac("21:10", "lock")
    before = [path.read_bytes() for path in (rig.log, rig.memory)]
    lines = script.replay(rig.log, rig.memory, DAY, ZONE, script.stub_ask)
    assert [(at, kind) for at, kind, _key, _text in lines] == [
        ("07:00", "morning"), ("10:00", "new"), ("21:30", "evening"),
    ]
    assert [text for _at_, kind, _key, text in lines if kind != "new"] == [script.STUB] * 2
    assert script.summary(lines) == "morning 1, new 1, leave 0, evening 1; total 3"
    assert [path.read_bytes() for path in (rig.log, rig.memory)] == before
    with pytest.raises(SystemExit):
        script.replay(rig.log, rig.memory, DAY + timedelta(days=3), ZONE, script.stub_ask)


# --- leave-by rings -------------------------------------------------------------------------


def _leg_item(thing_id: str, item_id: str = "p9", **fields: Any) -> plan.PlanItem:  # noqa: ANN401
    return replace(DENTIST, item_id=item_id, source=f"item:{thing_id}", **fields)


def _ring(rig: Rig, thing_id: str) -> tuple[tuple[str, ...], str]:
    with closing(rig.conn()) as conn:
        one = things.get(conn, thing_id)
    return one.rings, one.rings_from


def test_a_default_ring_becomes_the_leave_time_of_the_leg_once(rig: Rig) -> None:
    """The dentist at 14:00 rings at 13:20 (40 min before), logged once as a leave line."""
    dentist = rig.thing("Dentist", start="14:00", at="06:00", rings=("-30m",), rings_from="default")
    rig.drafted(_leg_item(dentist))
    assert "leave:" + dentist in rig.watch.tick(_at("07:05"))
    assert _ring(rig, dentist) == (("-40m",), "leave")
    assert rig.said()[f"leave:{dentist}"].record_id == ""
    assert [k for k in rig.watch.tick(_at("07:06")) if k.startswith("leave:")] == []
    with closing(rig.conn()) as conn:
        (due,) = things.ring_times(conn, _ms("00:00"), _ms("23:59"), zone=ZONE)
    assert due.due_ms == _ms("13:20")


def test_rings_he_asked_for_and_a_moved_thing_keep_their_rings(rig: Rig) -> None:
    """Asked rings stay; an item that is an old copy of a moved thing sets nothing."""
    asked = rig.thing("Dentist", start="14:00", at="06:00", rings=("-90m",))
    moved = rig.thing("Haircut", start="16:00", at="06:00", rings=("-30m",), rings_from="default")
    rig.drafted(_leg_item(asked), _leg_item(moved, "p8", start="15:00"))
    assert [k for k in rig.watch.tick(_at("07:05")) if k.startswith("leave:")] == []
    assert _ring(rig, asked) == (("-90m",), "asked")
    assert _ring(rig, moved) == (("-30m",), "default")


def test_leave_rings_share_the_cap_with_new_things_and_obey_the_mute(rig: Rig) -> None:
    """Two new-thing lines leave no room for a leave ring; a muted kind sets none either."""
    dentist = rig.thing("Dentist", start="14:00", at="06:00", rings=("-30m",), rings_from="default")
    with closing(rig.conn()) as conn:
        for n in (1, 2):
            plan.say(conn, DAY, f"new:thing-x{n}", "new", "told", f"r{n}", _ms("06:30"))
    rig.drafted(_leg_item(dentist))
    assert [k for k in rig.watch.tick(_at("07:05")) if k.startswith("leave:")] == []
    assert _ring(rig, dentist) == (("-30m",), "default")
    other = Rig(rig.log.parent / "muted")
    clinic = other.thing("Dentist", start="14:00", at="06:00", rings=("-30m",),
                         rings_from="default")
    with closing(other.conn()) as conn:
        plan.mute(conn, "leave", muted=True)
    other.drafted(_leg_item(clinic))
    other.watch.tick(_at("07:05"))
    assert _ring(other, clinic) == (("-30m",), "default")
