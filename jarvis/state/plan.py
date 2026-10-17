"""The plan of a day, folded from the event log (ADR 0288).

There is no plan table: a date's plan is its last ``plan.drafted`` or ``plan.revised`` (each
carries the whole item list), marked settled by a ``plan.settled`` and opened by a
``plan.opened``. ``plan.draft_failed`` says the nightly draft did not come. The nightly job
(runtime), her ``plan`` tool, the opening and the phone all read the same fold.

The watcher (ADR 0289) adds two more types beside the fold: ``plan.noticed`` (the program saw the
day depart from the plan) and ``plan.decided`` (what she made of it: silent, tell or adjust).
Both are read back per date, never folded into the plan. :func:`apply_changes` is the one place
that turns a list of add, edit and drop ops into items, for her ``plan`` tool and her decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

TYPES: Final[tuple[str, ...]] = (
    "plan.drafted",
    "plan.revised",
    "plan.settled",
    "plan.opened",
    "plan.draft_failed",
)
WATCH_TYPES: Final[tuple[str, ...]] = ("plan.noticed", "plan.decided")
ACTS: Final[tuple[str, ...]] = ("silent", "tell", "adjust")
ACTOR: Final[str] = "jarvis_runtime"
FIRMNESS: Final[tuple[str, ...]] = ("fixed", "movable", "droppable")
BY: Final[tuple[str, ...]] = ("him", "her", "program")
SETTLED_BY: Final[tuple[str, ...]] = ("him", "first_item")
SOURCE_NAMES: Final[tuple[str, ...]] = ("todo", "goal", "his")
SOURCE_PREFIXES: Final[tuple[str, ...]] = ("commitment:", "routine:")
TITLE_CHARS: Final[int] = 120
MAX_ITEMS: Final[int] = 40
ERROR_CHARS: Final[int] = 200
CHANGE_CHARS: Final[int] = 200
NOTE_CHARS: Final[int] = 300
_DAY_MINUTES: Final[int] = 24 * 60
_UNPLACED: Final[str] = "99:99"
_HM: Final[re.Pattern[str]] = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


@dataclass(frozen=True)
class PlanItem:
    """One thing in a day.

    ``start`` is ``HH:MM`` local, or None for an item with only a window or no time at all.
    ``source`` says where it came from: ``commitment:<id>``, ``todo``, ``goal``, ``routine:<id>``
    or ``his``. ``route`` is the computed leg to the place (leave at, line, stops, arrive at, a
    backup), filled in by the program, never by the model.
    """

    item_id: str
    title: str
    firmness: str
    source: str
    start: str | None = None
    minutes: int = 60
    window_from: str | None = None
    window_to: str | None = None
    place: str = ""
    why: str = ""
    goal: str = ""
    prepare: tuple[str, ...] = ()
    private: bool = False
    route: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        """The item as it is written to the log."""
        return {
            "item_id": self.item_id, "title": self.title, "start": self.start,
            "minutes": self.minutes, "window_from": self.window_from,
            "window_to": self.window_to, "place": self.place, "firmness": self.firmness,
            "why": self.why, "goal": self.goal, "prepare": list(self.prepare),
            "source": self.source, "private": self.private, "route": self.route,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> PlanItem:
        """The item a ``payload()`` said; fields a newer writer added are ignored."""
        route = raw.get("route")
        return cls(
            item_id=str(raw["item_id"]),
            title=str(raw["title"]),
            firmness=str(raw["firmness"]),
            source=str(raw["source"]),
            start=raw.get("start"),
            minutes=int(raw.get("minutes") or 60),
            window_from=raw.get("window_from"),
            window_to=raw.get("window_to"),
            place=str(raw.get("place") or ""),
            why=str(raw.get("why") or ""),
            goal=str(raw.get("goal") or ""),
            prepare=tuple(str(p) for p in raw.get("prepare") or ()),
            private=bool(raw.get("private")),
            route=dict(route) if isinstance(route, dict) else None,
        )


@dataclass
class Plan:
    """One date's plan as the log leaves it."""

    day: date
    items: tuple[PlanItem, ...]
    status: str = "draft"  # draft | settled
    settled_by: str | None = None
    opened: bool = False
    opened_record_id: str | None = None
    opened_signal: str | None = None
    opened_at_ms: int | None = None
    source: str = "drafted"  # drafted | revised:<by>
    digest: str = ""


