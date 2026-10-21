"""ADR 0355: today in parts, added up in code from WeatherKit's hourly rows, and the line first.

A daily row covers midnight to midnight, so it cannot say that the drizzle is over. These
functions take the hourly rows of a place's day (from its local midnight) and say what fell
earlier, what is left of the day and what tonight holds, and write the first line of an answer
from those figures. They read no clock and call nothing: the caller passes ``now`` as a time in
the place's zone, and the rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from zoneinfo import ZoneInfo

type Hour = tuple[datetime, Mapping[str, Any]]
"""One hourly row and its start, local to the place."""

LIKELY: Final = 0.5
"""A chance at or above this makes an hour one in which rain is likely."""
NIGHT_FROM: Final = time(19)
NIGHT_TO: Final = time(7)
"""Apple's own split of a day: the daytime part is 07:00 to 19:00, the overnight part the rest."""
LINE_CHARS: Final = 380
"""A first line stays under this, inside the standard's 400."""
RESULT_CHARS: Final = 3800
"""An answer is cut to this many characters, leaving room for what the tool adds after it."""
_TRACE_MM: Final = 0.5
_QUIET_PCT: Final = 10
_PCT: Final = 100
_AHEAD_HOURS: Final = 12
_SEE_MORE: Final = "ask for fewer days, or a narrower `when`, to see them"
_CUT_ORDER: Final = ("hourly", "next_hours", "air_quality.days", "days")
"""The lists an over-long answer gives up, first to last, one whole row at a time."""
_KEEP_ROWS: Final = {"days": 1}


def hours_of(body: Mapping[str, Any], zone: ZoneInfo) -> list[Hour]:
    """The hourly rows of a WeatherKit answer with their local starts, earliest first."""
    rows = (body.get("forecastHourly") or {}).get("hours", ())
    found = [(datetime.fromisoformat(row["forecastStart"]).astimezone(zone), row) for row in rows]
    return sorted(found, key=lambda hour: hour[0])


def clock(at: datetime) -> str:
    """``6:00``: a local time as the lines say it."""
    return f"{at.hour}:{at.minute:02d}"


def _stop(start: datetime) -> datetime:
    """The end of the hour that begins at ``start``, by the clock, not the wall."""
    return (start.astimezone(UTC) + timedelta(hours=1)).astimezone(start.tzinfo)


def _until(start: datetime) -> str:
    """The end of the hour that begins at ``start``; the midnight that ends a day is 24:00."""
    stop = _stop(start)
    return "24:00" if stop.date() != start.date() and stop.time() == time() else clock(stop)


def _mm(rows: Sequence[Hour]) -> float:
    total = sum(float(row.get("precipitationAmount") or 0) for _, row in rows)
    return max(round(total, 1), 0.1) if total > 0 else 0.0


def _chance(rows: Sequence[Hour]) -> int:
    peak = max((float(row.get("precipitationChance") or 0) for _, row in rows), default=0.0)
    return round(peak * _PCT)


def _wet(row: Mapping[str, Any]) -> bool:
    return float(row.get("precipitationAmount") or 0) > 0


def _likely(row: Mapping[str, Any]) -> bool:
    return float(row.get("precipitationChance") or 0) >= LIKELY


def earlier_today(past: Sequence[Hour], words: Callable[[str], str]) -> dict[str, Any] | None:
    """What fell from midnight to now: the amount, from and until when, and the condition.

    None when no hour of the day has ended yet.
    """
    if not past:
        return None
    wet = [hour for hour in past if _wet(hour[1])]
    out: dict[str, Any] = {"rain_mm": _mm(past)}
    if not wet:
        out["note"] = "dry since midnight"
        return out
    heaviest = max(wet, key=lambda hour: float(hour[1].get("precipitationAmount") or 0))
    out["from"] = clock(wet[0][0])
    out["until"] = _until(wet[-1][0])
    out["condition"] = words(str(heaviest[1].get("conditionCode", "")))
    return out


def rest_of_today(left: Sequence[Hour], now: datetime) -> dict[str, Any] | None:
    """From now to midnight: the highest chance, the amount, and from when to when rain is likely.

    ``left`` are the rows from the hour ``now`` is in. None when there are none.
    """
    if not left:
        return None
    out: dict[str, Any] = {"max_chance_pct": _chance(left), "rain_mm": _mm(left)}
    likely = [hour for hour in left if _likely(hour[1])]
    if likely:
        begun = likely[0][0].timestamp() <= now.timestamp()
        out["likely_from"] = "now" if begun else clock(likely[0][0])
        out["likely_to"] = _until(likely[-1][0])
    elif out["max_chance_pct"] <= _QUIET_PCT:
        out["note"] = "no more rain expected today"
    return out


