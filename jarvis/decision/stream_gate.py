"""L3 stream gate: classify, durably audit, then issue a text-bound receipt."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.decision.response_run import ResponseEmissionPolicy
from jarvis.decision.stream_risk import RULE_VERSION, ResponseRiskContext, SegmentRiskClassifier
from jarvis.shared.stream_emission import EmissionPermit, StreamGateAssessment
from jarvis.state.stream_emission import StreamEmissionError, append_stream_gate

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.stream_sentences import SemanticCandidate
    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.lifecycle_terminal import FailureInjector


@dataclass(frozen=True)
class StreamGateOutcome:
    """Durable assessment and optional committed permission; never raw speculation."""

    event: Event
    permit: EmissionPermit | None


def routine_stream_policy(
    context: ResponseRiskContext,
    *,
    preset_snapshot_hash: str,
) -> ResponseEmissionPolicy:
    """Pin the conservative route before provider I/O; unknown context buffers."""
    classifier = SegmentRiskClassifier()
    # A neutral candidate cannot lower the complete user/context risk floor.
    risk = classifier.classify("这是普通解释。", context).risk
    return ResponseEmissionPolicy(
        emission_mode="routine_stream" if risk == "routine" else "full_text",
        output_risk_class=risk,
        required_gate_mode="sentence" if risk == "routine" else "full_text",
        allowed_phases=("final",),
        allowed_channels=("speech", "document", "both"),
        active_subject_ref=context.active_subject_ref,
        evidence_snapshot_hash=context.evidence_snapshot_hash,
        preset_snapshot_hash=preset_snapshot_hash,
        risk_context_hash=context.context_hash,
        classifier_rule_version=RULE_VERSION,
    )


def stream_emission_gate(  # noqa: PLR0913 - explicit immutable gate inputs
    conn: sqlite3.Connection,
    *,
    policy: ResponseEmissionPolicy,
    context: ResponseRiskContext,
    segment: SemanticCandidate,
    sequence: int,
    phase: str,
    channel: str,
    classifier: SegmentRiskClassifier | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> StreamGateOutcome:
    """Return a usable permit only after its complete assessment commits.

    Once buffered, L2 refuses any later permission for this response, including
    attempts through a new in-memory gate instance. Caller errors fail before
    evaluation; candidate denials become durable buffer/refuse assessments.
    """
    if (
        policy.risk_context_hash != context.context_hash
        or policy.evidence_snapshot_hash != context.evidence_snapshot_hash
        or policy.active_subject_ref != context.active_subject_ref
        or phase not in {"commentary", "final"}
        or channel not in {"speech", "document", "both"}
    ):
        message = "stream gate context or output scope differs from pinned policy"
        raise StreamEmissionError(message)
    result = (classifier or SegmentRiskClassifier()).classify(segment.text, context)
    candidate_shape_valid = (
        segment.boundary in {"sentence", "subclause", "final"}
        and bool(segment.text.strip())
        and len(segment.text) <= (60 if channel in {"speech", "both"} else 2048)
    )
    eligible = (
        result.risk == "routine"
        and policy.emission_mode == "routine_stream"
        and policy.output_risk_class == "routine"
        and policy.required_gate_mode == "sentence"
        and phase in policy.allowed_phases
        and channel in policy.allowed_channels
        and policy.classifier_rule_version == result.rule_version == RULE_VERSION
        and candidate_shape_valid
    )
    reasons = result.reasons if eligible else (*result.reasons, "routine_ceiling_not_met")
    if not candidate_shape_valid:
        reasons = (*reasons, "invalid_candidate_shape_or_bound")
    assessment = StreamGateAssessment(
        response_id=context.response_id,
        sequence=sequence,
        phase="commentary" if phase == "commentary" else "final",
        channel="speech"
        if channel == "speech"
        else "document"
        if channel == "document"
        else "both",
        segment_hash=hashlib.sha256(segment.text.encode()).hexdigest(),
        policy_hash=policy.policy_hash,
        evidence_snapshot_hash=context.evidence_snapshot_hash,
        risk_context_hash=context.context_hash,
        classifier_rule_version=policy.classifier_rule_version,
        attention_channel=context.attention_channel,
        candidate_risk=result.risk,
        outcome="permit" if eligible else "buffer_full_text",
        reasons=reasons,
        classification_elapsed_ms=result.elapsed_ms,
    )
    event = append_stream_gate(
        conn,
        assessment,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
    )
    permit = None
    if event.payload["outcome"] == "permit":
        permit = EmissionPermit(
            response_id=context.response_id,
            segment_sequence=sequence,
            phase=assessment.phase,
            channel=assessment.channel,
            segment_hash=assessment.segment_hash,
            policy_hash=policy.policy_hash,
            gate_mode="sentence",
            source_gate_event_uid=event.event_uid,
            issued_at_ms=event.ts_epoch_ms,
        )
    return StreamGateOutcome(event, permit)
