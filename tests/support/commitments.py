"""The commitments world's zone and clock: the owner's local time on a fixed day."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Los_Angeles")  # UTC-7 until Nov 1 2026, then UTC-8


NOW = datetime(2026, 10, 14, 9, 0, tzinfo=ZONE)  # a Wednesday


def local_at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE)
