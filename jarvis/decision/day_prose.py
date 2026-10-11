"""L3 day prose: the input and the gates for the model-made text of a day (ADR 0201, 0235).

A day's computed numbers, its conversation summary, its work report and its day-line items go in;
"The day" (two to four plain sentences), one line to him and up to five highlights come out, or
the reason to store nothing. A draft of a day under way asks for the line and highlights alone.
The runtime owns when it runs and the client; L2 owns the numbers and the write.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state.day_highlights import LABEL_MAX_CHARS, LINE_MAX_CHARS, MAX_HIGHLIGHTS

if TYPE_CHECKING:
    from collections.abc import Container, Mapping, Sequence
    from datetime import tzinfo

_REPORT_CHARS: Final[int] = 2600
_NOTE_CHARS: Final[int] = 300
_MARKDOWN: Final[re.Pattern[str]] = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+\.\s)", re.MULTILINE)
# A sentence end with more after it (the second set: the full-width stop, exclamation, question).
_SECOND_SENTENCE: Final[re.Pattern[str]] = re.compile(r"[.!?]\s+\S|[\u3002\uff01\uff1f]\s*\S")
_FENCE: Final[str] = "```"
# What an item carries for the screen and not for the writer: ids and times are printed on their
# own, coordinates are not for a model, a picture's note is the fact that matters.
_PLUMBING: Final[frozenset[str]] = frozenset(
    {"id", "lane", "kind", "start_ms", "end_ms", "state", "lat", "lng", "accuracy_m", "attachment"},
)


class DayAnswer(NamedTuple):
    """A parsed answer: the prose (nightly only), the line with its picks, or why they were dropped.

    A pick is ``{"id", "label", "start_ms"}``. ``dropped`` is the reason the line and highlights
    failed their gates while the prose stood, else None.
    """

    prose: str | None
    line: str | None
    highlights: tuple[dict[str, Any], ...]
    dropped: str | None


def _clock(ms: int, zone: tzinfo) -> str:
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%H:%M")


def _fact(key: str, value: object) -> str | None:
    """One ``key=value`` of an item, or None for a field with nothing to say."""
    if value is None or value in ("", []):
        return None
    if isinstance(value, list):  # the work block's projects and apps
        names = [str(one["name"]) for one in value if isinstance(one, dict) and "name" in one]
        return f"{key}={', '.join(names)}" if names else None
    if isinstance(value, bool):
        return f"{key}={'yes' if value else 'no'}"
    text = " ".join(str(value).split())
    return f'{key}="{text[:_NOTE_CHARS]}"' if key == "note" else f"{key}={text}"


def day_items_text(items: Sequence[Mapping[str, Any]], zone: tzinfo) -> str:
    """One line per day-line item: ``id | kind | local time | facts``, in the order given."""
    lines = []
    for item in items:
        start, end = int(item["start_ms"]), item["end_ms"]
        when = _clock(start, zone)
        if end is None:
            when += "-" if item["state"] == "now" else ""  # still going, or a moment (a reminder)
        elif int(end) != start:
            when += f"-{_clock(int(end), zone)}"
        if item["state"] != "done":
            when += f" ({item['state']})"
        facts = (_fact(key, value) for key, value in item.items() if key not in _PLUMBING)
        lines.append(
            " | ".join(
                (str(item["id"]), str(item["kind"]), when, "; ".join(one for one in facts if one)),
            ),
        )
    return "\n".join(lines) or "(no items)"


def build_day_prose_messages(  # noqa: PLR0913 — the day's four inputs, its items and the language.
    day: str, numbers: str, summary: str | None, report: str | None,
    items: str, language: str,
) -> list[dict[str, Any]]:
    """The one user message the writer sees: ``day``'s numbers, summary, report excerpt, items."""
    excerpt = (report or "").strip()[:_REPORT_CHARS]
    talked = (summary or "").strip() or "(none yet)"
    content = "\n\n".join(
        (
            f"DAY: {day}",
            f"NUMBERS (computed by the program):\n{numbers.strip()}",
            f"DAY SUMMARY (conversation with the assistant):\n{talked}",
            f"DAILY WORK REPORT (excerpt):\n{excerpt or '(none yet)'}",
            f"DAY LINE ITEMS (id | kind | local time | facts):\n{items}",
            f"LINE LANGUAGE: {language}",
        ),
    )
    return [{"role": "user", "content": content}]


def build_day_draft_messages(day: str, now: str, items: str, language: str) -> list[dict[str, Any]]:
    """The one user message of a draft: ``day`` is under way at local time ``now``; its items."""
    content = "\n\n".join(
        (
            f"DAY: {day} (still under way; it is {now} now)",
            f"DAY LINE ITEMS (id | kind | local time | facts):\n{items}",
            f"LINE LANGUAGE: {language}",
        ),
    )
    return [{"role": "user", "content": content}]


