"""Where a bus ride is, from the bus's own position and the trip's stops (ADR 0224).

Pure functions over what BC Transit's feeds said; nothing here reads the log or the network.
The bus is the truth about the ride: the phone's fixes only say whether he is still with it
(:func:`rides_with`, :func:`apart_from`), and the trip's stop list, the vehicle's position and
the live prediction give the next stop, the stops left and the arrival at his stop
(:func:`progress`). Two facts are kept apart: whether the bus's position is live
(``position_live``) and whether the arrival at his stop is a live prediction (``arrival_live``).
BC Transit's TripUpdates often lack his stop, so a live position can come with a timetable
arrival. Without a live vehicle the whole answer comes from the timetable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from jarvis.shared.geo import metres
from jarvis.shared.rounding import arrival_ms
from jarvis.state import stop_finder

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

BOARD_M: Final = 60.0
"""Two consecutive fixes this close to one vehicle, moving with it, are a boarding (ADR 0224)."""
MOVE_M: Final = 40.0
"""Both the vehicle and the phone must have moved this far between the two fixes."""
LEFT_M: Final = 150.0
"""A fix this far from his vehicle, twice running, is him leaving it."""
ACCURACY_M: Final = 100.0
"""A fix less accurate than this decides nothing."""
ALIGN_MS: Final = 20_000
"""How far outside the vehicle's recorded positions a fix may be and still be compared."""
STOPPED_AT: Final = 1
"""GTFS-realtime ``current_status``: the vehicle is at ``stop_id`` (0 incoming, 2 in transit)."""

type Sample = tuple[int, float, float]
"""``(epoch ms, lat, lng)`` of a vehicle, in time order."""


@dataclass(frozen=True)
class Vehicle:
    """One bus in the VehiclePositions feed."""

    vehicle_id: str
    trip_id: str
    route: str
    lat: float
    lng: float
    at_ms: int
    stop_id: str | None = None
    stop_seq: int | None = None
    status: int | None = None
    start_date: str | None = None


@dataclass(frozen=True)
class StopTime:
    """One stop of a trip: its order, place and scheduled time (epoch ms)."""

    seq: int
    stop_id: str
    name: str
    lat: float
    lng: float
    sched_ms: int


@dataclass(frozen=True)
class Fix:
    """One location fix from his phone."""

    at_ms: int
    lat: float
    lng: float
    accuracy_m: float
    device: str


@dataclass(frozen=True)
class Progress:
    """Where the ride is. ``position_live``: a live vehicle gave the place; else the timetable did.

    ``arrival_live``: the feed predicted the arrival at his stop; else it is a timetable estimate.
    """

    next_stop: str | None
    stops_left: int | None
    eta_ms: int | None
    minutes: int | None
    passed: bool
    position_live: bool
    arrival_live: bool = False


def position_at(samples: Sequence[Sample], at_ms: int) -> tuple[float, float] | None:
    """The vehicle's place at ``at_ms``: interpolated, or the end sample within ``ALIGN_MS``."""
    if not samples:
        return None
    first, last = samples[0], samples[-1]
    if at_ms < first[0]:
        return (first[1], first[2]) if first[0] - at_ms <= ALIGN_MS else None
    if at_ms > last[0]:
        return (last[1], last[2]) if at_ms - last[0] <= ALIGN_MS else None
    for (t0, la0, ln0), (t1, la1, ln1) in pairwise(samples):
        if t0 <= at_ms <= t1:
            share = 0.0 if t1 == t0 else (at_ms - t0) / (t1 - t0)
            return la0 + (la1 - la0) * share, ln0 + (ln1 - ln0) * share
    return (last[1], last[2])


def rides_with(first: Fix, second: Fix, samples: Sequence[Sample]) -> float | None:
    """How close, in metres on average, two fixes are to a vehicle they move with; else None."""
    a, b = position_at(samples, first.at_ms), position_at(samples, second.at_ms)
    if a is None or b is None or max(first.accuracy_m, second.accuracy_m) > ACCURACY_M:
        return None
    near = (metres((first.lat, first.lng), a), metres((second.lat, second.lng), b))
    if max(near) > BOARD_M:
        return None
    phone_moved = metres((first.lat, first.lng), (second.lat, second.lng))
    if min(metres(a, b), phone_moved) < MOVE_M:
        return None
    return sum(near) / 2


