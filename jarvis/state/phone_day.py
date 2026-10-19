"""The phone's part of a day, folded from the event log (ADR 0197, ADR 0199).

One read of the phone's rows (:func:`read_rows`) and the folds that turn them into stays, moves and
sleep, with the thresholds that decide them. The day line (ADR 0199, :mod:`jarvis.state.day_line`)
serves these as timed items and the ledger (ADR 0201, :mod:`jarvis.state.ledger`) counts them into
the prompt, so the two cannot disagree about where he stayed or when he slept: they share these
rules. Nothing is stored and nothing here calls a model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state.event_log import iter_events_of_types
from jarvis.state.phone_location import VISIT_EVENT

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
ASLEEP_STAGES: Final = frozenset({"unspecified", "core", "deep", "rem"})
"""The ``detail`` of a ``sleep`` span that counts as asleep (the others are in bed and awake)."""


class PhoneRows(NamedTuple):
    """The phone's rows by kind, each in the log's append order."""

    visits: list[Event]
    motions: list[Event]
    health: list[Event]


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
    """The phone's visits, motion and Health rows reported at or after ``since_epoch_ms``.

    One indexed read of the three types. The caller reaches back :data:`LOOKBACK_MS` before the
    window it folds.
    """
    rows = PhoneRows([], [], [])
    by_type = {VISIT_EVENT: rows.visits, MOTION_EVENT: rows.motions, HEALTH_EVENT: rows.health}
    for event in iter_events_of_types(conn, tuple(by_type), since_epoch_ms=since_epoch_ms):
        by_type[event.type].append(event)
    return rows


def stamp(conn: sqlite3.Connection, since_epoch_ms: int, until_epoch_ms: int) -> tuple[int, int]:
    """``(count, newest id)`` of the phone rows timed in ``[since, until)``, without decoding any.

    Changes exactly when a row for that stretch of time arrives, so it keys a cache of a text
    built from those rows.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM events WHERE type IN (?, ?, ?) "
        "AND ts_epoch_ms >= ? AND ts_epoch_ms < ?",
        (VISIT_EVENT, MOTION_EVENT, HEALTH_EVENT, since_epoch_ms, until_epoch_ms),
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


def stays(events: list[Event]) -> list[Span]:
    """Where he was: the visits, with an arrival that never got a departure ended by the next."""
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


def _reach(end: int | None) -> float:
    return math.inf if end is None else end


def moves(events: list[Event], held: list[Span], now_ms: int) -> list[Span]:
    """How he got about: walking, running, cycling and driving, de-flickered.

    A segment ends where it says, else where the next segment of any activity starts, so a
    stationary or doubtful segment ends the move before it. Segments of one activity that touch
    within :data:`MOVE_MERGE_GAP_MS` merge; short moves and moves inside one stay (``held``) are
    dropped.
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
        and not any(
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


def _payloads(
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
    for row in inside(_payloads(conn, HEALTH_EVENT, "steps", start_ms, until)):
        steps[(int(row["start_ms"]), int(row["end_ms"]))] = float(row["value"])
    resting = inside(_payloads(conn, MEASURE_EVENT, "resting_heart_rate", start_ms, until))
    rows = _payloads(conn, MEASURE_EVENT, "heart_rate_variability_sdnn", start_ms, until)
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
