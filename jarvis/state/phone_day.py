"""The phone's part of a day, folded from the event log (ADR 0197, ADR 0199).

One read of the phone's rows (:func:`read_rows`) and the folds that turn them into stays, moves and
sleep, with the thresholds that decide them. The day line (ADR 0199, :mod:`jarvis.state.day_line`)
serves these as timed items and the ledger (ADR 0201, :mod:`jarvis.state.ledger`) counts them into
the prompt, so the two cannot disagree about where he stayed or when he slept: they share these
rules. Nothing is stored and nothing here calls a model.
"""

from __future__ import annotations

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
    """Sleep as blocks: spans that overlap or sit within :data:`SLEEP_MERGE_GAP_MS` are one."""
    spans = sorted(
        (int(event.payload["start_ms"]), int(event.payload["end_ms"]), place, event)
        for place, event in enumerate(
            e for e in events if e.payload["metric"] == "sleep_min"
        )
    )
    blocks: list[Span] = []
    for start, end, place, event in spans:
        origin = (place, event.event_uid)
        last = blocks[-1] if blocks else None
        if last is not None and last.end is not None and start <= last.end + SLEEP_MERGE_GAP_MS:
            blocks[-1] = replace(
                last,
                origin=min(last.origin, origin),
                end=max(last.end, end),
                fields={"minutes": last.fields["minutes"] + event.payload["value"]},
            )
        else:
            blocks.append(Span(origin, "sleep", start, end, {"minutes": event.payload["value"]}))
    return [
        replace(block, fields={"minutes": _tidy(block.fields["minutes"])}) for block in blocks
    ]


def _tidy(total: float) -> float:
    """A whole sum as an integer, any other rounded to hundredths of a minute."""
    return int(total) if float(total).is_integer() else round(total, 2)
