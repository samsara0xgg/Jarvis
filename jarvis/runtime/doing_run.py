"""Runtime wiring for the writer of the doing lane (ADR 0302).

For a day, the writer folds the day line, cuts the waking window into segments, gathers the clues
and asks one model for what he was doing, on a dedicated client pinned to ``doing.preset``. L3
builds the input and gates the answer; L2 appends the run. A run that fails or is gated out
stores nothing and the run before it stays.

:class:`DoingSchedule` looks at today and the last few days every quarter of an hour, in a thread
off the loop, and asks the model only when the inputs changed:

- today: a draft, at most once an hour;
- a day that is over (it is a day before today, or he has gone to bed): one final run, and
  another when late rows change its inputs.

The inputs are told apart by a digest of the facts and clues (:func:`inputs_digest`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.doing_run import Brief, build_messages, inputs_digest, read_run
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.shared import lang
from jarvis.state.day_line import DayLine, DaySources, day_window, fold_day, shipped_conn
from jarvis.state.doing import cut, fact_spans, his_facts, waking_window
from jarvis.state.doing_clues import HIS_WORDS, gather
from jarvis.state.doing_signals import gather as gather_signals
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import append_doing_run, latest_doing_run, local_now, open_memory_db
from jarvis.state.picture_notes import last_words

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.state.doing_clues import Clue

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "doing_run"
DEFAULT_PRESET: Final[str] = "haiku-bg"
DRAFT_EVERY: Final[timedelta] = timedelta(hours=1)
"""A day is written at most this often, however often its inputs change."""
CHECK_EVERY: Final[timedelta] = timedelta(minutes=15)
"""How often a day is folded to see whether its inputs changed."""
RECENT_DAYS: Final[int] = 3
"""The days before today that are written when their inputs change."""
_ATTEMPTS: Final[int] = 2
"""An answer that fails a gate is asked for once more, as the day summaries are."""
_QUIET: Final[frozenset[str]] = frozenset(
    {"unchanged", "waiting", "no waking window", "no inputs"},
)


@dataclass(frozen=True)
class DoingSettings:
    """The ``doing:`` block of ``config/jarvis.yaml``."""

    preset: str
    prompt: str

    @classmethod
    def from_config(cls, raw: object) -> DoingSettings | None:
        """The settings, or None (feature off) unless ``enabled`` is true and a prompt is there."""
        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
            return None
        preset, prompt = raw.get("preset"), raw.get("prompt")
        if not (isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("doing needs a prompt; what he was doing is not written")
            return None
        return cls(
            preset=preset if isinstance(preset, str) and preset.strip() else DEFAULT_PRESET,
            prompt=prompt.strip(),
        )


def _screen_reach(line: DayLine, sources: DaySources) -> dict[str, int] | None:
    """How far his phone and tablet use is known: None when none of it reaches the brain."""
    conn = shipped_conn(sources)
    if conn is None:
        return None
    conn.close()
    return None if "screen" in line.missing else line.synced


def _title_language(clues: Sequence[Clue], memory_db: Path | None) -> str:
    """The English name of the language of his words that day, else of his latest words."""
    heard = " ".join(c.text for c in clues if c.kind == "words" and c.text.startswith(HIS_WORDS))
    if not heard and memory_db is not None:
        heard = last_words(memory_db, None)
    return lang.language_name(lang.reply_language(heard))


def read_brief(
    day: date, sources: DaySources, event_log_path: Path, now: datetime,
) -> Brief | None:
    """What the writer is shown of ``day`` as of ``now``, or None when it has no waking window.

    The day line is folded as ``GET /inherent/day`` folds it and cut as it is there, so the
    spans written are over the segments the page shows. The day is ``final`` once the waking part
    of it is over.
    """
    zone = sources.zone
    start_ms, end_ms = day_window(day, zone)
    now_ms = int(now.timestamp() * 1000)
    with closing(open_runtime_event_log(event_log_path)) as conn:
        line = fold_day(conn, start_ms, end_ms, now_ms, sources)
        items = line.items
        facts = his_facts(fact_spans(items))
        waking = waking_window(facts, start_ms, end_ms, now_ms)
        if waking.end_ms <= waking.start_ms:
            return None
        clues = gather(conn, sources.memory_db, day, items, (waking.start_ms, waking.end_ms), zone)
        segments = cut(facts, waking)
        signals = gather_signals(conn, sources, segments)
    return Brief(
        day=day.isoformat(), weekday=f"{day:%A}", zone=zone, now_ms=now_ms,
        final=waking.end_ms < now_ms, language=_title_language(clues, sources.memory_db),
        waking=waking, segments=segments, facts=facts, items=items, clues=clues, signals=signals,
        screen=_screen_reach(line, sources),
    )


class Composed(NamedTuple):
    """What the model answered for a day: the gated spans, or why none may be stored."""

    spans: list[dict[str, Any]] | None
    why: str
    model: str | None
    tokens_in: int
    tokens_out: int


def compose_run(
    brief: Brief, settings: DoingSettings, client: LLMClient, cost_recorder: CostRecorder,
) -> Composed:
    """Ask the model for ``brief``'s day and gate the answer; a rejected one is asked once more.

    Blocking. A call that raises propagates, with nothing stored.
    """
    messages = build_messages(brief)
    why, model, spent_in, spent_out = "", None, 0, 0
    for _attempt in range(_ATTEMPTS):
        result = cost_recorder.chat(
            client, messages=messages, system=settings.prompt, tools=None, tool_choice=None,
            kind=KIND, turn_id=None,
        )
        model = result.model_used or client.model
        spent_in, spent_out = spent_in + result.tokens_in, spent_out + result.tokens_out
        found = read_run(result.text, result.finish_reason, brief)
        if not isinstance(found, str):
            return Composed(found, "", model, spent_in, spent_out)
        why = found
        LOGGER.warning("doing: %s rejected (%s)", brief.day, why)
    return Composed(None, why, model, spent_in, spent_out)


def store_run(memory_db: Path, brief: Brief, composed: Composed) -> str:
    """Append the gated spans as the day's newest run; the run id.

    A run under way is a draft; it covers the waking window up to where it ends.
    """
    if composed.spans is None:
        msg = "a rejected answer is not stored"
        raise ValueError(msg)
    return append_doing_run(
        memory_db, day=brief.day, draft=not brief.final, covers_until_ms=brief.waking.end_ms,
        model=composed.model, inputs_digest=inputs_digest(brief), spans=composed.spans,
    )


class DoingSchedule:
    """Folds today and the last days every ``poll_s``; writes a day when its inputs changed."""

    def __init__(  # noqa: PLR0913 — the stores, the knobs, the accounting seam and the poll.
        self,
        *,
        settings: DoingSettings,
        sources: DaySources,
        memory_db: Path,
        event_log_path: Path,
        llm_config: Mapping[str, Any],
        pricing_table: Mapping[str, Mapping[str, float]],
        poll_s: float = 300.0,
        client: LLMClient | None = None,
    ) -> None:
        """Bind the stores and the knobs; nothing runs until :meth:`run`.

        ``client`` stands in for the one built from ``settings.preset`` on first use.
        """
        self._settings = settings
        self._sources = sources
        self._memory_db = memory_db
        self._event_log_path = event_log_path
        self._llm_config = llm_config
        self._pricing_table = pricing_table
        self._poll_s = poll_s
        self._client = client
        self._looked: dict[str, datetime] = {}

    def tick(self, now: datetime) -> dict[str, str]:
        """Look at each day not looked at for :data:`CHECK_EVERY`, oldest first; what was done.

        Blocking (a fold per day and at most one model call per day). A day that fails is logged
        and the next one still runs; one that has nothing to say is left out of the result.
        """
        today = now.astimezone(self._sources.zone).date()
        outcomes: dict[str, str] = {}
        for ago in range(RECENT_DAYS, -1, -1):
            day = today - timedelta(days=ago)
            key = day.isoformat()
            looked = self._looked.get(key)
            if looked is not None and now - looked < CHECK_EVERY:
                continue
            self._looked[key] = now
            try:
                outcome = self._day(day, now)
            except Exception:
                LOGGER.exception("doing: %s failed; the previous run stands", key)
                outcome = "failed"
            if outcome == "failed" or outcome.startswith("rejected"):
                # A call that did not land is not made again for an hour, as a draft would not be.
                self._looked[key] = now + DRAFT_EVERY - CHECK_EVERY
            if outcome not in _QUIET:
                outcomes[key] = outcome
        return outcomes

    def _day(self, day: date, now: datetime) -> str:
        brief = read_brief(day, self._sources, self._event_log_path, now)
        if brief is None:
            return "no waking window"
        if not brief.facts and not brief.clues:
            return "no inputs"
        with closing(open_memory_db(self._memory_db)) as conn:
            latest = latest_doing_run(conn, brief.day)
        if latest is not None:
            # A draft whose inputs did not change still becomes the final run once the day is over.
            if latest.inputs_digest == inputs_digest(brief) and not (brief.final and latest.draft):
                return "unchanged"
            if now - datetime.fromisoformat(latest.written_at).astimezone() < DRAFT_EVERY:
                return "waiting"
        if self._client is None:
            self._client = build_compact_client(self._llm_config, self._settings.preset)
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing_table)
            composed = compose_run(brief, self._settings, self._client, recorder)
        if composed.spans is None:
            return f"rejected ({composed.why})"
        store_run(self._memory_db, brief, composed)
        asked = sum(span["question"] is not None for span in composed.spans)
        kind = "final" if brief.final else "draft"
        return f"landed ({kind}, {len(composed.spans)} spans, {asked} questions)"

    async def run(self) -> None:
        """The loop; a failed pass is logged and the next one tries again."""
        while True:
            await asyncio.sleep(self._poll_s)
            try:
                outcomes = await asyncio.to_thread(self.tick, local_now())
            except Exception:
                LOGGER.exception("doing: the check failed")
            else:
                if outcomes:
                    LOGGER.info("doing: %s", outcomes)


__all__ = [
    "CHECK_EVERY",
    "DEFAULT_PRESET",
    "DRAFT_EVERY",
    "KIND",
    "RECENT_DAYS",
    "Composed",
    "DoingSchedule",
    "DoingSettings",
    "compose_run",
    "read_brief",
    "store_run",
]
