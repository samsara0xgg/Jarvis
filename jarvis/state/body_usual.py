"""ADR 0411: a few measures of his body over a window, each beside his usual level then.

``GET /inherent/body`` serves this for the phone's 「跟平时比」 on a stretch of his day. Every
measure is read from the rows the ``body`` tool reads (:mod:`jarvis.state.body`) and set against
the same usual level (:class:`jarvis.state.body.Usual`): the same clock hours on each of the
:data:`~jarvis.state.body.USUAL_DAYS` days before, when at least
:data:`~jarvis.state.body.USUAL_MIN_DAYS` of them hold any.

- A reading measure is the median of the readings taken in the window, with their count, low and
  high; its usual level pools the readings of those hours.
- A total (steps) is the sum of Health's hourly totals, every hour the window touches counted
  whole as Health keeps them; for a window that runs to now, the usual sum counts the hour now
  falls in only as far as it has passed.
- Sleep is the minutes asleep of the sleeps he woke from in the window: sleep belongs to when he
  woke, as on his day line.

Layer rules: stdlib, :mod:`jarvis.shared` and other :mod:`jarvis.state` modules only (L2).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.state import body
from jarvis.state.phone_day import sleeps

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import tzinfo

    from jarvis.state.phone_day import Span

_HOUR_MS: Final = 3_600_000
MAX_WINDOW_MS: Final = 25 * _HOUR_MS
"""A window is at most a day, the day the clocks go back included."""
BASELINE: Final = {
    "days_before": body.USUAL_DAYS,
    "min_days": body.USUAL_MIN_DAYS,
    "rule": (
        f"the same clock hours on each of the {body.USUAL_DAYS} days before; his usual level is"
        " the median of what those days held, with its middle half (the 25th to the 75th"
        f" percentile), when at least {body.USUAL_MIN_DAYS} of them hold any"
    ),
}
"""How every measure's usual level is read, as the route says it."""


@dataclass(frozen=True)
class _Measure:
    """One measure the route serves: its name (the ``body`` tool's), Health's metric, its unit."""

    name: str
    metric: str
    kind: Literal["reading", "total", "sleep"]
    unit: str
    scale: float = 1.0
    places: int = 0


MEASURES: Final = (
    _Measure("steps", "steps", "total", "steps"),
    _Measure("heart_rate", "heart_rate", "reading", "bpm"),
    _Measure("resting_heart_rate", "resting_heart_rate", "reading", "bpm"),
    _Measure("hrv", "heart_rate_variability_sdnn", "reading", "ms"),
    _Measure("breathing_rate", "respiratory_rate", "reading", "/min", places=1),
    _Measure("blood_oxygen", "oxygen_saturation", "reading", "%", scale=100),
    _Measure("sleep", "sleep", "sleep", "min"),
)


def beside_usual(
    conn: sqlite3.Connection, zone: tzinfo, start_ms: int, end_ms: int, now_ms: int,
) -> dict[str, Any]:
    """``{start_ms, end_ms, now_ms, baseline, measures}`` for ``[start_ms, end_ms)``.

    A window that runs past now ends now. Each of :data:`MEASURES` is ``{kind, unit, value,
    usual: {median, low, high, days}, vs_usual}``: ``value`` is None when nothing was recorded
    in the window, the usual numbers are None when too few days hold any, and ``vs_usual``
    (``below``, ``within`` or ``above`` the middle half) is None without both. A reading measure
    adds ``readings``, ``low`` and ``high``; sleep adds ``sleeps``.

    Raises:
        ValueError: the window is empty, has not begun, or is longer than a day.
    """
    if end_ms <= start_ms:
        msg = "end_ms must be after start_ms"
        raise ValueError(msg)
    if start_ms >= now_ms:
        msg = "the window has not begun yet"
        raise ValueError(msg)
    if end_ms - start_ms > MAX_WINDOW_MS:
        msg = "a window is at most 25 hours"
        raise ValueError(msg)
    to_now, end_ms = end_ms >= now_ms, min(end_ms, now_ms)
    before = _same_hours(start_ms, end_ms, zone)
    since = before[-1][0]
    found = body.readings(
        conn, [m.metric for m in MEASURES if m.kind == "reading"], since, end_ms,
    )
    totals = body.totals(
        conn, [m.metric for m in MEASURES if m.kind == "total"], _hour_floor(since, zone), end_ms,
    )
    nights = sleeps(body.span_events(conn, ["sleep"], since - body.SPAN_REACH_MS, end_ms))
    measures: dict[str, dict[str, Any]] = {}
    for measure in MEASURES:
        if measure.kind == "reading":
            served = _readings(measure, found[measure.metric], start_ms, end_ms, before)
        elif measure.kind == "total":
            held = totals[measure.metric]
            served = _beside(
                measure, _summed(held, start_ms, end_ms, zone, prorate=False),
                [_summed(held, a, b, zone, prorate=to_now) for a, b in before],
            )
        else:
            woke = _woke(nights, start_ms, end_ms)
            served = {
                **_beside(
                    measure, _asleep(woke), [_asleep(_woke(nights, a, b)) for a, b in before],
                ),
                "sleeps": len(woke),
            }
        measures[measure.name] = served
    return {
        "start_ms": start_ms, "end_ms": end_ms, "now_ms": now_ms, "baseline": BASELINE,
        "measures": measures,
    }


