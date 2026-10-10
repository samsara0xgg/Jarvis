"""ADR 0199: one day of the owner's life as timed items, folded from the event log when asked.

Nothing is stored: each call reads the log and folds the day. A day is the local midnight to the
next local midnight in a zone (:func:`day_window`); an item that overlaps the window is returned
whole, with its real start and end, and an item with no end yet (``end_ms`` ``None``) is still
going, so it overlaps every window after its start.

Each source folds on its own and a source that raises leaves its items out (:func:`fold_day`):

- ``phone``: stays from ``phone.visit_observed``, moves from ``phone.motion_observed`` and sleep
  from ``phone.health_observed`` (ADR 0197), read in one query from two days before the window;
- ``reminders``: the fold of ADR 0179.

Mac work blocks and conversations are not folded here yet. The thresholds below belong to the
code, not to settings.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import reminders
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.phone_location import VISIT_EVENT

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping
    from datetime import tzinfo

    from jarvis.shared import Event

    type Source = Callable[[sqlite3.Connection, int, int, int], list[dict[str, Any]]]

LOGGER = logging.getLogger(__name__)

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

_DAY_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DayLine:
    """The items of one window, sorted by start then id, and the sources that could not be read."""

    items: list[dict[str, Any]]
    missing: list[str]


class _Fact(NamedTuple):
    """What a phone reported about one start time, once its rows are settled."""

    origin: tuple[int, str]
    start: int
    end: int | None
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _Span:
    """One item of the phone's lane before it is worded as JSON."""

    origin: tuple[int, str]  # (place in the rows read, event_uid) of the first row it came from
    kind: str
    start: int
    end: int | None
    fields: dict[str, Any]


def parse_day(text: str) -> date:
    """The calendar day ``YYYY-MM-DD`` names.

    Raises:
        ValueError: ``text`` is any other form of a date, or not a real day.
    """
    if _DAY_TEXT.fullmatch(text) is None:
        msg = "date must be YYYY-MM-DD"
        raise ValueError(msg)
    try:
        return date.fromisoformat(text)
    except ValueError:
        msg = "date must be YYYY-MM-DD"
        raise ValueError(msg) from None


def day_window(day: date, zone: tzinfo) -> tuple[int, int]:
    """``(start_ms, end_ms)``: the local midnights that bound ``day`` in ``zone``.

    Each midnight is cut on the calendar, so a day with a clock change is 23 or 25 hours long.

    Raises:
        ValueError: the day is too near the ends of the calendar to have epoch times.
    """
    try:
        first = datetime.combine(day, time.min, tzinfo=zone)
        after = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        return int(first.timestamp()) * 1000, int(after.timestamp()) * 1000
    except (OverflowError, ValueError, OSError):
        msg = "date is out of range"
        raise ValueError(msg) from None


def fold_day(conn: sqlite3.Connection, start_ms: int, end_ms: int, now_ms: int) -> DayLine:
    """Every item that overlaps ``[start_ms, end_ms)``, as of ``now_ms``.

    A source that raises is logged, contributes nothing and is named in ``missing``.
    """
    sources: tuple[tuple[str, Source], ...] = (
        ("phone", _phone_items),
        ("reminders", _reminder_items),
    )
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, source in sources:
        try:
            items.extend(source(conn, start_ms, end_ms, now_ms))
        except Exception:
            LOGGER.exception("day line: the %s source could not be read", name)
            missing.append(name)
    items.sort(key=lambda item: (item["start_ms"], item["id"]))
    return DayLine(items, missing)


def _state(start: int, end: int | None, now_ms: int) -> str:
    if start > now_ms:
        return "next"
    return "now" if end is None or end > now_ms else "done"


def _overlaps(start: int, end: int | None, window_start: int, window_end: int) -> bool:
    if start >= window_end:
        return False
    return start >= window_start or end is None or end > window_start


def _reach(end: int | None) -> float:
    return math.inf if end is None else end


# --- the reminders -------------------------------------------------------------------------


