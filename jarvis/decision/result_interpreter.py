"""L3 Post-action Gate — Result Interpreter.

Per ADR 0001 § Gate contracts (Result Interpreter table). The mapping
``result_semantics -> (ClaimType, EvidenceLevel)`` is borrowed from
legacy ``core/tool_result.py`` ``claim_policy`` / ``outcome.type``
machinery, simplified to the Day-1 vocabulary the ADR locks in:

    | result_semantics | claim type    | evidence level |
    |------------------|---------------|----------------|
    | ack              | Execution     | executed       |
    | observation      | Artifact      | observed       |
    | verification     | Postcondition | verified       |
    | report           | Report        | reported       |
    | error            | Limitation    | reported       |

Constraint per ADR Acceptance E3: never emit an ``evidence.level``
exceeding the row's allowed value for the source semantics. The
interpreter is the single point of enforcement; gating in
``decide()`` is downstream.

Crucially per ADR § Acceptance A8: ``claim.created`` and
``evidence.attached`` are SEPARATE events (each its own row).

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
from sibling layers and no import of ``jarvis.decision.llm``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import (
        ActionRequest,
        ClaimType,
        Event,
        EvidenceLevel,
        RawResult,
        ResultSemantics,
    )


# Step 0b note: ``RawResult`` and ``ResultSemantics`` now live in
# ``jarvis.shared`` (below L3 in the layer DAG), so the Day-1
# ``RawResult`` Protocol — needed when ``RawResult`` lived in L4 and L3
# could not import it — is no longer required. L4 handlers continue to
# return ``RawResult`` directly; the Result Interpreter consumes the same
# type without a structural shim.


# --- Semantics -> (ClaimType, EvidenceLevel) table -------------------------

_SEMANTICS_TO_CLAIM: dict[ResultSemantics, tuple[ClaimType, EvidenceLevel]] = {
    "ack": ("Execution", "executed"),
    "observation": ("Artifact", "observed"),
    "verification": ("Postcondition", "verified"),
    "report": ("Report", "reported"),
    "error": ("Limitation", "reported"),
}

# Default statement templates per semantics. Day-1 keeps them short and
# stable so canary tests can pattern-match if needed; Stage 2 will move
# them into the prompt asset.
_STATEMENT_TEMPLATES: dict[ResultSemantics, str] = {
    "ack": "tool {tool_name} acknowledged dispatch for {subject_ref}",
    "observation": "tool {tool_name} observed state of {subject_ref}",
    "verification": "{tool_name} predicate satisfied for {subject_ref}",
    "report": "worker reported on {subject_ref}",
    "error": "tool {tool_name} reported limitation: {error}",
}


# --- result_interpreter -----------------------------------------------------


def _evidence_payload_extras(raw: RawResult) -> dict[str, Any]:
    """Pull ``artifact_path`` / ``content_hash`` / ``scope`` from raw.payload.

    These optional fields surface in the ``evidence.attached`` payload so
    Stage 2 consumers (and Acceptance E2's artifact-path check) can read
    them without re-running the tool.
    """
    extras: dict[str, Any] = {}
    if "artifact_path" in raw.payload:
        extras["artifact_path"] = raw.payload["artifact_path"]
    if "content_hash" in raw.payload:
        extras["content_hash"] = raw.payload["content_hash"]
    if "scope" in raw.payload:
        extras["scope"] = raw.payload["scope"]
    return extras


def _render_statement(
    semantics: ResultSemantics,
    raw: RawResult,
    action_request: ActionRequest,
) -> str:
    """Render a short statement string for the claim payload."""
    template = _STATEMENT_TEMPLATES[semantics]
    return template.format(
        tool_name=action_request.tool_name,
        subject_ref=action_request.target_entity_ref or "unknown",
        error=raw.error or "unknown",
    )


def result_interpreter(  # noqa: PLR0913 — Result Interpreter signature is a public contract per ADR § Gate contracts.
    raw: RawResult,
    source_event_id: str,
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    *,
    subject_ref_override: str | None = None,
    statement_override: str | None = None,
) -> tuple[Event, ...]:
    """Emit ``claim.created`` + ``evidence.attached`` for one RawResult.

    Per ADR § Gate contracts (Post-action / Result Interpreter):

    - Maps ``raw.semantics`` to ``(ClaimType, EvidenceLevel)`` per the
      table above. Never emits ``evidence.level`` exceeding the row.
    - Each emitted event is its own row (ADR § Acceptance A8: no
      lumping).
    - Sets ``source_event_id`` on the ``claim.created`` event to
      point back to the ``action.result_observed`` (or ``worker.reported``)
      that produced ``raw``. The ``evidence.attached`` event sets
      ``source_event_id`` to the freshly-emitted ``claim.created``
      so the canary H10 cause-chain check sees a clean DAG.

    Args:
        raw: One RawResult from L4 (or synthesized from a
            ``worker.reported`` payload).
        source_event_id: ``event_uid`` of the upstream event the
            Result Interpreter is reacting to. Usually the
            ``action.result_observed`` row; for the
            ``worker.reported`` re-entry it is that event's uid.
        action_request: The originating ActionRequest, used to
            shape statement + subject_ref defaults.
        conn: Open Event Log connection (single thread of execution
            in ``decide()``).
        subject_ref_override: When non-None, used as the claim's
            ``subject_ref`` (Day-1 callers pass the canonical task_id
            after Resolver returns).
        statement_override: When non-None, used as the claim's
            ``statement`` text. Otherwise rendered from the template
            for the matched semantics.

    Returns:
        Tuple ``(claim_event, evidence_event)`` of the two emitted
        events, in emission order. ``decide()`` can include these in
        ``DecideResult.events_emitted``.
    """
    claim_type, evidence_level = _SEMANTICS_TO_CLAIM[raw.semantics]

    subject_ref = subject_ref_override or action_request.target_entity_ref or raw.action_id
    statement = statement_override or _render_statement(raw.semantics, raw, action_request)

    claim_id = "C" + uuid.uuid4().hex[:8]
    evidence_id = "E" + uuid.uuid4().hex[:8]

    correlation = _build_correlation(action_request)

    claim_event = emit_event(
        conn,
        type="claim.created",
        payload={
            "claim_id": claim_id,
            "type": claim_type,
            "statement": statement,
            "subject_ref": subject_ref,
            "produced_by_event_id": source_event_id,
        },
        source_event_id=source_event_id,
        correlation=correlation,
    )

    evidence_payload: dict[str, Any] = {
        "evidence_id": evidence_id,
        "claim_id": claim_id,
        "level": evidence_level,
    }
    evidence_payload.update(_evidence_payload_extras(raw))

    evidence_event = emit_event(
        conn,
        type="evidence.attached",
        payload=evidence_payload,
        source_event_id=claim_event.event_uid,
        correlation=correlation,
    )

    return (claim_event, evidence_event)


def _build_correlation(action_request: ActionRequest) -> Mapping[str, str]:
    """Build the standard ``{action_id, run_id?, turn_id?}`` correlation."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


__all__ = [
    "result_interpreter",
]
