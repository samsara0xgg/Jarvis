"""A clock the test owns: wall time in a named zone, monotonic time, and a sleep that moves both.

Code that takes its time from an injected clock, or whose ``time.time``, ``time.monotonic`` and
``time.sleep`` a test replaces with this clock's methods, runs against this one. Nothing here
waits: ``sleep`` and ``advance`` move the clock and return, so a test that needs a hold to pass
says ``clock.advance(1.5)`` and carries on.

Wall time and monotonic time move together under ``advance`` and ``sleep``; ``set`` moves the
wall clock alone, as a clock correction would. Elapsed time is absolute, so an hour added across
a daylight-saving change is an hour on the monotonic clock and two on the zone's wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_ZONE = ZoneInfo("America/Vancouver")
"""The owner's zone, which most local-day behaviour is specified in."""
DEFAULT_START = datetime(2026, 10, 14, 9, 0, tzinfo=DEFAULT_ZONE)
"""A Wednesday morning, far from midnight and from a daylight-saving change."""


class FakeClock:
    """One test's time: aware wall time in ``zone``, monotonic seconds, and ``sleep``."""

    def __init__(
        self,
        start: datetime | None = None,
        *,
        zone: ZoneInfo = DEFAULT_ZONE,
        monotonic_s: float = 1_000.0,
    ) -> None:
        """Start at ``start`` (aware, or naive: read in ``zone``); monotonic at ``monotonic_s``."""
        self.zone = zone
        self._utc = self._as_utc(DEFAULT_START if start is None else start)
        self._monotonic = monotonic_s

    def _as_utc(self, moment: datetime) -> datetime:
        return (moment.replace(tzinfo=self.zone) if moment.tzinfo is None else moment).astimezone(
            UTC
        )

    def now(self, tz: ZoneInfo | None = None) -> datetime:
        """Aware wall time, in the clock's zone unless ``tz`` names another."""
        return self._utc.astimezone(tz or self.zone)

    def utc_now(self) -> datetime:
        """Aware wall time in UTC."""
        return self._utc

    def time(self) -> float:
        """Epoch seconds, as ``time.time()``."""
        return self._utc.timestamp()

    def time_ms(self) -> int:
        """Epoch milliseconds, the event log's own unit."""
        return round(self._utc.timestamp() * 1000)

    def monotonic(self) -> float:
        """Monotonic seconds, as ``time.monotonic()``."""
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move wall time and monotonic time forward together."""
        self._utc += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleep(self, seconds: float) -> None:
        """Stand in for ``time.sleep``: nothing waits, both clocks move on."""
        self.advance(max(seconds, 0.0))

    async def async_sleep(self, seconds: float) -> None:
        """Stand in for ``asyncio.sleep``: the same, for code that awaits."""
        self.sleep(seconds)

    def set(self, moment: datetime) -> None:
        """Jump the wall clock to ``moment`` (aware, or naive: in the zone); monotonic stays."""
        self._utc = self._as_utc(moment)
