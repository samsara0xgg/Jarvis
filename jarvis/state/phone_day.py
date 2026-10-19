"""The phone's part of a day, folded from the event log (ADR 0197, ADR 0199).

One read of the phone's rows (:func:`read_rows`) and the folds that turn them into stays, moves and
sleep, with the thresholds that decide them. A stay is a visit iOS reported or, where no visit
covers the time, a stretch cut from the phone's location fixes (ADR 0301). The day line (ADR 0199,
:mod:`jarvis.state.day_line`) serves these as timed items and the ledger (ADR 0201,
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


def _runs(fixes: list[Event]) -> list[list[_Fix]]:
    """The fixes good enough to place him, in time order, cut into runs.

    A run goes on while the next fix lies within :data:`STAY_RADIUS_M` of the mean of the run's
    fixes so far; a fix beyond it starts the next run. A fix worse than :data:`FIX_MAX_ACCURACY_M`
    is passed over and ends nothing.
    """
    good = sorted(
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
    runs: list[list[_Fix]] = []
    for fix in good:
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


def _long_moves(events: list[Event], now_ms: int) -> list[Span]:
    """Walking, running, cycling and driving, de-flickered, before any stay is taken out.

    A segment ends where it says, else where the next segment of any activity starts, so a
    stationary or doubtful segment ends the move before it. Segments of one activity that touch
    within :data:`MOVE_MERGE_GAP_MS` merge; moves shorter than :data:`MOVE_MIN_MS` are dropped.
    """
    facts = settle(events, "started_at_ms", "ended_at_ms")
    merged: list[Span] = []
    for index, fact in enumerate(facts):
        end = fact.end
        if end is None and index + 1 < len(facts):
            end = facts[index + 1].start
        activity = fact.payload["activity"]
        if activity not in MOVE_ACTIVITIES or fact.payload["confidence"] not in MOVE_CONFIDENCES:
            continue
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.fields["activity"] == activity
            and last.end is not None
            and fact.start - last.end <= MOVE_MERGE_GAP_MS
        ):
            merged[-1] = replace(
                last,
                origin=min(last.origin, fact.origin),
                end=None if end is None else max(last.end, end),
            )
        else:
            merged.append(Span(fact.origin, "move", fact.start, end, {"activity": activity}))
    return [
        move
        for move in merged
        if (now_ms if move.end is None else move.end) - move.start >= MOVE_MIN_MS
    ]


def moves(events: list[Event], held: list[Span], now_ms: int) -> list[Span]:
    """How he got about: the moves of :func:`_long_moves` less those wholly inside one stay.

    ``held`` are his stays, visits and cut ones alike; a stay still going counts.
    """
    return [
        move
        for move in _long_moves(events, now_ms)
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
