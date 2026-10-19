"""L3 doing run — the input of the doing lane's writer and the gates on its answer (ADR 0302).

One day's facts go in: the waking window, the program's cut of it with the facts that cover each
segment, and the clues; the day's spans come out in the shape a stored run holds, or the reason to
store nothing. The model names times and clues by short labels (``t3``, ``c12``) that the program
gave it, never by milliseconds or text of its own, so a time that is not a boundary of the facts
and a clue that does not exist cannot get through. The runtime owns when it runs and the client;
L2 owns the facts, the clues and the write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.day_prose import read_object
from jarvis.state import places
from jarvis.state.doing import (
    BASES,
    CATEGORIES,
    CONFIDENCES,
    DOING_KINDS,
    Doing,
    coverage,
    primary_place,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import tzinfo

    from jarvis.state.doing import Segment, Span, Waking
    from jarvis.state.doing_clues import Clue

TITLE_MAX_CHARS: Final = 40
"""The longest title of a span: a short activity phrase."""
DETAIL_MAX_CHARS: Final = 200
QUESTION_MAX_CHARS: Final = 120
OPTION_MAX_CHARS: Final = 24
MAX_OPTIONS: Final = 4
MAX_QUESTIONS: Final = 5
"""The most questions a day holds (ADR 0302): what waits for the nightly review."""
MAX_SPAN_CLUES: Final = 8

_WORDS_CLUES: Final = ("words", "answer", "away_note")
"""The clues that make a span ``stated``: he said it, in a conversation, an answer or a note."""
_LEADING: Final = ("at ", "in ", "@", "在")
_PUNCTUATION: Final = " .,:;!?\u3002\uff0c\u3001"
"""Spaces and the stops of either language, which a title may be wrapped in."""


@dataclass(frozen=True)
class Brief:
    """One day as the writer is shown it.

    ``final`` says the day is over; ``language`` is the one the titles are written in; ``items``
    are the day line's, which give a fact its details; ``signals`` are the observed signals of a
    segment that has any (its Mac, phone and watch), by the segment's start.
    """

    day: str
    weekday: str
    zone: tzinfo
    now_ms: int
    final: bool
    language: str
    waking: Waking
    segments: Sequence[Segment]
    facts: Sequence[Span]
    items: Sequence[Mapping[str, Any]]
    clues: Sequence[Clue]
    signals: Mapping[int, str]


def _reach(span: Mapping[str, Any], brief: Brief) -> int:
    return brief.waking.end_ms if span["end_ms"] is None else int(span["end_ms"])


def times(brief: Brief) -> list[int]:
    """The moments a span may start or end at, oldest first.

    The window's two edges, the edges of every segment and the edges of every fact inside the
    window.
    """
    start, end = brief.waking
    found = {start, end}
    for segment in brief.segments:
        found.update((segment.start_ms, segment.end_ms))
    for span in brief.facts:
        found.update(t for t in (span["start_ms"], _reach(span, brief)) if start < t < end)
    return sorted(found)


def inputs_digest(brief: Brief) -> str:
    """A short hash of what the writer is shown: facts, clues, signals and where the window starts.

    The window's end is left out: for a day under way it is the clock, which moves on its own,
    and for a day that is over it follows from the facts. The signals of the segment still going
    do grow with the clock, so a day under way is told apart as often as it may be written.
    """
    body = {
        "start_ms": brief.waking.start_ms,
        "facts": [
            [s["id"], s["kind"], s["start_ms"], s["end_ms"], s["title"], s["category"],
             s["place_id"]]
            for s in brief.facts
        ],
        "clues": [list(c) for c in brief.clues],
        "signals": sorted(brief.signals.items()),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- the input -----------------------------------------------------------------------------


def _clock(ms: int, zone: tzinfo, *, seconds: bool = False) -> str:
    moment = datetime.fromtimestamp(ms / 1000, zone)
    return f"{moment:%H:%M:%S}" if seconds else f"{moment:%H:%M}"


def _names(rows: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return [str(row["name"]) for row in rows or []]


def _fact_text(span: Span, item: Mapping[str, Any] | None, brief: Brief) -> str:
    """One fact as the writer reads it: its kind, what it was about and when."""
    names = [] if item is None else [*_names(item.get("projects")), *_names(item.get("apps"))]
    about = ", ".join(names) or span["title"] or ""
    if item is not None and item.get("device"):
        about = f"{item['device']}: {about}"
    end = "now" if span["end_ms"] is None else _clock(int(span["end_ms"]), brief.zone)
    when = f"{_clock(int(span['start_ms']), brief.zone)}-{end}"
    return f"{span['kind']} {about} {when}".replace("  ", " ")


def _segment_line(segment: Segment, labels: Mapping[int, str], brief: Brief) -> str:
    by_id = {s["id"]: s for s in brief.facts}
    items = {str(i["id"]): i for i in brief.items}
    place = by_id.get(segment.place_span_id or "")
    where = "place: none known" if place is None else (
        f"place: {place['title'] or 'unnamed'} [{place['category']}]"
    )
    held = "; ".join(_fact_text(by_id[i], items.get(i), brief) for i in segment.item_ids)
    inner = [label for ms, label in labels.items() if segment.start_ms < ms < segment.end_ms]
    parts = [
        f"{labels[segment.start_ms]}-{labels[segment.end_ms]}",
        f"{_clock(segment.start_ms, brief.zone)}-{_clock(segment.end_ms, brief.zone)}",
        where,
        f"facts: {held or 'none'}",
    ]
    if segment.start_ms in brief.signals:
        parts.append(f"signals: {brief.signals[segment.start_ms]}")
    if inner:
        parts.append(f"inner times: {' '.join(inner)}")
    return " | ".join(parts)


def _clue_line(index: int, clue: Clue, brief: Brief) -> str:
    start, end = brief.waking
    moment = datetime.fromtimestamp(clue.at_ms / 1000, brief.zone)
    when = f"{moment:%H:%M}" if start <= clue.at_ms < end else f"{moment:%m-%d %H:%M}"
    return f"c{index} | {when} | {clue.kind} | {clue.text}"


def build_messages(brief: Brief) -> list[dict[str, Any]]:
    """The one user message the writer sees: the day, its times, its segments and its clues."""
    labels = {ms: f"t{n}" for n, ms in enumerate(times(brief))}
    start, end = brief.waking
    state = (
        "the day is over; this is its final account" if brief.final
        else f"the day is under way; it is {_clock(brief.now_ms, brief.zone)} now"
    )
    time_lines = [f"{label} {_clock(ms, brief.zone, seconds=True)}" for ms, label in labels.items()]
    segment_lines = [_segment_line(s, labels, brief) for s in brief.segments]
    clue_lines = [_clue_line(n, c, brief) for n, c in enumerate(brief.clues, 1)]
    zone = getattr(brief.zone, "key", brief.zone)
    content = "\n\n".join((
        f"DAY: {brief.day} {brief.weekday}, timezone {zone}; {state}",
        f"TITLE LANGUAGE: {brief.language}",
        f"WAKING WINDOW: {labels[start]} {_clock(start, brief.zone, seconds=True)} to "
        f"{labels[end]} {_clock(end, brief.zone, seconds=True)}",
        "TIMES (local; a span starts and ends only at one of these):\n" + "\n".join(time_lines),
        "SEGMENTS (the program's cut of the window; times | local | place | facts | signals):\n"
        + "\n".join(segment_lines),
        "CLUES (id | local time | kind | text):\n" + ("\n".join(clue_lines) or "(none)"),
    ))
    return [{"role": "user", "content": content}]


# --- the gates -----------------------------------------------------------------------------


class _Rejected(Exception):  # noqa: N818 — a gate's verdict, caught where the answer is read
    """The answer may not be stored; the message says why."""


def _bare_words(brief: Brief) -> frozenset[str]:
    """The words that only say where, or what kind: the day's place names, roles and categories."""
    named = (s["title"] for s in brief.facts if s["lane"] == "place" and s["title"])
    return frozenset(w.casefold() for w in (*named, *places.ROLES, *CATEGORIES))


