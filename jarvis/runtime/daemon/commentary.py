"""The lead-in commentary path: the fixed wait lines a turn he spoke says while it works.

ADR-0008 D6, ADR 0116, 0121, 0136, 0140, 0320. One durable-cursor watcher over the log, the
worker-thread decision of whether a row speaks, and the commentary run it renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import secrets
import sqlite3
import statistics
import threading
import time
import uuid
from typing import TYPE_CHECKING, Final

from jarvis.decision.commentary import (
    COMMENTARY_ATTENTION_CHANNEL,
    commentary_intent_for,
    commentary_speech_text,
)
from jarvis.decision.gates import ResponsePlan, pre_emit_gate
from jarvis.decision.response_run import (
    ResponseCancelRequest,
    ResponseRun,
    ResponseRunRegistry,
    ResponseTerminalizer,
    deterministic_commentary_policy,
    evidence_snapshot_hash,
    request_response_cancel,
    start_response_run,
)
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S
from jarvis.runtime.turn.wait import event_action_id
from jarvis.shared import Event, steps
from jarvis.shared.realtime import AlreadyTerminal, new_response_id
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.shared.text import is_english
from jarvis.state.action_facts import proposed_call
from jarvis.state.event_log import (
    emit_event,
    fetch_events_after,
    latest_id,
    open_runtime_event_log,
)
from jarvis.state.projections import rebuild_projections
from jarvis.state.turn_facts import (
    commentary_runs_opened,
    turn_over_or_answering,
    turn_trigger,
)
from jarvis.state.turn_steps import finished_call_ms
from jarvis.surface.cli import record_pre_emit_token
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.shared.clock import Clock
    from jarvis.shared.realtime import PresentationIntent

LOGGER = logging.getLogger(__name__)

# --- ADR-0008 D6 lifecycle commentary ---------------------------------------
#
# The observer below is the whole of the D6 wiring. It is deliberately a
# durable-cursor watcher rather than a `CommittedEventBus` subscriber:
# `jarvis/execution/tools.py` is never handed a bus, so `action.dispatched`
# commits unpublished and a subscriber would silently lose the acknowledge
# row. The Event Log is the only place it is guaranteed to appear.
#
# Nothing here re-enters `decide()`: `RUNTIME_TRIGGER_TYPES` is untouched and
# no model is ever called to produce a phrase (ADR-0008 D6, "a deep model is
# never called only to generate 我在查"). A turn he spoke says one fixed line
# when it is taking a while (ADR 0116, 0121, 0136): at the dispatch of a tool
# that is not quiet, saying what it is about to do, or once `COMMENTARY_AFTER_S`
# have passed with nothing of the answer started; then a follow-up at each of
# `COMMENTARY_STILL_AFTER_S` while the answer still has not started, at most
# `COMMENTARY_MAX_LINES` in all.

_COMMENTARY_ACTION_TYPES: Final[tuple[str, ...]] = (
    "action.dispatched", "utterance.received", "route.tool_predicted",
)
"""The D6 rows that speak (:data:`jarvis.decision.commentary._D6_ROWS`)."""

COMMENTARY_EARLIEST_S: Final[float] = 1.5
"""How long after Allen's words a tool's dispatch line may start.

