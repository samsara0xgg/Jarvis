"""Runtime wiring for the writer of the doing lane (ADR 0302).

For a day, the writer folds the day line, cuts the waking window into segments, gathers the clues
and the day's keys of his attention nobody told yet or told to vary, and asks one model for what
he was doing, what each new key served (ADR 0331) and what each key that varies served in each
span (ADR 9902), on a dedicated client pinned to ``doing.preset``. While it writes, the model may
read his Mac's screen text, as often as :data:`~jarvis.decision.screen_reads.MAX_ROUNDS` rounds
allow (ADR 9901). L3 builds the input and gates the answer; L2 reads the screen and appends the
run and the told keys. A run that fails or is gated out stores nothing and the run before it
stays; a telling gated out leaves its keys untold.

:class:`DoingSchedule` looks at today and the last few days every quarter of an hour, in a thread
off the loop, and asks the model only when the inputs changed (ADR 0310, ADR 0311):

- today: a run when he asks (:meth:`DoingSchedule.tidy`), and with ``doing.clock_drafts`` one at
  most every two hours as well; after the first, each writes only the time from where the last
  written span starts and keeps the spans before it (:func:`narrow`);
- a day that is over (it is a day before today, or he has gone to bed): one final run of the
  whole day, once his phone and tablet use is known past its end or :data:`FINAL_WAIT` after it,
  and never again on the clock (ADR 9903).

The inputs are told apart by a digest of the facts and clues (:func:`inputs_digest`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.decision.attention_threads import (
    known_threads,
    labels,
    read_told,
    untold,
    varying,
)
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.doing_run import (
    Brief,
    asked_again,
    build_messages,
    inputs_digest,
    narrow,
    read_run,
    times,
    varies_of,
)
from jarvis.decision.screen_reads import MAX_ROUNDS, SCREEN_TOOL, answer, requested
from jarvis.runtime.doing_day import lane_line, reach_past_start
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.shared import lang
from jarvis.state import attention, screen_text
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

    from jarvis.decision.llm import ChatResult, LLMClient
    from jarvis.state.doing import Waking
    from jarvis.state.doing_clues import Clue
    from jarvis.state.memory_db import DoingRun

LOGGER = logging.getLogger(__name__)

KIND: Final[str] = "doing_run"
DEFAULT_PRESET: Final[str] = "haiku-bg"
SHORT_EVERY: Final[timedelta] = timedelta(hours=2)
"""A day is written at most this often, however often its inputs change, unless he asks."""
CHECK_EVERY: Final[timedelta] = timedelta(minutes=15)
"""How often a day is folded to see whether its inputs changed."""
RECENT_DAYS: Final[int] = 3
"""The days before today that are written when their inputs change."""
FINAL_WAIT: Final[timedelta] = timedelta(hours=12)
"""How long after a day's waking part ends its final waits for his phone and tablet use to arrive
(it comes 6 to 8 hours late); past it the final is written with what is known."""
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
            clock_drafts=raw.get("clock_drafts") is True,
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
    segments the page shows. The day is ``final`` once the waking part of it is over. The keys of
    the waking window nobody told yet, and those told to vary, come with the threads told so far
    (:func:`_keys`).
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
        clues = gather(sources.memory_db, day, items, (waking.start_ms, waking.end_ms), zone)
        segments = cut(facts, waking)
        signals = gather_signals(conn, sources, segments, items)
    return Brief(
        day=day.isoformat(), weekday=f"{day:%A}", zone=zone, now_ms=now_ms,
        final=waking.end_ms < now_ms, language=_title_language(clues, sources.memory_db),
        waking=waking, segments=segments, facts=facts, items=items, clues=clues, signals=signals,
        screen=_screen_reach(line, sources),
        foreground=foreground(sources, (waking.start_ms, waking.end_ms)),
        **_keys(sources, waking),
    )


def _keys(sources: DaySources, waking: Waking) -> dict[str, Any]:
    """The waking window's keys nobody told yet or told to vary, and the threads told so far.

    With where those keys held him (ADR 0331, ADR 9902). None of it without memory.db, or when no
    key is untold and none that varies held him; a read that fails leaves them out, logged, and
    the day is written without them.
    """
    memory_db = sources.memory_db
    if memory_db is None or not memory_db.is_file():
        return {}
    try:
        pieces = attention.read(sources, waking.start_ms, waking.end_ms)
        with closing(open_memory_db(memory_db)) as conn:
            told = attention_threads(conn)
    except Exception:
        LOGGER.exception("doing: the day's keys could not be read; none is asked about")
        return {}
    asked, varies = untold(pieces, told), varying(pieces, told)
    if not asked and not varies:
        return {}
    names = {p.key: p.name for p in pieces}
    shown = {key: names.get(key) or attention.key_name(key) for key in told}
    keys = {one.key for one in (*asked, *varies)}
    return {
        "asked": asked, "varies": varies, "known": known_threads(told, shown),
        "pieces": [p for p in pieces if p.key in keys],
    }


class Composed(NamedTuple):
    """What the model answered for a day, each part gated by itself.

    ``spans`` are the gated spans, or None when none may be stored; ``threads`` what the new keys
    served, by key (empty with none asked), or None when it may not be stored; ``why`` the last
    refusal.
    """

    spans: list[dict[str, Any]] | None
    threads: dict[str, tuple[str | None, str | None]] | None
    why: str
    model: str | None
    tokens_in: int
    tokens_out: int
    reads: int = 0
    """How many times the model read the screen."""


type ScreenReader = Callable[[int, int, str | None], Sequence[screen_text.Window]]
"""Reads his Mac's screen over ``[start_ms, end_ms)``, of one key or of every window (None)."""