@dataclass(frozen=True)
class Revision:
    """One ``plan.revised``: who, why, and what it changed in a few words."""

    day: str
    by: str
    why: str
    change: str
    at_ms: int


def check_item(item: PlanItem) -> None:
    """ValueError when ``item`` could not be read back or acted on."""
    if not item.item_id.strip():
        msg = "plan item needs an item_id"
        raise ValueError(msg)
    title = item.title.strip()
    if not title or len(title) > TITLE_CHARS:
        msg = f"plan item title must be 1 to {TITLE_CHARS} characters"
        raise ValueError(msg)
    if item.firmness not in FIRMNESS:
        msg = f"plan item firmness must be one of {FIRMNESS}, not {item.firmness!r}"
        raise ValueError(msg)
    if isinstance(item.minutes, bool) or not 1 <= item.minutes <= _DAY_MINUTES:
        msg = f"plan item minutes must be 1 to {_DAY_MINUTES}, not {item.minutes!r}"
        raise ValueError(msg)
    for name in ("start", "window_from", "window_to"):
        value = getattr(item, name)
        if value is not None and not (isinstance(value, str) and _HM.fullmatch(value)):
            msg = f"plan item {name} must be HH:MM or None, not {value!r}"
            raise ValueError(msg)
    if item.source not in SOURCE_NAMES and not (
        item.source.startswith(SOURCE_PREFIXES) and item.source.partition(":")[2]
    ):
        msg = f"plan item source must be one of {SOURCE_NAMES} or {SOURCE_PREFIXES}<id>"
        raise ValueError(msg)


def _items_payload(items: Iterable[PlanItem]) -> list[dict[str, Any]]:
    held = list(items)
    if len(held) > MAX_ITEMS:
        msg = f"a plan holds at most {MAX_ITEMS} items"
        raise ValueError(msg)
    for item in held:
        check_item(item)
    if len({item.item_id for item in held}) != len(held):
        msg = "plan item ids must be unique within a day"
        raise ValueError(msg)
    return [item.payload() for item in held]


def _day(day: date) -> str:
    return day.isoformat()


def draft(
    conn: sqlite3.Connection, day: date, items: Sequence[PlanItem], digest: str, model: str,
) -> None:
    """Append ``plan.drafted``: the nightly draft of ``day`` from inputs hashing to ``digest``."""
    payload = {"date": _day(day), "items": _items_payload(items), "digest": digest, "model": model}
    emit_event(conn, type="plan.drafted", payload=payload, actor=ACTOR)


def revise(
    conn: sqlite3.Connection, day: date, items: Sequence[PlanItem], by: str, why: str,
) -> None:
    """Append ``plan.revised``: the whole new item list, by ``him``, ``her`` or ``program``."""
    if by not in BY:
        msg = f"plan revision by must be one of {BY}, not {by!r}"
        raise ValueError(msg)
    payload = {"date": _day(day), "items": _items_payload(items), "by": by, "why": why}
    emit_event(conn, type="plan.revised", payload=payload, actor=ACTOR)


def settle(
    conn: sqlite3.Connection, day: date, by: str,
) -> None:
    """Append ``plan.settled``: ``him`` said so, or the program did when the first item started."""
    if by not in SETTLED_BY:
        msg = f"plan settled by must be one of {SETTLED_BY}, not {by!r}"
        raise ValueError(msg)
    emit_event(conn, type="plan.settled", payload={"date": _day(day), "by": by}, actor=ACTOR)


