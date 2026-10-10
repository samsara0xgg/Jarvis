"""Runtime wiring for the ledger (ADR 0201): the prompt blocks and the nightly "The day" line.

:class:`LedgerContext` is the callable ``render_context`` asks for the day, week and job-hunt
blocks. The standing blocks are one text per local day, cached here; the per-turn blocks are
recomputed each call. It never raises: a ledger that fails costs the turn its blocks, not the
turn. :func:`run_day_prose` is the one model call of the ledger, run from the nightly schedule.
"""

from __future__ import annotations

import logging
import math
import threading
import time as _time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.day_prose import build_day_prose_messages, check_day_prose
from jarvis.decision.llm import failure_reason
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.ledger import (
    DAY_STARTS_AT,
    LedgerSources,
    day_active,
    day_numbers_text,
    day_report,
    job_hunt_text,
    notes_stamp,
    phone_stamp,
    since_text,
    standing_text,
    today_text,
)
from jarvis.state.memory_db import (
    append_day_prose,
    latest_day_summaries,
    open_memory_db,
    prose_days,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "day_prose"
_DEFAULT_PRESET: Final[str] = "gpt6-luna-flex"
_DEFAULT_DAYS: Final[int] = 7
_DEFAULT_MAX_CHARS: Final[int] = 800
_ATTEMPTS: Final[int] = 3
_BACKOFF_S: Final[tuple[float, ...]] = (2.0, 8.0)
_RETRY_S: Final[float] = 60.0
_TRANSIENT: Final[frozenset[str]] = frozenset({"rate_limited", "network", "timeout"})


@dataclass(frozen=True)
class ProseSettings:
    """The ``ledger.prose:`` block."""

    preset: str
    prompt: str
    days: int = _DEFAULT_DAYS
    max_chars: int = _DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class LedgerSettings:
    """The ``ledger:`` block of ``config/jarvis.yaml``."""

    full_days: int = 7
    compact_days: int = 7
    prose: ProseSettings | None = None

    @classmethod
    def from_config(cls, raw: object) -> LedgerSettings | None:
        """The settings, or None (ledger off) when the block is absent or not enabled."""
        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
            return None

        def _count(value: object, default: int) -> int:
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
            return default

        block = raw.get("prose")
        prose: ProseSettings | None = None
        if isinstance(block, Mapping):
            prompt, preset = block.get("prompt"), block.get("preset")
            if isinstance(prompt, str) and prompt.strip():
                prose = ProseSettings(
                    preset=preset if isinstance(preset, str) and preset else _DEFAULT_PRESET,
                    prompt=prompt.strip(),
                    days=_count(block.get("days"), _DEFAULT_DAYS),
                    max_chars=_count(block.get("max_chars"), _DEFAULT_MAX_CHARS),
                )
            else:
                LOGGER.warning("ledger.prose needs a prompt; no day prose is written")
        return cls(_count(raw.get("full_days"), 7), _count(raw.get("compact_days"), 7), prose)


class LedgerContext:
    """The ``memory_db.Ledger`` callable: ``(now, last_ts) -> (standing, per_turn)``."""

    def __init__(self, sources: LedgerSources, settings: LedgerSettings) -> None:
        """Bind the sources and the knobs; nothing is read until the first call."""
        self.sources = sources
        self.settings = settings
        self._lock = threading.Lock()
        self._cached: tuple[object, str, float] | None = None  # key, text, trust it until

    def __call__(self, now: datetime, last_ts: datetime | None) -> tuple[str, str]:
        """The text that follows the core memory and the text that follows the time line."""
        return self._standing(now), self._per_turn(now, last_ts)

    def warm(self, now: Callable[[], datetime]) -> None:
        """Build today's standing text in the background, so the first turn finds it cached."""
        threading.Thread(
            target=lambda: self._standing(now()), name="jarvis-ledger-warm", daemon=True,
        ).start()

    def _standing(self, now: datetime) -> str:
        zone = self.sources.zone
        today = now.astimezone(zone).date()
        midnight = datetime.combine(today, time.min, zone)
        # Yesterday's working day may run past midnight: its stop is known once the day starts.
        day_start = datetime.combine(today, DAY_STARTS_AT, zone)
        day_end = day_start if now >= day_start else midnight
        try:
            with self._lock:  # the warm thread and the turn share one computation
                key = (
                    today, day_end, notes_stamp(self.sources),
                    phone_stamp(
                        self.sources, midnight,
                        days=self.settings.full_days + self.settings.compact_days,
                    ),
                )
                if (
                    self._cached is None or self._cached[0] != key
                    or _time.monotonic() > self._cached[2]
                ):
                    terminal = self.sources.terminal
                    missed = 0 if terminal is None else terminal.misses
                    text = standing_text(
                        self.sources, midnight,
                        full_days=self.settings.full_days,
                        compact_days=self.settings.compact_days, notes_as_of=now,
                        day_end_as_of=day_end,
                    )
                    # a text built while the terminal was away is built again, once a minute,
                    # until it answers
                    away = terminal is not None and terminal.misses != missed
                    self._cached = (key, text, _time.monotonic() + (_RETRY_S if away else math.inf))
                return self._cached[1]
        except Exception:
            LOGGER.exception("ledger: the standing blocks failed; the turn goes without them")
            return ""

    def _per_turn(self, now: datetime, last_ts: datetime | None) -> str:
        parts = [
            self._guarded(partial(job_hunt_text, self.sources, now)),
            self._guarded(partial(today_text, self.sources, now)),
        ]
        if last_ts is not None:
            parts.append(self._guarded(partial(since_text, self.sources, now, last_ts)))
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _guarded(block: Callable[[], str]) -> str:
        try:
            return block()
        except Exception:
            LOGGER.exception("ledger: a per-turn block failed; the turn goes without it")
            return ""


def _transient(exc: BaseException) -> bool:
    """A rate limit, timeout, dropped connection or 5xx: worth asking again."""
    if failure_reason(exc) in _TRANSIENT:
        return True
    seen: BaseException | None = exc
    while seen is not None:
        status = getattr(seen, "status_code", None)
        if isinstance(status, int) and status >= 500:  # noqa: PLR2004 — HTTP server errors
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _ask(  # noqa: PLR0913 — the day, its input, the knobs, the client and two seams.
    day: str, messages: list[dict[str, str]], settings: ProseSettings, client: LLMClient,
    cost_recorder: CostRecorder, sleep: Callable[[float], None],
) -> tuple[str | None, str]:
    """``(text, "")`` for ``day`` once an answer passes the gate, else ``(None, why)``.

    A transient error is asked again (the last one raises); a rejected answer once.
    """
    reason = ""
    for _round in range(2):
        for attempt in range(_ATTEMPTS):
            try:
                result = cost_recorder.chat(
                    client, messages=messages, system=settings.prompt, tools=None,
                    tool_choice=None, kind=KIND, turn_id=None,
                )
                break
            except Exception as exc:
                if attempt == _ATTEMPTS - 1 or not _transient(exc):
                    raise
                LOGGER.warning("day_prose: %s try %d failed (%s); retrying", day, attempt + 1, exc)
                sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
        found = check_day_prose(result.text, result.finish_reason, settings.max_chars)
        if found is None:
            return (result.text or "").strip(), ""
        reason = found
        LOGGER.warning("day_prose: %s rejected (%s)", day, found)
    return None, reason


def run_day_prose(  # noqa: PLR0913 — the sources, the knobs, the client and the accounting seam.
    sources: LedgerSources,
    settings: ProseSettings,
    client: LLMClient,
    *,
    event_log_path: Path,
    pricing_table: Mapping[str, Mapping[str, float]],
    today: date,
    sleep: Callable[[float], None] = _time.sleep,
) -> dict[str, str]:
    """Write "The day" for each of the last ``settings.days`` complete days that has none.

    Oldest first; an empty day is skipped. Blocking (one LLM call per day). A failed or rejected
    day stores nothing, is logged and does not stop the next one.
    """
    outcomes: dict[str, str] = {}
    done = prose_days(sources.memory_db)
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        for ago in range(settings.days, 0, -1):
            day = today - timedelta(days=ago)
            key = day.isoformat()
            if key in done:
                continue
            try:
                nxt = datetime.combine(day + timedelta(days=1), time.min, sources.zone)
                if not day_active(sources, day, nxt):
                    outcomes[key] = "no activity"
                    continue
                with closing(open_memory_db(sources.memory_db)) as db:
                    found = latest_day_summaries(db, key, key)
                numbers = day_numbers_text(sources, day, nxt)
                messages = build_day_prose_messages(
                    key, numbers, found[0][1] if found else None,
                    day_report(sources, day, datetime.now(sources.zone)),
                )
                text, why = _ask(key, messages, settings, client, cost_recorder, sleep)
                if text is None:
                    outcomes[key] = f"rejected ({why})"
                    continue
                append_day_prose(
                    sources.memory_db, day=key, text=text, model=client.model,
                    input_chars=len(str(messages[0]["content"])), output_chars=len(text),
                )
                outcomes[key] = f"landed ({len(text)} chars)"
            except Exception:
                LOGGER.exception("day_prose: %s failed; nothing stored", key)
                outcomes[key] = "failed"
    return outcomes


__all__ = [
    "LedgerContext",
    "LedgerSettings",
    "ProseSettings",
    "run_day_prose",
]
