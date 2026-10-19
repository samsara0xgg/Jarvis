"""ADR 0302: what he was doing, as spans in four lanes, cut and served from the day's facts.

The day line (:mod:`jarvis.state.day_line`) holds facts as items. This module is the pure part of
the doing lane, and every function here is a fact about those items or a rule over them:

- :func:`fact_spans` serves the items that last a while as spans of three fact lanes, ``place``,
  ``use`` and ``body``, with basis ``observed``. A point (a picture, a payment, a reminder) is not
  a span.
- :func:`waking_window` says when the day's waking part began and ended.
- :func:`cut` cuts that window into segments at the edges of the facts: every waking minute is
  in exactly one segment, a minute no fact covers included.
- :func:`serve` turns segments into the ``doing`` lane: the latest stored run's words up to what
  it covers, program-cut segments after it, his answers over both (they win), and the plan after
  now. :func:`coverage` counts how much of the waking window that fills.
- :func:`read_doing` is the one entry the day page and the answer route share: it reads the run
  and the answers from memory.db and applies the rest.

Nothing here calls a model. A run is written by a later step; here it is only read.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import commitments
from jarvis.state.memory_db import doing_answers, latest_doing_run, open_memory_db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from jarvis.state.memory_db import DoingAnswer, DoingRun

    type Span = dict[str, Any]

LOGGER = logging.getLogger(__name__)

LANES: Final = ("doing", "place", "use", "body")
CATEGORIES: Final = (
    "home", "study", "work", "commute", "errand", "meal", "social", "rest", "sleep", "exercise",
    "unknown",
)
BASES: Final = ("observed", "stated", "inferred", "planned")
CONFIDENCES: Final = ("high", "medium", "low")
CLUE_KINDS: Final = (
    "words", "picture", "mail", "calendar", "plan", "payment", "chat", "code", "screen", "answer",
    "photo", "away_note",
)
DOING_KINDS: Final = ("doing", "unknown")
"""The kind of a span of the doing lane: a thing he did, or a stretch nothing speaks to."""
PLANNED_KIND: Final = "planned"

_MINUTE_MS: Final = 60_000
_HOUR_MS: Final = 60 * _MINUTE_MS

CUT_MIN_MS: Final = 10 * _MINUTE_MS
"""A segment shorter than this is folded into a neighbour; a work block, a call or a block of
phone or tablet use cuts the day only when it lasts at least this long."""
WAKING_DEFAULT_START_MS: Final = 6 * _HOUR_MS
"""Where the waking part starts, from the day's start, when no sleep ended in the morning."""
WAKING_WAKE_REACH_MS: Final = 14 * _HOUR_MS
"""A sleep that ended within this of the day's start is the night he woke from."""
WAKING_BED_REACH_MS: Final = 6 * _HOUR_MS
"""A sleep that starts within this after the day's end is the night he went to."""
NIGHT_MIN_MS: Final = 3 * _HOUR_MS
"""A sleep at least this long is a night; a shorter one is a nap inside the waking part."""
ANSWER_TEXT_MAX_CHARS: Final = 200
"""The most characters of an answer he types."""

_MOVE_KINDS: Final = {
    "walking": "walk", "running": "run", "cycling": "cycle", "automotive": "drive",
}
_CUTTING_USE_KINDS: Final = frozenset({"work", "call", "screen"})
_UNATTENDED_KINDS: Final = frozenset({"code"})
"""Use spans that show a program ran, not that he was there: a Claude Code session goes on while
he rests, so it is a clue of the doing lane, never one of its facts."""
_PLANNED_SOURCES: Final = frozenset({"plan", *commitments.SOURCES})
_ROLE_CATEGORY: Final = {"home": "home", "school": "study"}


class Waking(NamedTuple):
    """The waking part of a day: ``[start_ms, end_ms)``, ended by now for today."""

    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Segment:
    """One cut of the waking window: the fact spans it overlaps and its primary place span."""

    start_ms: int
    end_ms: int
    item_ids: tuple[str, ...]
    place_span_id: str | None

    @property
    def id(self) -> str:
        """Stable while the cut stays: it names the segment by where it starts."""
        return f"doing-{self.start_ms}"


