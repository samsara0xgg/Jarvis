"""ADR 0388: his body, as his watch and phone measured it, is one read-only tool, ``body``.

Every Health measure the phone sends (ADR 0293) can be asked for over a day, a span of days or
some hours of one day, in his local time. A reading measure (heart rate and its kin, blood
oxygen, breathing, wrist temperature, noise) comes back as its count, low, median and high, with
the readings Health took while he was still apart; a total (steps, energy, distance) as its sum;
sleep as its nights with their stages; workouts as a list. Each stands beside his usual level for
the same hours of the 14 days before, and a stretch the watch recorded no heart rate in is named,
with the watch's charge readings inside it. Nothing here judges what a number means: she does.

One call reads the log three times at most, whatever it asks for: the readings, the hourly totals
and the spans, from the first of the days its usual level looks at to the window's end.

Layer rules: L4; reads the event log through :mod:`jarvis.state.body`, takes the time from the
call's clock (ADR 0322).
"""

from __future__ import annotations

import bisect
import re
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.execution import speakable
from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state import body
from jarvis.state.daily_contract import encoded
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.phone_day import HEALTH_EVENT, MEASURE_EVENT, sleeps, workouts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext
    from jarvis.shared import Event
    from jarvis.state.phone_day import Span

Kind = Literal["reading", "total", "sleep", "workout", "mindful", "stand_hours"]


@dataclass(frozen=True)
class _Measure:
    """One measure as the tool names it: the phone's metric, how it is read and how it is said."""

    name: str
    metric: str
    kind: Kind
    label: str
    unit: str = ""
    scale: float = 1.0
    places: int = 0


_MEASURES: Final = (
    _Measure("heart_rate", "heart_rate", "reading", "heart rate", "bpm"),
    _Measure("resting_heart_rate", "resting_heart_rate", "reading", "resting heart rate", "bpm"),
    _Measure("hrv", "heart_rate_variability_sdnn", "reading", "HRV (SDNN)", "ms"),
    _Measure("blood_oxygen", "oxygen_saturation", "reading", "blood oxygen", "%", scale=100),
    _Measure("breathing_rate", "respiratory_rate", "reading", "breathing rate", "/min", places=1),
    _Measure(
        "wrist_temperature", "sleeping_wrist_temperature", "reading", "wrist temperature asleep",
        "°C", places=2,
    ),
    _Measure("sleep", "sleep", "sleep", "sleep"),
    _Measure("workouts", "workout", "workout", "workouts"),
    _Measure("steps", "steps", "total", "steps", "steps"),
    _Measure("active_energy", "active_energy", "total", "active energy", "kcal"),
    _Measure("exercise_minutes", "exercise_time", "total", "exercise minutes", "min"),
    _Measure("stand_hours", "stand_hour", "stand_hours", "stand hours"),
    _Measure(
        "distance", "distance_walking_running", "total", "walking and running distance", "km",
        scale=0.001, places=2,
    ),
    _Measure("flights", "flights_climbed", "total", "flights climbed", "flights"),
    _Measure("daylight", "time_in_daylight", "total", "time in daylight", "min"),
    _Measure("noise", "environmental_audio_exposure", "reading", "noise around him", "dB"),
    _Measure("headphone_volume", "headphone_audio_exposure", "reading", "headphone volume", "dB"),
    _Measure(
        "walking_heart_rate", "walking_heart_rate_average", "reading", "walking heart rate", "bpm",
    ),
    _Measure("vo2_max", "vo2_max", "reading", "VO2 max", "mL/kg/min", places=1),
    _Measure("mindful", "mindful_session", "mindful", "mindful minutes"),
    _Measure("resting_energy", "basal_energy", "total", "resting energy", "kcal"),
    _Measure("stand_minutes", "stand_time", "total", "stand minutes", "min"),
)
_BY_NAME: Final = {m.name: m for m in _MEASURES}
_STEPS: Final = {"15min": 15, "30min": 30, "hour": 60, "day": 24 * 60}
_DETAILS: Final = ("summary", "readings")

_DESCRIPTION: Final = (
    "Use for how his body was or is, as his Apple Watch and phone measured it: heart rate, resting"
    " heart rate, HRV, blood oxygen, breathing, wrist temperature, sleep and its stages, workouts,"
    " steps and the rest Health records. Not for what he did or where he was: use my_day. Returns"
    " each measure over a day, days or some hours, in his local time: count, low, median and high"
    " or the total, by stretch, beside his usual level for the same hours of the 14 days before,"
    " and the stretches the watch recorded nothing in."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "measures": {
            "type": "array",
            "items": {"type": "string", "enum": [m.name for m in _MEASURES]},
            "description": (
                "What to read; leave out for one line on each measure recorded in the stretch."
                " Up to three come with their stretches."
            ),
        },
        "when": {
            "type": "string",
            "description": (
                "His words for the day or days: today, yesterday, Friday, last week, the past 7"
                " days, 2026-10-20. Leave out for today. Sleep belongs to the day he woke."
            ),
        },
        "from": {
            "type": "string",
            "description": (
                "Only some hours of that one day: where they start, his local time, 14:00."
            ),
        },
        "until": {
            "type": "string",
            "description": (
                "Where those hours end, his local time, 17:30, or now; leave out for the end of"
                " the day, or now today."
            ),
        },
        "step": {
            "type": "string",
            "enum": list(_STEPS),
            "description": (
                "How long each stretch is; leave out to fit the window (an hour for a day, a day"
                " for several days)."
            ),
        },
        "detail": {
            "type": "string",
            "enum": list(_DETAILS),
            "description": (
                "readings: every reading of one reading measure, for at most six hours. summary"
                " (default) does not list them."
            ),
        },
    },
    "additionalProperties": False,
}

