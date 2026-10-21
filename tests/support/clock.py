"""A clock the test owns: wall time in a named zone, monotonic time, and a sleep that moves both.

Code that takes its time from an injected clock, or whose ``time.time``, ``time.monotonic`` and
``time.sleep`` a test replaces with this clock's methods, runs against this one. Nothing here
waits: ``sleep`` and ``advance`` move the clock and return, so a test that needs a hold to pass
says ``clock.advance(1.5)`` and carries on.

Wall time and monotonic time move together under ``advance`` and ``sleep``; ``set`` moves the
wall clock alone, as a clock correction would. Elapsed time is absolute, so an hour added across
a daylight-saving change is an hour on the monotonic clock and two on the zone's wall clock.

``wait(event, timeout)`` is the protocol's wait on this clock: it returns once the event is set or
the clock has been advanced past the deadline, and a test that needs the waiter to be blocked
first says ``clock.wait_until_parked()``. The deadline is only ever reached by ``advance`` or
``sleep``; real time passing never ends a wait. ``blocked`` counts the waits that are still
blocked, which is how a test knows the code under it has gone as far as it can without the clock
moving.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_ZONE = ZoneInfo("America/Vancouver")
"""The owner's zone, which most local-day behaviour is specified in."""
DEFAULT_START = datetime(2026, 10, 14, 9, 0, tzinfo=DEFAULT_ZONE)
"""A Wednesday morning, far from midnight and from a daylight-saving change."""


_POLL_S = 0.001
"""How often a blocked wait looks at its event again. An event has no way to call the waiter, so
this is the longest a set event goes unseen; it never decides when a deadline passes."""
_GUARD_S = 5.0
"""How long a test waits for something to happen before it fails; not a time the clock keeps."""


@dataclass(eq=False)
class _Wait:
    """One thread's wait: its event and the monotonic second it ends at (``None``: never)."""

    event: threading.Event
    deadline: float | None


class FakeClock:
    """One test's time: aware wall time in ``zone``, monotonic seconds, ``sleep`` and ``wait``.

    It implements :class:`jarvis.shared.clock.Clock`; ``tests/integration/test_clock.py`` makes
    mypy prove it.
    """

    def __init__(
        self,
        start: datetime | None = None,
        *,
        zone: ZoneInfo = DEFAULT_ZONE,
        monotonic_s: float = 1_000.0,
    ) -> None:
        """Start at ``start`` (aware, or naive: read in ``zone``); monotonic at ``monotonic_s``."""
        self.zone = zone
        self._cond = threading.Condition()
        self._waits: list[_Wait] = []
        self._utc = self._as_utc(DEFAULT_START if start is None else start)
        self._monotonic = monotonic_s

    def _as_utc(self, moment: datetime) -> datetime:
        return (moment.replace(tzinfo=self.zone) if moment.tzinfo is None else moment).astimezone(
            UTC
        )

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        """Aware wall time, in the clock's zone unless ``tz`` names another."""
        with self._cond:
            return self._utc.astimezone(tz or self.zone)

    def utc_now(self) -> datetime:
        """Aware wall time in UTC."""
        with self._cond:
            return self._utc

    def time(self) -> float:
        """Epoch seconds, as ``time.time()``."""
        with self._cond:
            return self._utc.timestamp()

    def time_ms(self) -> int:
        """Epoch milliseconds, the event log's own unit."""
        with self._cond:
            return round(self._utc.timestamp() * 1000)

    def monotonic(self) -> float:
        """Monotonic seconds, as ``time.monotonic()``."""
        with self._cond:
            return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move wall time and monotonic time forward together; a wait that is due wakes."""
        with self._cond:
            self._utc += timedelta(seconds=seconds)
            self._monotonic += seconds
            self._cond.notify_all()

    def sleep(self, seconds: float) -> None:
        """Stand in for ``time.sleep``: nothing waits, both clocks move on."""
        self.advance(max(seconds, 0.0))

    def wait(self, event: threading.Event, timeout: float | None) -> bool:
        """Stand in for ``event.wait(timeout)``; whether the event is set.

        It ends when the event is set or the clock has been advanced ``timeout`` seconds;
        ``None`` waits for the event alone.
        """
        with self._cond:
            this = _Wait(event, None if timeout is None else self._monotonic + max(timeout, 0.0))
            self._waits.append(this)
            self._cond.notify_all()
            try:
                while not event.is_set() and not self._due(this):
                    self._cond.wait(_POLL_S)
                return event.is_set()
            finally:
                self._waits.remove(this)
                self._cond.notify_all()

    def _due(self, wait: _Wait) -> bool:
        return wait.deadline is not None and self._monotonic >= wait.deadline

    @property
    def parked(self) -> int:
        """How many threads are inside :meth:`wait` now, ended or not."""
        with self._cond:
            return len(self._waits)

    @property
    def blocked(self) -> int:
        """How many of them can still go on only if the clock moves or their event is set."""
        with self._cond:
            return sum(1 for w in self._waits if not w.event.is_set() and not self._due(w))

    def wait_until_parked(self, count: int = 1) -> None:
        """Block the test until ``count`` threads are inside :meth:`wait`, or fail it."""
        self.wait_until(lambda: self.parked >= count, f"{count} threads inside the clock's wait")

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        """Block the test until ``condition`` holds, or fail it saying ``what`` never did.

        It is checked each time a wait starts or ends, the clock moves, or :meth:`poke` is
        called; the condition may read this clock.
        """
        with self._cond:
            if not self._cond.wait_for(condition, _GUARD_S):
                msg = f"gave up waiting for {what}"
                raise AssertionError(msg)

    def poke(self) -> None:
        """Tell :meth:`wait_until` that something it may be watching has changed."""
        with self._cond:
            self._cond.notify_all()

    async def async_sleep(self, seconds: float) -> None:
        """Stand in for ``asyncio.sleep``: the same, for code that awaits."""
        self.sleep(seconds)

    def set(self, moment: datetime) -> None:
        """Jump the wall clock to ``moment`` (aware, or naive: in the zone); monotonic stays."""
        with self._cond:
            self._utc = self._as_utc(moment)
            self._cond.notify_all()
