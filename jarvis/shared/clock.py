"""The one clock: wall time in his zone, monotonic time, sleeping and waiting (ADR 0322, 0333).

The runtime builds one :class:`SystemClock` and every service that needs the time of day, an
elapsed time or a wait takes it from there. A test hands the same code
``tests.support.clock.FakeClock``, which moves only when the test says so.

:class:`SystemClock` reads through ``time.time()``, ``time.monotonic()`` and ``datetime.now()``
and nothing lower, so a freeze that replaces those (the tool exam's ``time_machine``) holds for
every reader that is already on the clock.

:func:`zone_data_problem` checks the host's tz database for one rule an old one lacks.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterable

_LOCALTIME: Final = Path("/etc/localtime")
_HALF_YEAR_S: Final = 182 * 24 * 3600
"""Far enough from now to land on the other side of a daylight-saving change."""
_RULE_ZONE: Final = "America/Vancouver"
_RULE_AT: Final = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
_RULE_OFFSET: Final = timedelta(hours=-7)
"""The rule the host's tz database must hold: ``_RULE_ZONE`` is on UTC-7 at 12:00 on 2026-11-02.

``_RULE_AT`` is that wall time; the zone replaces its UTC label. A database older than the rule
gives UTC-8 there, and every time of day in BC after it is an hour off.
"""


class ZoneError(ValueError):
    """The zone database does not know his zone's current rule."""


class Clock(Protocol):
    """What a service may ask of time: the date and hour, how long, a sleep and a wait."""

    @property
    def zone(self) -> tzinfo:
        """His zone: where "today" is cut and where a source's times are converted from."""
        ...

    def now(self) -> datetime:
        """The wall time, aware, in :attr:`zone`."""
        ...

    def time(self) -> float:
        """Epoch seconds, as ``time.time()``."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never steps back, as ``time.monotonic()``."""
        ...

    def sleep(self, seconds: float) -> None:
        """Let ``seconds`` pass on this clock."""
        ...

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        """Wait up to ``timeout`` seconds of this clock for ``event``; whether it was set."""
        ...


class SystemClock:
    """The real time, in his zone."""

    def __init__(self, zone: tzinfo | None = None) -> None:
        """Take ``zone`` (``work_state.timezone``, parsed), else the machine's own."""
        self.zone: tzinfo = system_zone() if zone is None else zone

    def now(self) -> datetime:
        """The wall time, aware, in his zone."""
        return datetime.now(self.zone)

    def time(self) -> float:
        """Epoch seconds."""
        return time.time()

    def monotonic(self) -> float:
        """Monotonic seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Block this thread for ``seconds``."""
        time.sleep(max(seconds, 0.0))

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        """Block until ``event`` is set or ``timeout`` seconds pass; whether it was set."""
        return event.wait(None if timeout is None else max(timeout, 0.0))

    def check_rule(self) -> None:
        """ADR 0322's boot check: the zone database has a rule for now and the half year after.

        When his zone is the machine's own, the machine's clock is the witness: the database
        and the C library must give the same offset at both moments, or one of them lacks the
        rule in force. Another zone has no second witness here; it must still give an offset.
        """
        started = self.time()
        key = zone_key(self.zone)
        witness = key is not None and key == zone_key(system_zone())
        for moment in (started, started + _HALF_YEAR_S):
            try:
                offset = datetime.fromtimestamp(moment, self.zone).utcoffset()
            except (ValueError, OverflowError, OSError):  # a zone with no offset fails in fromutc
                offset = None
            if offset is None:
                msg = f"the zone database gives no offset for {_name(self.zone)}"
                raise ZoneError(msg)
            machine = timedelta(seconds=time.localtime(moment).tm_gmtoff or 0)
            if witness and offset != machine:
                msg = (
                    f"the zone database and this machine disagree on {_name(self.zone)} "
                    f"(UTC{_signed(offset)} against UTC{_signed(machine)} at "
                    f"{datetime.fromtimestamp(moment, UTC):%Y-%m-%d}); refresh the machine's "
                    "zone data or name his zone in work_state.timezone"
                )
                raise ZoneError(msg)


def system_zone() -> tzinfo:
    """The machine's zone: ``TZ`` if it names one, else what ``/etc/localtime`` points at, else UTC.

    ``TZ`` comes first because the C library's local time (``datetime.now()`` with no zone)
    follows it, and the clock must agree with those readers.
    """
    named = os.environ.get("TZ")
    if named is not None:
        name = named.removeprefix(":").strip()
        if not name:
            return UTC
        zone = _load(name)
        if zone is not None:
            return zone
    try:
        parts = _LOCALTIME.resolve().parts
        zone = _load("/".join(parts[parts.index("zoneinfo") + 1 :]))
    except (OSError, ValueError):
        zone = None
    return UTC if zone is None else zone


def _load(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def tz_database_version(directories: Iterable[str] = TZPATH) -> str:
    """The version of the tz database the zone files are read from, else ``unknown``.

    The first directory that holds a zone file names it: ``tzdata.zi`` begins ``# version 2026e``,
    and some systems keep it in ``+VERSION``.
    """
    for directory in directories:
        root = Path(directory)
        if not (root / _RULE_ZONE).is_file():
            continue
        for name in ("tzdata.zi", "+VERSION"):
            try:
                first = (root / name).read_text(encoding="utf-8").splitlines()[0]
            except (OSError, UnicodeDecodeError, IndexError):
                continue
            version = first.removeprefix("#").strip().removeprefix("version").strip()
            if version:
                return version
        return "unknown"
    return "unknown"


def zone_data_problem(
    load: Callable[[str], tzinfo] = ZoneInfo,
    version: Callable[[], str] = tz_database_version,
) -> str | None:
    """None when the host's tz database gives America/Vancouver UTC-7 at 2026-11-02 12:00.

    Otherwise the sentence that says its tzdata is out of date, naming what it gives there (no
    offset for a zone it lacks) and its version. ``load`` is the zone provider and ``version``
    the database's version.
    """
    try:
        offset = _RULE_AT.replace(tzinfo=load(_RULE_ZONE)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError):
        offset = None
    if offset == _RULE_OFFSET:
        return None
    gives = "no offset" if offset is None else f"UTC{_signed(offset)}"
    return (
        f"the host's tzdata is out of date: {_RULE_ZONE} gives {gives} at "
        f"{_RULE_AT:%Y-%m-%d %H:%M}, not UTC{_signed(_RULE_OFFSET)} (tz database {version()})"
    )


def zone_key(zone: tzinfo) -> str | None:
    """The IANA name of ``zone``, or ``None`` for a zone that is only an offset."""
    key = getattr(zone, "key", None)
    return "UTC" if zone is UTC else key if isinstance(key, str) else None


def _name(zone: tzinfo) -> str:
    return zone_key(zone) or str(zone)


def _signed(offset: timedelta) -> str:
    minutes = round(offset.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
