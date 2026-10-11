"""ADR 0235: a day's highlights as the day line serves them, her picks under his marks.

Her picks are written with the day's line (``day_prose``, by the night and the drafts); his marks
are rows of their own (``day_highlight_marks``) and always win. :func:`merge_highlights` is the one
place the two meet: the day line (``GET /inherent/day``) and recall read through it. The rest
resolves what he names, a day and a clock time, to the item that covers it.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
import re
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state.day_line import parse_day
from jarvis.state.memory_db import latest_day_lines, latest_highlight_marks, open_memory_db

if TYPE_CHECKING:
    from collections.abc import Container, Iterable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.state.memory_db import HighlightMark

LOGGER = logging.getLogger(__name__)

MAX_HIGHLIGHTS: Final = 5
"""Highlights a day shows: his marks first, her picks in the slots he leaves."""
LINE_MAX_CHARS: Final = 140
LABEL_MAX_CHARS: Final = 24

_MINUTE_MS: Final = 60_000
_CLOCK: Final = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")


class Highlight(NamedTuple):
    """One highlight of a day: an item's id, its label, when it starts, and who chose it."""

    id: str
    label: str
    start_ms: int
    by: str  # "her" or "him"


def clock_text(start_ms: int, zone: tzinfo | None) -> str:
    """``HH:MM`` of an epoch time in ``zone`` (the machine's own when None)."""
    return datetime.fromtimestamp(start_ms / 1000, zone).strftime("%H:%M")


def merge_highlights(
    picks: Sequence[Mapping[str, Any]],
    marks: Iterable[HighlightMark],
    zone: tzinfo | None,
    present: Container[str] | None = None,
) -> list[Highlight]:
    """Her picks minus what he unmarked, plus what he marked, by start time.

    ``marks`` are his latest word per item (``HighlightMark`` rows). A marked item wears his label,
    else her label for it, else its kind and time; his marks are never cut by
    :data:`MAX_HIGHLIGHTS`, her picks fill the slots left, in the order she gave them. With
    ``present`` (the ids on the day line now) an id that is not there is skipped, whoever chose it.
    """
    marks = [m for m in marks if present is None or m.item_id in present]
    said = {m.item_id for m in marks}
    hers = {str(p["id"]): p for p in picks}
    his = [
        Highlight(
            m.item_id,
            m.label or (str(hers[m.item_id]["label"]) if m.item_id in hers else "")
            or f"{m.kind} {clock_text(m.start_ms, zone)}",
            m.start_ms, "him",
        )
        for m in marks if m.on
    ]
    room = max(0, MAX_HIGHLIGHTS - len(his))
    kept = [
        Highlight(str(p["id"]), str(p["label"]), int(p["start_ms"]), "her")
        for p in picks
        if str(p["id"]) not in said and (present is None or str(p["id"]) in present)
    ][:room]
    return sorted([*his, *kept], key=lambda h: (h.start_ms, h.id))


def day_notes(
    memory_db: Path | None, day: str, items: Sequence[Mapping[str, Any]], zone: tzinfo | None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """``(line, highlights)`` as the day line serves them for ``day``, its ``items`` in hand.

    ``line`` is ``{"text", "draft", "written_at"}`` or None; highlights are ``{"id", "label",
    "by"}`` for the ids still on the line. A store that cannot be read is no line and no
    highlights, not a failed day.
    """
    if memory_db is None or not memory_db.is_file():
        return None, []
    try:
        with closing(open_memory_db(memory_db)) as conn:
            row = latest_day_lines(conn, day, day).get(day)
            marks = latest_highlight_marks(conn, day, day).get(day, [])
    except Exception:
        LOGGER.exception("day line: the line and highlights of %s could not be read", day)
        return None, []
    merged = merge_highlights(
        [] if row is None else row.highlights, marks, zone, {str(i["id"]) for i in items},
    )
    line = None if row is None else {
        "text": row.line, "draft": row.draft, "written_at": row.written_at,
    }
    return line, [{"id": h.id, "label": h.label, "by": h.by} for h in merged]


def resolve_day(text: str, today: date) -> date:
    """The day ``YYYY-MM-DD``, ``today`` or ``yesterday`` names.

    Raises:
        ValueError: any other form.
    """
    word = text.strip().lower()
    if word == "today":
        return today
    if word == "yesterday":
        return today - timedelta(days=1)
    try:
        return parse_day(word)
    except ValueError:
        msg = 'day must be YYYY-MM-DD, "today" or "yesterday"'
        raise ValueError(msg) from None


def parse_clock(text: str) -> time:
    """The local time ``HH:MM`` names.

    Raises:
        ValueError: any other form.
    """
    found = _CLOCK.fullmatch(text.strip())
    if found is None:
        msg = "time must be HH:MM"
        raise ValueError(msg)
    return time(int(found[1]), int(found[2]))


def covering(
    items: Iterable[Mapping[str, Any]], minute_ms: int, now_ms: int,
) -> Mapping[str, Any] | None:
    """The item that covers the minute starting at ``minute_ms``; of several, the shortest.

    So a picture sent inside a stay is the picture. An item still going (no end, ``state`` now)
    lasts to ``now_ms``; any other item with no end, a reminder, is a moment. A moment covers the
    minute it falls in; ties go to the earlier start, then the id.
    """
    window_end = minute_ms + _MINUTE_MS
    best: tuple[tuple[int, int, str], Mapping[str, Any]] | None = None
    for item in items:
        start, end = int(item["start_ms"]), item["end_ms"]
        if end is None:
            end = now_ms if item["state"] == "now" else start
        if end <= start:  # a moment is in the minute it falls in
            covers = minute_ms <= start < window_end
        else:
            covers = start < window_end and end > minute_ms
        if not covers:
            continue
        rank = (max(end - start, 0), start, str(item["id"]))
        if best is None or rank < best[0]:
            best = (rank, item)
    return None if best is None else best[1]


__all__ = [
    "LABEL_MAX_CHARS",
    "LINE_MAX_CHARS",
    "MAX_HIGHLIGHTS",
    "Highlight",
    "clock_text",
    "covering",
    "day_notes",
    "merge_highlights",
    "parse_clock",
    "resolve_day",
]
