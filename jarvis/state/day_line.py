"""ADR 0199: one day of the owner's life as timed items, folded from the event log when asked.

Nothing is stored: each call reads the log and folds the day. A day is the local midnight to the
next local midnight in a zone (:func:`day_window`); an item that overlaps the window is returned
whole, with its real start and end, and an item with no end yet (``end_ms`` ``None``) is still
going, so it overlaps every window after its start.

Each source folds on its own and a source that raises leaves its items out (:func:`fold_day`):

- ``phone``: stays from ``phone.visit_observed`` and, where no visit covers the time, cut from
  ``phone.location_observed`` (ADR 0301), moves from ``phone.motion_observed`` and from the fixes
  between stays (ADR 0316), and sleep
  and workouts from ``phone.health_observed`` (ADR 0197, 0293), read in one query from two days
  before the window and folded by :mod:`jarvis.state.phone_day`, whose rules the ledger counts by
  too; a stay inside a place he keeps (ADR 0234) also carries it as ``kept_place``, a sleep block
  its ``stages`` and a workout its ``activity``, ``energy_kcal``, ``distance_m`` and ``avg_hr``
  where Health knew them;
- ``reminders``: the fold of ADR 0179;
- ``rides``: the bus rides he was on (ADR 0224); one replaces the phone's automotive or unnamed
  ``move`` it covers for half its span or more, and takes its time from every other stay and
  move (ADR 0330), so no two items of the place line share a minute;
- ``mac``: work blocks and calls from TimeSink, read through the ledger's own loading
  (:func:`jarvis.state.ledger.window_activity`) so the numbers are the ones she is told; when
  TimeSink cannot be read live, or has nothing for the stretch, the rows the Mac terminal shipped
  to the brain (ADR 0294, :func:`jarvis.state.ledger.shipped_activity`) give the same items;
- ``talks``: his conversations with her, from the records of his in memory.db;
- ``pictures``: each picture he sent her, at the time of the turn it came with and with the stay
  that covers that time, and its note once written (ADR 0223). With no memory store and no
  attachment store it has nothing to read and says nothing in ``missing``;
- ``commitments``: what he has to go to or do at a time (ADR 0274, 0299), the occurrences of the
  ledger of his things in the event log, one item per occurrence in his lane as kind ``event``
  (every build of the phone draws it), with ``source`` (where the ledger had it from: said by
  him, read from mail, or mirrored from his Outlook, so his calendar reaches the line only
  here), ``confirmed``, ``private`` and ``place``; its id is ``commitment-<hex>@<day>``,
  ``<hex>`` being the hex of the thing's id; an all-day one (a date and no hour) is a label of
  the day (:attr:`DayLine.labels`) instead of an item, with the same fields;
- ``plan``: a day with a plan (ADR 0288) draws its items in his lane as kind ``event`` with
  ``source`` ``plan``, id ``plan-<date>-<item id>``, from its start (or its window's start) for
  its minutes, with ``place`` and ``private``; an item with no time is left out, and so is one
  that stands for a thing drawn above (``source`` ``item:*``, or ``commitment:*`` in a plan from
  before ADR 0299), which is drawn once, as the thing;
- ``imports``: what he handed over (ADR 0259), in his lane: each payment that moved money as a
  point (amount, direction, counterparty, item), and each conversation with Claude as a span from
  its first to its last message of the window (the title as text), and each day's nightly letter
  on his Claude chats (ADR 0295) as kind ``claude_day``, a span over the chats' times with their
  titles as text and ``chats`` their number, and each day's report on his Claude Code work
  (ADR 0303) the same way, id ``claude-code-day-<date>``, ``source`` ``claude_code_daily``, the
  projects' titles as text and ``projects`` their number; with no memory store it has nothing to
  read and says nothing in ``missing``;
- ``code``: his Claude Code sessions, from what the Mac terminal shipped (ADR 0294), in his lane:
  one item per session, from its first to its last message clipped to the day (a session across
  midnight is on both days under one id), with the best of its titles, its project, its account
  and Claude Code's latest recap as ``detail``; a sidechain is left out. With no shipped store
  it has nothing to read and says nothing in ``missing``;
- ``away_note``: what he wrote in TimeSink for a stretch he was away from his Mac, from what the
  Mac terminal shipped (ADR 0294), in his lane: one item per note, its stretch clipped to the
  day, its words as ``title``. One line for a whole stretch, it is a clue to what he was doing
  (ADR 0302), not his answer for all of it. With no shipped store it says nothing in
  ``missing``;
- ``screen``: the apps in the foreground of his iPhone and iPad, from what the Mac terminal
  shipped as ``device-apps`` (ADR 0304), in his lane: one item per block of use on one device,
  stretches of any app joined when the gap between them is at most five minutes, blocks with
  less than five minutes of foreground time dropped. ``device`` is "iPhone" or "iPad",
  ``active_s`` the foreground seconds inside the block (not end minus start), ``apps`` the five
  with the most time, each ``{name, bundle, minutes, category}`` (name and category from the
  newest ``app_name`` row of the bundle, else the bundle id and ``None``). The devices' records
  reach the Mac late, so :attr:`DayLine.synced` names, for each device that is behind the end of
  the window (or now), the time its newest record is from: past it the device is not known, which
  is not the same as not used. With a shipped store but no device record at all, ``screen`` is in
  ``missing``; with no shipped store it says nothing;
- ``device_records``: what else the same sync brings, from what the Mac terminal shipped as
  ``device-records`` (ADR 0305), in his lane, folded by :mod:`jarvis.state.device_day`: ``media``
  (an app playing sound on a device), ``earbuds`` (Bluetooth earbuds connected), ``messages``
  (a burst of messages in one conversation) and ``phone_call`` (a call Siri remembers; ``call``
  stays TimeSink's). Their devices' records reach the Mac late too. With no such row, or no
  shipped store, it has nothing to draw and says nothing in ``missing`` (``screen`` is the one that
  tells the devices are not reaching the Mac);
- ``departures``: the bus trips he pinned (ADR 0204), each a point at its departure time, from the
  pin as the log held it at the end of the day; the reminder that rings for a trip on the line is
  left out, the departure stands for it.

A source whose store is not there (no TimeSink path or file, no memory store) is not an error: it
is named in ``missing`` without a log line. The thresholds below belong to the code, not to
settings.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import (
    commitments,
    departures,
    device_day,
    imports,
    picture_notes,
    places,
    plan,
    reminders,
    things,
    timesink,
)
from jarvis.state import rides as ride_state
from jarvis.state.attachments import Attachments
from jarvis.state.import_parsers import money
from jarvis.state.ledger import CALL_FLOOR_S, TOP_FLOOR_S, shipped_activity, window_activity
from jarvis.state.phone_day import (
    CUT_FROM_KEY,
    LOOKBACK_MS,
    Span,
    read_rows,
    sleeps,
    stays,
    uncovered,
    workouts,
)
from jarvis.state.phone_day import moves as phone_moves

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.state.ledger import Activity, ActivitySpan, CachedScreen, TerminalScreen

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

PICTURE_TURN_EVENTS: Final = ("surface.user_intent", "utterance.received", "user.shared")
"""The rows that name files he sent: the row that opens a turn, or a saved share."""

SCREEN_MERGE_GAP_MS: Final = 5 * _MINUTE_MS
"""Stretches of use on one device this close (or overlapping) are one block."""
SCREEN_MIN_S: Final = 5 * 60
"""A block with less foreground time than this is dropped."""
SCREEN_TOP: Final = 5
"""Apps named in a block."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NO_PROJECT: Final = frozenset({"none", ""})