class Doing(NamedTuple):
    """What a day serves besides its items: fact spans, doing spans and coverage."""

    facts: list[Span]
    served: list[Span]
    coverage: dict[str, int]

    @property
    def spans(self) -> list[Span]:
        """Fact spans and doing spans together, by start and then id."""
        return sorted([*self.facts, *self.served], key=_order)


def _order(span: Span) -> tuple[int, str]:
    return span["start_ms"], span["id"]


# --- fact spans ----------------------------------------------------------------------------


def _span(  # noqa: PLR0913 — a span's fields, all keywords but the identity and the time.
    item: Mapping[str, Any], lane: str, kind: str, *, title: str | None = None,
    category: str = "unknown", place_id: str | None = None,
) -> Span:
    return {
        "id": str(item["id"]),
        "lane": lane,
        "kind": kind,
        "start_ms": int(item["start_ms"]),
        "end_ms": None if item["end_ms"] is None else int(item["end_ms"]),
        "title": title or None,
        "detail": None,
        "category": category,
        "basis": "observed",
        "confidence": "high",
        "confirmed": False,
        "clues": [],
        "question": None,
        "place_id": place_id,
        "item_ids": [str(item["id"])],
    }


def _top_name(item: Mapping[str, Any]) -> str | None:
    for key in ("projects", "apps"):
        top = item.get(key) or []
        if top:
            return str(top[0]["name"])
    return None


def _stay(item: Mapping[str, Any]) -> Span:
    kept = item.get("kept_place") or {}
    return _span(
        item, "place", "stay", title=kept.get("name") or item.get("place"),
        category=_ROLE_CATEGORY.get(kept.get("role", ""), "unknown"), place_id=kept.get("id"),
    )


def _move(item: Mapping[str, Any]) -> Span:
    kind = _MOVE_KINDS.get(str(item.get("activity")), "drive")
    return _span(item, "place", kind, category="commute")


_BUILDERS: Final[dict[str, Callable[[Mapping[str, Any]], Span]]] = {
    "stay": _stay,
    "move": _move,
    "ride": lambda i: _span(i, "place", "ride", title=i.get("route"), category="commute"),
    "work": lambda i: _span(i, "use", "work", title=_top_name(i)),
    "call": lambda i: _span(i, "use", "call", title=i.get("app")),
    "phone_call": lambda i: _span(i, "use", "call", title=i.get("app")),
    "screen": lambda i: _span(i, "use", "screen", title=_top_name(i)),
    "code": lambda i: _span(i, "use", "code", title=i.get("title")),
    "talk": lambda i: _span(i, "use", "talk"),
    "sleep": lambda i: _span(i, "body", "sleep", category="sleep"),
    "workout": lambda i: _span(
        i, "body", "workout", title=i.get("activity"), category="exercise",
    ),
}
"""How each kind of item that lasts a while becomes a span; a kind not here is a point or a note."""


def fact_spans(items: Iterable[Mapping[str, Any]]) -> list[Span]:
    """The day-line items that last a while, as spans of the place, use and body lanes.

    A stay is a place span; a move or a ride is a place span in the category ``commute``. A work
    block, a call, a block of phone or tablet use, a Claude Code session and a talk are use
    spans; a sleep block and a workout are body spans. Every one is observed, with high
    confidence, unconfirmed, with no clues and no question, and ``item_ids`` is its own item. A
    picture, payment, event, reminder, departure, Claude chat or Claude day, and a label, are
    points or notes of the day, not spans.
    """
    found = [_BUILDERS[item["kind"]](item) for item in items if item["kind"] in _BUILDERS]
    return sorted(found, key=_order)


def his_facts(facts: Iterable[Span]) -> list[Span]:
    """The fact spans that show him: what the doing lane is cut from and worded by."""
    return [s for s in facts if s["kind"] not in _UNATTENDED_KINDS]


# --- the waking window ---------------------------------------------------------------------