def check_day_prose(text: str | None, finish_reason: str | None, max_chars: int) -> str | None:
    """Return why the line must not be stored, or None when it may."""
    body = (text or "").strip()
    gates: tuple[tuple[bool, str], ...] = (
        (not body, "empty"),
        (finish_reason in ("length", "max_tokens"), "cut off by the output limit"),
        (len(body) > max_chars, f"{len(body)} chars over the {max_chars} cap"),
        (bool(_MARKDOWN.search(body)), "contains markdown headings or bullets"),
    )
    return next((reason for failed, reason in gates if failed), None)


def _pick(
    one: object, starts: Mapping[str, int], taken: Container[str],
) -> dict[str, Any] | str:
    """One highlight as stored, or why it fails: it must name an item of that day, once."""
    item_id = one.get("id") if isinstance(one, dict) else None
    label = one.get("label") if isinstance(one, dict) else None
    if not (isinstance(item_id, str) and isinstance(label, str)):
        return "a highlight lacks an id or a label"
    label = label.strip()
    gates: tuple[tuple[bool, str], ...] = (
        (item_id not in starts, f"highlight {item_id!r} is not an item of that day"),
        (item_id in taken, f"highlight {item_id!r} is named twice"),
        (not label or len(label) > LABEL_MAX_CHARS, f"a label is empty or over {LABEL_MAX_CHARS}"),
    )
    if (why := next((why for failed, why in gates if failed), None)) is not None:
        return why
    return {"id": item_id, "label": label, "start_ms": starts[item_id]}


def _line_part(
    data: Mapping[str, Any], starts: Mapping[str, int],
) -> tuple[str, tuple[dict[str, Any], ...]] | str:
    """The line and its picks if they pass every gate together, else why not."""
    line = data.get("line")
    if not isinstance(line, str) or not line.strip():
        return "no line"
    line = line.strip()
    raw = data.get("highlights", [])
    if not isinstance(raw, list):
        return "highlights are not a list"
    gates: tuple[tuple[bool, str], ...] = (
        ("\n" in line or bool(_SECOND_SENTENCE.search(line)), "the line is not one sentence"),
        (bool(_MARKDOWN.search(line)), "the line holds markdown"),
        (len(line) > LINE_MAX_CHARS, f"the line is over the {LINE_MAX_CHARS} cap"),
        (len(raw) > MAX_HIGHLIGHTS, f"more than {MAX_HIGHLIGHTS} highlights"),
    )
    if (why := next((why for failed, why in gates if failed), None)) is not None:
        return why
    picks: dict[str, dict[str, Any]] = {}
    for one in raw:
        pick = _pick(one, starts, picks)
        if isinstance(pick, str):
            return pick
        picks[pick["id"]] = pick
    return line, tuple(picks.values())


def _read_object(text: str | None, finish_reason: str | None) -> dict[str, Any] | str:
    """The answer's one JSON object (a fenced one is still that), or why it is not."""
    if finish_reason in ("length", "max_tokens"):
        return "cut off by the output limit"
    body = (text or "").strip()
    if body.startswith(_FENCE):
        body = body.removeprefix(_FENCE).removeprefix("json").removesuffix(_FENCE).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "not valid JSON"
    return data if isinstance(data, dict) else "not a JSON object"


def parse_day_answer(
    text: str | None, finish_reason: str | None, max_chars: int, starts: Mapping[str, int],
    *, with_prose: bool,
) -> DayAnswer | str:
    """The answer as JSON, or why none of it may be stored.

    ``starts`` maps each id of that day's items to its start, so a highlight can only name an item
    that exists. The prose keeps :func:`check_day_prose`'s gates; one that fails rejects the whole
    answer. A line or highlight that fails its gates drops the line and the highlights whole: the
    prose still stands for the night (``with_prose``), a draft has nothing else and is rejected.
    """
    data = _read_object(text, finish_reason)
    if isinstance(data, str):
        return data
    prose: str | None = None
    if with_prose:
        raw = data.get("prose")
        if not isinstance(raw, str):
            return 'no "prose" text'
        prose = raw.strip()
        if (reason := check_day_prose(prose, finish_reason, max_chars)) is not None:
            return reason
    part = _line_part(data, starts)
    if isinstance(part, str):
        return DayAnswer(prose, None, (), part) if with_prose else part
    return DayAnswer(prose, part[0], part[1], None)


__all__ = [
    "DayAnswer",
    "build_day_draft_messages",
    "build_day_prose_messages",
    "check_day_prose",
    "day_items_text",
    "parse_day_answer",
]
