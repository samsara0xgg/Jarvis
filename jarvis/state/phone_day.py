"""The phone's part of a day, folded from the event log (ADR 0197, ADR 0199).

One read of the phone's rows (:func:`read_rows`) and the folds that turn them into stays, moves and
sleep, with the thresholds that decide them. A stay is a visit iOS reported or, where no visit
covers the time, a stretch cut from the phone's location fixes (ADR 0301). A move is what motion
reported, or a stretch where the fixes leave the stays and change place (ADR 0316). The day line
(ADR 0199, :mod:`jarvis.state.day_line`) serves these as timed items and the ledger (ADR 0201,
:mod:`jarvis.state.ledger`) counts them into the prompt, so the two cannot disagree about where he
stayed or when he slept: they share these rules. Nothing is stored and nothing here calls a
model.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.shared.geo import metres
from jarvis.state.event_log import get_event, iter_events_of_types
from jarvis.state.phone_location import LOCATION_EVENT, VISIT_EVENT

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

    from jarvis.shared import Event

MOTION_EVENT: Final = "phone.motion_observed"
HEALTH_EVENT: Final = "phone.health_observed"
MEASURE_EVENT: Final = "phone.measure_observed"

_MINUTE_MS: Final = 60_000

LOOKBACK_MS: Final = 2 * 24 * 60 * _MINUTE_MS
"""How far before the window the phone rows are read, by the time the phone reported them: a stay
that began before the window and ends inside it is still found."""
MOVE_ACTIVITIES: Final = frozenset({"walking", "running", "cycling", "automotive"})
MOVE_CONFIDENCES: Final = frozenset({"medium", "high"})
MOVE_MERGE_GAP_MS: Final = 3 * _MINUTE_MS
"""Consecutive segments of one activity this close (or overlapping) are one move."""
MOVE_MIN_MS: Final = 3 * _MINUTE_MS
"""A merged move shorter than this is dropped; one with no end yet is measured to now."""
SLEEP_MERGE_GAP_MS: Final = 30 * _MINUTE_MS
"""Sleep spans that overlap or are this close are one block."""
FIX_MAX_ACCURACY_M: Final = 100.0
"""A location fix worse than this (metres) is left out of the stays (ADR 0301)."""
STAY_RADIUS_M: Final = 150.0
"""Fixes this close to their run's running centre are one stay: the distance the phone itself uses
to decide he has not moved."""
STAY_MIN_MS: Final = 5 * _MINUTE_MS
"""A stay cut from fixes, or what a visit leaves of one, shorter than this is dropped; one with no
end yet is measured to now."""
PATH_GAP_MS: Final = 5 * _MINUTE_MS
"""Fixes outside the stays this close are one path, and a path reaches a stay this close to its
nearest fix: the phone sends a fix every 30 to 60 s while he moves (ADR 0316)."""
VEHICLE_SPEED_MPS: Final = 25 / 3.6
"""Two points of a path this fast apart, less both their accuracies, were ridden: the speed the
phone itself takes for a vehicle (ADR 0312)."""
VEHICLE_SPAN_MS: Final = 10_000
"""The least time between the two points a vehicle's speed is taken over."""
CUT_FROM_KEY: Final = "cut_from"
CUT_FROM_FIXES: Final = "fixes"
"""A stay cut from fixes carries ``cut_from: "fixes"`` in its fields; a visit has no such key."""
ASLEEP_STAGES: Final = frozenset({"unspecified", "core", "deep", "rem"})
"""The ``detail`` of a ``sleep`` span that counts as asleep (the others are in bed and awake)."""


class PhoneRows(NamedTuple):
    """The phone's rows by kind, each in the log's append order."""

    visits: list[Event]
    motions: list[Event]
    health: list[Event]
    fixes: list[Event]


class Fact(NamedTuple):
    """What a phone reported about one start time, once its rows are settled."""

    origin: tuple[int, str]
    start: int
    end: int | None
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Span:
    """One item of the phone's lane before it is worded."""

    origin: tuple[int, str]  # (place in the rows read, event_uid) of the first row it came from
    kind: str
    start: int
    end: int | None
    fields: dict[str, Any]