_READ_ENOUGH: Final = (
    "[From the program, not the user] No more screen reads for this answer. Write the whole "
    "answer now as one JSON object."
)


def screen_reader(sources: DaySources) -> ScreenReader:
    """The screen reads of ``sources``' shipped captures (:func:`jarvis.state.screen_text.seen`)."""
    return lambda start_ms, end_ms, key: screen_text.seen(sources, start_ms, end_ms, key)


def _assistant_turn(result: ChatResult) -> dict[str, Any]:
    return {
        "role": "assistant", "content": result.text or "",
        "tool_calls": [
            {
                "id": call.call_id, "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in result.tool_calls
        ],
    }


class _Asked(NamedTuple):
    result: ChatResult
    messages: list[dict[str, Any]]
    tokens_in: int
    tokens_out: int
    reads: int


def _ask(  # noqa: PLR0913 — the conversation, the knobs, the model and its two seams.
    messages: list[dict[str, Any]], brief: Brief, settings: DoingSettings, client: LLMClient,
    cost_recorder: CostRecorder, read: ScreenReader | None,
) -> _Asked:
    """One answer, after the screen reads it asks for, at most :data:`MAX_ROUNDS` rounds of them.

    Each read is answered and the model asked again; past the last round it is told to answer
    and may read no more. Without ``read`` the model is given no tool.
    """
    tools = None if read is None else [SCREEN_TOOL]
    at = {f"t{n}": ms for n, ms in enumerate(times(brief))}
    keys = labels(brief.asked, brief.varies)
    names = {one.key: one.name for one in (*brief.asked, *brief.varies)}
    spent_in = spent_out = reads = 0
    for rounds in range(MAX_ROUNDS + 1):
        last = rounds == MAX_ROUNDS
        result = cost_recorder.chat(
            client, messages=messages, system=settings.prompt, tools=tools,
            tool_choice=None if tools is None else "none" if last else "auto",
            kind=KIND, turn_id=None,
        )
        spent_in, spent_out = spent_in + result.tokens_in, spent_out + result.tokens_out
        calls = [] if read is None or last else requested(result)
        if not calls:
            return _Asked(result, messages, spent_in, spent_out, reads)
        assert read is not None  # noqa: S101 — calls are read only with a reader
        replies = []
        for call_id, arguments in calls:
            text, error = answer(arguments, at, keys, names, read, brief.zone)
            replies.append({
                "role": "tool", "tool_call_id": call_id, "content": text,
                **({"is_error": True} if error else {}),
            })
        reads += len(calls)
        messages = [*messages, _assistant_turn(result), *replies]
        if rounds == MAX_ROUNDS - 1:
            messages.append({"role": "user", "content": _READ_ENOUGH})
    msg = "unreachable: the last round reads nothing"
    raise AssertionError(msg)


def compose_run(
    brief: Brief, settings: DoingSettings, client: LLMClient, cost_recorder: CostRecorder,
    read: ScreenReader | None = None,
) -> Composed:
    """Ask the model for ``brief``'s day and gate the answer; a rejected one is asked once more.

    With ``read`` the model may read his Mac's screen while it writes (ADR 9901). The spans and
    what the new keys served are gated apart: either refused asks again, and a part that passed
    in either answer is kept, the later one when both passed. What a span says a key that varies
    served is gated with the spans, by the keys the same answer told to vary (ADR 9902). The
    second ask shows the model its answer and why the gates refused it, so it fixes that rather
    than writing the day afresh. Blocking. A call that raises propagates, with nothing stored.
    """
    messages = build_messages(brief)
    spans: list[dict[str, Any]] | None = None
    threads: dict[str, tuple[str | None, str | None]] | None = None
    why, model, spent_in, spent_out, reads = "", None, 0, 0, 0
    for _attempt in range(_ATTEMPTS):
        asked = _ask(messages, brief, settings, client, cost_recorder, read)
        result, messages = asked.result, asked.messages
        model = result.model_used or client.model
        spent_in, spent_out = spent_in + asked.tokens_in, spent_out + asked.tokens_out
        reads += asked.reads
        told = read_told(result.text, result.finish_reason, brief.asked)
        found = read_run(result.text, result.finish_reason, brief, varies_of(brief, told))
        spans = spans if isinstance(found, str) else found
        threads = threads if isinstance(told, str) else told
        refused = [part for part in (found, told) if isinstance(part, str)]
        if not refused:
            return Composed(spans, threads, "", model, spent_in, spent_out, reads)
        why = "; ".join(refused)
        LOGGER.warning("doing: %s rejected (%s)", brief.day, why)
        messages = asked_again(messages, result.text, why)
    return Composed(spans, threads, why, model, spent_in, spent_out, reads)


def store_told(memory_db: Path, brief: Brief, composed: Composed) -> int:
    """Append what the answer told of ``brief``'s new keys, as the model's; how many it told.

    Nothing is kept when that part was refused or no key was asked, and its keys are asked again
    at the next run.
    """
    if not composed.threads:
        if composed.threads is None:
            LOGGER.warning("doing: %s's new keys not told (%s)", brief.day, composed.why)
        return 0
    append_attention_threads(
        memory_db, composed.threads, origin="model", model=composed.model, day=brief.day,
    )
    return len(composed.threads)


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


def settled(brief: Brief, now: datetime) -> bool:
    """Whether ``brief``'s finished day may have its final: its late use is in, or waited for.

    His phone and tablet use reaches the brain hours late; the final waits until every device is
    known past the waking window, or none reaches the brain, or :data:`FINAL_WAIT` has passed
    since the window ended (ADR 9903).
    """
    if not brief.screen:
        return True
    ended = datetime.fromtimestamp(brief.waking.end_ms / 1000, brief.zone)
    return now - ended >= FINAL_WAIT


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
        self._refused: dict[str, str] = {}
        """The inputs of each finished day whose final the gates refused, by day."""
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

    def _held(
        self, brief: Brief, latest: DoingRun | None, digest: str, now: datetime, *, asked: bool,
    ) -> str | None:
        """Why ``brief``'s day is not written now (``unchanged`` or ``waiting``), else None."""
        recent = False
        if latest is not None:
            # A finished day has one final (ADR 9903); late rows do not rewrite it unasked.
            if brief.final and not latest.draft and not asked:
                return "unchanged"
            # A draft whose inputs did not change still becomes the final run once the day is over.
            if latest.inputs_digest == digest and not (brief.final and latest.draft):
                return "unchanged"
            recent = now - datetime.fromisoformat(latest.written_at).astimezone() < SHORT_EVERY
        if asked:
            return None
        # Unasked, today is written on the clock only with clock_drafts (ADR 0311).
        if recent or not (brief.final or self._settings.clock_drafts):
            return "waiting"
        # A final waits for his late phone and tablet use; one the gates refused is tried again
        # on the clock only when its inputs change.
        if brief.final and (not settled(brief, now) or self._refused.get(brief.day) == digest):
            return "waiting"
        return None

    def _day(self, day: date, now: datetime, *, asked: bool) -> str:
        brief = read_brief(day, self._sources, self._event_log_path, now)
        if brief is None:
            return "no waking window"
        if not brief.facts and not brief.clues:
            return "no inputs"
        with closing(open_memory_db(self._memory_db)) as conn:
            latest = latest_doing_run(conn, brief.day)
        digest = inputs_digest(brief)
        if (held := self._held(brief, latest, digest, now, asked=asked)) is not None:
            return held
        # Today's newest part is written after what the last draft said; a final run rewrites all.
        short = latest is not None and latest.draft and not brief.final
        writing = narrow(brief, latest.spans) if short and latest is not None else brief
        if self._client is None:
            self._client = build_compact_client(self._llm_config, self._settings.preset)
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing_table)
            composed = compose_run(
                writing, self._settings, self._client, recorder, screen_reader(self._sources),
            )
        told = store_told(self._memory_db, brief, composed)
        keys = f", {told} keys told" if told else ""
        if composed.spans is None:
            if brief.final:
                self._refused[brief.day] = digest
            return f"rejected ({composed.why}){keys}"
        spans = [*map(dict, writing.kept), *composed.spans]
        store_run(self._memory_db, brief, composed._replace(spans=spans))
        questions = sum(span["question"] is not None for span in spans)
        kind = "final" if brief.final else "short" if writing.kept else "draft"
        return f"landed ({kind}, {len(spans)} spans, {questions} questions{keys})"

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
    "FINAL_WAIT",
    "KIND",
    "RECENT_DAYS",
    "SHORT_EVERY",
    "Composed",
    "DoingSchedule",
    "DoingSettings",
    "ScreenReader",
    "compose_run",
    "read_brief",
    "screen_reader",
    "settled",
    "store_run",
    "store_told",
]
