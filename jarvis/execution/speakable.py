"""How the day tools word a time, a day and a stretch of time (local, as he would say them)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from jarvis.execution.tools import ToolError

if TYPE_CHECKING:
    from datetime import tzinfo

_NOON: Final = 12
_MINUTE: Final = 60
_HOUR: Final = 3600


def now() -> datetime:
    """The clock the day tools read unless a test hands them another."""
    return datetime.now(UTC)


def cut(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def clock(at: datetime) -> str:
    """``2:40 pm`` for an already-local time."""
    hour = at.hour % _NOON or _NOON
    return f"{hour}:{at.minute:02d} {'am' if at.hour < _NOON else 'pm'}"


def duration(seconds: float) -> str:
    """``3h 40m``, ``2h``, ``45m`` (under half a minute rounds to ``0m``)."""
    hours, minutes = divmod(round(seconds / _MINUTE), _MINUTE)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def day_name(day: date, today: date) -> str:
    """``today (Thu)``, ``yesterday (Wed)``, ``tomorrow (Fri)``, else ``Mon Sep 22`` (+ year)."""
    if (name := {-1: "tomorrow", 0: "today", 1: "yesterday"}.get((today - day).days)) is not None:
        return f"{name} ({day:%a})"
    year = f" {day.year}" if day.year != today.year else ""
    return f"{day:%a %b} {day.day}{year}"


def when(at: datetime, current: datetime, zone: tzinfo) -> str:
    """``at`` as he would say it: ``today (Thu) 2:40 pm``, else ``Mon Sep 22, 2:40 pm``."""
    local = at.astimezone(zone)
    today = current.astimezone(zone).date()
    if (today - local.date()).days in (0, 1):
        return f"{day_name(local.date(), today)} {clock(local)}"
    return f"{day_name(local.date(), today)}, {clock(local)}"


def span(first: date, last: date, today: date) -> str:
    """``Mon Oct 5 to Sun Oct 11`` (one day: its own name)."""
    if first == last:
        return day_name(first, today)
    return f"{day_name(first, today)} to {day_name(last, today)}"


def age(then: datetime, current: datetime) -> str:
    """How long before ``current`` ``then`` was: ``12 minutes ago``, ``3 hours ago``."""
    seconds = max(0, round((current - then).total_seconds()))
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        minutes = seconds // _MINUTE
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = seconds // _HOUR
    if hours < 48:  # noqa: PLR2004 - past two days a count of hours is not how anyone says it
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    return f"{days} days ago"


def parse_day(value: str, today: date, tool: str) -> date:
    """A local day from ``2026-10-01``, ``today`` or ``yesterday``; else a ToolError saying how."""
    word = value.strip().lower()
    if word == "today":
        return today
    if word == "yesterday":
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(word)
    except ValueError:
        msg = f"{tool}: a date looks like 2026-10-01 (or the words today, yesterday), not {value!r}"
        raise ToolError(msg, code="invalid_arguments") from None
    if day > today:
        msg = f"{tool}: {value} has not happened yet; today is {today.isoformat()}"
        raise ToolError(msg, code="invalid_arguments")
    return day


def page_number(value: object, tool: str, example: str) -> int:
    """The page asked for: 1 when none is, else a whole number from 1."""
    if value is None:
        return 1
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{tool}: page must be a whole number, e.g. page=2 for {example}"
        raise ToolError(msg, code="invalid_arguments")
    return value
