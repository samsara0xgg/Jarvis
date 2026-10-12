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
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.decision.turn_guards import CALENDAR_LINE
from jarvis.execution import speakable
from jarvis.execution.my_day_tool import work_state_now
from jarvis.execution.tools import ToolError
from jarvis.shared.about import prompt_title

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, tzinfo
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


def calendar_line(
    read: CalendarRead | None, zone: tzinfo, clock: Callable[[], datetime],
) -> str | None:
    """His next one or two calendar items still ahead, with how long ago they were read.

    None without a reader (the default), without a reading, or when nothing in it is still
    ahead: a reading from an hour ago never offers an item that has begun as the next one.
    """
    glance = None if read is None else read()
    if glance is None:
        return None
    now = clock()
    ahead = sorted((i for i in glance.items if i.start > now), key=lambda i: i.start)[:_ITEMS]
    if not ahead:
        return None
    today = now.astimezone(zone).date()

    def said(item: CalendarItem) -> str:
        local = item.start.astimezone(zone)
        days = (local.date() - today).days
        if days == 0:
            when = speakable.clock(local)
        elif days == 1:
            when = f"tomorrow ({local:%a}) {speakable.clock(local)}"
        else:
            when = speakable.when(item.start, now, zone)
        return f'{when} "{speakable.cut(prompt_title(item.title), _TITLE_CHARS)}"'

    line = (
        f"{CALENDAR_PREFIX} (read {speakable.age(glance.read_at, now)}): "
        + "; ".join(said(i) for i in ahead) + "."
    )
    return speakable.cut(line, LINE_CHARS)