_BUDGET: Final = 4000
_SUMMARY_CHARS: Final = 400
_WITH_STRETCHES: Final = 3
"""Measures asked for at most this many come with their stretches; more get one line each."""
_MAX_STRETCHES: Final = 48
_SPAN_DAYS: Final = 31
_USUAL_DAYS: Final = 14
_USUAL_MIN_DAYS: Final = 3
_NIGHTS_SHOWN: Final = 7
_READINGS_WINDOW: Final = timedelta(hours=6)
_GAP: Final = timedelta(minutes=30)
"""A stretch this long with no heart rate reading is named. The watch takes one about every four
and a half minutes while it is worn, asleep or awake; in his first five days a gap this long came
0 to 4 times a day."""
_LATE: Final = timedelta(minutes=90)
"""The phone sends heart rate at most hourly and the watch syncs to it first, so the newest
readings of a stretch that runs to now may still be on their way for this long."""
_FRESH: Final = timedelta(minutes=5)
_STILL_NOTE: Final = (
    "taken after he had been still a while; not his resting heart rate, which is"
    " resting_heart_rate"
)
_NEAREST_REACH: Final = timedelta(days=30)
_GAPS_SHOWN: Final = 6
_HOUR: Final = timedelta(hours=1)
_DAY_MS: Final = 24 * 3_600_000
_SOURCES: Final = ("Apple Health from his watch and phone",)
_HOW_MORE: Final = "ask for fewer measures, shorter hours or a longer step"
_CLOCK_24: Final = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_CLOCK_12: Final = re.compile(r"(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?")
_NOON: Final = 12
_STAGES: Final = (("deep", "deep"), ("core", "core"), ("rem", "REM"), ("awake", "awake"))
_LAST_NIGHT: Final = frozenset({"last night", "昨晚", "昨天晚上", "昨夜", "昨天夜里"})
_ENDS: Final = ".?!\u3002\uff1f\uff01 "
"""What may close his words for last night: the stops of either language."""


# --- the stretch asked for -------------------------------------------------------------------


@dataclass(frozen=True)
class _Window:
    """The days asked for and, for one day, its hours; ``until`` is None for the day's end.

    ``to_now`` is a window that runs to the call's clock: today, or a span that ends today.
    """

    first: date
    last: date
    start: time | None
    until: time | None
    to_now: bool
    now: datetime

    @property
    def one_day(self) -> bool:
        return self.first == self.last

    @property
    def whole_days(self) -> bool:
        return self.start is None and self.until is None

    def bounds(self, zone: tzinfo) -> tuple[datetime, datetime]:
        """Its start and end as local moments; a stretch to now ends at the call's clock."""
        begin = _at(self.first, self.start or time(0), zone)
        if self.to_now:
            return begin, self.now
        if self.until is not None:
            return begin, _at(self.last, self.until, zone)
        return begin, _midnight_after(self.last, zone)

    def same_hours(self, day: date, zone: tzinfo) -> tuple[datetime, datetime]:
        """The same clock hours on another day: what "usual" is measured over for one day."""
        begin = _at(day, self.start or time(0), zone)
        until = self.now.time() if self.to_now else self.until
        if until is None:
            return begin, _midnight_after(day, zone)
        return begin, _at(day, until, zone)

    def label(self) -> str:
        """``today (Fri) 2:00 pm to 5:36 pm, now``, ``yesterday (Thu)``, ``Mon Oct 20 to ...``."""
        today = self.now.date()
        if not self.one_day:
            return speakable.span(self.first, self.last, today) + (" so far" if self.to_now else "")
        name = speakable.day_name(self.first, today)
        if self.whole_days:
            return f"{name} so far, to {speakable.clock(self.now)}" if self.to_now else name
        begin = speakable.clock(datetime.combine(self.first, self.start or time(0)))
        if self.to_now:
            return f"{name} {begin} to {speakable.clock(self.now)}, now"
        if self.until is None:
            return f"{name} {begin} to midnight"
        return f"{name} {begin} to {speakable.clock(datetime.combine(self.first, self.until))}"


def _at(day: date, clock: time, zone: tzinfo) -> datetime:
    return datetime.combine(day, clock, zone)


def _midnight_after(day: date, zone: tzinfo) -> datetime:
    return _at(day + timedelta(days=1), time(0), zone)


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _clock_of(text: str, name: str) -> time:
    """``14:00``, ``2pm`` or ``2:30 pm`` as a time of day; else a ToolError saying how."""
    word = text.strip().lower()
    if found := _CLOCK_24.fullmatch(word):
        return time(int(found[1]), int(found[2]))
    if found := _CLOCK_12.fullmatch(word):
        hour = int(found[1]) % _NOON + (_NOON if found[3] == "p" else 0)
        return time(hour, int(found[2] or 0))
    msg = f"body: {name} is a time of day in his local time, like 14:00 or 2:30 pm, not {text!r}"
    raise ToolError(msg, code="invalid_arguments")


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        msg = f"body: {name} is text, like the examples in its description"
        raise ToolError(msg, code="invalid_arguments")
    return value.strip()


def _window(args: Mapping[str, Any], now: datetime, *, sleep_only: bool) -> _Window:
    """The stretch the arguments name, checked: never in the future, hours only of one day.

    Hours that run past now end now, and the label says so. Last night is the sleep he woke from
    today; for anything else it is two days' hours, so the error says how to ask.
    """
    today = now.date()
    words = _text(args, "when")
    if words.lower().rstrip(_ENDS) in _LAST_NIGHT:
        if not sleep_only:
            msg = (
                "body: last night spans two days; ask sleep with when=today (the day he woke),"
                " and other measures with when=yesterday, from=18:00"
            )
            raise ToolError(msg, code="invalid_arguments")
        words = ""
    first, last = (
        speakable.resolve_when(words, today, back=True, tool="body") if words else (today, today)
    )
    if first > today:
        msg = f"body: {words!r} has not happened yet; today is {today.isoformat()}"
        raise ToolError(msg, code="invalid_arguments")
    last = min(last, today)
    if (last - first).days + 1 > _SPAN_DAYS:
        msg = f"body: at most {_SPAN_DAYS} days at once; ask for a longer stretch in parts"
        raise ToolError(msg, code="invalid_arguments")
    begin_text, until_text = _text(args, "from"), _text(args, "until")
    if (begin_text or until_text) and first != last:
        msg = "body: from and until are hours of one day; for several days leave them out"
        raise ToolError(msg, code="invalid_arguments")
    begin = _clock_of(begin_text, "from") if begin_text else None
    is_today = last == today
    if until_text.lower() == "now" and not is_today:
        msg = "body: until=now is only for today; give a time of day for another day"
        raise ToolError(msg, code="invalid_arguments")
    until = None if until_text.lower() in ("", "now") else _clock_of(until_text, "until")
    if begin is not None and is_today and begin >= now.time():
        msg = f"body: {begin_text} today has not come yet; it is {speakable.clock(now)}"
        raise ToolError(msg, code="invalid_arguments")
    if begin is not None and until is not None and until <= begin:
        msg = f"body: until={until_text} is not after from={begin_text}"
        raise ToolError(msg, code="invalid_arguments")
    if is_today and until is not None and until >= now.time():
        until = None
    return _Window(first, last, begin, until, is_today and until is None, now)