def waking_window(facts: Iterable[Span], day_start_ms: int, day_end_ms: int, now_ms: int) -> Waking:
    """When the day's waking part ran, from its nights of sleep, and from ``now_ms`` for today.

    Only a sleep of at least :data:`NIGHT_MIN_MS` is a night (one still going is measured to
    ``now_ms``); a nap is part of the waking day. The window starts where the latest night ending
    within 14 hours of the day's start ends, else six hours into the day. It ends where the first
    night after that, starting before six hours past the day's end, begins, else at the day's end.
    For today it stops at ``now_ms``; a window that has not begun is empty.
    """
    nights = [
        s for s in facts
        if s["kind"] == "sleep"
        and (now_ms if s["end_ms"] is None else s["end_ms"]) - s["start_ms"] >= NIGHT_MIN_MS
    ]
    woke = [
        s["end_ms"] for s in nights
        if s["end_ms"] is not None
        and day_start_ms <= s["end_ms"] <= day_start_ms + WAKING_WAKE_REACH_MS
    ]
    start = max(woke) if woke else day_start_ms + WAKING_DEFAULT_START_MS
    bed = [
        s["start_ms"] for s in nights
        if start < s["start_ms"] < day_end_ms + WAKING_BED_REACH_MS
    ]
    end = min(bed) if bed else day_end_ms
    return Waking(start, max(start, min(end, now_ms)))


# --- the cut -------------------------------------------------------------------------------


def _reach(span: Span, horizon: int) -> int:
    """Where a span ends; one still going reaches the horizon."""
    return horizon if span["end_ms"] is None else int(span["end_ms"])


def _shared(span: Span, start: int, end: int, horizon: int) -> int:
    """Milliseconds a span and ``[start, end)`` have in common."""
    return max(0, min(_reach(span, horizon), end) - max(int(span["start_ms"]), start))


def _touches(span: Span, start: int, end: int, horizon: int) -> bool:
    """Whether the span overlaps ``[start, end)``; a point counts when it falls inside."""
    first, last = int(span["start_ms"]), _reach(span, horizon)
    if last <= first:
        return start <= first < end
    return first < end and last > start


def _cuts(span: Span, horizon: int) -> bool:
    if span["lane"] == "place" or span["kind"] == "workout":
        return True
    return (
        span["kind"] in _CUTTING_USE_KINDS
        and _reach(span, horizon) - span["start_ms"] >= CUT_MIN_MS
    )


def _best(spans: Iterable[Span], start: int, end: int, horizon: int) -> Span | None:
    """The span sharing most of ``[start, end)``, the earlier and then the lower id on a tie."""
    ranked = [(s, _shared(s, start, end, horizon)) for s in spans]
    ranked = [(s, shared) for s, shared in ranked if shared > 0]
    if not ranked:
        return None
    return min(ranked, key=lambda pair: (-pair[1], pair[0]["start_ms"], pair[0]["id"]))[0]


def _segment(facts: Sequence[Span], start: int, end: int, horizon: int) -> Segment:
    held = sorted(
        (s for s in facts if _touches(s, start, end, horizon)), key=_order,
    )
    place = _best((s for s in held if s["lane"] == "place"), start, end, horizon)
    return Segment(start, end, tuple(s["id"] for s in held), None if place is None else place["id"])


def cut(facts: Sequence[Span], waking: Waking) -> list[Segment]:
    """The waking window cut into segments, none shorter than ten minutes unless alone.

    The boundaries are the edges of every place span, of every work block, call and block of
    phone or tablet use that lasts at least ten minutes, and of every workout, inside the
    window. Between two boundaries is an interval; an interval shorter than ten minutes is folded
    into the neighbour that shares its primary place span, else the longer neighbour (the earlier
    on a tie), until none is. The segments cover the window with no gap and no overlap; a
    stretch no fact covers is a segment too. A span still going reaches the end of the window.
    """
    start, end = waking
    if end <= start:
        return []
    edges = {start, end}
    for span in facts:
        if _cuts(span, end):
            edges.update(t for t in (span["start_ms"], _reach(span, end)) if start < t < end)
    ordered = sorted(edges)
    ranges = list(pairwise(ordered))

    def place_of(window: tuple[int, int]) -> str | None:
        return _segment(facts, *window, end).place_span_id

    while len(ranges) > 1:
        short = [i for i, (a, b) in enumerate(ranges) if b - a < CUT_MIN_MS]
        if not short:
            break
        i = min(short, key=lambda n: (ranges[n][1] - ranges[n][0], n))
        beside = [j for j in (i - 1, i + 1) if 0 <= j < len(ranges)]
        mine = place_of(ranges[i])
        sharing = [j for j in beside if mine is not None and place_of(ranges[j]) == mine]
        into = max(sharing or beside, key=lambda n: (ranges[n][1] - ranges[n][0], -n))
        first, last = sorted((i, into))
        ranges[first:last + 1] = [(ranges[first][0], ranges[last][1])]
    return [_segment(facts, a, b, end) for a, b in ranges]


