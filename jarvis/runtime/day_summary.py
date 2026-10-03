"""Runtime wiring for the daily conversation summaries (ADR 0142).

Once a day, after the configured local time, the schedule writes a summary for
every local date before today that has records and no summary row, oldest first.
That one rule backfills history on first deploy and catches up days the Mac slept
through. Each day is one call on a dedicated client pinned to ``day_summary.preset``,
run in a thread off the loop; L3 builds the input and gates the answer, L2 appends
the row. A rejected or failed day stores nothing and is picked up again on the next
run; it never stops the days after it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.day_summary import build_day_summary_messages, check_day_summary
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_day_summary,
    day_records,
    local_now,
    pending_days,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "day_summary"
_DEFAULT_MAX_CHARS: Final[int] = 3000


@dataclass(frozen=True)
class DaySummarySettings:
    """The ``day_summary:`` block of ``config/jarvis.yaml``."""

    at: time
    preset: str
    prompt: str
    max_chars: int = _DEFAULT_MAX_CHARS

    @classmethod
    def from_config(cls, raw: object) -> DaySummarySettings | None:
        """The settings, or None (feature off) when the block or ``at`` is absent or unusable."""
        if not isinstance(raw, Mapping) or raw.get("at") is None:
            return None
        preset, prompt, max_chars = raw.get("preset"), raw.get("prompt"), raw.get("max_chars")
        try:
            at = time.fromisoformat(str(raw["at"]))
        except ValueError:
            LOGGER.warning("day_summary.at %r is not HH:MM; no day summary is written", raw["at"])
            return None
        if not (isinstance(preset, str) and preset and isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("day_summary needs preset and prompt; no day summary is written")
            return None
        size = max_chars if isinstance(max_chars, int) and max_chars > 0 else _DEFAULT_MAX_CHARS
        return cls(at=at, preset=preset, prompt=prompt.strip(), max_chars=size)


def write_day_summary(
    day: str,
    *,
    memory: MemorySettings,
    settings: DaySummarySettings,
    client: LLMClient,
    cost_recorder: CostRecorder,
) -> str:
    """One LLM call for ``day``; append the row if the answer passes the gates.

    Returns a one-line outcome. A rejected answer stores nothing.
    """
    records = day_records(memory.db_path, day)
    if not records:
        return "no records"
    result = cost_recorder.chat(
        client,
        messages=build_day_summary_messages(day=day, records=records),
        system=settings.prompt,
        tools=None,
        tool_choice=None,
        kind=KIND,
        turn_id=None,
    )
    reason = check_day_summary(
        result.text,
        result.finish_reason,
        max_chars=settings.max_chars,
        record_ids=[rid for rid, _, _, _ in records],
    )
    if reason is not None:
        LOGGER.warning("day_summary: %s rejected (%s); nothing stored", day, reason)
        return f"rejected ({reason})"
    summary = (result.text or "").strip()
    append_day_summary(
        memory.db_path,
        day=day,
        summary=summary,
        model=result.model_used or client.model,
        record_count=len(records),
        input_chars=sum(len(text) for _, _, _, text in records),
        output_chars=len(summary),
    )
    return f"landed ({len(records)} records, {len(summary)} chars)"


def run_day_summaries(  # noqa: PLR0913 — the store, the knobs, the client and the accounting seam.
    memory: MemorySettings,
    settings: DaySummarySettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    today: date,
) -> dict[str, str]:
    """Summarise every past day that has records and no summary, oldest first.

    Blocking (one LLM call per day); the schedule runs it in a thread, which is why
    it opens its own Event Log connection for the cost records. A failed day is
    logged and the next one still runs.
    """
    outcomes: dict[str, str] = {}
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        for day, _count, _chars in pending_days(memory.db_path, today):
            try:
                outcomes[day] = write_day_summary(
                    day,
                    memory=memory,
                    settings=settings,
                    client=client,
                    cost_recorder=cost_recorder,
                )
            except Exception:
                LOGGER.exception("day_summary: %s failed; nothing stored", day)
                outcomes[day] = "failed"
    return outcomes


class DaySummarySchedule:
    """Checks every ``poll_s``; runs once per local date, after ``settings.at``."""

    def __init__(  # noqa: PLR0913 — the store, the knobs, the accounting seam, the poll.
        self,
        *,
        memory: MemorySettings,
        settings: DaySummarySettings,
        llm_config: Mapping[str, Any],
        event_log_path: Path,
        pricing_table: Mapping[str, Mapping[str, float]],
        poll_s: float = 60.0,
    ) -> None:
        """Bind the store and the knobs; nothing runs until :meth:`run`."""
        self._memory = memory
        self._settings = settings
        self._llm_config = llm_config
        self._event_log_path = event_log_path
        self._pricing_table = pricing_table
        self._poll_s = poll_s
        self._client: LLMClient | None = None
        self._last: date | None = None

    def due(self, now: datetime) -> date | None:
        """Today (this Mac's zone) once ``at`` has passed and today was not tried yet."""
        local = now.astimezone()
        if local.time() < self._settings.at or local.date() == self._last:
            return None
        return local.date()

    def write(self, today: date) -> dict[str, str]:
        """One attempt per local date: a day that fails waits for tomorrow's run."""
        self._last = today
        if self._client is None:
            self._client = build_compact_client(self._llm_config, self._settings.preset)
        return run_day_summaries(
            self._memory,
            self._settings,
            self._client,
            event_log_path=self._event_log_path,
            pricing_table=self._pricing_table,
            today=today,
        )

    async def run(self) -> None:
        """A Mac asleep at ``at`` writes on its first check after waking or after a start."""
        while True:
            await asyncio.sleep(self._poll_s)
            today = self.due(local_now())
            if today is None:
                continue
            try:
                outcomes = await asyncio.to_thread(self.write, today)
            except Exception:
                LOGGER.exception("day_summary: scheduled run for %s failed", today)
                continue
            LOGGER.info("day_summary: scheduled %s -> %s", today, outcomes)


__all__ = [
    "DaySummarySchedule",
    "DaySummarySettings",
    "run_day_summaries",
    "write_day_summary",
]