An answer that begins within it needs no lead-in: a light switched in 0.4 s
(2026-09-26 to 09-29). The line speaks for the turn, not for the one tool:
a tool that has already finished does not silence it; an answer that is out
does. A Tier 0 turn ends within about 1 s, so the floor and the turn-ended
check keep it silent.
"""

COMMENTARY_AFTER_S: Final[float] = 8.0
"""How long after Allen's words, with nothing of the answer started, the line
speaks whether or not a tool is involved (ADR 0121: a toolless turn answers at
a median 2.8 s and p90 5.0 s, so 2.5 s fired right before the answer; 2026-10-02
Allen moved it from 4 s to 8 s: 8 of 16 turns that heard a line at 4 s were
answered before 8 s)."""

COMMENTARY_STILL_AFTER_S: Final[tuple[float, ...]] = (25.0,)
"""How long after Allen's words each follow-up ("still working") may speak while
the answer still has not started (ADR 0121)."""

COMMENTARY_MAX_LINES: Final = 2
"""The most lines one turn says: the first and the follow-ups (ADR 0121)."""

_COMMENTARY_SHUTDOWN_BUDGET_S: Final[float] = 0.5
"""Whole-budget SQLite wait the teardown cancel may spend on the event loop."""

_COMMENTARY_ORIGIN_TRIGGER_TYPE: Final = "utterance.received"
"""What makes a turn one he spoke. A typed turn (`surface.user_intent`), a
GPT-Live relay (also `surface.user_intent`) and a reconciliation or
supervisor-sweep turn (no `turn.started` at all) never match it.
"""

_COMMENTARY_LOCK: Final = threading.Lock()
"""One decide-and-render at a time, so the dispatch and the clock of the same
turn cannot both find it unspoken."""


@dataclasses.dataclass(frozen=True)
class OpenCommentary:
    """One commentary ResponseRun that rendered but has not been heard yet.

    It stays open on purpose. ``response.completed`` is written when the
    phrase reaches the speaker (`surface.playback_started`); one that never
    did is cancelled at teardown.
    """

    subject_ref: str
    run: ResponseRun
    registry: ResponseRunRegistry
    response_hash: str


def commentary_terminalizer(
    runtime: JarvisRuntime,
    *,
    deadline: float | None = None,
) -> ResponseTerminalizer:
    """Build a terminalizer that owns its connection, like the cancel seam.

    The run outlives the worker call that opened it, so the terminal owner
    cannot close over that call's connection; ``close_after=True`` with a
    per-call ``open_runtime_event_log`` is the established shape
    (:func:`jarvis.runtime.turn.cancel.make_response_cancel_callable`).

    ``deadline`` becomes the connection's SQLite ``busy_timeout``. The
    teardown path passes one because it runs on the event-loop thread: an
    unbounded wait there would hold the loop for the default five seconds per
    unheard run while a worker owns the writer.
    """
    event_log_path = runtime.runtime_paths.event_log
    return ResponseTerminalizer(
        lambda: open_runtime_event_log(event_log_path, deadline=deadline),
        close_after=True,
        committed_event_bus=runtime.committed_event_bus,
    )


def emit_pre_emit_verdict(
    conn: sqlite3.Connection,
    *,
    plan: ResponsePlan,
    turn_id: str,
) -> None:
    """Record the Pre-emit Gate verdict this commentary was approved under.

    ADR-0001 § Gate contracts: the gate emits
    ``gate.evaluated(gate="pre_emit", ...)`` before any byte of the response
    reaches L5. The commentary path calls the gate itself rather than going
    through ``decide()``, so it owns that append too; the payload is the same
    shape ``jarvis.decision``'s emitter writes.
    """
    emit_event(
        conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_emit",
            "outcome": plan.permission,
            "reasons": [
                f"permission={plan.permission}",
                f"downgrade_required={plan.downgrade_required}",
                f"active_claim_levels={list(plan.active_claim_levels)}",
            ],
            "response_hash": plan.response_hash,
            "claim_levels": list(plan.active_claim_levels),
            "attempt": 0,
        },
        correlation={"turn_id": turn_id},
    )


def _commentary_turn(
    conn: sqlite3.Connection, event: Event,
) -> tuple[str, str, int] | None:
    """Return this row's turn Allen spoke, his words and when, or ``None``.

    Durable reads and no heuristics: the row names its turn, that turn's
    ``turn.started`` names the trigger it was claimed from, and the trigger's
    own type decides whether Allen spoke this.
    """
    # Both rows that speak name their turn (`_action_correlation` fills it
    # from the ActionRequest; the voice pipeline writes it on his words).
    turn_id = (event.correlation or {}).get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    trigger = turn_trigger(conn, turn_id)
    if trigger is None or trigger.type != _COMMENTARY_ORIGIN_TRIGGER_TYPE:
        return None
    transcript = trigger.payload.get("transcript")
    return turn_id, transcript if isinstance(transcript, str) else "", trigger.ts_epoch_ms


_LEAD_IN_MIN_CALLS: Final = 5
"""A tool with fewer finished calls in the log than this has no measured time: it says nothing."""

def cancel_unheard_commentary(
    runtime: JarvisRuntime,
    entry: OpenCommentary,
    *,
    reason: str,
    deadline: float | None = None,
) -> None:
    """Cancel a commentary run that never reached the speaker."""
    request_response_cancel(
        entry.registry,
        commentary_terminalizer(runtime, deadline=deadline),
        ResponseCancelRequest(
            request_id="CREQ" + uuid.uuid4().hex,
            response_id=entry.run.response_id,
            scope="generation",
            reason=reason,
        ),
    )
    # Unconditional: whatever outcome came back, the watcher is done with this
    # entry, so the operator seam must stop seeing it.
    entry.registry.unregister(entry.run.response_id)


def _complete_commentary(runtime: JarvisRuntime, entry: OpenCommentary) -> None:
    """Close a commentary run whose phrase reached the speaker."""
    # Before any early return: this entry is being released either way.
    entry.registry.unregister(entry.run.response_id)
    if not entry.run.is_open:
        # A cancel already won the CAS — playback of a cancelled phrase that
        # started anyway is not a reason to raise out of the watcher.
        return
    entry.run.mark("finalizing")
    outcome = commentary_terminalizer(runtime).complete(
        entry.run.facts,
        response_hash=entry.response_hash,
    )
    if isinstance(outcome, AlreadyTerminal):
        return
    entry.run.mark("completed")


def render_commentary(  # noqa: PLR0913 - the run's identity plus what it says
    runtime: JarvisRuntime,
    conn: sqlite3.Connection,
    *,
    intent: PresentationIntent,
    trigger_event: Event,
    turn_id: str,
    speech: str,
) -> OpenCommentary:
    """Open one ``phase="commentary"`` run and deliver its single segment.

    ``speech`` is the intent's phrase as speech.

    ``turn_id`` is the action's own turn, so ``response_group_id`` derives to
    that turn's group and voice_media appends the phrase to the lane instead
    of interrupting whatever else that group is saying. The trigger is the
    row that justified the phrase, a tool's dispatch or his words: D6's
    "truth derives from the durable event" is literally this run's
    ``source_event_id``.

    A request client is built only because ``response.started`` carries the
    preset snapshot. No request is ever issued, so the run has no cost
    disposition to record.
    """
    factory = runtime.llm_session_factory
    response_id = new_response_id()
    snapshot = factory.snapshot(None)
    run = start_response_run(
        conn,
        turn_id=turn_id,
        trigger_event_uid=trigger_event.event_uid,
        request_client=factory.create(snapshot, response_id=response_id),
        policy=deterministic_commentary_policy(
            active_subject_ref=intent.subject_ref,
            evidence_snapshot_hash=evidence_snapshot_hash(conn),
            preset_snapshot_hash=snapshot.snapshot_hash,
        ),
        response_id=response_id,
        phase="commentary",
        channel="speech",
        committed_event_bus=runtime.committed_event_bus,
    )
    if trigger_event.type == "action.dispatched":
        run.link_action(intent.subject_ref)
    # The registry the normal response path registers under
    # (`jarvis/runtime/turn/start.py`), so the commentary run is reachable by id
    # like any final run; a runtime without one keeps a private registry.
    registry = runtime.response_runs if runtime.response_runs is not None else ResponseRunRegistry()
    # No subject is in scope for a fixed lifecycle phrase, so the Pre-emit
    # Gate short-circuits to its routine pass-through and hands back the
    # token `render_response` demands.
    plan = pre_emit_gate(speech)
    emit_pre_emit_verdict(conn, plan=plan, turn_id=turn_id)
    render_response(
        record_pre_emit_token(plan.response_hash),
        plan,
        conn=conn,
        turn_id=turn_id,
        attention_channel=COMMENTARY_ATTENTION_CHANNEL,
        available_surfaces=frozenset(),
        streaming_enabled=True,
        response_id=run.response_id,
        response_group_id=run.response_group_id,
        phase="commentary",
    )
    record_realtime_trace(
        "lifecycle_commentary_rendered",
        response_id=run.response_id,
        response_group_id=run.response_group_id,
        turn_id=turn_id,
        subject_ref=intent.subject_ref,
        intent_type=intent.intent_type,
    )
    # Registered last, once nothing above can still raise: an entry the
    # watcher never receives is an entry no close path can ever unregister,
    # and in the runtime registry that would be a permanently open run.
    registry.register(run)
    return OpenCommentary(
        subject_ref=intent.subject_ref,
        run=run,
        registry=registry,
        response_hash=plan.response_hash,
    )


def _commentary_turn_is_over(runtime: JarvisRuntime, trigger_event: Event) -> bool:
    """Whether the turn this row belongs to can no longer say a line.

    True for a turn he did not speak, one that has ended and one whose answer has
    started: the later clocks of the row have nothing left to watch for.
    """
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        turn = _commentary_turn(conn, trigger_event)
        return turn is None or turn_over_or_answering(conn, turn[0])
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _hold_until(clock: Clock, said_at_ms: int, after_s: float, stop: threading.Event) -> bool:
    """Wait on ``clock`` until ``after_s`` past his words; ``False`` if shutdown came first."""
    return not clock.wait(stop, max(said_at_ms / 1000 + after_s - clock.time(), 0.0))


def _slow_tool_line(
    runtime: JarvisRuntime, conn: sqlite3.Connection, dispatched: Event, user_text: str,
) -> str | None:
    """The step table's line for the tool a dispatch started, only when that tool is slow.

    Slow is a median over its last 20 finished calls in the log that reaches
    ``voice.lead_in_ms``, with at least :data:`_LEAD_IN_MIN_CALLS` of them (ADR 0320). The line is
    in his language (that of his words), without a detail.
    """
    action_id = dispatched.payload.get("action_id")
    call = proposed_call(conn, action_id) if isinstance(action_id, str) else None
    if call is None:
        return None
    waits = finished_call_ms(conn, call.tool_name)
    lead_in_ms = runtime.config.voice.lead_in_ms
    if len(waits) < _LEAD_IN_MIN_CALLS or statistics.median(waits) < lead_in_ms:
        return None
    return steps.line(
        call.tool_name, call.arguments, language="en" if is_english(user_text) else "zh",
    )


def open_commentary_in_worker_thread(  # noqa: PLR0911 - one early return per suppression rule
    runtime: JarvisRuntime,
    *,
    trigger_event: Event,
    stop: threading.Event | None = None,
    after_s: float | None = None,
    still: bool = False,
) -> OpenCommentary | None:
    """Decide and deliver one row's commentary on a worker thread.

    The row is the ``action.dispatched`` of a tool that is not quiet (ADR 0136), Jev's
    ``route.tool_predicted`` for the line (ADR 0140; it speaks at once, before any tool) or the
    ``utterance.received`` that carries his words; the second one is the clock
    of ADR 0116 and waits ``after_s`` (default :data:`COMMENTARY_AFTER_S`) after
    his words before it looks at anything. ``still`` makes it a follow-up line
    of a wait that goes on (ADR 0121), which may be any of the turn's lines up
    to :data:`COMMENTARY_MAX_LINES`; the first line needs the turn to have none.

    Opens its own connection for the same ``check_same_thread`` reason
    :func:`_drive_turn_in_worker_thread` does, and for a second one: the
    projection rebuild and the four appends must not stall the event loop
    the TTS watcher polls on.

    Returns ``None`` — writing nothing at all — when the row maps to no D6
    intent (a quiet tool), when the turn is not one he spoke
    (typed, GPT-Live, system), when the turn has ended or its answer has started
    by the time the wait is over, when this turn already said its lines, or when
    a confirmation is still awaiting an answer. The ended and answer-started
    checks are the last thing before the line is rendered. An answer that begins
    after them is the media owner's to resolve: it drops a line that has not
    started playing (ADR 0121). ``stop`` ends the wait early at shutdown.
    """
    clock = trigger_event.type == "utterance.received"
    stop = stop or threading.Event()
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        clock_s = COMMENTARY_AFTER_S if after_s is None else after_s
        if clock and not _hold_until(runtime.clock, trigger_event.ts_epoch_ms, clock_s, stop):
            return None  # before the lookup below: the claim row lands just after his words
        turn = _commentary_turn(conn, trigger_event)
        if turn is None:
            return None
        turn_id, user_text, said_at_ms = turn
        tool_line: str | None = None
        if trigger_event.type == "action.dispatched":
            tool_line = _slow_tool_line(runtime, conn, trigger_event, user_text)
        intent = commentary_intent_for(
            trigger_event,
            user_text=user_text,
            tool_line=tool_line,
            still=still,
            pick=secrets.choice,
        )
        if intent is None:  # a tool not known to be slow
            return None
        # A quick answer needs no lead-in: give it until the floor to begin. Jev's predicted
        # group (ADR 0140) is a read or a draft, never quick, and speaks at once.
        if trigger_event.type == "action.dispatched" and not _hold_until(
            runtime.clock, said_at_ms, COMMENTARY_EARLIEST_S, stop,
        ):
            return None
        with _COMMENTARY_LOCK:
            if commentary_runs_opened(conn, turn_id) >= (COMMENTARY_MAX_LINES if still else 1):
                # The turn's lines are said. Checked after the origin filter and
                # before every write, so that a suppression path below never
                # consumes a slot, and under the lock, because the dispatch and
                # the clocks of one turn race.
                return None
            # ADR 0164: the shared cache folds only what was appended since the last read;
            # hand-built runtimes hold none and fold the whole log.
            projections = (
                runtime.decision_state.read(conn).projections
                if runtime.decision_state is not None
                else rebuild_projections(conn)
            )
            slot = projections.pending_confirmations.slot
            if slot is not None and slot.is_live(int(runtime.clock.time() * 1000)):
                # ADR-0014: new commentary never overwrites an unresolved
                # confirmation. The slot is globally unique and its
                # calls carry no `action_id`, so this is enforced
                # at the only granularity the fold supports.
                return None
            if turn_over_or_answering(conn, turn_id) or (
                runtime.response_runs is not None
                and (
                    runtime.response_runs.turn_stopped(turn_id)
                    or runtime.response_runs.turn_text_only(turn_id)
                )
            ):
                # The stop button's mark covers a turn still queued, whose run
                # has not opened to write its response.cancelled yet. A turn that answers
                # in text only (ADR 0269) has no wait line to say.
                # Last before the render, after the slow projection rebuild: the
                # answer is out or on its way (a quick answer, or a Tier 0 turn
                # that ends before its tool's rows reach this watcher), and a
                # phrase now would talk over it or follow it.
                return None
            return render_commentary(
                runtime,
                conn,
                intent=intent,
                trigger_event=trigger_event,
                turn_id=turn_id,
                speech=commentary_speech_text(intent),
            )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


async def commentary_watcher(  # noqa: C901, PLR0915 - the row handlers share the watcher's state
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: speak the deterministic wait lines of a turn he spoke.

    One cursor over ``action.dispatched``, ``utterance.received``,
    ``surface.speech_dropped`` plus ``surface.playback_started``, anchored at
    the boot high-water mark exactly like ``_tts_watcher`` — a historical
    action from a previous session must never speak fake progress.
    ``surface.playback_started`` closes a phrase that reached the speaker;
    ``surface.speech_dropped`` is the media owner discarding one that had not
    started when its turn's answer opened (ADR 0121), so its run is cancelled.

    Each row that may speak gets a task of its own, because the clock row
    waits seconds and must not hold back the rows behind it. Audibility is
    decided by the per-turn count in :func:`open_commentary_in_worker_thread`,
    not here: a turn says at most :data:`COMMENTARY_MAX_LINES` lines, and the
    first row of that turn that actually opens one wins. The ``spoken`` set
    below is the cheaper guard in front of it — one entry per
    ``(subject, event type)``, so a repeated row is dropped before the thread
    hop and the projection rebuild. It decides work, not what is heard.

    Per-row dispatch is wrapped in a catch-all for the same reason
    ``_tts_watcher``'s is: commentary is a courtesy, and a failure to produce
    it must never stall the watcher or the turn it is commenting on.
    """
    after_id = latest_id(runtime.conn)
    open_by_subject: dict[str, OpenCommentary] = {}  # by response id
    spoken: set[tuple[str, str]] = set()
    tasks: set[asyncio.Task[None]] = set()
    # A playback or drop row can be read while the worker that rendered its phrase has not yet
    # handed the run back; it waits here, by response id, until `_speak` registers that run.
    early: dict[str, Event] = {}
    stop = threading.Event()

    async def _speak(ev: Event) -> None:
        # The clock row keeps watching the turn: the first line, then a follow-up
        # at each later time while the answer has still not started.
        schedule: list[tuple[float | None, bool]] = [(None, False)]
        if ev.type == "utterance.received":
            schedule = [(COMMENTARY_AFTER_S, False)]
            schedule += [(after_s, True) for after_s in COMMENTARY_STILL_AFTER_S]
        for after_s, still in schedule:
            work = asyncio.ensure_future(
                asyncio.to_thread(
                    open_commentary_in_worker_thread,
                    runtime,
                    trigger_event=ev,
                    stop=stop,
                    after_s=after_s,
                    still=still,
                ),
            )
            try:
                opened = await asyncio.shield(work)
            except asyncio.CancelledError:
                # Shutdown while the worker may be mid-render: its run would otherwise be
                # lost with the cancelled await and stay open for good.
                if (late := await work) is not None:
                    open_by_subject[late.run.response_id] = late
                raise
            if opened is not None:
                open_by_subject[opened.run.response_id] = opened
                if (arrived := early.pop(opened.run.response_id, None)) is not None:
                    await _commentary_closed(runtime, open_by_subject, arrived, None)
            elif len(schedule) > 1 and (
                stop.is_set() or await asyncio.to_thread(_commentary_turn_is_over, runtime, ev)
            ):
                return

    async def _on_row(ev: Event) -> None:
        try:
            if ev.type in ("surface.playback_started", "surface.speech_dropped"):
                await _commentary_closed(runtime, open_by_subject, ev, early if tasks else None)
                return
            subject = event_action_id(ev) or ev.event_uid
            if (subject, ev.type) in spoken:
                return
            spoken.add((subject, ev.type))
            task = asyncio.create_task(_speak(ev))
            tasks.add(task)
            task.add_done_callback(_task_done)
        except Exception as exc:  # noqa: BLE001 — commentary must not crash the watcher.
            LOGGER.warning("commentary_watcher: %s raised: %r", ev.type, exc)

    def _task_done(task: asyncio.Task[None]) -> None:
        tasks.discard(task)
        if not tasks:
            early.clear()  # nothing can still be on its way to register
        if not task.cancelled() and (exc := task.exception()) is not None:
            LOGGER.warning("commentary_watcher: a line raised: %r", exc)

    LOGGER.info("commentary_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=(
                    *_COMMENTARY_ACTION_TYPES,
                    "surface.speech_dropped",
                    "surface.playback_started",
                ),
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                await _on_row(ev)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # a run mid-render lands first
        shutdown_deadline = time.monotonic() + _COMMENTARY_SHUTDOWN_BUDGET_S
        for entry in open_by_subject.values():
            with contextlib.suppress(Exception):
                cancel_unheard_commentary(
                    runtime,
                    entry,
                    reason="shutdown",
                    deadline=shutdown_deadline,
                )
        LOGGER.info("commentary_watcher cancelled")
        raise


