"""ADR 0302: his answer about a stretch of a day, written for the timeline's route.

``POST /inherent/day/answer`` names a span of the doing lane that ``GET /inherent/day`` served.
The span is folded again here, from the same stores and the same rules, so what he answered is
what the page showed; the answer is kept by the time the span covers, not by its id, so a later
run that cuts the day differently still shows his words (:mod:`jarvis.state.doing`). A stretch of
the day before that runs past midnight is answered on the day after's page and kept for the day
it belongs to (ADR 0313).

Layer rules: runtime may wire across layers.
"""

from __future__ import annotations

import threading
import time as _time
from contextlib import closing
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.doing_day import DayDoing, Lane, read_day_doing
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.doing import DOING_KINDS, primary_place
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import append_doing_answer

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from pathlib import Path

ORIGIN: Final = "timeline"
"""Where the answer came from: the day timeline's route (the nightly review writes its own)."""


class DoingAnswers:
    """His answers about the spans of the doing lane, checked against what the page served."""

    def __init__(
        self, event_log: Path, sources: DaySources, clock: Callable[[], float] = _time.time,
    ) -> None:
        """``sources`` are the stores the day is folded from; its ``memory_db`` keeps the answers.

        ``clock`` is the brain's clock in seconds.
        """
        self._event_log = event_log
        self._sources = sources
        self._clock = clock
        self._lock = threading.Lock()

    def answer(
        self, on_day: date, span_id: str, choice: str | None, text: str | None,
    ) -> dict[str, Any]:
        """Keep his ``choice`` (an option of the span's question) or ``text``; the span as served.

        The answer carries the span's own start and end, and where he was (the latitude,
        longitude and kept place) when the span's primary place is a stay. It is kept for the day
        the span belongs to: the day before, for its stretch past midnight.

        Raises:
            LookupError: ``span_id`` is not a served, unplanned span of the doing lane on
                ``on_day``.
            ValueError: ``choice`` is not an option of the span's question (or it has none), or
                the day is outside the calendar.
            RuntimeError: there is no memory store to keep it in.
        """
        sources = self._sources
        memory_db = sources.memory_db
        if memory_db is None:
            msg = "answers cannot be kept right now (no memory store)"
            raise RuntimeError(msg)
        day = on_day.isoformat()
        with self._lock:
            now_ms = int(self._clock() * 1000)
            items, doing = self._read(on_day, now_ms)
            lane = doing.lane_of(span_id)
            if lane is None:
                msg = f"nothing on {day} is a span of what he was doing with the id {span_id!r}"
                raise LookupError(msg)
            span = next(s for s in doing.served(set()) if s["id"] == span_id)
            if choice is not None:
                options = (span["question"] or {}).get("options") or []
                if choice not in options:
                    msg = f"{choice!r} is not one of the options of this span's question"
                    raise ValueError(msg)
            place = _stay_of(lane, span)
            append_doing_answer(
                memory_db, day=lane.day.isoformat(), span_id=span_id, start_ms=span["start_ms"],
                end_ms=span["end_ms"], choice=choice, text=text, lat=place[0], lng=place[1],
                place_id=place[2], origin=ORIGIN,
            )
            _, after = self._read(on_day, now_ms)
            ids = {item["id"] for item in items}
            return next(s for s in after.served(ids) if s["id"] == span_id)

    def answer_stretch(self, on_day: date, stretch: tuple[int, int], text: str) -> dict[str, Any]:
        """Keep his ``text`` for a stretch he names himself; the span that now holds it.

        The answer is kept by that time and carved into the lane over it, and carries where he
        was when a stay holds most of it.

        Raises:
            ValueError: the stretch ends before it starts, leaves the day, touches no served,
                unplanned span of the doing lane, or touches spans of two days' waking parts.
            RuntimeError: there is no memory store to keep it in.
        """
        sources = self._sources
        memory_db = sources.memory_db
        if memory_db is None:
            msg = "answers cannot be kept right now (no memory store)"
            raise RuntimeError(msg)
        day = on_day.isoformat()
        start_ms, end_ms = day_window(on_day, sources.zone)
        first, last = stretch
        if not start_ms <= first < last <= end_ms:
            msg = f"the stretch is not inside {day}"
            raise ValueError(msg)
        with self._lock:
            now_ms = int(self._clock() * 1000)
            items, doing = self._read(on_day, now_ms)
            touched = [
                s for s in doing.served(set())
                if s["kind"] in DOING_KINDS and s["start_ms"] < last and s["end_ms"] > first
            ]
            lanes = [lane for s in touched if (lane := doing.lane_of(s["id"])) is not None]
            if not lanes:
                msg = f"nothing on {day} between those times is a span of what he was doing"
                raise ValueError(msg)
            if len({lane.day for lane in lanes}) > 1:
                msg = "the stretch runs from the day before's waking part into this day's"
                raise ValueError(msg)
            lane = lanes[0]
            place = _stay_of(lane, {"start_ms": first, "end_ms": last})
            append_doing_answer(
                memory_db, day=lane.day.isoformat(), span_id="", start_ms=first, end_ms=last,
                choice=None, text=text, lat=place[0], lng=place[1], place_id=place[2],
                origin=ORIGIN,
            )
            held = max(first, touched[0]["start_ms"])  # where his words begin inside the lane
            _, after = self._read(on_day, now_ms)
            ids = {item["id"] for item in items}
            return next(
                s for s in after.served(ids)
                if s["confirmed"] and s["start_ms"] <= held < s["end_ms"]
            )

    def _read(self, on_day: date, now_ms: int) -> tuple[list[dict[str, Any]], DayDoing]:
        """``on_day``'s calendar line and the doing lane its page shows, as the day route reads."""
        sources = self._sources
        start_ms, end_ms = day_window(on_day, sources.zone)
        with closing(open_runtime_event_log(self._event_log)) as conn:
            items = fold_day(conn, start_ms, end_ms, now_ms, sources).items
            return items, read_day_doing(conn, sources.memory_db, on_day, items, sources, now_ms)


def _stay_of(lane: Lane, span: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """``(lat, lng, place_id)`` of the stay that holds most of a span, else all None."""
    place = primary_place(lane.doing, span)
    if place is None or place["kind"] != "stay":
        return None, None, None
    stay = next(item for item in lane.items if item["id"] == place["id"])
    return float(stay["lat"]), float(stay["lng"]), place["place_id"]


__all__ = ["ORIGIN", "DoingAnswers"]
