"""The one clock (ADR 0333): what the real one reads, and what the fake one waits for.

``Clock`` is a protocol, and mypy is the proof that ``SystemClock`` and ``FakeClock`` both are one:
``_through`` takes a ``Clock`` and is called with each. The real clock is checked on what it
reads (so a freeze of ``time.time()``, ``time.monotonic()`` and ``datetime.now()`` holds), on
where its zone comes from, and on the boot check of ADR 0322. The fake clock's ``wait`` is checked
with a waiter on another thread: it ends on the event or on the clock moving, never on real time.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import time_machine

from jarvis.runtime import RuntimeBootstrapError, bootstrap_runtime_app
from jarvis.shared import clock as clock_module
from jarvis.shared.clock import Clock, SystemClock, ZoneError, system_zone
from tests.support.clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

PARIS = ZoneInfo("Europe/Paris")
VANCOUVER = ZoneInfo("America/Vancouver")
MOMENT = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _through(clock: Clock) -> tuple[datetime, float, float, bool]:
    """Everything a service may ask of a clock; the call is what mypy checks."""
    clock.sleep(0.0)
    return clock.now(), clock.time(), clock.monotonic(), clock.wait(threading.Event(), 0.0)


def test_both_clocks_are_the_protocol() -> None:
    """The real and the fake clock answer the same five questions with the same shapes."""
    for clock in (SystemClock(), FakeClock()):
        now, epoch, mono, was_set = _through(clock)
        assert now.tzinfo is not None
        assert abs(now.timestamp() - epoch) < 3600
        assert mono > 0
        assert was_set is False


# --- the real clock ----------------------------------------------------------------------------


@pytest.fixture
def machine_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Set the zone the machine's own clock runs in, as ``TZ`` does for the C library."""

    def point(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield point
    monkeypatch.undo()
    time.tzset()


def test_it_reads_through_the_time_module_and_datetime_now() -> None:
    """A freeze of those readers is a freeze of the clock: that is how the tool exam stops time."""
    clock = SystemClock(PARIS)
    with time_machine.travel(MOMENT, tick=False):
        assert clock.time() == MOMENT.timestamp()
        assert clock.now() == MOMENT
        assert clock.now().tzinfo is PARIS
        assert clock.now().utcoffset() == timedelta(hours=2)  # Paris is on summer time


def test_monotonic_and_sleep_go_through_their_time_module_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing lower is read: replacing the module's functions replaces what the clock sees."""
    slept: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: 123.5)
    monkeypatch.setattr(time, "sleep", slept.append)
    clock = SystemClock()
    assert clock.monotonic() == 123.5
    clock.sleep(2.5)
    clock.sleep(-1.0)
    assert slept == [2.5, 0.0]


def test_wait_returns_whether_the_event_was_set() -> None:
    """A set event ends it at once, true; an unset one with no time left is false."""
    clock = SystemClock()
    event = threading.Event()
    assert clock.wait(event, 0.0) is False
    event.set()
    assert clock.wait(event, 60.0) is True
    assert clock.wait(event, None) is True


def test_his_zone_is_the_setting_when_there_is_one() -> None:
    """``work_state.timezone``, parsed, is the zone; the machine's does not matter then."""
    assert SystemClock(PARIS).zone is PARIS


@pytest.mark.parametrize("name", ["Asia/Tokyo", ":America/Vancouver", "UTC"])
def test_without_a_setting_his_zone_is_the_machines(
    machine_zone: Callable[[str], None],
    name: str,
) -> None:
    """The clock follows ``TZ`` as the C library's local time does, so the two cannot differ."""
    machine_zone(name)
    clock = SystemClock()
    assert clock.now().utcoffset() == datetime.now().astimezone().utcoffset()
    assert getattr(clock.zone, "key", None) == name.removeprefix(":")


