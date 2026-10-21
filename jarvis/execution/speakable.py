"""How the day tools word a time, a day and a stretch of time (local, as he would say them)."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from jarvis.execution.tools import ToolError

if TYPE_CHECKING:
    from datetime import tzinfo

_NOON: Final = 12
_MINUTE: Final = 60
_HOUR: Final = 3600


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


_EN_WEEKDAY: Final = (
    r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?"
    r"|sat(?:urday)?|sun(?:day)?)"
)
_EN_MONTH: Final = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_NAMES: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS: Final = tuple(name.lower() for name in calendar.month_abbr[1:])
_ZH_DIGITS: Final = {c: i for i, c in enumerate("一二三四五六七八九十", 1)} | {"两": 2}
_ZH_WEEKDAY: Final = {**{c: i for i, c in enumerate("一二三四五六")}, "日": 6, "天": 6}
_REL: Final = {"这": 0, "这个": 0, "本": 0, "this": 0, "下": 1, "下个": 1, "next": 1,
               "上": -1, "上个": -1, "last": -1}
_ZH_REL: Final = r"(这个?|本|下个?|上个?)"
_EN_REL: Final = r"(this|next|last)"
_SPAN_SPLIT: Final = re.compile(r"\s*(?:到|至|~|\bto\b|\bthrough\b|\s-\s|(?<=\D)-(?=\D))\s*")
_ISO: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_ZH_TODAY: Final = ("今天", "今日", "今晚", "今夜", "今天晚上")
_EN_DAY_AFTER: Final = ("the day after", "the day after tomorrow", "day after tomorrow")
_EN_DAY_BEFORE: Final = ("the day before yesterday", "day before yesterday")
_Range = tuple[date, date]


def _zh_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if len(text) == 2 and text[0] == "十" and text[1] in _ZH_DIGITS:  # noqa: PLR2004
        return 10 + _ZH_DIGITS[text[1]]
    if len(text) == 1 and text in _ZH_DIGITS:
        return _ZH_DIGITS[text]
    return None


def _monday(day: date, weeks: int) -> date:
    return day - timedelta(days=day.weekday()) + timedelta(weeks=weeks)


def _month_range(today: date, months: int) -> _Range:
    index = today.year * 12 + today.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, 1), date(year, month + 1, calendar.monthrange(year, month + 1)[1])


def _month_day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _clip(first: date, last: date, today: date, *, back: bool) -> _Range:
    """A stretch that is still running is cut at today: its past when back, its rest when ahead."""
    if back and first <= today < last:
        return first, today
    if not back and first < today <= last:
        return today, last
    return first, last


def _weekend(monday: date) -> _Range:
    return monday + timedelta(days=5), monday + timedelta(days=6)


def _day_of_month(day: int, today: date, *, back: bool, after: date | None) -> date | None:
    """The day of the month: ahead the next one (today's included), back the last one."""
    start = after or today
    for step in range(13):
        year, month = divmod(start.year * 12 + start.month - 1 + (-step if back else step), 12)
        if (found := _month_day(year, month + 1, day)) is None:
            continue
        if (found <= today) if back and after is None else (found >= start):
            return found
    return None


def _month_and_day(month: int, day: int, today: date, *, back: bool) -> date | None:
    this = _month_day(today.year, month, day)
    if this is None:
        return None
    if back:
        return this if this <= today else _month_day(today.year - 1, month, day)
    return this if this >= today else _month_day(today.year + 1, month, day)


def _one(text: str, today: date, *, back: bool, after: date | None) -> _Range | None:  # noqa: C901, PLR0911, PLR0912
    """One day or stretch from ``text`` (already lower-cased, spaces collapsed), else None."""
    zh = text.replace(" ", "")
    one = timedelta(days=1)
    if text in ("today", "tonight", "this evening") or zh in _ZH_TODAY:
        return today, today
    if text == "tomorrow" or zh in ("明天", "明日"):
        return today + one, today + one
    if text in _EN_DAY_AFTER or zh == "后天":
        return today + 2 * one, today + 2 * one
    if text == "yesterday" or zh in ("昨天", "昨日"):
        return today - one, today - one
    if text in _EN_DAY_BEFORE or zh == "前天":
        return today - 2 * one, today - 2 * one
    if _ISO.fullmatch(text[:10]) and len(text) in (10, 19, 20, 25):
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return day, day
    if match := re.fullmatch(_ZH_REL + r"?(?:周|星期|礼拜)([一二三四五六日天])", zh):
        index, rel = _ZH_WEEKDAY[match[2]], _REL.get(match[1] or "")
        return _weekday(index, rel, today, back=back, after=after)
    if match := re.fullmatch(rf"(?:{_EN_REL} )?{_EN_WEEKDAY}", text):
        index, rel = _NAMES.index(match[2][:3]), _REL.get(match[1] or "")
        return _weekday(index, rel, today, back=back, after=after)
    if match := re.fullmatch(_ZH_REL + r"(?:周|星期|礼拜)", zh) or re.fullmatch(
        _EN_REL + " week", text,
    ):
        first = _monday(today, _REL[match[1]])
        return _clip(first, first + 6 * one, today, back=back)
    if match := re.fullmatch(_ZH_REL + r"?周末", zh) or re.fullmatch(
        rf"(?:{_EN_REL} )?weekend", text,
    ):
        bare = -1 if back and today.weekday() < 5 else 0  # noqa: PLR2004
        weeks = bare if match[1] is None else _REL[match[1]]
        return _clip(*_weekend(_monday(today, weeks)), today, back=back)
    if match := re.fullmatch(_ZH_REL + "月", zh) or re.fullmatch(_EN_REL + " month", text):
        return _clip(*_month_range(today, _REL[match[1]]), today, back=back)
    if match := re.fullmatch(r"(\d{1,2})月(\d{1,2})[号日]?", zh) or re.fullmatch(
        rf"{_EN_MONTH}\.? (\d{{1,2}})(?:st|nd|rd|th)?", text,
    ):
        month = int(match[1]) if match[1].isdigit() else _MONTHS.index(match[1][:3]) + 1
        named = _month_and_day(month, int(match[2]), today, back=back)
        return (named, named) if named else None
    if match := re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{_EN_MONTH}", text):
        named = _month_and_day(_MONTHS.index(match[2][:3]) + 1, int(match[1]), today, back=back)
        return (named, named) if named else None
    if match := re.fullmatch(r"(\d{1,2})[号日]", zh) or re.fullmatch(
        r"(?:on )?(?:the )?(\d{1,2})(?:st|nd|rd|th)", text,
    ):
        found = _day_of_month(int(match[1]), today, back=back, after=after)
        return (found, found) if found else None
    return None


def _weekday(index: int, rel: int | None, today: date, *, back: bool, after: date | None) -> _Range:
    if rel is not None:
        day = _monday(today, rel) + timedelta(days=index)
    elif back and after is None:
        day = today - timedelta(days=(today.weekday() - index) % 7)
    else:
        start = after or today
        day = start + timedelta(days=(index - start.weekday()) % 7)
    return day, day


def _back_only(text: str, today: date) -> _Range | None:
    zh = text.replace(" ", "")
    one = timedelta(days=1)
    if match := re.fullmatch(r"(?:最近|过去|近)([\d一二两三四五六七八九十]+)天", zh) or (
        re.fullmatch(r"(?:the )?(?:past|last) (\d+) days", text)
    ):
        count = _zh_number(match[1])
        return (today - (count - 1) * one, today) if count else None
    if match := (
        re.fullmatch(r"(.+)以来", zh)
        or re.fullmatch(r"从(.+)起", zh)
        or re.fullmatch(r"since (.+)", text)
    ):
        found = _one(match[1].strip(), today, back=True, after=None)
        return (found[0], today) if found else None
    return None


def resolve_when(words: str, today: date, *, back: bool, tool: str) -> _Range:
    """``(first, last)`` for his words about a day or a stretch, as of ``today``.

    ``back`` reads a weekday or a day of the month as the last one (today's included), a still
    running week or month as ending today, and takes past N days and since <day>; ahead reads the
    next one and a running stretch as its rest. A span is two of these joined by to, through, 到,
    至 or a dash; its second part counts forward from the first.
    """
    text = " ".join(words.lower().split()).rstrip(".。!?\uff01\uff1f ")
    if (found := _read(text, today, back=back)) is not None:
        return found
    parts = _SPAN_SPLIT.split(text)
    if len(parts) == 2 and all(parts):  # noqa: PLR2004
        left = _read(parts[0], today, back=back)
        bare = f"{parts[1]}号" if parts[1].isdigit() else parts[1]  # Oct 25 - 27
        right = left and _one(bare, today, back=False, after=left[0])
        if left and right:
            if right[1] < left[0]:
                msg = f"{tool}: {words!r} ends before it starts"
                raise ToolError(msg, code="invalid_arguments")
            return left[0], right[1]
    forms = (
        "today, tonight, tomorrow, the day after, yesterday, the day before yesterday,"
        " a weekday (Fri, 周五; this, next or last before it), this, next or last week, weekend or"
        " month (这周, 下周末, 上个月), a day of the month (the 25th, 25号), Oct 25, 10月25日,"
        " 2026-10-25"
        + (", past N days (最近3天), since <day> (<day>以来), " if back else ", ")
        + "and two of these joined by to, through, 到 or 至"
    )
    msg = f"{tool}: I read {forms}; not {words!r}"
    raise ToolError(msg, code="invalid_arguments")


def _read(text: str, today: date, *, back: bool) -> _Range | None:
    if (found := _one(text, today, back=back, after=None)) is not None:
        return found
    return _back_only(text, today) if back else None


def phrasings(value: object) -> list[str]:
    """A search's phrasings: one string, or a list of them (his words in each language)."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    kept = (text.strip() for text in items if isinstance(text, str))
    return list(dict.fromkeys(text for text in kept if text))


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
