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

    Day-1 carries only the fields needed by Result Interpreter and Pre-emit
    Gate. Status / required_for_completion / supersede chain (full §8.7
    record) come in later steps when the projection actually exposes them.
    """

    claim_id: str
    type: ClaimType
    statement: str
    subject_ref: str
    produced_by_event_id: str
    ts_epoch_ms: int


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


__all__ = [
    "ActionRequest",
    "AuthorizationLease",
    "CallerPrincipal",
    "Claim",
    "ClaimType",
    "Event",
    "Evidence",
    "EvidenceLevel",
    "PromptContext",
    "RiskLevel",
]
