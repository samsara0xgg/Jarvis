"""Which open things are late: the one question the ledger block and the 04:00 draft ask alike.

A reader that lists a day's things (ADR 0201's today-so-far, the draft of ADR 0288) also owes him
the things whose time went by before the day began and which nobody has closed (ADR 0299).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING, Final

from jarvis.state import things

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import tzinfo


_DAY_CHARS: Final = len("YYYY-MM-DD")


def due_is_day(one: things.Thing) -> bool:
    """Whether ``one.due`` is a date alone (by the end of that day), not a time."""
    return len(one.due) == _DAY_CHARS


def when_ms(one: things.Thing, zone: tzinfo) -> int | None:
    """The instant ``one`` is due or starts (the first one of a series); None when untimed."""
    if one.due:
        return things.due_ms(one, zone)
    if one.start_at:
        return int(datetime.fromisoformat(one.start_at).timestamp() * 1000)
    if one.on_day:
        return int(datetime.combine(
            date.fromisoformat(one.on_day), time.min, tzinfo=zone,
        ).timestamp() * 1000)
    return None


def late(open_things: Iterable[things.Thing], before_ms: int, zone: tzinfo) -> list[things.Thing]:
    """The open ``do`` things whose time came before ``before_ms``, in the order given.

    A ``do`` is late when it was due, or was to start or be done on its day, before ``before_ms``.
    A ``go`` is never late (he went, or the time is gone), nor is a weekly thing, whose dates are
    closed one at a time, nor an untimed thing, which has no time to miss.
    """
    found: list[things.Thing] = []
    for one in open_things:
        if one.state != "open" or one.kind != "do" or one.weekly_until:
            continue
        at = when_ms(one, zone)
        if at is not None and at < before_ms:
            found.append(one)
    return found
