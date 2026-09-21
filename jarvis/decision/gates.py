"""L3 invariant gates — Pre-action / Pre-emit / Attention Policy.

Per ADR 0001 § Gate contracts (Day-1).

Each gate is a **pure function**: same inputs -> same outputs. The
caller (``decide()`` in :mod:`jarvis.decision`) emits the
``gate.evaluated`` event after invoking the gate. The gates themselves
do NOT touch the event log — keeping side-effect emission in one place
is what makes Acceptance C inspectable.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers and no import of ``jarvis.decision.llm``.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from jarvis.decision.policy import risk_rank
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.decision.packet import SituationPacket
    from jarvis.decision.policy import EffectivePolicy
    from jarvis.shared import ActionRequest, AuthorizationLease, EvidenceLevel
    from jarvis.state.projections import PendingConfirmations


# --- Public types -----------------------------------------------------------

GateOutcome = Literal["pass", "refuse", "confirm_required"]
"""Pre-action Gate outcome ladder (ADR § Gate contracts)."""

PreEmitPermission = Literal["allow_completion_language", "force_limitation_language"]
"""Pre-emit Gate verdict (ADR § Gate contracts)."""


OutputRiskClass = Literal["routine", "consequential_claim", "high_risk_claim"]
"""ResponsePlan output risk class per spec §3.4.13.

ADR-0002 Step 12 lands the field on every ResponsePlan construction:

- ``routine`` — default for non-consequential acks (no Postcondition
  Claim at level=verified backing the response).
- ``consequential_claim`` — response references a Postcondition Claim
  at level=verified (e.g. "task done, verified by pytest").
- ``high_risk_claim`` — response asserts completion of a high-risk
  task (reserved Day-2; nothing in the canonical scenario fits, so
  the field is defined but no Day-2 site emits it).
"""


RequiredGateMode = Literal["sentence", "full_text", "structured"]
"""ResponsePlan required gate mode per spec §3.4.13.

Day-2 mapping:

- ``sentence`` — routine paths gate per-sentence (default).
- ``full_text`` — gate the entire response (anything riskier than
  routine).
