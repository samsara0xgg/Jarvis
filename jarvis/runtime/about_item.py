"""ADR 0298: the record of the day-line item his phone has open, for the turn's about line.

The phone names the item it has open (ADR 0214) by kind, id, title and start. Its title is all
the line said, so "how did I sleep" on a sleep row reached her with no numbers. This folds the
day the item starts on, the way ``GET /inherent/day`` does, finds the item by its id and writes
its fields on one line, so she answers from the same record the phone drew.

A stretch of the doing lane (``doing``, ADR 0410) is found by its time, not its id: the lane is
cut again as the day goes on, and a stretch's id is its start. Its record is the window the phone
drew, what the day page says now about the stretch that shares most of it, where his screens had
him in it, and the items it holds.

The fold reads only local stores: no terminal (his Mac's rows come from what it shipped), so a
turn never waits on another machine for this.
"""

from __future__ import annotations

import json
import time as _time
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.runtime.doing_day import page_reach, read_day_doing
from jarvis.state import attention
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.doing import best, held_by
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.imports import shared_reads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

RECORD_MAX_CHARS: Final = 900
STRETCH_RECORD_MAX_CHARS: Final = 1800
"""A stretch holds several items, each of which may take a record's whole length."""
_STRETCH_WORDS: Final = ("title", "detail", "category", "basis", "confidence", "confirmed")
_SCREEN_THREADS: Final = 5
_TEXT_MAX_CHARS: Final = 300
_DROPPED: Final = frozenset({"id", "lane", "kind", "start_ms", "end_ms", "stages"})
_SLEEP_STAGES: Final = ("deep", "core", "rem", "awake", "unspecified", "in_bed")


class AboutItems:
    """Finds the day-line item an ``about`` names and writes its record, or its row."""

    def __init__(
        self, event_log: Path, sources: DaySources, clock: Callable[[], float] = _time.time,
    ) -> None:
        """``sources`` are the local stores the day is folded from; ``clock`` is in seconds."""
        self._event_log = event_log
        self._sources = sources
        self._clock = clock

    def record(self, about: Mapping[str, Any]) -> str | None:
        """The item's record on one line, or ``None`` when the day has no item with its id.

        A ``doing`` stretch always has one: a window nothing was recorded in says so.
        """
        if about["kind"] == "doing":
            return self._stretch(about).record(self._sources.zone)
        found = self._item(about)
        return None if found is None else item_record(found[0], self._sources.zone)

    def row(self, about: Mapping[str, Any]) -> dict[str, Any] | None:
        """The item as a row an answer may link to (ADR 0391), or ``None`` as for :meth:`record`.

        ``{kind: "row", id, date, start_ms, end_ms, title}``: ``date`` is the day it starts on,
        ``end_ms`` is now for one still going and its start for a moment; ``title`` is the one
        the phone gave, else ``None``.

        A ``doing`` stretch is the day page's stretch that shares most of its window, by that
        stretch's id now; ``None`` when no stretch of the page overlaps it.
        """
        if about["kind"] == "doing":
            stretch = self._stretch(about)
            found = None if stretch.span is None else (stretch.span, stretch.now_ms)
        else:
            found = self._item(about)
        if found is None:
            return None
        item, now_ms = found
        start, end = int(item["start_ms"]), item["end_ms"]
        if end is None:
            end = now_ms if item.get("state") == "now" else start
        zone = self._sources.zone
        return {
            "kind": "row",
            "id": str(item["id"]),
            "date": datetime.fromtimestamp(start / 1000, zone).date().isoformat(),
            "start_ms": start,
            "end_ms": int(end),
            "title": about.get("title") or None,
        }

    def _item(self, about: Mapping[str, Any]) -> tuple[dict[str, Any], int] | None:
        """The item of the day ``about`` starts on with its id, and the time it was folded at."""
        zone = self._sources.zone
        now_ms = int(self._clock() * 1000)
        start = about.get("start_ms")
        at_ms = start if isinstance(start, int) else now_ms
        start_ms, end_ms = day_window(datetime.fromtimestamp(at_ms / 1000, zone).date(), zone)
        with closing(open_runtime_event_log(self._event_log)) as conn:
            items = fold_day(conn, start_ms, end_ms, now_ms, self._sources).items
        item = next((one for one in items if str(one["id"]) == about["id"]), None)
        return None if item is None else (item, now_ms)

    def _stretch(self, about: Mapping[str, Any]) -> Stretch:
        """The window ``about`` names, as the day page of the day it starts on cuts it now.

        That page's lane holds the day before's stretches that run past midnight too (ADR 0313),
        and its items reach as far as its stretches do.
        """
        zone, sources = self._sources.zone, self._sources
        now_ms = int(self._clock() * 1000)
        start, end = int(about["start_ms"]), int(about["end_ms"])
        day = datetime.fromtimestamp(start / 1000, zone).date()
        day_start, day_end = day_window(day, zone)
        before_ms, after_ms = page_reach(day, zone)
        with (
            closing(open_runtime_event_log(self._event_log)) as conn,
            shared_reads(before_ms=before_ms, after_ms=after_ms),
        ):
            items = fold_day(conn, day_start, day_end, now_ms, sources).items
            doing = read_day_doing(conn, sources.memory_db, day, items, sources, now_ms)
        lanes = [doing.own] if doing.prior is None else [doing.own, doing.prior]
        pool = {str(one["id"]): one for one in items}
        for lane in lanes:
            pool.update({str(one["id"]): one for one in lane.items if str(one["id"]) not in pool})
        window = {"start_ms": start, "end_ms": end}
        return Stretch(
            start, end,
            best(doing.served(pool), start, end, now_ms),
            [pool[one] for one in held_by(window, pool.values(), now_ms)],
            attention.within(doing.pieces, start, end, doing.threads, doing.told),
            now_ms,
        )


