"""Starting a turn: its id and the durable ResponseRun it opens under."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from jarvis.decision.packet import assemble_packet
from jarvis.decision.pre_route import (
    ROUTINE_ATTENTION_CHANNEL,
    RoutineStreamRoute,
    StreamCorrection,
    pre_route,
    routine_risk_context,
    spoken_risk_context,
    spoken_turn,
    typed_turn,
)
from jarvis.decision.response_run import (
    ResponseCancelRequest,
    ResponseRun,
    ResponseRunRegistry,
    ResponseTerminalizer,
    evidence_snapshot_hash,
    legacy_full_text_policy,
    request_response_cancel,
    start_response_run,
)
from jarvis.decision.stream_gate import routine_stream_policy, spoken_stream_policy
from jarvis.runtime.stream_bridge import LoopBoundTokenStream
from jarvis.runtime.turn.context import snapshot_reader
from jarvis.shared.realtime import new_response_id
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.stream_emission import emit_permitted_segment

if TYPE_CHECKING:

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event


def labels_phases(base_url: str | None) -> bool:
    """Whether the host labels each message ``commentary`` or ``final_answer``.

    Only OpenAI's own /v1/responses does. On a compatible host (the x.ai
    presets) the line before a call would stream as the answer.
    """
    return "api.openai.com" in (base_url or "api.openai.com")


def _late_cancel_reason(registry: ResponseRunRegistry, run: ResponseRun) -> str | None:
    """Why a run that just opened is already cancelled: a stop or a newer sentence came first."""
    if registry.turn_stopped(run.turn_id):
        return "user_stop"
    if (
        run.phase == "final"
        and run.interrupt_policy.generation_action == "cancel"
        and registry.turn_superseded(run.turn_id)
    ):
        return "superseded"
    return None


def start_drive_turn_response(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    turn_id: str,
    correction: StreamCorrection | None = None,
) -> tuple[ResponseRun, ResponseTerminalizer, RoutineStreamRoute | None]:
    """Open one durable ResponseRun for this turn.

    ``runtime`` here is ``drive_turn``'s own runtime — in the daemon that is
    the ``dataclasses.replace(conn=worker_conn)`` copy, so the run's
    ``BEGIN IMMEDIATE`` lands on the worker thread's own connection and never
    on the asyncio event loop's.

    ADR-0008 Step 8: with ``routine_streaming`` on, the route is decided here,
    before the run opens, and a ``casual_or_explanatory`` turn opens under
    ``routine_stream_policy`` with the streaming seam bound. With
    ``spoken_streaming`` on, a turn Allen spoke or typed opens as ``spoken``
    under ``spoken_stream_policy`` first (a typed one is never played, ADR 0181).
    Every other turn (and every correction run) keeps ``legacy_full_text_policy``.
    """
    response_id = new_response_id()
    think = runtime.think_mode
    preset = think.preset_for(runtime.conn, user_intent_event) if think else None
    snapshot = runtime.llm_session_factory.snapshot(preset, think=preset is not None)
    request_client = runtime.llm_session_factory.create(snapshot, response_id=response_id)
    policy = legacy_full_text_policy(
        evidence_snapshot_hash=evidence_snapshot_hash(runtime.conn),
        preset_snapshot_hash=snapshot.snapshot_hash,
    )
    route: str | None = None
    context = None
    if (
        runtime.response_flags.spoken_streaming
        and correction is None
        and (spoken_turn(user_intent_event) or typed_turn(user_intent_event))
        and (
            snapshot.provider == "anthropic"
            or (snapshot.provider == "openai" and labels_phases(snapshot.base_url))
        )
    ):
        # docs/plans/speak-as-written-proposal.md: a turn Allen spoke streams
        # every request through /v1/responses, whose phase labels tell the line
        # before a call from the answer, and speaks the answer as it is written.
        # Anthropic labels none: a line it writes before a call is read as part of the
        # answer, and a structured reply has no place for one.
        route = "spoken"
        context = spoken_risk_context(
            assemble_packet(user_intent_event, runtime.conn, snapshot_reader(runtime)),
            response_id=response_id,
            turn_id=turn_id,
        )
        policy = spoken_stream_policy(context, preset_snapshot_hash=snapshot.snapshot_hash)
    elif runtime.response_flags.routine_streaming and correction is None:
        packet = assemble_packet(user_intent_event, runtime.conn, snapshot_reader(runtime))
        route = pre_route(
            packet,
            tier0_table=runtime.tier0_table,
            tool_cues=runtime.tool_cues,
            now_ms=int(time.time() * 1000),
        )
        if route == "casual_or_explanatory":
            context = routine_risk_context(packet, response_id=response_id, turn_id=turn_id)
            routine = routine_stream_policy(context, preset_snapshot_hash=snapshot.snapshot_hash)
            if routine.emission_mode == "routine_stream":
                policy = routine
            else:
                # The risk floor of the request itself refused; D2 rule 1.
                route, context = "unknown", None
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=user_intent_event.event_uid,
        request_client=request_client,
        policy=policy,
        response_id=response_id,
        committed_event_bus=runtime.committed_event_bus,
        corrects_response_id=correction.corrects_response_id if correction else None,
        route=route,
    )
    terminalizer = ResponseTerminalizer(
        lambda: runtime.conn,
        close_after=False,
        committed_event_bus=runtime.committed_event_bus,
    )
    if runtime.response_runs is not None:
        runtime.response_runs.register(run)
        # Checked after registering, so a stop (or a sweep, ADR 0074) landing at
        # any moment either sees the run or is seen here.
        late_cancel = _late_cancel_reason(runtime.response_runs, run)
        if late_cancel is not None:
            request_response_cancel(
                runtime.response_runs,
                terminalizer,
                ResponseCancelRequest(
                    request_id="CREQ" + uuid.uuid4().hex,
                    response_id=run.response_id,
                    scope="generation",
                    reason=late_cancel,
                    source_turn_id=(
                        runtime.response_runs.superseded_by(run.turn_id)
                        if late_cancel == "superseded"
                        else None
                    ),
                ),
            )
    if context is None:
        return run, terminalizer, None
    transcript_raw = user_intent_event.payload.get("transcript", "")
    query = transcript_raw if isinstance(transcript_raw, str) else ""
    seam = RoutineStreamRoute(
        policy=policy,
        context=context,
        open_stream=lambda handle: LoopBoundTokenStream(
            handle, cancellation_token=run.cancellation_token,
        ),
        emit_segment=lambda permit, text: emit_permitted_segment(
            runtime.conn,
            permit,
            text,
            query=query,
            attention_channel=ROUTINE_ATTENTION_CHANNEL,
            committed_event_bus=runtime.committed_event_bus,
        ).event,
        segment_guard=run.admission_guard,
        committed_event_bus=runtime.committed_event_bus,
        first_clause_chars=(
            runtime.response_flags.spoken_first_clause_chars if route == "spoken" else 0
        ),
        structured=(
            route == "spoken"
            and runtime.response_flags.spoken_structured
            and snapshot.spoken_structured
        ),
    )
    record_realtime_trace(
        "routine_stream_route_opened", turn_id=turn_id, response_id=response_id,
    )
    return run, terminalizer, seam


def new_turn_id() -> str:
    """Mint a fresh turn id (``"T" + 8-hex``)."""
    return "T" + uuid.uuid4().hex[:8]