def test_an_empty_tz_is_utc(machine_zone: Callable[[str], None]) -> None:
    """The C library reads ``TZ=`` as UTC, and so does the clock."""
    machine_zone("")
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_without_tz_his_zone_is_what_the_localtime_file_points_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The way a Mac or a Linux box without ``TZ`` names its zone: a link into a zoneinfo tree."""
    target = tmp_path / "var" / "db" / "zoneinfo" / "Asia" / "Tokyo"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(target)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(clock_module, "_LOCALTIME", link)
    assert getattr(system_zone(), "key", None) == "Asia/Tokyo"


def test_without_tz_or_a_localtime_file_his_zone_is_utc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nothing names a zone: UTC, not a guess."""
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(clock_module, "_LOCALTIME", tmp_path / "nothing-here")
    assert system_zone() is UTC


@pytest.mark.parametrize(
    "name",
    ["UTC", "America/Vancouver", "Asia/Tokyo", "Australia/Lord_Howe", "Europe/Dublin"],
)
def test_the_boot_check_passes_where_the_database_and_the_machine_agree(
    machine_zone: Callable[[str], None],
    name: str,
) -> None:
    """Lord Howe moves by half an hour and Dublin is on summer time in winter: still agreed."""
    machine_zone(name)
    SystemClock().check_rule()
    SystemClock(ZoneInfo(name)).check_rule()


def test_the_boot_check_needs_only_an_offset_for_a_zone_that_is_not_the_machines(
    machine_zone: Callable[[str], None],
) -> None:
    """There is no second witness for another zone; the database must still know it."""
    machine_zone("UTC")
    SystemClock(ZoneInfo("Pacific/Auckland")).check_rule()


class _Table(tzinfo):
    """A zone table written by the test: ``offset_of`` says what the table holds."""

    def __init__(self, key: str, offset_of: Callable[[datetime | None], timedelta | None]) -> None:
        self.key = key
        self._offset_of = offset_of

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return self._offset_of(dt)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None if self._offset_of(dt) is None else timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        return None if self._offset_of(dt) is None else self.key


def test_the_boot_check_stops_a_database_that_lacks_the_rule_in_force(
    machine_zone: Callable[[str], None],
) -> None:
    """Vancouver is on summer time half of the year; a table with no summer time is wrong then."""
    machine_zone("America/Vancouver")
    lost_its_rule = _Table("America/Vancouver", lambda _: timedelta(hours=-8))
    with pytest.raises(ZoneError, match="America/Vancouver"):
        SystemClock(lost_its_rule).check_rule()


def test_the_boot_check_stops_a_zone_with_no_offset() -> None:
    """Nothing to cut a day by."""
    with pytest.raises(ZoneError, match="no offset"):
        SystemClock(_Table("Nowhere/Land", lambda _: None)).check_rule()


# --- the fake clock's wait ---------------------------------------------------------------------


class _Waiter:
    """A thread blocked in ``clock.wait``; it is parked on the clock before the test goes on."""

    def __init__(self, clock: FakeClock, event: threading.Event, timeout: float | None) -> None:
        self._got: list[bool] = []
        self._thread = threading.Thread(
            target=lambda: self._got.append(clock.wait(event, timeout)),
            daemon=True,
        )
        self._thread.start()
        clock.wait_until_parked()

    def result(self) -> bool:
        """What the wait returned; a wait that does not end fails the test."""
        self._thread.join(timeout=5.0)
        assert not self._thread.is_alive(), "the wait did not end"
        return self._got[0]


def test_a_wait_on_a_set_event_ends_at_once_and_moves_nothing() -> None:
    """True without the clock moving, however long the timeout."""
    clock = FakeClock()
    event = threading.Event()
    event.set()
    before = clock.monotonic()
    assert clock.wait(event, 3600.0) is True
    assert clock.wait(event, None) is True
    assert clock.monotonic() == before


def test_a_wait_with_no_time_left_returns_the_event_as_it_is() -> None:
    """Zero or less is a look, as ``Event.wait(0)`` is."""
    clock = FakeClock()
    event = threading.Event()
    assert clock.wait(event, 0.0) is False
    assert clock.wait(event, -3.0) is False
    event.set()
    assert clock.wait(event, 0.0) is True


def test_a_thread_waiting_on_the_clock_wakes_when_the_clock_passes_its_deadline() -> None:
    """Ten seconds of waiting end on ``advance(10)``, not on ten seconds going by."""
    clock = FakeClock()
    waiter = _Waiter(clock, threading.Event(), 10.0)
    assert clock.blocked == 1
    clock.advance(10.0)
    assert clock.blocked == 0  # due at once: the thread has only still to notice
    assert waiter.result() is False
    assert clock.parked == 0