class Stretch(NamedTuple):
    """A window of the doing lane: the page's stretch sharing most of it, its items, his screens."""

    start_ms: int
    end_ms: int
    span: dict[str, Any] | None
    items: list[dict[str, Any]]
    screens: Mapping[str, Any]
    now_ms: int

    def record(self, zone: tzinfo) -> str:
        """The window on one line: its times, the page's words on it, his screens, then its items.

        Items are kept whole while they fit :data:`STRETCH_RECORD_MAX_CHARS`; the rest are
        counted.
        """
        parts = [_span({"start_ms": self.start_ms, "end_ms": self.end_ms}, zone)]
        if self.span is not None:
            words = {k: self.span[k] for k in _STRETCH_WORDS if self.span.get(k) is not None}
            parts.append(
                f"his day page has {_span(self.span, zone)} there: "
                + json.dumps(words, ensure_ascii=False, separators=(",", ":")),
            )
        threads = self.screens.get("threads") or []
        if threads:
            held = ", ".join(
                f"{one['name']} {_minutes(one['ms'])} min on {one['where']}"
                for one in threads[:_SCREEN_THREADS]
            )
            parts.append(f"his screens had him {_minutes(self.screens['front_ms'])} min: {held}")
        parts.append(f"{len(self.items)} items of his day line in it")
        line, left = "; ".join(parts), len(self.items)
        tail = len(" | and 999 more items")
        for item in self.items:
            said = f" | {item['kind']}: {item_record(item, zone)}"
            if len(line) + len(said) > STRETCH_RECORD_MAX_CHARS - tail:
                break
            line, left = line + said, left - 1
        if left:
            line += f" | and {left} more items"
        return line[:STRETCH_RECORD_MAX_CHARS]


def item_record(item: Mapping[str, Any], zone: tzinfo) -> str:
    """``item`` as one line: its local times, a sleep's minutes by stage, then its other fields."""
    parts = [_span(item, zone)]
    stages = item.get("stages")
    if isinstance(stages, list) and stages:
        parts.append(_sleep_stages(stages))
    rest = {
        key: _clip(value) for key, value in item.items()
        if key not in _DROPPED and value is not None
    }
    if rest:
        parts.append(json.dumps(rest, ensure_ascii=False, separators=(",", ":")))
    line = "; ".join(parts)
    if len(line) <= RECORD_MAX_CHARS:
        return line
    return line[: RECORD_MAX_CHARS - 1] + "…"


def _span(item: Mapping[str, Any], zone: tzinfo) -> str:
    start = datetime.fromtimestamp(int(item["start_ms"]) / 1000, zone)
    end_ms = item.get("end_ms")
    if end_ms is None:
        return f"from {start:%Y-%m-%d %H:%M}, still going" if item.get("state") == "now" else (
            f"at {start:%Y-%m-%d %H:%M}"
        )
    end = datetime.fromtimestamp(int(end_ms) / 1000, zone)
    if int(end_ms) == int(item["start_ms"]):
        return f"at {start:%Y-%m-%d %H:%M}"
    shown = f"{end:%H:%M}" if end.date() == start.date() else f"{end:%Y-%m-%d %H:%M}"
    return f"{start:%Y-%m-%d %H:%M} to {shown}"


def _sleep_stages(stages: list[Mapping[str, Any]]) -> str:
    """Minutes in each stage, overlaps of one stage counted once, and how often he woke."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for one in stages:
        spans.setdefault(str(one["stage"]), []).append((int(one["start_ms"]), int(one["end_ms"])))
    said = []
    for stage in sorted(spans, key=lambda s: (*_SLEEP_STAGES, s).index(s)):
        minutes = round(_covered(spans[stage]) / 60_000)
        said.append(f"{stage} {minutes} min")
    woke = len(spans.get("awake", ()))
    return "stages: " + ", ".join(said) + f"; woke {woke} times"


def _covered(spans: list[tuple[int, int]]) -> int:
    total, reach = 0, None
    for start, end in sorted(spans):
        if reach is None or start > reach:
            total += end - start
            reach = end
        elif end > reach:
            total += end - reach
            reach = end
    return total


def _minutes(ms: float) -> int:
    return round(ms / 60_000)


def _clip(value: object) -> object:
    if isinstance(value, str) and len(value) > _TEXT_MAX_CHARS:
        return value[: _TEXT_MAX_CHARS - 1] + "…"
    return value


__all__ = ["RECORD_MAX_CHARS", "STRETCH_RECORD_MAX_CHARS", "AboutItems", "Stretch", "item_record"]