def opened(
    conn: sqlite3.Connection, day: date, record_id: str, signal: str, at_ms: int,
) -> None:
    """Append ``plan.opened``: her opening is in the conversation as ``record_id``."""
    if not record_id.strip() or not signal.strip():
        msg = "plan.opened needs a record id and the signal that opened it"
        raise ValueError(msg)
    payload = {"date": _day(day), "record_id": record_id, "signal": signal, "at_ms": int(at_ms)}
    emit_event(conn, type="plan.opened", payload=payload, actor=ACTOR)


def draft_failed(
    conn: sqlite3.Connection, day: date, error: str,
) -> None:
    """Append ``plan.draft_failed``: no draft came for ``day``; ``error`` is a short class."""
    payload = {"date": _day(day), "error": error[:ERROR_CHARS]}
    emit_event(conn, type="plan.draft_failed", payload=payload, actor=ACTOR)


def noticed(  # noqa: PLR0913 - one notice is these six fields.
    conn: sqlite3.Connection, day: date, key: str, kind: str, item_id: str | None,
    facts: Mapping[str, Any],
) -> None:
    """Append ``plan.noticed``: the program saw ``kind`` (about ``item_id``, if any) on ``day``."""
    if not key.strip() or not kind.strip():
        msg = "plan.noticed needs a key and a kind"
        raise ValueError(msg)
    payload = {
        "date": _day(day), "key": key, "kind": kind, "item_id": item_id, "facts": dict(facts),
    }
    emit_event(conn, type="plan.noticed", payload=payload, actor=ACTOR)


def decided(  # noqa: PLR0913 - one decision is these seven fields.
    conn: sqlite3.Connection, day: date, key: str, act: str, say: str, why: str, model: str,
) -> None:
    """Append ``plan.decided``: what she made of the notice ``key``, and the model that did."""
    if act not in ACTS:
        msg = f"plan decision act must be one of {ACTS}, not {act!r}"
        raise ValueError(msg)
    payload = {
        "date": _day(day), "key": key, "act": act, "say": say, "why": why[:NOTE_CHARS],
        "model": model,
    }
    emit_event(conn, type="plan.decided", payload=payload, actor=ACTOR)


@dataclass(frozen=True)
class Noticed:
    """One ``plan.noticed`` as the log holds it."""

    key: str
    kind: str
    item_id: str | None
    facts: dict[str, Any]
    at_ms: int


@dataclass(frozen=True)
class Decided:
    """One ``plan.decided`` as the log holds it. ``act`` ``tell`` and ``adjust`` were said."""

    key: str
    act: str
    say: str
    why: str
    model: str
    at_ms: int


def notices(conn: sqlite3.Connection, day: date) -> dict[str, Noticed]:
    """``day``'s notices by key, the first written for each key."""
    found: dict[str, Noticed] = {}
    for event in iter_events_of_types(conn, ("plan.noticed",)):
        p = event.payload
        if p["date"] == _day(day) and p["key"] not in found:
            found[str(p["key"])] = Noticed(
                str(p["key"]), str(p["kind"]), p.get("item_id"), dict(p["facts"]),
                event.ts_epoch_ms,
            )
    return found


def decisions(conn: sqlite3.Connection, day: date) -> list[Decided]:
    """``day``'s decisions in the order they were written. Told today: ``act`` tell or adjust."""
    return [
        Decided(
            str(p["key"]), str(p["act"]), str(p["say"]), str(p["why"]), str(p["model"]),
            event.ts_epoch_ms,
        )
        for event in iter_events_of_types(conn, ("plan.decided",))
        if (p := event.payload)["date"] == _day(day)
    ]


