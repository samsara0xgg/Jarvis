"""ADR 0249: the lines of "him right now" the state block adds to what it already carried.

The block already holds the bus ride, the Dashboard view, the phone's last place with its age
and, in the ledger's "Today so far", every pending reminder. What a present-tense question
still sent to a tool was what he is doing now, and what is next on his calendar. Each
function is a ``Runtime.live_context`` producer: one line, or None when it has nothing, never a
tool call. What he is doing says how old it is; what is next is read from the ledger of his
things (ADR 0299) as the line is made.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.decision.turn_guards import CALENDAR_LINE
from jarvis.execution import speakable
from jarvis.execution.my_day_tool import work_state_now
from jarvis.shared.about import prompt_title
from jarvis.state import plan, things
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.plan_budget import DAY_ENDS, budget
from jarvis.state.things_late import due_is_day

if TYPE_CHECKING:
    from collections.abc import Callable
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


class CalendarItem(NamedTuple):
    """One thing ahead of him: when it starts and its title (his words for it)."""

    start: datetime
    title: str
    all_day: bool = False  # a date with no hour
    due: bool = False  # a thing to do by then, not one to go to


class Ahead(NamedTuple):
    """The next things (soonest first) and the open things he has not confirmed, counted."""

    items: list[CalendarItem]
    unconfirmed: int


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


def plan_line(
    event_log: Path, zone: tzinfo, clock: Callable[[], datetime], day_ends: time = DAY_ENDS,
) -> str | None:
    """Today's plan in one line (ADR 0291).

    The next task, the time left, the next thing on the clock.

    None without a plan for today. A private task or item is named by its time only (a task has
    none, so it is just "a private item").
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
    left = budget(held, local, day_ends=day_ends)
    parts = []
    if left.next_task is not None:
        task = left.next_task
        title = "a private item" if task.private else prompt_title(task.title)
        count = len(left.tasks)
        parts.append(f"next task {speakable.cut(title, _TITLE_CHARS)}")
        parts.append(
            f"{count} task{'s' if count != 1 else ''} left, "
            f"{left.remaining_min} of {left.free_min} free minutes",
        )
    elif left.next_timed is None:
        return f"{PLAN_PREFIX}: nothing more."
    else:
        parts.append("no tasks left")
    if (fixed := left.next_timed) is not None:
        at = fixed.start or fixed.window_from
        name = "" if fixed.private else " " + speakable.cut(prompt_title(fixed.title), _TITLE_CHARS)
        parts.append(f"next fixed {at}{name}")
    return f"{PLAN_PREFIX}: " + "; ".join(parts) + "."


def _name(one: things.Thing) -> str:
    """How the line names a thing: a private one only as such, an unconfirmed one says so."""
    title = "something private" if one.private else one.title
    return title if one.confirmed else f"{title} (not confirmed)"


def next_appointments(event_log: Path, zone: tzinfo, now: datetime) -> Ahead:
    """The ledger's open occurrences and things due from ``now`` to the end of tomorrow.

    Soonest first, and with them the count of open things he has not confirmed. A private one is
    named only as such and an unconfirmed one says so. The event log is local, so this answers
    whatever Microsoft does; the mirror brought his calendar into it (ADR 0299).
    """
    local = now.astimezone(zone)
    first = int(now.timestamp() * 1000)
    end_day = datetime.combine(local.date() + timedelta(days=2), time(), zone)
    last = int(end_day.timestamp() * 1000)
    try:
        with closing(open_runtime_event_log(event_log)) as conn:
            found = things.occurrences(conn, first, last, zone=zone)
            open_now = things.open_things(conn)
    except sqlite3.Error as exc:
        LOGGER.warning("appointments: not read (%s)", exc)
        return Ahead([], 0)
    items = [
        CalendarItem(
            datetime.fromtimestamp(occ.start_ms / 1000, UTC), _name(occ.thing), occ.all_day,
        )
        for occ in found
    ]
    items.extend(
        CalendarItem(
            datetime.fromtimestamp(things.due_ms(one, zone) / 1000, UTC), _name(one),
            due_is_day(one), due=True,
        )
        for one in open_now
        if one.due and first <= things.due_ms(one, zone) < last
    )
    items.sort(key=lambda i: i.start)
    return Ahead(items, sum(1 for one in open_now if not one.confirmed))


def calendar_line(event_log: Path, zone: tzinfo, clock: Callable[[], datetime]) -> str | None:
    """His next one or two things still ahead, from the ledger, and how many wait on his word.

    None when nothing is ahead: one that has begun is never offered as the next. The count of
    open things he has not confirmed, when it is not zero, stands in the heading.
    """
    now = clock()
    ahead = next_appointments(event_log, zone, now)
    today = now.astimezone(zone).date()
    soon = [
        i for i in ahead.items
        if i.start > now or (i.all_day and i.start.astimezone(zone).date() >= today)
    ][:_ITEMS]
    if not soon:
        return None

    def said(item: CalendarItem) -> str:
        local = item.start.astimezone(zone)
        days = (local.date() - today).days
        if item.all_day:
            day = "today" if days == 0 else (
                f"tomorrow ({local:%a})" if days == 1 else f"{local:%a %b} {local.day}"
            )
            when = f"due {day}" if item.due else f"all day {day}"
        else:
            if days == 0:
                when = speakable.clock(local)
            elif days == 1:
                when = f"tomorrow ({local:%a}) {speakable.clock(local)}"
            else:
                when = speakable.when(item.start, now, zone)
            when = f"due {when}" if item.due else when
        return f'{when} "{speakable.cut(prompt_title(item.title), _TITLE_CHARS)}"'

    waiting = f" ({ahead.unconfirmed} not confirmed)" if ahead.unconfirmed else ""
    line = f"{CALENDAR_PREFIX}{waiting}: " + "; ".join(said(i) for i in soon) + "."
    return speakable.cut(line, LINE_CHARS)
