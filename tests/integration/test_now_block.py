"""ADR 0249: the state block says what he is doing now and what is next on his calendar.

Real Event Log, the real producers and ``_live_lines``, then the one-turn request builder with a
stub model (no provider is called). Each check is an input and the exact line or request it
gives: what he is doing with its age, the stale warning, the next things read from the ledger of
his things (ADR 0299) and how many wait on his word, every line present and absent, the size
cap, and the order of the block.
"""

from __future__ import annotations

from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import _live_lines
from jarvis.runtime.now_block import (
    CALENDAR_PREFIX,
    DOING_PREFIX,
    LINE_CHARS,
    calendar_line,
    doing_now_line,
)
from jarvis.state import things
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_backend_prompt_assembly import HISTORY, LINE, STATUS, _drive_one_turn
from tests.integration.test_ledger import NOW, ZONE, _at

if TYPE_CHECKING:
    from datetime import datetime
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


def _iso(day: int, hour: int, minute: int = 0) -> str:
    return _at(day, hour, minute).isoformat()


Row = tuple[str, str, dict[str, Any]]
WRITE: dict[str, Any] = {"by": "her", "evidence": "said so", "zone": ZONE, "now": NOW}


def _ledger(path: Path, *rows: Row) -> Path:
    """Add these ``(title, kind, fields)`` to the ledger in the Event Log at ``path``."""
    with closing(open_event_log(path)) as conn:
        for title, kind, fields in rows:
            things.add(conn, things.Thing(things.new_id(), title, kind, **fields), **WRITE)
    return path


def _calendar(tmp_path: Path, *rows: Row) -> str | None:
    return calendar_line(_ledger(_world(tmp_path, []), *rows), ZONE, _clock)


def _go(title: str, start: str, **fields: Any) -> Row:  # noqa: ANN401 - the thing's fields
    return title, "go", {"start_at": start, **fields}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # Today's by clock, tomorrow's by name; nothing says when it was read.
        (
            [_go("Dentist", _iso(8, 15, 30)), _go("Standup", _iso(8, 17))],
            f'{CALENDAR_PREFIX}: 3:30 pm "Dentist"; 5:00 pm "Standup".',
        ),
        (
            [_go("Interview", _iso(9, 9, 30))],
            f'{CALENDAR_PREFIX}: tomorrow (Fri) 9:30 am "Interview".',
        ),
        # Two at most, soonest first, whatever order they were written in.
        (
            [_go("C", _iso(8, 18)), _go("A", _iso(8, 16)), _go("B", _iso(8, 17))],
            f'{CALENDAR_PREFIX}: 4:00 pm "A"; 5:00 pm "B".',
        ),
        # An item that has begun is never offered as the next one; two days out is not next.
        (
            [_go("Done", _iso(8, 14)), _go("Gym", _iso(8, 16)), _go("Planning", _iso(12, 14))],
            f'{CALENDAR_PREFIX}: 4:00 pm "Gym".',
        ),
        # A title is his words: one line, no double quotes, cut at 40.
        (
            [_go('Lunch with "Sam"\nroom 3', _iso(8, 16))],
            f"{CALENDAR_PREFIX}: 4:00 pm \"Lunch with 'Sam' room 3\".",
        ),
        ([_go("x" * 100, _iso(8, 16))], f'{CALENDAR_PREFIX}: 4:00 pm "{"x" * 39}…".'),
        # A private one is named only as such, an unconfirmed one says so and is counted.
        (
            [_go("Therapy", _iso(8, 16), private=True), _go("Class", _iso(8, 17), confirmed=False)],
            f'{CALENDAR_PREFIX} (1 not confirmed): 4:00 pm "something private"; '
            '5:00 pm "Class (not confirmed)".',
        ),
        # A whole day is not a clock time; a thing to do by a day or an hour says it is due.
        (
            [("Holiday", "go", {"on_day": "2026-10-08"}), _go("Dentist", _iso(8, 16))],
            f'{CALENDAR_PREFIX}: all day today "Holiday"; 4:00 pm "Dentist".',
        ),
        (
            [("Pay rent", "do", {"due": "2026-10-08"}), ("Send form", "do", {"due": _iso(9, 17)})],
            f'{CALENDAR_PREFIX}: due today "Pay rent"; due tomorrow (Fri) 5:00 pm "Send form".',
        ),
        # A week's repeat that falls tomorrow; one that does not; another offset, said in his zone.
        (
            [_go("Pottery", _iso(2, 9), weekly_until="2026-10-30")],
            f'{CALENDAR_PREFIX}: tomorrow (Fri) 9:00 am "Pottery".',
        ),
        ([_go("Pottery", _iso(3, 19), weekly_until="2026-10-30")], None),
        ([_go("Dentist", "2026-10-08T22:30:00+00:00")], f'{CALENDAR_PREFIX}: 3:30 pm "Dentist".'),
        # Nothing ahead, nothing timed, nothing in the ledger.
        ([_go("Past", _iso(8, 14))], None),
        ([("Buy milk", "do", {})], None),
        ([], None),
    ],
)
def test_the_calendar_line_table(rows: list[Row], expected: str | None, tmp_path: Path) -> None:
    """Each ledger gives the line or nothing; none past the cap."""
    line = _calendar(tmp_path, *rows)
    assert line == expected
    assert line is None or len(line) <= LINE_CHARS


