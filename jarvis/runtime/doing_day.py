"""ADR 0313: a day's doing lane as the page and his answers see it, past midnight included.

A day's waking part ends where his night's sleep begins, which can be after midnight
(:func:`jarvis.state.doing.waking_window`). So a day's lane is read from its day line folded to
six hours past its end (:func:`lane_line`), and the page of the day after shows, before its own
lane, the stretches of the day before that run past its midnight: the same spans with the same
ids, as a sleep across midnight shows on both days. An answer about one of them is kept for the
day it belongs to.

Each stretch the page shows also tells where his attention went in it (ADR 0314,
:func:`jarvis.state.attention.within`), by the threads its keys were told to serve (ADR 0315);
a planned one, which has not happened, tells nothing.

Layer rules: runtime may wire across layers.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from jarvis.state import attention
from jarvis.state.attention import Thread
from jarvis.state.day_line import day_window, fold_day
from jarvis.state.doing import (
    DOING_KINDS,
    PLANNED_KIND,
    WAKING_BED_REACH_MS,
    Doing,
    fact_spans,
    his_facts,
    read_doing,
    waking_window,
)
from jarvis.state.memory_db import attention_threads, open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Mapping, Sequence
    from datetime import date, tzinfo
    from pathlib import Path

    from jarvis.state.attention import Piece
    from jarvis.state.day_line import DayLine, DaySources

LOGGER = logging.getLogger(__name__)


def lane_line(
    conn: sqlite3.Connection, day: date, sources: DaySources, now_ms: int,
) -> DayLine:
    """``day``'s line folded to where its waking part can reach: six hours past its end."""
    start_ms, end_ms = day_window(day, sources.zone)
    return fold_day(conn, start_ms, end_ms + WAKING_BED_REACH_MS, now_ms, sources)


def page_reach(day: date, zone: tzinfo) -> tuple[int, int]:
    """How far before and after ``day`` the folds of its page reach: the day before, the bed reach.

    What :func:`read_day_doing` may fold beside the page's own line, for
    :func:`jarvis.state.imports.shared_reads`.
    """
    start_ms, _ = day_window(day, zone)
    before_ms, _ = day_window(day - timedelta(days=1), zone)
    return start_ms - before_ms, WAKING_BED_REACH_MS


def reach_past_start(
    items: Sequence[Mapping[str, Any]], day: date, zone: tzinfo, now_ms: int,
) -> int:
    """Where the waking part of the day before ``day`` ends, past ``day``'s start; else its start.

    ``items`` are ``day``'s line: they hold any night that begins after midnight, and the one he
    was in at midnight, so the day before's end is read from them by the same rule.
    """
    start_ms, _ = day_window(day, zone)
    before_ms, _ = day_window(day - timedelta(days=1), zone)
    window = waking_window(his_facts(fact_spans(items)), before_ms, start_ms, now_ms)
    return max(start_ms, window.end_ms)


class Lane(NamedTuple):
    """One day's doing lane and the items it was read from."""

    day: date
    items: list[dict[str, Any]]
    doing: Doing


class DayDoing(NamedTuple):
    """What a day's page shows of the doing lane: its own, and the day before's past midnight."""

    own: Lane
    prior: Lane | None
    """The day before, when its waking part runs past this day's start; else None."""
    start_ms: int
    pieces: list[Piece]
    """Where his attention was through the stretches the page shows."""
    threads: dict[str, Thread]
    """What the keys told so far served (ADR 0315)."""

    def before(self) -> list[dict[str, Any]]:
        """The day before's doing spans that run past this day's start, by start."""
        if self.prior is None:
            return []
        reach = self.prior.doing.coverage["waking_end_ms"]
        return [
            s for s in self.prior.doing.served
            if s["kind"] in DOING_KINDS and (reach if s["end_ms"] is None else s["end_ms"])
            > self.start_ms
        ]

    def served(self, ids: Collection[str]) -> list[dict[str, Any]]:
        """The doing spans the page shows, holding only the items whose ids are in ``ids``."""
        return [
            {
                **s, "item_ids": [i for i in s["item_ids"] if i in ids],
                "attention": None if s["kind"] == PLANNED_KIND
                else attention.within(self.pieces, s["start_ms"], s["end_ms"], self.threads),
            }
            for s in (*self.before(), *self.own.doing.served)
        ]

    def lane_of(self, span_id: str) -> Lane | None:
        """The lane an unplanned doing span the page shows belongs to, else None."""
        if any(s["id"] == span_id and s["kind"] in DOING_KINDS for s in self.own.doing.served):
            return self.own
        if any(s["id"] == span_id for s in self.before()):
            return self.prior
        return None


def read_day_doing(  # noqa: PLR0913 — the stores, the day, its line and the clock.
    conn: sqlite3.Connection, memory_db: Path | None, day: date,
    items: list[dict[str, Any]], sources: DaySources, now_ms: int,
) -> DayDoing:
    """``day``'s doing lane and the day before's past midnight, ``items`` its calendar line.

    A day not over yet is read from ``items`` (nothing lies past now); one that is over, from its
    line folded six hours on. The day before is folded only when its waking part reaches past
    ``day``'s start. Where his attention was is read once, over the stretches both show.
    """
    zone = sources.zone
    start_ms, end_ms = day_window(day, zone)
    own_items = items if now_ms <= end_ms else lane_line(conn, day, sources, now_ms).items
    own = Lane(day, own_items, read_doing(memory_db, day.isoformat(), own_items, start_ms, end_ms,
                                          now_ms))
    prior = None
    if reach_past_start(items, day, zone, now_ms) > start_ms:
        before = day - timedelta(days=1)
        before_start, before_end = day_window(before, zone)
        before_items = lane_line(conn, before, sources, now_ms).items
        prior = Lane(before, before_items, read_doing(
            memory_db, before.isoformat(), before_items, before_start, before_end, now_ms,
        ))
    told = DayDoing(own, prior, start_ms, [], _threads(memory_db))
    shown = [s for s in (*told.before(), *own.doing.served) if s["kind"] != PLANNED_KIND]
    if not shown:
        return told
    return told._replace(pieces=attention.read(
        sources, min(s["start_ms"] for s in shown), max(s["end_ms"] for s in shown),
    ))


def _threads(memory_db: Path | None) -> dict[str, Thread]:
    """What each key told so far served; none when the store cannot be read, logged."""
    if memory_db is None or not memory_db.is_file():
        return {}
    try:
        with closing(open_memory_db(memory_db)) as conn:
            return {key: Thread(*told) for key, told in attention_threads(conn).items()}
    except Exception:
        LOGGER.exception("doing: what the keys of his attention served could not be read")
        return {}


__all__ = ["DayDoing", "Lane", "lane_line", "page_reach", "reach_past_start", "read_day_doing"]
