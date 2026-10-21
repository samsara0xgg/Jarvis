"""ADR 0323: his footprint of a day, folded from the event log when asked.

The day line already folds where he stayed and how he moved (ADR 0199, 0301,
:mod:`jarvis.state.phone_day`). This keeps the stays that count in a day, the long ones, and the
outings between them. Each outing carries its route, from the phone's dense track (ADR 0312) with
its sampled fixes added where the track has none, how far and how fast he went, the stops on the
way and his heart rate. Nothing is stored and no words are made: the phone words what it shows.

Layer rules: stdlib, :mod:`jarvis.shared` and other :mod:`jarvis.state` modules only (L2).
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.shared.geo import metres
from jarvis.state import places
from jarvis.state.phone_day import (
    LOOKBACK_MS,
    MEASURE_EVENT,
    Span,
    moves,
    payloads,
    read_rows,
    stays,
)
from jarvis.state.phone_location import LOCATION_EVENT
from jarvis.state.phone_track import MAX_WINDOW_MS, SIMULATED, track

if TYPE_CHECKING:
    import sqlite3

_MINUTE_MS: Final = 60_000

LONG_STAY_MS: Final = 20 * _MINUTE_MS
"""A stay at least this long is a long stay; so is one of any length in a place he keeps, and the
one he is still at."""
OUTING_MIN_M: Final = 300.0
"""Two long stays whose centres are this far apart, or farther, have an outing between them
even when no move was seen."""
ROUTE_MAX_ACCURACY_M: Final = 50.0
"""A fix worse than this (metres), or one iOS marks as simulated, is left out of the route."""
SAMPLED_NEAR_MS: Final = 30_000
"""A sampled fix joins the route only where the track has no fix within this."""
GAP_MS: Final = 2 * _MINUTE_MS
GAP_M: Final = 300.0
"""Two fixes in a row more than :data:`GAP_MS` and more than :data:`GAP_M` apart are a gap: no
line is drawn across it, but its straight line counts in the distance."""
SPEED_NEAR_MS: Final = 5_000
"""The speed at a point is the mean over the fixes this close to it, before or after."""
SIMPLIFY_M: Final = 5.0
"""A fix is left out of the points only if it lies this near (metres) the position the kept
points on either side give for its time."""
_DEGREE_M: Final = metres((0.0, 0.0), (1.0, 0.0))
"""Metres in a degree of latitude, as :func:`metres` counts them."""

_SAMPLED_SQL: Final = (
    "SELECT payload_json, ts_epoch_ms, ingestion_node FROM events WHERE type = ? "
    "AND ts_epoch_ms >= ? AND ts_epoch_ms <= ? ORDER BY ts_epoch_ms ASC, id ASC"
)
"""The sampled fixes of a window and the device each came from, in the order they happened."""


class _Fix(NamedTuple):
    at: int
    lat: float
    lng: float
    accuracy_m: float | None
    speed_mps: float | None  # iOS's own; None when it gave none
    flags: int


class _Stretch(NamedTuple):
    """The time between two long stays, or from the first move, or on from the last stay."""

    before: Span | None
    after: Span | None
    start: int
    end: int | None


class _Trace(NamedTuple):
    distance_m: float
    max_speed_mps: float | None
    gaps: list[dict[str, int]]
    points: list[list[Any]]


def fold(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, now_ms: int, kept: list[places.Place],
) -> dict[str, Any]:
    """His footprint of ``[start_ms, end_ms)`` as of ``now_ms``: ``{"stays", "outings"}``.

    ``stays`` are the long stays that overlap the window and ``outings`` the outings that do,
    each whole and in start order; ``kept`` are the places he keeps. A stay has the id the day
    line gives it. An outing is the time between two consecutive long stays, kept only when a
    move overlaps it or the two stays' centres are :data:`OUTING_MIN_M` apart; before the first
    long stay it starts at the first move, and after the last one it is still going. The stays
    that are not long, inside it, are its stops. Times are in UTC milliseconds throughout.
    """
    rows = read_rows(conn, start_ms - LOOKBACK_MS)
    held = stays(rows, now_ms)
    items = {stay.origin[1]: _stay_item(stay, kept) for stay in held}
    long_stays = [stay for stay in held if _is_long(stay, items[stay.origin[1]])]
    going = moves(rows, held, now_ms)
    here = [
        stretch for stretch in _stretches(long_stays, going)
        if _overlaps(stretch.start, stretch.end, start_ms, end_ms)
    ]
    beats = _heart_samples(conn, here, start_ms, end_ms, now_ms)
    home = _home_mean(
        beats, [stay for stay in long_stays if _is_home(items[stay.origin[1]])], start_ms, end_ms,
    )
    outings: list[dict[str, Any]] = []
    for stretch in here:
        until = now_ms if stretch.end is None else stretch.end
        stops = [
            items[stay.origin[1]] for stay in held
            if stay.start >= stretch.start and _reach(stay.end) <= _reach(stretch.end)
        ]
        trace = _trace(_route(conn, stretch.start, until))
        stopped = sum(
            (now_ms if stop["end_ms"] is None else stop["end_ms"]) - stop["start_ms"]
            for stop in stops
        )
        outings.append({
            "id": f"outing-{stretch.start}",
            "start_ms": stretch.start,
            "end_ms": stretch.end,
            "from_stay": None if stretch.before is None else stretch.before.origin[1],
            "to_stay": None if stretch.after is None else stretch.after.origin[1],
            "activity": _activity(going, stretch.start, until, now_ms),
            "distance_m": trace.distance_m,
            "moving_ms": max(0, until - stretch.start - stopped),
            "max_speed_mps": trace.max_speed_mps,
            "stops": stops,
            "gaps": trace.gaps,
            "heart_rate": _heart_rate(beats, home, stretch.start, stretch.end),
            "points": trace.points,
        })
    return {
        "stays": [
            items[stay.origin[1]] for stay in long_stays
            if _overlaps(stay.start, stay.end, start_ms, end_ms)
        ],
        "outings": outings,
    }


def _reach(end: int | None) -> float:
    return math.inf if end is None else end


def _overlaps(start: int, end: int | None, from_ms: int, until_ms: int | None) -> bool:
    """Whether ``[start, end)`` meets ``[from_ms, until_ms)``, as the day line cuts a window.

    A span that begins inside the window meets it; ``None`` is no end yet.
    """
    return start < _reach(until_ms) and (start >= from_ms or _reach(end) > from_ms)


def _stay_item(stay: Span, kept: list[places.Place]) -> dict[str, Any]:
    """A stay as the day line gives it: its id and fields, and the place he keeps there."""
    inside = places.containing(kept, stay.fields["lat"], stay.fields["lng"])
    return {
        "id": stay.origin[1],
        "start_ms": stay.start,
        "end_ms": stay.end,
        "lat": stay.fields["lat"],
        "lng": stay.fields["lng"],
        "place": stay.fields.get("place"),
        "kept_place": (
            None if inside is None else {"id": inside.id, "name": inside.name, "role": inside.role}
        ),
    }


def _is_long(stay: Span, item: dict[str, Any]) -> bool:
    """Whether a stay bounds outings: he is still there, it lasted, or it is in a kept place."""
    return (
        stay.end is None or stay.end - stay.start >= LONG_STAY_MS
        or item["kept_place"] is not None
    )


def _is_home(item: dict[str, Any]) -> bool:
    return item["kept_place"] is not None and item["kept_place"]["role"] == "home"


def _stretches(long_stays: list[Span], going: list[Span]) -> list[_Stretch]:
    """The outings between the long stays, in start order, each as the time it spans.

    A stretch runs from the end of one long stay to the start of the next. Before the first long
    stay it starts at the first move and, after the last one, which must have ended, it has no
    end. A stretch is kept when a move overlaps it, or the two stays are far enough apart.
    """
    found: list[_Stretch] = []
    for before, after in zip([None, *long_stays], [*long_stays, None], strict=True):
        if before is None:
            first = next((m for m in going if after is None or m.start < after.start), None)
            if first is None:
                continue
            start = first.start
        elif before.end is None:
            continue
        else:
            start = before.end
        end = None if after is None else after.start
        if end is not None and end <= start:
            continue
        far = (
            before is not None and after is not None
            and metres(
                (before.fields["lat"], before.fields["lng"]),
                (after.fields["lat"], after.fields["lng"]),
            ) >= OUTING_MIN_M
        )
        if far or any(_overlaps(move.start, move.end, start, end) for move in going):
            found.append(_Stretch(before, after, start, end))
    return found


def _activity(going: list[Span], start: int, until: int, now_ms: int) -> str | None:
    """The named activity with the most time inside ``[start, until]``; ``None`` with none.

    A move with no activity (ADR 0316: his fixes moved and nothing named how) names nothing.
    """
    spent: Counter[str] = Counter()
    for move in going:
        time_in = min(now_ms if move.end is None else move.end, until) - max(move.start, start)
        if time_in > 0 and (activity := move.fields.get("activity")) is not None:
            spent[activity] += time_in
    return max(sorted(spent), key=spent.__getitem__) if spent else None


# --- the route ------------------------------------------------------------------------------


def _track_fix(row: list[Any]) -> _Fix:
    """A row of :data:`jarvis.state.phone_track.COLUMNS` as a fix of the route."""
    at, lat, lng, accuracy, _altitude, _vertical, speed, *_rest, flags = row
    return _Fix(at, lat, lng, accuracy, speed, flags)


def _tracked(conn: sqlite3.Connection, start: int, end: int) -> dict[str, list[_Fix]]:
    """Each device's track fixes over ``[start, end]``, read 12 hours at a time."""
    found: dict[str, list[_Fix]] = defaultdict(list)
    first = start
    while first <= end:
        last = min(first + MAX_WINDOW_MS, end)
        for entry in track(conn, first, last):
            found[entry["device"]].extend(_track_fix(row) for row in entry["fixes"])
        first = last + 1
    return found


