"""Runtime wiring for the topics night (ADR 0390).

Runs inside the daily day-summary job, after the summaries and core memory: every past day that
has a summary and comes after the last day sorted, oldest first and one at a time, so the first
run covers every day there is. Each day is one call on a client pinned to ``topics.preset``; L3
builds the input and gates the answer, L2 applies it. A day without a message is recorded without
a call. A day that fails both attempts stops the chain (later days build on it) and is tried again
on the next run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.topics_night import (
    EARLIER_DAYS,
    DayLine,
    KeptTopic,
    Mention,
    build_night_messages,
    check_night,
    labels,
    summary_mentions,
)
from jarvis.state import topics
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import MemorySettings, day_records, latest_day_summaries, open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "topics_night"
_WORD_FOR_WORD: Final = "(the whole day, word for word)"
"""The heading of a day summary kept as the day's own words (``runtime.day_summary``)."""


@dataclass(frozen=True)
class TopicsSettings:
    """The ``topics:`` block of ``config/jarvis.yaml``."""

    preset: str
    prompt: str
    since: str = ""
    """The first day the first run sorts (``YYYY-MM-DD``); empty for every day with a summary."""

    @classmethod
    def from_config(cls, raw: object) -> TopicsSettings | None:
        """The settings, or None (no night) when the block, preset or prompt is absent."""
        if not isinstance(raw, Mapping):
            return None
        preset, prompt, since = raw.get("preset"), raw.get("prompt"), raw.get("since")
        if not (isinstance(preset, str) and preset and isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("topics needs preset and prompt; no topics night runs")
            return None
        first = ""
        if since is not None:
            try:
                first = date.fromisoformat(str(since)).isoformat()
            except ValueError:
                LOGGER.warning("topics.since %r is not YYYY-MM-DD; every day is sorted", since)
        return cls(preset=preset, prompt=prompt.strip(), since=first)


def _day_lines(conn: sqlite3.Connection, db: Path, day: str) -> list[DayLine]:
    """The day's messages, his and hers, with the topics each was said or filed in."""
    said = [
        record for record in day_records(db, day)
        if record[2] in topics.MESSAGE_SOURCES and record[3].strip()
    ]
    under = topics.filed_under(conn, [record[0] for record in said])
    return [
        DayLine(rid, ts, source, text, tuple(under.get(rid, ())))
        for rid, ts, source, text in said
    ]


def _earlier(conn: sqlite3.Connection, db: Path, day: str) -> list[Mention]:
    """Talk of the days before ``day`` that no topic holds, from their summaries.

    A day kept word for word has no summary lines to point at; its lines of his stand for it.
    """
    last = date.fromisoformat(day) - timedelta(days=1)
    first = last - timedelta(days=EARLIER_DAYS - 1)
    found: list[Mention] = []
    for one_day, summary in latest_day_summaries(conn, first.isoformat(), last.isoformat()):
        heading = summary.split("\n", 1)[0]
        if _WORD_FOR_WORD in heading:
            found += [
                Mention(one_day, text, rid) for rid, _ts, source, text in day_records(db, one_day)
                if source == "allen" and text.strip()
            ]
        else:
            found += summary_mentions(one_day, summary)
    open_ones = topics.unfiled_messages(conn, [one.record_id for one in found])
    return [one for one in found if one.record_id in open_ones]


def sort_day(  # noqa: PLR0913 - the day, the store, the knobs, the client, the seam and the zone.
    day: str,
    *,
    memory: MemorySettings,
    settings: TopicsSettings,
    client: LLMClient,
    cost_recorder: CostRecorder,
    zone: tzinfo,
) -> str:
    """One call for ``day``; apply its answer if it passes the gates. Returns a one-line outcome.

    A rejected answer stores nothing and the day stays pending.
    """
    db = memory.db_path
    base = {one.version_id for one in topics.current(db).values()}
    kept = [
        KeptTopic(one.topic, one.places[-1].day.isoformat()) for one in topics.listed(db, zone)
    ]
    with closing(open_memory_db(db)) as conn:
        lines = _day_lines(conn, db, day)
        earlier = _earlier(conn, db, day)
    if not lines:
        topics.apply_night(db, topics.NightPlan(day, (), ()), base=base, model="none")
        return "no messages"
    result = cost_recorder.chat(
        client,
        messages=build_night_messages(day=day, kept=kept, earlier=earlier, lines=lines),
        system=settings.prompt,
        tools=None,
        tool_choice=None,
        kind=KIND,
        turn_id=None,
    )
    answer = check_night(
        result.text, result.finish_reason, day=day, kept=kept,
        known=labels(day, earlier, lines),
    )
    if isinstance(answer, str):
        LOGGER.warning("topics_night: %s rejected (%s); nothing stored", day, answer)
        return f"rejected ({answer})"
    written = topics.apply_night(
        db, answer.plan, base=base, model=result.model_used or client.model,
    )
    if written is None:
        LOGGER.warning("topics_night: %s rejected (the topics changed during the call)", day)
        return "rejected (the topics changed during the call)"
    if answer.dropped:
        LOGGER.info("topics_night: %s left out new topics below the bar: %s", day, answer.dropped)
    return (
        f"landed ({len(written['opened'])} opened, {len(written['touched'])} touched, "
        f"{len(written['merged'])} merged, {written['filed']} lines filed, "
        f"{len(answer.dropped)} below the bar)"
    )


def run_topics_nights(  # noqa: PLR0913 - the store, the knobs, the client and the accounting seam.
    memory: MemorySettings,
    settings: TopicsSettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    today: date,
    zone: tzinfo,
) -> dict[str, str]:
    """Sort every pending day, oldest first; the first day that fails stops the chain.

    Blocking (one call per day); the schedule runs it in a thread, which is why it opens its own
    Event Log connection for the cost records.
    """
    outcomes: dict[str, str] = {}
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        for day in topics.pending_days(memory.db_path, today, settings.since):
            try:
                for _attempt in range(2):  # one retry on a rejected answer, as day summaries do
                    outcomes[day] = sort_day(
                        day, memory=memory, settings=settings, client=client,
                        cost_recorder=cost_recorder, zone=zone,
                    )
                    if not outcomes[day].startswith("rejected"):
                        break
            except Exception:
                LOGGER.exception("topics_night: %s failed; nothing stored", day)
                outcomes[day] = "failed"
            if outcomes[day].startswith(("rejected", "failed")):
                break  # later days build on this one
    return outcomes


__all__ = ["KIND", "TopicsSettings", "run_topics_nights", "sort_day"]