def _bare(title: str, bare: frozenset[str]) -> bool:
    """Whether the title is only a place or a kind, with or without a leading "at" or "在"."""
    plain = title.casefold().strip(_PUNCTUATION)
    for lead in _LEADING:
        plain = plain.removeprefix(lead).strip(_PUNCTUATION)
    return plain in bare


def _text(value: object, cap: int, what: str) -> str | None:
    """``value`` on one line (None when absent or blank).

    Raises:
        _Rejected: it is not text, or is over ``cap`` characters.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{what} is not text"
        raise _Rejected(msg)
    text = " ".join(value.split())
    if len(text) > cap:
        msg = f"{what} is over {cap} characters"
        raise _Rejected(msg)
    return text or None


def _question(raw: object, span_id: str) -> dict[str, Any] | None:
    """The question of a span as stored; ``_Rejected`` when it has no text or too many options."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = "a question is not an object"
        raise _Rejected(msg)
    text = _text(raw.get("text"), QUESTION_MAX_CHARS, "a question")
    options = raw.get("options", [])
    if text is None or not isinstance(options, list) or len(options) > MAX_OPTIONS:
        msg = f"a question has no text, or its options are not a list of at most {MAX_OPTIONS}"
        raise _Rejected(msg)
    kept: list[str] = []
    for option in options:
        one = _text(option, OPTION_MAX_CHARS, "an option")
        if one is None or one in kept:
            msg = "an option is empty or repeated"
            raise _Rejected(msg)
        kept.append(one)
    return {"id": f"q:{span_id}", "text": text, "options": kept}


