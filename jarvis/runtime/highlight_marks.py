"""ADR 0235: what the runtime does for ``mark_highlight``.

He names a day and a clock time; this folds that day, finds the item that covers the time (the
shortest, so a picture sent inside a stay is the picture), keeps his mark in memory.db and answers
with the item. The tool layer cannot reach the stores, so :meth:`HighlightMarks.mark` is what the
runtime hands it.
"""

from __future__ import annotations

import time as _time
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import ToolError
from jarvis.state.day_highlights import clock_text, covering, day_notes, parse_clock, resolve_day
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import append_highlight_mark

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class HighlightMarks:
    """His marks of the day line's items, found by day and time."""

    def __init__(
        self, event_log: Path, sources: DaySources, clock: Callable[[], float] = _time.time,
    ) -> None:
        """``sources`` are the stores the day is folded from; its ``memory_db`` keeps the marks.

        ``clock`` is the brain's clock in seconds; ``today`` and ``yesterday`` are read from it.
        """
        self._event_log = event_log
        self._sources = sources
        self._clock = clock

    def mark(self, day: str, at: str, on: bool, label: str | None) -> dict[str, Any]:  # noqa: FBT001 — the tool's argument.
        """Mark (``on``) or unmark the item of ``day`` that covers the local time ``at``.

        Raises:
            ToolError: the day or time is not one, or no item covers the time.
        """
        sources = self._sources
        memory_db = sources.memory_db
        if memory_db is None:
            msg = "there is no memory store to keep a mark in"
            raise ToolError(msg, code="no_store")
        now_ms = int(self._clock() * 1000)
        try:
            on_day = resolve_day(day, datetime.fromtimestamp(now_ms / 1000, sources.zone).date())
            moment = datetime.combine(on_day, parse_clock(at), sources.zone)
            start_ms, end_ms = day_window(on_day, sources.zone)
        except ValueError as exc:
            raise ToolError(str(exc), code="invalid_argument") from None
        with closing(open_runtime_event_log(self._event_log)) as conn:
            items = fold_day(conn, start_ms, end_ms, now_ms, sources).items
        item = covering(items, int(moment.timestamp()) * 1000, now_ms)
        if item is None:
            msg = f"nothing on {on_day.isoformat()} covers {at}"
            raise ToolError(msg, code="no_item")
        append_highlight_mark(
            memory_db, day=on_day.isoformat(), item_id=str(item["id"]), on=on, label=label,
            kind=str(item["kind"]), start_ms=int(item["start_ms"]),
        )
        start, end = int(item["start_ms"]), item["end_ms"]
        when = clock_text(start, sources.zone)
        if end is None:
            when += "-" if item["state"] == "now" else ""  # still going, or a moment
        elif int(end) != start:
            when += f"-{clock_text(int(end), sources.zone)}"
        found: dict[str, Any] = {
            "status": "marked" if on else "unmarked",
            "day": on_day.isoformat(),
            "kind": item["kind"],
            "when": when,
        }
        if on:
            _line, shown = day_notes(memory_db, on_day.isoformat(), items, sources.zone)
            found["label"] = next((h["label"] for h in shown if h["id"] == item["id"]), label)
        return found


__all__ = ["HighlightMarks"]
