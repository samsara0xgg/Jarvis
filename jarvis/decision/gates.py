"""L3 invariant gates — Pre-action / Pre-emit / Attention Policy.

Per ADR 0001 § Gate contracts (Day-1).

Each gate is a **pure function**: same inputs -> same outputs. The
caller (``decide()`` in :mod:`jarvis.decision`) emits the
``gate.evaluated`` event after invoking the gate. The gates themselves
do NOT touch the event log — keeping side-effect emission in one place
is what makes Acceptance C inspectable.

The Post-action Gate (Result Interpreter) lives in
:mod:`jarvis.decision.result_interpreter` because it emits two events
(claim.created + evidence.attached) rather than returning a single
verdict; bundling it with the other gates would smear the
"gate-decides, caller-emits" pattern.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers and no import of ``jarvis.decision.llm``.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from jarvis.decision.policy import risk_rank

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.decision.packet import SituationPacket
    from jarvis.decision.policy import EffectivePolicy
    from jarvis.shared import ActionRequest, EvidenceLevel
    from jarvis.state.projections import ClaimEvidenceProjection, TaskLedgerSnapshot


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
        text: Final response text the surface should render. Day-1
            this is either the original draft (when
            ``permission=allow_completion_language``) or a
            template-downgraded variant (when ``downgrade_required``
            forces a rewrite).
        permission: ``allow_completion_language`` or
            ``force_limitation_language`` per ADR contract.
        downgrade_required: True iff
            ``permission == force_limitation_language`` AND the draft
            contained completion-class language. Drives the
            ``decide()`` retry loop.
        active_claim_levels: Evidence levels actually backing
            completion claims for the active subject. Surfaces on
            the ``gate.evaluated(pre_emit)`` event so Acceptance C4
            can assert the ladder.
        response_hash: ``sha256`` hex of ``text`` (after any
            downgrade). Stamped onto the ``gate.evaluated`` event so
            Acceptance C3 can check the surface uses the same token
            it was gated on.
        output_risk_class: spec §3.4.13 risk classification per
            ADR-0002 § ResponsePlan schema extension. Day-2 defaults
            to ``"routine"`` for non-consequential acks; lifts to
            ``"consequential_claim"`` when the response references a
            Postcondition Claim at ``level=verified``. The Pre-emit
            Gate is the sole derivation site (Step 12 wires it).
        required_gate_mode: spec §3.4.13 required gate mode. Defaults
            to ``"sentence"`` for routine acks; lifts to
            ``"full_text"`` when ``output_risk_class != "routine"``.
            ``"structured"`` is reserved for Stage 2.
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


def pre_action_gate(
    action_request: ActionRequest,
    policy: EffectivePolicy,
    ledger_snapshot: TaskLedgerSnapshot,
) -> GateResult:
    """Evaluate the four MUST-checks per ADR § Gate contracts.

    Checks in order (each runs even if a prior one failed so the
    audit surfaces every problem at once — short-circuiting would
    hide bug clusters from the autonomous loop):

    1. **caller_allowed**: ``action_request.caller_principal`` is in
       ``policy.allowed_tools_per_caller`` for ``tool_name``.
    2. **entity_trusted**: if ``target_entity_ref`` is non-None, it
       must be the ``task_id`` of an open task in
       ``ledger_snapshot``. Day-1 only task entities; Stage 2 widens
       to an Entity Registry.
    3. **risk_within_ceiling**: ``risk_level <= autonomy_ceiling``
       per the L0..L4 ladder.
    4. **lease_validated**: when
       ``risk_level >= confirmation_required_at_or_above``,
       ``authorization_lease`` must be non-None and unexpired with a
       scope that permits the tool + target. Day-1 scenario never
       triggers this branch; the check is preserved for Stage 2.

    Outcome ladder:

    - All passed -> ``"pass"``.
    - Risk above ceiling AND lease invalid (or missing when
      required) -> ``"refuse"``.
    - Risk above ceiling AND a lease MIGHT cover it if Allen grants
      one -> ``"confirm_required"``. Day-1 scenario does not hit
      this branch, but the symmetric path is kept for canary H12.
    - Any other failure -> ``"refuse"``.

    Args:
        action_request: ActionRequest the L3 LLM proposed.
        policy: EffectivePolicy from
            :func:`jarvis.decision.policy.effective_policy`.
        ledger_snapshot: TaskLedgerSnapshot for entity-trust check.

    Returns:
        Frozen :class:`GateResult` with per-check bool + reason.
    """
    reasons: list[str] = []
    checks: dict[str, bool] = {}

    # 1. caller_allowed
    allowed = policy.allowed_tools_per_caller.get(action_request.caller_principal, frozenset())
    caller_allowed = action_request.tool_name in allowed
    checks["caller_allowed"] = caller_allowed
    reasons.append(
        f"caller_allowed: {action_request.caller_principal.value!r} "
        f"{'may' if caller_allowed else 'may not'} call "
        f"{action_request.tool_name!r} under mode={policy.mode}"
    )

    # 2. entity_trusted
    if action_request.target_entity_ref is None:
        entity_trusted = True
        reasons.append("entity_trusted: no target_entity_ref to check")
    else:
        # A task is "trusted" when it appears in the Task Ledger at
        # all — open OR reported_complete (mid-turn verify must still
        # pass) OR verified_complete (re-verify after the fact). The
        # ledger is the gate's universe of known entities; rejecting
        # reported_complete tasks would break the verify_diff leg of
        # the Day-1 happy path. (Step 12 follow-up.)
        known_task_ids = set(ledger_snapshot.records_by_task_id.keys())
        entity_trusted = action_request.target_entity_ref in known_task_ids
        reasons.append(
            f"entity_trusted: target_entity_ref={action_request.target_entity_ref!r} "
            f"{'is' if entity_trusted else 'is NOT'} in Task Ledger"
        )
    checks["entity_trusted"] = entity_trusted

    # 3. risk_within_ceiling
    risk_within_ceiling = (
        risk_rank(action_request.risk_level) <= risk_rank(policy.autonomy_ceiling)
    )
    checks["risk_within_ceiling"] = risk_within_ceiling
    reasons.append(
        f"risk_within_ceiling: risk_level={action_request.risk_level} "
        f"{'<=' if risk_within_ceiling else '>'} autonomy_ceiling={policy.autonomy_ceiling}"
    )

    # 4. lease_validated
    needs_lease = risk_rank(action_request.risk_level) >= risk_rank(
        policy.confirmation_required_at_or_above,
    )
    lease_validated = True
    if needs_lease:
        lease = action_request.authorization_lease
        if lease is None:
            lease_validated = False
            reasons.append("lease_validated: required at this risk but no lease present")
        else:
            now_ms = _now_epoch_ms()
            expires_at_ms = int(lease["expires_at_ms"])
            scope = lease["scope"]
            unexpired = expires_at_ms > now_ms
            scope_ok = _lease_scope_permits(scope, action_request)
            lease_validated = unexpired and scope_ok
            reasons.append(
                f"lease_validated: unexpired={unexpired}, scope_ok={scope_ok}"
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
    elif (
        needs_lease
        and not lease_validated
        and caller_allowed
        and entity_trusted
        and risk_within_ceiling
    ):
        # The action is otherwise legal but lacks an authorization
        # lease; Allen could grant one. Surface as confirm_required.
        outcome = "confirm_required"
    else:
        outcome = "refuse"

    return GateResult(outcome=outcome, reasons=tuple(reasons), check_results=checks)


def _lease_scope_permits(
    scope: Mapping[str, object],
    action_request: ActionRequest,
) -> bool:
    """Return True iff the lease scope covers this tool + target.

    Day-1 minimum scope shape (matches ADR § AuthorizationLease):

    - ``allowed_tools``: iterable of tool name strings.
    - ``allowed_targets``: iterable of entity-ref strings (optional).
    """
    allowed_tools = scope.get("allowed_tools") or ()
    if isinstance(allowed_tools, (list, tuple, frozenset, set)):
        if action_request.tool_name not in allowed_tools:
            return False
    else:
        return False

    if action_request.target_entity_ref is not None:
        allowed_targets = scope.get("allowed_targets")
        if (
            allowed_targets is not None
            and isinstance(allowed_targets, (list, tuple, frozenset, set))
            and action_request.target_entity_ref not in allowed_targets
        ):
            return False
    return True


# --- Pre-emit Gate ----------------------------------------------------------

# Completion-class language detection. ADR § Gate contracts spells out the
# canonical keyword set: ``完成`` / ``已完成`` / ``verified`` / ``done``.
# Match case-insensitively. ``\b`` for English so a stray ``redone`` does
# not trigger; the CJK forms are stripped of word-boundary requirement.
#
# Mirror set: ``_COMPLETION_SCRUB_PATTERNS`` in `jarvis.decision.__init__`.
# Every new entry here must declare its scrub counterpart in
# `tests/unit/test_pre_emit_forced_template.py::_GATE_TO_SCRUB_COVERAGE`
# (drift guard) — adding a gate keyword without a scrub decision will
# fail Tier 1.
_COMPLETION_KEYWORDS: tuple[re.Pattern[str], ...] = (
    re.compile(r"完成"),
    re.compile(r"已完成"),
    re.compile(r"\bverified\b", re.IGNORECASE),
    re.compile(r"\bdone\b", re.IGNORECASE),
)


def _contains_completion_language(text: str) -> bool:
    """Return True iff ``text`` contains any completion-class keyword."""
    return any(pat.search(text) for pat in _COMPLETION_KEYWORDS)


def _response_hash(text: str) -> str:
    """sha256 hex of ``text`` (utf-8). Stamped on gate.evaluated(pre_emit)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pre_emit_gate(
    draft_text: str,
    claim_evidence: ClaimEvidenceProjection,
    active_subject_ref: str,
) -> ResponsePlan:
    """Evaluate the draft response per ADR § Gate contracts.

    Logic:

    1. Find the strongest evidence level for ``active_subject_ref``
       via ``claim_evidence.strongest_level_for(...)``.
    2. Detect completion-class language in the draft.
    3. ``allow_completion_language`` IFF strongest evidence level
       is ``verified``/``accepted`` AND there exists a Postcondition
       Claim with ``subject_ref == active_subject_ref``.
    4. ``force_limitation_language`` otherwise.
    5. ``downgrade_required`` = (``force_limitation_language`` AND
       draft contains completion language).

    Args:
        draft_text: LLM's draft response (output of the tool-use loop).
        claim_evidence: Folded Claim/Evidence projection.
        active_subject_ref: Subject the response is "about" — Day-1
            this is the active ``task_id``.

    Returns:
        Frozen :class:`ResponsePlan` carrying the verdict + final
        text (which equals the draft Day-1 — ``decide()`` does any
        rewriting after observing the verdict).
    """
    strongest = claim_evidence.strongest_level_for(active_subject_ref)
    has_postcondition_for_subject = any(
        claim.type == "Postcondition"
        for claim in claim_evidence.claims_for(active_subject_ref)
    )

    # Collect ALL evidence levels for the subject (not just the
    # strongest) so the ``active_claim_levels`` audit field is
    # informative.
    levels: list[EvidenceLevel] = [
        ev.level
        for claim in claim_evidence.claims_for(active_subject_ref)
        for ev in claim_evidence.evidence_for(claim.claim_id)
    ]

    has_completion = _contains_completion_language(draft_text)

    if strongest in ("verified", "accepted") and has_postcondition_for_subject:
        permission: PreEmitPermission = "allow_completion_language"
    else:
        permission = "force_limitation_language"

    downgrade_required = permission == "force_limitation_language" and has_completion

    # ADR-0002 Step 12 (spec §3.4.13): when the response is allowed to
    # carry completion language for a Postcondition Claim backed by
    # verified/accepted evidence, classify the output as
    # ``consequential_claim`` and lift the gate mode to ``full_text``
    # so the surface gates the whole response, not just per-sentence.
    # Routine acks default to ``routine`` / ``sentence``.
    output_risk_class, required_gate_mode = _derive_output_risk(
        strongest=strongest,
        has_postcondition=has_postcondition_for_subject,
    )

    # Day-1: the gate does NOT rewrite the text. ``decide()`` reads the
    # ResponsePlan and either re-prompts the LLM (preferred) or
    # template-downgrades on the second attempt. Returning ``text``
    # unchanged keeps the response_hash check meaningful — the surface
    # writes the same text the gate just hashed.
    return ResponsePlan(
        text=draft_text,
        permission=permission,
        downgrade_required=downgrade_required,
        active_claim_levels=tuple(levels),
        response_hash=_response_hash(draft_text),
        output_risk_class=output_risk_class,
        required_gate_mode=required_gate_mode,
    )


