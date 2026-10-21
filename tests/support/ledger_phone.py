"""A week of phone rows timed from the ledger's origin."""

from __future__ import annotations

from datetime import datetime

from tests.support.day_line import Row, health, visit
from tests.support.ledger import ZONE

ORIGIN = int(datetime(2026, 10, 4, tzinfo=ZONE).timestamp() * 1000)  # Sunday 00:00


STEPS, WORKOUT = "steps", "workout"


def at(day: int, hour: int, minute: int = 0) -> float:
    """Minutes from Sunday 10-04 00:00 to ``day`` of October at ``hour:minute``."""
    return (day - 4) * 1440 + hour * 60 + minute


def phone_rows() -> list[Row]:
    """Mon 10-05 is full, Tue 10-06 has nothing, Wed 10-07 crosses midnight both ways, Thu is today.

    The compact stretch (more than seven days back) has one day, Wed 09-30.
    """
    return [
        # Sunday night to Monday morning, in three stages with awake gaps: 390 minutes asleep (a
        # span's `value` is not read; the minutes asleep are the length of the spans).
        health("sl1", at(4, 23, 30), at(5, 1), 90),
        health("sl2", at(5, 1, 10), at(5, 3), 110),
        health("sl3", at(5, 3, 20), at(5, 6, 30), 190),
        # Monday steps; the first window is reported twice and the later total stands.
        health("st1", at(5, 8), at(5, 9), 1200, STEPS),
        health("st1b", at(5, 8), at(5, 9), 1250, STEPS),
        health("st2", at(5, 12), at(5, 13), 3000.5, STEPS),
        health("st3", at(5, 18), at(5, 19), 2219.5, STEPS),
        health("wo1", at(5, 17), at(5, 17, 45), 45 * 60, WORKOUT),
        # Monday places: Home before dawn (arrived Sunday), the library, a short coffee, an
        # unnamed stay, Home again until 23:00. Arrivals are reported first, then with departures.
        visit("v0", at(4, 22), at(5, 8, 10), "Home"),
        visit("v1a", at(5, 8, 30), None, "Library"),
        visit("v1", at(5, 8, 30), at(5, 12), "Library"),
        visit("v2", at(5, 12, 20), at(5, 12, 30), "Cafe"),
        visit("v3", at(5, 13), at(5, 14)),
        visit("v4", at(5, 14, 30), at(5, 23), "Home"),
        # Tuesday night to Wednesday morning: the sleep belongs to Wednesday.
        health("sl4", at(6, 23, 45), at(7, 3), 195),
        health("sl5", at(7, 3, 10), at(7, 7, 15), 245),
        # Wednesday steps (the last window runs past midnight: half is Wednesday's) and places.
        health("st4", at(7, 9), at(7, 10), 6000, STEPS),
        health("st5", at(7, 23, 30), at(8, 0, 30), 1000, STEPS),
        visit("v5", at(7, 18), at(7, 19), "Gym"),
        visit("v6", at(7, 19, 30), None, "Home"),  # still there when the day ends
        # Wednesday night to Thursday morning, and Thursday so far.
        health("sl6", at(7, 23, 50), at(8, 7, 20), 450),
        health("st6", at(8, 7, 30), at(8, 8, 30), 800, STEPS),
        health("st7", at(8, 10), at(8, 11), 1500, STEPS),
        # The compact stretch (day 0 of October is Wednesday 09-30): woke at 06:00, 420 minutes.
        health("sl7", at(-1, 23), at(0, 6), 420),
        health("st8", at(0, 9), at(0, 10), 9000, STEPS),
    ]