async def _commentary_closed(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, OpenCommentary],
    event: Event,
    early: dict[str, Event] | None,
) -> None:
    """Close (heard) or cancel (dropped) the run this row names.

    A run that is not ours yet is kept in ``early`` when a worker may still be handing it
    over (``early`` is not None); the watcher closes it as soon as it registers the run.
    """
    handler = _commentary_heard if event.type == "surface.playback_started" else _commentary_dropped
    if not await handler(runtime, open_by_subject, event) and early is not None:
        early[str(event.payload.get("response_id"))] = event


async def _commentary_dropped(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, OpenCommentary],
    event: Event,
) -> bool:
    """Cancel the commentary run whose phrase the media owner discarded unplayed.

    The answer of the turn opened before the phrase started: the phrase was
    never spoken, so its run must not stay open until teardown. False when the
    run is not (yet) one of ours.
    """
    entry = open_by_subject.pop(str(event.payload.get("response_id")), None)
    if entry is None:
        return False
    await asyncio.to_thread(
        cancel_unheard_commentary, runtime, entry, reason="answer_started",
    )
    return True


async def _commentary_heard(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, OpenCommentary],
    event: Event,
) -> bool:
    """Close the commentary run this playback belongs to; False when it is not (yet) ours."""
    response_id = event.payload.get("response_id")
    entry = next(
        (item for item in open_by_subject.values() if item.run.response_id == response_id),
        None,
    )
    if entry is None:
        return False
    del open_by_subject[entry.run.response_id]
    await asyncio.to_thread(_complete_commentary, runtime, entry)
    return True
