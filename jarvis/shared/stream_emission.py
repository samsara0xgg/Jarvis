"""Immutable L3/L2/L5 contracts for a durably authorized text segment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type SegmentRisk = Literal["routine", "consequential_claim", "high_risk_claim", "unknown"]
type StreamDisposition = Literal["permit", "buffer_full_text", "refuse"]
type StreamPhase = Literal["commentary", "final"]
type StreamChannel = Literal["speech", "document", "both"]


@dataclass(frozen=True, kw_only=True)
class StreamGateAssessment:
    """An L3 verdict to commit; this object alone cannot authorize output."""

    response_id: str
    sequence: int
    phase: StreamPhase
    channel: StreamChannel
    segment_hash: str
    policy_hash: str
    evidence_snapshot_hash: str
    risk_context_hash: str
    classifier_rule_version: str
    attention_channel: str
    candidate_risk: SegmentRisk
    outcome: StreamDisposition
    reasons: tuple[str, ...]
    classification_elapsed_ms: float


@dataclass(frozen=True, kw_only=True)
class EmissionPermit:
    """Receipt issued after gate commit, revalidated against L2 at consumption."""

    response_id: str
    segment_sequence: int
    phase: StreamPhase
    channel: StreamChannel
    segment_hash: str
    policy_hash: str
    gate_mode: Literal["sentence"]
    source_gate_event_uid: str
    issued_at_ms: int
