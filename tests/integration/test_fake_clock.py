"""FakeClock: aware wall time in a named zone, monotonic time, and a sleep that never waits.

The acceptance is the clock itself, on the properties a test relies on when it stands in for
``time``: the zone and the fold, one sleep moving both clocks, a jump moving only the wall, and
the answer hold (ADR 0272) released by ``clock.sleep(1.5)`` where the real one waits 1.5 s.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.runtime import answer_hold
from jarvis.shared import Event
from tests.support.clock import DEFAULT_START, FakeClock

VANCOUVER = ZoneInfo("America/Vancouver")
TOKYO = ZoneInfo("Asia/Tokyo")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")  # Vancouver is moving to permanent summer time


def test_it_starts_on_a_wednesday_morning_in_the_owners_zone() -> None:
    """The default is aware, in Vancouver, and its epoch time is that instant's."""
    clock = FakeClock()
    assert clock.now() == DEFAULT_START
    assert clock.now().tzinfo is VANCOUVER
    assert clock.now().strftime("%A %H:%M") == "Wednesday 09:00"
    assert clock.time() == DEFAULT_START.timestamp()
    assert clock.time_ms() == round(DEFAULT_START.timestamp() * 1000)
    assert clock.utc_now() == DEFAULT_START.astimezone(UTC)
    assert clock.utc_now().tzinfo is UTC


def test_a_naive_start_is_read_in_the_clocks_zone_and_an_aware_one_is_kept() -> None:
    """The same wall reading is a different instant in another zone; an aware start is exact."""
    naive = datetime(2026, 3, 2, 8, 30)  # noqa: DTZ001 - naive on purpose: read in the zone
    assert FakeClock(naive, zone=TOKYO).now() == datetime(2026, 3, 2, 8, 30, tzinfo=TOKYO)
    assert FakeClock(naive).now() == datetime(2026, 3, 2, 8, 30, tzinfo=VANCOUVER)
    assert FakeClock(naive, zone=TOKYO).time() != FakeClock(naive).time()
    aware = datetime(2026, 3, 2, 8, 30, tzinfo=TOKYO)
    clock = FakeClock(aware)
    assert clock.now() == aware  # the same instant, shown in the clock's zone
    assert clock.now().utcoffset() == timedelta(hours=-8)
    assert clock.now(TOKYO).utcoffset() == timedelta(hours=9)


def test_sleep_moves_both_clocks_by_the_same_seconds_and_returns_at_once() -> None:
    """An hour of sleep costs no time and is an hour on the wall and on the monotonic clock."""
    clock = FakeClock()
    wall, mono = clock.time(), clock.monotonic()
    started = time.perf_counter()
    clock.sleep(3600)
    assert time.perf_counter() - started < 0.5
    assert clock.time() - wall == 3600
    assert clock.monotonic() - mono == 3600
    assert clock.now() == DEFAULT_START + timedelta(hours=1)


def test_a_sleep_of_nothing_or_less_does_not_run_the_clock_back() -> None:
    """``time.sleep`` rejects a negative number; the stand-in only never goes backwards."""
    clock = FakeClock()
    wall, mono = clock.time(), clock.monotonic()
    clock.sleep(0)
    clock.sleep(-5)
    assert (clock.time(), clock.monotonic()) == (wall, mono)


def test_an_hour_over_the_fall_back_is_an_hour_elapsed_and_two_on_the_wall() -> None:
    """2026-11-01 01:30 happens twice in Los Angeles; the second is an hour after the first."""
    clock = FakeClock(datetime(2026, 11, 1, 1, 30, tzinfo=LOS_ANGELES), zone=LOS_ANGELES)
    first = clock.now()
    assert first.utcoffset() == timedelta(hours=-7)  # PDT
    mono = clock.monotonic()
    clock.advance(3600)
    second = clock.now()
    assert (second.hour, second.minute) == (1, 30)  # the same wall reading
    assert second.utcoffset() == timedelta(hours=-8)  # PST
    assert second.timestamp() - first.timestamp() == 3600
    assert clock.monotonic() - mono == 3600


def test_an_hour_over_the_spring_forward_skips_the_wall_hour() -> None:
    """2026-03-08 01:30 PST plus an hour is 03:30 PDT: 02:30 never exists."""
    clock = FakeClock(datetime(2026, 3, 8, 1, 30, tzinfo=LOS_ANGELES), zone=LOS_ANGELES)
    clock.advance(3600)
    assert clock.now().strftime("%H:%M") == "03:30"
    assert clock.now().utcoffset() == timedelta(hours=-7)


def test_a_jump_moves_the_wall_clock_alone() -> None:
    """A correction of the wall (or a zone change) leaves elapsed time as it was."""
    clock = FakeClock()
    mono = clock.monotonic()
    clock.set(datetime(2027, 1, 1, 0, 0))  # noqa: DTZ001 - naive on purpose: read in the zone
    assert clock.now() == datetime(2027, 1, 1, 0, 0, tzinfo=VANCOUVER)
    assert clock.monotonic() == mono
    clock.set(datetime(2026, 12, 31, 16, 0, tzinfo=UTC))
    assert clock.utc_now() == datetime(2026, 12, 31, 16, 0, tzinfo=UTC)
    assert clock.now().tzinfo is VANCOUVER


def test_async_sleep_stands_in_for_asyncio_sleep() -> None:
    """Awaiting it moves the clocks and does not wait."""
    clock = FakeClock()
    mono = clock.monotonic()
    started = time.perf_counter()
    asyncio.run(clock.async_sleep(900))
    assert time.perf_counter() - started < 0.5
    assert clock.monotonic() - mono == 900


def test_it_stands_in_for_the_time_module_of_the_answer_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold ends when the fake clock has slept through it; the test waits for nothing."""
    clock = FakeClock()
    monkeypatch.setattr(answer_hold, "time", clock)
    words = Event(
        event_uid="e1",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=clock.time_ms(),
        payload={
            "transcript": "我想问一下去学校的公交",
            "turn_id": "T",
            "channel": "inherent_wake",
        },
        source_event_id=None,
        correlation=None,
    )
    until = answer_hold.output_not_before({}, words)
    assert until == pytest.approx(clock.monotonic() + answer_hold.DEFAULT_HOLD_S)
    clock.sleep(answer_hold.DEFAULT_HOLD_S - 0.5)
    assert answer_hold.output_not_before({}, words) is not None
    clock.sleep(0.5)
    assert answer_hold.output_not_before({}, words) is None
