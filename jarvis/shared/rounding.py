"""One rounding for every bus time (ADR 0342): a departure falls to the minute, an arrival rises.

A bus said to leave later than it does costs him the bus; said earlier, a minute. For an arrival it
is the other way round. Every surface (the answer, the card, the pin and its refresh, the ride and
the Live Activity) rounds through these two, so no two of them say different minutes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

MINUTE_MS: Final = 60_000


def departure_ms(ms: int) -> int:
    """``ms`` (epoch) rounded down to the minute."""
    return ms // MINUTE_MS * MINUTE_MS


def arrival_ms(ms: int) -> int:
    """``ms`` (epoch) rounded up to the minute."""
    return -(-ms // MINUTE_MS) * MINUTE_MS


def departure(at: datetime) -> datetime:
    """``at`` rounded down to the minute."""
    return at.replace(second=0, microsecond=0)


def arrival(at: datetime) -> datetime:
    """``at`` rounded up to the minute."""
    down = at.replace(second=0, microsecond=0)
    return down if down == at else down + timedelta(minutes=1)