def _measures(args: Mapping[str, Any]) -> tuple[list[_Measure], bool]:
    """The measures asked for, and whether none was (the overview)."""
    raw = args.get("measures")
    if raw is None or raw == []:
        return list(_MEASURES), True
    names = [raw] if isinstance(raw, str) else raw
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        msg = "body: measures is a list of names, like ['heart_rate', 'sleep']"
        raise ToolError(msg, code="invalid_arguments")
    unknown = [n for n in names if n not in _BY_NAME]
    if unknown:
        msg = f"body: no measure {unknown[0]!r}; the measures are {', '.join(_BY_NAME)}"
        raise ToolError(msg, code="invalid_arguments")
    return [_BY_NAME[n] for n in dict.fromkeys(names)], False


# --- numbers as she will say them ------------------------------------------------------------


def _bare(measure: _Measure, value: float) -> str:
    """``72``, ``97``, ``3.21``, ``4,321``: a value in the measure's unit, without the unit."""
    return f"{value * measure.scale:,.{measure.places}f}"


def _said(measure: _Measure, value: float) -> str:
    """``72 bpm``, ``97%``, ``3.21 km``, ``4,321 steps``: a value with its unit."""
    if measure.unit in ("", "%"):
        return f"{_bare(measure, value)}{measure.unit}"
    return f"{_bare(measure, value)} {measure.unit}"


def _number(measure: _Measure, value: float) -> float | int:
    scaled = round(value * measure.scale, measure.places)
    return int(scaled) if measure.places == 0 else scaled


