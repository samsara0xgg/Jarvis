"""Runtime wiring for compacting the running conversation.

On every supervisor tick the sweep asks whether the conversation is
eligible. With ``session.recent_records`` off: Jarvis idle for
``session.idle_before_compact_s`` with no Live connection open, the verbatim
history estimated past ``session.compact_at_context_ratio`` of the decision
preset's ``context_length``, and at least one record older than
``session.verbatim_window_days``. With it on, the window alone decides
(ADR 0134): the records it cuts out of the prompt are folded as soon as they
exist, whatever the idle time, size or age. When eligible, one job runs off
the loop thread on a dedicated client pinned to ``session.compact_preset``;
L3 builds the input and gates the answer, L2 commits the row only if the
summary it extends is still current. A failed job changes nothing and is
tried again on a later tick (window mode: after the next record).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.compaction import build_compaction_messages, check_summary, cited_record_ids
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    Record,
    SessionSettings,
    VerbatimStats,
    append_summary,
    compaction_range,
    local_now,
    verbatim_stats,
)

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Characters per token for the size estimate: Chinese-heavy text on
# DeepSeek's tokenizer. A threshold, not a guarantee; the ratio in config
# is the knob.
CHARS_PER_TOKEN: Final[float] = 1.5
# Bounds on the one summariser call; it is off the latency path and thinks.
_COMPACT_CALL_TIMEOUT_S: Final[float] = 600.0
_COMPACT_CALL_MAX_RETRIES: Final[int] = 1
# One summariser call reads at most this many characters of records (about
# 125k tokens at 1.6 characters per token; luna's limit is 200k a minute). A
# fold of 1080 records (160k characters, 2026-10-02) is one call; a longer
# one is a chain of calls, each extending the summary the one before wrote.
_CHUNK_CHARS: Final[int] = 200_000
_LINE_OVERHEAD_CHARS: Final[int] = 80  # record_id | time | speaker | in front of the words


def blocked_reason(  # noqa: PLR0911 — one linear condition list; each return names the blocker.
    stats: VerbatimStats,
    *,
    now: datetime,
    settings: SessionSettings,
    live_open: bool,
    context_length: int | None,
) -> str | None:
    """Why a compaction must not start now, or None when every condition holds.

    The size threshold is the gate: until the verbatim history passes
    ``compact_at_context_ratio`` of the window nothing else is looked at.
    """
    if not settings.compact_prompt:
        return "session.compact_prompt is empty"
    if context_length is None:
        return "the active decision preset has no context_length"
    if stats.newest_ts is None or stats.oldest_ts is None:
        return "no verbatim records"
    estimated_tokens = stats.chars / CHARS_PER_TOKEN
    limit = settings.compact_at_context_ratio * context_length
    if estimated_tokens <= limit:
        return f"~{estimated_tokens:.0f} tokens <= {limit:.0f} ({stats.chars} chars)"
    idle_s = (now - datetime.fromisoformat(stats.newest_ts)).total_seconds()
    if idle_s < settings.idle_before_compact_s:
        return f"idle {idle_s:.0f}s < {settings.idle_before_compact_s:.0f}s"
    if live_open:
        return "a Live connection is open"
    cutoff = now - timedelta(days=settings.verbatim_window_days)
    if datetime.fromisoformat(stats.oldest_ts) >= cutoff:
        return f"nothing older than {settings.verbatim_window_days} days"
    return None


def window_blocked_reason(
    stats: VerbatimStats, *, settings: SessionSettings, last_tried_ts: str | None,
) -> str | None:
    """Why a window fold must not start now, or None when it is due.

    Due when the window hides records no summary covers. A fold that was
    tried and did not land is not retried until a newer record exists, so a
    persistent failure costs one paid call per turn, not one per tick.
    """
    if not settings.compact_prompt:
        return "session.compact_prompt is empty"
    if not stats.hidden:
        return "the window hides nothing no summary covers"
    if stats.newest_ts == last_tried_ts:
        return "already tried since the newest record"
    return None


def preset_context_length(client: LLMClient) -> int | None:
    """``context_length`` of the preset the decision client is on, if configured."""
    preset = client.active_preset
    if preset is None:
        return None
    value = client.get_presets().get(preset, {}).get("context_length")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def build_compact_client(llm_config: Mapping[str, Any], preset_name: str) -> LLMClient:
    """A dedicated client pinned to the compact preset.

    The decision loop's own client switches presets in place, so sharing it
    would leave the wrong model active for the next turn (the ``vision``
    precedent).
    """
    presets = llm_config.get("presets")
    preset = presets.get(preset_name) if isinstance(presets, Mapping) else None
    if preset is None:
        msg = f"preset {preset_name!r} is not under llm.presets"
        raise ValueError(msg)
    if not isinstance(preset, Mapping):
        msg = f"llm.presets.{preset_name} is not a mapping"
        raise TypeError(msg)
    return LLMClient(
        {
            "provider": llm_config.get("provider", "openai"),
            "presets": {preset_name: dict(preset)},
            "default_preset": preset_name,
            "timeout_s": _COMPACT_CALL_TIMEOUT_S,
            "max_retries": _COMPACT_CALL_MAX_RETRIES,
        },
    )


def run_compaction(  # noqa: PLR0913 — the store, the knobs, the client and the accounting seam.
    memory: MemorySettings,
    settings: SessionSettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    now: datetime | None = None,
) -> str:
    """Run one compaction to completion and return a one-line outcome.

    Blocking (one LLM call); the sweep runs it in a thread, which is why
    the call opens its own Event Log connection for the cost record.
    """
    span = compaction_range(
        memory.db_path,
        window_days=settings.verbatim_window_days,
        since=settings.history_since,
        now=now,
        recent=settings.recent_records,
    )
    if span is None:
        return "nothing to fold"
    base_id, previous_summary = span.base_id, span.previous_summary
    landed_records = 0
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        for chunk in _chunks(span.records):
            result = cost_recorder.chat(
                client,
                messages=build_compaction_messages(
                    previous_summary=previous_summary, records=chunk,
                ),
                system=settings.compact_prompt,
                tools=None,
                tool_choice=None,
                kind="compaction",
                turn_id=None,
            )
            range_chars = sum(len(text) for _, _, _, text in chunk) + len(previous_summary or "")
            known_ids = {record_id for record_id, _, _, _ in chunk}
            if previous_summary:
                known_ids |= cited_record_ids(previous_summary)
            reason = check_summary(
                result.text,
                result.finish_reason,
                max_chars=settings.summary_max_chars,
                known_ids=known_ids,
            )
            if reason is not None:
                return (
                    f"summary rejected ({reason}) after {landed_records} of "
                    f"{len(span.records)} records; the last landed summary stays"
                )
            summary = (result.text or "").strip()
            landed_id = append_summary(
                memory.db_path,
                base_id=base_id,
                upto_record_id=chunk[-1][0],
                summary=summary,
                model=result.model_used or client.model,
                input_chars=range_chars,
                output_chars=len(summary),
            )
            if landed_id is None:
                return "summary discarded: the base summary is no longer current"
            base_id, previous_summary = landed_id, summary
            landed_records += len(chunk)
            LOGGER.info(
                "compaction: %d of %d records folded (%d chars -> %d), anchor %s",
                landed_records, len(span.records), range_chars, len(summary), chunk[-1][0],
            )
    return f"summary landed: {landed_records} records, anchor {span.records[-1][0]}"


def _chunks(records: tuple[Record, ...]) -> list[tuple[Record, ...]]:
    """``records`` in order, cut into runs of at most ``_CHUNK_CHARS`` characters.

    A single record over the limit is a run of its own.
    """
    runs: list[list[Record]] = [[]]
    size = 0
    for record in records:
        cost = len(record[3]) + _LINE_OVERHEAD_CHARS
        if runs[-1] and size + cost > _CHUNK_CHARS:
            runs.append([])
            size = 0
        runs[-1].append(record)
        size += cost
    return [tuple(run) for run in runs]


class CompactionSweep:
    """Ticks on the supervisor sweep; runs at most one job at a time."""

    def __init__(  # noqa: PLR0913 — the store, the knobs, the accounting seam, two probes.
        self,
        *,
        memory: MemorySettings,
        settings: SessionSettings,
        llm_config: Mapping[str, Any],
        event_log_path: Path,
        pricing_table: Mapping[str, Mapping[str, float]],
        context_length: Callable[[], int | None],
        live_open: Callable[[], bool],
    ) -> None:
        """Bind the store, the knobs and the two runtime probes; opens nothing."""
        self._memory = memory
        self._settings = settings
        self._llm_config = llm_config
        self._event_log_path = event_log_path
        self._pricing_table = pricing_table
        self._context_length = context_length
        self._live_open = live_open
        self._client: LLMClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_attempt: datetime | None = None
        self._last_tried_ts: str | None = None

    def tick(self) -> None:
        """Start a job if one is due and none is running. Loop thread only."""
        if self._task is not None and not self._task.done():
            return
        now = local_now()
        windowed = self._settings.recent_records > 0
        # A rejected or failed summary is retried no sooner than one idle
        # period later: every attempt is a paid call on the deep preset (live
        # 2026-09-14: a persistent length rejection retried on every tick).
        # Window mode retries after the next record instead (ADR 0134).
        if (
            not windowed
            and self._last_attempt is not None
            and (now - self._last_attempt).total_seconds() < self._settings.idle_before_compact_s
        ):
            return
        stats = verbatim_stats(
            self._memory.db_path,
            since=self._settings.history_since,
            recent=self._settings.recent_records,
        )
        if windowed:
            reason = window_blocked_reason(
                stats, settings=self._settings, last_tried_ts=self._last_tried_ts,
            )
        else:
            reason = blocked_reason(
                stats,
                now=now,
                settings=self._settings,
                live_open=self._live_open(),
                context_length=self._context_length(),
            )
        if reason is not None:
            LOGGER.debug("compaction not due: %s", reason)
            return
        LOGGER.info(
            "compaction due: %d verbatim chars since %s, %d hidden by the window",
            stats.chars, stats.oldest_ts, stats.hidden,
        )
        self._last_attempt = now
        self._last_tried_ts = stats.newest_ts
        self._task = asyncio.create_task(self._run(), name="session_compaction")

    async def _run(self) -> None:
        try:
            if self._client is None:
                self._client = build_compact_client(self._llm_config, self._settings.compact_preset)
            outcome = await asyncio.to_thread(
                run_compaction,
                self._memory,
                self._settings,
                self._client,
                event_log_path=self._event_log_path,
                pricing_table=self._pricing_table,
            )
        except Exception:
            LOGGER.exception("compaction failed; the previous summary stays")
            return
        LOGGER.info("compaction: %s", outcome)


__all__ = [
    "CHARS_PER_TOKEN",
    "CompactionSweep",
    "blocked_reason",
    "build_compact_client",
    "preset_context_length",
    "run_compaction",
    "window_blocked_reason",
]