def read_rows(conn: sqlite3.Connection, since_epoch_ms: int) -> PhoneRows:
    """The phone's visits, motion, Health rows and location fixes reported at or after the time.

    One indexed read of the four types. The caller reaches back :data:`LOOKBACK_MS` before the
    window it folds. The fixes also start with the last fix of accuracy :data:`FIX_MAX_ACCURACY_M`
    or better from before that time, however long before: a stay that began days earlier, such as
    a weekend at home, still covers the window (ADR 0301).
    """
    rows = PhoneRows([], [], [], [])
    by_type = {
        VISIT_EVENT: rows.visits, MOTION_EVENT: rows.motions, HEALTH_EVENT: rows.health,
        LOCATION_EVENT: rows.fixes,
    }
    for event in iter_events_of_types(conn, tuple(by_type), since_epoch_ms=since_epoch_ms):
        by_type[event.type].append(event)
    last = conn.execute(
        "SELECT event_uid FROM events WHERE type = ? AND ts_epoch_ms < ? "
        "AND json_extract(payload_json, '$.accuracy_m') <= ? ORDER BY ts_epoch_ms DESC, id DESC "
        "LIMIT 1",
        (LOCATION_EVENT, since_epoch_ms, FIX_MAX_ACCURACY_M),
    ).fetchone()
    if last is not None and (before := get_event(conn, str(last[0]))) is not None:
        rows.fixes.insert(0, before)
    return rows