def _sampled(conn: sqlite3.Connection, start: int, end: int) -> dict[str, list[_Fix]]:
    """Each device's sampled fixes over ``[start, end]``."""
    found: dict[str, list[_Fix]] = defaultdict(list)
    for raw, at, device in conn.execute(_SAMPLED_SQL, (LOCATION_EVENT, start, end)):
        payload = json.loads(raw)
        speed = payload.get("speed_mps")
        found[str(device)].append(_Fix(
            int(at), float(payload["lat"]), float(payload["lng"]), float(payload["accuracy_m"]),
            None if speed is None else float(speed), 0,
        ))
    return found


def _near(times: list[int], at: int) -> bool:
    """Whether any of the sorted ``times`` is within :data:`SAMPLED_NEAR_MS` of ``at``."""
    index = bisect_left(times, at)
    return (index < len(times) and times[index] - at <= SAMPLED_NEAR_MS) or (
        index > 0 and at - times[index - 1] <= SAMPLED_NEAR_MS
    )


def _route(conn: sqlite3.Connection, start: int, end: int) -> list[_Fix]:
    """The fixes of the route over ``[start, end]``, one per millisecond, oldest first.

    The route is the device's with the most fixes there, track and sampled together. It has that
    device's track fixes and its sampled fixes with no track fix within :data:`SAMPLED_NEAR_MS`.
    A fix with no accuracy, one worse than :data:`ROUTE_MAX_ACCURACY_M` and a simulated one are
    left out.
    """
    end = max(start, end)
    tracked, sampled = _tracked(conn, start, end), _sampled(conn, start, end)
    counts = {
        device: len(tracked.get(device, [])) + len(sampled.get(device, []))
        for device in {*tracked, *sampled}
    }
    if not counts:
        return []
    device = max(sorted(counts), key=counts.__getitem__)
    dense = tracked.get(device, [])
    times = [fix.at for fix in dense]
    chosen = sorted(
        [*dense, *(fix for fix in sampled.get(device, []) if not _near(times, fix.at))],
        key=lambda fix: fix.at,
    )
    route: list[_Fix] = []
    for fix in chosen:
        good = (
            fix.accuracy_m is not None and fix.accuracy_m <= ROUTE_MAX_ACCURACY_M
            and not fix.flags & SIMULATED
        )
        if good and (not route or fix.at != route[-1].at):
            route.append(fix)
    return route


