"""ADR 0249: the lines of "him right now" the state block adds to what it already carried.

The block already holds the bus ride, the Dashboard view, the phone's last place with its age
and, in the ledger's "Today so far", every pending reminder. What a present-tense question
still sent to a tool was what he is doing now, and what is next on his calendar. Each
function is a ``Runtime.live_context`` producer: one line, or None when it has nothing, never a
tool call, and each fact says how old it is.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.decision.turn_guards import CALENDAR_LINE
from jarvis.execution import speakable
from jarvis.execution.my_day_tool import work_state_now
from jarvis.execution.tools import ToolError
from jarvis.shared.about import prompt_title
from jarvis.state import commitments, plan
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from datetime import tzinfo
    from pathlib import Path

LINE_CHARS: Final = 200
"""One line's cap; the two lines together stay near 400 characters."""
DOING_PREFIX: Final = "Doing now"
CALENDAR_PREFIX: Final = CALENDAR_LINE
"""The turn guards (ADR 0250) read this line as backing what the answer says of his calendar."""
OLDEST_ANALYSIS: Final = timedelta(hours=12)
"""An analysis older than this says nothing about now, so the block leaves it to ``my_day``."""
_TITLE_CHARS: Final = 40
_ITEMS: Final = 2
LOGGER = logging.getLogger(__name__)
KEPT: Final = 3
"""Items a reading holds: the line shows two, so one that begins before the next read leaves two."""
REFRESH_S: Final = 900.0
"""How often the calendar is read; a turn never waits on it."""
RETRY_S: Final = 120.0
"""How soon a read that failed (or found Microsoft not connected) is tried again."""


class CalendarItem(NamedTuple):
    """One thing on his calendar: when it starts and its title (his calendar's words)."""

    start: datetime
    title: str
    all_day: bool = False  # a date with no hour: shown as "due <day>"


class CalendarGlance(NamedTuple):
    """The next items a calendar reader holds, and when it last read the calendar."""

    read_at: datetime
    items: Sequence[CalendarItem]


CalendarRead = Callable[[], CalendarGlance | None]
"""Injected by the composition root; it must not wait on the network (a turn calls it)."""


class CalendarCache:
    """The calendar reader the composition root injects: reads in the background, holds the last.

    ``fetch(now)`` is the one network read (``schedule_tool.upcoming_events`` over ``Home.graph``)
    and returns ``(start, title)`` pairs. A turn calls the cache and gets the last reading or
    None; a read that failed, or found Microsoft not connected, leaves None, so the block says
    nothing of his calendar rather than that it is clear.
    """

    def __init__(
        self,
        fetch: Callable[[datetime], Sequence[tuple[datetime, str]]],
        clock: Callable[[], datetime],
    ) -> None:
        """``fetch`` reads the calendar as of ``clock()``; neither is called by a turn."""
        self._fetch = fetch
        self._clock = clock
        self._glance: CalendarGlance | None = None
        self._failing = False

    def __call__(self) -> CalendarGlance | None:
        """The last reading; no I/O."""
        return self._glance

    def refresh(self) -> bool:
        """Read the calendar now; False (and no reading held) when it could not be read."""
        read_at = self._clock()
        try:
            items = [CalendarItem(start, title) for start, title in self._fetch(read_at)]
        except (LookupError, ToolError, ValueError, TypeError, AttributeError) as exc:
            self._glance = None
            if not self._failing:  # once per outage, not every retry
                LOGGER.warning("calendar glance: not read (%s: %s)", type(exc).__name__, exc)
            self._failing = True
            return False
        self._glance, self._failing = CalendarGlance(read_at, items[:KEPT]), False
        return True

    async def run(self) -> None:
        """Read at once, then every :data:`REFRESH_S` (every :data:`RETRY_S` while failing)."""
        LOGGER.info("calendar glance started (every %.0f s)", REFRESH_S)
        try:
            while True:
                read = await asyncio.to_thread(self.refresh)
                await asyncio.sleep(REFRESH_S if read else RETRY_S)
        except asyncio.CancelledError:
            LOGGER.info("calendar glance cancelled")
            raise


def doing_now_line(
    event_log: Path, zone: tzinfo, clock: Callable[[], datetime],
) -> str | None:
    """What the latest work analysis says he is doing, with its age; as ``my_day`` words it.

    None without an analysis, with one that names nothing he is doing now, or past
    :data:`OLDEST_ANALYSIS`. Over three hours old it carries ``my_day``'s warning.
    """
    state = work_state_now(event_log, zone, clock(), within=OLDEST_ANALYSIS)
    if state is None or "text" not in state:
        return None
    head = f"{DOING_PREFIX} (as of {state['as_of']}): "
    note = f" Note: {state['stale']}." if "stale" in state else ""
    room = LINE_CHARS - len(head) - len(note) - 1
    return f"{head}{speakable.cut(str(state['text']), room)}.{note}"


