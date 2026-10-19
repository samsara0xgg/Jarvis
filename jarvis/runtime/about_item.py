"""ADR 0298: the record of the day-line item his phone has open, for the turn's about line.

The phone names the item it has open (ADR 0214) by kind, id, title and start. Its title is all
the line said, so "how did I sleep" on a sleep row reached her with no numbers. This folds the
day the item starts on, the way ``GET /inherent/day`` does, finds the item by its id and writes
its fields on one line, so she answers from the same record the phone drew.

The fold reads only local stores: no terminal (his Mac's rows come from what it shipped), so a
turn never waits on another machine for this.
"""

from __future__ import annotations

import json
import time as _time
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

RECORD_MAX_CHARS: Final = 900
_TEXT_MAX_CHARS: Final = 300
_DROPPED: Final = frozenset({"id", "lane", "kind", "start_ms", "end_ms", "stages"})
_SLEEP_STAGES: Final = ("deep", "core", "rem", "awake", "unspecified", "in_bed")


class AboutItems:
    """Finds the day-line item an ``about`` names and writes its record."""

    def __init__(
        self, event_log: Path, sources: DaySources, clock: Callable[[], float] = _time.time,
    ) -> None:
        """``sources`` are the local stores the day is folded from; ``clock`` is in seconds."""
        self._event_log = event_log
        self._sources = sources
        self._clock = clock

    def record(self, about: Mapping[str, Any]) -> str | None:
        """The item's record on one line, or ``None`` when the day has no item with its id."""
        zone = self._sources.zone
        now_ms = int(self._clock() * 1000)
        start = about.get("start_ms")
        at_ms = start if isinstance(start, int) else now_ms
        start_ms, end_ms = day_window(datetime.fromtimestamp(at_ms / 1000, zone).date(), zone)
        with closing(open_runtime_event_log(self._event_log)) as conn:
            items = fold_day(conn, start_ms, end_ms, now_ms, self._sources).items
        item = next((one for one in items if str(one["id"]) == about["id"]), None)
        return None if item is None else item_record(item, zone)


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


def _clip(value: object) -> object:
    if isinstance(value, str) and len(value) > _TEXT_MAX_CHARS:
        return value[: _TEXT_MAX_CHARS - 1] + "…"
    return value


__all__ = ["RECORD_MAX_CHARS", "AboutItems", "item_record"]
