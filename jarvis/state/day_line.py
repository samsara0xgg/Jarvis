"""ADR 0199: one day of the owner's life as timed items, folded from the event log when asked.

Nothing is stored: each call reads the log and folds the day. A day is the local midnight to the
next local midnight in a zone (:func:`day_window`); an item that overlaps the window is returned
whole, with its real start and end, and an item with no end yet (``end_ms`` ``None``) is still
going, so it overlaps every window after its start.

Each source folds on its own and a source that raises leaves its items out (:func:`fold_day`):

- ``phone``: stays from ``phone.visit_observed``, moves from ``phone.motion_observed`` and sleep
  from ``phone.health_observed`` (ADR 0197), read in one query from two days before the window and
  folded by :mod:`jarvis.state.phone_day`, whose rules the ledger counts by too;
- ``reminders``: the fold of ADR 0179;
- ``mac``: work blocks and calls from TimeSink, read through the ledger's own loading
  (:func:`jarvis.state.ledger.window_activity`) so the numbers are the ones she is told;
- ``talks``: his conversations with her, from the records of his in memory.db.

A source whose store is not there (no TimeSink path or file, no memory store) is not an error: it
is named in ``missing`` without a log line. The thresholds below belong to the code, not to
settings.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import reminders, timesink
from jarvis.state.ledger import CALL_FLOOR_S, TOP_FLOOR_S, window_activity
from jarvis.state.phone_day import LOOKBACK_MS, read_rows, sleeps, stays
from jarvis.state.phone_day import moves as phone_moves

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.state.ledger import ActivitySpan, TerminalScreen

    type Source = Callable[[sqlite3.Connection, int, int, int], list[dict[str, Any]]]

LOGGER = logging.getLogger(__name__)

_MINUTE_MS: Final = 60_000

WORK_MERGE_GAP_MS: Final = 15 * _MINUTE_MS
"""Stretches of Mac activity this close (or overlapping) are one work block."""
WORK_MIN_S: Final = 5 * 60
"""A work block with less active time than this is dropped."""
WORK_TOP: Final = 3
"""Projects and apps named in a work block."""
TALK_MERGE_GAP_MS: Final = 10 * _MINUTE_MS
"""His utterances this close are one talk."""
BLOCK_REACH_MS: Final = 24 * 60 * _MINUTE_MS
"""How far either side of the window the Mac and his talks are read, so a block or a talk that
crosses a midnight is found whole; one that runs on longer than this is cut where it was read."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NO_PROJECT: Final = frozenset({"none", ""})

_DAY_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DaySources:
    """What the day reads beyond the event log. A path left None is a source not connected."""

    zone: tzinfo
    timesink: Path | None = None
    memory_db: Path | None = None
    terminal: TerminalScreen | None = None  # a brain's ``timesink``: the terminal's store


class _NotConnected(Exception):  # noqa: N818 — a source's answer, not an error
    """A source has no store to read: named in ``missing``, not logged."""


@dataclass(frozen=True)
class DayLine:
    """The items of one window, sorted by start then id, and the sources that could not be read."""

    items: list[dict[str, Any]]
    missing: list[str]


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


def fold_day(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, now_ms: int, sources: DaySources,
) -> DayLine:
    """Every item that overlaps ``[start_ms, end_ms)``, as of ``now_ms``.

    A source that has no store is named in ``missing``; one that raises is logged, and named too.
    Either contributes nothing.
    """
    folds: tuple[tuple[str, Source], ...] = (
        ("phone", _phone_items),
        ("reminders", _reminder_items),
        ("mac", lambda _conn, first, last, now: _mac_items(sources, first, last, now)),
        ("talks", lambda _conn, first, last, now: _talk_items(sources, first, last, now)),
    )
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, source in folds:
        try:
            items.extend(source(conn, start_ms, end_ms, now_ms))
        except _NotConnected:
            missing.append(name)
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