def _reminder_items(
    conn: sqlite3.Connection, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each reminder that is due in the window and not cancelled, in the lane of her."""
    items: list[dict[str, Any]] = []
    for one in reminders.fold(conn).values():
        if one.cancelled or not window_start <= one.due_at_ms < window_end:
            continue
        fired = one.fired_at_ms is not None
        items.append({
            "id": one.reminder_id,
            "lane": "her",
            "kind": "reminder",
            "start_ms": one.due_at_ms,
            "end_ms": None,
            "state": "done" if fired else _state(one.due_at_ms, None, now_ms),
            "text": one.text,
            "fired": fired,
            "acknowledged": one.acknowledged,
        })
    return items


# --- the phone -----------------------------------------------------------------------------


def _phone_items(
    conn: sqlite3.Connection, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Stays, moves and sleep: one read of the phone's rows, folded by kind."""
    rows: dict[str, list[Event]] = {VISIT_EVENT: [], MOTION_EVENT: [], HEALTH_EVENT: []}
    for event in iter_events_of_types(
        conn, tuple(rows), since_epoch_ms=window_start - LOOKBACK_MS,
    ):
        rows[event.type].append(event)
    stays = _stays(rows[VISIT_EVENT])
    spans = [*stays, *_moves(rows[MOTION_EVENT], stays, now_ms), *_sleeps(rows[HEALTH_EVENT])]
    return [
        {
            "id": span.origin[1],
            "lane": "you",
            "kind": span.kind,
            "start_ms": span.start,
            "end_ms": span.end,
            "state": _state(span.start, span.end, now_ms),
            **span.fields,
        }
        for span in spans
        if _overlaps(span.start, span.end, window_start, window_end)
    ]


def _settle(events: list[Event], start_key: str, end_key: str) -> list[_Fact]:
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
    facts: list[_Fact] = []
    for start, event in sorted(kept.items()):
        end = event.payload.get(end_key)
        facts.append(_Fact(first[start], start, None if end is None else int(end), event.payload))
    return facts


def _stays(events: list[Event]) -> list[_Span]:
    """Where he was: the visits, with an arrival that never got a departure ended by the next."""
    facts = _settle(events, "arrived_at_ms", "departed_at_ms")
    stays: list[_Span] = []
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
        stays.append(_Span(fact.origin, "stay", fact.start, end, fields))
    return stays


def _moves(events: list[Event], stays: list[_Span], now_ms: int) -> list[_Span]:
    """How he got about: walking, running, cycling and driving, de-flickered.

    A segment ends where it says, else where the next segment of any activity starts, so a
    stationary or doubtful segment ends the move before it. Segments of one activity that touch
    within :data:`MOVE_MERGE_GAP_MS` merge; short moves and moves inside one stay are dropped.
    """
    facts = _settle(events, "started_at_ms", "ended_at_ms")
    merged: list[_Span] = []
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
            merged.append(_Span(fact.origin, "move", fact.start, end, {"activity": activity}))
    return [
        move
        for move in merged
        if (now_ms if move.end is None else move.end) - move.start >= MOVE_MIN_MS
        and not any(
            stay.start <= move.start and _reach(move.end) <= _reach(stay.end) for stay in stays
        )
    ]


def _sleeps(events: Iterable[Event]) -> list[_Span]:
    """Sleep as blocks: spans that overlap or sit within :data:`SLEEP_MERGE_GAP_MS` are one."""
    spans = sorted(
        (int(event.payload["start_ms"]), int(event.payload["end_ms"]), place, event)
        for place, event in enumerate(
            e for e in events if e.payload["metric"] == "sleep_min"
        )
    )
    blocks: list[_Span] = []
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
            blocks.append(_Span(origin, "sleep", start, end, {"minutes": event.payload["value"]}))
    return [
        replace(block, fields={"minutes": _tidy(block.fields["minutes"])}) for block in blocks
    ]


def _tidy(total: float) -> float:
    """A whole sum as an integer, any other rounded to hundredths of a minute."""
    return int(total) if float(total).is_integer() else round(total, 2)
