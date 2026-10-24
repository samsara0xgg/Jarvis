"""The one clock (ADR 0333): what the real one reads, and what the fake one waits for.

``Clock`` is a protocol, and mypy is the proof that ``SystemClock`` and ``FakeClock`` both are one:
``_through`` takes a ``Clock`` and is called with each. The real clock is checked on what it
reads (so a freeze of ``time.time()``, ``time.monotonic()`` and ``datetime.now()`` holds), on
where its zone comes from, and on the boot check of ADR 0322. The host's tz database is checked for
the rule of BC's clocks from 2026-11-01, over zone providers the test writes, and the boot for
logging one error for an old one and going on. The fake clock's ``wait`` is checked with a waiter
on another thread: it ends on the event or on the clock moving, never on real time.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zoneinfo
from datetime import UTC, datetime, timedelta, tzinfo
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
import time_machine
import tzdata

from jarvis.runtime.bootstrap import core as core_module
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.shared import clock as clock_module
from jarvis.shared.clock import (
    Clock,
    SystemClock,
    ZoneError,
    system_zone,
    tz_database_version,
    zone_data_problem,
)
from jarvis.state import daily_report as daily_report_module
from tests.support.clock import FakeClock
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


# --- the host's tz database --------------------------------------------------------------------


def _vancouver_at(offset: timedelta | None) -> Callable[[str], tzinfo]:
    """A zone provider whose every zone is on ``offset`` (None: a table with no offset)."""
    return lambda name: _Table(name, lambda _: offset)


def test_a_database_that_holds_the_rule_has_nothing_to_report() -> None:
    """Vancouver on UTC-7 at noon on 2026-11-02: the day after the old rule turns clocks back."""
    asked: list[str] = []
    noon = datetime(2026, 11, 2, 12, tzinfo=UTC)

    def utc7_at_noon(dt: datetime | None) -> timedelta | None:
        return timedelta(hours=-7) if dt is not None and dt.replace(tzinfo=UTC) == noon else None

    def load(name: str) -> tzinfo:
        asked.append(name)
        return _Table(name, utc7_at_noon)

    assert zone_data_problem(load, lambda: "2026e") is None
    assert asked == ["America/Vancouver"]


def test_an_old_database_says_what_it_gives_and_which_version_it_is() -> None:
    """The one line the log and init.sh print: the zone, the offset it gives, the version."""
    old = _vancouver_at(timedelta(hours=-8))
    assert zone_data_problem(old, lambda: "2025b") == (
        "the tzdata is out of date: America/Vancouver gives UTC-08:00 at 2026-11-02 12:00,"
        " not UTC-07:00 (tz database 2025b)"
    )


def test_a_database_without_the_zone_or_its_offset_gives_no_offset() -> None:
    """A zone that is not there, or has no offset at that time, is as out of date as a wrong one."""

    def missing(name: str) -> tzinfo:
        raise ZoneInfoNotFoundError(name)

    for load in (missing, _vancouver_at(None)):
        problem = zone_data_problem(load, lambda: "unknown")
        assert problem is not None
        assert "America/Vancouver gives no offset at 2026-11-02 12:00" in problem


def _zoneinfo_tree(root: Path, *, header: str | None, zone: bool = True) -> str:
    """A directory shaped like a zoneinfo tree; ``header`` is its ``tzdata.zi`` text, if any."""
    root.mkdir(parents=True)
    if zone:
        (root / "America").mkdir()
        (root / "America" / "Vancouver").write_bytes(b"")
    if header is not None:
        (root / "tzdata.zi").write_text(header, encoding="utf-8")
    return str(root)


def test_the_version_is_the_one_of_the_directory_the_zone_is_read_from(tmp_path: Path) -> None:
    """The first directory of the search path that holds the zone names it, not one before it."""
    without = _zoneinfo_tree(tmp_path / "a", header="# version 2020a\n", zone=False)
    held = _zoneinfo_tree(tmp_path / "b", header="# version 2025b\n# dataform rearguard\n")
    later = _zoneinfo_tree(tmp_path / "c", header="# version 2026e\n")
    assert tz_database_version((without, held, later)) == "2025b"


def test_the_version_may_be_in_a_version_file_and_is_unknown_without_one(tmp_path: Path) -> None:
    """Some systems keep it as ``+VERSION``; a tree with neither names none.

    A search path where no directory holds the zone leaves it to the ``tzdata`` package, as
    ``zoneinfo`` does, so the package's version is the one in force.
    """
    plus = _zoneinfo_tree(tmp_path / "plus", header=None)
    (tmp_path / "plus" / "+VERSION").write_text("2024a\n", encoding="utf-8")
    bare = _zoneinfo_tree(tmp_path / "bare", header=None)
    assert tz_database_version((plus,)) == "2024a"
    assert tz_database_version((bare,)) == "unknown"
    assert tz_database_version((str(tmp_path / "nowhere"),)) == tzdata.IANA_VERSION


def test_every_zone_is_read_from_the_pinned_package_not_the_host() -> None:
    """ADR 0398: importing jarvis empties the search path, so no host directory is read.

    The zones and the version init.sh and the boot report are the ``tzdata`` package's, which
    holds the rule for BC's clocks.
    """
    assert zoneinfo.TZPATH == ()
    assert tz_database_version() == tzdata.IANA_VERSION
    assert zone_data_problem() is None
    winter = datetime(2026, 11, 12, 12, tzinfo=ZoneInfo("America/Winnipeg"))
    assert winter.utcoffset() == timedelta(hours=-5)


PACKAGE_ZONES = Path(str(files("tzdata.zoneinfo")))

_LOCAL = """
import os, time, jarvis
from datetime import datetime
from jarvis.shared.clock import system_zone
noon = datetime(2026, 11, 12, 20, 0).timestamp()
print(os.environ["TZ"], time.localtime(noon).tm_gmtoff, system_zone())
"""


def _local_time_in(host: Path) -> list[str]:
    """What a fresh ``import jarvis`` leaves: ``TZ``, his offset at 2026-11-12 12:00, his zone.

    The machine is on America/Vancouver and glibc reads its zone files from ``host`` (``TZDIR``).
    """
    env = {**os.environ, "TZ": "America/Vancouver", "TZDIR": str(host)}
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script.
        [sys.executable, "-c", _LOCAL], capture_output=True, text=True, check=True, env=env,
    )
    return done.stdout.split()


@pytest.mark.skipif(sys.platform != "linux", reason="glibc reads TZDIR; macOS does not")
def test_a_host_database_without_the_rule_points_local_time_at_the_package(
    tmp_path: Path,
) -> None:
    """ADR 0398: an old host gives UTC-8 in BC after 2026-11-01; local time reads the package.

    The old host is US Pacific's rule under Vancouver's name, which is what a database older
    than 2026e holds for it.
    """
    (tmp_path / "America").mkdir()
    shutil.copyfile(PACKAGE_ZONES / "PST8PDT", tmp_path / "America" / "Vancouver")

    pinned, offset, zone = _local_time_in(tmp_path)

    assert pinned == f":{PACKAGE_ZONES / 'America' / 'Vancouver'}"
    assert int(offset) == -7 * 3600
    assert zone == "America/Vancouver"


@pytest.mark.skipif(sys.platform != "linux", reason="glibc reads TZDIR; macOS does not")
def test_a_host_database_that_agrees_with_the_package_is_left_as_it_is(tmp_path: Path) -> None:
    """``TZ`` keeps the name the machine gave it."""
    (tmp_path / "America").mkdir()
    shutil.copyfile(PACKAGE_ZONES / "America" / "Vancouver", tmp_path / "America" / "Vancouver")

    assert _local_time_in(tmp_path) == ["America/Vancouver", str(-7 * 3600), "America/Vancouver"]


def test_a_tz_that_is_the_path_of_a_zone_file_names_that_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The form :func:`pin_local_time` leaves in ``TZ`` still names his zone."""
    monkeypatch.setenv("TZ", f":{PACKAGE_ZONES / 'Asia' / 'Tokyo'}")
    assert getattr(system_zone(), "key", None) == "Asia/Tokyo"