def _night(now: datetime) -> tuple[datetime, datetime]:
    """The night ``now`` is in or the one coming: 19:00 to 07:00, from now once it has begun."""
    zone = now.tzinfo
    hour = now.replace(minute=0, second=0, microsecond=0)
    if now.time() < NIGHT_TO:
        return hour, datetime.combine(now.date(), NIGHT_TO, zone)
    start = datetime.combine(now.date(), NIGHT_FROM, zone)
    return max(hour, start), datetime.combine(now.date() + timedelta(days=1), NIGHT_TO, zone)


def tonight(hours: Sequence[Hour], now: datetime) -> dict[str, Any] | None:
    """19:00 to 07:00 (from now when it is past 19:00 or before 07:00): the low, rain, the hours."""
    begin, end = _night(now)
    rows = [h for h in hours if begin.timestamp() <= h[0].timestamp() < end.timestamp()]
    cold = [h for h in rows if h[1].get("temperature") is not None]
    if not rows or not cold:
        return None
    low_at, low = min(cold, key=lambda hour: float(hour[1]["temperature"]))
    began = begin.timestamp() <= now.timestamp()
    out: dict[str, Any] = {
        "from": "now" if began else clock(begin), "to": clock(end),
        "low_c": round(float(low["temperature"]), 1), "low_at": clock(low_at),
        "rain_chance_pct": _chance(rows), "rain_mm": _mm(rows),
    }
    if out["rain_chance_pct"] <= _QUIET_PCT and out["rain_mm"] == 0:
        out["note"] = "dry"
    return out


def split(hours: Sequence[Hour], now: datetime, words: Callable[[str], str]) -> dict[str, Any]:
    """``earlier_today``, ``rest_of_today`` and ``tonight`` of the day ``now`` is in."""
    this_hour = now.replace(minute=0, second=0, microsecond=0).timestamp()
    mine = [hour for hour in hours if hour[0].date() == now.date()]
    parts = {
        "earlier_today": earlier_today([h for h in mine if h[0].timestamp() < this_hour], words),
        "rest_of_today": rest_of_today(
            [h for h in mine if h[0].timestamp() >= this_hour], now,
        ),
        "tonight": tonight(hours, now),
    }
    return {name: part for name, part in parts.items() if part is not None}


def horizon(now: datetime) -> datetime:
    """How far the next hours reach: the end of tonight, or 12 hours on if that is later."""
    return max(_night(now)[1], now + timedelta(hours=_AHEAD_HOURS))


def hours_between(hours: Sequence[Hour], start: datetime, end: datetime) -> list[Hour]:
    """The rows that overlap ``start`` to ``end``."""
    return [
        hour for hour in hours
        if _stop(hour[0]).timestamp() > start.timestamp() and hour[0].timestamp() < end.timestamp()
    ]


def _amount(mm: float) -> str:
    return "a trace" if mm < _TRACE_MM else f"about {mm:g} mm"


def _rain(chance: int, mm: float) -> str:
    """How a stretch's rain is worded from its highest chance and its amount."""
    if chance >= LIKELY * _PCT:
        return f"rain likely ({chance}%)"
    return f"a {chance}% chance of rain" if chance > _QUIET_PCT or mm >= _TRACE_MM else "dry"


def _today_sentences(parts: Mapping[str, Any]) -> list[str]:
    """The sentences of today in parts: what fell, what is left of it, tonight."""
    out: list[str] = []
    if (earlier := parts.get("earlier_today")) and "until" in earlier:
        out.append(f"{_amount(earlier['rain_mm'])} fell before {earlier['until']}")
    if rest := parts.get("rest_of_today"):
        if "likely_from" in rest:
            span = (
                f"until {rest['likely_to']}" if rest["likely_from"] == "now"
                else f"{rest['likely_from']}-{rest['likely_to']}"
            )
            out.append(
                f"rain likely {span} today (up to {rest['max_chance_pct']}%,"
                f" {_amount(rest['rain_mm'])})",
            )
        elif "note" in rest:
            out.append("no more rain today")
        else:
            out.append(f"no more rain likely today (up to {rest['max_chance_pct']}%)")
    if night := parts.get("tonight"):
        wet = _rain(night["rain_chance_pct"], night["rain_mm"])
        out.append(f"tonight's low {round(night['low_c'])}° at {night['low_at']}, {wet}")
    return out