def _speed_at(fixes: list[_Fix], index: int, cut: list[bool]) -> float | None:
    """The speed iOS gave at a fix, else distance over time between its neighbours (no gap)."""
    speed = fixes[index].speed_mps
    if speed is not None and speed >= 0:
        return speed
    first = index - 1 if index > 0 and not cut[index - 1] else index
    last = index + 1 if index + 1 < len(fixes) and not cut[index] else index
    if first == last:
        return None
    a, b = fixes[first], fixes[last]
    return metres((a.lat, a.lng), (b.lat, b.lng)) / ((b.at - a.at) / 1000)


def _mean_speeds(fixes: list[_Fix], cut: list[bool]) -> list[float | None]:
    """The mean speed at each fix, over the fixes within :data:`SPEED_NEAR_MS` of it (itself too).

    ``cut[i]`` says there is a gap between fix ``i`` and the next. A fix with no speed adds
    nothing to the mean; with none near, the mean is ``None``.
    """
    own = [_speed_at(fixes, index, cut) for index in range(len(fixes))]
    times = [fix.at for fix in fixes]
    sums, counts = [0.0], [0]
    for speed in own:
        sums.append(sums[-1] + (0.0 if speed is None else speed))
        counts.append(counts[-1] + (0 if speed is None else 1))
    means: list[float | None] = []
    for fix in fixes:
        low = bisect_left(times, fix.at - SPEED_NEAR_MS)
        high = bisect_right(times, fix.at + SPEED_NEAR_MS)
        number = counts[high] - counts[low]
        means.append((sums[high] - sums[low]) / number if number else None)
    return means