PLAN_PREFIX: Final = "Plan today"


def plan_line(event_log: Path, zone: tzinfo, clock: Callable[[], datetime]) -> str | None:
    """Today's plan in one line (ADR 0289): whether it is settled, its next item, how many follow.

    None without a plan for today. A private item is named by its time only. Items without a
    time come last and count as ahead.
    """
    local = clock().astimezone(zone)
    try:
        with closing(open_runtime_event_log(event_log)) as conn:
            held = plan.for_day(conn, local.date())
    except sqlite3.Error as exc:
        LOGGER.warning("plan line: not read (%s)", exc)
        return None
    if held is None:
        return None
    now = f"{local:%H:%M}"
    ahead = [i for i in held.items if (i.start or i.window_from or "99:99") >= now]
    state = "settled" if held.status == "settled" else "not settled yet"
    if not ahead:
        return f"{PLAN_PREFIX} ({state}): nothing more."
    first = ahead[0]
    title = "a private item" if first.private else prompt_title(first.title)
    when = first.start or first.window_from or "any time"
    line = f"{PLAN_PREFIX} ({state}): next {when} {speakable.cut(title, _TITLE_CHARS)}"
    return f"{line}; {len(ahead) - 1} more." if len(ahead) > 1 else f"{line}."


Appointments = Callable[[datetime], Sequence[CalendarItem]]
"""The things he has to go to (ADR 0274) from now to the end of tomorrow; a local read."""


def next_appointments(
    event_log: Path, zone: tzinfo, now: datetime,
) -> list[CalendarItem]:
    """The occurrences of his commitments from ``now`` to the end of tomorrow, soonest first.

    A private one is named only as such and an unconfirmed one says so. The event log is local,
    so this still answers when Microsoft does not.
    """
    local = now.astimezone(zone)
    end = datetime.combine(local.date() + timedelta(days=2), time(), zone)
    try:
        with closing(open_runtime_event_log(event_log)) as conn:
            found = commitments.occurrences(
                conn, int(now.timestamp() * 1000), int(end.timestamp() * 1000), zone=zone,
            )
    except sqlite3.Error as exc:
        LOGGER.warning("appointments: not read (%s)", exc)
        return []
    items = []
    for occ in found:
        one = occ.commitment
        title = "something private" if one.private else one.title
        if not one.confirmed:
            title += " (not confirmed)"
        items.append(
            CalendarItem(datetime.fromtimestamp(occ.start_ms / 1000, UTC), title, one.all_day),
        )
    return items


def calendar_line(
    read: CalendarRead | None, zone: tzinfo, clock: Callable[[], datetime],
    appointments: Appointments | None = None,
) -> str | None:
    """His next one or two items still ahead, calendar and appointments, with the calendar's age.

    None when there is neither a reading nor an appointment, or when nothing in them is still
    ahead: a reading from an hour ago never offers an item that has begun as the next one. A
    calendar that could not be read leaves the appointments, which are read here, just now.
    """
    glance = None if read is None else read()
    now = clock()
    held = [] if appointments is None else list(appointments(now))
    if glance is None and not held:
        return None
    items = [*(() if glance is None else glance.items), *held]
    today = now.astimezone(zone).date()
    ahead = sorted(
        (
            i for i in items
            if i.start > now or (i.all_day and i.start.astimezone(zone).date() >= today)
        ),
        key=lambda i: i.start,
    )[:_ITEMS]
    if not ahead:
        return None

    def said(item: CalendarItem) -> str:
        local = item.start.astimezone(zone)
        days = (local.date() - today).days
        if item.all_day:
            when = "due today" if days == 0 else (
                f"due tomorrow ({local:%a})" if days == 1 else f"due {local:%a %b} {local.day}"
            )
        elif days == 0:
            when = speakable.clock(local)
        elif days == 1:
            when = f"tomorrow ({local:%a}) {speakable.clock(local)}"
        else:
            when = speakable.when(item.start, now, zone)
        return f'{when} "{speakable.cut(prompt_title(item.title), _TITLE_CHARS)}"'

    read_at = now if glance is None else glance.read_at
    line = (
        f"{CALENDAR_PREFIX} (read {speakable.age(read_at, now)}): "
        + "; ".join(said(i) for i in ahead) + "."
    )
    return speakable.cut(line, LINE_CHARS)
