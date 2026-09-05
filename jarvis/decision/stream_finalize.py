"""L3 stream finalizer (ADR-0008 D3): what was spoken stays, only the tail is judged."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jarvis.decision.gates import ResponsePlan
from jarvis.decision.stream_risk import SegmentRiskClassifier
from jarvis.state.stream_emission import StreamEmissionError, committed_text_prefix

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.response_run import ResponseEmissionPolicy
    from jarvis.decision.stream_risk import ResponseRiskContext

type StreamFinalizationReason = Literal[
    "suffix_rejected",
    "committed_prefix_invalid",
    "policy_mismatch",
]


@dataclass(frozen=True)
class StreamFinalizationFailure:
    """A typed refusal; the caller fails or corrects the run, never regenerates the prefix.

    ``committed_prefix_hash`` is always the hash of the durable prefix, so a
    ``response.failed``/``response.cancelled`` written from it names what was
    actually spoken even when the caller's own prefix was wrong.
    """

    response_id: str
    reason: StreamFinalizationReason
    committed_prefix_hash: str
    gate_reasons: tuple[str, ...]


def finalize_stream(  # noqa: PLR0913 - explicit immutable finalizer inputs
    conn: sqlite3.Connection,
    *,
    committed_prefix: str,
    uncommitted_suffix: str,
    policy: ResponseEmissionPolicy,
    context: ResponseRiskContext,
    classifier: SegmentRiskClassifier | None = None,
) -> ResponsePlan | StreamFinalizationFailure:
    """Bind the durable prefix to an approved suffix, writing nothing.

    The prefix is validated against the Event Log under the policy the permits
    were issued with; only the suffix is classified, so a rejection can be
    answered by regenerating the suffix alone. The returned plan's text is
    byte-for-byte ``committed_prefix + uncommitted_suffix``.
    """
    if (
        policy.emission_mode != "routine_stream"
        or policy.risk_context_hash != context.context_hash
        or policy.evidence_snapshot_hash != context.evidence_snapshot_hash
        or policy.active_subject_ref != context.active_subject_ref
    ):
        message = "stream finalizer context differs from pinned routine stream policy"
        raise StreamEmissionError(message)
    durable = committed_text_prefix(conn, context.response_id)
    if policy.policy_hash != durable.policy_hash:
        return StreamFinalizationFailure(
            context.response_id,
            "policy_mismatch",
            durable.prefix_hash,
            ("policy_hash_differs_from_committed_permits",),
        )
    if committed_prefix != durable.text:
        return StreamFinalizationFailure(
            context.response_id,
            "committed_prefix_invalid",
            durable.prefix_hash,
            ("committed_prefix_differs_from_durable_chain",),
        )
    if uncommitted_suffix.strip():
        result = (classifier or SegmentRiskClassifier()).classify(uncommitted_suffix, context)
        if result.risk != "routine":
            return StreamFinalizationFailure(
                context.response_id,
                "suffix_rejected",
                durable.prefix_hash,
                (*result.reasons, "routine_ceiling_not_met"),
            )
    text = committed_prefix + uncommitted_suffix
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )
