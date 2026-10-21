"""A zone label a mail writes beside a time, and the instants that time stands for (ADR 0368).

The job-mail reader finds a date and a wall-clock time in a sentence and asks :func:`read` what
instants that time stands for in his zone. A time with no label is his own. A label is read as
the zone it names: Mountain, Central and Eastern as Edmonton, Winnipeg and Toronto, ``UTC`` and
``GMT`` as UTC, and an offset (``UTC-5``, ``GMT+8``, ``UTC+05:30``) as that offset, which beats a
name in the same sentence. ``PST``, ``PDT``, ``PT`` and ``Pacific`` are read as America/Los_Angeles
and as America/Vancouver, because a sender writes them loosely ("PST" in summer for PDT, "PT" for
BC's own time) and the two zones differ whenever US Pacific is on standard time and BC is not. On
a date where they differ both instants come back, the earlier first; otherwise one.

Layer rules: stdlib and ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from datetime import tzinfo

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
# A name and the words after it that belong to the label ("Pacific Standard Time"); its first
# group is the key of ``_ZONES``.
_NAME: Final = re.compile(
    r"\b(" + "|".join(_ZONES) + r")\b(?:\s+(?:Standard|Daylight))?(?:\s+Time)?"
)
# An offset is written with a hyphen, a minus sign or an en dash.
_OFFSET: Final = re.compile(
    r"\b(?:UTC|GMT)\s?([+\-\u2212\u2013])\s?(\d{1,2})(?::?([0-5]\d))?(?!\d)"
)
_MAX_OFFSET_HOURS: Final[int] = 14


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