- ``structured`` — reserved for future structured-output gating.
"""


@dataclass(frozen=True)
class GateResult:
    """Output of the Pre-action Gate.

    Attributes:
        outcome: ``"pass"`` / ``"refuse"`` / ``"confirm_required"``.
        reasons: Human-readable strings, one per MUST-check that ran.
            ADR § Acceptance C5: empty ``reasons`` on a passing gate
            is a regression — every check that ran contributes a
            string regardless of pass/fail.
        check_results: Boolean map of named checks. Day-1 names:
            ``caller_allowed`` / ``entity_trusted`` /
            ``risk_within_ceiling`` / ``lease_validated``. Each is
            True when the check passed (or was a no-op for the
            current ActionRequest shape).
    """

    outcome: GateOutcome
    reasons: tuple[str, ...]
    check_results: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class ResponsePlan:
    """Output of the Pre-emit Gate.

    Attributes:
        text: Final response text the surface should render — the
            draft, unchanged.
        permission: ``allow_completion_language`` or
            ``force_limitation_language`` per ADR contract. Always
            ``allow_completion_language`` since ADR 0019 removed the
            claim/evidence ladder.
        downgrade_required: Always ``False`` (see ``permission``).
        active_claim_levels: Always empty (see ``permission``).
        response_hash: ``sha256`` hex of ``text``. Stamped onto the
            ``gate.evaluated`` event so Acceptance C3 can check the
            surface uses the same token it was gated on.
        output_risk_class: spec §3.4.13 risk classification. Always
            ``"routine"``.
        required_gate_mode: spec §3.4.13 required gate mode. Always
            ``"sentence"`` (routine = sentence-boundary streaming).
    """

    text: str
    permission: PreEmitPermission
    downgrade_required: bool
    active_claim_levels: tuple[EvidenceLevel, ...]
    response_hash: str
    output_risk_class: OutputRiskClass
    required_gate_mode: RequiredGateMode


# --- Pre-action Gate --------------------------------------------------------


def _now_epoch_ms() -> int:
    """Return current wall-clock as integer ms (matches event_log helper)."""
    return int(time.time() * 1000)


class _EntityGateToolLike(Protocol):
    """Minimal structural shape ``pre_action_gate``'s D3 entity arm needs.

    ADR-0011 D3: the gate takes the caller's already-resolved tool
    definition (not a registry lookup) so a third, gate-internal
    lookup can never disagree with the definition the caller actually
    resolved. The gate reads only ``requires_entity`` off this object
    — not ``name`` or ``risk_level`` — so this Protocol declares
    nothing else.
    """

    @property
    def requires_entity(self) -> bool:
        """Whether the gate must see a resolved ``target_entity_ref``."""
        ...


def _check_entity_trusted(
    action_request: ActionRequest,
    tool_def: _EntityGateToolLike | None,
) -> tuple[bool, str]:
    """Evaluate check 2 (entity_trusted) for :func:`pre_action_gate`.

    ADR-0011 D3: if ``target_entity_ref`` is None and
    ``tool_def.requires_entity`` is True, the tool declares it cannot act
    without a resolved entity — refuse with reason ``"entity_required:
    <tool_name> demands a resolved target"``. A resolved ref passes: the
    resolver that produced it is the trusted source (ADR 0019 removed the
    Task Ledger / EntityRegistry membership universes).
    """
    if action_request.target_entity_ref is None:
        if tool_def is not None and tool_def.requires_entity:
            return False, (
                "entity_trusted: entity_required: "
                f"{action_request.tool_name} demands a resolved target"
            )
        return True, "entity_trusted: no target_entity_ref to check"
    return True, (
        f"entity_trusted: target_entity_ref={action_request.target_entity_ref!r} "
        "was produced by the resolver"
    )


def pre_action_gate(
    action_request: ActionRequest,
    policy: EffectivePolicy,
    *,
    tool_def: _EntityGateToolLike | None,
    pending_confirmations: PendingConfirmations | None = None,
) -> GateResult:
    """Evaluate the four MUST-checks per ADR § Gate contracts.

    Checks in order (each runs even if a prior one failed so the
    audit surfaces every problem at once — short-circuiting would
    hide bug clusters from the autonomous loop):

    1. **caller_allowed**: ``action_request.caller_principal`` is in
       ``policy.allowed_tool_surface`` for ``tool_name``.
    2. **entity_trusted**: if ``target_entity_ref`` is None AND
       ``tool_def.requires_entity`` is True, the check FAILS
       (ADR-0011 D3) with reason ``"entity_required: <tool_name>
       demands a resolved target"`` — this is the arm that makes the
       entity check non-vacuous for tools like ADR-0012's
       ``write_file``. Tools with ``requires_entity=False`` keep
       passing on a None ref, and a resolved ref always passes (the
       resolver is the trusted source).
    3. **risk_within_ceiling**: ``risk_level <= autonomy_ceiling``
       per the L0..L4 ladder.
    4. **lease_validated** (ADR-0012 D2 — lease validation v2): when
       ``risk_level >= confirmation_threshold``, three sub-checks run
       in order — shape, expiry, scope. A **malformed** lease (missing
       key or wrong-typed value) never raises; it fails shape and is
       reported via reason ``"lease_malformed"`` (fixes the bare
       ``lease["expires_at_ms"]`` / ``lease["scope"]`` subscript hole
       that used to let a malformed lease raise ``KeyError`` out of
       the gate). Otherwise the lease must be unexpired
       (``expires_at_ms > now``) and in scope: tool_name in
       ``allowed_tools``, and the target side must agree in shape (a
       target-bearing request needs its target listed in
       ``allowed_targets``; a target-less request needs
       ``allowed_targets`` empty — a lease scoped to specific targets
       must not vacuously cover a target-less action of the same
       tool). **Single-use** (D2.4, Step 6): the lease's
       ``source_confirmation_event_id`` must equal the
       PendingConfirmations projection's current slot's
       ``accepted_event_uid`` (the D6 join key), and
       ``lease["lease_id"]`` must not already be in
       ``pending_confirmations.consumed_lease_ids`` (folded from a
       prior passing ``gate.evaluated`` that carried this same
       ``lease_id`` — see :func:`_lease_single_use_ok`).
    Outcome ladder — ``lease_hard_invalid`` (see that local variable's
    definition below) distinguishes "the lease is corrupt or spent"
    from "the lease is merely unsatisfied but a fresh grant could fix
    it":

    - All passed -> ``"pass"``.
    - The lease is **hard-invalid** (malformed, OR already-consumed
      replay per D2.4/Step 6, ADR-0012 §7 acceptance row C5) ->
      ``"refuse"`` outright — re-asking Allen would be wrong for
      corruption or replay, so this skips the softer
      confirm-required path entirely. A lease whose
      ``source_confirmation_event_id`` simply does not match the
      projection's current accepted slot (e.g. the ask it named was
      superseded) is NOT hard-invalid — that is merely unsatisfied,
      same as an expired or wrong-scope lease: a fresh grant could
      still fix it.
    - Risk above ceiling AND lease invalid for any other reason
      (missing / expired / wrong-tool / wrong-target) but otherwise
      legal -> a lease MIGHT cover it if Allen grants/re-grants one ->
      ``"confirm_required"``. Day-1 scenario does not hit this
      branch, but the symmetric path is kept for canary H12 and is
      now ADR-0012's live entry point.
    - Any other failure -> ``"refuse"``.

    Args:
        action_request: ActionRequest the L3 LLM proposed.
        policy: EffectivePolicy from
            :func:`jarvis.decision.policy.effective_policy`.
        tool_def: The tool definition the caller already resolved for
            this ``action_request.tool_name`` (or ``None`` for an
            unknown tool). ADR-0011 D3: the gate takes this from the
            caller rather than doing its own registry lookup. The two
            call sites resolve ``tool_def`` through two different
            lookups (``_find_registered_tool_def`` is caller-blind;
            ``_find_tool_def`` is JARVIS_LLM-scoped), so a third,
            gate-internal lookup could disagree with the definition
            the caller actually resolved; taking the caller's
            resolved definition removes that divergence. Keyword-only
            and required — there are only two call sites and both
            already hold the value.
        pending_confirmations: The folded PendingConfirmations
            projection (``packet.pending_confirmation``), or ``None``.
            ADR-0012 D2.4 (Step 6): check 4's single-use sub-check
            reads this to confirm a lease's
            ``source_confirmation_event_id`` names a confirmation the
            projection shows as accepted, and that
            ``lease["lease_id"]`` is not already in
            ``consumed_lease_ids``. Keyword-only with a ``None``
            default so callers that never attach a lease (the five
            pre-existing ADR-0012 D7 sites) keep compiling without
            passing it. UNLIKE ``entity_registry``, ``None`` here does
            NOT degrade to a no-op pass: this is the one check
            standing between a replayed lease and an L3 dispatch, so a
            lease-bearing request with no projection supplied fails
            single-use SOFT (``confirm_required``, not ``refuse`` —
            see :func:`_lease_single_use_ok`'s docstring for the
            fail-closed rationale).

    Returns:
        Frozen :class:`GateResult` with per-check bool + reason.
    """
    reasons: list[str] = []
    checks: dict[str, bool] = {}

    # 1. caller_allowed
    allowed = policy.allowed_tool_surface.get(action_request.caller_principal, frozenset())
    caller_allowed = action_request.tool_name in allowed
    checks["caller_allowed"] = caller_allowed
    reasons.append(
        f"caller_allowed: {action_request.caller_principal.value!r} "
        f"{'may' if caller_allowed else 'may not'} call "
        f"{action_request.tool_name!r} under mode={policy.mode}"
    )

    # 2. entity_trusted — split into a helper purely to keep this
    #    function's branch/statement count under ruff's PLR0912/PLR0915
    #    thresholds; see `_check_entity_trusted`'s docstring for the
    #    full contract (unchanged from the inline version).
    entity_trusted, entity_reason = _check_entity_trusted(action_request, tool_def)
    checks["entity_trusted"] = entity_trusted
    reasons.append(entity_reason)

    # 3. risk_within_ceiling
    risk_within_ceiling = (
        risk_rank(action_request.risk_level) <= risk_rank(policy.autonomy_ceiling)
    )
    checks["risk_within_ceiling"] = risk_within_ceiling
    reasons.append(
        f"risk_within_ceiling: risk_level={action_request.risk_level} "
        f"{'<=' if risk_within_ceiling else '>'} autonomy_ceiling={policy.autonomy_ceiling}"
    )

    # 4. lease_validated (ADR-0012 D2 — lease validation v2)
    needs_lease = risk_rank(action_request.risk_level) >= risk_rank(
        policy.confirmation_threshold,
    )
    lease_validated = True
    # `lease_hard_invalid` means "not merely unsatisfied-but-re-grantable;
    # corrupt or spent" — Allen re-granting cannot fix it, so it must
    # skip confirm_required and refuse outright. Today only the shape
    # (malformed) sub-check sets it; Step 6's single-use fold (D2.4,
    # ADR-0012 §7 acceptance row C5 — replaying a consumed lease must
    # refuse, and a consumed lease is otherwise shape/expiry/scope
    # valid) will set it too, on the same branch this comment marks
    # below, without needing to touch the outcome ladder again.
    lease_hard_invalid = False
    if needs_lease:
        lease = action_request.authorization_lease
        if lease is None:
            lease_validated = False
            reasons.append("lease_validated: required at this risk but no lease present")
        else:
            shape_ok, shape_detail = _lease_shape_ok(lease)
            if not shape_ok:
                lease_validated = False
                lease_hard_invalid = True
                reasons.append(f"lease_validated: lease_malformed: {shape_detail}")
            else:
                now_ms = _now_epoch_ms()
                unexpired = lease["expires_at_ms"] > now_ms
                scope_ok = _lease_scope_permits(lease, action_request)
                # Single-use (ADR-0012 D2.4, Step 6): see
                # `_lease_single_use_ok`'s docstring for the full
                # contract. An already-consumed lease sets BOTH
                # `lease_validated = False` (via `single_use_ok=False`
                # below) AND `lease_hard_invalid = True` — not just a
                # failed validation — so the outcome ladder refuses per
                # C5 instead of asking Allen again.
                single_use_ok, single_use_hard_invalid, single_use_detail = (
                    _lease_single_use_ok(lease, pending_confirmations)
                )
                if single_use_hard_invalid:
                    lease_hard_invalid = True
                lease_validated = unexpired and scope_ok and single_use_ok
                reasons.append(
                    f"lease_validated: unexpired={unexpired}, scope_ok={scope_ok}, "
                    f"single_use_ok={single_use_ok} ({single_use_detail})"
                )
    else:
        reasons.append(
            "lease_validated: risk below confirmation threshold; no lease required"
        )
    checks["lease_validated"] = lease_validated

    # Outcome decision
    all_pass = all(checks.values())
    if all_pass:
        outcome: GateOutcome = "pass"
    elif lease_hard_invalid:
        # Corrupt (malformed) or, from Step 6, spent (already-consumed)
        # — not "no lease yet". Hard refuse, no confirm_required
        # invitation to re-ask Allen (ADR-0012 §4 failure-mode table;
        # §7 acceptance row C5 for the Step 6 replay case).
        outcome = "refuse"
    elif (
        needs_lease
        and not lease_validated
        and caller_allowed
        and entity_trusted
        and risk_within_ceiling
    ):
        # The action is otherwise legal but lacks a valid authorization
        # lease; Allen could grant (or re-grant) one. Surface as
        # confirm_required.
        outcome = "confirm_required"
    else:
        outcome = "refuse"

    return GateResult(outcome=outcome, reasons=tuple(reasons), check_results=checks)


_LEASE_REQUIRED_KEYS: tuple[str, ...] = (
    "lease_id",
    "granted_by",
    "granted_to",
    "allowed_tools",
    "allowed_targets",
    "expires_at_ms",
    "max_uses",
    "reason",
    "source_confirmation_event_id",
)
"""The nine §3.5.2 fields, per ``AuthorizationLease`` (``jarvis/shared``)."""


def _lease_shape_ok(lease: Mapping[str, object]) -> tuple[bool, str]:
    """Validate lease shape without ever raising (ADR-0012 D2.1).

    A ``TypedDict`` is a plain ``dict`` at runtime — mypy's static
    ``AuthorizationLease`` typing does not stop a malformed mapping
    (missing key, wrong-typed value) from reaching the gate. This is
    check 4's shape sub-check: it must catch that case and report it
    via a ``(False, detail)`` pair instead of letting a bare subscript
    raise ``KeyError`` out of the gate (the hole this closes).

    Returns:
        ``(True, "ok")`` when every field is present with the
        expected type; otherwise ``(False, <detail>)`` naming the
        first problem found.
    """
    missing = [key for key in _LEASE_REQUIRED_KEYS if key not in lease]
    if missing:
        return False, f"missing keys {missing!r}"

    if not isinstance(lease["granted_to"], CallerPrincipal):
        return False, "granted_to is not a CallerPrincipal"
    for key in ("allowed_tools", "allowed_targets"):
        if not isinstance(lease[key], (frozenset, set, list, tuple)):
            return False, f"{key} is not a collection"
    for key in ("expires_at_ms", "max_uses"):
        if not isinstance(lease[key], int) or isinstance(lease[key], bool):
            return False, f"{key} is not an int"
    for key in ("lease_id", "granted_by", "reason", "source_confirmation_event_id"):
        if not isinstance(lease[key], str):
            return False, f"{key} is not a str"
    return True, "ok"


def _lease_scope_permits(
    lease: AuthorizationLease,
    action_request: ActionRequest,
) -> bool:
    """Return True iff the lease's tool/target scope covers this request.

    ADR-0012 D2.3 (check 4.3) — byte-equal match, fail-closed in BOTH
    directions on target: the request's ``tool_name`` must be in
    ``allowed_tools``; and (``ActionRequest`` has no separate
    canonical-target field, so ``target_entity_ref`` is "the request's
    canonical target" for this check) the target side must agree in
    shape, not just in membership:

    | request target        | lease ``allowed_targets`` | result  |
    |------------------------|---------------------------|---------|
    | non-None, member       | non-empty                 | permit  |
    | non-None, non-member   | non-empty                 | refuse  |
    | non-None                | empty                     | refuse  |
    | None                    | non-empty                 | refuse  |
    | None                    | empty                     | permit  |

    The first two rows are D2.3's explicit "request has a target"
    case. The last three are the symmetric case the ADR states in
    words but the original draft of this function only half-applied:
    a lease scoped to specific targets must not vacuously authorize a
    target-less action of the same tool — spec §1 "the lease ...
    binds the approval to the exact frozen action", not the tool name
    alone.

    Callers must run :func:`_lease_shape_ok` first — this function
    trusts ``lease``'s keys/types and will raise if they're wrong.
    """
    if action_request.tool_name not in lease["allowed_tools"]:
        return False
    if not lease["allowed_targets"]:
        return action_request.target_entity_ref is None
    return (
        action_request.target_entity_ref is not None
        and action_request.target_entity_ref in lease["allowed_targets"]
    )


def _lease_single_use_ok(
    lease: AuthorizationLease,
    pending_confirmations: PendingConfirmations | None,
) -> tuple[bool, bool, str]:
    """Evaluate D2.4 single-use for an already shape/expiry/scope-valid lease.

    Two independent facts, both read off the PendingConfirmations
    projection (never a lease store — D2: leases are never pooled):

    1. **References a real acceptance.** The lease's
       ``source_confirmation_event_id`` must equal the projection's
       CURRENT slot's ``accepted_event_uid`` — the D6 join key Step 3
       added ``PendingConfirmationSlot.accepted_event_uid`` for
       exactly this purpose (the slot is keyed on ``confirmation_id``;
       the lease carries the accepted EVENT's uid, a different
       string). A mismatch here is NOT hard-invalid: the referenced
       ask may simply have been superseded by a newer one (D4
       single-slot), and Allen granting a fresh lease would fix it —
       same posture as an expired or wrong-scope lease.
    2. **Not already spent.** ``lease["lease_id"] in
       pending_confirmations.consumed_lease_ids`` — folded from every
       prior ``gate.evaluated(outcome="pass")`` that carried this
       exact ``lease_id`` (cross-step contract #3: the gate event this
       function's caller emits MUST carry ``lease_id`` for that fold
       to ever fire). A lease found here IS hard-invalid — replaying a
       spent lease must refuse outright (ADR-0012 §7 acceptance row
       C5), never soften to ``confirm_required``.

    Args:
        lease: The already shape/expiry/scope-valid lease under
            evaluation.
        pending_confirmations: The folded projection, or ``None``.
            ``None`` fails CLOSED, not open: this is the one check
            standing between a replayed lease and an L3 dispatch, so a
            missing projection must not be read as "unspent" — it must
            be read as "unverifiable". Unlike ``entity_registry=None``
            (which NARROWS check 2 by removing a universe a ref could
            match in, making that check strictly stricter),
            ``pending_confirmations=None`` here would SKIP this check
            entirely if allowed to pass — a different failure shape,
            not the same one. The missing-projection case is soft
            (``hard_invalid=False``, i.e. ``confirm_required``), not
            hard: the lease itself is not proven corrupt or spent, the
            verifier simply lacked its input, and re-asking Allen is
            the safe resolution — same posture ``_lease_shape_ok``'s
            key-presence check and ``_lease_scope_permits``'s missing-
            ``allowed_tools`` case both take (§1: "fail-closed on a
            missing key"). In production the sole lease-minting call
            site (the D6 accept handler) always passes the just-
            refolded projection, so this branch is exercised only by
            callers that never attach a lease in the first place (the
            five untouched ADR-0012 D7 sites) — but that is call-site
            discipline, not a structural guarantee, hence fail-closed
            here regardless.

    Returns:
        ``(single_use_ok, hard_invalid, detail)`` — ``detail`` always
        names the projection's current ``confirmation_id`` when a slot
        exists, satisfying C5's "the reason names the consumed
        confirmation".
    """
    if pending_confirmations is None:
        return (
            False,
            False,
            "single_use could not be verified: no PendingConfirmations projection wired",
        )

    slot = pending_confirmations.slot
    confirmation_id = slot.confirmation_id if slot is not None else None
    accepted_match = (
        slot is not None and slot.accepted_event_uid == lease["source_confirmation_event_id"]
    )
    already_consumed = lease["lease_id"] in pending_confirmations.consumed_lease_ids

    if already_consumed:
        return False, True, f"lease already consumed by confirmation_id={confirmation_id!r}"
    if not accepted_match:
        return (
            False,
            False,
            "source_confirmation_event_id does not match any accepted confirmation",
        )
    return True, False, f"confirmation_id={confirmation_id!r}"


# --- Pre-emit Gate ----------------------------------------------------------


def _response_hash(text: str) -> str:
    """sha256 hex of ``text`` (utf-8). Stamped on gate.evaluated(pre_emit)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pre_emit_gate(draft_text: str) -> ResponsePlan:
    """Stamp the draft with the routine ResponsePlan (spec §3.4.12 v0).

    ADR 0019 removed the claim/evidence ladder, so no completion claim is
    ever judged: the draft passes through unchanged with ``routine`` /
    ``sentence`` streaming and its ``response_hash`` for Acceptance C3.
    """
    return ResponsePlan(
        text=draft_text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=_response_hash(draft_text),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


# --- Attention Policy (Day-1 minimal) ---------------------------------------

AttentionChannel = Literal[
    "voice_notify", "silent_log", "queue_review", "ask_confirm", "badge_card",
]
"""Day-1 attention-policy decisions, plus ``ask_confirm`` (ADR-0012 D5).

``ask_confirm`` is never returned by :func:`attention_policy` itself —
it is set by ``_finalize_response``'s post-hoc scan for a
``confirmation.requested`` event emitted THIS turn, the same
finalize-scan-override pattern as the existing ``hard_refusal_used``
check (`jarvis.decision.__init__`). Other 5 channels map to
silent_log."""


# Terminal triggers that re-enter L3 through ``_handle_action_terminal_failure``
# (runtime waiter + ADR-0009 D4 supervisor sweep). Their Limitation queues for
# review instead of speaking to an empty room (spec §3.2.5 安静优先).
_RECONCILIATION_TRIGGER_TYPES: frozenset[str] = frozenset(
    {"action.timeout_assumed", "action.failed", "action.cancelled"}
)


def attention_policy(
    packet: SituationPacket,
    *,
    document_form: bool = False,
) -> AttentionChannel:
    """Decide where this L3 invocation should surface output.

    - If trigger is a reconciliation terminal
      (``action.timeout_assumed`` / ``action.failed`` /
      ``action.cancelled``) -> ``"queue_review"`` (B-0005 pinned):
      after a worker timeout Allen has typically walked away, so the
      limitation queues for review. Keyed on the trigger: a 3am system
      turn must never speak.
    - If the answer to the user's own utterance is ``document_form``
      (material to read, not a one-line conclusion) -> ``"badge_card"``
      (spec §12.4: Inherent panel, 不出声; §18.3: voice = conclusion,
      panel = evidence). The card still receives the text; only TTS
      is skipped.
    - Default -> ``"voice_notify"``: everything left is a direct
      answer to the user's own utterance, and Jarvis is a voice
      assistant (docs/goals/speak-ordinary-answers.md).

    Args:
        packet: Current SituationPacket (trigger + correlations).
        document_form: ``True`` when the caller already holds the final
            answer text and it is multi-line material (a list, a file,
            a result table). Only meaningful for user-utterance
            triggers; system triggers keep their own routing.

    Returns:
        One of ``"voice_notify"`` / ``"queue_review"`` / ``"badge_card"``.
    """
    trigger_type = packet.trigger_event.type
    if trigger_type in _RECONCILIATION_TRIGGER_TYPES:
        return "queue_review"
    if document_form and trigger_type in ("surface.user_intent", "utterance.received"):
        return "badge_card"
    return "voice_notify"


__all__ = [
    "AttentionChannel",
    "GateOutcome",
    "GateResult",
    "OutputRiskClass",
    "PreEmitPermission",
    "RequiredGateMode",
    "ResponsePlan",
    "attention_policy",
    "pre_action_gate",
    "pre_emit_gate",
]