def test_boot_logs_one_error_for_an_old_database_and_goes_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It names the zone and the version; it does not refuse a boot that has passed its check."""
    sentence = "the host's tzdata is out of date: America/Vancouver gives UTC-08:00 (2025b)"
    monkeypatch.setattr(core_module, "zone_data_problem", lambda: sentence)
    with caplog.at_level(logging.ERROR):
        clock = _boot(tmp_path, monkeypatch, "language: en\n")
    assert isinstance(clock, SystemClock)
    assert [r.getMessage() for r in caplog.records if r.name == core_module.__name__] == [
        f"runtime: {sentence}",
    ]


def test_boot_logs_nothing_for_a_database_that_holds_the_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A current database is not mentioned."""
    monkeypatch.setattr(core_module, "zone_data_problem", lambda: None)
    with caplog.at_level(logging.ERROR):
        _boot(tmp_path, monkeypatch, "language: en\n")
    assert [r for r in caplog.records if r.name == core_module.__name__] == []


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
    runtime = boot_runtime(root)
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
    # The config resolves the owner's zone once, through the report's resolver (ADR 0370).
    monkeypatch.setattr(
        daily_report_module,
        "system_zone",
        lambda: _Table("America/Vancouver", lambda _: timedelta(hours=-8)),
    )
    with pytest.raises(RuntimeBootstrapError, match=r"America/Vancouver.*work_state\.timezone"):
        _boot(tmp_path, monkeypatch, "language: en\n")