def _derive_output_risk(
    *,
    strongest: EvidenceLevel | None,
    has_postcondition: bool,
) -> tuple[OutputRiskClass, RequiredGateMode]:
    """Return ``(output_risk_class, required_gate_mode)`` per spec §3.4.13.

    Day-2 rule (ADR-0002 § ResponsePlan schema extension):

    - Verified/accepted Postcondition Claim backing the subject →
      ``("consequential_claim", "full_text")``. The response is
      asserting a real-world completion fact; gate the entire text.
    - Otherwise → ``("routine", "sentence")``. Routine acks gate
      per-sentence so the LLM keeps natural cadence.

    ``high_risk_claim`` is reserved Day-2; nothing in the flagship
    scenario fits and the field stays available for Stage 2 high-risk
    operators.
    """
    if strongest in ("verified", "accepted") and has_postcondition:
        return "consequential_claim", "full_text"
    return "routine", "sentence"


# --- Attention Policy (Day-1 minimal) ---------------------------------------

AttentionChannel = Literal["voice_notify", "silent_log", "queue_review"]
"""Day-1 attention-policy decisions. Other 7 channels map to silent_log."""


def attention_policy(  # noqa: C901 — small branch tree but ruff counts each ``if`` separately.
    packet: SituationPacket,
    claim_evidence: ClaimEvidenceProjection,
) -> AttentionChannel:
    """Decide where this L3 invocation should surface output.

    Day-1 rules per ADR § Stub strategy L3 Attention row:

    - If a verified Postcondition Claim was just emitted for the
      current subject -> ``"voice_notify"``.
    - If trigger is ``worker.reported`` and no verified evidence
      yet -> ``"silent_log"`` (Allen said "审核了再告诉我";
      reporting an unverified status would violate the spirit).
    - Default -> ``"queue_review"``.

    Args:
        packet: Current SituationPacket (trigger + open tasks +
            correlations).
        claim_evidence: Folded projection used to detect verified
            Postcondition evidence.

    Returns:
        One of ``"voice_notify"`` / ``"silent_log"`` /
        ``"queue_review"``.
    """
    trigger_type = packet.trigger_event.type

    # Active subject: prefer an explicit ``task_id`` in the trigger
    # correlation; fall back to the first open task. This mirrors
    # ``decide()``'s active-subject extraction.
    active_subject: str | None = None
    if packet.trigger_event.correlation is not None:
        candidate = packet.trigger_event.correlation.get("task_id")
        if isinstance(candidate, str):
            active_subject = candidate
    if active_subject is None and packet.open_tasks:
        active_subject = packet.open_tasks[0].task_id

    has_verified_postcondition = False
    if active_subject is not None:
        for claim in claim_evidence.claims_for(active_subject):
            if claim.type != "Postcondition":
                continue
            for ev in claim_evidence.evidence_for(claim.claim_id):
                if ev.level in ("verified", "accepted"):
                    has_verified_postcondition = True
                    break
            if has_verified_postcondition:
                break

    if has_verified_postcondition:
        return "voice_notify"
    if trigger_type == "worker.reported":
        return "silent_log"
    return "queue_review"


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