def apart_from(fix: Fix, samples: Sequence[Sample]) -> bool | None:
    """Whether the fix is farther than ``LEFT_M`` from the vehicle; None when it cannot tell."""
    place = position_at(samples, fix.at_ms)
    if place is None or fix.accuracy_m > ACCURACY_M:
        return None
    return metres((fix.lat, fix.lng), place) > LEFT_M


def left_at(fixes: Sequence[Fix], samples: Sequence[Sample]) -> int | None:
    """When his fixes left the vehicle: the first of two far fixes in a row, else None.

    A fix that cannot be judged is skipped; a near fix ends the run.
    """
    run: list[int] = []
    for fix in sorted(fixes, key=lambda one: one.at_ms):
        far = apart_from(fix, samples)
        if far is None:
            continue
        run = [*run, fix.at_ms] if far else []
        if len(run) == 2:  # noqa: PLR2004 - "twice" is the rule.
            return run[0]
    return None


def nearest_stop(
    stops: Sequence[StopTime], place: tuple[float, float], within: float = stop_finder.NEAR_M,
) -> StopTime | None:
    """The stop closest to ``place``, if it is within ``within`` metres of it (ADR 0344)."""
    best = min(stops, key=lambda s: metres((s.lat, s.lng), place), default=None)
    if best is None or metres((best.lat, best.lng), place) > within:
        return None
    return best


def stop_named(stops: Sequence[StopTime], words: str) -> list[StopTime]:
    """The stops whose name holds every word of ``words``; those named just that when there are."""
    hits = stop_finder.matching(stops, words, lambda s: s.name)
    asked = stop_finder.words(words)
    exact = [s for s in hits if stop_finder.words(s.name) == asked]
    return exact or hits


def next_index(stops: Sequence[StopTime], vehicle: Vehicle | None, now_ms: int) -> int:
    """The index of the stop the bus reaches next; ``len(stops)`` when the trip is over."""
    if vehicle is not None and vehicle.stop_seq is not None:
        index = next((i for i, s in enumerate(stops) if s.seq >= vehicle.stop_seq), len(stops))
        at_it = index < len(stops) and stops[index].seq == vehicle.stop_seq
        return index + 1 if at_it and vehicle.status == STOPPED_AT else index
    if vehicle is not None and stops:
        here = (vehicle.lat, vehicle.lng)
        return min(range(len(stops)), key=lambda i: metres((stops[i].lat, stops[i].lng), here))
    return next((i for i, s in enumerate(stops) if s.sched_ms > now_ms), len(stops))


def progress(  # noqa: PLR0913 - the trip, his stop, the bus, the prediction and the clock.
    stops: Sequence[StopTime],
    alight_id: str | None,
    vehicle: Vehicle | None,
    predicted: Mapping[str, int],
    now_ms: int,
    *,
    alight_seq: int | None = None,
) -> Progress:
    """Next stop, stops left and arrival at his stop; ``vehicle`` None means the timetable.

    ``predicted`` is the live arrival (epoch ms) by stop id, used only with a live vehicle. A
    stop's lateness is carried forward: the next stop's prediction minus its timetable moves the
    stops after it.
    """
    nxt = next_index(stops, vehicle, now_ms)
    name = stops[nxt].name if nxt < len(stops) else None
    live = vehicle is not None
    predicted = predicted if live else {}
    if alight_id is None:
        return Progress(name, None, None, None, passed=False, position_live=live)
    found = [i for i, s in enumerate(stops) if s.seq == alight_seq] if alight_seq else []
    found = found or [i for i, s in enumerate(stops) if s.stop_id == alight_id]
    if not found:
        return Progress(name, None, None, None, passed=False, position_live=live)
    goal = next((i for i in found if i >= nxt), found[-1])
    if goal < nxt:
        return Progress(name, 0, None, None, passed=True, position_live=live)
    stop = stops[goal]
    late = 0
    if nxt < len(stops) and stops[nxt].stop_id in predicted:
        late = predicted[stops[nxt].stop_id] - stops[nxt].sched_ms
    eta = arrival_ms(predicted.get(stop.stop_id, stop.sched_ms + late))  # one rounding (ADR 0342)
    minutes = max(0, math.ceil((eta - now_ms) / 60_000))
    return Progress(
        name, goal - nxt + 1, eta, minutes, passed=False, position_live=live,
        arrival_live=stop.stop_id in predicted,
    )
