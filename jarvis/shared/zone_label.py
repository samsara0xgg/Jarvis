"""A zone label a mail writes beside a time, and the instants that time stands for (ADR 0368).

The job-mail reader finds a date and a wall-clock time in a sentence and asks :func:`read` what
instants that time stands for in his zone. A time with no label is his own. A label is read as
the zone it names: Mountain, Central and Eastern as Edmonton, Winnipeg and Toronto, ``UTC`` and
``GMT`` as UTC, and an offset (``UTC-5``, ``GMT+8``, ``UTC+05:30``) as that offset, which beats a
name in the same sentence. ``PST``, ``PDT``, ``PT`` and ``Pacific`` are read as America/Los_Angeles
and as America/Vancouver, because a sender writes them loosely ("PST" in summer for PDT, "PT" for
BC's own time) and the two zones differ whenever US Pacific is on standard time and BC is not. On
a date where they differ both instants come back, the earlier first; otherwise one.

:func:`said` quotes what the mail wrote for the time, and :func:`when_text` words the readings
for him: the one place the two-reading text is built.

Layer rules: stdlib and ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from jarvis.shared import lang

if TYPE_CHECKING:
    from datetime import tzinfo

    from jarvis.shared.lang import Language

_PACIFIC: Final[tuple[str, ...]] = ("America/Los_Angeles", "America/Vancouver")
_MOUNTAIN: Final[tuple[str, ...]] = ("America/Edmonton",)
_CENTRAL: Final[tuple[str, ...]] = ("America/Winnipeg",)
_EASTERN: Final[tuple[str, ...]] = ("America/Toronto",)
_UTC: Final[tuple[str, ...]] = ("UTC",)
_ZONES: Final[dict[str, tuple[str, ...]]] = {
    "PST": _PACIFIC,
    "PDT": _PACIFIC,
    "PT": _PACIFIC,
    "Pacific": _PACIFIC,
    "MST": _MOUNTAIN,
    "MDT": _MOUNTAIN,
    "MT": _MOUNTAIN,
    "Mountain": _MOUNTAIN,
    "CST": _CENTRAL,
    "CDT": _CENTRAL,
    "CT": _CENTRAL,
    "Central": _CENTRAL,
    "EST": _EASTERN,
    "EDT": _EASTERN,
    "ET": _EASTERN,
    "Eastern": _EASTERN,
    "UTC": _UTC,
    "GMT": _UTC,
}
# A name and the words after it that belong to the label ("Pacific Standard Time"); the match
# is what :func:`said` quotes, its first group the key of ``_ZONES``.
_NAME: Final = re.compile(
    r"\b(" + "|".join(_ZONES) + r")\b(?:\s+(?:Standard|Daylight))?(?:\s+Time)?"
)
# An offset is written with a hyphen, a minus sign or an en dash.
_OFFSET: Final = re.compile(
    r"\b(?:UTC|GMT)\s?([+\-\u2212\u2013])\s?(\d{1,2})(?::?([0-5]\d))?(?!\d)"
)
_MAX_OFFSET_HOURS: Final[int] = 14
_CLOCK: Final[str] = r"\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?"
# The clock, or the range of clocks, written right before a label ("10 am", "1:00pm - 2:00pm").
_TIME_BEFORE: Final = re.compile(
    rf"({_CLOCK}(?:\s*(?:-|\u2013|\u2014|to)\s*{_CLOCK})?)\s*[(\[]?\s*$", re.IGNORECASE
)
_UNQUOTED: Final[str] = "Pacific"


def _find(sentence: str) -> tuple[re.Match[str], tuple[tzinfo, ...]] | None:
    """The label a sentence names, with the zones it stands for: an offset first, else a name."""
    for offset in _OFFSET.finditer(sentence):
        hours, minutes = int(offset[2]), int(offset[3] or 0)
        if hours <= _MAX_OFFSET_HOURS:
            sign = 1 if offset[1] == "+" else -1
            return offset, (timezone(sign * timedelta(hours=hours, minutes=minutes)),)
    named = _NAME.search(sentence)
    if named is None:
        return None
    return named, tuple(ZoneInfo(key) for key in _ZONES[named[1]])


def read(sentence: str, day: date, clock: tuple[int, int], zone: tzinfo) -> tuple[datetime, ...]:
    """The instants a date and a clock time stand for, in ``zone``, the earlier first.

    ``day`` and ``clock`` (hour, minute) are what ``sentence`` gives, and the label in ``sentence``
    says whose clock it is. With no label it is ``zone``'s, his own, and the answer is one
    instant. Two come back only for a Pacific label, on a date US and BC Pacific tell differently.
    A ``clock`` that is no time of day is a ValueError.
    """
    found = _find(sentence)
    zones = (zone,) if found is None else found[1]
    return tuple(
        sorted(
            {
                datetime(day.year, day.month, day.day, *clock, tzinfo=named).astimezone(zone)
                for named in zones
            }
        )
    )


def said(sentence: str) -> str:
    """What the mail wrote for the time and its zone (``10 am PT``); '' when it names no zone."""
    found = _find(sentence)
    if found is None:
        return ""
    label = found[0]
    clock = _TIME_BEFORE.search(sentence[: label.start()])
    return " ".join((f"{clock[1]} {label[0]}" if clock else label[0]).split())


def _clock(at: datetime, minutes: int | None) -> str:
    """``10:00``, or ``10:00-11:00`` when the event runs ``minutes``."""
    end = f"-{at + timedelta(minutes=minutes):%H:%M}" if minutes else ""
    return f"{at:%H:%M}{end}"


def when_text(  # noqa: PLR0913 - the readings, his zone, the mail's words, a length and a language
    first: datetime,
    later: datetime | None,
    zone: tzinfo,
    *,
    sentence: str = "",
    minutes: int | None = None,
    language: Language | None = None,
) -> str:
    """The time of an event as he is told it, in ``zone``: ``10:00``, or both of its readings.

    With a ``later`` reading it is ``10:00 or 11:00 (the mail says 10 am PT, BC or US Pacific)``,
    worded in ``language`` (the current one by default); the quoted words are :func:`said` of
    ``sentence``, the mail's own. A ``later`` on another day than ``first`` carries its date.
    ``minutes`` is how long the event runs, shown as the end of each reading.
    """
    one = first.astimezone(zone)
    if later is None:
        return _clock(one, minutes)
    other = later.astimezone(zone)
    second = _clock(other, minutes)
    if other.date() != one.date():
        second = f"{other:%m-%d} {second}"
    return lang.t(
        "job.when.two",
        lang=language,
        first=_clock(one, minutes),
        later=second,
        said=said(sentence) or _UNQUOTED,
    )