# --- serving -------------------------------------------------------------------------------


def _doing_span(  # noqa: PLR0913 — the served shape of a span of the doing lane.
    span_id: str, start: int, end: int | None, *, kind: str, title: str | None,
    detail: str | None, category: str, basis: str, confidence: str,
    clues: list[dict[str, Any]], question: dict[str, Any] | None, place_id: str | None,
    item_ids: Sequence[str],
) -> Span:
    return {
        "id": span_id,
        "lane": "doing",
        "kind": kind,
        "start_ms": start,
        "end_ms": end,
        "title": title,
        "detail": detail,
        "category": category,
        "basis": basis,
        "confidence": confidence,
        "confirmed": False,
        "clues": clues,
        "question": question,
        "place_id": place_id,
        "item_ids": list(item_ids),
    }


_ACTIVITY_ORDER: Final = (
    ("work",), ("ride",), ("walk", "run", "cycle", "drive"), ("call",), ("screen",),
    ("sleep", "workout"),
)
"""The facts that say what he was doing, strongest first; a stay says only where."""


def _dominant(
    segment: Segment, by_id: Mapping[str, Span], horizon: int,
) -> tuple[bool, str | None, str]:
    """``(known, title, category)`` the program can give a segment: what he did, never the place.

    The first kind of activity in :data:`_ACTIVITY_ORDER` with a fact that covers at least half
    of the segment says what he was doing (``known``), with its title (a project, a route, an
    app, the app he used most on his phone or tablet, an activity; a walk, run, cycle or drive
    has none) and its category (``work`` for a work block). With none, nothing says what he did:
    no title, and the category of its primary place span (a home stay alone is ``home``), else of
    the fact with most of it, as a colour.
    """
    start, end = segment.start_ms, segment.end_ms
    held = [by_id[i] for i in segment.item_ids]
    for kinds in _ACTIVITY_ORDER:
        found = _best((s for s in held if s["kind"] in kinds), start, end, horizon)
        if found is not None and 2 * _shared(found, start, end, horizon) >= end - start:
            work = found["kind"] in _ACTIVITY_ORDER[0]
            return True, found["title"], "work" if work else found["category"]
    primary = by_id.get(segment.place_span_id or "") or _best(
        held, segment.start_ms, segment.end_ms, horizon,
    )
    return False, None, "unknown" if primary is None else primary["category"]


def _program_span(segment: Segment, by_id: Mapping[str, Span], horizon: int) -> Span:
    """A segment with the words the program has: what the facts say he was doing, low.

    It is ``unknown`` when no fact says what he did, though a place may give it a colour.
    """
    place = by_id.get(segment.place_span_id or "")
    known, title, category = _dominant(segment, by_id, horizon)
    return _doing_span(
        segment.id, segment.start_ms, segment.end_ms,
        kind="doing" if known else "unknown",
        title=title, detail=None, category=category,
        basis="observed", confidence="low", clues=[], question=None,
        place_id=None if place is None else place["place_id"],
        item_ids=segment.item_ids,
    )


def _checked(value: Any, allowed: Sequence[str], name: str) -> str:  # noqa: ANN401 — stored JSON
    if value not in allowed:
        msg = f"a run's {name} must be one of {', '.join(allowed)}, not {value!r}"
        raise ValueError(msg)
    return str(value)


