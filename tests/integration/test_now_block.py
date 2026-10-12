"""ADR 0249: the state block says what he is doing now and what is next on his calendar.

Real Event Log, the real producers and ``_live_lines``, then the one-turn request builder with a
stub model (no provider is called). Each check is an input and the exact line or request it
gives: what he is doing with its age, the stale warning, the calendar slot left empty or read,
every line present and absent, the size cap, and the order of the block.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import _live_lines
from jarvis.runtime.now_block import (
    CALENDAR_PREFIX,
    DOING_PREFIX,
    LINE_CHARS,
    CalendarGlance,
    CalendarItem,
    calendar_line,
    doing_now_line,
)
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_backend_prompt_assembly import HISTORY, LINE, STATUS, _drive_one_turn
from tests.integration.test_ledger import NOW, ZONE, _at

if TYPE_CHECKING:
    from pathlib import Path

NOTE = " Note: this analysis is old and may not describe the present."
RIDE = (
    "Bus ride: he is on route 14, boarded at Uptown at 14:20. Next stop: Shelbourne. "
    "His stop: Hillside, 3 stops left, arrives about 15:12 (in 12 min)."
)


def _clock() -> datetime:
    return NOW


def _world(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    """An Event Log holding these saved work states, oldest first."""
    log = tmp_path / "events.db"
    with closing(open_event_log(log)) as conn:
        for item in rows:
            emit_event(conn, type="work_state.revised", payload={"item": item, "trigger": "auto"})
    return log


def _claim(at: datetime, text: str) -> dict[str, Any]:
    stamp = at.isoformat()
    return {"analyzed_at": stamp, "now": {"text": text, "basis": "observed", "as_of": stamp}}


def _doing(tmp_path: Path, rows: list[dict[str, Any]]) -> str | None:
    return doing_now_line(_world(tmp_path, rows), ZONE, _clock)


LONG = "Reading the replay exam output and comparing the three models " * 8


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # Fresh: as my_day words it, with the age first and the clock after.
        (
            [_claim(_at(8, 14, 30), "Writing the my_day tool tests")],
            f"{DOING_PREFIX} (as of 30 minutes ago, at 2:30 pm): Writing the my_day tool tests.",
        ),
        (
            [_claim(_at(8, 14, 59), "Reviewing a diff")],
            f"{DOING_PREFIX} (as of 1 minute ago, at 2:59 pm): Reviewing a diff.",
        ),
        # The latest analysis wins; an older one is not mixed in.
        (
            [_claim(_at(8, 9), "Old thing"), _claim(_at(8, 14, 45), "New thing")],
            f"{DOING_PREFIX} (as of 15 minutes ago, at 2:45 pm): New thing.",
        ),
        # Over three hours: the warning my_day gives rides with it, so it is never read as now.
        (
            [_claim(_at(8, 11), "Reading a paper")],
            f"{DOING_PREFIX} (as of 4 hours ago, at 11:00 am): Reading a paper.{NOTE}",
        ),
        (
            [_claim(_at(8, 3, 30), "Reading a paper")],
            f"{DOING_PREFIX} (as of 11 hours ago, at 3:30 am): Reading a paper.{NOTE}",
        ),
        # Over twelve hours it says nothing about now: the line leaves it to my_day.
        ([_claim(_at(8, 2, 59), "Reading a paper")], None),
        ([_claim(_at(7, 13), "Reading a paper")], None),
        # Nothing saved, or an analysis that covered the day and names no current activity.
        ([], None),
        ([{"analyzed_at": _at(8, 14).isoformat(), "activities": [{"text": "Wrote tests"}]}], None),
    ],
)
def test_the_doing_now_line_table(
    tmp_path: Path, rows: list[dict[str, Any]], expected: str | None,
) -> None:
    """Each saved state reads as the line, with its age, or as nothing."""
    assert _doing(tmp_path, rows) == expected


@pytest.mark.parametrize(
    ("analysed", "tail"),
    [(_at(8, 14, 30), ""), (_at(8, 11), NOTE)],
)
def test_a_long_claim_is_cut_so_the_line_and_its_warning_fit(
    tmp_path: Path, analysed: datetime, tail: str,
) -> None:
    """The text gives way: the age and the warning stay whole inside the line cap."""
    line = _doing(tmp_path, [_claim(analysed, LONG)])
    assert line is not None
    assert len(line) <= LINE_CHARS
    assert line.endswith(f"…{'.' + tail if tail else '.'}")
    assert ("old and may not describe" in line) == bool(tail)
    assert line.startswith(f"{DOING_PREFIX} (as of ")


def _glance(read_at: datetime, *items: tuple[datetime, str]) -> CalendarGlance:
    return CalendarGlance(read_at, [CalendarItem(start, title) for start, title in items])


def _calendar(glance: CalendarGlance | None) -> str | None:
    return calendar_line(lambda: glance, ZONE, _clock)


@pytest.mark.parametrize(
    ("glance", "expected"),
    [
        # Today's items by clock, tomorrow's by name; the read's age after the heading.
        (
            _glance(_at(8, 14, 56), (_at(8, 15, 30), "Dentist"), (_at(8, 17), "Standup")),
            f'{CALENDAR_PREFIX} (read 4 minutes ago): 3:30 pm "Dentist"; 5:00 pm "Standup".',
        ),
        (
            _glance(_at(8, 13), (_at(9, 9, 30), "Interview")),
            f'{CALENDAR_PREFIX} (read 2 hours ago): tomorrow (Fri) 9:30 am "Interview".',
        ),
        (
            _glance(_at(8, 14, 59), (_at(12, 14), "Planning")),
            f'{CALENDAR_PREFIX} (read 1 minute ago): Mon Oct 12, 2:00 pm "Planning".',
        ),
        # Two at most, soonest first, whatever order the reader gave.
        (
            _glance(
                _at(8, 14, 59), (_at(8, 18), "C"), (_at(8, 16), "A"), (_at(8, 17), "B"),
            ),
            f'{CALENDAR_PREFIX} (read 1 minute ago): 4:00 pm "A"; 5:00 pm "B".',
        ),
        # An item that has begun is never offered as the next one.
        (
            _glance(_at(8, 13), (_at(8, 14), "Done"), (_at(8, 16), "Gym")),
            f'{CALENDAR_PREFIX} (read 2 hours ago): 4:00 pm "Gym".',
        ),
        # A title is his calendar's words: one line, no double quotes, cut at 40.
        (
            _glance(_at(8, 14, 59), (_at(8, 16), 'Lunch with "Sam"\nroom 3')),
            f"{CALENDAR_PREFIX} (read 1 minute ago): 4:00 pm \"Lunch with 'Sam' room 3\".",
        ),
        (
            _glance(_at(8, 14, 59), (_at(8, 16), "x" * 100)),
            f'{CALENDAR_PREFIX} (read 1 minute ago): 4:00 pm "{"x" * 39}…".',
        ),
        # Nothing ahead, nothing held, no reading.
        (_glance(_at(8, 13), (_at(8, 14), "Done")), None),
        (_glance(_at(8, 13)), None),
        (None, None),
    ],
)
def test_the_calendar_line_table(glance: CalendarGlance | None, expected: str | None) -> None:
    """Each reading of the injected reader gives the line or nothing; none past the cap."""
    line = _calendar(glance)
    assert line == expected
    assert line is None or len(line) <= LINE_CHARS


def test_without_a_calendar_reader_the_line_is_left_out() -> None:
    """The default: nothing is wired, so the block has no calendar line."""
    assert calendar_line(None, ZONE, _clock) is None


def test_an_item_is_said_in_his_zone_not_the_readers() -> None:
    """22:30 UTC is 3:30 pm at UTC-7."""
    utc_item = (datetime(2026, 10, 8, 22, 30, tzinfo=UTC), "Dentist")
    assert _calendar(_glance(_at(8, 14, 59), utc_item)) == (
        f'{CALENDAR_PREFIX} (read 1 minute ago): 3:30 pm "Dentist".'
    )


def test_the_two_lines_together_stay_near_400_characters(tmp_path: Path) -> None:
    """The worst case: a stale long claim and two long titles."""
    doing = _doing(tmp_path, [_claim(_at(8, 11), LONG)])
    calendar = _calendar(_glance(_at(8, 13), (_at(8, 16), "t" * 90), (_at(9, 9), "u" * 90)))
    assert doing is not None
    assert calendar is not None
    assert len(doing) + len(calendar) <= 2 * LINE_CHARS == 400


@pytest.mark.parametrize(
    "expected", [(), ("doing",), ("calendar",), ("doing", "calendar")],
)
def test_each_line_is_present_only_when_it_has_content(
    tmp_path: Path, expected: tuple[str, ...],
) -> None:
    """Through ``_live_lines``: the state line and the calendar line come and go separately."""
    rows = [_claim(_at(8, 14, 30), "Writing the my_day tool tests")] if "doing" in expected else []
    log = _world(tmp_path, rows)
    glance = (
        _glance(_at(8, 14, 56), (_at(8, 15, 30), "Dentist")) if "calendar" in expected else None
    )
    lines = _live_lines((
        lambda: doing_now_line(log, ZONE, _clock),
        lambda: calendar_line(lambda: glance, ZONE, _clock),
    ))
    kinds = tuple(
        "doing" if one.startswith(DOING_PREFIX) else "calendar" for one in lines
    )
    assert kinds == expected
    assert all(len(one) <= LINE_CHARS for one in lines)


def _request(tmp_path: Path, lines: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
    (tmp_path / "turn").mkdir(exist_ok=True)
    return _drive_one_turn(tmp_path / "turn", HISTORY, live_context=lines)


def test_the_lines_ride_the_end_of_the_block_and_leave_the_cached_prefix_alone(
    tmp_path: Path,
) -> None:
    """After the ride and the Dashboard line, ahead of his words; system and history unchanged."""
    log = _world(tmp_path, [_claim(_at(8, 14, 30), "Writing the my_day tool tests")])
    glance = _glance(_at(8, 14, 56), (_at(8, 15, 30), "Dentist"))
    dashboard = "Dashboard: Allen is on the Mail page."
    old = _live_lines((lambda: RIDE, lambda: dashboard))
    new = _live_lines((
        lambda: RIDE,
        lambda: dashboard,
        lambda: doing_now_line(log, ZONE, _clock),
        lambda: calendar_line(lambda: glance, ZONE, _clock),
    ))
    assert new[:2] == old
    plain_system, plain = _request(tmp_path, old)
    system, messages = _request(tmp_path, new)

    assert system == plain_system
    assert messages[:2] == plain[:2] == list(HISTORY)
    block = "\n".join(new)
    assert block == (
        f"{RIDE}\n{dashboard}\n"
        f"{DOING_PREFIX} (as of 30 minutes ago, at 2:30 pm): Writing the my_day tool tests.\n"
        f'{CALENDAR_PREFIX} (read 4 minutes ago): 3:30 pm "Dentist".'
    )
    assert messages[2]["content"] == f"{STATUS}{block}\n\n后天呢\n\n{LINE}"
    assert len(new[2]) + len(new[3]) <= 400


def test_the_order_is_the_same_whatever_the_ages(tmp_path: Path) -> None:
    """Ages change the words inside a line, never which line comes first."""
    log = _world(tmp_path, [_claim(_at(8, 11), "Reading a paper")])
    orders = set()
    for read_at in (_at(8, 14, 59), _at(8, 9)):
        read = partial(_glance, read_at, (_at(8, 16), "Gym"))
        lines = _live_lines((
            lambda: RIDE,
            lambda: doing_now_line(log, ZONE, _clock),
            partial(calendar_line, read, ZONE, _clock),
        ))
        orders.add(tuple(one.split(" ")[0] for one in lines))
    assert orders == {("Bus", "Doing", "Next")}
