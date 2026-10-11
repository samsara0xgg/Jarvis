"""Runtime wiring for the ledger (ADR 0201): the prompt blocks and the nightly "The day" line.

:class:`LedgerContext` is the callable ``render_context`` asks for the day, week and job-hunt
blocks. The standing blocks are one text per local day, cached here; the per-turn blocks are
recomputed each call. It never raises: a ledger that fails costs the turn its blocks, not the
turn. :func:`run_day_prose` is the one model call of the ledger, run from the nightly schedule; it
also writes the day's line and highlights (ADR 0235), which :class:`DayDrafts` drafts through the
day.
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
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.day_prose import (
    DayAnswer,
    build_day_draft_messages,
    build_day_prose_messages,
    day_items_text,
    parse_day_answer,
)
from jarvis.decision.llm import failure_reason
from jarvis.shared import lang
from jarvis.state.day_line import DayLine, DaySources, day_window, fold_day
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
    append_day_draft,
    append_day_prose,
    last_draft_at,
    latest_day_summaries,
    open_memory_db,
    prose_days,
)
from jarvis.state.picture_notes import last_words

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision.llm import ChatResult, LLMClient

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "day_prose"
DRAFT_KIND: Final[str] = "day_draft"
DRAFT_EVERY: Final[timedelta] = timedelta(minutes=30)
"""A draft of today's line waits this long after the last one (ADR 0235)."""
_DEFAULT_PRESET: Final[str] = "gpt6-luna-flex"
_DEFAULT_DAYS: Final[int] = 7
_DEFAULT_MAX_CHARS: Final[int] = 800
_ATTEMPTS: Final[int] = 3
_BACKOFF_S: Final[tuple[float, ...]] = (2.0, 8.0)
_RETRY_S: Final[float] = 60.0
_KEEP_WARM_S: Final[float] = 60.0
_TRANSIENT: Final[frozenset[str]] = frozenset({"rate_limited", "network", "timeout"})


def _text(value: object) -> str | None:
    """``value`` stripped when it is a non-blank string, else None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class ProseSettings:
    """The ``ledger.prose:`` block."""

    preset: str
    prompt: str
    days: int = _DEFAULT_DAYS
    max_chars: int = _DEFAULT_MAX_CHARS
    draft_prompt: str | None = None  # none: no drafts of today's line (ADR 0235)
    reply_language: str = "follow"  # the top-level setting: the language the line is written in


@dataclass(frozen=True)
class LedgerSettings:
    """The ``ledger:`` block of ``config/jarvis.yaml``."""

    full_days: int = 7
    compact_days: int = 7
    prose: ProseSettings | None = None

    @classmethod
    def from_config(cls, raw: object, reply_language: str = "follow") -> LedgerSettings | None:
        """The settings, or None (ledger off) when the block is absent or not enabled.

        ``reply_language`` is the top-level setting, which the line of a day is written in.
        """
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
            draft = block.get("draft_prompt")
            if isinstance(prompt, str) and prompt.strip():
                prose = ProseSettings(
                    preset=preset if isinstance(preset, str) and preset else _DEFAULT_PRESET,
                    prompt=prompt.strip(),
                    days=_count(block.get("days"), _DEFAULT_DAYS),
                    max_chars=_count(block.get("max_chars"), _DEFAULT_MAX_CHARS),
                    draft_prompt=_text(draft),
                    reply_language=reply_language,
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
        """Keep the standing text current in the background, so no turn builds it.

        The text is a 38-day read that costs seconds on a Pi, and it is rebuilt when the day turns,
        a note lands, a late phone batch arrives or the terminal answers after being away. Checked
        once a minute, as :meth:`_standing` checks it per turn (cheap when nothing changed), the
        first turn of the day finds it already built.
        """
        def keep() -> None:
            while True:
                try:
                    self._standing(now())
                except Exception:  # the next minute tries again; a turn still builds it if needed
                    LOGGER.exception("ledger: keeping the standing text warm failed")
                _time.sleep(_KEEP_WARM_S)

        threading.Thread(target=keep, name="jarvis-ledger-warm", daemon=True).start()

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


def _read(
    result: ChatResult, *, max_chars: int, starts: Mapping[str, int], with_prose: bool,
) -> DayAnswer | str:
    """:func:`parse_day_answer` of a model's result."""
    return parse_day_answer(
        result.text, result.finish_reason, max_chars, starts, with_prose=with_prose,
    )


