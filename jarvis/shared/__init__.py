"""Cross-layer typed contracts — minimal, immutable, no business logic.

Every layer above (L2..L6 plus runtime / cli) imports types from here. This module
imports nothing from `jarvis.*` (sibling-of-constitution at the bottom of the
layer DAG per `.importlinter`).

Shapes are derived from:
- spec.html §3.4.8  (ActionRequest skeleton + ghost-action prevention)
- spec.html §3.5.2  (CallerPrincipal + AuthorizationLease)
- spec.html §5.1    (Event Log columns)
- spec.html §5.4    (Event Type Registry)
- spec.html §8      (Claim / Evidence Model)
- ADR 0001 § Module map + § AuthorizationLease (Day-1 treatment)

Day-1 minimum: just the types the L2..L6 wiring needs to compile. Higher-fidelity
fields (lease.max_uses, claim.required_for_completion, evidence.freshness, etc.)
are intentionally deferred — when a later step actually consumes them, add then.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping


# --- ResultSemantics (relocated from jarvis.execution.tools in Step 0b) -----

ResultSemantics = Literal["ack", "observation", "verification", "report", "error"]
"""Day-1 `result_semantics` vocabulary (ADR § Gate contracts, Result Interpreter table).

Relocated from :mod:`jarvis.execution.tools` in Step 0b of ADR-0002 so L3
(Result Interpreter) and L4 (handler return shape) can both reference it
without crossing the layer DAG.
"""


class CallerPrincipal(enum.Enum):
    """Real caller identity used by the Pre-action Gate (spec §3.5.2).

    Not a temporary capability grant — that lives on AuthorizationLease.
    Names mirror the canonical list in spec §3.5.2; legacy `tools_v2/registry.py`
    used string labels (`"llm"`, `"regex_router"`), which Day-1 replaces with
    this typed enum per ADR § Module map (`Set[CallerPrincipal]` everywhere).
    """

    OBSERVER = "observer"
    REGEX_ROUTER = "regex_router"
    JARVIS_LLM = "jarvis_llm"
    WORKER_AGENT = "worker_agent"
    BACKGROUND_SUBSCRIBER = "background_subscriber"
    SYSTEM_MAINTENANCE = "system_maintenance"


# --- Evidence Model literals (spec §8.4 / ADR § Gate contracts) -------------

EvidenceLevel = Literal["reported", "observed", "executed", "verified", "accepted"]
"""Evidence level ladder (spec §8.4).

Day-1 Result Interpreter maps ToolDefinition.result_semantics to one of these.
"""

ClaimStatus = Literal["open", "supported", "refuted", "limited", "superseded"]
"""Claim lifecycle status (spec §8.7 enum, exactly).

