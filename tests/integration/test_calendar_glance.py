"""ADR 0249: the state block's calendar line is fed by a background read, never by a turn.

The real ``Home`` over the scripted Microsoft server of ``test_schedule_tool`` (calendarView by
overlap with the bounds it was sent), a fixed clock (Monday 2026-10-12, 12:40 at UTC-7), and the
real line producer. Each check is a server state and the exact line a turn renders, or the
number of calls the server saw. No network, no key, no model.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.schedule_tool import upcoming_events
from jarvis.execution.tools import ToolError
from jarvis.runtime import _live_lines, bootstrap_runtime_app
from jarvis.runtime import now_block as block
from jarvis.runtime.home import Home
from jarvis.runtime.now_block import CALENDAR_PREFIX, KEPT, CalendarCache, calendar_line
from tests.integration.test_home_routes import _Connections
from tests.integration.test_schedule_tool import (
    LOCAL,
    NOW,
    ZONE,
    _all_day,
    _at,
    _Microsoft,
    _timed,
    _week,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

INTERVIEW = '1:30 pm "Interview: Reliable Controls"'
DENTIST = 'tomorrow (Tue) 8:00 am "Dentist"'


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _cache(server: _Microsoft | None, clock: _Clock) -> CalendarCache:
    home = Home(_Connections(server), (ZONE, LOCAL), None)  # type: ignore[arg-type]
    return CalendarCache(partial(upcoming_events, home.graph, LOCAL, limit=KEPT), clock)


def _line(cache: CalendarCache, clock: _Clock) -> str | None:
    return calendar_line(cache, LOCAL, clock)


def test_the_tick_fills_the_cache_and_a_turn_renders_the_line_with_its_age() -> None:
    """The next two timed items of today and tomorrow; all-day and later days stay out."""
    server, clock = _week(), _Clock()
    cache = _cache(server, clock)
    assert _line(cache, clock) is None  # before the first tick there is no reading
    assert cache.refresh() is True
    clock.now = NOW + timedelta(minutes=4)
    assert _line(cache, clock) == (
        f"{CALENDAR_PREFIX} (read 4 minutes ago): {INTERVIEW}; {DENTIST}."
    )
    # One read, over the rest of today and tomorrow in the local days' own bounds.
    assert server.views() == [("2026-10-12T12:40:00-07:00", "2026-10-14T00:00:00-07:00")]


def test_a_turn_makes_no_call_however_often_it_renders_the_line() -> None:
    """The only call the server sees is the tick's."""
    server, clock = _week(), _Clock()
    cache = _cache(server, clock)
    cache.refresh()
    calls = len(server.calls)
    for minute in range(0, 120, 7):
        clock.now = NOW + timedelta(minutes=minute)
        _line(cache, clock)
    assert len(server.calls) == calls == 1


def test_an_item_that_begins_before_the_next_read_leaves_the_following_ones() -> None:
    """The reading keeps three, so the line still shows two after the first has begun."""
    events = [_timed(f"e{i}", f"Item {i}", _at(12, 13 + i), _at(12, 13 + i, 30)) for i in range(5)]
    server, clock = _Microsoft(events), _Clock()
    cache = _cache(server, clock)
    cache.refresh()
    glance = cache()
    assert glance is not None
    assert [i.title for i in glance.items] == ["Item 0", "Item 1", "Item 2"]
    clock.now = _at(12, 13, 10)  # Item 0 has begun
    assert _line(cache, clock) == (
        f'{CALENDAR_PREFIX} (read 30 minutes ago): 2:00 pm "Item 1"; 3:00 pm "Item 2".'
    )


def _gone(server: _Microsoft) -> None:
    server.fail["get-calendar-view"] = ToolError("microsoft: HTTP 401 expired", code="mcp_server")


def _garbage(server: _Microsoft) -> None:
    server.raw["get-calendar-view"] = "not json"


@pytest.mark.parametrize("breaks", [_gone, _garbage])
def test_a_failed_read_drops_the_reading_and_the_line_is_left_out(
    breaks: Callable[[_Microsoft], None],
) -> None:
    """A good reading, then a read that fails: the old one is not kept as if it were current."""
    server, clock = _week(), _Clock()
    cache = _cache(server, clock)
    assert cache.refresh() is True
    assert _line(cache, clock) is not None
    breaks(server)
    assert cache.refresh() is False
    assert cache() is None
    assert _line(cache, clock) is None


def test_microsoft_not_connected_leaves_the_line_out() -> None:
    """The connection is looked up at each read; none is no line."""
    clock = _Clock()
    cache = _cache(None, clock)
    assert cache.refresh() is False
    assert _line(cache, clock) is None


def test_an_empty_calendar_is_a_reading_but_never_a_line_saying_clear() -> None:
    """A read that found nothing ahead is held, and the block says nothing of the calendar."""
    server, clock = _Microsoft(), _Clock()
    cache = _cache(server, clock)
    assert cache.refresh() is True
    assert cache() is not None
    assert _line(cache, clock) is None


def test_only_timed_events_still_ahead_are_next() -> None:
    """All-day, cancelled and begun events are not next; a missing title is named."""
    server = _Microsoft([
        _all_day("a", "Holiday", NOW.date() + timedelta(days=1), NOW.date() + timedelta(days=1)),
        {**_timed("c", "Cancelled", _at(12, 15), _at(12, 16)), "isCancelled": True},
        _timed("b", "Begun", _at(12, 12), _at(12, 14)),
        _timed("n", "", _at(12, 16), _at(12, 17)),
    ])
    home = Home(_Connections(server), (ZONE, LOCAL), None)  # type: ignore[arg-type]
    assert upcoming_events(home.graph, LOCAL, NOW, 3) == [(_at(12, 16), "(no title)")]


def test_the_loop_reads_at_once_then_every_15_minutes_and_retries_a_failure_in_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure, failure, success, success: sleeps of 120, 120, 900, 900."""
    outcomes = iter([False, False, True, True])
    cache = CalendarCache(lambda _now: [], _Clock())
    reads: list[bool] = []
    slept: list[float] = []

    def refresh() -> bool:
        reads.append(next(outcomes))
        return reads[-1]

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == len(reads) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(cache, "refresh", refresh)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cache.run())
    assert reads == [False, False, True, True]
    assert slept == [block.RETRY_S, block.RETRY_S, block.REFRESH_S, block.REFRESH_S]
    assert (block.RETRY_S, block.REFRESH_S) == (120.0, 900.0)


def test_the_booted_runtime_feeds_the_state_block_from_its_own_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not connected: no line, no claim. Connected: the next read puts the line in the block."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        "runtime:\n  role: brain\nobserver:\n  claude_sessions:\n    enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = bootstrap_runtime_app(runtime_root=root)
    try:
        cache = runtime.calendar_next
        assert cache is not None
        assert runtime.home is not None

        def calendar() -> list[str]:
            return [x for x in _live_lines(runtime.live_context) if x.startswith(CALENDAR_PREFIX)]

        assert cache.refresh() is False  # no Microsoft server in a fresh root
        assert calendar() == []

        soon = datetime.now(LOCAL) + timedelta(minutes=20)
        server = _Microsoft([_timed("e", "Dentist", soon, soon + timedelta(hours=1))])
        monkeypatch.setattr(runtime.home, "_connections", _Connections(server))  # type: ignore[arg-type]
        assert cache.refresh() is True
        (line,) = calendar()
        assert line.startswith(f"{CALENDAR_PREFIX} (read just now): ")
        assert '"Dentist"' in line
    finally:
        runtime.conn.close()

