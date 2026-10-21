"""Runtime wiring for the writer of the doing lane (ADR 0302).

For a day, the writer folds the day line, cuts the waking window into segments, gathers the clues
and asks one model for what he was doing, on a dedicated client pinned to ``doing.preset``. L3
builds the input and gates the answer; L2 appends the run. A run that fails or is gated out
stores nothing and the run before it stays.

:class:`DoingSchedule` looks at today and the last few days every quarter of an hour, in a thread
off the loop, and asks the model only when the inputs changed (ADR 0310, ADR 0311):

- today: a run when he asks (:meth:`DoingSchedule.tidy`), and with ``doing.clock_drafts`` one at
  most every two hours as well; after the first, each writes only the time from where the last
  written span starts and keeps the spans before it (:func:`narrow`);
- a day that is over (it is a day before today, or he has gone to bed): one final run of the
  whole day, and another when late rows change its inputs.

The inputs are told apart by a digest of the facts and clues (:func:`inputs_digest`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.decision.attention_threads import (
    ThreadBrief,
    known_threads,
    read_threads,
    untold,
)
from jarvis.decision.attention_threads import (
    build_messages as build_thread_messages,
)
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.doing_run import (
    Brief,
    asked_again,
    build_messages,
    inputs_digest,
    narrow,
    read_run,
)
from jarvis.runtime.doing_day import lane_line, reach_past_start
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.shared import lang
from jarvis.state import attention
from jarvis.state.day_line import DayLine, DaySources, day_window, fold_day, shipped_conn
from jarvis.state.doing import cut, fact_spans, his_facts, waking_window
from jarvis.state.doing_clues import HIS_WORDS, gather
from jarvis.state.doing_signals import foreground
from jarvis.state.doing_signals import gather as gather_signals
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import (
    append_attention_threads,
    append_doing_run,
    attention_threads,
    latest_doing_run,
    local_now,
    open_memory_db,
)
from jarvis.state.picture_notes import last_words

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.state.doing_clues import Clue

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "doing_run"
THREADS_KIND: Final[str] = "attention_threads"
DEFAULT_PRESET: Final[str] = "haiku-bg"
SHORT_EVERY: Final[timedelta] = timedelta(hours=2)
"""A day is written at most this often, however often its inputs change, unless he asks."""
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
    clock_drafts: bool = False
    """Today is also written on the clock every two hours, not only when he asks (ADR 0311)."""
    threads_prompt: str | None = None
    """What the model is told when it says what new keys of his attention served (ADR 0315);
    None: no key is told, and each stays its own thread."""
    threads_preset: str | None = None
    """The model that tells keys; None: the writer's."""

    @classmethod
    def from_config(cls, raw: object) -> DoingSettings | None:
        """The settings, or None (feature off) unless ``enabled`` is true and a prompt is there."""
        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
            return None
        preset, prompt = raw.get("preset"), raw.get("prompt")
        if not (isinstance(prompt, str) and prompt.strip()):
            LOGGER.warning("doing needs a prompt; what he was doing is not written")
            return None
        threads_prompt, threads_preset = raw.get("threads_prompt"), raw.get("threads_preset")
        return cls(
            preset=preset if isinstance(preset, str) and preset.strip() else DEFAULT_PRESET,
            prompt=prompt.strip(),
            clock_drafts=raw.get("clock_drafts") is True,
            threads_prompt=threads_prompt.strip()
            if isinstance(threads_prompt, str) and threads_prompt.strip() else None,
            threads_preset=threads_preset
            if isinstance(threads_preset, str) and threads_preset.strip() else None,
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

    The day line is folded as ``GET /inherent/day`` folds the doing lane, to where the waking part
    can reach past midnight (ADR 0313), and cut as it is there, so the spans written are over the
    segments the page shows. The day is ``final`` once the waking part of it is over.
    """
    zone = sources.zone
    start_ms, end_ms = day_window(day, zone)
    now_ms = int(now.timestamp() * 1000)
    with closing(open_runtime_event_log(event_log_path)) as conn:
        line = lane_line(conn, day, sources, now_ms)
        items = line.items
        facts = his_facts(fact_spans(items))
        waking = waking_window(facts, start_ms, end_ms, now_ms)
        if waking.end_ms <= waking.start_ms:
            return None
        clues = gather(conn, sources.memory_db, day, items, (waking.start_ms, waking.end_ms), zone)
        segments = cut(facts, waking)
        signals = gather_signals(conn, sources, segments, items)
    return Brief(
        day=day.isoformat(), weekday=f"{day:%A}", zone=zone, now_ms=now_ms,
        final=waking.end_ms < now_ms, language=_title_language(clues, sources.memory_db),
        waking=waking, segments=segments, facts=facts, items=items, clues=clues, signals=signals,
        screen=_screen_reach(line, sources),
        foreground=foreground(sources, (waking.start_ms, waking.end_ms)),
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

    The second ask shows the model its answer and why the gates refused it, so it fixes that
    rather than writing the day afresh. Blocking. A call that raises propagates, with nothing
    stored.
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
        messages = asked_again(messages, result.text, why)
    return Composed(None, why, model, spent_in, spent_out)


def store_run(memory_db: Path, brief: Brief, composed: Composed) -> str:
    """Append the gated spans, after those ``brief`` keeps, as the day's newest run; the run id.

    A run under way is a draft; it covers the waking window up to where it ends. ``brief`` is
    the whole day's: its digest is what the next look compares.
    """
    if composed.spans is None:
        msg = "a rejected answer is not stored"
        raise ValueError(msg)
    return append_doing_run(
        memory_db, day=brief.day, draft=not brief.final, covers_until_ms=brief.waking.end_ms,
        model=composed.model, inputs_digest=inputs_digest(brief), spans=composed.spans,
    )


def read_thread_brief(brief: Brief, sources: DaySources, memory_db: Path) -> ThreadBrief | None:
    """The keys of ``brief``'s waking window nobody told yet, as the model is shown them (0315).

    None when there is none to ask about.
    """
    pieces = attention.read(sources, brief.waking.start_ms, brief.waking.end_ms)
    with closing(open_memory_db(memory_db)) as conn:
        told = attention_threads(conn)
    asked = untold(pieces, told)
    if not asked:
        return None
    names = {p.key: p.name for p in pieces}
    shown = {key: names.get(key) or " ".join(key.split("|")[1:]).strip() for key in told}
    projects = sorted({
        (str(item.get("project") or ""), str(item.get("title") or ""))
        for item in brief.items if item["kind"] == "code" and item.get("project")
    })
    return ThreadBrief(
        day=brief.day, language=brief.language, zone=brief.zone, asked=asked,
        known=known_threads(told, shown), projects=projects,
    )


class Told(NamedTuple):
    """What the model said new keys served, or why none of it may be stored (ADR 0315)."""

    threads: dict[str, tuple[str | None, str | None]] | None
    why: str
    model: str | None
    tokens_in: int
    tokens_out: int


def compose_threads(
    brief: ThreadBrief, settings: DoingSettings, client: LLMClient, cost_recorder: CostRecorder,
) -> Told:
    """Ask the model what ``brief``'s keys served, and gate it; a refused answer is asked again.

    Blocking; a call that raises propagates, with nothing stored.
    """
    if settings.threads_prompt is None:
        msg = "keys are told only with doing.threads_prompt"
        raise ValueError(msg)
    messages = build_thread_messages(brief)
    why, model, spent_in, spent_out = "", None, 0, 0
    for _attempt in range(_ATTEMPTS):
        result = cost_recorder.chat(
            client, messages=messages, system=settings.threads_prompt, tools=None,
            tool_choice=None, kind=THREADS_KIND, turn_id=None,
        )
        model = result.model_used or client.model
        spent_in, spent_out = spent_in + result.tokens_in, spent_out + result.tokens_out
        found = read_threads(result.text, result.finish_reason, brief)
        if not isinstance(found, str):
            return Told(found, "", model, spent_in, spent_out)
        why = found
        LOGGER.warning("attention threads: %s rejected (%s)", brief.day, why)
        messages = asked_again(messages, result.text, why)
    return Told(None, why, model, spent_in, spent_out)


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
        threads_client: LLMClient | None = None,
    ) -> None:
        """Bind the stores and the knobs; nothing runs until :meth:`run`.

        ``client`` stands in for the one built from ``settings.preset`` on first use, and
        ``threads_client`` for the one built from ``settings.threads_preset``.
        """
        self._settings = settings
        self._sources = sources
        self._memory_db = memory_db
        self._event_log_path = event_log_path
        self._llm_config = llm_config
        self._pricing_table = pricing_table
        self._poll_s = poll_s
        self._client = client
        self._threads_client = threads_client
        self._looked: dict[str, datetime] = {}
        self._writing = threading.Lock()
        """One day is written at a time: the clock's pass and his asking never write two runs."""

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
            outcome = self._guarded(day, now, asked=False)
            if outcome == "failed" or outcome.startswith("rejected"):
                # A call that did not land is not made again until a short run would be.
                self._looked[key] = now + SHORT_EVERY - CHECK_EVERY
            if outcome not in _QUIET:
                outcomes[key] = outcome
        return outcomes

    def tidy(self, now: datetime | None = None) -> str:
        """He asked to have today tidied (ADR 0310): a run now, whenever the last one was.

        Past midnight while the day before's waking part goes on (he has not gone to bed), that
        day is the one tidied (ADR 0313). Blocking for as long as the model writes; what was done,
        as :meth:`tick` says it.
        """
        moment = now or local_now()
        return self._guarded(None, moment, asked=True)

    def _tidied(self, moment: datetime) -> date:
        """Today, or the day before while its waking part still runs past midnight."""
        zone = self._sources.zone
        today = moment.astimezone(zone).date()
        now_ms = int(moment.timestamp() * 1000)
        start_ms, end_ms = day_window(today, zone)
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            items = fold_day(conn, start_ms, end_ms, now_ms, self._sources).items
        up = reach_past_start(items, today, zone, now_ms) >= now_ms > start_ms
        return today - timedelta(days=1) if up else today

    def _guarded(self, day: date | None, now: datetime, *, asked: bool) -> str:
        """Write ``day`` (None: the day he asks to tidy); a failure is logged and said."""
        with self._writing:
            try:
                return self._day(self._tidied(now) if day is None else day, now, asked=asked)
            except Exception:
                LOGGER.exception("doing: %s failed; the previous run stands", day or "tidying")
                return "failed"

    def _day(self, day: date, now: datetime, *, asked: bool) -> str:
        brief = read_brief(day, self._sources, self._event_log_path, now)
        if brief is None:
            return "no waking window"
        if not brief.facts and not brief.clues:
            return "no inputs"
        with closing(open_memory_db(self._memory_db)) as conn:
            latest = latest_doing_run(conn, brief.day)
        recent = False
        if latest is not None:
            # A draft whose inputs did not change still becomes the final run once the day is over.
            if latest.inputs_digest == inputs_digest(brief) and not (brief.final and latest.draft):
                return "unchanged"
            recent = now - datetime.fromisoformat(latest.written_at).astimezone() < SHORT_EVERY
        # Unasked, today is written on the clock only with clock_drafts (ADR 0311).
        if not asked and (recent or not (brief.final or self._settings.clock_drafts)):
            return "waiting"
        # Today's newest part is written after what the last draft said; a final run rewrites all.
        short = latest is not None and latest.draft and not brief.final
        writing = narrow(brief, latest.spans) if short and latest is not None else brief
        if self._client is None:
            self._client = build_compact_client(self._llm_config, self._settings.preset)
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing_table)
            self._tell_threads(brief, recorder)
            composed = compose_run(writing, self._settings, self._client, recorder)
        if composed.spans is None:
            return f"rejected ({composed.why})"
        spans = [*map(dict, writing.kept), *composed.spans]
        store_run(self._memory_db, brief, composed._replace(spans=spans))
        questions = sum(span["question"] is not None for span in spans)
        kind = "final" if brief.final else "short" if writing.kept else "draft"
        return f"landed ({kind}, {len(spans)} spans, {questions} questions)"

    def _tell_threads(self, brief: Brief, recorder: CostRecorder) -> None:
        """Before a run, have the day's new keys told (ADR 0315); a failure only logs."""
        if self._settings.threads_prompt is None:
            return
        try:
            asked = read_thread_brief(brief, self._sources, self._memory_db)
            if asked is None:
                return
            if self._threads_client is None:
                self._threads_client = build_compact_client(
                    self._llm_config, self._settings.threads_preset or self._settings.preset,
                )
            told = compose_threads(asked, self._settings, self._threads_client, recorder)
            if told.threads is None:
                LOGGER.warning("attention threads: %s not told (%s)", brief.day, told.why)
                return
            append_attention_threads(
                self._memory_db, told.threads, origin="model", model=told.model, day=brief.day,
            )
            LOGGER.info("attention threads: %s told %d keys", brief.day, len(told.threads))
        except Exception:
            LOGGER.exception("attention threads: %s failed; its keys stay as they are", brief.day)

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
    "KIND",
    "RECENT_DAYS",
    "SHORT_EVERY",
    "THREADS_KIND",
    "Composed",
    "DoingSchedule",
    "DoingSettings",
    "Told",
    "compose_run",
    "compose_threads",
    "read_brief",
    "read_thread_brief",
    "store_run",
]