def test_a_wait_does_not_end_before_its_deadline() -> None:
    """Short of the deadline it stays blocked, and the event then ends it as true."""
    clock = FakeClock()
    event = threading.Event()
    waiter = _Waiter(clock, event, 10.0)
    clock.advance(9.9)
    assert clock.blocked == 1
    event.set()
    assert waiter.result() is True


def test_a_thread_waiting_on_the_clock_wakes_when_its_event_is_set() -> None:
    """True as soon as the event is set, with the clock where it was."""
    clock = FakeClock()
    event = threading.Event()
    before = clock.monotonic()
    waiter = _Waiter(clock, event, 600.0)
    event.set()
    assert waiter.result() is True
    assert clock.monotonic() == before


def test_a_wait_with_no_timeout_ends_only_on_its_event() -> None:
    """A day goes by on the clock and it is still blocked."""
    clock = FakeClock()
    event = threading.Event()
    waiter = _Waiter(clock, event, None)
    clock.advance(86_400.0)
    assert clock.blocked == 1
    event.set()
    assert waiter.result() is True


def test_another_threads_sleep_moves_the_clock_and_wakes_the_waiter() -> None:
    """``sleep`` moves the clock for everyone: a waiter whose deadline it passes is released."""
    clock = FakeClock()
    waiter = _Waiter(clock, threading.Event(), 5.0)
    other = threading.Thread(target=clock.sleep, args=(5.0,))
    other.start()
    other.join(timeout=5.0)
    assert waiter.result() is False


def test_pass_deadline_of_runs_the_code_elsewhere_and_moves_the_clock_once_it_waits() -> None:
    """The test thread is free to move the clock while the code under test is parked on it."""
    clock = FakeClock()
    began = clock.monotonic()
    event = threading.Event()
    assert clock.pass_deadline_of(2.0, lambda: clock.wait(event, 2.0)) is False
    assert clock.monotonic() - began == 2.0


def test_each_of_several_waiters_ends_at_its_own_deadline() -> None:
    """Two waiters, 5 s and 20 s: the first goes at 5 s and the second stays until 20 s."""
    clock = FakeClock()
    event = threading.Event()
    soon = _Waiter(clock, event, 5.0)
    late = _Waiter(clock, event, 20.0)
    clock.advance(5.0)
    assert soon.result() is False
    assert clock.blocked == 1
    clock.advance(15.0)
    assert late.result() is False


# --- the runtime's clock -----------------------------------------------------------------------


def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: str) -> Clock:
    """Boot the real runtime on a settings file; return the clock it holds."""
    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = bootstrap_runtime_app(runtime_root=root)
    runtime.conn.close()
    return runtime.clock


def test_the_runtime_holds_one_clock_in_the_zone_work_state_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one setting is ``work_state.timezone``."""
    clock = _boot(tmp_path, monkeypatch, "work_state:\n  timezone: Pacific/Auckland\n")
    assert isinstance(clock, SystemClock)
    assert getattr(clock.zone, "key", None) == "Pacific/Auckland"


def test_the_runtime_clock_follows_the_machine_when_no_zone_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine_zone: Callable[[str], None],
) -> None:
    """The default is the system zone."""
    machine_zone("Asia/Tokyo")
    clock = _boot(tmp_path, monkeypatch, "language: en\n")
    assert getattr(clock.zone, "key", None) == "Asia/Tokyo"


def test_boot_stops_when_the_zone_database_lacks_the_rule_in_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine_zone: Callable[[str], None],
) -> None:
    """The error names the zone and the setting, instead of a daemon that cuts days wrongly."""
    machine_zone("America/Vancouver")
    monkeypatch.setattr(
        clock_module,
        "system_zone",
        lambda: _Table("America/Vancouver", lambda _: timedelta(hours=-8)),
    )
    with pytest.raises(RuntimeBootstrapError, match=r"America/Vancouver.*work_state\.timezone"):
        _boot(tmp_path, monkeypatch, "language: en\n")