def _ask(  # noqa: PLR0913 — the day, its input, the prompt, the gate, the client and two seams.
    day: str, messages: list[dict[str, Any]], system: str,
    read: Callable[[ChatResult], DayAnswer | str], client: LLMClient,
    cost_recorder: CostRecorder, sleep: Callable[[float], None], kind: str,
) -> tuple[DayAnswer | None, str]:
    """``(answer, "")`` for ``day`` once an answer passes the gate ``read``, else ``(None, why)``.

    A transient error is asked again (the last one raises); a rejected answer once.
    """
    reason = ""
    for _round in range(2):
        for attempt in range(_ATTEMPTS):
            try:
                result = cost_recorder.chat(
                    client, messages=messages, system=system, tools=None,
                    tool_choice=None, kind=kind, turn_id=None,
                )
                break
            except Exception as exc:
                if attempt == _ATTEMPTS - 1 or not _transient(exc):
                    raise
                LOGGER.warning("%s: %s try %d failed (%s); retrying", kind, day, attempt + 1, exc)
                sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
        found = read(result)
        if isinstance(found, DayAnswer):
            return found, ""
        reason = found
        LOGGER.warning("%s: %s rejected (%s)", kind, day, found)
    return None, reason


def fold_local_day(
    conn: sqlite3.Connection, sources: LedgerSources, day: date, now: datetime,
) -> DayLine:
    """The day line of ``day`` as of ``now``, folded as ``GET /inherent/day`` folds it."""
    start_ms, end_ms = day_window(day, sources.zone)
    day_sources = DaySources(
        sources.zone, sources.timesink, sources.memory_db, sources.terminal, sources.attachments,
    )
    return fold_day(conn, start_ms, end_ms, int(now.timestamp() * 1000), day_sources)


def line_language(sources: LedgerSources, pinned: str) -> str:
    """The English name of the language the line is written in: the pinned one, else his words'."""
    return lang.language_name(lang.reply_language(last_words(sources.memory_db, None), pinned))


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
    """Write "The day", its line and its highlights for each of the last complete days without.

    The last ``settings.days`` days, oldest first; an empty day is skipped, and a day with only
    drafts (ADR 0235) still has none. Blocking (one LLM call per day). A failed or rejected day
    stores nothing, is logged and does not stop the next one. A line or highlight that fails its
    gates is dropped whole and the prose is stored without it.
    """
    outcomes: dict[str, str] = {}
    done = prose_days(sources.memory_db)
    now = datetime.now(sources.zone)
    with closing(open_runtime_event_log(event_log_path)) as conn:
        cost_recorder = CostRecorder(conn, pricing_table=pricing_table)
        language = line_language(sources, settings.reply_language)
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
                items = fold_local_day(conn, sources, day, now).items
                starts = {str(item["id"]): int(item["start_ms"]) for item in items}
                messages = build_day_prose_messages(
                    key, numbers, found[0][1] if found else None, day_report(sources, day, now),
                    day_items_text(items, sources.zone), language,
                )
                answer, why = _ask(
                    key, messages, settings.prompt,
                    partial(_read, max_chars=settings.max_chars, starts=starts, with_prose=True),
                    client, cost_recorder, sleep, KIND,
                )
                if answer is None:
                    outcomes[key] = f"rejected ({why})"
                    continue
                text = answer.prose or ""
                append_day_prose(
                    sources.memory_db, day=key, text=text, model=client.model,
                    input_chars=len(str(messages[0]["content"])), output_chars=len(text),
                    line=answer.line, highlights=answer.highlights,
                )
                dropped = "" if answer.line else f"; line dropped: {answer.dropped}"
                outcomes[key] = f"landed ({len(text)} chars{dropped})"
            except Exception:
                LOGGER.exception("day_prose: %s failed; nothing stored", key)
                outcomes[key] = "failed"
    return outcomes