def _cited(raw: object, known: Mapping[str, Clue]) -> list[dict[str, Any]]:
    """The clues a span cites, in the shape a run stores; ``_Rejected`` for one not given."""
    if not isinstance(raw, list) or len(raw) > MAX_SPAN_CLUES:
        msg = f"a span's clues are not a list of at most {MAX_SPAN_CLUES}"
        raise _Rejected(msg)
    chosen: dict[str, Clue] = {}
    for label in raw:
        if not isinstance(label, str) or label not in known:
            msg = f"clue {label!r} is not one of the clues given"
            raise _Rejected(msg)
        chosen[label] = known[label]
    return [{"kind": c.kind, "at_ms": c.at_ms, "text": c.text} for c in chosen.values()]


def _check_vocabulary(raw: Mapping[str, Any]) -> None:
    for key, allowed in (
        ("kind", DOING_KINDS), ("category", CATEGORIES), ("basis", BASES[:3]),
        ("confidence", CONFIDENCES),
    ):
        if raw.get(key) not in allowed:
            msg = f"a span's {key} must be one of {', '.join(allowed)}, not {raw.get(key)!r}"
            raise _Rejected(msg)


def _check_words(
    raw: Mapping[str, Any], title: str | None, cited: Sequence[Mapping[str, Any]],
    bare: frozenset[str],
) -> None:
    """The gates on what a span says: a title for what he did, none for the unknown, not a place."""
    unknown = raw["kind"] == "unknown"
    gates: tuple[tuple[bool, str], ...] = (
        (unknown and title is not None, "an unknown span has a title"),
        (not unknown and title is None, "a span of something he did has no title"),
        (title is not None and _bare(title, bare), f"title {title!r} says where, not what"),
        (
            raw["basis"] == "stated" and not any(c["kind"] in _WORDS_CLUES for c in cited),
            "a stated span cites none of his words or answers",
        ),
    )
    if (why := next((why for failed, why in gates if failed), None)) is not None:
        raise _Rejected(why)


