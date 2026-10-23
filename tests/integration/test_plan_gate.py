"""ADR 0383: which unprompted plan lines are due, as a table over the gate (pure, no I/O).

Facts are built by hand, the clock is an argument, and the examples are invented. Each row is
one rule: due, not due, switched off, capped, already said, outside its hours.

- morning: the first sign he is up, before 17:00, once, with a plan or its failure;
- new: a thing with an hour that mail or the mirror added or moved after the morning line, never
  one from his own words, at most two with the leave-by lines, once per thing;
- evening: home from elsewhere after 20:00 for ten minutes, the Mac locked or asleep after 21:00
  for twenty, or an hour before the day ends; once.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision import plan_gate
from jarvis.state.plan import Plan, PlanItem
from jarvis.state.plan_facts import DayFacts, MacAway, NotedThing, Stay, Wake

if TYPE_CHECKING:
    from collections.abc import Collection

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)
DAY_ENDS = time(23, 30)
HOME = frozenset({"home", "flat 4b"})
PLAN = Plan(DAY, (PlanItem("p1", "Write the report", "movable", "goal", minutes=60),))


def _at(clock: str, day: date = DAY) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


def _ms(clock: str) -> int:
    return int(_at(clock).timestamp() * 1000)


def _facts(**fields: Any) -> DayFacts:  # noqa: ANN401
    base: dict[str, Any] = {
        "now_ms": 0, "wake": None, "here": None, "stays": (), "moves": (), "things": (),
        "mac_away": (),
    }
    return DayFacts(**(base | fields))


def _wake(clock: str = "07:00") -> Wake:
    return Wake("focus_off", _ms(clock))


def _thing(  # noqa: PLR0913 - the table's knobs.
    thing_id: str = "t1", *, by: str = "mail", start: str = "15:00", noted: str = "10:00",
    day: date = DAY, private: bool = False,
) -> NotedThing:
    return NotedThing(thing_id, "Parent meeting", start, day, "School", private, by, _ms(noted))


def _stay(place: str, start: str, end: str | None = None, role: str = "") -> Stay:
    return Stay(
        _ms(start), None if end is None else _ms(end), place, place if role else "", role, 49.0,
        -123.0,
    )


def _due(  # noqa: PLR0913 - the table's knobs.
    now: str, facts: DayFacts, *, plan: Plan | None = PLAN, said: Collection[str] = (),
    muted: Collection[str] = (), first_said: str | None = None, day_ends: time = DAY_ENDS,
) -> list[tuple[str, str]]:
    lines = plan_gate.due(
        plan, facts, _at(now), said, muted, day_ends=day_ends, home=HOME,
        first_said_ms=None if first_said is None else _ms(first_said),
    )
    return [(line.key, line.kind) for line in lines]


# --- the morning ------------------------------------------------------------------------------


def test_the_morning_line_is_due_at_the_first_sign_he_is_up_and_says_which() -> None:
    """Wake at 07:00: due once, with the signal and its time."""
    lines = plan_gate.due(
        PLAN, _facts(wake=_wake()), _at("07:05"), (), (), day_ends=DAY_ENDS, home=HOME,
        first_said_ms=None,
    )
    (line,) = lines
    assert (line.key, line.kind, line.facts) == (
        "morning", "morning", {"signal": "focus_off", "at": "07:00"},
    )


@pytest.mark.parametrize(
    ("now", "facts", "plan", "said", "muted"),
    [
        ("07:05", _facts(), PLAN, (), ()),  # no sign of him yet
        ("07:05", _facts(wake=_wake()), None, (), ()),  # no plan and no failed draft
        ("16:59", _facts(wake=_wake()), PLAN, ("morning",), ()),  # already said
        ("17:00", _facts(wake=_wake()), PLAN, (), ()),  # past the morning
        ("07:05", _facts(wake=_wake()), PLAN, (), ("morning",)),  # he switched it off
    ],
)
def test_the_morning_line_is_not_due(
    now: str, facts: DayFacts, plan: Plan | None, said: tuple[str, ...], muted: tuple[str, ...],
) -> None:
    """No wake, no plan, said, from 17:00, muted: nothing."""
    assert _due(now, facts, plan=plan, said=said, muted=muted) == []


def test_a_failed_draft_still_has_a_morning_line() -> None:
    """The runtime passes a failed draft as an empty plan: the morning still goes."""
    assert _due("07:05", _facts(wake=_wake()), plan=Plan(DAY, ())) == [("morning", "morning")]


# --- a new hard thing -------------------------------------------------------------------------


def test_a_mail_or_mirror_thing_noted_after_the_morning_line_is_one_new_line() -> None:
    """Mail and the calendar mirror count; the line carries what the fixed sentence needs."""
    facts = _facts(wake=_wake(), things=(
        _thing("t1", by="mail"),
        _thing("t2", by="mirror", start="09:00", noted="10:05", day=DAY + timedelta(days=1),
               private=True),
    ))
    lines = plan_gate.due(
        PLAN, facts, _at("10:10"), ("morning",), (), day_ends=DAY_ENDS, home=HOME,
        first_said_ms=_ms("08:00"),
    )
    assert [(line.key, line.kind) for line in lines] == [("new:t1", "new"), ("new:t2", "new")]
    assert lines[0].facts == {
        "title": "Parent meeting", "start": "15:00", "day": "today", "place": "School",
        "private": False,
    }
    assert (lines[1].facts["day"], lines[1].facts["private"]) == ("tomorrow", True)


@pytest.mark.parametrize(
    ("thing", "said", "muted", "first_said"),
    [
        (_thing(by="her"), ("morning",), (), "08:00"),  # his own words are never told back
        (_thing(by="pass"), ("morning",), (), "08:00"),
        (_thing(by="him"), ("morning",), (), "08:00"),
        (_thing(noted="07:30"), ("morning",), (), "08:00"),  # the morning line holds it
        (_thing(), (), (), None),  # no morning line yet: it will hold it
        (_thing(start=""), ("morning",), (), "08:00"),  # no hour: not a hard thing
        (_thing(), ("morning", "new:t1"), (), "08:00"),  # once per thing
        (_thing(), ("morning",), ("new",), "08:00"),  # switched off
        (_thing(start="10:05"), ("morning",), (), "08:00"),  # its hour is already behind
    ],
)
def test_a_new_line_is_not_due(
    thing: NotedThing, said: tuple[str, ...], muted: tuple[str, ...], first_said: str | None,
) -> None:
    """Own words, before the morning line, no hour, said, muted, behind: nothing."""
    lines = _due("10:10", _facts(wake=_wake(), things=(thing,)), said=said, muted=muted,
                 first_said=first_said)
    assert [key for key, kind in lines if kind == "new"] == []


def test_two_new_or_leave_lines_fill_the_day_and_the_rest_wait_for_tomorrow() -> None:
    """Leave-by lines count in the pair: new + leave said means no third."""
    things = tuple(_thing(f"t{n}", noted=f"10:0{n}") for n in range(1, 5))
    facts = _facts(wake=_wake(), things=things)
    assert _due("10:30", facts, said=("morning",), first_said="08:00") == [
        ("new:t1", "new"), ("new:t2", "new"),
    ]
    assert _due("10:30", facts, said=("morning", "new:t1"), first_said="08:00") == [
        ("new:t2", "new"),
    ]
    assert _due("10:30", facts, said=("morning", "new:t1", "leave:t9"), first_said="08:00") == []


def test_with_the_morning_switched_off_the_first_sign_he_is_up_stands_in_for_the_line() -> None:
    """No morning line will ever hold a new thing: it is told if it came after he woke."""
    facts = _facts(wake=_wake("07:00"), things=(
        _thing("t1", noted="06:00"), _thing("t2", noted="09:00"),
    ))
    assert _due("10:10", facts, muted=("morning",)) == [("new:t2", "new")]


# --- the evening ------------------------------------------------------------------------------


def _evening(now: str, facts: DayFacts, **kwargs: Any) -> list[tuple[str, str]]:  # noqa: ANN401
    return _due(now, facts, said=("morning",), **kwargs)


@pytest.mark.parametrize(
    ("now", "stays", "due"),
    [
        # home from the library at 20:30, ten minutes on
        ("20:40", (_stay("Library", "15:00", "20:25"), _stay("Home", "20:30", role="home")), True),
        ("20:39", (_stay("Library", "15:00", "20:25"), _stay("Home", "20:30", role="home")), False),
        # a home known only by a name he gave it
        ("20:41", (_stay("Library", "15:00", "20:25"), _stay("Flat 4B", "20:30")), True),
        # arrived before 20:00: dinner out, not the end of the day
        ("20:40", (_stay("Library", "15:00", "19:25"), _stay("Home", "19:30", role="home")), False),
        # from home to home (a stay split in two): not coming back
        ("20:40", (_stay("Home", "15:00", "20:25", role="home"),
                   _stay("Home", "20:30", role="home")), False),
        # left home, did not come back
        ("21:40", (_stay("Home", "15:00", "20:25", role="home"), _stay("Bar", "20:30")), False),
        # home for good before; a stay that ended after ten minutes still counts
        ("22:00", (_stay("Library", "15:00", "20:25"),
                   _stay("Home", "20:30", "20:42", role="home")), True),
        ("22:00", (_stay("Library", "15:00", "20:25"),
                   _stay("Home", "20:30", "20:39", role="home")), False),
    ],
)
def test_the_evening_line_is_due_once_he_is_home_from_elsewhere_for_ten_minutes(
    now: str, stays: tuple[Stay, ...], *, due: bool,
) -> None:
    """Home (kept role or a name) after 20:00 following a stay elsewhere, for 10 minutes."""
    lines = _evening(now, _facts(wake=_wake(), stays=stays))
    assert lines == ([("evening", "evening")] if due else [])


@pytest.mark.parametrize(
    ("now", "away", "due"),
    [
        ("21:30", MacAway(_ms("21:10"), None, "lock"), True),
        ("21:29", MacAway(_ms("21:10"), None, "lock"), False),
        ("21:30", MacAway(_ms("21:10"), None, "sleep"), True),
        ("21:30", MacAway(_ms("21:10"), None, "idle"), False),  # idle is dinner, not the night
        ("21:30", MacAway(_ms("20:50"), None, "lock"), False),  # a lock before 21:00 is dinner
        ("22:00", MacAway(_ms("21:10"), _ms("21:31"), "lock"), True),  # it lasted 21 minutes
        ("22:00", MacAway(_ms("21:10"), _ms("21:29"), "lock"), False),  # a break of 19
    ],
)
def test_the_evening_line_is_due_when_the_mac_stopped_for_twenty_minutes(
    now: str, away: MacAway, *, due: bool,
) -> None:
    """Locked or asleep from 21:00 for 20 minutes, still or lasted; idle never."""
    lines = _evening(now, _facts(wake=_wake(), mac_away=(away,)))
    assert lines == ([("evening", "evening")] if due else [])


@pytest.mark.parametrize(
    ("now", "day_ends", "due"),
    [
        ("22:30", time(23, 30), True),
        ("22:29", time(23, 30), False),
        ("21:00", time(22, 0), True),
        ("20:59", time(22, 0), False),
    ],
)
def test_the_evening_line_is_due_an_hour_before_the_day_ends_whatever_else_was_seen(
    now: str, day_ends: time, *, due: bool,
) -> None:
    """The fallback: day_ends less an hour."""
    lines = _evening(now, _facts(wake=_wake()), day_ends=day_ends)
    assert lines == ([("evening", "evening")] if due else [])


def test_the_evening_line_says_which_signal_and_when_and_prefers_home_then_the_mac() -> None:
    """Facts carry the signal and its time; home beats the Mac, the Mac beats the clock."""
    home = (_stay("Library", "15:00", "20:25"), _stay("Home", "20:30", role="home"))
    mac = (MacAway(_ms("21:10"), None, "lock"),)

    def signal(facts: DayFacts) -> dict[str, str]:
        (line,) = plan_gate.due(
            PLAN, facts, _at("22:45"), ("morning",), (), day_ends=DAY_ENDS, home=HOME,
            first_said_ms=_ms("08:00"),
        )
        return line.facts

    assert signal(_facts(wake=_wake(), stays=home, mac_away=mac)) == {
        "signal": "home", "at": "20:30",
    }
    assert signal(_facts(wake=_wake(), mac_away=mac)) == {"signal": "mac", "at": "21:10"}
    assert signal(_facts(wake=_wake())) == {"signal": "late", "at": "22:30"}


def test_nothing_in_the_afternoon_makes_the_evening_line_due() -> None:
    """17:30 with the Mac locked and him on the move: still not the evening."""
    facts = _facts(
        wake=_wake(), mac_away=(MacAway(_ms("17:00"), None, "lock"),),
        stays=(_stay("Library", "09:00", "17:20"), _stay("Cafe", "17:30")),
    )
    assert _evening("18:30", facts) == []


def test_the_evening_line_is_said_once_and_a_muted_one_is_not_said() -> None:
    """Already said and switched off both hold it back; the other kinds go on."""
    facts = _facts(wake=_wake())
    assert _due("22:45", facts, said=("morning", "evening")) == []
    assert _due("22:45", facts, said=("morning",), muted=("evening",)) == []
    assert _due("22:45", facts, said=("morning",), muted=("morning",)) == [("evening", "evening")]