def _run_span(
    raw: Mapping[str, Any], covers_until_ms: int, facts: Sequence[Span], horizon: int,
) -> Span | None:
    """One span of a stored run, in the served shape and cut off where the run stops covering.

    Raises:
        ValueError: a closed vocabulary is broken, or the span is not the shape a run holds.
    """
    start, end = int(raw["start_ms"]), min(int(raw["end_ms"]), covers_until_ms)
    if end <= start:
        return None
    question = raw.get("question")
    clues = [
        {"kind": _checked(c["kind"], CLUE_KINDS, "clue kind"), "at_ms": int(c["at_ms"]),
         "text": str(c["text"])}
        for c in raw.get("clues") or []
    ]
    return _doing_span(
        str(raw["id"]), start, end,
        kind=_checked(raw["kind"], DOING_KINDS, "kind"),
        title=raw.get("title"), detail=raw.get("detail"),
        category=_checked(raw["category"], CATEGORIES, "category"),
        basis=_checked(raw["basis"], BASES[:3], "basis"),
        confidence=_checked(raw["confidence"], CONFIDENCES, "confidence"),
        clues=clues,
        question=None if question is None else {
            "id": str(question["id"]), "text": str(question["text"]),
            "options": [str(o) for o in question.get("options") or []],
        },
        place_id=raw.get("place_id"),
        item_ids=[s["id"] for s in facts if _touches(s, start, end, horizon)],
    )


def _piece(span: Span, start: int, end: int, facts: Sequence[Span], horizon: int) -> Span:
    """The part of a span from ``start`` to ``end``, with the facts it still overlaps."""
    return {
        **span, "id": span["id"] if start == span["start_ms"] else f"doing-{start}",
        "start_ms": start, "end_ms": end,
        "item_ids": [s["id"] for s in facts if _touches(s, start, end, horizon)],
    }


def _answered(
    spans: list[Span], answer: DoingAnswer, facts: Sequence[Span], horizon: int,
) -> list[Span]:
    """The spans with ``answer`` carved in: his words over his time, the rest cut around them.

    The stretch the answer covers inside the spans becomes one span with his words, ``stated``,
    ``high`` and ``confirmed``, no question, and the category and place of the span that held
    most of it. What is left of a span on either side stays; a piece shorter than ten minutes
    goes to the answer instead.
    """
    inside = [s for s in spans if s["start_ms"] < answer.end_ms and s["end_ms"] > answer.start_ms]
    if not inside:
        return spans
    first, last = inside[0], inside[-1]
    start = max(answer.start_ms, first["start_ms"])
    end = min(answer.end_ms, last["end_ms"])
    if start - first["start_ms"] < CUT_MIN_MS:
        start = first["start_ms"]
    if last["end_ms"] - end < CUT_MIN_MS:
        end = last["end_ms"]
    base = max(inside, key=lambda s: min(s["end_ms"], end) - max(s["start_ms"], start))
    said = {
        **_piece(first if first["start_ms"] == start else base, start, end, facts, horizon),
        "kind": "doing", "title": answer.text or answer.choice, "category": base["category"],
        "place_id": base["place_id"], "basis": "stated", "confidence": "high",
        "confirmed": True, "question": None,
    }
    kept = [s for s in spans if s not in inside]
    if first["start_ms"] < start:
        kept.append(_piece(first, first["start_ms"], start, facts, horizon))
    if last["end_ms"] > end:
        kept.append(_piece(last, end, last["end_ms"], facts, horizon))
    return sorted([*kept, said], key=_order)


def _planned(items: Iterable[Mapping[str, Any]], now_ms: int) -> list[Span]:
    """Each plan item and commitment that starts after ``now_ms``, as a planned span."""
    return [
        _doing_span(
            str(item["id"]), int(item["start_ms"]),
            None if item["end_ms"] is None else int(item["end_ms"]),
            kind=PLANNED_KIND, title=item.get("title") or None, detail=None, category="unknown",
            basis="planned", confidence="high", clues=[], question=None, place_id=None,
            item_ids=[],
        )
        for item in items
        if item["kind"] == "event" and item.get("source") in _PLANNED_SOURCES
        and int(item["start_ms"]) > now_ms
    ]


