"""The day-plan prompts and the parser of the model's answers (ADR 0288, 0383). Pure: no I/O.

The nightly draft, the morning line and the evening line all use the ``day-plan`` skill. The
model's answer is untrusted JSON: :func:`parse_items` keeps only the items that read, do not
collide with a fixed item and name a known source, and adds back any fixed item the model left
out; the program, not the model, owns the timed things of the day. :func:`parse_message` and
:func:`parse_evening` read the words of the two lines and where each undone task goes. A private
item reaches the model already masked: the program decides what it may know, the prompt has no
privacy rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import lang
from jarvis.shared.skills import load_skill
from jarvis.state import things
from jarvis.state.plan import DESTINATIONS, ITEM_PREFIX, MAX_ITEMS, Plan, PlanItem, check_item
from jarvis.state.plan_budget import is_task
from jarvis.state.things_late import due_is_day, late, when_ms

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.state.things import Occurrence, Thing

MESSAGE_CHARS: Final[int] = 600
"""Longest morning or evening message the parser accepts; a longer one is not a short one."""
_DAY_MINUTES: Final[int] = 24 * 60
_FENCE: Final[re.Pattern[str]] = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


@dataclass(frozen=True)
class DayPlanInputs:
    """Everything the draft may use for one day, as plain JSON-able values."""

    day: date
    fixed: tuple[PlanItem, ...] = ()
    upcoming: tuple[Mapping[str, Any], ...] = ()
    calendar_all_day: tuple[str, ...] = ()
    things: tuple[Mapping[str, Any], ...] = ()
    goals: tuple[str, ...] = ()
    deadlines: tuple[str, ...] = ()
    routines: tuple[Mapping[str, Any], ...] = ()
    weather: Mapping[str, Any] | None = None
    recent_days: tuple[Mapping[str, Any], ...] = ()
    his_changes: tuple[Mapping[str, Any], ...] = ()
    carried: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def payload(self, *, zone_name: str, today: date) -> dict[str, Any]:
        """The user message of the draft call, as a dict."""
        return {
            "task": "draft",
            "day": self.day.isoformat(),
            "weekday": self.day.strftime("%A"),
            "today": today.isoformat(),
            "timezone": zone_name,
            "fixed": [_public(item) for item in self.fixed],
            "upcoming": list(self.upcoming),
            "calendar_all_day": list(self.calendar_all_day),
            "things": list(self.things),
            "goals": list(self.goals),
            "deadlines": list(self.deadlines),
            "routines": list(self.routines),
            "weather": self.weather,
            "recent_days": list(self.recent_days),
            "his_changes": list(self.his_changes),
            "carried": list(self.carried),
            "notes": list(self.notes),
            "sources": list(self.sources),
        }

    def digest(self) -> str:
        """A short hash of the inputs, stored with the draft to tell a changed day from a rerun."""
        body = self.payload(zone_name="", today=self.day)
        text = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


def _zone_name(zone: tzinfo) -> str:
    return str(getattr(zone, "key", None) or zone)


def _public(item: PlanItem) -> dict[str, Any]:
    """What the model sees of an item: no route, no id churn."""
    row = item.payload()
    row.pop("route", None)
    return row


@cache
def _instructions() -> str:
    return load_skill("day-plan").instructions


def build_prompt(
    inputs: DayPlanInputs, *, zone: tzinfo, today: date,
) -> tuple[str, str]:
    """``(system, user)`` of the draft call: the skill, and the day's inputs as JSON."""
    body = inputs.payload(zone_name=_zone_name(zone), today=today)
    user = json.dumps(body, ensure_ascii=False, default=str)
    return _instructions(), user


def _spoken(clock: str, day: date, zone: tzinfo, said_in: lang.Language) -> str:
    """``HH:MM`` as it is said aloud (``晚上9点35分``, ``9:35 PM``)."""
    hour, minute = (int(part) for part in clock.split(":"))
    moment = datetime.combine(day, datetime.min.time(), zone).replace(hour=hour, minute=minute)
    return lang.spoken_time(moment, said_in)


def _masked(clock: str | None, day: date, zone: tzinfo, said_in: lang.Language) -> str:
    """What a private item is called to the model: its time alone, else that it is private."""
    if clock:
        return lang.t("plan.private", lang=said_in, time=_spoken(clock, day, zone, said_in))
    return lang.t("plan.private_untimed", lang=said_in)


def _material(item: PlanItem, day: date, zone: tzinfo, said_in: lang.Language) -> dict[str, Any]:
    """An item as the morning prompt shows it: route cut to what is said, a private one masked."""
    row = _public(item)
    if item.route:
        row["route"] = {k: item.route.get(k) for k in ("leave_at", "line", "board")}
    if item.private:
        row |= {
            "title": _masked(item.start or item.window_from, day, zone, said_in),
            "place": "", "why": "", "goal": "", "prepare": [],
        }
    row.pop("private")
    return row