_FIELDS: Final[frozenset[str]] = frozenset({
    "title", "start", "minutes", "window_from", "window_to", "place", "firmness", "why", "goal",
    "prepare", "source", "private",
})
_TEXT_FIELDS: Final[tuple[str, ...]] = ("title", "firmness", "source", "place", "why", "goal")


def _coerce(fields: Mapping[str, Any]) -> dict[str, Any]:
    """``fields`` with the types a PlanItem takes; TypeError or ValueError on anything else."""
    unknown = set(fields) - _FIELDS
    if unknown:
        msg = f"unknown field {sorted(unknown)[0]}"
        raise ValueError(msg)
    out: dict[str, Any] = dict(fields)
    for name in _TEXT_FIELDS:
        if name in out:
            if not isinstance(out[name], str):
                msg = f"{name} must be text"
                raise TypeError(msg)
            out[name] = out[name].strip()
    if "prepare" in out:
        held = out["prepare"]
        if not isinstance(held, list) or not all(isinstance(p, str) for p in held):
            msg = "prepare must be a list of text"
            raise TypeError(msg)
        out["prepare"] = tuple(p.strip() for p in out["prepare"] if p.strip())
    if "private" in out and not isinstance(out["private"], bool):
        msg = "private must be true or false"
        raise TypeError(msg)
    return out  # start, minutes and the windows are checked by check_item


def _next_id(items: Iterable[PlanItem]) -> str:
    numbers = [int(i.item_id[1:]) for i in items if re.fullmatch(r"p\d+", i.item_id)]
    return f"p{max(numbers, default=0) + 1}"


def _sort_key(item: PlanItem) -> tuple[str, str]:
    return item.start or item.window_from or _UNPLACED, item.title


def _apply_op(items: list[PlanItem], op: object) -> list[PlanItem]:
    """``items`` after one op; ValueError or TypeError when the op cannot be applied."""
    if not isinstance(op, dict):
        msg = "not an object"
        raise TypeError(msg)
    kind = op.get("op")
    if kind == "add":
        if len(items) >= MAX_ITEMS:
            msg = f"a plan holds at most {MAX_ITEMS} items"
            raise ValueError(msg)
        raw = op.get("item")
        if not isinstance(raw, dict):
            msg = "add needs an item"
            raise TypeError(msg)
        fields = {"firmness": "movable", "source": "his"} | _coerce(raw)
        item = PlanItem(item_id=_next_id(items), **fields)
        check_item(item)
        return [*items, item]
    target = next((i for i in items if i.item_id == op.get("id")), None)
    if target is None:
        msg = f"no item {op.get('id')!r}"
        raise ValueError(msg)
    if kind == "drop":
        return [i for i in items if i is not target]
    if kind == "edit":
        fields = _coerce({k: v for k, v in op.items() if k not in ("op", "id")})
        edited = replace(target, **fields)
        if edited.start != target.start or edited.place != target.place:
            edited = replace(edited, route=None)  # the leg was computed for the old time or place
        check_item(edited)
        return [edited if i is target else i for i in items]
    msg = f"unknown op {kind!r}"
    raise ValueError(msg)


def apply_changes(
    items: Sequence[PlanItem], changes: object,
) -> tuple[list[PlanItem], list[str]]:
    """``(new items, notes)``: ``items`` after the ops in ``changes``, in time order.

    An op is ``{"op": "add", "item": {...}}`` (a new id is made; the fields are a PlanItem's but
    ``item_id`` and ``route``), ``{"op": "edit", "id": ..., <fields>}`` (any field but ``item_id``
    and ``route``; changing ``start`` or ``place`` clears the item's route, the program computes it
    again) or ``{"op": "drop", "id": ...}``. ``changes`` is untrusted: an op that does not read, or
    would leave an item that fails :func:`check_item`, is skipped and noted, and the others still
    apply. A ``fixed`` item may be edited or dropped like any other: he can change anything.
    """
    if not isinstance(changes, list):
        return list(items), ["changes is not a list"]
    held = list(items)
    notes: list[str] = []
    for index, op in enumerate(changes, 1):
        try:
            held = _apply_op(held, op)
        except (TypeError, ValueError) as exc:
            notes.append(f"change {index} skipped: {exc}"[:NOTE_CHARS])
    held.sort(key=_sort_key)
    return held, notes


