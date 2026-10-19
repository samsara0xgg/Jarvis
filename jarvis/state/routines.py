"""His routines, counted on read from what he did (ADR 0279).

Nothing is stored: every read folds the phone's stays and sleep, his rides and his kept places
over the last :data:`WINDOW_DAYS` days, and offers a routine only once it held for
:data:`MIN_WEEKS` distinct weeks and :data:`MIN_SHARE` of the weeks the source saw. A routine is a
default for planning a day; a commitment that day wins over it.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.state import phone_day, rides
from jarvis.state.places import containing

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence
    from datetime import tzinfo

    from jarvis.state.places import Place

KINDS: Final[tuple[str, ...]] = ("class", "ride", "leave_home", "meal", "sleep", "other")
"""``class`` and ``meal`` are the planner's; this module counts the other four."""
WINDOW_DAYS: Final[int] = 84
MIN_WEEKS: Final[int] = 3
MIN_SHARE: Final[float] = 0.6
_DAY_MIN: Final[int] = 24 * 60


@dataclass(frozen=True)
class Routine:
    """What he usually does on one weekday."""

    routine_id: str
    what: str
    kind: str
    weekdays: tuple[int, ...]  # Monday is 0
    start: str  # "HH:MM", local
    end: str  # "HH:MM", or empty
    spread_min: int
    place: str
    seen: int  # weeks it happened
    of: int  # weeks the source saw anything on that weekday
    last_seen: str  # ISO date
    sources: tuple[str, ...]  # event uids, ride ids
    line: str = ""
    from_stop: str = ""
    to_stop: str = ""


class _Time(NamedTuple):
    day: date
    start: int  # minute of the day
    end: int | None
    source: str


@dataclass
class _Habit:
    kind: str
    key: str
    what: str = ""
    place: str = ""
    line: str = ""
    from_stop: str = ""
    to_stop: str = ""


def _day_ms(day: date, zone: tzinfo) -> int:
    return int(datetime.combine(day, time.min, zone).timestamp() * 1000)


def _local(ms: int, zone: tzinfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, zone)


def _minute(ms: int, zone: tzinfo) -> int:
    moment = _local(ms, zone)
    return moment.hour * 60 + moment.minute


def _typical(minutes: Sequence[int], *, night: bool = False) -> tuple[int, int]:
    """The median minute of the day and the median distance from it; a night's counted from noon."""
    shift = _DAY_MIN // 2 if night else 0
    moved = [(m + shift) % _DAY_MIN for m in minutes]
    middle = round(statistics.median(moved))
    spread = round(statistics.median(abs(m - middle) for m in moved))
    return (middle - shift) % _DAY_MIN, spread


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _first_per_day(times: Iterable[_Time]) -> list[_Time]:
    first: dict[date, _Time] = {}
    for one in times:
        if one.day not in first or one.start < first[one.day].start:
            first[one.day] = one
    return list(first.values())


def _from_stays(
    stays: Sequence[phone_day.Span],
    places: Sequence[Place],
    zone: tzinfo,
) -> dict[str, tuple[_Habit, list[_Time]]]:
    """When he first leaves home each day, and his first arrival each day at each kept place."""
    where = [containing(places, float(s.fields["lat"]), float(s.fields["lng"])) for s in stays]
    found: dict[str, tuple[_Habit, list[_Time]]] = {}
    for index, (stay, place) in enumerate(zip(stays, where, strict=True)):
        if place is None:
            continue
        uid = stay.origin[1]
        if place.role == "home":
            following = where[index + 1] if index + 1 < len(where) else None
            if stay.end is None or (following is not None and following.role == "home"):
                continue
            habit = _Habit("leave_home", place.id, place=place.name)
            moment = _Time(_local(stay.end, zone).date(), _minute(stay.end, zone), None, uid)
        else:
            habit = _Habit("other", place.id, what=place.name, place=place.name)
            end = None if stay.end is None else _minute(stay.end, zone)
            moment = _Time(_local(stay.start, zone).date(), _minute(stay.start, zone), end, uid)
        key = f"{habit.kind}|{habit.key}"
        found.setdefault(key, (habit, []))[1].append(moment)
    return {key: (habit, _first_per_day(times)) for key, (habit, times) in found.items()}


