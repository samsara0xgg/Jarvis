"""Runtime wiring for the nightly core memory consolidation (ADR 0146).

Runs inside the daily day-summary job, right after the summaries are written: every
local day after the current version's ``upto_day`` that has a day summary, up to
yesterday, oldest first and one at a time. Each day is one call on a dedicated client
pinned to ``core_memory.preset``; L3 builds the input and gates the answer, L2 appends
the version. A day that fails both attempts stops the chain (later days build on it) and
is picked up again on the next run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.core_memory import build_core_memory_messages, check_core_memory, day_labels
from jarvis.decision.core_memory_review import ReviewFailed, review_changes
from jarvis.decision.cost_guard import CostRecorder
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_nightly_core_memory,
    core_memory_pending_days,
    current_core_memory,
    day_records,
    latest_day_summaries,
    open_memory_db,
)

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.decision.surrogate_route import SurrogateRoute

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "core_memory"
_DEFAULT_MAX_CHARS: Final[int] = 4000
_DEFAULT_MAX_STALE: Final[int] = 5
DEFAULT_REVIEW_MODEL: Final[str] = "typesafe/jev-1.13"
_DEFAULT_REVIEW_TIMEOUT_MS: Final[int] = 10_000


@dataclass(frozen=True)
class CoreMemorySettings:
    """The ``core_memory:`` block of ``config/jarvis.yaml``."""

    preset: str
    prompt: str
    max_chars: int = _DEFAULT_MAX_CHARS
    max_stale: int = _DEFAULT_MAX_STALE
    review_min_confidence: float | None = None  # None = no Jev review
    review_model: str = DEFAULT_REVIEW_MODEL
    review_timeout_ms: int = _DEFAULT_REVIEW_TIMEOUT_MS

    @classmethod
    def from_config(cls, raw: object) -> CoreMemorySettings | None:
        """The settings, or None (consolidation off) when the block, preset or prompt is absent."""
        if not isinstance(raw, Mapping):
            return None
        preset, prompt = raw.get("preset"), raw.get("prompt")
        if not (isinstance(preset, str) and preset and isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("core_memory needs preset and prompt; no consolidation runs")
            return None
        chars, stale = raw.get("max_chars"), raw.get("max_stale")
        bar, model, timeout = (
            raw.get("review_min_confidence"),
            raw.get("review_model"),
            raw.get("review_timeout_ms"),
        )
        return cls(
            preset=preset,
            prompt=prompt.strip(),
            max_chars=chars if isinstance(chars, int) and chars > 0 else _DEFAULT_MAX_CHARS,
            max_stale=stale if isinstance(stale, int) and stale >= 0 else _DEFAULT_MAX_STALE,
            review_min_confidence=(
                float(bar)
                if isinstance(bar, int | float) and not isinstance(bar, bool) and 0 < bar <= 1
                else None
            ),
            review_model=model.strip()
            if isinstance(model, str) and model.strip()
            else DEFAULT_REVIEW_MODEL,
            review_timeout_ms=(
                timeout
                if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0
                else _DEFAULT_REVIEW_TIMEOUT_MS
            ),
        )


def consolidate_day(  # noqa: PLR0913 — the day, the store, the knobs, the client, the accounting seam, Jev.
    day: str,
    *,
    memory: MemorySettings,
    settings: CoreMemorySettings,
    client: LLMClient,
    cost_recorder: CostRecorder,
    review: SurrogateRoute | None = None,
) -> str:
    """One LLM call for ``day``; append a version if the answer passes the gates.

    Returns a one-line outcome. A rejected answer stores nothing; an empty change list
    still appends a version, because that is how ``upto_day`` advances. With ``review`` (and
    ``settings.review_min_confidence``) Jev reads each add and rewrite first: a change it does
    not keep is dropped and logged, and a review that fails rejects the day.
    """
    base = current_core_memory(memory.db_path)
    records = day_records(memory.db_path, day)
    with closing(open_memory_db(memory.db_path)) as conn:
        summaries = latest_day_summaries(conn, day, day)
    result = cost_recorder.chat(
        client,
        messages=build_core_memory_messages(
            day=day,
            doc=base.doc,
            day_summary=summaries[0][1] if summaries else "",
            records=records,
        ),
        system=settings.prompt,
        tools=None,
        tool_choice=None,
        kind=KIND,
        turn_id=None,
    )
    changes, reason = check_core_memory(
        result.text,
        result.finish_reason,
        doc=base.doc,
        labels=day_labels(records),
        max_stale=settings.max_stale,
        max_chars=settings.max_chars,
        day=day,
    )
    if changes is None:
        LOGGER.warning("core_memory: %s rejected (%s); nothing stored", day, reason)
        return f"rejected ({reason})"
    dropped: list[dict[str, Any]] = []
    if settings.review_min_confidence is not None:
        if review is None:  # a review that was asked for never silently turns off
            return "rejected (review: no Jev route)"
        try:
            changes, dropped = review_changes(
                changes,
                doc=base.doc,
                records=records,
                day=day,
                route=review,
                min_confidence=settings.review_min_confidence,
            )
        except ReviewFailed as exc:
            LOGGER.warning("core_memory: %s rejected (review: %s); nothing stored", day, exc)
            return f"rejected (review: {exc})"
    if (
        append_nightly_core_memory(
            memory.db_path,
            base_id=base.id,
            day=day,
            changes=changes,
            dropped=dropped,
        )
        is None
    ):
        LOGGER.warning("core_memory: %s rejected (core memory changed during the call)", day)
        return "rejected (core memory changed during the call)"
    return f"landed ({len(changes)} changes, {len(dropped)} dropped)"


def run_core_memory(  # noqa: PLR0913 — the store, the knobs, the client and the accounting seam.
    memory: MemorySettings,
    settings: CoreMemorySettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    today: date,
    review: SurrogateRoute | None = None,
) -> dict[str, str]:
    """Consolidate every pending day, oldest first; the first day that fails stops the chain.

    Blocking (one LLM call per day); the schedule runs it in a thread, which is why it
    opens its own Event Log connection for the cost records.
    """
    outcomes: dict[str, str] = {}
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        for day in core_memory_pending_days(memory.db_path, today):
            try:
                for _attempt in range(2):  # one retry on a rejected answer, as day summaries do
                    outcomes[day] = consolidate_day(
                        day,
                        memory=memory,
                        settings=settings,
                        client=client,
                        cost_recorder=cost_recorder,
                        review=review,
                    )
                    if not outcomes[day].startswith("rejected"):
                        break
            except Exception:
                LOGGER.exception("core_memory: %s failed; nothing stored", day)
                outcomes[day] = "failed"
            if not outcomes[day].startswith("landed"):
                break  # later days build on this one
    return outcomes


__all__ = ["CoreMemorySettings", "consolidate_day", "run_core_memory"]