def _same_hours(start_ms: int, end_ms: int, zone: tzinfo) -> list[tuple[int, int]]:
    """The window's clock hours on each of the days before, the nearest day first."""
    start, end = _local(start_ms, zone), _local(end_ms, zone)
    return [
        (_ms(start - timedelta(days=back)), _ms(end - timedelta(days=back)))
        for back in range(1, body.USUAL_DAYS + 1)
    ]


def _readings(
    measure: _Measure, held: Sequence[body.Reading], start_ms: int, end_ms: int,
    before: Sequence[tuple[int, int]],
) -> dict[str, Any]:
    values = [r.value for r in held if start_ms <= r.at_ms < end_ms]
    pooled: list[float] = []
    days = 0
    for a, b in before:
        then = [r.value for r in held if a <= r.at_ms < b]
        days += bool(then)
        pooled += then
    usual = body.Usual.of(pooled, days) if days >= body.USUAL_MIN_DAYS else None
    median = statistics.median(values) if values else None
    return {
        **_served(measure, median, usual, days),
        "readings": len(values),
        "low": _number(measure, min(values)) if values else None,
        "high": _number(measure, max(values)) if values else None,
    }


def _summed(
    held: Sequence[body.Total], start_ms: int, end_ms: int, zone: tzinfo, *, prorate: bool,
) -> float | None:
    """The hourly totals of the hours ``[start_ms, end_ms)`` touches, summed; None without any.

    With ``prorate`` the hour ``end_ms`` falls in counts only as far as it has passed.
    """
    floor = _hour_floor(start_ms, zone)
    hours = [t for t in held if floor <= t.start_ms < end_ms]
    if not hours:
        return None
    passed = (end_ms - _hour_floor(end_ms, zone)) / _HOUR_MS
    return sum(
        t.value * (passed if prorate and t.start_ms <= end_ms < t.end_ms else 1) for t in hours
    )


def _woke(nights: Sequence[Span], start_ms: int, end_ms: int) -> list[Span]:
    """The sleeps he woke from in ``[start_ms, end_ms)``."""
    return [n for n in nights if n.end is not None and start_ms <= n.end < end_ms]


def _asleep(held: Sequence[Span]) -> float | None:
    """The minutes asleep in ``held``; None when he woke from no sleep."""
    return sum(float(n.fields["minutes"]) for n in held) if held else None


def _beside(
    measure: _Measure, value: float | None, then: Sequence[float | None],
) -> dict[str, Any]:
    """A measure whose usual level is one number a day: the days that hold one."""
    held = [one for one in then if one is not None]
    usual = body.Usual.of(held, len(held)) if len(held) >= body.USUAL_MIN_DAYS else None
    return _served(measure, value, usual, len(held))


def _served(
    measure: _Measure, value: float | None, usual: body.Usual | None, days: int,
) -> dict[str, Any]:
    level = {"median": None, "low": None, "high": None} if usual is None else {
        "median": _number(measure, usual.median),
        "low": _number(measure, usual.low),
        "high": _number(measure, usual.high),
    }
    return {
        "kind": measure.kind,
        "unit": measure.unit,
        "value": None if value is None else _number(measure, value),
        "usual": {**level, "days": days},
        "vs_usual": None if value is None or usual is None else usual.versus(value),
    }


def _number(measure: _Measure, value: float) -> float | int:
    scaled = round(value * measure.scale, measure.places)
    return int(scaled) if measure.places == 0 else scaled


def _local(at_ms: int, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(at_ms / 1000, zone)


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _hour_floor(at_ms: int, zone: tzinfo) -> int:
    """The start of the local hour ``at_ms`` falls in: where Health's hourly totals start."""
    return _ms(_local(at_ms, zone).replace(minute=0, second=0, microsecond=0))


__all__ = ["BASELINE", "MAX_WINDOW_MS", "MEASURES", "beside_usual"]