def _from_rides(
    ridden: Iterable[rides.Ride],
    zone: tzinfo,
) -> dict[str, tuple[_Habit, list[_Time]]]:
    found: dict[str, tuple[_Habit, list[_Time]]] = {}
    for ride in ridden:
        board, alight = ride.board_stop or "", ride.alight_stop or ""
        habit = _Habit(
            "ride",
            f"{ride.route}|{board}|{alight}",
            what=ride.route,
            place=alight,
            line=ride.route,
            from_stop=board,
            to_stop=alight,
        )
        end = None if ride.ended_at_ms is None else _minute(ride.ended_at_ms, zone)
        moment = _Time(
            _local(ride.started_at_ms, zone).date(),
            _minute(ride.started_at_ms, zone),
            end,
            ride.ride_id,
        )
        found.setdefault(habit.key, (habit, []))[1].append(moment)
    return {key: (habit, _first_per_day(times)) for key, (habit, times) in found.items()}


def _nights(blocks: Iterable[phone_day.Span], zone: tzinfo) -> list[_Time]:
    """Each night's main sleep (the longest block ending that day), by the evening it began."""
    longest: dict[date, phone_day.Span] = {}
    for block in blocks:
        if block.end is None:
            continue
        woke = _local(block.end, zone).date()
        held = longest.get(woke)
        if held is None or block.end - block.start > (held.end or 0) - held.start:
            longest[woke] = block
    return [
        _Time(
            woke - timedelta(days=1),
            _minute(block.start, zone),
            _minute(block.end or block.start, zone),
            block.origin[1],
        )
        for woke, block in longest.items()
    ]


def _routine(habit: _Habit, times: Sequence[_Time], weekday: int, of: int) -> Routine:
    start, spread = _typical([t.start for t in times], night=habit.kind == "sleep")
    ends = [t.end for t in times if t.end is not None]
    digest = hashlib.sha256(f"{habit.kind}|{habit.key}|{weekday}".encode()).hexdigest()[:12]
    return Routine(
        routine_id=f"routine-{digest}",
        what=habit.what,
        kind=habit.kind,
        weekdays=(weekday,),
        start=_clock(start),
        end=_clock(_typical(ends)[0]) if ends else "",
        spread_min=spread,
        place=habit.place,
        seen=len(times),
        of=of,
        last_seen=max(t.day for t in times).isoformat(),
        sources=tuple(t.source for t in sorted(times)),
        line=habit.line,
        from_stop=habit.from_stop,
        to_stop=habit.to_stop,
    )


def for_day(
    conn: sqlite3.Connection,
    day: date,
    *,
    zone: tzinfo,
    places: Sequence[Place],
) -> list[Routine]:
    """The routines that held on ``day``'s weekday over the :data:`WINDOW_DAYS` days before it.

    ``conn`` is the event log; ``places`` the kept places (:func:`jarvis.state.places.read_kept`).
    Empty until something held for :data:`MIN_WEEKS` weeks; in start order.
    """
    first = day - timedelta(days=WINDOW_DAYS)
    rows = phone_day.read_rows(conn, _day_ms(first, zone) - phone_day.LOOKBACK_MS)
    stays = phone_day.stays(rows, _day_ms(day, zone))
    ridden = rides.fold(conn, _day_ms(first, zone))
    nights = _nights(phone_day.sleeps(rows.health), zone)

    stay_days = {
        _local(ms, zone).date() for s in stays for ms in (s.start, s.end) if ms is not None
    }
    phone_days = {
        _local(e.ts_epoch_ms, zone).date() for e in (*rows.visits, *rows.motions, *rows.health)
    } | {_local(r.started_at_ms, zone).date() for r in ridden}
    counted: list[tuple[_Habit, list[_Time], set[date]]] = [
        (habit, times, stay_days) for habit, times in _from_stays(stays, places, zone).values()
    ]
    counted += [(habit, times, phone_days) for habit, times in _from_rides(ridden, zone).values()]
    counted.append((_Habit("sleep", "sleep"), nights, {n.day for n in nights}))

    weekday = day.weekday()

    def mine(when: date) -> bool:
        return first <= when < day and when.weekday() == weekday

    found: list[Routine] = []
    for habit, times, observed in counted:
        held = [t for t in times if mine(t.day)]
        of = len({d for d in observed if mine(d)} | {t.day for t in held})
        if len(held) >= MIN_WEEKS and len(held) >= MIN_SHARE * of:
            found.append(_routine(habit, held, weekday, of))
    return sorted(found, key=lambda r: (r.start, r.kind, r.routine_id))