def serve(  # noqa: PLR0913 — what the lane is made of: cut, facts, run, answers, plan, clock.
    segments: Sequence[Segment], facts: Sequence[Span], run: DoingRun | None,
    answers: Sequence[DoingAnswer], items: Iterable[Mapping[str, Any]], now_ms: int,
) -> list[Span]:
    """The spans of the doing lane, by start and then id.

    The latest ``run`` speaks up to the time it covers and the program's own segments, with
    the facts they overlap and no words, after it. ``answers`` (oldest first) win over both:
    each is carved into the lane over its own time (:func:`_answered`). The plan and commitments
    among ``items`` that start after ``now_ms`` are planned spans. Raises ValueError when a run
    holds what the lane cannot serve.
    """
    horizon = segments[-1].end_ms if segments else now_ms
    by_id = {s["id"]: s for s in facts}
    spans: list[Span] = []
    covers = None if run is None else run.covers_until_ms
    if run is not None:
        spans.extend(
            span for raw in run.spans
            if (span := _run_span(raw, run.covers_until_ms, facts, horizon)) is not None
        )
    for segment in segments:
        if covers is not None and segment.end_ms <= covers:
            continue
        if covers is not None and segment.start_ms < covers:
            segment = _segment(facts, covers, segment.end_ms, horizon)  # noqa: PLW2901
        spans.append(_program_span(segment, by_id, horizon))
    for answer in answers:  # a later answer is carved over an earlier one
        spans = _answered(sorted(spans, key=_order), answer, facts, horizon)
    spans.extend(_planned(items, now_ms))
    return sorted(spans, key=_order)


def coverage(served: Iterable[Span], waking: Waking) -> dict[str, int]:
    """``waking_start_ms``, ``waking_end_ms``, ``waking_ms`` and ``filled_ms`` of a day.

    ``filled_ms`` is the time of the doing spans that say what he was doing (``kind`` ``doing``,
    which his answer makes any span), planned ones excluded, inside the waking window.
    """
    start, end = waking
    spoken = sorted(
        (max(s["start_ms"], start), min(s["end_ms"], end))
        for s in served
        if s["kind"] == "doing" and s["basis"] != "planned"
    )
    filled, reached = 0, start
    for a, b in spoken:
        a = max(a, reached)  # noqa: PLW2901
        if b > a:
            filled += b - a
            reached = b
    return {
        "waking_start_ms": start, "waking_end_ms": end, "waking_ms": end - start,
        "filled_ms": filled,
    }


def read_doing(  # noqa: PLR0913 — the day, its items and window, and the clock.
    memory_db: Path | None, day: str, items: Sequence[Mapping[str, Any]], start_ms: int,
    end_ms: int, now_ms: int,
) -> Doing:
    """The fact spans, served doing spans and coverage of ``day``, its ``items`` in hand.

    The latest run and the answers of the day are read from ``memory_db``. A store that cannot be
    read, or a run that cannot be served, leaves the doing lane program-cut alone, never a failed
    day.
    """
    facts = fact_spans(items)
    his = his_facts(facts)
    waking = waking_window(his, start_ms, end_ms, now_ms)
    segments = cut(his, waking)
    run, answers = None, []
    if memory_db is not None and memory_db.is_file():
        try:
            with closing(open_memory_db(memory_db)) as conn:
                run, answers = latest_doing_run(conn, day), doing_answers(conn, day)
        except Exception:
            LOGGER.exception("doing: the run and answers of %s could not be read", day)
    try:
        served = serve(segments, his, run, answers, items, now_ms)
    except Exception:
        LOGGER.exception("doing: the run of %s could not be served; program cut only", day)
        served = serve(segments, his, None, answers, items, now_ms)
    return Doing(facts, served, coverage(served, waking))


def primary_place(doing: Doing, span: Span) -> Span | None:
    """The place fact span that covers most of a served span, or None.

    A place still going reaches the end of the waking window.
    """
    places = [s for s in doing.facts if s["lane"] == "place"]
    return _best(places, span["start_ms"], span["end_ms"], doing.coverage["waking_end_ms"])


__all__ = [
    "ANSWER_TEXT_MAX_CHARS",
    "BASES",
    "CATEGORIES",
    "CLUE_KINDS",
    "CONFIDENCES",
    "CUT_MIN_MS",
    "DOING_KINDS",
    "LANES",
    "PLANNED_KIND",
    "Doing",
    "Segment",
    "Waking",
    "coverage",
    "cut",
    "fact_spans",
    "primary_place",
    "read_doing",
    "serve",
    "waking_window",
]