_DAY_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DaySources:
    """What the day reads beyond the event log. A path left None is a source not connected."""

    zone: tzinfo
    timesink: Path | None = None
    memory_db: Path | None = None
    # a brain's ``timesink``: the terminal's store
    terminal: TerminalScreen | CachedScreen | None = None
    attachments: Path | None = None  # where his pictures are kept (ADR 0223)
    # the imports directory (ADR 0294), whose shipped.db holds what the Mac terminal copied in:
    # TimeSink when the live read has nothing, and the Claude Code sessions. None: not connected.
    shipped: Path | None = None


class _NotConnected(Exception):  # noqa: N818 — a source's answer, not an error
    """A source has no store to read: named in ``missing``, not logged."""


@dataclass(frozen=True)
class DayLine:
    """The items of one window, sorted by start then id, and the sources that could not be read.

    ``labels`` are what the day carries without a stretch of time: each all-day occurrence of a
    thing, ``{title, source, confirmed, private, place, id}``. ``synced`` maps a device
    ("iPad") to the time of its newest record when that is before the window's end or now:
    the ``screen`` items of that device are known only up to it (ADR 0304).
    """

    items: list[dict[str, Any]]
    missing: list[str]
    labels: list[dict[str, Any]] = field(default_factory=list)
    synced: dict[str, int] = field(default_factory=dict)


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
    Either contributes nothing. A source may return rows of kind ``label`` (an all-day event): they
    are not items but the day's ``labels``.
    """
    folds: tuple[tuple[str, Source], ...] = (
        ("phone", lambda db, first, last, now: _phone_items(db, sources, first, last, now)),
        ("reminders", _reminder_items),
        ("rides", _ride_items),
        ("mac", lambda _conn, first, last, now: _mac_items(sources, first, last, now)),
        ("talks", lambda _conn, first, last, now: _talk_items(sources, first, last, now)),
        ("pictures", lambda db, first, last, now: _picture_items(db, sources, first, last, now)),
        (
            "commitments",
            lambda db, first, last, now: _commitment_items(db, sources, first, last, now),
        ),
        ("departures", _departure_items),
        ("plan", lambda db, first, last, now: _plan_items(db, sources, first, last, now)),
        ("imports", lambda _conn, first, last, now: _import_items(sources, first, last, now)),
        ("code", lambda _conn, first, last, now: _code_items(sources, first, last, now)),
        ("away", lambda _conn, first, last, now: _away_items(sources, first, last, now)),
        ("screen", lambda _conn, first, last, now: _screen_items(sources, first, last, now)),
        (
            "device_records",
            lambda _conn, first, last, now: _record_items(sources, first, last, now),
        ),
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
    labels = [
        {k: row[k] for k in ("title", "source", "confirmed", "private", "place", "id")}
        for row in items if row["kind"] == "label"
    ]
    items = _without_pin_rings([row for row in items if row["kind"] != "label"])
    items = _around_rides(items, start_ms, end_ms, now_ms)
    items.sort(key=lambda item: (item["start_ms"], item["id"]))
    try:
        synced = _screen_synced(sources, min(end_ms, now_ms))
    except Exception:
        LOGGER.exception("day line: the sync times of his devices could not be read")
        synced = {}
    return DayLine(items, missing, labels, synced)


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
        if one.reminder_id.startswith((things.RING_PREFIX, commitments.REMINDER_PREFIX)):
            continue  # a thing's ring (or a commitment's, before ADR 0299): the thing stands for it
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


# --- the bus rides -------------------------------------------------------------------------


def _ride_items(
    conn: sqlite3.Connection, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each bus ride in the window, in the lane of him: route, stops, how it began and ended."""
    return [
        {
            "id": one.ride_id,
            "lane": "you",
            "kind": "ride",
            "start_ms": one.started_at_ms,
            "end_ms": one.ended_at_ms,
            "state": _state(one.started_at_ms, one.ended_at_ms, now_ms),
            "route": one.route,
            "board_stop": one.board_stop,
            "alight_stop": one.alight_stop,
            "source": one.source,
            "ended_reason": one.end_reason,
            "credit": "BC Transit" if one.trip_id else None,
        }
        for one in ride_state.fold(conn, window_start - ride_state.LOOKBACK_MS)
        if _overlaps(one.started_at_ms, one.ended_at_ms, window_start, window_end)
    ]