def _label(item: PlanItem) -> str:
    return f"{item.title} {item.start}" if item.start else item.title


def diff(before: Sequence[PlanItem], after: Sequence[PlanItem]) -> str:
    """What changed between two item lists, in a few words (``+`` added, ``-`` dropped)."""
    was = {item.item_id: item for item in before}
    now = {item.item_id: item for item in after}
    parts = [f"+ {_label(now[i])}" for i in now if i not in was]
    parts += [f"- {_label(was[i])}" for i in was if i not in now]
    for item_id in now.keys() & was.keys():
        old, new = was[item_id], now[item_id]
        if old.title != new.title:
            parts.append(f"{old.title} -> {new.title}")
        for name in ("start", "minutes", "place", "firmness"):
            if getattr(old, name) != getattr(new, name):
                parts.append(f"{new.title} {name}: {getattr(old, name) or '-'} -> "
                             f"{getattr(new, name) or '-'}")
    return "; ".join(parts)[:CHANGE_CHARS]


@dataclass
class _Fold:
    plans: dict[str, Plan]
    failures: dict[str, str]
    revisions: list[Revision]


def _walk(conn: sqlite3.Connection) -> _Fold:
    found = _Fold({}, {}, [])
    for event in iter_events_of_types(conn, TYPES):
        payload = event.payload
        key = str(payload["date"])
        plan = found.plans.get(key)
        if event.type == "plan.draft_failed":
            found.failures[key] = str(payload["error"])
        elif event.type in ("plan.drafted", "plan.revised"):
            items = tuple(PlanItem.from_payload(raw) for raw in payload["items"])
            if event.type == "plan.drafted":
                source, digest = "drafted", str(payload["digest"])
            else:
                source, digest = f"revised:{payload['by']}", plan.digest if plan else ""
                found.revisions.append(Revision(
                    key, str(payload["by"]), str(payload["why"]),
                    diff(plan.items if plan else (), items), event.ts_epoch_ms,
                ))
            if plan is None:
                day = date.fromisoformat(key)
                found.plans[key] = Plan(day, items, source=source, digest=digest)
            else:
                plan.items, plan.source, plan.digest = items, source, digest
        elif plan is None:
            continue  # settled or opened with no plan to hold it: nothing to mark
        elif event.type == "plan.settled":
            plan.status, plan.settled_by = "settled", str(payload["by"])
        else:
            plan.opened = True
            plan.opened_record_id = str(payload["record_id"])
            plan.opened_signal = str(payload["signal"])
            plan.opened_at_ms = int(payload["at_ms"])
    return found


def fold(conn: sqlite3.Connection) -> dict[str, Plan]:
    """Every date's plan by ISO date: the last drafted or revised items, settled and opened."""
    return _walk(conn).plans


def for_day(conn: sqlite3.Connection, day: date) -> Plan | None:
    """``day``'s plan, or None when no draft or revision was written for it."""
    return fold(conn).get(_day(day))


def failed(conn: sqlite3.Connection, day: date) -> str | None:
    """The error of ``day``'s last failed draft, or None. A plan may exist beside it."""
    return _walk(conn).failures.get(_day(day))


def revisions(
    conn: sqlite3.Connection, first: date, last: date, *, by: str | None = None,
) -> list[Revision]:
    """The revisions of dates ``first`` to ``last`` (inclusive), newest first; ``by`` filters."""
    lo, hi = _day(first), _day(last)
    return [
        r for r in reversed(_walk(conn).revisions)  # the log is in append order
        if lo <= r.day <= hi and by in (None, r.by)
    ]