class DayDrafts:
    """Today's line and highlights, drafted again when the day line has changed (ADR 0235).

    One draft at most every :data:`DRAFT_EVERY`, and only when the items differ from those of the
    last draft in their ids, states or end times. A draft that fails or is rejected leaves the
    previous one in place and is not tried again until the interval has passed. :meth:`tick` takes
    the clock as an argument and blocks on the model, so the schedule runs it in a thread.
    """

    def __init__(  # noqa: PLR0913 — the sources, the knobs, the client and the accounting seam.
        self,
        sources: LedgerSources,
        settings: ProseSettings,
        client: LLMClient,
        *,
        event_log_path: Path,
        pricing_table: Mapping[str, Mapping[str, float]],
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        """Bind the sources, the knobs and the client; nothing is read until the first tick."""
        self._sources = sources
        self._settings = settings
        self._client = client
        self._event_log_path = event_log_path
        self._pricing_table = pricing_table
        self._sleep = sleep
        self._day: date | None = None
        self._seen: tuple[tuple[object, ...], ...] | None = None  # the last landed draft's items
        self._tried: datetime | None = None  # the last draft attempt of this day, landed or not

    def tick(self, now: datetime) -> str:
        """Draft today's line if it is due; what happened, in one phrase.

        ``waiting`` (the last draft is under 30 minutes old), ``no items``, ``unchanged``, or the
        outcome of a draft: ``landed (...)``, ``rejected (...)`` or ``failed``.
        """
        if self._settings.draft_prompt is None:
            return "off"
        sources = self._sources
        day = now.astimezone(sources.zone).date()
        key = day.isoformat()
        if day != self._day:  # a new day starts with no draft
            self._day, self._seen, self._tried = day, None, None
        last = self._tried if self._tried is not None else last_draft_at(sources.memory_db, key)
        if last is not None and now - last < DRAFT_EVERY:
            return "waiting"
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            items = fold_local_day(conn, sources, day, now).items
            if not items:
                return "no items"
            seen = tuple((item["id"], item["state"], item["end_ms"]) for item in items)
            if seen == self._seen:
                return "unchanged"
            self._tried = now
            landed, outcome = self._draft(conn, key, now, items)
        if landed:
            self._seen = seen
        return outcome

    def _draft(
        self, conn: sqlite3.Connection, key: str, now: datetime, items: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Ask for the draft of ``key`` and store it: ``(landed, outcome)``."""
        sources, settings = self._sources, self._settings
        starts = {str(item["id"]): int(item["start_ms"]) for item in items}
        messages = build_day_draft_messages(
            key, now.astimezone(sources.zone).strftime("%H:%M"),
            day_items_text(items, sources.zone), line_language(sources, settings.reply_language),
        )
        try:
            answer, why = _ask(
                key, messages, settings.draft_prompt or "",
                partial(_read, max_chars=settings.max_chars, starts=starts, with_prose=False),
                self._client, CostRecorder(conn, pricing_table=self._pricing_table),
                self._sleep, DRAFT_KIND,
            )
        except Exception:
            LOGGER.exception("day_draft: %s failed; the previous draft stands", key)
            return False, "failed"
        if answer is None or answer.line is None:
            return False, f"rejected ({why})"
        append_day_draft(
            sources.memory_db, day=key, line=answer.line, highlights=answer.highlights,
            model=self._client.model, input_chars=len(str(messages[0]["content"])),
            output_chars=len(answer.line),
        )
        return True, f"landed ({len(answer.highlights)} highlights)"


__all__ = [
    "DRAFT_EVERY",
    "DayDrafts",
    "LedgerContext",
    "LedgerSettings",
    "ProseSettings",
    "fold_local_day",
    "line_language",
    "run_day_prose",
]