def _around_rides(
    items: list[dict[str, Any]], window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """The stays and moves less the time his rides cover (ADR 0224, ADR 0330).

    An automotive or unnamed move a ride covers for half its span or more is that ride, and is
    dropped. Every other stay and move gives the ride the time they share: each piece left that
    overlaps the window keeps its fields and state for its own span, the first keeps the id and a
    later one adds ``@`` and its start; one with nothing left is dropped.
    """
    rides = [
        Span((0, str(i["id"])), "ride", i["start_ms"], i["end_ms"], {})
        for i in items if i["kind"] == "ride"
    ]
    if not rides:
        return items

    def ridden(move: dict[str, Any]) -> bool:
        start, end = move["start_ms"], now_ms if move["end_ms"] is None else move["end_ms"]
        shared = sum(
            max(0, min(end, now_ms if r.end is None else r.end) - max(start, r.start))
            for r in rides
        )
        return move["kind"] == "move" and move.get("activity") in {None, "automotive"} and (
            2 * shared >= end - start
        )

    kept: list[dict[str, Any]] = []
    for item in items:
        if item["kind"] not in {"stay", "move"}:
            kept.append(item)
        elif not ridden(item):
            kept.extend(
                {
                    **item, "id": item["id"] if number == 0 else f"{item['id']}@{first}",
                    "start_ms": first, "end_ms": last, "state": _state(first, last, now_ms),
                }
                for number, (first, last) in enumerate(
                    uncovered(item["start_ms"], item["end_ms"], rides),
                )
                if _overlaps(first, last, window_start, window_end)
            )
    return kept


# --- the things and the pinned departures --------------------------------------------------


def _commitment_items(
    conn: sqlite3.Connection, sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each occurrence of a thing in the window: an ``event``, a ``label`` if all-day (ADR 0299).

    The id keeps the shape the phone has: ``commitment-<hex>@<day>`` for ``thing-<hex>``.
    """
    return [
        {
            "id": f"{commitments.ID_PREFIX}{occ.thing.thing_id.removeprefix(things.ID_PREFIX)}"
            f"@{occ.day}",
            "lane": "you",
            "kind": "label" if occ.all_day else "event",
            "start_ms": occ.start_ms,
            "end_ms": occ.end_ms,
            "state": _state(occ.start_ms, occ.end_ms, now_ms),
            "title": occ.thing.title,
            "source": occ.thing.source,
            "confirmed": occ.thing.confirmed,
            "private": occ.thing.private,
            "place": occ.thing.place,
        }
        for occ in things.occurrences(
            conn, window_start, window_end, zone=sources.zone, closed=True,
        )
    ]


def _plan_items(
    conn: sqlite3.Connection, sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each timed item of a day's plan that overlaps the window, as an ``event`` (ADR 0288)."""
    first = datetime.fromtimestamp(window_start / 1000, sources.zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(window_end / 1000, sources.zone).date()
    rows: list[dict[str, Any]] = []
    timed = {
        one.thing_id for one in things.fold(conn).values() if one.start_at or one.on_day
    }
    for held in plan.fold(conn).values():
        if not first <= held.day <= last:
            continue
        for item in held.items:
            clock = item.start or item.window_from
            if clock is None or plan.thing_of(item.source) in timed:
                continue
            begin = datetime.combine(held.day, time.fromisoformat(clock), tzinfo=sources.zone)
            start, end = _ms(begin), _ms(begin + timedelta(minutes=item.minutes))
            if not _overlaps(start, end, window_start, window_end):
                continue
            rows.append({
                "id": f"plan-{held.day.isoformat()}-{item.item_id}",
                "lane": "you",
                "kind": "event",
                "start_ms": start,
                "end_ms": end,
                "state": _state(start, end, now_ms),
                "title": item.title,
                "source": "plan",
                "private": item.private,
                "place": item.place,
            })
    return rows


def _departure_items(
    conn: sqlite3.Connection, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each trip of the pin that leaves in the window, in his lane, as a point at its departure.

    The pin is the one the log held at the end of the window, so a past day shows what was pinned
    then. Each row carries ``_rings``, the reminders that rang for the trip, for
    :func:`_without_pin_rings`.
    """
    pin = departures.current(conn, until_ms=window_end)
    if pin is None:
        return []
    return [
        {
            "id": f"departure-{trip.tid}",
            "lane": "you",
            "kind": "departure",
            "start_ms": trip.departs_at_ms,
            "end_ms": trip.departs_at_ms,
            "state": _state(trip.departs_at_ms, trip.departs_at_ms, now_ms),
            "title": f"{trip.route} · {trip.board_stop}",
            "route": trip.route,
            "board_stop": trip.board_stop,
            "to": trip.to,
            "leave_at_ms": trip.leave_at_ms,
            "_rings": [one for one, tid in pin.rings.items() if tid == trip.tid],
        }
        for trip in pin.trips
        if window_start <= trip.departs_at_ms < window_end
    ]


def _without_pin_rings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop each reminder that rings for a departure on the line, and the ``_rings`` markers.

    The pin's alarm is an ordinary reminder (ADR 0204); the departure stands for it (ADR 0230).
    """
    rung: set[str] = set()
    for item in items:
        if item["kind"] == "departure":
            rung.update(item.pop("_rings"))
    return [item for item in items if item["kind"] != "reminder" or item["id"] not in rung]


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


def _live_activity(sources: DaySources, begin: datetime, end: datetime) -> Activity | None:
    """The Mac's activity read live (TimeSink here, or his terminal's); ``None`` when it is away."""
    path = sources.timesink
    if path is None and sources.terminal is None:
        return None
    if path is not None:
        with timesink.snapshot(path) as snap:
            opened = snap is not None
        if not opened:
            return None
    missed = sources.terminal.misses if sources.terminal else 0
    activity = window_activity(begin, end, sources.zone, timesink=path, terminal=sources.terminal)
    if sources.terminal and sources.terminal.misses != missed:
        return None  # the terminal is away, or has no store
    return activity


def shipped_conn(sources: DaySources) -> sqlite3.Connection | None:
    """A read-only connection to what the Mac shipped (ADR 0294), or ``None`` without the file."""
    if sources.shipped is None:
        return None
    path = sources.shipped / imports.SHIPPED_DB
    if not path.is_file():
        return None
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)


def _mac_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Work blocks and calls from TimeSink, in the lane of him.

    Read live when the Mac can be; when it cannot, or it answers with nothing for the stretch
    (it has dropped the day), from the rows the terminal shipped to the brain (ADR 0294).
    """
    lo, hi = _moment(window_start - BLOCK_REACH_MS), _moment(window_end + BLOCK_REACH_MS)
    activity = _live_activity(sources, lo, hi)
    if activity is None or not (activity.spans or activity.calls):
        conn = shipped_conn(sources)
        if conn is not None:
            with closing(conn):
                activity = shipped_activity(conn, lo, hi) or activity
    if activity is None:
        raise _NotConnected
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


# --- what he handed over -------------------------------------------------------------------


def _import_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """His payments as points, his Claude chats as spans and his Claude days, from the imports."""
    path = sources.memory_db
    if path is None or not path.is_file():
        return []  # no memory store: ``talks`` already says so in ``missing``
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = imports.entries(
            conn, start_ms=window_start, end_ms=window_end, kinds=("payment", "message"),
        )
        days = imports.latest_daily(conn, window_start, window_end)
        code_days = imports.latest_daily(conn, window_start, window_end, "claude_code_daily")
    items: list[dict[str, Any]] = []
    chats: dict[str, list[imports.Entry]] = defaultdict(list)
    for row in rows:
        if row.kind == "message":
            chats[str(row.fields.get("conversation"))].append(row)
        elif row.fields.get("counts"):
            fields = row.fields
            items.append({
                "id": f"payment-{row.id}",
                "lane": "you",
                "kind": "payment",
                "start_ms": row.at_ms,
                "end_ms": row.at_ms,
                "state": _state(row.at_ms, row.at_ms, now_ms),
                "source": row.source,
                "amount": money(fields["amount_fen"]),
                "currency": fields["currency"],
                "direction": fields["direction"],
                "counterparty": fields["counterparty"],
                "text": fields["item"],
            })
    for row in days:  # the night's letter on his Claude chats (ADR 0295): the newest one a day
        first, last = int(row.fields["start_ms"]), int(row.fields["end_ms"])
        items.append({
            "id": f"claude-day-{row.fields['date']}",
            "lane": "you",
            "kind": "claude_day",
            "start_ms": first,
            "end_ms": last,
            "state": _state(first, last, now_ms),
            "source": "claude_daily",
            "text": "; ".join(row.fields["titles"]) or row.text[:120],
            "chats": int(row.fields["sections"]),
        })
    for row in code_days:  # the day's report on his Claude Code work (ADR 0303), by project
        first, last = int(row.fields["start_ms"]), int(row.fields["end_ms"])
        items.append({
            "id": f"claude-code-day-{row.fields['date']}",
            "lane": "you",
            "kind": "claude_day",
            "start_ms": first,
            "end_ms": last,
            "state": _state(first, last, now_ms),
            "source": "claude_code_daily",
            "text": "; ".join(row.fields["titles"]) or row.text[:120],
            "projects": int(row.fields["sections"]),
        })
    for chat, messages in chats.items():
        first, last = min(m.at_ms for m in messages), max(m.at_ms for m in messages)
        items.append({
            "id": f"claude-{chat}-{first}",
            "lane": "you",
            "kind": "claude_chat",
            "start_ms": first,
            "end_ms": last,
            "state": _state(first, last, now_ms),
            "source": "claude",
            "text": messages[0].fields.get("conversation_name") or "Claude conversation",
            "messages": len(messages),
        })
    return items


# --- his Claude Code sessions --------------------------------------------------------------


def _newest(*lists: Any) -> str:  # noqa: ANN401 - a shipped record's fields, each a list or a text
    """The first non-empty of the lists' (or texts') newest values, else an empty text."""
    for one in lists:
        last = (one[-1:] or [""])[0] if isinstance(one, list) else one
        if last:
            return str(last)
    return ""


def _code_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each Claude Code session of the window as a ``code`` item, clipped to the day (ADR 0294)."""
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        rows = imports.newest(
            conn, "claude-code", end_ms=window_end, kinds=("session",), by_prefix=True,
        )
    items: list[dict[str, Any]] = []
    for row in rows:
        f = row.fields
        if f.get("is_sidechain") or not f.get("first_timestamp") or not f.get("last_timestamp"):
            continue
        first = _ms(datetime.fromisoformat(f["first_timestamp"]))
        last = _ms(datetime.fromisoformat(f["last_timestamp"]))
        if not _overlaps(first, last, window_start, window_end):
            continue
        start, end = max(first, window_start), min(last, window_end)
        item: dict[str, Any] = {
            "id": f"code-{f['session_id']}",
            "lane": "you",
            "kind": "code",
            "start_ms": start,
            "end_ms": end,
            "state": _state(start, end, now_ms),
            "title": _newest(
                f.get("custom_title"), f.get("desktop_title"), f.get("ai_title"),
                f.get("agent_name"),
            ),
            "project": _project(f),
            "prompts": int(f.get("user_prompts") or 0),
            "account": f["account"],
            "session_id": f["session_id"],
        }
        if recap := _newest([a.get("content") for a in f.get("away_summaries") or []]):
            item["detail"] = recap
        items.append(item)
    return items


def _project(fields: dict[str, Any]) -> str:
    """A session's project: its repo's name on its remote, else its repo's or folder's name.

    A worktree's folder is named for the task (``bridge-cse_…``); its remote names the project.
    """
    origin = str(fields.get("git_origin") or "").rstrip("/")
    if name := origin.rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git"):
        return name
    return PurePosixPath(fields.get("git_toplevel") or fields.get("cwd") or "").name


def _away_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Each note he wrote in TimeSink for a stretch away from his Mac, clipped to the day."""
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        rows = imports.newest(conn, "timesink", kinds=("awayNote",), by_prefix=True)
    items: list[dict[str, Any]] = []
    for row in rows:
        f = row.fields
        label = str(f.get("label") or "").strip()
        if not label or not f.get("start") or not f.get("end"):
            continue
        first = _ms(datetime.fromisoformat(timesink.moment(str(f["start"]))))
        last = _ms(datetime.fromisoformat(timesink.moment(str(f["end"]))))
        if last <= first or not _overlaps(first, last, window_start, window_end):
            continue
        start, end = max(first, window_start), min(last, window_end)
        items.append({
            "id": f"away-{f['id']}",
            "lane": "you",
            "kind": "away_note",
            "start_ms": start,
            "end_ms": end,
            "state": _state(start, end, now_ms),
            "title": label,
        })
    return items


# --- his iPhone and iPad -------------------------------------------------------------------


def _horizons(conn: sqlite3.Connection) -> dict[str, int]:
    """Each device's newest record time, as the Mac last shipped it (ADR 0304); by device name."""
    rows = imports.newest(
        conn, imports.DEVICE_SOURCE, kinds=(imports.DEVICE_HORIZON,), by_prefix=True,
    )
    newest: dict[str, int] = {}
    for row in rows:
        device = str(row.fields["device"])
        newest[device] = max(newest.get(device, 0), int(row.fields["synced_ms"]))
    return newest


def app_names(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The newest ``app_name`` row's fields of each bundle id (ADR 0304)."""
    return {
        str(row.fields["bundle_id"]): row.fields
        for row in imports.newest(
            conn, imports.DEVICE_SOURCE, kinds=(imports.DEVICE_NAME,), by_prefix=True,
        )
    }


def _screen_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Blocks of foreground use on one device, from the app stretches the Mac shipped (0304)."""
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        if not _horizons(conn):
            raise _NotConnected
        rows = imports.newest(
            conn, imports.DEVICE_SOURCE, start_ms=window_start - BLOCK_REACH_MS, end_ms=window_end,
            kinds=(imports.DEVICE_APP,), by_prefix=True,
        )
        names = app_names(conn)
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_device[str(row.fields["device_id"])].append(row.fields)
    items: list[dict[str, Any]] = []
    for stretches in by_device.values():
        stretches.sort(key=lambda f: f["start_ms"])
        blocks: list[list[dict[str, Any]]] = []
        reach = 0
        for one in stretches:
            if blocks and one["start_ms"] - reach <= SCREEN_MERGE_GAP_MS:
                blocks[-1].append(one)
                reach = max(reach, one["end_ms"])
            else:
                blocks.append([one])
                reach = one["end_ms"]
        items.extend(
            item for block in blocks
            if (item := _screen_block(block, names, window_start, window_end, now_ms)) is not None
        )
    return items


def _screen_block(
    block: list[dict[str, Any]], names: dict[str, dict[str, Any]],
    window_start: int, window_end: int, now_ms: int,
) -> dict[str, Any] | None:
    """One block of a device's stretches as a ``screen`` item, or ``None`` if it is too short."""
    start, end = block[0]["start_ms"], max(f["end_ms"] for f in block)
    active = sum(f["end_ms"] - f["start_ms"] for f in block) / 1000
    if active < SCREEN_MIN_S or not _overlaps(start, end, window_start, window_end):
        return None
    seconds: dict[str, float] = defaultdict(float)
    for f in block:
        seconds[f["bundle_id"]] += (f["end_ms"] - f["start_ms"]) / 1000
    top = sorted(seconds, key=lambda bundle: (-seconds[bundle], bundle))[:SCREEN_TOP]
    device = block[0]["device"]
    return {
        "id": f"screen-{device}-{start}",
        "lane": "you",
        "kind": "screen",
        "start_ms": start,
        "end_ms": end,
        "state": _state(start, end, now_ms),
        "device": device,
        "active_s": round(active),
        "apps": [
            {
                "name": names.get(bundle, {}).get("app") or bundle,
                "bundle": bundle,
                "minutes": max(1, round(seconds[bundle] / 60)),
                "category": names.get(bundle, {}).get("category"),
            }
            for bundle in top
        ],
    }


def _screen_synced(sources: DaySources, until_ms: int) -> dict[str, int]:
    """Each device whose newest record is before ``until_ms``, with that record's time."""
    conn = shipped_conn(sources)
    if conn is None:
        return {}
    with closing(conn):
        newest = _horizons(conn)
    return {device: at for device, at in sorted(newest.items()) if at < until_ms}


def _record_items(
    sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Media, earbuds, message bursts and Siri's calls, from the records the Mac shipped (0305)."""
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        def fields(kind: str, *, whole: bool = False) -> list[dict[str, Any]]:
            return [
                row.fields for row in imports.newest(
                    conn, imports.RECORDS_SOURCE, kinds=(kind,), by_prefix=True,
                    start_ms=None if whole else window_start - BLOCK_REACH_MS,
                    end_ms=None if whole else window_end + BLOCK_REACH_MS,
                )
            ]

        apps = {bundle: str(row.get("app") or bundle) for bundle, row in app_names(conn).items()}
        pods = fields(imports.RECORD_BLUETOOTH, whole=True)
        found = [
            *device_day.media_items(
                fields(imports.RECORD_NOW_PLAYING), apps, device_day.bluetooth_names(pods),
            ),
            *device_day.earbuds_items(pods, now_ms),
            *device_day.message_items(fields(imports.RECORD_MESSAGE), apps),
            *device_day.call_items(fields(imports.RECORD_CALL), apps),
        ]
    return [
        {**item, "lane": "you", "state": _state(item["start_ms"], item["end_ms"], now_ms)}
        for item in found
        if _overlaps(item["start_ms"], item["end_ms"], window_start, window_end)
    ]


# --- the pictures he sent ------------------------------------------------------------------


def _picture_items(
    conn: sqlite3.Connection, sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """A point in his lane for each picture sent in the window, with a turn or saved (0223)."""
    sent: dict[str, int] = {}
    for at, raw in conn.execute(
        "SELECT ts_epoch_ms, payload_json FROM events WHERE type IN (?, ?, ?) "
        "AND ts_epoch_ms >= ? AND ts_epoch_ms < ? "
        "AND json_extract(payload_json, '$.attachments') IS NOT NULL ORDER BY id",
        (*PICTURE_TURN_EVENTS, window_start, window_end),
    ):
        for one in json.loads(raw).get("attachments") or []:
            sent.setdefault(str(one), int(at))
    if not sent:
        return []
    notes = {} if sources.memory_db is None else picture_notes.bodies(sources.memory_db, sent)
    store = None if sources.attachments is None else Attachments(sources.attachments)
    shown = {
        one: at for one, at in sent.items()
        if one in notes or (store is not None and getattr(store.get(one), "kind", "") == "image")
    }
    held = stays(read_rows(conn, window_start - LOOKBACK_MS), now_ms) if shown else []
    items: list[dict[str, Any]] = []
    for one, at in shown.items():
        item: dict[str, Any] = {
            "id": f"picture-{one}",
            "lane": "you",
            "kind": "picture",
            "start_ms": at,
            "end_ms": at,
            "state": _state(at, at, now_ms),
            "attachment": one,
        }
        if one in notes:
            item["note"] = notes[one]
        here = next((s for s in held if s.start <= at and (s.end is None or at < s.end)), None)
        if here is not None:
            item.update({k: v for k, v in here.fields.items() if k != CUT_FROM_KEY})
        items.append(item)
    return items


# --- the phone -----------------------------------------------------------------------------


def _phone_items(
    conn: sqlite3.Connection, sources: DaySources, window_start: int, window_end: int, now_ms: int,
) -> list[dict[str, Any]]:
    """Stays, moves, sleep and workouts: one read of the phone's rows, folded by kind.

    A stay inside a place he keeps carries it as ``kept_place`` (ADR 0234); a sleep block carries
    its ``stages`` and a workout its ``activity`` and what Health knew of it (ADR 0293).
    """
    rows = read_rows(conn, window_start - LOOKBACK_MS)
    held = stays(rows, now_ms)
    kept = [] if sources.memory_db is None else places.read_kept(sources.memory_db)
    spans = [
        *held, *phone_moves(rows, held, now_ms), *sleeps(rows.health),
        *workouts(rows.health),
    ]
    items: list[dict[str, Any]] = []
    for span in spans:
        if not _overlaps(span.start, span.end, window_start, window_end):
            continue
        item = {
            "id": span.origin[1],
            "lane": "you",
            "kind": span.kind,
            "start_ms": span.start,
            "end_ms": span.end,
            "state": _state(span.start, span.end, now_ms),
            **span.fields,
        }
        if span.kind == "stay" and (
            inside := places.containing(kept, span.fields["lat"], span.fields["lng"])
        ):
            item["kept_place"] = {"id": inside.id, "name": inside.name, "role": inside.role}
        items.append(item)
    return items
