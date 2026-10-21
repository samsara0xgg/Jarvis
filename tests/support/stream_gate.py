"""A streamed answer run through the emission gate, with a risk context."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.packet import assemble_packet
from jarvis.decision.response_run import ResponseRun, ResponseTerminalizer, start_response_run
from jarvis.decision.stream_gate import (
    StreamGateOutcome,
    routine_stream_policy,
    stream_emission_gate,
)
from jarvis.decision.stream_risk import ResponseRiskContext, snapshot_content_hash
from jarvis.decision.stream_sentences import SemanticCandidate
from jarvis.state.event_log import emit_event
from jarvis.surface.stream_emission import emit_permitted_segment

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.shared.stream_emission import EmissionPermit

QUERY = "解释冰为什么融化。"


TEXT = "冰从周围吸收热量。"


def risk_context(**changes: Any) -> ResponseRiskContext:  # noqa: ANN401 - typed dataclass replacement table
    base = ResponseRiskContext(
        response_id="response-stream-test",
        turn_id="turn-stream-test",
        user_request=QUERY,
        route="casual_or_explanatory",
        active_subject_ref="none",
        linked_action_ids=(),
        pending_action_risk="none",
        confirmation_state="none",
        evidence_snapshot_hash="a" * 64,
        attention_channel="voice_notify",
        tools_offered=False,
        context_complete=True,
        unresolved_references=False,
        history_status="complete",
    )
    return replace(base, **changes)


class GateRun:
    def __init__(
        self,
        conn: sqlite3.Connection,
        context: ResponseRiskContext,
        llm_config: dict[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={
                "transcript": context.user_request,
                "turn_id": context.turn_id,
            },
        )
        packet = assemble_packet(trigger, conn)
        self.context = replace(context, evidence_snapshot_hash=snapshot_content_hash(packet))
        self.policy = routine_stream_policy(self.context, preset_snapshot_hash="b" * 64)
        factory = LLMSessionFactory(
            llm_config
            if llm_config is not None
            else {
                "provider": "openai",
                "model": "synthetic",
                "base_url": "https://example.invalid",
            }
        )
        self.run: ResponseRun = start_response_run(
            conn,
            turn_id=context.turn_id,
            trigger_event_uid=trigger.event_uid,
            request_client=factory.create(factory.snapshot(), response_id=context.response_id),
            policy=self.policy,
            response_id=context.response_id,
        )
        self.terminalizer = ResponseTerminalizer(lambda: conn, close_after=False)

    def gate(
        self,
        text: str = TEXT,
        sequence: int = 0,
        **kwargs: Any,  # noqa: ANN401 - forward real transaction failure fixtures
    ) -> StreamGateOutcome:
        return stream_emission_gate(
            self.conn,
            policy=self.policy,
            context=self.context,
            segment=SemanticCandidate(text, "sentence"),
            sequence=sequence,
            phase="final",
            channel="both",
            **kwargs,
        )

    def emit(
        self,
        permit: EmissionPermit,
        text: str = TEXT,
        **kwargs: Any,  # noqa: ANN401 - forward real transaction failure fixtures
    ) -> Event:
        return emit_permitted_segment(
            self.conn,
            permit,
            text,
            query=self.context.user_request,
            attention_channel=self.context.attention_channel,
            **kwargs,
        ).event
