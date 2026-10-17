"""What changed in a planned day, as the program sees it (ADR 0289). Pure: no I/O, no model.

:func:`notice` compares a day's plan with the facts the log holds (:mod:`jarvis.state.plan_facts`)
and names each change worth her attention once. It only notices: whether a late start matters
depends on what follows it, so the answer (silent, tell or adjust) is hers, asked of a small model
with the notice, the plan and the facts (:mod:`jarvis.decision.day_plan`). The thresholds are the
code's, not settings. A notice is keyed by its kind and item (``left_late:p2``), so each is noticed
once a day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import tzinfo

    from jarvis.state.plan import Plan, PlanItem
    from jarvis.state.plan_facts import DayFacts, Here, Stay

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
_MIN_NAME: Final[int] = 4
"""A place name shorter than this must match whole, never as part of another."""


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


def _at(plan: Plan, text: object, zone: tzinfo | None) -> datetime | None:
    """``HH:MM`` on the plan's day in ``zone``, or None when ``text`` is not a clock time."""
    try:
        return datetime.combine(plan.day, time.fromisoformat(str(text)), tzinfo=zone)
    except ValueError:
        return None


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


def _left_late(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    here = facts.here
    if here is None or not here.in_stay:
        return []
    found = []
    for item, leave, origin in _legs(plan, now):
        arrive = _begin(plan, item, now.tzinfo)
        since = datetime.fromtimestamp((here.since_ms or 0) / 1000, now.tzinfo)
        if (
            now >= leave + LEFT_LATE_AFTER
            and (arrive is None or now < arrive)
            and since <= leave
            and _same(origin, here)
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


def _placed(plan: Plan, now: datetime) -> list[tuple[PlanItem, datetime, datetime]]:
    """``(item, start, end)`` of the items with a place and a start."""
    found = []
    for item in plan.items:
        begin, end = _begin(plan, item, now.tzinfo), _end(plan, item, now.tzinfo)
        if item.place and item.start and begin and end:
            found.append((item, begin, end))
    return found


def _stayed_longer(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    here = facts.here
    if here is None or not here.in_stay:
        return []
    since = datetime.fromtimestamp((here.since_ms or 0) / 1000, now.tzinfo)
    placed = _placed(plan, now)
    found = []
    for item, _begun, end in placed:
        # another item at this place that is on now means staying is the plan
        kept_on = any(
            other is not item and _same(other.place, here) and a <= now < b
            for other, a, b in placed
        )
        if now >= end + STAYED_AFTER and since < end and _same(item.place, here) and not kept_on:
            late = int((now - end).total_seconds() // 60)
            found.append(Notice(
                _key("stayed_longer", item.item_id), "stayed_longer", item.item_id,
                _item_facts(item) | {"ended_at": _clock(end), "minutes_past": late,
                                     "doing": list(facts.doing)},
            ))
    return found


def _left_early(plan: Plan, facts: DayFacts, now: datetime) -> list[Notice]:
    found = []
    for item, begin, end in _placed(plan, now):
        for stay in reversed(facts.stays):
            left = None if stay.end_ms is None else datetime.fromtimestamp(
                stay.end_ms / 1000, now.tzinfo)
            came = datetime.fromtimestamp(stay.start_ms / 1000, now.tzinfo)
            if (
                left is not None and _same(item.place, stay)
                and came < end and begin < left <= end - LEFT_EARLY_BEFORE and left <= now
            ):
                early = int((end - left).total_seconds() // 60)
                found.append(Notice(
                    _key("left_early", item.item_id), "left_early", item.item_id,
                    _item_facts(item) | {"left_at": _clock(left), "ends_at": _clock(end),
                                         "minutes_early": early},
                ))
                break
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
    plan: Plan, facts: DayFacts, now: datetime, seen: Collection[str],
) -> list[Notice]:
    """The changes in ``plan``'s day at ``now`` (aware, in his zone) that are not in ``seen``.

    ``seen`` holds the keys already noticed today. Kinds (ADR 0289): ``woke``; ``not_up`` (no
    wake signal :data:`NOT_UP_AFTER` after a first item that starts before noon);
    ``outing_soon`` (:data:`OUTING_LEAD` before a leg's leave time); ``left_late``
    (:data:`LEFT_LATE_AFTER` after it, still in the stay he leaves from, until the item starts);
    ``item_due`` (a ``movable`` item's start or window start, while it lasts); ``stayed_longer``
    (:data:`STAYED_AFTER` past a placed item's end, still in that stay and no other item there is
    on); ``left_early`` (he left a placed item's stay :data:`LEFT_EARLY_BEFORE` before its end);
    ``new_commitment`` (noted today for today and not in the plan).
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
    ]
    return [n for n in found if n.key not in seen]