def _span(  # noqa: PLR0913 — the span, the day, the labels given and what the facts say of it.
    raw: object, brief: Brief, labels: Mapping[str, int], clues: Mapping[str, Clue],
    bare: frozenset[str], held: Doing,
) -> dict[str, Any]:
    """One span of the answer in the shape a run stores; ``_Rejected`` when it fails a gate."""
    if not isinstance(raw, dict):
        msg = "a span is not an object"
        raise _Rejected(msg)
    start, end = labels.get(str(raw.get("from"))), labels.get(str(raw.get("to")))
    if start is None or end is None or end <= start:
        msg = f"a span from {raw.get('from')!r} to {raw.get('to')!r} is not between two times given"
        raise _Rejected(msg)
    _check_vocabulary(raw)
    title = _text(raw.get("title"), TITLE_MAX_CHARS, "a title")
    cited = _cited(raw.get("clues", []), clues)
    _check_words(raw, title, cited, bare)
    if raw["basis"] == "observed" and not any(
        s["start_ms"] < end and _reach(s, brief) > start for s in brief.facts
    ):
        msg = "an observed span has no fact under it"
        raise _Rejected(msg)
    span_id = f"doing-{start}"
    place = primary_place(held, {"start_ms": start, "end_ms": end})
    return {
        "id": span_id, "start_ms": start, "end_ms": end, "kind": raw["kind"], "title": title,
        "detail": _text(raw.get("detail"), DETAIL_MAX_CHARS, "a detail"),
        "category": raw["category"], "basis": raw["basis"], "confidence": raw["confidence"],
        "clues": cited, "question": _question(raw.get("question"), span_id),
        "place_id": None if place is None else place["place_id"],
    }


def _tiled(spans: Sequence[Mapping[str, Any]], brief: Brief) -> str | None:
    """Why the spans do not cover the waking window exactly once, else None."""
    at = brief.waking.start_ms
    for span in sorted(spans, key=lambda s: s["start_ms"]):
        if span["start_ms"] < at:
            return f"spans overlap at {_clock(span['start_ms'], brief.zone, seconds=True)}"
        if span["start_ms"] > at:
            return f"no span covers {_clock(at, brief.zone, seconds=True)}"
        at = span["end_ms"]
    if at != brief.waking.end_ms:
        return f"no span covers {_clock(at, brief.zone, seconds=True)}"
    return None


def read_run(
    text: str | None, finish_reason: str | None, brief: Brief,
) -> list[dict[str, Any]] | str:
    """The answer's spans in the shape a stored run holds, or why none of it may be stored.

    The answer is one JSON object, ``{"spans": [...]}``. Every span is checked by itself:
    the times it names are ones given, and it runs from an earlier to a later one; the closed
    vocabularies hold; the title is at most :data:`TITLE_MAX_CHARS` characters, is there for
    something he did, is absent for a stretch unknown, and does not merely say where; the clues it
    cites exist; ``stated`` rests on his words or an answer and ``observed`` on a fact under it;
    a question has text and at most :data:`MAX_OPTIONS` options. Then together: they tile the
    waking window with no gap and no overlap, and at most :data:`MAX_QUESTIONS` have a question.
    A span's id is ``doing-<start>``, as the program's own segments are named, and its place is
    the one the facts under it say. One bad span rejects the whole answer.
    """
    data = read_object(text, finish_reason)
    if isinstance(data, str):
        return data
    raw = data.get("spans")
    if not isinstance(raw, list) or not raw:
        return 'no "spans" list'
    labels = {f"t{n}": ms for n, ms in enumerate(times(brief))}
    clues = {f"c{n}": c for n, c in enumerate(brief.clues, 1)}
    bare = _bare_words(brief)
    held = Doing(list(brief.facts), [], coverage([], brief.waking))
    try:
        spans = [_span(one, brief, labels, clues, bare, held) for one in raw]
    except _Rejected as rejected:
        return str(rejected)
    if (why := _tiled(spans, brief)) is not None:
        return why
    if sum(s["question"] is not None for s in spans) > MAX_QUESTIONS:
        return f"more than {MAX_QUESTIONS} questions"
    return sorted(spans, key=lambda s: s["start_ms"])


__all__ = [
    "DETAIL_MAX_CHARS",
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_SPAN_CLUES",
    "OPTION_MAX_CHARS",
    "QUESTION_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "Brief",
    "build_messages",
    "inputs_digest",
    "read_run",
    "times",
]