def _keep(fixes: list[_Fix], first: int, last: int) -> list[int]:
    """The indices of ``fixes[first..last]`` to keep, the ends among them (Douglas-Peucker).

    The distance of a fix is taken from the position the two kept points around it give for its
    time (their latitudes and longitudes interpolated linearly in time), so a fix is left out only
    where the line would have him at that moment within :data:`SIMPLIFY_M`. Iterative: a day of
    fixes would be as deep as it is long. The distance is taken flat, north and east of that
    position in metres: at a few metres that is the great-circle distance to a fraction of a
    millimetre, and several times quicker than :func:`metres` for each fix of each pass.
    """
    points = [
        (fix.at, fix.lat * _DEGREE_M, fix.lng, _DEGREE_M * math.cos(math.radians(fix.lat)))
        for fix in fixes[first : last + 1]
    ]
    kept = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        low, high = pending.pop()
        if high <= low + 1:
            continue
        t0, north0, east0, _ = points[low]
        t1, north1, east1, _ = points[high]
        north_rate, east_rate = (north1 - north0) / (t1 - t0), (east1 - east0) / (t1 - t0)
        off = [
            math.hypot(
                north - north0 - north_rate * (at - t0),
                (east - east0 - east_rate * (at - t0)) * wide,
            )
            for at, north, east, wide in points[low + 1 : high]
        ]
        worst = max(off)
        if worst > SIMPLIFY_M:
            farthest = low + 1 + off.index(worst)
            kept.add(farthest)
            pending.extend(((low, farthest), (farthest, high)))
    return [first + index for index in sorted(kept)]


