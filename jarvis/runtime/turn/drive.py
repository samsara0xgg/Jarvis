"""The turn driver: decide, wait for L4, hold, finalize and render one turn."""

from __future__ import annotations

import contextlib
import io
import logging
import sys
import time
from collections.abc import Mapping  # runtime use: isinstance in the config readers.
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Final, cast

from jarvis.decision import (
    SPOKEN_REPLY_FORMAT,
    DecideContext,
    LifecycleLike,
    ToolRegistryLike,
    decide,
    emit_turn_ended,
    spoken_reply_rules,
    turn_timing,
)
from jarvis.decision.pre_route import RoutineStreamRoute, StreamCorrection, typed_turn
from jarvis.decision.response_run import (
    ResponseCancelledError,
    ResponseRun,
    ResponseTerminalizer,
    wait_out_hold,
)
from jarvis.execution.tools import ReadOnlyToolRegistry, release_turn_actions, turn_action_ids
from jarvis.runtime import JarvisRuntime, RunTurnResult, answer_hold, device_voice
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.runtime.turn.cancel import SUPERSEDE_WINDOW_S
from jarvis.runtime.turn.context import (
    about_record,
    about_row,
    file_in_topic,
    history_pictures,
    history_receipts,
    live_lines,
    make_entity_resolver,
    max_tool_iterations,
    snapshot_reader,
    turn_device,
    turn_topic,
    voice_service_tier,
    warm_next_prefix,
    whereabouts_lines,
)
from jarvis.runtime.turn.finish import (
    emit_turn_timing,
    fetched_mail,
    queue_picture_notes,
    record_attachments,
    record_reaction,
    record_words,
    turn_links,
    turn_places,
    turn_times,
)
from jarvis.runtime.turn.start import start_drive_turn_response
from jarvis.runtime.turn.wait import (
    TriggerWaitTimeout,
    TurnSuspended,
    WaitingTurn,
    event_action_id,
    trigger_wait_budget,
    wait_for_next_trigger,
)
from jarvis.shared.action_admission import bind_action_admission
from jarvis.shared.realtime import AlreadyTerminal
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.shared.system_prompt import SplitSystem
from jarvis.state import picture_notes as note_records
from jarvis.state.event_log import latest_id
from jarvis.state.memory_db import append_record, record_sent, render_context
from jarvis.state.stream_emission import committed_text_prefix
from jarvis.state.trigger_consumption import mark_trigger_consumed
from jarvis.surface.cli import record_pre_emit_token
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.decision import ResponsePlan
    from jarvis.shared import Event
    from jarvis.shared.realtime_trace import TraceValue


LOGGER = logging.getLogger(__name__)


# ADR 0053: a hold that is never released (a lost release) delays an answer by
# at most this; an utterance itself ends by max_utterance_s (30 s).
_ALLEN_TALKING_CEILING_S: Final[float] = 60.0


# Default per-turn iteration ceiling. Protects ``drive_turn`` from a stuck
# trigger chain. 50 is ample headroom.
_DEFAULT_MAX_ITERATIONS: int = 50


