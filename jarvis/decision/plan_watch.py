"""What changed in a planned day, as the program sees it (ADR 0289). Pure: no I/O, no model.

:func:`notice` compares a day's plan with the facts the log holds (:mod:`jarvis.state.plan_facts`)
and names each change worth her attention once. It only notices: whether a late start matters
depends on what follows it, so the answer (silent, tell or adjust) is hers, asked of a small model
with the notice, the plan and the facts (:mod:`jarvis.decision.day_plan`). The thresholds are the
code's, not settings. A notice is keyed by its kind and item (``left_late:p2``), so each is noticed
once a day.

A plan is an ordered task list with a few things on the clock (ADR 0289): :func:`budget` works out
whether the tasks left still fit in the rest of the day, and the ``progress`` notice asks her to
look at that every couple of hours and whenever a timed item ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.plan_budget import DAY_ENDS, budget, is_task, span_of
from jarvis.state.plan_budget import at as _at

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import tzinfo

    from jarvis.state.plan import Plan, PlanItem
    from jarvis.state.plan_facts import DayFacts, Here, Move, Stay

NOT_UP_AFTER: Final[timedelta] = timedelta(minutes=30)
"""No wake signal this long after the first item's start."""
NOT_UP_BEFORE: Final[time] = time(12, 0)
"""``not_up`` is only asked of a first item that starts before this."""
OUTING_LEAD: Final[timedelta] = timedelta(minutes=25)
"""``outing_soon`` this long before a leg's leave time."""
LEFT_LATE_AFTER: Final[timedelta] = timedelta(minutes=3)
"""``left_late`` this long after the leave time, still in the stay he leaves from."""
STAYED_AFTER: Final[timedelta] = timedelta(minutes=15)
"""``stayed_longer`` this long past a placed item's end, still in its stay."""
LEFT_EARLY_BEFORE: Final[timedelta] = timedelta(minutes=20)
"""``left_early`` when he left a placed item's stay this long before its end."""
MOVE_SLACK: Final[timedelta] = timedelta(minutes=10)
"""A move this long before a leave time or an end already counts as leaving."""
NEXT_SOON: Final[timedelta] = timedelta(minutes=45)
"""``stayed_longer`` waits until the next item elsewhere is this close; earlier, staying is fine."""
LEAVING_WALK: Final[timedelta] = timedelta(minutes=10)
"""A walk this long during a placed item reads as leaving it; a shorter one as stepping out."""
_MIN_NAME: Final[int] = 4
"""A place name shorter than this must match whole, never as part of another."""
PROGRESS_EVERY: Final[timedelta] = timedelta(hours=2)
"""``progress`` this often after he woke, and when a timed item ends; never inside one."""


@dataclass(frozen=True)
class Notice:
    """One change worth her attention. ``facts`` is JSON-able and goes to her as it is.

    ``outing_soon`` carries ``weather`` and ``leg_now`` as None: the runtime reads both fresh
    when it acts on the notice, and fills them in before she is asked.
    """

    key: str
    kind: str
    item_id: str | None
    facts: dict[str, Any]


def _key(kind: str, item_id: str | None) -> str:
    return f"{kind}:{item_id}" if item_id else kind


def _clock(moment: datetime) -> str:
    return f"{moment:%H:%M}"


def _begin(plan: Plan, item: PlanItem, zone: tzinfo | None) -> datetime | None:
    text = item.start or item.window_from
    return _at(plan, text, zone) if text else None


def _end(plan: Plan, item: PlanItem, zone: tzinfo | None) -> datetime | None:
    """When a placed item ends: its start and minutes, or its window's end."""
    if item.start:
        begin = _at(plan, item.start, zone)
        return begin and begin + timedelta(minutes=item.minutes)
    return _at(plan, item.window_to, zone) if item.window_to else None


def _said(name: str) -> str:
    return " ".join(name.casefold().split())


def _same(place: str, where: Here | Stay) -> bool:
    """True when ``place`` (a kept name or free text) is where ``where`` is."""
    wanted = _said(place)
    if not wanted:
        return False
    for name in (where.kept, where.kept_role, where.place):
        held = _said(name)
        if held and (
            held == wanted
            or (min(len(held), len(wanted)) >= _MIN_NAME and (held in wanted or wanted in held))
        ):
            return True
    return False


def _item_facts(item: PlanItem) -> dict[str, Any]:
    return {"item": item.title, "place": item.place, "private": item.private}


def _woke(facts: DayFacts, zone: tzinfo | None) -> list[Notice]:
    if facts.wake is None:
        return []
    at = datetime.fromtimestamp(facts.wake.at_ms / 1000, zone)
    return [Notice("woke", "woke", None, {"signal": facts.wake.kind, "at": _clock(at)})]