def stamp(conn: sqlite3.Connection, since_epoch_ms: int, until_epoch_ms: int) -> tuple[int, int]:
    """``(count, newest id)`` of the phone rows timed in ``[since, until)``, without decoding any.

    Changes exactly when a row for that stretch of time arrives, a location fix included, so it
    keys a cache of a text built from those rows.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM events WHERE type IN (?, ?, ?, ?) "
        "AND ts_epoch_ms >= ? AND ts_epoch_ms < ?",
        (VISIT_EVENT, MOTION_EVENT, HEALTH_EVENT, LOCATION_EVENT, since_epoch_ms, until_epoch_ms),
    ).fetchone()
    return int(row[0]), int(row[1])


def settle(events: list[Event], start_key: str, end_key: str) -> list[Fact]:
    """One fact per start time, in start order.

    A fact the phone reports twice (once as it begins, again with its end) shares its start time
    in both rows: the row with an end wins, and of two rows with one, the later.
    """
    first: dict[int, tuple[int, str]] = {}
    kept: dict[int, Event] = {}
    for place, event in enumerate(events):
        start = int(event.payload[start_key])
        first.setdefault(start, (place, event.event_uid))
        held = kept.get(start)
        if (
            held is None
            or event.payload.get(end_key) is not None
            or held.payload.get(end_key) is None
        ):
            kept[start] = event
    facts: list[Fact] = []
    for start, event in sorted(kept.items()):
        end = event.payload.get(end_key)
        facts.append(Fact(first[start], start, None if end is None else int(end), event.payload))
    return facts


def _visit_stays(events: list[Event]) -> list[Span]:
    """The visits iOS reported, with an arrival that never got a departure ended by the next."""
    facts = settle(events, "arrived_at_ms", "departed_at_ms")
    found: list[Span] = []
    for index, fact in enumerate(facts):
        end = fact.end
        if end is None and index + 1 < len(facts):
            end = facts[index + 1].start
        fields: dict[str, Any] = {
            "lat": float(fact.payload["lat"]),
            "lng": float(fact.payload["lng"]),
            "accuracy_m": float(fact.payload["accuracy_m"]),
        }
        if fact.payload.get("place"):
            fields["place"] = str(fact.payload["place"])
        found.append(Span(fact.origin, "stay", fact.start, end, fields))
    return found


class _Fix(NamedTuple):
    at: int
    origin: tuple[int, str]
    lat: float
    lng: float
    accuracy_m: float
    place: str


def _centre(run: list[_Fix]) -> tuple[float, float]:
    return sum(f.lat for f in run) / len(run), sum(f.lng for f in run) / len(run)


def _good(fixes: list[Event]) -> list[_Fix]:
    """The fixes good enough to place him, :data:`FIX_MAX_ACCURACY_M` or better, in time order."""
    return sorted(
        (
            _Fix(
                event.ts_epoch_ms, (place, event.event_uid), float(event.payload["lat"]),
                float(event.payload["lng"]), float(event.payload["accuracy_m"]),
                str(event.payload.get("place") or ""),
            )
            for place, event in enumerate(fixes)
            if float(event.payload["accuracy_m"]) <= FIX_MAX_ACCURACY_M
        ),
        key=lambda fix: (fix.at, fix.origin[0]),
    )


def _runs(fixes: list[Event]) -> list[list[_Fix]]:
    """The good fixes (:func:`_good`) cut into runs.

    A run goes on while the next fix lies within :data:`STAY_RADIUS_M` of the mean of the run's
    fixes so far; a fix beyond it starts the next run. A fix worse than :data:`FIX_MAX_ACCURACY_M`
    is passed over and ends nothing.
    """
    runs: list[list[_Fix]] = []
    for fix in _good(fixes):
        if runs and metres(_centre(runs[-1]), (fix.lat, fix.lng)) <= STAY_RADIUS_M:
            runs[-1].append(fix)
        else:
            runs.append([fix])
    return runs


def _uncovered(start: int, end: int | None, held: list[Span]) -> list[tuple[int, int | None]]:
    """``[start, end)`` without the time any of ``held`` covers, as the pieces left in order."""
    pieces: list[tuple[int, int | None]] = [(start, end)]
    for visit in held:
        left: list[tuple[int, int | None]] = []
        for first, last in pieces:
            if _reach(visit.end) <= first or visit.start >= _reach(last):
                left.append((first, last))
                continue
            if visit.start > first:
                left.append((first, visit.start))
            if visit.end is not None and visit.end < _reach(last):
                left.append((visit.end, last))
        pieces = left
    return pieces


def _off_moves(first: int, last: int | None, going: list[Span]) -> tuple[int, int | None]:
    """``[first, last)`` with the ends of moves that cross its edges taken off.

    The first fix of a run often comes while he is still walking in, and the last while he is
    already walking off; the move owns those minutes. A move wholly inside or wholly around the
    piece changes nothing here.
    """
    for move in going:
        inside_start = move.start <= first < _reach(move.end) < _reach(last)
        if inside_start and move.end is not None:
            first = move.end
    for move in going:
        if first < move.start < _reach(last) <= _reach(move.end):
            last = move.start
    return first, last


def _cut_stays(rows: PhoneRows, held: list[Span], now_ms: int) -> list[Span]:
    """The stays the fixes show where no visit in ``held`` covers the time (ADR 0301).

    A run of fixes is a stay from its first fix to the moment he left. When a next run exists he
    left at the start of the last move (as :func:`moves` counts them) that starts after this run's
    last fix and before the next run's first fix, the move that carried him away, else at that
    first fix: a short walk about the flat at night does not end the stay. A run with no next run
    is open, whatever moves follow its last fix. What a visit covers is cut out of it, a move that
    crosses a piece's start or end keeps its minutes (:func:`_off_moves`), and a piece
    left under :data:`STAY_MIN_MS`, or wholly inside one move (a slow walk sends fixes close enough
    to look like a run), is dropped. The first piece keeps the first fix's ``event_uid``
    as its id; a later one, which a visit in the middle makes, adds ``@`` and its start.
    """
    runs = _runs(rows.fixes)
    going = _long_moves(rows.motions, now_ms)
    starts = [move.start for move in going]
    found: list[Span] = []
    for index, run in enumerate(runs):
        left: int | None = None
        if index + 1 < len(runs):
            left = runs[index + 1][0].at
            carried = bisect_left(starts, left)
            if carried > bisect_right(starts, run[-1].at):
                left = starts[carried - 1]
        lat, lng = _centre(run)
        fields: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "accuracy_m": sum(f.accuracy_m for f in run) / len(run),
        }
        if named := Counter(f.place for f in run if f.place).most_common(1):
            fields["place"] = named[0][0]
        fields[CUT_FROM_KEY] = CUT_FROM_FIXES
        place, uid = run[0].origin
        kept = [
            piece for piece in (
                _off_moves(first, last, going) for first, last in _uncovered(run[0].at, left, held)
            )
            if (now_ms if piece[1] is None else piece[1]) - piece[0] >= STAY_MIN_MS
            and not any(
                move.start <= piece[0] and _reach(piece[1]) <= _reach(move.end) for move in going
            )
        ]
        for number, (first, last) in enumerate(kept):
            found.append(Span(
                (place, uid if number == 0 else f"{uid}@{first}"), "stay", first, last,
                dict(fields),
            ))
    return found


def stays(rows: PhoneRows, now_ms: int) -> list[Span]:
    """Where he was, in start order: the visits and, where no visit covers the time, the fixes.

    The visits are what iOS reported (:func:`_visit_stays`); the rest is cut from the fixes
    (:func:`_cut_stays`) and carries :data:`CUT_FROM_KEY`. A stay with no end yet is measured to
    ``now_ms``.
    """
    seen = _visit_stays(rows.visits)
    return sorted([*seen, *_cut_stays(rows, seen, now_ms)], key=lambda stay: stay.start)


def _reach(end: int | None) -> float:
    return math.inf if end is None else end


def _segments(events: list[Event]) -> list[Span]:
    """Motion's walking, running, cycling and driving of medium or high confidence, as reported.

    A segment ends where it says, else where the next segment of any activity starts, so a
    stationary or doubtful segment ends the one before it.
    """
    facts = settle(events, "started_at_ms", "ended_at_ms")
    found: list[Span] = []
    for index, fact in enumerate(facts):
        end = fact.end
        if end is None and index + 1 < len(facts):
            end = facts[index + 1].start
        activity = fact.payload["activity"]
        if activity in MOVE_ACTIVITIES and fact.payload["confidence"] in MOVE_CONFIDENCES:
            found.append(Span(fact.origin, "move", fact.start, end, {"activity": activity}))
    return found


def _merged(found: Iterable[Span], now_ms: int, *, first_row: bool) -> list[Span]:
    """Moves of one activity (or of none) within :data:`MOVE_MERGE_GAP_MS` joined, short ones out.

    ``found`` is in start order. A joined move keeps the id of the row reported first when
    ``first_row``, else of the move that starts first; one shorter than :data:`MOVE_MIN_MS` is
    dropped, and one with no end yet is measured to now.
    """
    merged: list[Span] = []
    for move in found:
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.fields.get("activity") == move.fields.get("activity")
            and last.end is not None
            and move.start - last.end <= MOVE_MERGE_GAP_MS
        ):
            merged[-1] = replace(
                last,
                origin=min(last.origin, move.origin) if first_row else last.origin,
                end=None if move.end is None else max(last.end, move.end),
            )
        else:
            merged.append(move)
    return [
        move
        for move in merged
        if (now_ms if move.end is None else move.end) - move.start >= MOVE_MIN_MS
    ]


def _long_moves(events: list[Event], now_ms: int) -> list[Span]:
    """Motion's moves (:func:`_segments`), de-flickered, before any stay is taken out.

    Segments of one activity that touch within :data:`MOVE_MERGE_GAP_MS` merge, under the id of
    the row reported first; moves shorter than :data:`MOVE_MIN_MS` are dropped.
    """
    return _merged(_segments(events), now_ms, first_row=True)


class _Path(NamedTuple):
    start: int
    end: int | None
    first: _Fix  # its first fix, whose event_uid names the moves cut from it
    points: list[_Fix]  # its fixes, with the centres of the stays it runs from and to


def _centre_of(stay: Span, at: int) -> _Fix:
    fields = stay.fields
    return _Fix(at, stay.origin, fields["lat"], fields["lng"], fields["accuracy_m"], "")


def _paths(fixes: list[Event], held: list[Span], now_ms: int) -> list[_Path]:
    """Where the good fixes leave the stays in ``held`` and change place (ADR 0316).

    The fixes no stay covers, in time order, are one path while no stay starts among them and
    no two in a row are more than :data:`PATH_GAP_MS` apart. A path runs from the end of the stay
    before it, and to the start of the stay after it, when that is within :data:`PATH_GAP_MS` of
    its nearest fix; else from its first fix, and to its last, or on with no end while that fix
    is at most :data:`PATH_GAP_MS` old. A path is kept when one of its points lies farther from
    its first or last point than :data:`STAY_RADIUS_M` beyond both their accuracies: a fix that
    strays from a stay and comes back is not a trip.
    """
    ordered = sorted(held, key=lambda stay: stay.start)
    starts = [stay.start for stay in ordered]
    latest: list[Span] = []  # of the first i + 1 stays, the one that reaches furthest
    for stay in ordered:
        further = not latest or _reach(stay.end) > _reach(latest[-1].end)
        latest.append(stay if further else latest[-1])
    groups: list[tuple[int, list[_Fix]]] = []  # (stays started before, fixes)
    for fix in _good(fixes):
        before = bisect_right(starts, fix.at)
        if before and _reach(latest[before - 1].end) > fix.at:
            continue
        if groups and groups[-1][0] == before and fix.at - groups[-1][1][-1].at <= PATH_GAP_MS:
            groups[-1][1].append(fix)
        else:
            groups.append((before, [fix]))
    found: list[_Path] = []
    for before, run in groups:
        points = list(run)
        start: int = run[0].at
        left = latest[before - 1] if before else None
        if left is not None and left.end is not None and start - left.end <= PATH_GAP_MS:
            start = left.end
            points.insert(0, _centre_of(left, start))
        end: int | None = run[-1].at
        if before < len(ordered) and ordered[before].start - run[-1].at <= PATH_GAP_MS:
            end = ordered[before].start
            points.append(_centre_of(ordered[before], end))
        elif before == len(ordered) and now_ms - run[-1].at <= PATH_GAP_MS:
            end = None
        if any(
            metres((one.lat, one.lng), (point.lat, point.lng))
            > STAY_RADIUS_M + one.accuracy_m + point.accuracy_m
            for one in (points[0], points[-1]) for point in points
        ):
            found.append(_Path(start, end, run[0], points))
    return found


def _ridden(points: list[_Fix], first: int, last: int | None) -> bool:
    """True when he was ridden somewhere in ``[first, last)``.

    That is when two points at least :data:`VEHICLE_SPAN_MS` apart, the first of them before
    ``last`` and the other after ``first``, lie :data:`VEHICLE_SPEED_MPS` apart less both their
    accuracies.
    """
    later = 0
    for one in points:
        while later < len(points) and points[later].at - one.at < VEHICLE_SPAN_MS:
            later += 1
        if later == len(points):
            return False
        other = points[later]
        if other.at <= first or one.at >= _reach(last):
            continue
        apart = metres((one.lat, one.lng), (other.lat, other.lng))
        seconds = (other.at - one.at) / 1000
        if apart - one.accuracy_m - other.accuracy_m >= VEHICLE_SPEED_MPS * seconds:
            return True
    return False


def _covering(segments: list[Span], first: int, last: int, now_ms: int) -> str | None:
    """The activity of ``segments`` that covers most of ``[first, last)``, if any covers it."""
    covered: Counter[str] = Counter()
    for segment in segments:
        end = now_ms if segment.end is None else segment.end
        shared = min(last, end) - max(first, segment.start)
        if shared > 0:
            covered[segment.fields["activity"]] += shared
    return covered.most_common(1)[0][0] if covered else None


def _path_moves(rows: PhoneRows, held: list[Span], going: list[Span], now_ms: int) -> list[Span]:
    """The parts of each path (:func:`_paths`) that no motion move in ``going`` covers, named.

    Automotive when :func:`_ridden` says so and motion said no cycling on the path; else the
    activity motion reported for most of the part; else the one activity motion reported on the
    path, if only one; else none. Each part's id is the path's first fix's ``event_uid``, ``@``
    and the part's start.
    """
    segments = _segments(rows.motions)
    found: list[Span] = []
    for path in _paths(rows.fixes, held, now_ms):
        stop = now_ms if path.end is None else path.end
        on_path = [
            segment for segment in segments
            if segment.start < stop and _reach(segment.end) > path.start
        ]
        said = {segment.fields["activity"] for segment in on_path}
        for first, last in _uncovered(path.start, path.end, going):
            if "cycling" not in said and _ridden(path.points, first, last):
                activity: str | None = "automotive"
            else:
                activity = _covering(on_path, first, now_ms if last is None else last, now_ms)
                if activity is None and len(said) == 1:
                    (activity,) = said
            place, uid = path.first.origin
            found.append(Span(
                (place, f"{uid}@{first}"), "move", first, last,
                {} if activity is None else {"activity": activity},
            ))
    return found


def moves(rows: PhoneRows, held: list[Span], now_ms: int) -> list[Span]:
    """How he got about: motion's moves and the paths his fixes show, less those inside a stay.

    Motion's moves (:func:`_long_moves`) and the uncovered parts of the paths
    (:func:`_path_moves`), merged as motion's segments are (:func:`_merged`); a move with no
    activity merges only with another with none. ``held`` are his stays, visits and cut ones
    alike; a move wholly inside one, a stay still going included, is dropped.
    """
    going = _long_moves(rows.motions, now_ms)
    found = sorted([*going, *_path_moves(rows, held, going, now_ms)], key=lambda move: move.start)
    return [
        move
        for move in _merged(found, now_ms, first_row=False)
        if not any(
            stay.start <= move.start and _reach(move.end) <= _reach(stay.end) for stay in held
        )
    ]


def sleeps(events: Iterable[Event]) -> list[Span]:
    """Sleep as blocks, from the stages the source recorded (ADR 0293).

    Only the asleep stages count (``unspecified``, ``core``, ``deep``, ``rem``); in bed and awake
    do not. Spans from every source are joined where they overlap, so time two sources both
    recorded counts once; the joined spans within :data:`SLEEP_MERGE_GAP_MS` of each other are one
    block, whose ``minutes`` are the time asleep in it (the gaps are not counted). A block also
    carries its ``stages``: every stage sample, asleep or not, that overlaps it, as the sources
    recorded it (``{stage, start_ms, end_ms}``, by start, two identical samples once).
    """
    held = [e for e in events if e.payload["metric"] == "sleep"]
    spans = sorted(
        (int(event.payload["start_ms"]), int(event.payload["end_ms"]), (place, event.event_uid))
        for place, event in enumerate(e for e in held if e.payload.get("detail") in ASLEEP_STAGES)
    )
    joined: list[tuple[int, int, tuple[int, str]]] = []
    for start, end, origin in spans:
        if joined and start <= joined[-1][1]:
            first, last, held_origin = joined[-1]
            joined[-1] = (first, max(last, end), min(held_origin, origin))
        else:
            joined.append((start, end, origin))
    blocks: list[tuple[int, int, int, tuple[int, str]]] = []  # start, end, asleep ms, origin
    for start, end, origin in joined:
        if blocks and start <= blocks[-1][1] + SLEEP_MERGE_GAP_MS:
            first, _last, asleep, held_origin = blocks[-1]
            blocks[-1] = (first, end, asleep + end - start, min(held_origin, origin))
        else:
            blocks.append((start, end, end - start, origin))
    samples = sorted({
        (int(e.payload["start_ms"]), int(e.payload["end_ms"]), str(e.payload.get("detail")))
        for e in held
    })
    return [
        Span(origin, "sleep", start, end, {
            "minutes": _tidy(asleep / _MINUTE_MS),
            "stages": [
                {"stage": stage, "start_ms": begin, "end_ms": stop}
                for begin, stop, stage in samples if begin < end and stop > start
            ],
        })
        for start, end, asleep, origin in blocks
    ]


def workouts(events: Iterable[Event]) -> list[Span]:
    """The workouts Health recorded (ADR 0293), one per start time, as ``workout`` spans.

    ``activity`` is the name the phone gave it; ``energy_kcal``, ``distance_m`` and ``avg_hr``
    are what Health knew of it and are left out when it did not know them.
    """
    found: list[Span] = []
    held = [e for e in events if e.payload["metric"] == "workout"]
    for fact in settle(held, "start_ms", "end_ms"):
        meta = fact.payload.get("meta") or {}
        activity = str(fact.payload.get("detail") or "workout")
        fields: dict[str, Any] = {"activity": activity, "text": activity}
        for name, key in (
            ("energy_kcal", "total_energy_kcal"), ("distance_m", "distance_m"),
            ("avg_hr", "avg_heart_rate"),
        ):
            if isinstance(meta.get(key), (int, float)):
                fields[name] = _tidy(float(meta[key]))
        found.append(Span(fact.origin, "workout", fact.start, fact.end, fields))
    return found


def health_totals(events: Iterable[Event], metric: str) -> list[tuple[int, int, float]]:
    """The totals of one metric as (start_ms, end_ms, value); a span reported again is the later."""
    latest: dict[tuple[int, int], float] = {}
    for event in events:
        if event.payload["metric"] == metric:
            span = (int(event.payload["start_ms"]), int(event.payload["end_ms"]))
            latest[span] = float(event.payload["value"])
    return [(start, end, value) for (start, end), value in latest.items()]


def payloads(
    conn: sqlite3.Connection, event_type: str, metric: str, since_ms: int, until_ms: int,
) -> list[dict[str, Any]]:
    """The payloads of one metric of one phone type reported in ``[since_ms, until_ms)``."""
    return [
        json.loads(raw) for (raw,) in conn.execute(
            "SELECT payload_json FROM events WHERE type = ? AND ts_epoch_ms >= ? "
            "AND ts_epoch_ms < ? AND json_extract(payload_json, '$.metric') = ? ORDER BY id",
            (event_type, since_ms, until_ms, metric),
        )
    ]


def day_summary(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> dict[str, Any]:
    """The day's body in three numbers: ``{steps, resting_hr, hrv}``, each ``None`` unreported.

    A sample belongs to the day it starts in. ``steps`` is the sum of the day's hourly totals
    (a total reported again counts as the later), ``resting_hr`` the day's latest resting heart
    rate sample (beats a minute) and ``hrv`` the mean of its SDNN samples (milliseconds). The
    rows are read up to :data:`LOOKBACK_MS` after the day, as the phone may report late.
    """
    until = end_ms + LOOKBACK_MS

    def inside(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if start_ms <= int(row["start_ms"]) < end_ms]

    steps: dict[tuple[int, int], float] = {}
    for row in inside(payloads(conn, HEALTH_EVENT, "steps", start_ms, until)):
        steps[(int(row["start_ms"]), int(row["end_ms"]))] = float(row["value"])
    resting = inside(payloads(conn, MEASURE_EVENT, "resting_heart_rate", start_ms, until))
    rows = payloads(conn, MEASURE_EVENT, "heart_rate_variability_sdnn", start_ms, until)
    sdnn = [float(row["value"]) for row in inside(rows)]
    return {
        "steps": round(sum(steps.values())) if steps else None,
        "resting_hr": (
            round(float(max(resting, key=lambda row: int(row["start_ms"]))["value"]), 1)
            if resting else None
        ),
        "hrv": round(sum(sdnn) / len(sdnn), 1) if sdnn else None,
    }


def _tidy(total: float) -> float:
    """A whole sum as an integer, any other rounded to hundredths of a minute."""
    return int(total) if float(total).is_integer() else round(total, 2)
