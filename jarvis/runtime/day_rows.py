"""ADR 0391: the rows of his day page an answer may link to, as ``my_day`` names them to her.

The phone's day page lists a day's spans by id (``GET /inherent/day``, ADR 0302) and its map
opens the outings between his long stays (``GET /inherent/footprint``, ADR 0323). For an answer
to point at one, she has to have read its id this turn, so ``my_day`` lists a day's rows with
theirs. This folds them as those two routes do, from the same stores: what he was doing (the
doing lane), where he stayed, his sleep and workouts, his calls and the day's events, and his
outings. A move is left out, its outing stands for it; so are the stretches nothing speaks to.
The tool layer cannot fold the doing lane, so the runtime hands it :meth:`DayRows.read`.

Layer rules: runtime may wire across layers.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.doing_day import page_reach, read_day_doing
from jarvis.state import footprint, places
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.doing import PLANNED_KIND, fact_spans
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.imports import shared_reads

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from pathlib import Path

MAX_ROWS: Final = 20
"""At most this many rows of a day, the longest kept, so a day's list stays one short read."""
_SPANS: Final = {
    ("doing", "doing"): "doing",
    ("doing", PLANNED_KIND): "planned",
    ("place", "stay"): "stay",
    ("body", "sleep"): "sleep",
    ("body", "workout"): "workout",
    ("use", "call"): "call",
}
"""Which spans of the page are rows, by lane and kind, and what each is called to her."""


class DayRows:
    """Folds a day's rows from the stores ``GET /inherent/day`` folds it from."""

    def __init__(self, event_log: Path, sources: DaySources) -> None:
        """``sources`` are the day line's stores; its ``memory_db`` also holds his kept places."""
        self._event_log = event_log
        self._sources = sources

    def read(self, day: date, now_ms: int) -> list[dict[str, Any]]:
        """``day``'s rows as of ``now_ms``, by start.

        Each is ``{kind, id, date, start_ms, end_ms, title, what}``: ``kind`` is ``row`` (a span
        or item of the day page) or ``outing`` (a trip of its map); ``end_ms`` is ``now_ms`` for
        one still going, and ``title`` may be ``None``. Past :data:`MAX_ROWS` the shortest are
        left out.
        """
        sources = self._sources
        start_ms, end_ms = day_window(day, sources.zone)
        before_ms, after_ms = page_reach(day, sources.zone)
        kept = [] if sources.memory_db is None else places.read_kept(sources.memory_db)
        with (
            closing(open_runtime_event_log(self._event_log)) as conn,
            shared_reads(before_ms=before_ms, after_ms=after_ms),
        ):
            items = fold_day(conn, start_ms, end_ms, now_ms, sources).items
            doing = read_day_doing(conn, sources.memory_db, day, items, sources, now_ms)
            outings = footprint.fold(conn, start_ms, end_ms, now_ms, kept)["outings"]
        on = day.isoformat()
        spans = [*fact_spans(items), *doing.served({str(item["id"]) for item in items})]
        rows = [
            _row({**span, "title": _span_title(span)}, what, on, now_ms) for span in spans
            if (what := _SPANS.get((span["lane"], span["kind"]))) is not None
        ]
        rows += [
            _row({**item, "title": _event_title(item)}, "event", on, now_ms)
            for item in items if item["kind"] == "event"
        ]
        stays = {str(span["id"]): span.get("title") for span in spans if span["kind"] == "stay"}
        rows += [_outing(one, stays, on, now_ms) for one in outings]
        longest = sorted(rows, key=lambda row: row["start_ms"] - row["end_ms"])[:MAX_ROWS]
        return sorted(longest, key=lambda row: (row["start_ms"], row["id"]))


def _row(span: Mapping[str, Any], what: str, on: str, now_ms: int) -> dict[str, Any]:
    end = span.get("end_ms")
    return {
        "kind": "row",
        "id": str(span["id"]),
        "date": on,
        "start_ms": int(span["start_ms"]),
        "end_ms": now_ms if end is None else int(end),
        "title": span.get("title") or None,
        "what": what,
    }


def _span_title(span: Mapping[str, Any]) -> str | None:
    """A span's title; a stretch of the doing lane without one says its category, if it has one."""
    if span.get("title"):
        return str(span["title"])
    category = span.get("category")
    return str(category) if span["lane"] == "doing" and category not in (None, "unknown") else None


def _event_title(item: Mapping[str, Any]) -> str | None:
    """An event's title, or none for one he marked private."""
    return None if item.get("private") else item.get("title") or None


def _outing(
    outing: Mapping[str, Any], stays: Mapping[str, str | None], on: str, now_ms: int,
) -> dict[str, Any]:
    """An outing as a row: its activity, and the stay it ended at when that has a name."""
    to = stays.get(str(outing.get("to_stay")))
    activity = outing.get("activity") or None
    title = f"{activity or 'out'} to {to}" if to else activity
    return {
        **_row(outing, "outing", on, now_ms), "kind": "outing", "title": title,
    }


__all__ = ["MAX_ROWS", "DayRows"]