def _trace(fixes: list[_Fix]) -> _Trace:
    """The distance, top speed, gaps and simplified points of a route (fixes in time order).

    The speed at each fix is taken before the route is simplified. Each piece between gaps is
    simplified on its own, keeping its first and last fix, and the distance runs along the points
    kept, a gap's straight line included: a fix a second while he is parked would add its jitter.
    """
    if not fixes:
        return _Trace(0.0, None, [], [])
    pairs = list(pairwise(fixes))
    cut = [
        b.at - a.at > GAP_MS and metres((a.lat, a.lng), (b.lat, b.lng)) > GAP_M for a, b in pairs
    ]
    means = _mean_speeds(fixes, cut)
    shown: list[int] = []
    first = 0
    for index, ends in enumerate([*cut, True]):
        if ends:
            shown.extend(_keep(fixes, first, index))
            first = index + 1
    speeds = [None if mean is None else round(mean, 2) for mean in means]
    known = [speed for speed in speeds if speed is not None]
    distance = sum(
        metres((fixes[a].lat, fixes[a].lng), (fixes[b].lat, fixes[b].lng))
        for a, b in pairwise(shown)
    )
    return _Trace(
        round(distance, 1),
        max(known) if known else None,
        [
            {"start_ms": a.at, "end_ms": b.at}
            for (a, b), gap in zip(pairs, cut, strict=True) if gap
        ],
        [
            [fixes[i].at, round(fixes[i].lat, 6), round(fixes[i].lng, 6), speeds[i]]
            for i in shown
        ],
    )


# --- the heart ------------------------------------------------------------------------------


def _heart_samples(
    conn: sqlite3.Connection, here: list[_Stretch], start_ms: int, end_ms: int, now_ms: int,
) -> list[tuple[int, float]]:
    """The Watch's heart rate samples that start in the day or in an outing of it, by time.

    ``(start_ms, beats a minute)``; a sample the phone sent twice counts once, by its uuid. The
    rows are timed at the end of the sample, so they are read from the first start to a little
    past the last, as the phone may report late.
    """
    if not here:
        return []
    low = min(start_ms, *(stretch.start for stretch in here))
    high = max(end_ms, *(now_ms if stretch.end is None else stretch.end for stretch in here))
    seen: set[str] = set()
    found: list[tuple[int, float]] = []
    for row in payloads(conn, MEASURE_EVENT, "heart_rate", low, high + LOOKBACK_MS):
        uuid, at = str(row["sample_uuid"]).lower(), int(row["start_ms"])
        if uuid not in seen and low <= at < high:
            seen.add(uuid)
            found.append((at, row["value"]))
    return sorted(found, key=lambda sample: sample[0])


def _home_mean(
    beats: list[tuple[int, float]], homes: list[Span], start_ms: int, end_ms: int,
) -> float | None:
    """The mean of the day's samples that start in a long stay in his kept home, if any."""
    at_home = [
        bpm for at, bpm in beats
        if start_ms <= at < end_ms and any(home.start <= at < _reach(home.end) for home in homes)
    ]
    return round(sum(at_home) / len(at_home), 1) if at_home else None


def _heart_rate(
    beats: list[tuple[int, float]], home: float | None, start: int, end: int | None,
) -> dict[str, Any] | None:
    """The samples that start in ``[start, end)``, or ``None`` when there are none."""
    inside = [(at, bpm) for at, bpm in beats if start <= at < _reach(end)]
    if not inside:
        return None
    values = [bpm for _, bpm in inside]
    return {
        "avg_bpm": round(sum(values) / len(values), 1),
        "max_bpm": max(values),
        "home_avg_bpm": home,
        "samples": [[at, bpm] for at, bpm in inside],
    }