def day_sentence(row: Mapping[str, Any], *, past: bool) -> str:
    """One whole day in a sentence: ``Sat Oct 25: rain, 12°/7°, rain 70% (about 3.5 mm)``."""
    high, low = row.get("high_c"), row.get("low_c")
    temps = "" if high is None or low is None else f", {round(high)}°/{round(low)}°"
    mm = float(row.get("precip_mm") or 0)
    chance = row.get("rain_chance_pct")
    if past:
        rain = f"{_amount(mm)} of rain" if mm > 0 else "no rain"
    elif chance is None:
        rain = "rain not forecast"
    elif chance <= _QUIET_PCT and mm < _TRACE_MM:
        rain = "dry"
    else:
        rain = f"rain {chance}% ({_amount(mm)})"
    return f"{row['day']}: {row['condition']}{temps}, {rain}"


def now_sentence(now: Mapping[str, Any]) -> str | None:
    """``9° and cloudy now``; None when the answer has no temperature."""
    temp = now.get("temp_c")
    return None if temp is None else f"{round(temp)}° and {now.get('condition') or 'unknown'} now"


def first_line(
    label: str, now: Mapping[str, Any] | None, parts: Mapping[str, Any],
    days: Sequence[tuple[Mapping[str, Any], bool]],
) -> str:
    """The line an answer opens with, written from its figures (ADR 0355).

    ``label`` is the place; ``now`` the current conditions (None for days all gone by); ``parts``
    today in parts; ``days`` the other days asked, each with whether it is past, in order. The
    sentences are joined by semicolons and the last ones go first when the line runs long, with a
    count of the days left out.
    """
    opening = None if now is None else now_sentence(now)
    pieces = [
        *([opening] if opening else []),
        *_today_sentences(parts),
        *(day_sentence(row, past=past) for row, past in days),
    ]
    kept = len(pieces)
    while kept > 1 and len(_join(label, pieces[:kept], len(pieces) - kept)) > LINE_CHARS:
        kept -= 1
    return _join(label, pieces[:kept], len(pieces) - kept)[:LINE_CHARS]


def _join(label: str, pieces: Sequence[str], left_out: int) -> str:
    more = f"; {left_out} more in the rows" if left_out else ""
    return f"{label}: {'; '.join(pieces)}{more}."


def _size(result: Mapping[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def _list_at(result: dict[str, Any], path: str) -> list[Any] | None:
    holder: Any = result
    *keys, last = path.split(".")
    for key in keys:
        holder = holder.get(key) if isinstance(holder, dict) else None
    found = holder.get(last) if isinstance(holder, dict) else None
    return found if isinstance(found, list) else None


def fit(result: dict[str, Any], limit: int = RESULT_CHARS) -> dict[str, Any]:
    """``result`` cut to ``limit`` characters at whole rows, saying how many were left out.

    The hour rows go first, then the air quality's days, then the days from the end, one row at a
    time; ``left_out`` counts them by list and ``see_more`` says how to get them.
    """
    cut: dict[str, int] = {}

    def noted() -> dict[str, Any]:
        left = {path.replace(".", "_"): n for path, n in cut.items()}
        return result | ({"left_out": left, "see_more": _SEE_MORE} if cut else {})

    while _size(noted()) > limit:
        found = next(
            (
                (path, rows) for path in _CUT_ORDER
                if (rows := _list_at(result, path)) and len(rows) > _KEEP_ROWS.get(path, 0)
            ),
            None,
        )
        if found is None:
            break
        found[1].pop()
        cut[found[0]] = cut.get(found[0], 0) + 1
    return noted()


__all__ = [
    "LIKELY",
    "LINE_CHARS",
    "RESULT_CHARS",
    "Hour",
    "clock",
    "day_sentence",
    "earlier_today",
    "first_line",
    "fit",
    "horizon",
    "hours_between",
    "hours_of",
    "now_sentence",
    "rest_of_today",
    "split",
    "tonight",
]
