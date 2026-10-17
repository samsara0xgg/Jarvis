"""The day-plan prompts and the parser of the model's answers (ADR 0288). Pure: no I/O, no clock.

The nightly draft, the morning opening and the judgement of a notice (ADR 0289) all use the
``day-plan`` skill. The model's answer is untrusted JSON: :func:`parse_items` keeps only the items
that read, do not collide with a fixed item and name a known source, and adds back any fixed item
the model left out; the program, not the model, owns the commitments of the day.
:func:`parse_decision` turns anything malformed in a notice's answer into silence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.plan_watch import budget_facts
from jarvis.shared import lang
from jarvis.shared.skills import load_skill
from jarvis.state.plan import ACTS, MAX_ITEMS, Plan, PlanItem, apply_changes, check_item
from jarvis.state.plan_budget import budget, is_task

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.decision.plan_watch import Notice
    from jarvis.state.commitments import Occurrence
    from jarvis.state.plan import Decided
    from jarvis.state.plan_facts import DayFacts

OPENING_CHARS: Final[int] = 600
"""Longest opening the parser accepts; a longer one is not the short message asked for."""
SAY_CHARS: Final[int] = 300
"""Longest line the parser accepts for a notice: one or two short sentences."""
WHY_CHARS: Final[int] = 300
_DAY_MINUTES: Final[int] = 24 * 60
_FENCE: Final[re.Pattern[str]] = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


@dataclass(frozen=True)
class DayPlanInputs:
    """Everything the draft may use for one day, as plain JSON-able values."""

    day: date
    fixed: tuple[PlanItem, ...] = ()
    upcoming: tuple[Mapping[str, Any], ...] = ()
    calendar_all_day: tuple[str, ...] = ()
    todo: tuple[Mapping[str, Any], ...] = ()
    goals: tuple[str, ...] = ()
    deadlines: tuple[str, ...] = ()
    routines: tuple[Mapping[str, Any], ...] = ()
    weather: Mapping[str, Any] | None = None
    recent_days: tuple[Mapping[str, Any], ...] = ()
    his_changes: tuple[Mapping[str, Any], ...] = ()
    past_plans: tuple[Mapping[str, Any], ...] = ()
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
            "todo": list(self.todo),
            "goals": list(self.goals),
            "deadlines": list(self.deadlines),
            "routines": list(self.routines),
            "weather": self.weather,
            "recent_days": list(self.recent_days),
            "his_changes": list(self.his_changes),
            "past_plans": list(self.past_plans),
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


def build_opening_prompt(  # noqa: PLR0913 - the plan, what is open, the weather, the clock, a failure.
    plan: Plan, *, unconfirmed: Sequence[Mapping[str, Any]], weather: Mapping[str, Any] | None,
    now: datetime, zone: tzinfo, draft_failed: bool = False,
) -> tuple[str, str]:
    """``(system, user)`` of the opening call: the plan, what he has not confirmed, the weather.

    ``draft_failed`` says the day's draft did not come, so ``plan`` is empty: she says so and asks.
    """
    local = now.astimezone(zone)
    body = {
        "task": "opening",
        "day": plan.day.isoformat(),
        "weekday": plan.day.strftime("%A"),
        "now": local.strftime("%H:%M"),
        "timezone": _zone_name(zone),
        "settled": plan.status == "settled",
        "draft_failed": draft_failed,
        "plan": [_public(item) for item in plan.items],
        "unconfirmed": list(unconfirmed),
        "weather": weather,
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


def parse_opening(text: str) -> str:
    """The opening message, or ValueError when the answer is not ``{"message": "..."}``."""
    found = _object(text)
    message = found.get("message") if found else None
    if not isinstance(message, str) or not message.strip():
        msg = "the opening answer has no message"
        raise ValueError(msg)
    message = message.strip()
    if len(message) > OPENING_CHARS:
        msg = f"the opening is {len(message)} characters, over {OPENING_CHARS}"
        raise ValueError(msg)
    return message


@dataclass(frozen=True)
class Decision:
    """What she made of a notice: ``silent``, ``tell`` (``say``) or ``adjust`` (also ``changes``).

    ``changes`` are ops for :func:`jarvis.state.plan.apply_changes`; ``notes`` say why an answer
    was turned into silence.
    """

    act: str
    say: str = ""
    changes: tuple[dict[str, Any], ...] = ()
    why: str = ""
    notes: tuple[str, ...] = ()


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
        hour, minute = (int(part) for part in value.split(":"))
        moment = datetime.combine(day, datetime.min.time(), zone).replace(hour=hour, minute=minute)
        return lang.spoken_time(moment, said_in)
    return value


def build_decide_prompt(  # noqa: PLR0913 - the plan, the notice, the facts, the clock, what was said.
    plan: Plan, notice: Notice, facts: DayFacts, now: datetime,
    told_today: Sequence[Decided], *, zone: tzinfo,
) -> tuple[str, str]:
    """``(system, user)`` of the notice call: the skill, and what she needs to judge it.

    The plan's items carry their computed ``route`` (read-only for her); ``told_today`` is what she
    already said today, so she does not say it twice. Every clock time is written as she says it,
    in the language of the plan's titles: given ``21:35`` she says 二十一点三十五, and given a mix
    of forms she misreads one against the other. Her changes still write ``HH:MM``.
    """
    local = now.astimezone(zone)
    said_in = lang.text_language(" ".join(item.title for item in plan.items))
    body = {
        "task": "notice",
        "day": plan.day.isoformat(),
        "weekday": plan.day.strftime("%A"),
        "now": lang.spoken_time(local, said_in),
        "timezone": _zone_name(zone),
        "settled": plan.status == "settled",
        "plan": _said_clocks([item.payload() for item in plan.items], plan.day, zone, said_in),
        "notice": {
            "key": notice.key, "kind": notice.kind, "item_id": notice.item_id,
            "facts": _said_clocks(notice.facts, plan.day, zone, said_in),
        },
        "facts": facts.payload(zone),
        "told_today": [
            {"at": lang.spoken_time(datetime.fromtimestamp(d.at_ms / 1000, zone), said_in),
             "say": d.say}
            for d in told_today if d.act != "silent"
        ],
    }
    return _instructions(), json.dumps(body, ensure_ascii=False, default=str)


def _decide(found: Mapping[str, Any], plan: Plan) -> Decision:
    """The decision ``found`` says; ValueError when it is malformed."""
    act = found.get("act")
    if act not in ACTS:
        msg = f"act {act!r} is not one of {ACTS}"
        raise ValueError(msg)
    why = found.get("why")
    why = why.strip()[:WHY_CHARS] if isinstance(why, str) else ""
    if act == "silent":
        return Decision("silent", why=why)
    say = found.get("say")
    if not isinstance(say, str) or not say.strip() or len(say.strip()) > SAY_CHARS:
        msg = f"{act} needs a say of 1 to {SAY_CHARS} characters"
        raise ValueError(msg)
    if act == "tell":
        return Decision("tell", say.strip(), why=why)
    changes = found.get("changes")
    if not isinstance(changes, list) or not changes:
        msg = "adjust needs a non-empty changes list"
        raise ValueError(msg)
    if notes := apply_changes(plan.items, changes)[1]:
        msg = "; ".join(notes)
        raise ValueError(msg)
    return Decision("adjust", say.strip(), tuple(changes), why)


def parse_decision(text: str, plan: Plan) -> Decision:
    """The decision in a model's answer to a notice; anything malformed becomes ``silent``.

    ``tell`` and ``adjust`` need a ``say`` of at most :data:`SAY_CHARS`; ``adjust`` needs changes
    that all apply to ``plan`` (a single op that does not read turns the whole answer to silence:
    a half-applied adjustment is worse than none). A ``silent`` or ``tell`` carries no changes.
    """
    found = _object(text)
    if found is None:
        return Decision("silent", notes=("the answer is not a JSON object",))
    try:
        return _decide(found, plan)
    except ValueError as exc:
        return Decision("silent", notes=(str(exc),))


def build_progress_prompt(
    plan: Plan, notice: Notice, now: datetime, *, zone: tzinfo,
) -> tuple[str, str]:
    """``(system, user)`` of the done-check call of a ``progress`` notice (task ``progress``).

    Input: the tasks in order (id, title, minutes, done) and the notice's ``evidence`` (the screen
    activities, his words and the commits since the last check). Clocks stay ``HH:MM``: nothing
    in this answer is spoken.
    """
    evidence = notice.facts.get("evidence") or {}
    body = {
        "task": "progress",
        "day": plan.day.isoformat(),
        "now": now.astimezone(zone).strftime("%H:%M"),
        "tasks": [
            {"id": i.item_id, "title": i.title, "minutes": i.minutes, "done": i.done}
            for i in plan.items if is_task(i)
        ],
        "evidence": {k: evidence.get(k, []) for k in ("screen", "words", "commits")}
        | {"since": evidence.get("since")},
    }
    return _instructions(), json.dumps(body, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Progress:
    """What the evidence shows of the tasks: ``done`` ids, and ``left`` minutes by id (under way).

    ``notes`` say what was dropped from the answer and why.
    """

    done: tuple[str, ...] = ()
    left: dict[str, int] = field(default_factory=dict)
    why: str = ""
    notes: tuple[str, ...] = ()

    def changes(self) -> list[dict[str, Any]]:
        """The edits for :func:`jarvis.state.plan.apply_changes`: done true, minutes the rest."""
        return [
            *({"op": "edit", "id": n, "done": True} for n in self.done),
            *({"op": "edit", "id": n, "minutes": m} for n, m in self.left.items()),
        ]


def _listed(found: Mapping[str, Any], name: str, kind: type, notes: list[str]) -> Any:  # noqa: ANN401
    """``found[name]`` when it is a ``kind``; otherwise nothing, and a note unless it is absent."""
    value = found.get(name)
    if isinstance(value, kind):
        return value
    if value is not None:
        notes.append(f"{name} is not a {kind.__name__}")
    return kind()


def parse_progress(text: str, plan: Plan) -> Progress:
    """The done-check in a model's answer ``{"done": [...], "left": {"p3": 40}, "why": "..."}``.

    Untrusted: each id must be a task of ``plan`` that is not done (a timed item is not marked
    here); ``left`` minutes must be a whole number from 1 to the task's own estimate (a larger
    figure is the decide call's to change), and ``done`` wins over ``left`` for one id. Anything
    else is dropped and noted; an answer that is no JSON object is an empty result.
    """
    found = _object(text)
    if found is None:
        return Progress(notes=("the answer is not a JSON object",))
    tasks = {i.item_id: i for i in plan.items if is_task(i)}
    notes: list[str] = []

    def task(raw: object, what: str) -> PlanItem | None:
        item = tasks.get(raw) if isinstance(raw, str) else None
        if item is None or item.done:
            why = "not a task of the plan" if item is None else "already done"
            notes.append(f"{what} {raw!r} dropped: {why}")
            return None
        return item

    done = list(dict.fromkeys(
        i.item_id for n in _listed(found, "done", list, notes) if (i := task(n, "done"))
    ))
    left: dict[str, int] = {}
    for name, minutes in _listed(found, "left", dict, notes).items():
        if (item := task(name, "left")) is None:
            continue
        if name in done:
            notes.append(f"left {name!r} dropped: marked done")
        elif isinstance(minutes, bool) or not isinstance(minutes, int) or not (
            1 <= minutes <= item.minutes
        ):
            notes.append(f"left {name!r} dropped: {minutes!r} is not 1 to {item.minutes} minutes")
        elif minutes != item.minutes:
            left[name] = minutes
    why = found.get("why")
    return Progress(
        tuple(done), left, why.strip()[:WHY_CHARS] if isinstance(why, str) else "", tuple(notes),
    )


def unfit_notice(
    plan: Plan, notice: Notice, marked: Progress, now: datetime, day_ends: time,
) -> Notice | None:
    """The ``progress`` notice to put to the decide call, or None when the tasks fit the day.

    ``plan`` is the plan after ``marked`` was applied. The notice's facts get the budget worked
    out again and ``marked`` (what the done-check changed); the evidence stays out of the call.
    """
    if budget(plan, now, day_ends=day_ends).fits:
        return None
    facts = {k: v for k, v in notice.facts.items() if k != "evidence"}
    facts |= budget_facts(plan, now, day_ends) or {}
    if marked.done or marked.left:
        facts["marked"] = {"done": list(marked.done), "left": marked.left}
    return replace(notice, facts=facts)


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
    by_source = {item.source: item for item in fixed if item.source.startswith("commitment:")}
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


def commitment_items(
    occurrences: Sequence[Occurrence], day: date, zone: tzinfo,
) -> list[PlanItem]:
    """The fixed items of ``day`` from commitment occurrences (starting that local day).

    An all-day commitment has no start and lasts the day; a timed one that runs past midnight is
    cut at midnight.
    """
    items: list[PlanItem] = []
    for occ in occurrences:
        if occ.day != day.isoformat():
            continue
        c = occ.commitment
        begin = datetime.fromtimestamp(occ.start_ms / 1000, zone)
        if c.all_day:
            start, minutes = None, _DAY_MINUTES
        else:
            start = _hm(begin)
            until_midnight = _DAY_MINUTES - _minute(start)
            minutes = max(1, min(c.minutes, until_midnight))
        items.append(PlanItem(
            item_id=c.commitment_id, title=c.title[:120], firmness="fixed",
            source=f"commitment:{c.commitment_id}", start=start, minutes=minutes,
            place=c.place, private=c.private,
        ))
    return items


def calendar_items(
    events: Sequence[Mapping[str, Any]], day: date, zone: tzinfo, taken: Sequence[PlanItem],
) -> tuple[list[PlanItem], list[str]]:
    """``(fixed items, all-day labels)`` from the day's calendar events (``Home.calendar`` rows).

    A timed event whose start and title a commitment already holds is left out: the same event
    twice. Events that cannot be read are skipped.
    """
    items: list[PlanItem] = []
    labels: list[str] = []
    held = {(item.start, item.title.casefold()) for item in taken}
    for event in events:
        try:
            title = str(event.get("title") or "").strip()[:120]
            if not title:
                continue
            if event.get("all_day"):
                labels.append(title)
                continue
            begin = datetime.fromisoformat(str(event["start"])).astimezone(zone)
            end = datetime.fromisoformat(str(event["end"])).astimezone(zone)
            if begin.date() != day or (begin.strftime("%H:%M"), title.casefold()) in held:
                continue
            minutes = max(1, min(int((end - begin) / timedelta(minutes=1)),
                                 _DAY_MINUTES - _minute(_hm(begin))))
            items.append(PlanItem(
                item_id=f"calendar-{len(items)}", title=title, firmness="fixed", source="his",
                start=_hm(begin), minutes=minutes,
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return items, labels
