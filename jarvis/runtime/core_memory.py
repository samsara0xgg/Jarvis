"""Runtime wiring for the nightly core memory consolidation (ADR 0146).

Runs inside the daily day-summary job, right after the summaries are written: every
local day after the current version's ``upto_day`` that has a day summary, up to
yesterday, oldest first and one at a time. Each day is one call on a dedicated client
pinned to ``core_memory.preset``; L3 builds the input and gates the answer, L2 appends
the version. A day that fails both attempts stops the chain (later days build on it) and
is picked up again on the next run. The monthly rebuild that follows lives in
:mod:`jarvis.runtime.core_memory_rebuild`; the once-a-day check-in on a quiet thread is
:func:`checkin_line` (ADR 0389).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.core_memory import build_core_memory_messages, check_core_memory, day_labels
from jarvis.decision.core_memory_review import ReviewFailed, review_changes
from jarvis.decision.cost_guard import CostRecorder
from jarvis.state import core_memory, places
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_nightly_core_memory,
    checkin_once,
    core_memory_pending_days,
    current_core_memory,
    day_records,
    latest_day_summaries,
    open_memory_db,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, tzinfo
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.decision.surrogate_route import SurrogateRoute

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "core_memory"
_DEFAULT_MAX_CHARS: Final[int] = 40000
_DEFAULT_MAX_STALE: Final[int] = 5
DEFAULT_REVIEW_MODEL: Final[str] = "typesafe/jev-1.13"
_DEFAULT_REVIEW_TIMEOUT_MS: Final[int] = 10_000
QUIET_DAYS: Final[int] = 21  # ADR 0389: an open thread this long without evidence is asked about
CHECKIN_LINE_PREFIX: Final[str] = "[Thread]"
CHECKIN_LINE: Final[str] = (
    CHECKIN_LINE_PREFIX + " A thread of his life has had nothing new for {days} days (since "
    "{since}). First look in his data (mail, calendar, his things) for where it stands. Only if "
    "nothing answers, and the moment suits, ask him once, in passing, whether it is still going. "
    "This note is not shown again. The thread: “{text}”"
)
CHECKIN_LINE_CHARS: Final[int] = 800  # the note and an item of up to 400 characters, whole


@dataclass(frozen=True)
class RebuildSettings:
    """``core_memory.rebuild``: the monthly rebuild's preset and prompt (ADR 0389)."""

    preset: str
    prompt: str

    @classmethod
    def from_config(cls, raw: object) -> RebuildSettings | None:
        """The settings, or None (no monthly rebuild) when the block, preset or prompt is absent."""
        if not isinstance(raw, Mapping):
            return None
        preset, prompt = raw.get("preset"), raw.get("prompt")
        if not (isinstance(preset, str) and preset and isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("core_memory.rebuild needs preset and prompt; no monthly rebuild runs")
            return None
        return cls(preset=preset, prompt=prompt.strip())


def section_caps(raw: object) -> dict[str, int]:
    """Each section's cap: ``section_caps:`` of the ``core_memory:`` block over the defaults."""
    given = raw.get("section_caps") if isinstance(raw, Mapping) else None
    caps = dict(core_memory.SECTION_CAPS)
    if isinstance(given, Mapping):
        for name, cap in given.items():
            if name in caps and isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                caps[name] = cap
    return caps


@dataclass(frozen=True)
class CoreMemorySettings:
    """The ``core_memory:`` block of ``config/jarvis.yaml``."""

    preset: str
    prompt: str
    max_chars: int = _DEFAULT_MAX_CHARS
    section_caps: Mapping[str, int] = field(default_factory=lambda: dict(core_memory.SECTION_CAPS))
    max_stale: int = _DEFAULT_MAX_STALE
    review_min_confidence: float | None = None  # None = no Jev review
    review_drop: bool = False  # False: Jev's verdicts are only logged, every change lands
    review_model: str = DEFAULT_REVIEW_MODEL
    review_timeout_ms: int = _DEFAULT_REVIEW_TIMEOUT_MS
    rebuild: RebuildSettings | None = None  # None = no monthly rebuild

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
            section_caps=section_caps(raw),
            max_stale=stale if isinstance(stale, int) and stale >= 0 else _DEFAULT_MAX_STALE,
            review_min_confidence=(
                float(bar)
                if isinstance(bar, int | float) and not isinstance(bar, bool) and 0 < bar <= 1
                else None
            ),
            review_drop=raw.get("review_drop") is True,
            review_model=model.strip()
            if isinstance(model, str) and model.strip()
            else DEFAULT_REVIEW_MODEL,
            review_timeout_ms=(
                timeout
                if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0
                else _DEFAULT_REVIEW_TIMEOUT_MS
            ),
            rebuild=RebuildSettings.from_config(raw.get("rebuild")),
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
    still appends a version, because that is how ``upto_day`` advances. With
    ``settings.review_min_confidence`` Jev reads each add and rewrite first and its verdicts go
    in the version's changes. With ``review_drop`` a change it does not keep is dropped, and a
    review that fails rejects the day; without, every change lands and a failed review is only
    a warning.
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
            places=places.names_block(memory.db_path),
            section_caps=settings.section_caps,
        ),
        system=settings.prompt,
        tools=None,
        tool_choice=None,
        kind=KIND,
        turn_id=None,
    )
    changes, notes, reason = check_core_memory(
        result.text,
        result.finish_reason,
        doc=base.doc,
        labels=day_labels(records),
        max_stale=settings.max_stale,
        max_chars=settings.max_chars,
        section_caps=settings.section_caps,
        day=day,
        since=base.upto_day,
    )
    if changes is None or notes is None:
        LOGGER.warning("core_memory: %s rejected (%s); nothing stored", day, reason)
        return f"rejected ({reason})"
    review_log: list[dict[str, Any]] = []
    if settings.review_min_confidence is not None:
        try:
            if review is None:
                msg = "no Jev route"
                raise ReviewFailed(msg)  # noqa: TRY301 - one exit for every review failure
            changes, review_log = review_changes(
                changes,
                doc=base.doc,
                records=records,
                day=day,
                route=review,
                min_confidence=settings.review_min_confidence,
                drop=settings.review_drop,
            )
        except ReviewFailed as exc:
            if settings.review_drop:  # an unreviewed item is never written
                LOGGER.warning("core_memory: %s rejected (review: %s); nothing stored", day, exc)
                return f"rejected (review: {exc})"
            LOGGER.warning("core_memory: %s review failed (%s); landing unreviewed", day, exc)
    if (
        append_nightly_core_memory(
            memory.db_path,
            base_id=base.id,
            day=day,
            changes=changes,
            review_log=review_log,
            notes=notes,
        )
        is None
    ):
        LOGGER.warning("core_memory: %s rejected (core memory changed during the call)", day)
        return "rejected (core memory changed during the call)"
    return f"landed ({len(changes)} changes, {len(review_log)} review entries)"


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


def checkin_line(memory_db: Path, zone: tzinfo, clock: Callable[[], datetime]) -> str | None:
    """ADR 0389: at most once a day, the open thread with no evidence for :data:`QUIET_DAYS`.

    A live-context line for her to raise when it fits, after looking in his data first; the
    store records it as shown, so the same thread comes again only after new evidence.
    """
    today = clock().astimezone(zone).date()
    item = checkin_once(memory_db, today, QUIET_DAYS)
    if item is None:
        return None
    since = str(core_memory.evidence_day(item) or "")
    days = (today - date.fromisoformat(since)).days if since else QUIET_DAYS
    return CHECKIN_LINE.format(days=days, since=since or "?", text=item["text"])


__all__ = [
    "CHECKIN_LINE_CHARS",
    "CHECKIN_LINE_PREFIX",
    "CoreMemorySettings",
    "RebuildSettings",
    "checkin_line",
    "consolidate_day",
    "run_core_memory",
    "section_caps",
]