def _not_up(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    firsts = [b for item in plan.items if (b := _begin(plan, item, now.tzinfo))]
    if facts.wake is not None or not firsts:
        return []
    first = min(firsts)
    if first.time() >= NOT_UP_BEFORE or now < first + NOT_UP_AFTER:
        return []
    return [Notice("not_up", "not_up", None, {"first_item_at": _clock(first)})]


def _legs(plan: Plan, now: datetime) -> list[tuple[PlanItem, datetime, str]]:
    """``(item, leave time, place left from)`` for each item with a leg that reads."""
    found = []
    for item in plan.items:
        route = item.route or {}
        leave = _at(plan, route.get("leave_at"), now.tzinfo)
        if leave is not None and route.get("from"):
            found.append((item, leave, str(route["from"])))
    return found


def _outing_soon(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    return [
        Notice(
            _key("outing_soon", item.item_id), "outing_soon", item.item_id,
            _item_facts(item) | {
                "leave_at": _clock(leave), "from": origin, "leg": item.route,
                "here": facts.here and facts.here.place,
                "weather": None, "leg_now": None,  # read fresh by the runtime
            },
        )
        for item, leave, origin in _legs(plan, now)
        if leave - OUTING_LEAD <= now < leave
    ]


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _moved(facts: DayFacts, since: datetime) -> bool:
    """True when a move (walking, driving...) started at or after ``since``.

    The phone's location fixes are sparse and its visits often missing, so a move is what says he
    left; where he is only rules a notice out when it names somewhere else.
    """
    cut = _ms(since)
    return any(move.start_ms >= cut for move in facts.moves)


def _leaving(move: Move, now: datetime) -> bool:
    """A drive, or a walk long enough to be going somewhere rather than stepping out."""
    if move.activity in {"automotive", "ride"}:
        return True
    end = _ms(now) if move.end_ms is None else move.end_ms
    return end - move.start_ms >= _ms_of(LEAVING_WALK)


def _ms_of(span: timedelta) -> int:
    return int(span.total_seconds() * 1000)


def _elsewhere(place: str, here: Here | None) -> bool:
    """True when ``here`` names a place and it is not ``place``."""
    return here is not None and bool(here.place) and not _same(place, here)


def _left_late(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    found = []
    for item, leave, origin in _legs(plan, now):
        arrive = _begin(plan, item, now.tzinfo)
        if (
            now >= leave + LEFT_LATE_AFTER
            and (arrive is None or now < arrive)
            and not _moved(facts, leave - MOVE_SLACK)
            and not _elsewhere(origin, facts.here)
        ):
            late = int((now - leave).total_seconds() // 60)
            found.append(Notice(
                _key("left_late", item.item_id), "left_late", item.item_id,
                _item_facts(item) | {"leave_at": _clock(leave), "minutes_past": late,
                                     "from": origin, "leg": item.route},
            ))
    return found


def _item_due(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    found = []
    for item in plan.items:
        begin = _begin(plan, item, now.tzinfo)
        if item.firmness != "movable" or begin is None:
            continue
        end = _end(plan, item, now.tzinfo) or begin + timedelta(minutes=item.minutes)
        if begin <= now < end:
            found.append(Notice(
                _key("item_due", item.item_id), "item_due", item.item_id,
                _item_facts(item) | {
                    "from": item.window_from, "to": item.window_to, "doing": list(facts.doing),
                    "here": facts.here and facts.here.place,
                },
            ))
    return found


def _progress(plan: Plan, facts: DayFacts, now: datetime, day_ends: time) -> list[Notice]:
    """A look at the tasks left, as one or two notices.

    ``progress:<n>`` comes every :data:`PROGRESS_EVERY` after waking, ``progress:after:<id>``
    when a timed item ends. Only while he is awake and a task is not done, never inside a timed
    item. A checkpoint that a timed item ended after is left to that item's own notice.
    """
    zone = now.tzinfo
    held = budget(plan, now, day_ends=day_ends)
    spans = [(held_span, i) for i in plan.items if (held_span := span_of(plan, i, zone))]
    if facts.wake is None or held.next_task is None or any(a <= now < b for (a, b), _ in spans):
        return []
    woke = datetime.fromtimestamp(facts.wake.at_ms / 1000, zone)
    ended = [(b, i) for (_, b), i in spans if woke <= b <= now]
    base = {
        "tasks": [
            {"id": i.item_id, "title": i.title, "minutes": i.minutes, "done": i.done}
            for i in plan.items if is_task(i)
        ],
        "remaining_min": held.remaining_min, "fixed_min": held.fixed_min,
        "free_min": held.free_min, "fits": held.fits, "day_ends": _clock(
            datetime.combine(plan.day, day_ends, tzinfo=zone)),
        "next_task": {"id": held.next_task.item_id, "title": held.next_task.title,
                      "minutes": held.next_task.minutes},
        "private": held.next_task.private, "doing": list(facts.doing),
        "commits": [
            {"at": _clock(datetime.fromtimestamp(c.at_ms / 1000, zone)), "repo": c.repo,
             "subject": c.subject}
            for c in facts.commits
        ],
    }
    found = []
    if ended:
        end, item = max(ended, key=lambda e: e[0])
        found.append(Notice(
            f"progress:after:{item.item_id}", "progress", None,
            base | {"after": {"id": item.item_id, "title": item.title, "ended_at": _clock(end)}},
        ))
    count = (now - woke) // PROGRESS_EVERY
    if count >= 1 and not any(b >= woke + count * PROGRESS_EVERY for b, _ in ended):
        found.append(Notice(f"progress:{count}", "progress", None, base | {"checkpoint": count}))
    return found


def _placed(plan: Plan, now: datetime) -> list[tuple[PlanItem, datetime, datetime]]:
    """``(item, start, end)`` of the items with a place and a start."""
    found = []
    for item in plan.items:
        begin, end = _begin(plan, item, now.tzinfo), _end(plan, item, now.tzinfo)
        if item.place and item.start and begin and end:
            found.append((item, begin, end))
    return found


def _next(
    placed: list[tuple[PlanItem, datetime, datetime]], after: datetime,
) -> tuple[PlanItem, datetime] | None:
    later = [(item, begin) for item, begin, _ in placed if begin >= after]
    return later[0] if later else None


def _stayed_longer(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    placed = _placed(plan, now)
    found = []
    for item, _begun, end in placed:
        following = _next(placed, end)
        if following is None:
            continue
        going, starts = following
        if (
            now >= end + STAYED_AFTER
            # a next item with a leg is left_late's to watch; one without is close enough to matter
            and _said(going.place) != _said(item.place) and not going.route
            and now >= starts - NEXT_SOON
            and not any(other is not item and a <= now < b for other, a, b in placed)
            and not _moved(facts, end - MOVE_SLACK)
            and not _elsewhere(item.place, facts.here)
        ):
            late = int((now - end).total_seconds() // 60)
            found.append(Notice(
                _key("stayed_longer", item.item_id), "stayed_longer", item.item_id,
                _item_facts(item) | {"ended_at": _clock(end), "minutes_past": late,
                                     "going_to": going.place, "next_at": _clock(starts),
                                     "doing": list(facts.doing)},
            ))
    return found


def _left_early(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    found = []
    for item, begin, end in _placed(plan, now):
        left = next(
            (m for m in facts.moves
             if _ms(begin) <= m.start_ms <= min(_ms(end - LEFT_EARLY_BEFORE), _ms(now))
             and _leaving(m, now)),
            None,
        )
        if left is not None:
            at = datetime.fromtimestamp(left.start_ms / 1000, now.tzinfo)
            found.append(Notice(
                _key("left_early", item.item_id), "left_early", item.item_id,
                _item_facts(item) | {"left_at": _clock(at), "ends_at": _clock(end),
                                     "minutes_early": int((end - at).total_seconds() // 60),
                                     "how": left.activity},
            ))
    return found


def _new_commitments(plan: Plan, facts: DayFacts) -> list[Notice]:
    held = {item.source for item in plan.items}
    return [
        Notice(
            _key("new_commitment", c.commitment_id), "new_commitment", c.commitment_id,
            {"title": c.title, "start": c.start or None, "place": c.place, "private": c.private},
        )
        for c in facts.commitments
        if f"commitment:{c.commitment_id}" not in held
    ]


def notice(
    plan: Plan, facts: DayFacts, now: datetime, seen: Collection[str], *, day_ends: time = DAY_ENDS,
) -> list[Notice]:
    """The changes in ``plan``'s day at ``now`` (aware, in his zone) that are not in ``seen``.

    ``seen`` holds the keys already noticed today. Kinds (ADR 0289): ``woke``; ``not_up`` (no
    wake signal :data:`NOT_UP_AFTER` after a first item that starts before noon);
    ``outing_soon`` (:data:`OUTING_LEAD` before a leg's leave time); ``left_late``
    (:data:`LEFT_LATE_AFTER` after it with no move since, until the item starts);
    ``item_due`` (a ``movable`` item's start or window start, while it lasts); ``stayed_longer``
    (:data:`STAYED_AFTER` past a placed item's end with no move since, when the next placed item
    is elsewhere, has no leg and starts within :data:`NEXT_SOON`, and nothing else is on);
    ``left_early`` (a drive or a walk of :data:`LEAVING_WALK` began :data:`LEFT_EARLY_BEFORE` or
    more before a placed item's end);
    ``new_commitment`` (noted today for today and not in the plan); ``progress`` (every
    :data:`PROGRESS_EVERY` after waking and when a timed item ends, while a task is not done and
    never inside a timed item; ``day_ends`` is when the day's work stops, for :func:`budget`).
    """
    found = [
        *_woke(facts, now.tzinfo),
        *_not_up(plan, facts, now),
        *_outing_soon(plan, facts, now),
        *_left_late(plan, facts, now),
        *_item_due(plan, facts, now),
        *_stayed_longer(plan, facts, now),
        *_left_early(plan, facts, now),
        *_new_commitments(plan, facts),
        *_progress(plan, facts, now, day_ends),
    ]
    return [n for n in found if n.key not in seen]



__all__ = ["Notice", "budget", "notice"]