def _ms(moment: datetime) -> int:
    """Whole milliseconds since the epoch, exact (no float on the way)."""
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _moment(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


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


# --- the Mac and her conversations ---------------------------------------------------------


def _union_s(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    """Seconds covered by the union of the intervals, two at once counting once."""
    total, stop = 0.0, None
    for begin, end in sorted(intervals):
        if stop is None or begin > stop:
            total += (end - begin).total_seconds()
            stop = end
        elif end > stop:
            total += (end - stop).total_seconds()
            stop = end
    return total


def _tops(spans: list[ActivitySpan], name: Callable[[ActivitySpan], str]) -> list[dict[str, Any]]:
    """The few names that took the most of the block, each at least the ledger's floor."""
    by: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for span in spans:
        by[name(span)].append((span.start, span.end))
    ranked = sorted(
        ((key, _union_s(times)) for key, times in by.items() if key not in _NO_PROJECT),
        key=lambda item: (-item[1], item[0]),
    )
    return [
        {"name": key, "seconds": round(seconds)}
        for key, seconds in ranked
        if seconds >= TOP_FLOOR_S
    ][:WORK_TOP]


def _blocks(spans: list[ActivitySpan]) -> list[list[ActivitySpan]]:
    """Spans in start order, grouped: a span starting within the gap of the group's end joins it.

    This is the union of the spans, as the ledger counts it, with the unions that sit within
    :data:`WORK_MERGE_GAP_MS` of each other merged.
    """
    gap = timedelta(milliseconds=WORK_MERGE_GAP_MS)
    groups: list[list[ActivitySpan]] = []
    stop: datetime | None = None
    for span in sorted(spans, key=lambda one: (one.start, one.end)):
        if stop is not None and span.start - stop <= gap:
            groups[-1].append(span)
            stop = max(stop, span.end)
        else:
            groups.append([span])
            stop = span.end
    return groups


def _mac_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Work blocks and calls from TimeSink, in the lane of him."""
    path = sources.timesink
    if path is None and sources.terminal is None:
        raise _NotConnected
    if path is not None:
        with timesink.snapshot(path) as snap:
            opened = snap is not None
        if not opened:
            raise _NotConnected
    missed = sources.terminal.misses if sources.terminal else 0
    activity = window_activity(
        _moment(window_start - BLOCK_REACH_MS), _moment(window_end + BLOCK_REACH_MS),
        sources.zone, timesink=path, terminal=sources.terminal,
    )
    if sources.terminal and sources.terminal.misses != missed:
        raise _NotConnected  # the terminal is away, or has no store
    items: list[dict[str, Any]] = []
    for group in _blocks(activity.spans):
        active = _union_s((span.start, span.end) for span in group)
        start, end = _ms(group[0].start), _ms(max(span.end for span in group))
        if active < WORK_MIN_S or not _overlaps(start, end, window_start, window_end):
            continue
        items.append({
            "id": f"work-{start}",
            "lane": "you",
            "kind": "work",
            "start_ms": start,
            "end_ms": end,
            "state": _state(start, end, now_ms),
            "active_s": round(active),
            "projects": _tops(group, lambda span: span.project),
            "apps": _tops(group, lambda span: span.app),
        })
    same_start: dict[int, int] = {}  # calls counted at each start, to keep their ids apart
    for call in dict.fromkeys(activity.calls):
        start, end = _ms(call.start), _ms(call.end)
        if (call.end - call.start).total_seconds() < CALL_FLOOR_S:
            continue
        same_start[start] = same_start.get(start, 0) + 1
        if not _overlaps(start, end, window_start, window_end):
            continue
        nth = same_start[start]
        items.append({
            "id": f"call-{start}" if nth == 1 else f"call-{start}-{nth}",
            "lane": "you",
            "kind": "call",
            "start_ms": start,
            "end_ms": end,
            "state": _state(start, end, now_ms),
            "app": call.app,
        })
    return items


def _talk_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """His conversations with her, in her lane: his utterances in runs of at most ten minutes."""
    path = sources.memory_db
    if path is None:
        raise _NotConnected
    activity = window_activity(
        _moment(window_start - BLOCK_REACH_MS), _moment(window_end + BLOCK_REACH_MS),
        sources.zone, memory_db=path,
    )
    talks: list[list[int]] = []
    for said in sorted(_ms(at) for at in activity.talks):
        if talks and said - talks[-1][-1] <= TALK_MERGE_GAP_MS:
            talks[-1].append(said)
        else:
            talks.append([said])
    return [
        {
            "id": f"talk-{run[0]}",
            "lane": "her",
            "kind": "talk",
            "start_ms": run[0],
            "end_ms": run[-1],
            "state": _state(run[0], run[-1], now_ms),
            "turns": len(run),
        }
        for run in talks
        if _overlaps(run[0], run[-1], window_start, window_end)
    ]


# --- the phone -----------------------------------------------------------------------------


def _phone_items(
    conn: sqlite3.Connection, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Stays, moves and sleep: one read of the phone's rows, folded by kind."""
    rows = read_rows(conn, window_start - LOOKBACK_MS)
    held = stays(rows.visits)
    spans = [*held, *phone_moves(rows.motions, held, now_ms), *sleeps(rows.health)]
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
