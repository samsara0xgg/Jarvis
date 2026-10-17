"""Whether the tasks left in a planned day still fit in the time left (ADR 0289). Pure.

A plan is an ordered list: an item with a ``start`` (or a window) sits on the clock, one without
is a task. :func:`budget` sets the minutes of the tasks not done against the minutes of the day
that nothing else has claimed. It lives here, below the decision layer, because her ``plan`` tool
(execution) and the plan watcher (decision) both read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import tzinfo

    from jarvis.state.plan import Plan, PlanItem

DAY_ENDS: Final[time] = time(23, 30)
"""When the day's work stops, unless ``day_plan.day_ends`` says otherwise."""


@dataclass(frozen=True)
class Budget:
    """Whether the tasks left still fit in the rest of the day.

    ``tasks`` are the items without a time that are not done, in the plan's order; ``fixed_min``
    is the time still ahead that is already spoken for (timed items not done, and their legs);
    ``free_min`` is what is left of the day after it, never below 0; ``next_timed`` is the next
    timed item not done that has not begun.
    """

    tasks: tuple[PlanItem, ...]
    remaining_min: int
    fixed_min: int
    free_min: int
    fits: bool
    next_task: PlanItem | None
    next_timed: PlanItem | None


def at(plan: Plan, text: object, zone: tzinfo | None) -> datetime | None:
    """``HH:MM`` on the plan's day in ``zone``, or None when ``text`` is not a clock time."""
    try:
        return datetime.combine(plan.day, time.fromisoformat(str(text)), tzinfo=zone)
    except ValueError:
        return None


def is_task(item: PlanItem) -> bool:
    """True for an item with no start and no window: done in its place in the list."""
    return item.start is None and item.window_from is None


def span_of(plan: Plan, item: PlanItem, zone: tzinfo | None) -> tuple[datetime, datetime] | None:
    """When a timed item lasts: its start (or window start) for its minutes; a task has none."""
    begin = at(plan, item.start or item.window_from, zone) if not is_task(item) else None
    return None if begin is None else (begin, begin + timedelta(minutes=item.minutes))


def _minutes(first: datetime, last: datetime) -> int:
    return max(0, int((last - first).total_seconds() // 60))


def budget(plan: Plan, now: datetime, *, day_ends: time) -> Budget:
    """The tasks left against the time left in ``plan``'s day at ``now`` (aware, in his zone).

    Fixed time counts what is still ahead of ``now``: a timed item not done, and the travel of its
    leg (leave at to arrive at), each only for the part after ``now``.
    """
    zone = now.tzinfo
    tasks = tuple(i for i in plan.items if is_task(i) and not i.done)
    fixed = 0
    ahead: list[tuple[datetime, PlanItem]] = []
    for item in plan.items:
        held = span_of(plan, item, zone)
        if held is None or item.done:
            continue
        fixed += _minutes(max(held[0], now), held[1])
        route = item.route or {}
        leave = at(plan, route.get("leave_at"), zone)
        arrive = at(plan, route.get("arrive_at"), zone)
        if leave and arrive:
            fixed += _minutes(max(leave, now), arrive)
        if held[0] >= now:
            ahead.append((held[0], item))
    remaining = sum(t.minutes for t in tasks)
    free = max(0, _minutes(now, datetime.combine(plan.day, day_ends, tzinfo=zone)) - fixed)
    return Budget(
        tasks, remaining, fixed, free, remaining <= free, tasks[0] if tasks else None,
        min(ahead, key=lambda a: a[0])[1] if ahead else None,
    )