def test_what_is_closed_or_cancelled_is_not_next(tmp_path: Path) -> None:
    """Done and cancelled things, and a cancelled date of a week, are left out."""
    log = _ledger(
        _world(tmp_path, []),
        _go("Gym", _iso(8, 16)), _go("Dentist", _iso(8, 17)), _go("Class", _iso(8, 18)),
        _go("Pottery", _iso(2, 9), weekly_until="2026-10-30"),
    )
    with closing(open_event_log(log)) as conn:
        by_title = {t.title: t for t in things.fold(conn).values()}
        things.close(conn, by_title["Gym"].thing_id, "done", **WRITE)
        things.close(conn, by_title["Dentist"].thing_id, "cancelled", **WRITE)
        things.close(
            conn, by_title["Pottery"].thing_id, "cancelled", occurrence="2026-10-09", **WRITE,
        )
    assert calendar_line(log, ZONE, _clock) == f'{CALENDAR_PREFIX}: 6:00 pm "Class".'


def test_the_count_of_unconfirmed_things_counts_every_open_one(tmp_path: Path) -> None:
    """Far off or with no time, an open thing he has not confirmed is counted; a closed one not."""
    log = _ledger(
        _world(tmp_path, []),
        _go("Gym", _iso(8, 16)),
        _go("Visa interview", _iso(20, 10), confirmed=False),
        ("Fix the bike", "do", {"confirmed": False}),
        _go("Old", _iso(1, 10), confirmed=False),
    )
    with closing(open_event_log(log)) as conn:
        old = next(t for t in things.fold(conn).values() if t.title == "Old")
        things.close(conn, old.thing_id, "cancelled", **WRITE)
    assert calendar_line(log, ZONE, _clock) == (
        f'{CALENDAR_PREFIX} (2 not confirmed): 4:00 pm "Gym".'
    )


def test_a_ledger_that_cannot_be_read_leaves_the_line_out(tmp_path: Path) -> None:
    """No Event Log there: no line and no error."""
    assert calendar_line(tmp_path / "missing" / "events.db", ZONE, _clock) is None


def test_the_two_lines_together_stay_near_400_characters(tmp_path: Path) -> None:
    """The worst case: a stale long claim and two long titles."""
    doing = _doing(tmp_path, [_claim(_at(8, 11), LONG)])
    calendar = _calendar(tmp_path, _go("t" * 90, _iso(8, 16)), _go("u" * 90, _iso(9, 9)))
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
    if "calendar" in expected:
        _ledger(log, _go("Dentist", _iso(8, 15, 30)))
    lines = _live_lines((
        lambda: doing_now_line(log, ZONE, _clock),
        lambda: calendar_line(log, ZONE, _clock),
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
    _ledger(log, _go("Dentist", _iso(8, 15, 30)))
    dashboard = "Dashboard: Allen is on the Mail page."
    old = _live_lines((lambda: RIDE, lambda: dashboard))
    new = _live_lines((
        lambda: RIDE,
        lambda: dashboard,
        lambda: doing_now_line(log, ZONE, _clock),
        lambda: calendar_line(log, ZONE, _clock),
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
        f'{CALENDAR_PREFIX}: 3:30 pm "Dentist".'
    )
    assert messages[2]["content"] == f"{STATUS}{block}\n\n后天呢\n\n{LINE}"
    assert len(new[2]) + len(new[3]) <= 400


def test_the_order_is_the_same_whatever_the_ages(tmp_path: Path) -> None:
    """Ages change the words inside a line, never which line comes first."""
    orders = set()
    for n, analysed in enumerate((_at(8, 14, 59), _at(8, 9))):
        (tmp_path / str(n)).mkdir()
        log = _world(tmp_path / str(n), [_claim(analysed, "Reading a paper")])
        _ledger(log, _go("Gym", _iso(8, 16)))
        lines = _live_lines((
            lambda: RIDE,
            partial(doing_now_line, log, ZONE, _clock),
            partial(calendar_line, log, ZONE, _clock),
        ))
        orders.add(tuple(one.split(" ")[0] for one in lines))
    assert orders == {("Bus", "Doing", "Next")}