`accepted` is deliberately NOT a status — spec §8.7 has no such state.
`claim.accepted` (Allen's manual acceptance) folds as accepted-LEVEL
evidence (§8.4 ladder top) plus status `supported`; see
`jarvis.state.projections._fold_claim_evidence`.
"""

ClaimType = Literal[
    "Report",
    "Postcondition",
    "Execution",
    "Artifact",
    "State",
    "Limitation",
    "Refute",
]
"""Day-1 subset of claim types (spec §8.2 + ADR § Gate contracts).

ADR Result-Interpreter table allows: Execution / Artifact / State / Postcondition
/ Report / Limitation / Refute. Coverage and Acceptance claims are part of spec
§8.2 but not on the Day-1 trace, so they are not included in this Literal.
"""

RiskLevel = Literal["L0", "L1", "L2", "L3", "L4"]
"""Risk ladder shared with `constitution.AUTONOMY_LEVELS`.

ActionRequest.risk_level is compared against EffectivePolicy.autonomy_ceiling
in the Pre-action Gate; both speak this vocabulary.
"""


# --- Event Log row (spec §5.1 — payload-side dataclass form) ----------------


@dataclass(frozen=True)
class Event:
    """One row in the Event Log (spec §5.1).

    Frozen + hashable: the Event Log is append-only and projections fold over
    Event sequences, so Events must not mutate after emission. `payload` is
    typed as `Mapping` (read-only protocol) instead of `dict` so the contract
    surface forbids mutation; concrete storage is the caller's choice.

    Attributes:
        event_uid: UUIDv7-ish hex string. The SQLite row also has an integer
            PRIMARY KEY for ordering, but the application-side identifier is
            this `event_uid` so events stay stable across replay/rebuild.
        type: Canonical event type, e.g. "task.created". Must exist in the
            EventTypeRegistry (spec §5.4).
        schema_version: Registry-versioned schema (spec §5.4.1). Bumped on
            incompatible payload change.
        ts_epoch_ms: Wall-clock ms (used for ordering / range queries).
        payload: Event-specific data. Read-only mapping.
        source_event_id: UID of the upstream event that triggered this one;
            None for surface-originated events.
        correlation: Optional mapping of correlation keys (turn_id / run_id /
            action_id). Spec §5.1 stores a single `correlation_id` column;
            Day-1 carries the full map application-side and projects to the
            most-relevant column when persisted.
    """

    event_uid: str
    type: str
    schema_version: int
    ts_epoch_ms: int
    payload: Mapping[str, Any]
    source_event_id: str | None
    correlation: Mapping[str, str] | None


# --- Claim / Evidence (spec §8.6, §8.7 — Day-1 minimum) ---------------------


@dataclass(frozen=True)
class Claim:
    """A claim Jarvis is willing to record / act on (spec §8.1, §8.7).

    `status` is the §8.7 lifecycle state, derived by the projection fold
    from the correction events (`claim.refuted` / `claim.limited` /
    `claim.superseded` / `claim.accepted`) — never stored outside the
    fold, mirroring the H11 rule for task status. Consumers must treat
    `refuted` / `superseded` claims as inactive (they no longer support
    completion); `limited` claims stay active — a limitation qualifies,
    it does not veto (ADR-0002 reviewer-advisory precedent).

    `required_for_completion` (§8.8) remains deferred until the
    completion-rule consumer lands.
    """

    claim_id: str
    type: ClaimType
    statement: str
    subject_ref: str
    produced_by_event_id: str
    ts_epoch_ms: int
    status: ClaimStatus = "open"


@dataclass(frozen=True)
class Evidence:
    """Evidence supporting / refuting / limiting a Claim (spec §8.6).

    Day-1 carries the minimum needed by the Pre-emit Gate's
    `active_claim_levels` check. Relation / scope / freshness / artifact_ref
    live in `payload` for now; they are promoted to first-class fields when
    a consumer needs typed access.
    """

    evidence_id: str
    claim_id: str
    level: EvidenceLevel
    source_event_id: str
    payload: Mapping[str, Any]
    ts_epoch_ms: int


# --- AuthorizationLease (spec §3.5.2 / ADR § AuthorizationLease Day-1) -----


class AuthorizationLease(TypedDict):
    """Temporary capability grant from Allen (spec §3.5.2).

    Day-1 scenario does not exercise leases (`spawn_worker` is L2, `verify_diff`
    is L0) — `ActionRequest.authorization_lease` is always None on Day-1. The
    type exists so Stage 2 high-risk scenarios can populate it.

    Fields:
        lease_id: Unique ID for audit chain.
        granted_at_ms: Wall-clock ms at grant time.
        expires_at_ms: Wall-clock ms expiry. Pre-action Gate refuses if past.
        scope: Free-form mapping covering allowed_tools / allowed_targets /
            risk ceiling. Day-1 keeps it as `Mapping[str, Any]`; Stage 2
            tightens to a TypedDict once consumers exist.
    """

    lease_id: str
    granted_at_ms: int
    expires_at_ms: int
    scope: Mapping[str, Any]


# --- ActionRequest (spec §3.4.8 — Day-1 fields only) ------------------------


@dataclass(frozen=True)
class ActionRequest:
    """A gated request for L4 capability execution (spec §3.4.8).

    Per ADR § Module map, Day-1 captures the fields the Pre-action Gate
    actually inspects (caller_principal / target_entity_ref / risk_level /
    authorization_lease) plus the correlation fields the lifecycle needs.
    `result_expected_by`, `max_duration`, `timeout_policy`, `retry_policy`,
    `expected_postcondition` from the §3.4.8 skeleton are not yet wired —
    when L4 lifecycle / scheduler consumes them they get added.

    Attributes:
        action_id: Unique per-action identifier; threads through
            action.proposed → action.result_observed.
        tool_name: Name of the ToolDefinition to dispatch.
        target_entity_ref: Canonical entity id (e.g. task_id). None when the
            tool requires no entity. Pre-action Gate refuses if non-None and
            not in trusted entity set.
        caller_principal: Real caller identity (spec §3.5.2).
        risk_level: One of L0..L4. Compared against EffectivePolicy ceiling.
        arguments: Tool-specific arguments (read-only mapping).
        authorization_lease: Allen's explicit grant (spec §3.5.2). None when
            risk_level < L3 or no Allen-approved override is in play.
        run_id: Correlation key for the worker run the action belongs to.
        turn_id: Correlation key for the conversation turn that produced
            the action.
        payload: Per-action L3-side data attached to the request (e.g.
            ``verify_command`` from the Task Ledger projection per
            ADR-0002 § Verify_command plumbing). Distinct from
            ``arguments``, which carries the LLM-supplied tool arguments.
            Defaulted to ``None`` so every Day-1 construction stays
            valid; Day-2 (Step 12) populates the key
            ``payload["verify_command"]``.
    """

    action_id: str
    tool_name: str
    target_entity_ref: str | None
    caller_principal: CallerPrincipal
    risk_level: RiskLevel
    arguments: Mapping[str, Any]
    authorization_lease: AuthorizationLease | None
    run_id: str | None
    turn_id: str | None
    payload: Mapping[str, Any] | None = None


# --- PromptContext (ADR § Reference sources Q3 option (a)) ------------------


@dataclass(frozen=True)
class PromptContext:
    """L3 -> LLM context bundle.

    Day-1 carries only `system` (the rendered system prompt string). Legacy
    `core/llm.py` accepted a richer `prompt_context` object wired into
    personality + hot memory assembler; ADR Q3 picks option (a) — replace
    with a single `system: str`. Wider fields can be added later without
    breaking existing call sites because the dataclass is frozen and
    additive.
    """

    system: str


# --- RawResult (relocated from jarvis.execution.tools in Step 0b) -----------


@dataclass(frozen=True)
class RawResult:
    """One handler's return value — fed to L3 Result Interpreter.

    Relocated from :mod:`jarvis.execution.tools` in Step 0b of ADR-0002
    so L3 (which consumes RawResults) can import the type directly
    without crossing the layer DAG. L4 handlers continue to construct
    and return ``RawResult`` instances; the Day-2 dispatcher wrap
    (Step 11) will wrap bare single-slot returns into a
    :class:`RawResultBundle` at the L4/L3 boundary.

    Attributes:
        action_id: The ActionRequest's ``action_id`` (round-tripped so
            the interpreter can join back to the lifecycle / event
            chain).
        semantics: One of ``ack`` / ``observation`` / ``verification`` /
            ``report`` / ``error`` per ADR § Gate contracts table. The
            Result Interpreter maps this to a claim type + evidence
            level.
        payload: Tool-specific structured data (e.g. ``{"run_id": ...}``
            for ack, ``{"artifact_path": ..., "content_hash": ...}`` for
            verification). Read-only mapping.
        tool_output: Optional JSON string of the form produced by
            ``tool_result(...)`` / ``tool_error(...)``. Day-1 L3 records
            this verbatim into ``action.result_observed.payload.tool_output``
            so legacy clients have a string they can show.
        error: Optional short error tag (``artifact_missing``,
            ``predicate_failed``, etc.). None on success.
        metadata: L4's side-channel for data not part of the canonical
            RawResult payload but must travel back to L3 so L3 can emit
            correlated events. Day-2 uses exactly one key:
            ``metadata["cost"]`` — dict carrying ``{kind, model,
            tokens_in, tokens_out, optional cache_read_in /
            cache_write_in}`` (per ADR-0002 § RawResult.metadata
            extension). Defaulted to ``None`` so every Day-1 RawResult
            construction stays valid; Step 10/12 populate it from the
            Codex ``turn/completed`` payload.
    """

    action_id: str
    semantics: ResultSemantics
    payload: Mapping[str, Any]
    tool_output: str | None
    error: str | None
    metadata: Mapping[str, Any] | None = None


# --- RawResultBundle (Day-2 addition per ADR-0002 § RawResultBundle contract)


@dataclass(frozen=True)
class RawResultBundle:
    """A sequence of RawResult slots returned by one L4 handler.

    Day-1 tools returned a single RawResult. Day-2 introduces
    RawResultBundle so tools declaring a ``post_action_check``
    (spec §3.5.7) can return TWO slots in one call — slot 1 for the
    primary observation, slot 2 for the chained verification/error.
    Each slot carries its own ``result_semantics`` per spec §3.4.11;
    L3 emits one ``action.result_observed`` event per slot (spec
    §5.4.2).

    Invariant: at least one slot. Two slots maximum Day-2 (one primary
    + one post_action_check). Multi-stage chains are out of scope.

    Step 0b note: the type is defined here so Day-2 steps (11 / 12) can
    wire the dispatcher-wrap and the dual-slot interpreter without a
    second relocation. No production call site constructs a bundle yet.
    """

    slots: tuple[RawResult, ...]

    def __post_init__(self) -> None:
        """Enforce the 1..2 slot invariant at construction time."""
        if not (1 <= len(self.slots) <= 2):  # noqa: PLR2004 — the 1..2 bound IS the contract per spec §3.5.7.
            msg = f"RawResultBundle requires 1-2 slots, got {len(self.slots)}"
            raise ValueError(msg)


__all__ = [
    "ActionRequest",
    "AuthorizationLease",
    "CallerPrincipal",
    "Claim",
    "ClaimStatus",
    "ClaimType",
    "Event",
    "Evidence",
    "EvidenceLevel",
    "PromptContext",
    "RawResult",
    "RawResultBundle",
    "ResultSemantics",
    "RiskLevel",
]