def build_morning_prompt(  # noqa: PLR0913 - the plan, the weather, the goals, the clock, a failure.
    plan: Plan, *, weather: Mapping[str, Any] | None, goals: Sequence[str], now: datetime,
    zone: tzinfo, draft_failed: bool, outings: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """``(system, user)`` of the morning call (task ``morning``): the plan, the weather, the goals.

    ``draft_failed`` says the day's draft did not come, so ``plan`` is empty. ``weather`` is today
    at home in parts and ``outings`` the weather of each outing's own hours at its place (ADR 0355).
    Clocks are spoken, in the language of the plan's titles.
    """
    local = now.astimezone(zone)
    said_in = lang.text_language(" ".join(item.title for item in plan.items))
    body = {
        "task": "morning",
        "day": plan.day.isoformat(),
        "weekday": plan.day.strftime("%A"),
        "now": lang.spoken_time(local, said_in),
        "language": said_in,
        "plan": _said_clocks(
            [_material(item, plan.day, zone, said_in) for item in plan.items], plan.day, zone,
            said_in,
        ),
        "weather": _said_clocks(weather, plan.day, zone, said_in),
        "outings": _said_clocks(list(outings), plan.day, zone, said_in),
        "goals": list(goals),
        "draft_failed": draft_failed,
    }
    return _instructions(), json.dumps(body, ensure_ascii=False, default=str)


def build_evening_prompt(
    plan: Plan, *, lived: Sequence[str], tomorrow: Sequence[Mapping[str, Any]], now: datetime,
    zone: tzinfo,
) -> tuple[str, str]:
    """``(system, user)`` of the evening call (task ``evening``).

    ``planned`` is the plan's items with whether each is done; ``lived`` is today as the log saw
    it; ``tomorrow`` is tomorrow's things with an hour (``title``, ``start``, ``place``,
    ``private``). Clocks are spoken; a private item is masked as in the morning.
    """
    local = now.astimezone(zone)
    said_in = lang.text_language(" ".join(item.title for item in plan.items))
    tomorrow_day = plan.day + timedelta(days=1)
    body = {
        "task": "evening",
        "day": plan.day.isoformat(),
        "weekday": plan.day.strftime("%A"),
        "now": lang.spoken_time(local, said_in),
        "language": said_in,
        "planned": _said_clocks([
            {
                "id": item.item_id,
                "title": _masked(item.start or item.window_from, plan.day, zone, said_in)
                if item.private else item.title,
                "start": item.start, "minutes": item.minutes, "done": item.done,
            }
            for item in plan.items
        ], plan.day, zone, said_in),
        "lived": list(lived),
        "tomorrow": _said_clocks([
            {
                "title": _masked(row.get("start"), tomorrow_day, zone, said_in)
                if row.get("private") else row["title"],
                "start": row.get("start"),
                "place": "" if row.get("private") else row.get("place", ""),
            }
            for row in tomorrow
        ], tomorrow_day, zone, said_in),
    }
    return _instructions(), json.dumps(body, ensure_ascii=False, default=str)


def _object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model answer: bare, in a code fence, or inside a little prose."""
    body = _FENCE.sub("", text.strip()).strip()
    for candidate in (body, body[body.find("{"): body.rfind("}") + 1]):
        try:
            found = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return None


def parse_message(text: str) -> str:
    """The message of a morning or evening answer, or ValueError when it has none.

    The answer is ``{"message": "..."}`` (the evening's holds more, see :func:`parse_evening`); the
    message is 1 to :data:`MESSAGE_CHARS` characters.
    """
    found = _object(text)
    message = found.get("message") if found else None
    if not isinstance(message, str) or not message.strip():
        msg = "the answer has no message"
        raise ValueError(msg)
    message = message.strip()
    if len(message) > MESSAGE_CHARS:
        msg = f"the message is {len(message)} characters, over {MESSAGE_CHARS}"
        raise ValueError(msg)
    return message


_CLOCK: Final[re.Pattern[str]] = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _said_clocks(value: Any, day: date, zone: tzinfo, said_in: lang.Language) -> Any:  # noqa: ANN401
    """``value`` with every ``HH:MM`` string as it is said aloud (``晚上9点35分``, ``9:35 PM``).

    What she says comes from these facts; a clock in digits comes back as 二十一点三十五.
    """
    if isinstance(value, dict):
        return {k: _said_clocks(v, day, zone, said_in) for k, v in value.items()}
    if isinstance(value, list):
        return [_said_clocks(v, day, zone, said_in) for v in value]
    if isinstance(value, str) and _CLOCK.fullmatch(value):
        return _spoken(value, day, zone, said_in)
    return value


@dataclass(frozen=True)
class Evening:
    """The evening's answer: the message, ``(item_id, to)`` for each task left undone, and notes.

    ``notes`` say what was dropped from the answer or defaulted, and why.
    """

    message: str
    unfinished: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()


def parse_evening(text: str, plan: Plan) -> Evening:
    """The evening in a model's answer ``{"message": "...", "unfinished": [{"id", "to"}]}``.

    ValueError when there is no message (see :func:`parse_message`). An ``unfinished`` entry is
    kept only when its id is a task of ``plan`` that is not done and its ``to`` is one of
    :data:`jarvis.state.plan.DESTINATIONS`; the first entry for an id wins. A task not done that
    the answer leaves out goes ``later`` (her default), noted. The result is in the plan's order.
    """
    message = parse_message(text)
    found = _object(text) or {}
    tasks = [i for i in plan.items if is_task(i) and not i.done]
    raw = found.get("unfinished")
    entries = raw if isinstance(raw, list) else []
    notes: list[str] = [] if raw is None or isinstance(raw, list) else ["unfinished is not a list"]
    chosen: dict[str, str] = {}
    for entry in entries:
        item_id = str(entry.get("id")) if isinstance(entry, dict) else ""
        to = entry.get("to") if isinstance(entry, dict) else None
        if not any(t.item_id == item_id for t in tasks):
            notes.append(f"unfinished {item_id!r} dropped: not a task of the plan left undone")
        elif to not in DESTINATIONS:
            notes.append(f"unfinished {item_id!r} dropped: {to!r} is not one of {DESTINATIONS}")
        elif item_id not in chosen:
            chosen[item_id] = to
    notes += [f"{t.item_id} left out: later" for t in tasks if t.item_id not in chosen]
    return Evening(
        message, tuple((t.item_id, chosen.get(t.item_id, "later")) for t in tasks), tuple(notes),
    )


def _minute(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


def _span(item: PlanItem) -> tuple[int, int] | None:
    if item.start is None:
        return None
    begin = _minute(item.start)
    return begin, min(begin + item.minutes, _DAY_MINUTES)


def _collides(item: PlanItem, fixed: Sequence[PlanItem]) -> PlanItem | None:
    mine = _span(item)
    if mine is None:
        return None
    for other in fixed:
        theirs = _span(other)
        if theirs and mine[0] < theirs[1] and theirs[0] < mine[1]:
            return other
    return None


def _model_item(raw: object) -> PlanItem:
    """One item as the model wrote it; raises on anything not usable."""
    if not isinstance(raw, dict):
        msg = "not an object"
        raise TypeError(msg)
    minutes = raw.get("minutes", 60)
    prepare = raw.get("prepare") or []
    if not isinstance(prepare, list):
        msg = "prepare is not a list"
        raise TypeError(msg)
    item = PlanItem(
        item_id="x",
        title=str(raw["title"]).strip() if isinstance(raw["title"], str) else "",
        firmness=str(raw["firmness"]),
        source=str(raw["source"]),
        start=raw.get("start"),
        minutes=minutes if isinstance(minutes, int) else 0,
        window_from=raw.get("window_from"),
        window_to=raw.get("window_to"),
        place=str(raw.get("place") or "").strip(),
        why=str(raw.get("why") or "").strip(),
        goal=str(raw.get("goal") or "").strip(),
        prepare=tuple(str(p).strip() for p in prepare if str(p).strip()),
        private=raw.get("private") is True,
    )
    check_item(item)
    return item


def parse_items(
    text: str, *, known_sources: Collection[str], fixed: Sequence[PlanItem],
) -> tuple[list[PlanItem], list[str]]:
    """``(items, dropped_notes)`` from the model's answer to a draft.

    An item is dropped (and noted) when it does not parse, overlaps a ``fixed`` item in time or
    names a source outside ``known_sources``. An item whose source is a fixed item's keeps that
    fixed item as it is and lends it ``why``, ``goal`` and ``prepare`` it lacks. Every fixed item
    comes back whether or not the model listed it: where the model put it, else at the end. An
    answer that is not JSON gives no items (nothing is added back) and one note. The order of the
    answer is the plan's order; items are numbered ``p1``.. in that order.
    """
    found = _object(text)
    raw_items = found.get("items") if found else None
    if not isinstance(raw_items, list):
        return [], ["the answer is not a JSON object with an items list"]
    notes: list[str] = []
    if len(raw_items) > MAX_ITEMS:
        notes.append(f"{len(raw_items) - MAX_ITEMS} items past the first {MAX_ITEMS} dropped")
        raw_items = raw_items[:MAX_ITEMS]
    by_source = {item.source: item for item in fixed if item.source.startswith(ITEM_PREFIX)}
    kept: list[PlanItem] = []
    lent: dict[str, PlanItem] = {}
    for index, raw in enumerate(raw_items, 1):
        try:
            item = _model_item(raw)
        except (KeyError, TypeError, ValueError) as exc:
            notes.append(f"item {index} dropped: unreadable ({exc})")
            continue
        base = by_source.get(item.source)
        if base is not None:
            if item.source not in lent:
                lent[item.source] = replace(
                    base, why=base.why or item.why, goal=base.goal or item.goal,
                    prepare=base.prepare or item.prepare,
                )
                kept.append(lent[item.source])
        elif item.source not in known_sources:
            notes.append(f"item {index} ({item.title}) dropped: unknown source {item.source}")
        elif (clash := _collides(item, fixed)) is not None:
            notes.append(f"item {index} ({item.title}) dropped: overlaps {clash.title}")
        else:
            kept.append(item)
    kept.extend(f for f in fixed if f.source not in lent)
    return [replace(item, item_id=f"p{n}") for n, item in enumerate(kept[:MAX_ITEMS], 1)], notes


def _hm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _local_day(ms: int, zone: tzinfo) -> date:
    return datetime.fromtimestamp(ms / 1000, zone).date()


def fixed_items(
    occurrences: Sequence[Occurrence], day: date, zone: tzinfo,
) -> list[PlanItem]:
    """The fixed items of ``day`` from the ledger's timed occurrences (starting that local day).

    A timed one that runs past midnight is cut at midnight. An all-day one is no item: it is the
    day's label or a task (:func:`day_labels`, :func:`candidate_things`).
    """
    items: list[PlanItem] = []
    for occ in occurrences:
        if occ.all_day or _local_day(occ.start_ms, zone) != day:
            continue
        one = occ.thing
        start = _hm(datetime.fromtimestamp(occ.start_ms / 1000, zone))
        until_midnight = _DAY_MINUTES - _minute(start)
        minutes = max(1, min(round((occ.end_ms - occ.start_ms) / 60_000), until_midnight))
        items.append(PlanItem(
            item_id=one.thing_id, title=one.title[:120], firmness="fixed",
            source=f"{ITEM_PREFIX}{one.thing_id}", start=start, minutes=minutes,
            place=one.place, private=one.private,
        ))
    return items


def day_labels(occurrences: Sequence[Occurrence], day: date, zone: tzinfo) -> list[str]:
    """The titles of what he goes to on ``day`` that has a day and no hour (a trip, a holiday)."""
    return [
        occ.thing.title[:120] for occ in occurrences
        if occ.all_day and occ.thing.kind == "go"
        and _local_day(occ.start_ms, zone) == day
    ]


def candidate_things(  # noqa: PLR0913 - what is open, what is on, the day, the zone and how far
    open_things: Sequence[Thing], occurrences: Sequence[Occurrence], day: date, zone: tzinfo,
    horizon_end: date, limit: int,
) -> list[dict[str, Any]]:
    """The open things that may become tasks of ``day``, in the program's order, at most ``limit``.

    Late ones (a ``do`` whose time went by before the day), the ones due by ``horizon_end``, the
    ``do`` things on the day itself, and the untimed ones: ``open_things`` already puts timed
    things soonest first and untimed ones oldest first, so a due thing comes before one with no
    time. A ``go`` thing is a fixed item or a label, not a task.
    """
    first = int(datetime.combine(day, time.min, zone).timestamp() * 1000)
    on_day = {
        occ.thing.thing_id for occ in occurrences
        if occ.all_day and occ.thing.kind == "do"
        and _local_day(occ.start_ms, zone) == day
    }
    behind = {one.thing_id for one in late(open_things, first, zone)}
    rows: list[dict[str, Any]] = []
    for one in open_things:
        by = None
        if one.due:
            at = _local_day(things.due_ms(one, zone), zone)
            if at > horizon_end:
                continue
            by = one.due if due_is_day(one) else at.isoformat()
        elif one.thing_id in behind:
            by = _local_day(when_ms(one, zone) or first, zone).isoformat()
        elif one.thing_id in on_day:
            by = day.isoformat()
        elif one.start_at or one.on_day:
            continue
        rows.append({
            "title": one.title, "due": by, "late": one.thing_id in behind,
            "private": one.private, "source": f"{ITEM_PREFIX}{one.thing_id}",
        })
    return rows[:limit]