def _local(at_ms: int, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(at_ms / 1000, zone)


def _clock_ms(at_ms: int, zone: tzinfo) -> str:
    return speakable.clock(_local(at_ms, zone))


def _when_ms(at_ms: int, now: datetime, zone: tzinfo) -> str:
    return speakable.when(_local(at_ms, zone), now, zone)


def _plural(count: int, word: str, many: str = "") -> str:
    return f"{count:,} {word if count == 1 else many or word + 's'}"


@dataclass(frozen=True)
class _Usual:
    """His usual level: the median and middle half of what the days before held."""

    median: float
    low: float
    high: float
    days: int

    @classmethod
    def of(cls, values: Sequence[float], days: int) -> _Usual:
        if len(values) == 1:
            return cls(values[0], values[0], values[0], days)
        low, mid, high = statistics.quantiles(values, n=4, method="inclusive")
        return cls(mid, low, high, days)

    def versus(self, value: float) -> str:
        """Where a value stands against the middle half: below, within or above it."""
        if value < self.low:
            return "below"
        if value > self.high:
            return "above"
        return "within"


# --- one read --------------------------------------------------------------------------------


class _Read:
    """What one call reads: the window, cut into stretches, and the rows it and its usual need.

    The rows are read once, from the first day the usual level looks at to the window's end, and
    every block slices them.
    """

    def __init__(
        self, conn: sqlite3.Connection, zone: tzinfo, window: _Window, step: str | None,
        measures: Sequence[_Measure],
    ) -> None:
        self.conn, self.zone, self.window = conn, zone, window
        begin, end = window.bounds(zone)
        self.start_ms, self.end_ms = _ms(begin), _ms(end)
        if not window.one_day or step == "day":
            self.minutes = _STEPS["day"]
        else:
            self.minutes = _STEPS[step] if step else _default_step(end - begin)
        self.stretches = _stretches(window, zone, self.minutes)
        self.usual = self._usual_spans()
        since = min(start for start, _end in self.usual)
        by_kind = {kind: [m.metric for m in measures if m.kind == kind] for kind in _KINDS}
        self._readings = body.readings(conn, by_kind["reading"], since, self.end_ms)
        self._readings_at = {k: [r.at_ms for r in v] for k, v in self._readings.items()}
        self._totals = body.totals(
            conn, by_kind["total"], self.hour_floor(since), self.end_ms,
        )
        spans = [m.metric for m in measures if m.kind not in ("reading", "total")]
        self._spans = body.span_events(conn, spans, since - _DAY_MS, self.end_ms) if spans else []
        self._sleeps: list[Span] | None = None

    @property
    def now(self) -> datetime:
        return self.window.now

    def _usual_spans(self) -> list[tuple[int, int]]:
        """The same hours on each of the 14 days before the window, or those whole days."""
        found = []
        for back in range(1, _USUAL_DAYS + 1):
            day = self.window.first - timedelta(days=back)
            if self.window.one_day:
                begin, end = self.window.same_hours(day, self.zone)
            else:
                begin, end = _at(day, time(0), self.zone), _midnight_after(day, self.zone)
            found.append((_ms(begin), _ms(end)))
        return found

    def hour_floor(self, at_ms: int) -> int:
        """The start of the local hour ``at_ms`` falls in: where Health's hourly totals start."""
        return _ms(_local(at_ms, self.zone).replace(minute=0, second=0, microsecond=0))

    def readings(self, metric: str, start_ms: int, end_ms: int) -> list[body.Reading]:
        held, at = self._readings[metric], self._readings_at[metric]
        return held[bisect.bisect_left(at, start_ms):bisect.bisect_left(at, end_ms)]

    def totals(self, metric: str, start_ms: int, end_ms: int) -> list[body.Total]:
        """The hourly totals of the hours that start in ``[start_ms, end_ms)``.

        The hour ``start_ms`` falls in counts whole: Health keeps no finer total.
        """
        floor = self.hour_floor(start_ms)
        return [t for t in self._totals[metric] if floor <= t.start_ms < end_ms]

    def spans(self, metric: str) -> list[Event]:
        return [e for e in self._spans if e.payload.get("metric") == metric]

    def sleeps(self) -> list[Span]:
        if self._sleeps is None:
            self._sleeps = sleeps(self.spans("sleep"))
        return self._sleeps


_KINDS: Final[tuple[Kind, ...]] = ("reading", "total", "sleep", "workout", "mindful", "stand_hours")


def _stretches(window: _Window, zone: tzinfo, minutes: int) -> list[tuple[int, int, str]]:
    """The window cut into stretches of ``minutes``, each ``(start ms, end ms, label)``.

    Days for a span of days, else clock stretches from the hour, the first and last cut to the
    window.
    """
    begin, end = window.bounds(zone)
    found: list[tuple[int, int, str]] = []
    if minutes == _STEPS["day"]:
        day = window.first
        while day <= window.last:
            start, stop = _at(day, time(0), zone), _midnight_after(day, zone)
            label = speakable.day_name(day, window.now.date())
            found.append((_ms(start), min(_ms(stop), _ms(end)), label))
            day += timedelta(days=1)
        return found
    at = begin.replace(minute=begin.minute - begin.minute % minutes, second=0, microsecond=0)
    width = timedelta(minutes=minutes)
    while at < end:
        start = max(at, begin)
        found.append((_ms(start), _ms(min(at + width, end)), speakable.clock(start)))
        at += width
    if len(found) > _MAX_STRETCHES:
        msg = (
            f"body: {minutes}-minute stretches cut these hours into {len(found)}; take a longer"
            " step or fewer hours"
        )
        raise ToolError(msg, code="invalid_arguments")
    return found


def _default_step(length: timedelta) -> int:
    if length <= timedelta(hours=3):
        return _STEPS["15min"]
    if length <= timedelta(hours=8):
        return _STEPS["30min"]
    return _STEPS["hour"]


def _usual_words(read: _Read) -> str:
    return "for the same hours" if read.window.one_day else "over the 14 days before"


def _lead(measure: _Measure, value: float) -> str:
    """``heart rate 72 bpm``, ``5,026 steps``: the measure and a value, its name said once."""
    said = _said(measure, value)
    return said if said.endswith(measure.label) else f"{measure.label} {said}"


def _nothing(read: _Read, measure: _Measure, *, nearest: bool) -> tuple[dict[str, Any], str]:
    """Nothing recorded: say so and, asked for in full, the nearest record either side.

    The nearest records tell a watch not worn from a measure it never took.
    """
    head = f"no {measure.label} recorded"
    if not nearest:
        return {"recorded": False}, head
    event = MEASURE_EVENT if measure.kind == "reading" else HEALTH_EVENT
    reach = int(_NEAREST_REACH.total_seconds() * 1000)
    found: dict[str, str] = {}
    sides = [("before", body.last_before, read.start_ms)]
    if not read.window.to_now:
        sides.append(("after", body.first_after, read.end_ms))
    for side, look, at_ms in sides:
        near = look(read.conn, event, measure.metric, at_ms, reach)
        found[side] = (
            f"none in the 30 days {side}" if near is None else _when_ms(near, read.now, read.zone)
        )
    before = found["before"]
    head += ", nor in the 30 days before" if before.startswith("none") else (
        f"; the last before was {before}"
    )
    return {"recorded": False, "nearest": found}, head


# --- reading measures ------------------------------------------------------------------------


def _usual_readings(
    read: _Read, measure: _Measure,
) -> tuple[_Usual | None, _Usual | None, int]:
    """His usual readings for the window's hours, all and while still, and the days that had any."""
    pooled: list[float] = []
    still: list[float] = []
    days = 0
    for start, end in read.usual:
        found = read.readings(measure.metric, start, end)
        days += bool(found)
        pooled += [r.value for r in found]
        still += [r.value for r in found if r.at_rest]
    if days < _USUAL_MIN_DAYS:
        return None, None, days
    return _Usual.of(pooled, days), _Usual.of(still, days) if still else None, days


def _readings_block(
    read: _Read, measure: _Measure, *, full: bool, listing: bool,
) -> tuple[dict[str, Any], str]:
    """A reading measure over the window: its block and its headline."""
    found = read.readings(measure.metric, read.start_ms, read.end_ms)
    if not found:
        return _nothing(read, measure, nearest=full)
    values = [r.value for r in found]
    median = statistics.median(values)
    block: dict[str, Any] = {
        "readings": len(found),
        "median": _number(measure, median),
        "low": _number(measure, min(values)),
        "high": _number(measure, max(values)),
        "unit": measure.unit,
    }
    if len(found) == 1:
        at_ms = found[0].at_ms
        at = (
            _clock_ms(at_ms, read.zone) if read.window.one_day
            else _when_ms(at_ms, read.now, read.zone)
        )
        head = f"{_lead(measure, median)}, one reading, at {at}"
    else:
        head = (
            f"{measure.label} median {_said(measure, median)}, {_bare(measure, min(values))} to"
            f" {_bare(measure, max(values))} over {_plural(len(found), 'reading')}"
        )
    head += _beside_usual(read, measure, found, block)
    if read.window.one_day and len(found) > 1:
        block["latest"] = f"{_clock_ms(found[-1].at_ms, read.zone)}, {_said(measure, values[-1])}"
    if measure.name == "heart_rate":
        head += _no_readings(read, found, block)
    if full:
        block["by"] = _reading_rows(read, measure, found)
    if listing:
        block["each"] = [_reading_row(read, measure, r) for r in found]
    return block, head


def _beside_usual(
    read: _Read, measure: _Measure, found: Sequence[body.Reading], block: dict[str, Any],
) -> str:
    """Put the readings while still and his usual level into ``block``; what the headline adds.

    Health's "sedentary" readings are said as "while still", never "at rest": his resting heart
    rate is its own measure, one a day and lower, and the two were said as one.
    """
    median = statistics.median(r.value for r in found)
    still = [r.value for r in found if r.at_rest]
    if still:
        block["while_still"] = {
            "readings": len(still),
            "median": _number(measure, statistics.median(still)),
            "note": _STILL_NOTE,
        }
    usual, usual_still, days = _usual_readings(read, measure)
    if usual is None:
        block["usual"] = f"only {days} of the 14 days before have readings, too few to say"
        return ""
    block["usual"] = {
        "median": _number(measure, usual.median),
        "middle_half": f"{_bare(measure, usual.low)} to {_said(measure, usual.high)}",
        "days": days,
    }
    block["vs_usual"] = usual.versus(median)
    head = (
        f"; usual {_said(measure, usual.median)} {_usual_words(read)} (middle half"
        f" {_bare(measure, usual.low)} to {_bare(measure, usual.high)}), so"
        f" {block['vs_usual']} it"
    )
    if usual_still is None:
        return head
    block["usual"]["while_still_median"] = _number(measure, usual_still.median)
    if still:
        still_median = statistics.median(still)
        block["while_still"]["vs_usual"] = usual_still.versus(still_median)
        head += (
            f"; while still {_bare(measure, still_median)} against usual"
            f" {_bare(measure, usual_still.median)}, {block['while_still']['vs_usual']}"
        )
    return head


def _no_readings(read: _Read, found: Sequence[body.Reading], block: dict[str, Any]) -> str:
    """Put the stretches without heart rate into ``block``; what the headline adds."""
    holes, late = _holes(read, found)
    if late:
        block["still_arriving"] = late
    if not holes:
        return ""
    block["no_readings"] = [_hole(read, a, b, charge=True) for a, b in holes[:_GAPS_SHOWN]]
    if len(holes) > _GAPS_SHOWN:
        block["no_readings"].append(f"{len(holes) - _GAPS_SHOWN} more")
    head = f"; none from {_hole(read, *holes[0], charge=False)}"
    if len(holes) > 1:
        head += f" and {_plural(len(holes) - 1, 'other stretch', 'other stretches')}"
    return head


def _reading_row(read: _Read, measure: _Measure, reading: body.Reading) -> str:
    still = {True: " still", False: " moving", None: ""}[reading.at_rest]
    return f"{_clock_ms(reading.at_ms, read.zone)} {_said(measure, reading.value)}{still}"


def _reading_rows(read: _Read, measure: _Measure, found: Sequence[body.Reading]) -> list[str]:
    rows = []
    for start, end, label in read.stretches:
        values = [r.value for r in found if start <= r.at_ms < end]
        if not values:
            continue
        spread = (
            f" ({_bare(measure, min(values))} to {_bare(measure, max(values))}, {len(values)})"
            if len(values) > 1 else ""
        )
        rows.append(f"{label}: {_said(measure, statistics.median(values))}{spread}")
    return rows


def _holes(read: _Read, found: Sequence[body.Reading]) -> tuple[list[tuple[int, int]], str]:
    """The stretches of the window with no heart rate, and a note when the newest may be late.

    A window that runs to now may still be waiting for its newest readings (:data:`_LATE`).
    """
    gap_ms = int(_GAP.total_seconds() * 1000)
    edges = [read.start_ms, *(r.at_ms for r in found)]
    holes = [(a, b) for a, b in pairwise(edges) if b - a >= gap_ms]
    tail = read.end_ms - found[-1].at_ms
    late = ""
    if read.window.to_now and tail < _LATE.total_seconds() * 1000:
        if tail >= _FRESH.total_seconds() * 1000:
            late = (
                f"the newest reading is {_clock_ms(found[-1].at_ms, read.zone)}; heart rate"
                " reaches her up to about an hour after the watch takes it"
            )
    elif tail >= gap_ms:
        holes.append((found[-1].at_ms, read.end_ms))
    return holes, late


def _hole(read: _Read, begin: int, end: int, *, charge: bool) -> str:
    """``2:05 pm to 5:58 pm``, with the watch's charge readings inside it when ``charge``."""
    to = "now" if end == read.end_ms and read.window.to_now else _clock_ms(end, read.zone)
    if read.window.one_day:
        line = f"{_clock_ms(begin, read.zone)} to {to}"
    else:
        line = f"{_when_ms(begin, read.now, read.zone)} to {to}"
    held = body.watch_charge(read.conn, begin, end) if charge else []
    if held:
        states = list(dict.fromkeys(c.state for c in held if c.state))
        line += (
            f" (the watch said {' then '.join(states) or 'its charge'}, {held[0].percent}%"
            f" at {_clock_ms(held[0].at_ms, read.zone)})"
        )
    return line


# --- totals ----------------------------------------------------------------------------------


def _complete_days(read: _Read) -> list[tuple[int, int]]:
    """The window's days that are over: a span that runs to now leaves today out."""
    found = []
    day = read.window.first
    while day <= read.window.last:
        if not (read.window.to_now and day == read.now.date()):
            found.append((_ms(_at(day, time(0), read.zone)), _ms(_midnight_after(day, read.zone))))
        day += timedelta(days=1)
    return found


def _totals_block(read: _Read, measure: _Measure, *, full: bool) -> tuple[dict[str, Any], str]:
    """A cumulative measure over the window: its hourly totals summed, beside his usual sum."""
    found = read.totals(measure.metric, read.start_ms, read.end_ms)
    if not found:
        return _nothing(read, measure, nearest=full)
    total = sum(t.value for t in found)
    block: dict[str, Any] = {"total": _number(measure, total), "unit": measure.unit}
    head = _lead(measure, total)
    if read.hour_floor(read.start_ms) != read.start_ms:
        block["note"] = "Health keeps these by the hour, so the hour it starts in counts whole"
    mine = total
    if not read.window.one_day:
        sums = [
            sum(t.value for t in read.totals(measure.metric, a, b)) for a, b in _complete_days(read)
        ]
        held = [one for one in sums if one]
        if held:
            mine = statistics.mean(held)
            block["per_day"] = _number(measure, mine)
            block["full_days"] = len(held)
            head += (
                f" in all; {_said(measure, mine)} a day over"
                f" {_plural(len(held), 'full day')}"
            )
    usual_sums = [
        _sum_by(read, held, b)
        for held, b in ((read.totals(measure.metric, a, b), b) for a, b in read.usual) if held
    ]
    if len(usual_sums) < _USUAL_MIN_DAYS:
        block["usual"] = f"only {len(usual_sums)} of the 14 days before have it, too few to say"
    else:
        usual = _Usual.of(usual_sums, len(usual_sums))
        block["usual"] = {
            "median": _number(measure, usual.median),
            "middle_half": f"{_bare(measure, usual.low)} to {_said(measure, usual.high)}",
            "days": usual.days,
        }
        block["vs_usual"] = usual.versus(mine)
        by = " by this time" if read.window.to_now and read.window.one_day else ""
        head += (
            f"; usual{by} {_said(measure, usual.median)}"
            f"{' a day' if not read.window.one_day else ''}, so {block['vs_usual']} it"
        )
    if full:
        block["by"] = _total_rows(read, measure, found)
    return block, head


def _sum_by(read: _Read, held: Sequence[body.Total], end_ms: int) -> float:
    """A day's totals summed to ``end_ms``, for his usual level.

    To now, the hour ``end_ms`` falls in counts only the part of it that had passed, since
    today's own hour is still filling.
    """
    if not read.window.to_now:
        return sum(t.value for t in held)
    passed = (end_ms - read.hour_floor(end_ms)) / (_HOUR.total_seconds() * 1000)
    return sum(t.value * (passed if t.start_ms <= end_ms < t.end_ms else 1) for t in held)


def _total_rows(read: _Read, measure: _Measure, found: Sequence[body.Total]) -> list[str]:
    """The totals by stretch; by the hour when the stretches are shorter, as Health keeps them."""
    if read.minutes < _STEPS["hour"]:
        return [
            f"{_clock_ms(t.start_ms, read.zone)}: {_said(measure, t.value)}" for t in found
            if t.value
        ]
    rows = []
    for start, end, label in read.stretches:
        part = sum(t.value for t in found if read.hour_floor(start) <= t.start_ms < end)
        if part:
            rows.append(f"{label}: {_said(measure, part)}")
    return rows


# --- sleep -----------------------------------------------------------------------------------


def _stage_minutes(stages: Sequence[Mapping[str, Any]], start: int, end: int) -> dict[str, int]:
    """Minutes in each stage inside ``[start, end)``, time two sources both recorded once."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for one in stages:
        a, b = max(int(one["start_ms"]), start), min(int(one["end_ms"]), end)
        if b > a:
            spans.setdefault(str(one["stage"]), []).append((a, b))
    minutes = {}
    for stage, held in spans.items():
        total, reach = 0, 0
        for a, b in sorted(held):
            total += max(0, b - max(a, reach))
            reach = max(reach, b)
        minutes[stage] = round(total / 60_000)
    return minutes


def _asleep(block: Span) -> float:
    return float(block.fields["minutes"])


def _night(block: Span, read: _Read) -> dict[str, Any]:
    stages = block.fields["stages"]
    end = block.end or block.start
    minutes = _stage_minutes(stages, block.start, end)
    night: dict[str, Any] = {
        "asleep": speakable.duration(_asleep(block) * 60),
        "from": _when_ms(block.start, read.now, read.zone),
        "to": _when_ms(end, read.now, read.zone),
    }
    staged = {
        name: speakable.duration(minutes[key] * 60) for key, name in _STAGES if minutes.get(key)
    }
    if staged:
        night["stages"] = staged
    if woke := sum(1 for s in stages if s["stage"] == "awake"):
        night["woke"] = woke
    return night


def _woke_in(read: _Read, start_ms: int, end_ms: int) -> list[Span]:
    return [b for b in read.sleeps() if b.end is not None and start_ms <= b.end < end_ms]


def _sleep_block(read: _Read, measure: _Measure, *, full: bool) -> tuple[dict[str, Any], str]:
    """His sleep, beside the main night he usually has.

    The nights he woke from on the days asked for, or the sleep that overlaps the hours asked for.
    """
    if read.window.whole_days:
        stop = _ms(_midnight_after(read.window.last, read.zone))
        found = _woke_in(read, read.start_ms, stop)
    else:
        found = [
            b for b in read.sleeps()
            if b.start < read.end_ms and (b.end or b.start) > read.start_ms
        ]
    if not found:
        return _nothing(read, measure, nearest=full)
    block: dict[str, Any] = {"nights": [_night(b, read) for b in found[-_NIGHTS_SHOWN:]]}
    if len(found) > _NIGHTS_SHOWN:
        block["earlier_nights"] = len(found) - _NIGHTS_SHOWN
    mains = _mains(found, read.zone)
    if read.window.one_day:
        main = mains[0]
        mine = _asleep(main)
        head = (
            f"slept {speakable.duration(mine * 60)}, {_clock_ms(main.start, read.zone)} to"
            f" {_clock_ms(main.end or main.start, read.zone)}"
        )
        if len(found) > 1:
            head += f", plus {_plural(len(found) - 1, 'shorter sleep')}"
    else:
        mine = statistics.mean(_asleep(b) for b in mains)
        short, long = min(mains, key=_asleep), max(mains, key=_asleep)
        block["average"] = speakable.duration(mine * 60)
        block["shortest"] = f"{speakable.duration(_asleep(short) * 60)}, woke {_woke(short, read)}"
        block["longest"] = f"{speakable.duration(_asleep(long) * 60)}, woke {_woke(long, read)}"
        head = (
            f"slept {speakable.duration(mine * 60)} a night on average over"
            f" {_plural(len(mains), 'night')}, {speakable.duration(_asleep(short) * 60)} to"
            f" {speakable.duration(_asleep(long) * 60)}"
        )
        head += _nightless(read, mains, block)
    usual_nights = [
        max(_asleep(b) for b in held)
        for held in (_woke_in(read, a, b) for a, b in _usual_whole_days(read)) if held
    ]
    if len(usual_nights) < _USUAL_MIN_DAYS:
        block["usual"] = f"only {len(usual_nights)} nights in the 14 days before, too few to say"
    else:
        usual = _Usual.of(usual_nights, len(usual_nights))
        block["usual"] = {
            "asleep": speakable.duration(usual.median * 60),
            "middle_half": (
                f"{speakable.duration(usual.low * 60)} to {speakable.duration(usual.high * 60)}"
            ),
            "nights": usual.days,
        }
        block["vs_usual"] = usual.versus(mine)
        head += f"; usual {speakable.duration(usual.median * 60)}, so {block['vs_usual']} it"
    return block, head


def _mains(found: Sequence[Span], zone: tzinfo) -> list[Span]:
    """The longest sleep he woke from on each day, longest first for one day."""
    by_day: dict[date, Span] = {}
    for one in found:
        day = _local(one.end or one.start, zone).date()
        if day not in by_day or _asleep(one) > _asleep(by_day[day]):
            by_day[day] = one
    return sorted(by_day.values(), key=_asleep, reverse=True)


def _nightless(read: _Read, mains: Sequence[Span], block: dict[str, Any]) -> str:
    """Name the days of a span he woke from no sleep on; what the headline adds.

    Today is left out: he may not have slept yet. Unnamed, a span's first day was taken for the
    first day his sleep was recorded.
    """
    today = read.now.date()
    held = {_local(b.end or b.start, read.zone).date() for b in mains}
    span = (read.window.last - read.window.first).days + 1
    days = [read.window.first + timedelta(days=n) for n in range(span)]
    missing = [d for d in days if d not in held and d < today]
    if not missing:
        return ""
    block["no_sleep_recorded"] = [speakable.day_name(d, today) for d in missing]
    return f"; no sleep recorded on {len(missing)} of the {span} days"


def _woke(block: Span, read: _Read) -> str:
    return _when_ms(block.end or block.start, read.now, read.zone)


def _usual_whole_days(read: _Read) -> list[tuple[int, int]]:
    """The 14 whole days before the window: a night is his whatever hours were asked."""
    found = []
    for back in range(1, _USUAL_DAYS + 1):
        day = read.window.first - timedelta(days=back)
        found.append((_ms(_at(day, time(0), read.zone)), _ms(_midnight_after(day, read.zone))))
    return found


# --- workouts, mindful sessions, stand hours -------------------------------------------------


def _overlapping(read: _Read, spans: Sequence[Span]) -> list[Span]:
    return [s for s in spans if s.start < read.end_ms and (s.end or s.start) > read.start_ms]


def _workouts_block(read: _Read, measure: _Measure, *, full: bool) -> tuple[dict[str, Any], str]:
    found = _overlapping(read, workouts(read.spans("workout")))
    if not found:
        return _nothing(read, measure, nearest=full)
    rows = []
    for one in found:
        seconds = ((one.end or one.start) - one.start) / 1000
        row = (
            f"{one.fields['activity']}, {_when_ms(one.start, read.now, read.zone)},"
            f" {speakable.duration(seconds)}"
        )
        if "energy_kcal" in one.fields:
            row += f", {round(one.fields['energy_kcal'])} kcal"
        if "distance_m" in one.fields:
            row += f", {one.fields['distance_m'] / 1000:.2f} km"
        if "avg_hr" in one.fields:
            row += f", average heart rate {round(one.fields['avg_hr'])} bpm"
        rows.append(row)
    seconds = sum(((w.end or w.start) - w.start) / 1000 for w in found)
    kinds = ", ".join(dict.fromkeys(str(w.fields["activity"]) for w in found))
    head = f"{_plural(len(found), 'workout')}, {speakable.duration(seconds)}: {kinds}"
    return {"workouts": rows}, head


def _mindful_block(read: _Read, measure: _Measure, *, full: bool) -> tuple[dict[str, Any], str]:
    found = [
        e for e in read.spans("mindful_session")
        if int(e.payload["start_ms"]) < read.end_ms and int(e.payload["end_ms"]) > read.start_ms
    ]
    if not found:
        return _nothing(read, measure, nearest=full)
    seconds = sum((int(e.payload["end_ms"]) - int(e.payload["start_ms"])) / 1000 for e in found)
    head = f"{_plural(len(found), 'mindful session')}, {speakable.duration(seconds)}"
    return {"sessions": len(found), "minutes": round(seconds / 60)}, head


def _stood(read: _Read, start_ms: int, end_ms: int) -> dict[int, bool]:
    """Each hour Health marked in ``[start_ms, end_ms)``: whether he stood in it."""
    return {
        int(e.payload["start_ms"]): e.payload.get("detail") == "stood"
        for e in read.spans("stand_hour") if start_ms <= int(e.payload["start_ms"]) < end_ms
    }


def _stand_block(read: _Read, measure: _Measure, *, full: bool) -> tuple[dict[str, Any], str]:
    hours = _stood(read, read.hour_floor(read.start_ms), read.end_ms)
    if not hours:
        return _nothing(read, measure, nearest=full)
    stood = sum(hours.values())
    block: dict[str, Any] = {"stood": stood, "hours_marked": len(hours)}
    head = f"stood in {stood} of {len(hours)} hours"
    if not read.window.one_day:
        return block, head
    usual_counts = [
        sum(held.values())
        for held in (_stood(read, read.hour_floor(a), b) for a, b in read.usual) if held
    ]
    if len(usual_counts) >= _USUAL_MIN_DAYS:
        usual = _Usual.of(usual_counts, len(usual_counts))
        block["usual"] = {"stood": round(usual.median), "days": usual.days}
        block["vs_usual"] = usual.versus(stood)
        head += f"; usual {round(usual.median)} {_usual_words(read)}, so {block['vs_usual']} it"
    return block, head


# --- the answer ------------------------------------------------------------------------------


def _block(
    read: _Read, measure: _Measure, *, full: bool, listing: bool,
) -> tuple[dict[str, Any], str]:
    readers: dict[Kind, Callable[[], tuple[dict[str, Any], str]]] = {
        "reading": lambda: _readings_block(read, measure, full=full, listing=listing),
        "total": lambda: _totals_block(read, measure, full=full),
        "sleep": lambda: _sleep_block(read, measure, full=full),
        "workout": lambda: _workouts_block(read, measure, full=full),
        "mindful": lambda: _mindful_block(read, measure, full=full),
        "stand_hours": lambda: _stand_block(read, measure, full=full),
    }
    return readers[measure.kind]()


def _fit(answer: dict[str, Any]) -> int:
    """Keep the answer within the budget, a row at a time from the longest list.

    A list loses its earliest row, since the latest is what he most often means; the overview's
    lines go from the end, where the measures he asks about least are.
    """
    dropped = 0
    while len(encoded(answer)) > _BUDGET:
        rows = [held for held in _lists(answer) if len(held) > 1]
        if not rows:
            break
        longest = max(rows, key=lambda held: len(encoded(held)))
        if isinstance(longest, dict):
            longest.popitem()
        else:
            longest.pop(0)
        dropped += 1
    return dropped


def _lists(value: object) -> list[list[Any] | dict[str, Any]]:
    """The answer's lists, and the overview's lines, which are a mapping of measure to line."""
    found: list[list[Any] | dict[str, Any]] = []
    if isinstance(value, dict):
        lines = value.get("measures")
        if isinstance(lines, dict):
            found.append(lines)
        for inner in value.values():
            if inner is not lines:
                found += _lists(inner)
    elif isinstance(value, list):
        found.append(value)
    return found


def _summary(label: str, heads: Sequence[str]) -> str:
    """One speakable line: the window, then as many headlines as fit."""
    text = f"{label[0].upper()}{label[1:]}: "
    shown = 0
    for head in heads:
        part = ("; " if shown else "") + head
        if len(text) + len(part) + len("; 99 more below.") > _SUMMARY_CHARS:
            break
        text += part
        shown += 1
    if shown < len(heads):
        text += f"{'; ' if shown else ''}{len(heads) - shown} more below"
    return text.replace("{", "(").replace("}", ")") + "."


class _Body:
    """The tool's handler: one read of his body over the stretch the arguments name."""

    def __init__(self, event_log: Path, zone: tzinfo) -> None:
        self._event_log = event_log
        self._zone = zone

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = (
                f"body: no argument {unknown[0]!r}; the arguments are measures, when, from,"
                " until, step and detail"
            )
            raise ToolError(msg, code="invalid_arguments")
        now = ctx.clock.now().astimezone(self._zone)
        measures, overview = _measures(args)
        window = _window(args, now, sleep_only=[m.kind for m in measures] == ["sleep"])
        step = _text(args, "step") or None
        detail = _text(args, "detail") or "summary"
        if step is not None and step not in _STEPS:
            msg = f"body: step is one of {', '.join(_STEPS)}"
            raise ToolError(msg, code="invalid_arguments")
        if step == "day" and window.one_day:
            msg = "body: a day step is for several days; for one day take hour or shorter"
            raise ToolError(msg, code="invalid_arguments")
        if detail not in _DETAILS:
            msg = f"body: detail is one of {', '.join(_DETAILS)}"
            raise ToolError(msg, code="invalid_arguments")
        listing = detail == "readings"
        if listing:
            begin, end = window.bounds(self._zone)
            if overview or len(measures) != 1 or measures[0].kind != "reading":
                msg = "body: detail=readings lists one reading measure, like heart_rate"
                raise ToolError(msg, code="invalid_arguments")
            if end - begin > _READINGS_WINDOW:
                msg = "body: detail=readings covers at most six hours; give from and until"
                raise ToolError(msg, code="invalid_arguments")
        try:
            with closing(open_runtime_event_log(self._event_log)) as conn:
                read = _Read(conn, self._zone, window, step, measures)
                answer = _answer(read, measures, overview=overview, listing=listing)
        except sqlite3.Error as exc:
            msg = f"His health records cannot be read just now ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc
        dropped = _fit(answer)
        if dropped:
            answer["cut"] = f"{dropped} rows left out to fit, the earliest first; {_HOW_MORE}"
        label = str(answer["window"])
        names = "all his measures" if overview else "his " + ", ".join(m.label for m in measures)
        line = f"read {names}, {label}" + (f", {dropped} rows left out" if dropped else "")
        shown = len(answer["measures"]) if "measures" in answer else len(measures)
        return Receipted(answer, Receipt(
            "done", reads_only=True, line=line,
            covered=Covered(_SOURCES, label, shown, dropped, _HOW_MORE if dropped else ""),
        ))


def _answer(
    read: _Read, measures: Sequence[_Measure], *, overview: bool, listing: bool,
) -> dict[str, Any]:
    """One line on each measure for the overview or many measures; else a block on each."""
    label = read.window.label()
    if overview or len(measures) > _WITH_STRETCHES:
        lines: dict[str, str] = {}
        empty = []
        for measure in measures:
            block, head = _block(read, measure, full=False, listing=False)
            if block.get("recorded") is False:
                empty.append(measure.name)
            else:
                lines[measure.name] = head
        summary = _summary(label, list(lines.values())) if lines else (
            f"No health data recorded {label}."
        )
        answer: dict[str, Any] = {"summary": summary, "window": label, "measures": lines}
        if empty:
            answer["not_recorded"] = empty
        return answer
    answer = {"summary": "", "window": label}
    heads = []
    for measure in measures:
        block, head = _block(read, measure, full=True, listing=listing)
        answer[measure.name] = block
        heads.append(head)
    answer["summary"] = _summary(label, heads)
    return answer


def build_body_tool(event_log: Path, zone: tzinfo) -> tuple[Tool, ...]:
    """``body`` over the event log at ``event_log``, its days cut in ``zone``."""
    handler = _Body(event_log, zone)

    def run(args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        return handler.run(args, ctx)

    return (
        Tool(
            name="body",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_body_tool"]