def drive_turn(  # noqa: C901, PLR0912, PLR0913, PLR0915 — composition-root entrypoint; argument set + traced multi-trigger loop are the cross-surface contract, and every ResponseRun branch is flag-guarded.
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    available_surfaces: frozenset[str] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float | None = None,
    streaming_enabled: bool = False,
    suspend_when_waiting: bool = False,
    continuation: WaitingTurn | None = None,
) -> RunTurnResult:
    """Drive the post-emit body of one turn from an already-emitted intent event.

    The decide loop, the finalize and the render of one turn. The
    ``surface.user_intent`` event is supplied externally;
    :func:`drive_turn` does NOT re-emit it. The effective ``turn_id``
    is read from ``user_intent_event.payload["turn_id"]``.

    The caller is responsible for emitting ``surface.user_intent``
    first: the daemon's submit handler writes it and its intent pump
    picks it up and drives, so a turn is never emitted twice and the
    watcher never loops on it.

    Steps (spec §3.4.1 multi-trigger loop):

    1. Call :func:`jarvis.decision.decide` with ``user_intent_event``
       as the first trigger. If it returns a :class:`ResponsePlan`,
       finalize immediately.
    2. Otherwise the decide() call paused on an action; poll the event
       log for the next ``action.result_observed`` /
       ``action.timeout_assumed`` / ``action.failed`` /
       ``action.cancelled`` row, re-enter decide() with that trigger,
       repeat.
    3. Release the turn's live action ids from the ``finally``.
    4. Record the Pre-emit token on a fresh :class:`SurfaceState`, then
       call :func:`jarvis.surface.cli_render.render_response` which
       routes the channel-split text across the L3 attention channel's
       physical surfaces (say / banner / stdout when attached) AND
       enforces the Pre-emit token check (canary H3) AND emits the
       audit ``surface.response_emitted`` event. When
       ``streaming_enabled=True`` (daemon path, ADR-0003 Step 2),
       render_response ALSO emits ``surface.response_open`` plus N
       ``surface.response_chunk`` rows BEFORE the audit
       ``surface.response_emitted``; the user transcript (lifted from
       ``user_intent_event.payload["transcript"]``) lands on the open
       payload as ``query``.

    The Pre-emit token check protects against a runtime that
    accidentally re-uses an old plan or fails to refresh the token —
    :class:`PreEmitTokenError` propagates out. It — and every other
    exception — still passes through the ``finally`` that releases this
    turn's live action_ids (ADR-0009 D4).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        user_intent_event: The already-emitted ``surface.user_intent``
            :class:`Event`. Its ``payload["turn_id"]`` is the
            canonical turn id used through this turn.
        available_surfaces: Passed through to
            :func:`jarvis.surface.cli_render.render_response`; daemon
            callers use ``frozenset()`` to suppress physical surfaces.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Explicit per-trigger wait timeout. ``None``
            (production) resolves per iteration via
            :func:`trigger_wait_budget`.
        streaming_enabled: Forwarded to
            :func:`jarvis.surface.cli_render.render_response`; the
            daemon watcher (ADR-0003 Step 2 Build 5) passes ``True``
            so the renderer emits the 3-event Inherent taxonomy.
            Default ``False`` preserves CLI single-emit semantics.
        suspend_when_waiting: Yield a connection-free checkpoint instead of
            occupying an input worker while L4 runs.
        continuation: Resume a previously suspended turn with a ready trigger
            or failure supplied by the runtime scheduler.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = str(user_intent_event.payload["turn_id"])
    timer = (
        continuation.timer
        if continuation is not None and continuation.timer is not None
        else turn_timing.TurnTimer(
            turn_id=effective_turn_id,
            channel=str(user_intent_event.payload.get("channel") or "") or None,
            trigger=user_intent_event.type,
            queue_ms=max(0, int(time.time() * 1000) - user_intent_event.ts_epoch_ms),
        )
    )
    record_realtime_trace(
        "intent_queue_accepted" if continuation is None else "turn_continuation_resumed",
        turn_id=effective_turn_id,
        source="runtime_drive_turn",
    )
    # memory.db: Allen's utterance lands before any routing (Tier 0 included).
    # The event uid is the record id, so a retried turn cannot double-write.
    # ADR-0016 D3: a Live delegation arrives with ``record_id`` because the
    # voice surface already wrote the row; that id is then what the prompt
    # note excludes, so the request appears once.
    memory = runtime.memory
    surface_record_id = user_intent_event.payload.get("record_id")
    memory_exclude_id = (
        surface_record_id
        if isinstance(surface_record_id, str) and surface_record_id
        else user_intent_event.event_uid
    )
    surface_wrote_row = memory_exclude_id != user_intent_event.event_uid
    # ADR 0062: a card's button is Allen's act, not his words; it writes no row.
    card_button = isinstance(user_intent_event.payload.get("confirmation_decision"), Mapping)
    # ADR 0271: a phone's sentence that goes on from earlier ones carries their whole thought as
    # its words; the rows they left are its own history already, so this turn's prompt skips them.
    folded_raw = user_intent_event.payload.get("folded")
    folded = [str(one) for one in folded_raw] if isinstance(folded_raw, list) else []
    # ADR 0272: the model request below goes at once; what leaves her waits until this time.
    output_hold = answer_hold.output_not_before(runtime.config.raw, user_intent_event)
    if memory is not None and continuation is None and not surface_wrote_row and not card_button:
        audio_ref = user_intent_event.payload.get("audio_artifact_ref")
        append_record(
            memory.db_path,
            record_id=user_intent_event.event_uid,
            source="allen",
            text=record_words(runtime.attachments, user_intent_event.payload),
            audio_path=audio_ref if isinstance(audio_ref, str) else None,
            attachments=record_attachments(runtime.attachments, user_intent_event.payload),
        )
    if memory is not None and continuation is None and not card_button:
        # ADR 0390: words sent from a topic are filed under it as soon as their row is there.
        file_in_topic(runtime, user_intent_event, memory_exclude_id)
    # ADR 0268: the same words again from the same device, while the turn that heard them first
    # is still answering, are kept as his row but start no second answer: this turn is cancelled
    # as superseded by that one the moment its run opens.
    if continuation is None and runtime.response_runs is not None:
        runs = runtime.response_runs
        twin = device_voice.repeat_of(
            runtime.runtime_paths.event_log,
            effective_turn_id,
            str(user_intent_event.payload.get("transcript", "")),
            window_s=SUPERSEDE_WINDOW_S,
            in_flight={run.turn_id for run in runs.open_runs()},
            usable=lambda turn: not (runs.turn_superseded(turn) or runs.turn_stopped(turn)),
        )
        if twin is not None:
            LOGGER.info("turn %s repeats turn %s; it starts no answer", effective_turn_id, twin)
            runs.mark_turns_superseded(
                [effective_turn_id], for_s=SUPERSEDE_WINDOW_S, by_turn=twin,
            )
    # docs/plans/replay-as-sent-proposal.md: this turn's message is kept as
    # sent for later histories; a card's button wrote no row to keep it under.
    record_sent_message: Callable[[str], None] | None = None
    if memory is not None and runtime.config.session.replay_sent:
        record_sent_message = (
            (lambda _text: None)
            if card_button or folded  # a thought of several rows replays from the rows
            else partial(record_sent, memory.db_path, memory_exclude_id)
        )
    # ADR-0008 Step 2 (Wave 4A) — open the durable ResponseRun before the
    # decide loop so the per-run request client, the terminal owner and the
    # L5 ids all name the same response. A continuation resumes the run it
    # suspended with.
    run_pair = None if continuation is not None else start_drive_turn_response(
        runtime,
        user_intent_event=user_intent_event,
        turn_id=effective_turn_id,
    )
    run: ResponseRun | None = continuation.run if continuation is not None else None
    terminalizer: ResponseTerminalizer | None = None
    stream_route: RoutineStreamRoute | None = None
    if run_pair is not None:
        run, terminalizer, stream_route = run_pair
    elif run is not None:
        terminalizer = ResponseTerminalizer(
            lambda: runtime.conn, close_after=False,
            committed_event_bus=runtime.committed_event_bus,
        )
    # One consistent read of memory.db for this turn's prompt: the history
    # block goes ahead of every per-turn note, the time line after them.
    with timer.part("memory"):
        memory_context = (
            render_context(
                memory.db_path, exclude_id=memory_exclude_id,
                since=runtime.config.session.history_since,
                recent=runtime.config.session.recent_records,
                context=runtime.config.session.context,
                raw_max_chars=runtime.config.session.context_raw_max_chars,
                ledger=timer.timed("ledger", runtime.ledger),
                places=timer.timed("places", runtime.places_block),
                pictures=timer.timed("pictures", history_pictures(
                    runtime, run.request_client if run is not None else runtime.llm_client,
                )),
                receipts=timer.timed("receipts", history_receipts(runtime)),
                also_exclude=folded,
            )
            if memory is not None
            else None
        )
    suspended = False
    answered = ""  # the answer text, for the note of a picture sent with this turn (ADR 0223)

    def _run_cancelled() -> bool:
        """Return whether this turn's ResponseRun has been cancelled."""
        return run is not None and run.cancellation_token.is_cancelled

    def _raise_if_cancelled(where: str) -> None:
        """Abort the turn when this run's cancel already won its terminal.

        Called at every point where the run's FSM would otherwise advance.
        The cancel path commits ``response.cancelled`` before it sets the
        token, so observing the token means the terminal is already durable
        and there is nothing left for this turn to write.
        """
        if run is not None and run.cancellation_token.is_cancelled:
            msg = (
                f"drive_turn: response run cancelled {where} "
                f"(turn_id={effective_turn_id!r})."
            )
            raise ResponseCancelledError(msg)

    response_trace_ids: dict[str, TraceValue] = (
        {}
        if run is None
        else {"response_id": run.response_id, "response_group_id": run.response_group_id}
    )
    record_realtime_trace(
        "response_started" if continuation is None else "response_resumed",
        turn_id=effective_turn_id,
        trigger_type=user_intent_event.type,
        **response_trace_ids,
    )
    # ADR-0009 D4 — every action this turn dispatches registers itself
    # in the L4 live set (`ToolRegistry.dispatch`). The release MUST run
    # even when the turn raises: a leaked action_id is one the supervisor
    # sweep will refuse to close for the life of the process, so a crashed
    # turn would permanently protect the very orphan it created.
    timer_token = turn_timing.bind(timer)
    try:
        _raise_if_cancelled("before the model")  # a stop that came before the run opened
        if continuation is not None:
            _raise_if_cancelled("before continuation")
            if continuation.failure is not None:
                raise continuation.failure  # noqa: TRY301 — resume through the same failure owner
            if run is not None:
                run.mark("generating")
        turn_system_prompt = runtime.system_prompt
        if runtime.plugin_connections is not None:
            turn_system_prompt += "\n\n" + runtime.plugin_connections.skills_prompt()
        connected_apps = (
            runtime.plugin_connections.connected_apps_line()
            if runtime.plugin_connections is not None
            else None
        )
        route_rules: list[str] = []
        if runtime.config.session.replay_sent:
            # What every turn would repeat rides the system prompt, cached once,
            # instead of each replayed state block; it follows the cached prefix.
            if stream_route is not None and stream_route.context.route == "spoken":
                route_rules.append(spoken_reply_rules(structured=stream_route.structured))
            if connected_apps is not None:
                route_rules.append(connected_apps)
                connected_apps = None
        decide_ctx = DecideContext(
            conn=runtime.conn,
            runtime_paths=runtime.runtime_paths,
            # ``ToolRegistry`` / ``ActionLifecycle`` satisfy the L3
            # Protocols structurally; the casts pin the boundary because
            # mypy's invariant generic stance over Protocol attribute
            # types treats the more-specific RawResult / LifecycleState
            # return types as a conflict.
            # ADR-0016 D6 — a Live delegation sees only read-only tools; the
            # shared registry is untouched, the view lives for this call.
            tool_registry=cast(
                "ToolRegistryLike",
                ReadOnlyToolRegistry(runtime.tool_registry)
                if (
                    user_intent_event.payload.get("channel") == "gpt_live"
                    or user_intent_event.payload.get("plugin_origin_channel") == "gpt_live"
                )
                else runtime.tool_registry,
            ),
            lifecycle=cast("LifecycleLike", runtime.lifecycle),
            # ADR-0008 D4 — this turn's provider identity is the run's own
            # immutable client, so two overlapping runs can never read each
            # other's last-call metadata. A turn without a run (a hand-built
            # runtime) uses the shared client.
            llm_client=run.request_client if run is not None else runtime.llm_client,
            # The profile rides the system prompt: stable across turns, so it
            # sits in the cached prefix rather than in the per-turn context.
            system_prompt=SplitSystem(
                f"{turn_system_prompt.rstrip()}\n\n{memory_context.profile}"
                if memory_context is not None and memory_context.profile
                else turn_system_prompt,
                "\n\n".join(route_rules),
            ),
            tier0_table=runtime.tier0_table,
            # ADR-0011 D4 — resolve-on-propose. Built fresh per turn (a
            # trivial closure) rather than stored on JarvisRuntime: it
            # closes over `runtime.conn`, which the JarvisRuntime fields
            # above already carry, so there is nothing to cache.
            entity_resolver=make_entity_resolver(runtime.conn, runtime.terminal_hub),
            # ADR-0012 §3 D4/V2 — confirmation TTL, config-overridable
            # via `confirmation.ttl_ms` so the live burn can shorten it.
            confirmation_ttl_ms=runtime.config.confirmation.ttl_ms,
            max_tool_iterations=max_tool_iterations(runtime.config, user_intent_event),
            # ADR-0012 §3 D6 — answer-path grammar, threaded the same
            # way tier0_table is threaded.
            confirm_grammar_table=runtime.confirm_grammar_table,
            words_client=runtime.confirmation_words,
            wave1_features=runtime.wave1_features,
            history=memory_context.history if memory_context is not None else (),
            time_note=memory_context.now if memory_context is not None else None,
            connected_apps=connected_apps,
            turn_device=turn_device(runtime, effective_turn_id),
            about_record=about_record(runtime, user_intent_event),
            topic=turn_topic(runtime, user_intent_event),
            live_context=(
                *live_lines(runtime.live_context),
                *whereabouts_lines(runtime, effective_turn_id),
            ),
            record_sent_message=record_sent_message,
            cancellation_checkpoint=run.check_cancelled if run is not None else None,
            request_admission=(
                partial(run.admit_request, runtime.conn) if run is not None else None
            ),
            output_not_before=output_hold,
            routine_stream=stream_route,
            read_snapshot=snapshot_reader(runtime),
            slow_results=runtime.response_flags.slow_results,
            surrogate_route=runtime.surrogate_route,
            oneshot=runtime.oneshot,
            # The same boot value as the system prompt's reply-language line.
            reply_language=runtime.config.reply_language,
            service_tier=voice_service_tier(runtime.config, user_intent_event),
            read_attachments=(
                None if runtime.attachments is None else runtime.attachments.load_many
            ),
            last_words=(
                note_records.last_words(memory.db_path, memory_exclude_id)
                if memory is not None and user_intent_event.payload.get("attachments")
                else ""
            ),
        )

        timer.device = decide_ctx.turn_device
        # SQLite row id of the surface.user_intent event — used as the
        # "after_id" anchor for the trigger poll loop.
        last_seen_id = (
            continuation.after_id if continuation is not None else latest_id(runtime.conn)
        )

        collected_events: list[Event] = list(continuation.events) if continuation else []
        trigger_event: Event = (
            continuation.trigger
            if continuation is not None and continuation.trigger is not None
            else user_intent_event
        )
        response_plan: ResponsePlan | None = None
        # Track the attention_channel from the final decide() iteration so
        # we can route the response to the right surfaces in Step 18.
        # Default ``"voice_notify"`` covers the flagship ADR-0002 scenario
        # when an L3 branch returns a plan without explicitly setting the
        # field; the L3 Attention Policy emits one of the 9 channels for
        # canonical branches today.
        final_attention_channel: str = "voice_notify"
        iterations = continuation.iterations if continuation is not None else 0
        streamed = False
        written_apart = False
        reaction: str | None = None  # ADR 0257: her <react> on the message that opened the turn
        last_gate_event_uid: str | None = None

        while response_plan is None and iterations < max_iterations:
            _raise_if_cancelled("before a decide iteration")
            iterations += 1
            with bind_action_admission(
                run.admission_guard if run is not None else contextlib.nullcontext,
            ):
                result = decide(trigger_event, decide_ctx)
            if trigger_event.type in {
                "action.failed", "action.timeout_assumed", "action.cancelled",
            }:
                mark_trigger_consumed(runtime.conn, trigger_event.event_uid, effective_turn_id)
            collected_events.extend(result.events_emitted)
            if result.stream_failure is not None and run is not None and terminalizer is not None:
                # ADR-0008 D3: the stream could not be finalized. The run
                # fails naming what it exposed, and a full-text correction
                # run continues that prefix for the same turn.
                failure = result.stream_failure
                terminalizer.fail(
                    run.facts,
                    reason=failure.reason,
                    retryable=False,
                    committed_prefix_hash=failure.committed_prefix_hash,
                )
                run.mark("failed")
                if runtime.response_runs is not None:
                    runtime.response_runs.unregister(run.response_id)
                correction = StreamCorrection(
                    corrects_response_id=run.response_id,
                    committed_prefix=committed_text_prefix(runtime.conn, run.response_id).text,
                )
                record_realtime_trace(
                    "routine_stream_correction_opened",
                    turn_id=effective_turn_id,
                    failed_response_id=run.response_id,
                    reason=failure.reason,
                )
                replacement = start_drive_turn_response(
                    runtime,
                    user_intent_event=user_intent_event,
                    turn_id=effective_turn_id,
                    correction=correction,
                )
                run, terminalizer, _ = replacement
                decide_ctx = replace(
                    decide_ctx,
                    llm_client=run.request_client,
                    routine_stream=None,
                    stream_correction=correction,
                    cancellation_checkpoint=run.check_cancelled,
                    request_admission=partial(run.admit_request, runtime.conn),
                )
                continue
            response_plan = result.response_plan
            if response_plan is not None:
                final_attention_channel = result.attention_channel
                streamed = result.route in {"casual_or_explanatory", "spoken"}
                written_apart = result.written_apart
                reaction = result.reaction
                last_gate_event_uid = result.last_gate_event_uid
                break

            # No final plan -> decide() paused on an action. Wait for the
            # next L4-side trigger event.
            # ADR-0009 D4 / F9 — the waiter is scoped to the actions THIS
            # turn dispatched. Read fresh each iteration: the action
            # that paused decide() was registered inside the call above.
            owned_action_ids = turn_action_ids(effective_turn_id)
            wait_timeout_s = trigger_wait_budget(trigger_timeout_s)
            record_realtime_trace(
                "action_wait_started",
                turn_id=effective_turn_id,
                action_count=len(owned_action_ids),
                timeout_s=wait_timeout_s,
            )
            if run is not None:
                _raise_if_cancelled("before the action wait")
                run.mark("waiting_action")
            if suspend_when_waiting:
                suspended = True
                raise TurnSuspended(WaitingTurn(  # noqa: TRY301 — scheduler handoff preserves finally ownership
                    intent=user_intent_event, run=run, after_id=last_seen_id,
                    action_ids=owned_action_ids, deadline=time.monotonic() + wait_timeout_s,
                    iterations=iterations, events=tuple(collected_events), timer=timer,
                ))
            try:
                next_event, last_seen_id = wait_for_next_trigger(
                    runtime.conn,
                    after_id=last_seen_id,
                    action_ids=owned_action_ids,
                    lifecycle=runtime.lifecycle,
                    timeout=wait_timeout_s,
                    cancelled=_run_cancelled if run is not None else None,
                )
            except TriggerWaitTimeout:
                record_realtime_trace(
                    "action_wait_completed",
                    turn_id=effective_turn_id,
                    outcome="bounded_timeout",
                    timeout_s=wait_timeout_s,
                )
                raise
            if run is not None:
                _raise_if_cancelled("while waiting on an action")
                run.mark("generating")
            action_id = event_action_id(next_event)
            record_realtime_trace(
                "action_wait_completed",
                turn_id=effective_turn_id,
                outcome="trigger_observed",
                trigger_type=next_event.type,
                action_id=action_id,
            )
            record_realtime_trace(
                "action_result_available",
                turn_id=effective_turn_id,
                action_id=action_id,
                result_source=next_event.type,
            )
            trigger_event = next_event

        if response_plan is None:
            if run is not None and terminalizer is not None:
                terminalizer.fail(
                    run.facts,
                    reason="turn_exhausted_iterations",
                    retryable=False,
                )
            msg = (
                f"drive_turn: exhausted max_iterations={max_iterations} without a final "
                f"ResponsePlan (turn_id={effective_turn_id!r})."
            )
            raise RuntimeBootstrapError(msg)  # noqa: TRY301 — the turn's own failure, not a helper's.

        # L5 emission (Step 18 — channel-split + multi-surface dispatch).
        # The Pre-emit token guard inside render_response() preserves the
        # canary H3 runtime check — calling record_pre_emit_token() then
        # render_response() in this order is the only legal path.
        # SurfaceState is allocated fresh per turn (spec §3.6.7 — local
        # surface state owns no truth and doesn't survive across turns)
        # and discarded once render_response returns the cleared state.
        primed_state = record_pre_emit_token(response_plan.response_hash)

        # ADR-0008 D1 — generation completion, not physical delivery, is the
        # ResponseRun's terminal condition, so the terminal is written BEFORE
        # render. The accepted consequence is pinned by
        # `test_completed_without_delivery_when_render_raises`: if render
        # then raises, the log holds response.completed with no
        # surface.response_emitted. The opposite ordering is unrecoverable —
        # it would let a cancel land after the words were already spoken.
        if run is not None and terminalizer is not None:
            # ADR 0053: no answer completes while Allen's words are coming in,
            # so his next sentence can still drop it unwritten and unheard.
            registry = runtime.response_runs
            if registry is not None:
                waiting_since = time.monotonic_ns()
                hold_ends = time.monotonic() + _ALLEN_TALKING_CEILING_S
                while (
                    not registry.wait_completion_allowed(0.05)
                    and time.monotonic() < hold_ends
                ):
                    _raise_if_cancelled("while Allen was talking")
                timer.hold("completion_hold", waiting_since)
            # ADR 0272: and not before the turn's own hold for his next words is past.
            waiting_since = time.monotonic_ns()
            wait_out_hold(output_hold, run.check_cancelled, "while Allen may go on")
            timer.hold("answer_hold", waiting_since)
            _raise_if_cancelled("before finalizing")
            run.mark("finalizing")
            completion = terminalizer.complete(
                run.facts,
                response_hash=response_plan.response_hash,
            )
            if isinstance(completion, AlreadyTerminal):
                # A cancel won the CAS between the check above and this
                # append. Nothing is rendered — the words must not be spoken
                # for a response the operator already stopped — and the turn
                # ends the same way every other cancelled turn does, so the
                # daemon watcher does not record it as a completed answer.
                msg = (
                    "drive_turn: response run reached "
                    f"{completion.event.type} before its completion CAS "
                    f"(turn_id={effective_turn_id!r})."
                )
                raise ResponseCancelledError(msg)  # noqa: TRY301 — the turn's own outcome, not a helper's.
            run.mark("completed")
            if streamed:
                # The finalizer wrote nothing; the turn closes after the
                # terminal, sourced on the last stream gate verdict.
                collected_events.append(
                    emit_turn_ended(
                        runtime.conn,
                        turn_id=effective_turn_id,
                        final_response_hash=response_plan.response_hash,
                        consumed_trigger_event_uid=user_intent_event.event_uid,
                        source_event_id=last_gate_event_uid or run.facts.started_event_uid,
                    ),
                )

        timer.mark("first_chunk")  # a streamed answer's first chunk went out long before this
        # The in-memory capture stream serves two ends at once. First the
        # operator console: render_response() writes the cli_stdout slice
        # into it so the runtime can re-emit the same bytes to the real
        # sys.stdout. Second the test / programmatic caller: the returned
        # RunTurnResult.response_text is the captured string. Beyond
        # stdout, render_response also routes voice text to say and
        # document text to the notify banner per the channel mapping, and
        # emits the audit surface.response_emitted event itself.
        capture: io.StringIO = io.StringIO()
        transcript_raw = user_intent_event.payload.get("transcript", "")
        query = transcript_raw if isinstance(transcript_raw, str) else ""
        _, render_event = render_response(
            primed_state,
            response_plan,
            conn=runtime.conn,
            turn_id=effective_turn_id,
            attention_channel=final_attention_channel,
            stream=capture,
            available_surfaces=available_surfaces,
            streaming_enabled=streaming_enabled,
            query=query,
            response_id=run.response_id if run is not None else None,
            response_group_id=run.response_group_id if run is not None else None,
            delivery_terminal_only=streamed,
            written_apart=written_apart,
            times=turn_times(runtime.conn, effective_turn_id),
            places=turn_places(runtime.conn, effective_turn_id),
            links=turn_links(
                runtime.conn, effective_turn_id, partial(about_row, runtime, user_intent_event),
            ),
        )
        rendered = capture.getvalue()
        sys.stdout.write(rendered)
        sys.stdout.flush()
        collected_events.append(render_event)
        timer.mark("rendered")
        emit_turn_timing(runtime.conn, timer)
        if memory is not None:
            # ADR 0063: the one message this turn read whole shows above the answer.
            mail = fetched_mail(runtime.conn, effective_turn_id)
            if mail is not None:
                append_record(memory.db_path, record_id=mail[0], source="mail", text=mail[1])
            # The full answer text, not the spoken form (ADR 0040), which
            # lives only in the voice channel; the audit event's uid is the
            # record id. A written part that only adds to the spoken part
            # (ADR 0114) is not the full answer: both go in.
            answer = str(
                render_event.payload.get("document_text")
                or render_event.payload.get("text", ""),
            )
            if render_event.payload.get("written_apart") is True:
                answer = f"{render_event.payload.get('voice_text', '')}\n\n{answer}"
            answered = answer
            # ADR 0257: a typed reply that is only her reaction has no words to keep as a row.
            reaction_only = (
                reaction is not None and not answer.strip() and typed_turn(user_intent_event)
            )
            if not reaction_only:
                append_record(
                    memory.db_path,
                    record_id=render_event.event_uid,
                    source="jarvis",
                    text=answer,
                )
                file_in_topic(runtime, user_intent_event, render_event.event_uid)
            if reaction is not None and not card_button:
                record_reaction(runtime, memory.db_path, memory_exclude_id, reaction, render_event)
            if (
                runtime.response_flags.prefix_warm
                and run is not None
                and user_intent_event.payload.get("channel") != "gpt_live"
            ):
                spoken = stream_route is not None and stream_route.context.route == "spoken"
                warm_next_prefix(
                    runtime,
                    memory,
                    llm_client=run.request_client,
                    system_prompt=decide_ctx.system_prompt,
                    responses=spoken or run.request_client.preset_snapshot.api == "responses",
                    text_format=(
                        SPOKEN_REPLY_FORMAT
                        if spoken and stream_route is not None and stream_route.structured
                        else None
                    ),
                )
        record_realtime_trace(
            "response_completed",
            turn_id=effective_turn_id,
            iterations=iterations,
            gate_mode=response_plan.required_gate_mode,
        )

        return RunTurnResult(
            response_text=rendered,
            response_plan=response_plan,
            turn_id=effective_turn_id,
            iterations=iterations,
            events_emitted=tuple(collected_events),
            attention_channel=final_attention_channel,
        )
    except TurnSuspended:
        raise
    except ResponseCancelledError:
        # The cancel caller already committed response.cancelled. Re-raise so
        # the daemon watcher can tell a cancelled turn from a failed one.
        raise
    except Exception:
        if run is not None and terminalizer is not None:
            # The turn's own exception is the one the caller must see. A CAS
            # that cannot reach the log here leaves the run open, and the boot
            # reconciler closes it as daemon_restart — losing the terminal is
            # recoverable, masking the real failure is not.
            try:
                terminalizer.fail(
                    run.facts,
                    reason="turn_raised",
                    retryable=False,
                    committed_prefix_hash=terminalizer.committed_prefix_hash(run),
                )
            except Exception:
                LOGGER.exception(
                    "drive_turn: could not write response.failed (response_id=%r)",
                    run.response_id,
                )
        raise
    finally:
        turn_timing.release(timer_token)
        if not suspended:
            if run is not None and runtime.response_runs is not None:
                runtime.response_runs.unregister(run.response_id)
            release_turn_actions(effective_turn_id)
            queue_picture_notes(runtime, user_intent_event, memory_exclude_id, answered)
