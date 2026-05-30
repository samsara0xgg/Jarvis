"""L3 Post-action Gate — Result Interpreter.

Per ADR 0001 § Gate contracts (Result Interpreter table) + ADR-0002
§ Evidence ladder (Day-2 dual-slot rewrite).

Two public entry points:

``result_interpreter(...)`` — single-slot path (Day-1) for tools that
return one :class:`RawResult`. Maps semantics → (ClaimType, EvidenceLevel)
per the table below and emits one ``claim.created`` + one
``evidence.attached``.

    | result_semantics | claim type    | evidence level |
    |------------------|---------------|----------------|
    | ack              | Execution     | executed       |
    | observation      | Artifact      | observed       |
    | verification     | Postcondition | verified       |
    | report           | Report        | reported       |
    | error            | Limitation    | reported       |

``interpret_verify_diff_bundle(...)`` — Day-2 dual-slot path for the
``verify_diff`` :class:`RawResultBundle`. Implements the F2 ladder from
ADR-0002 § Evidence ladder (lines 250-339): observation slot produces
an Artifact Claim + reviewer-derived Report-grade evidence row;
verification slot produces a Postcondition Claim at ``level=verified``
plus ``task.verified``; error slot produces a Limitation Claim at
``level=executed``. Missing slots (no diff / no verify_command) produce
the §8.5 rule 6 contrast Limitation row.

Constraint per ADR Acceptance E3: never emit an ``evidence.level``
exceeding the row's allowed value for the source semantics. The
interpreter is the single point of enforcement.

Crucially per ADR § Acceptance A8: ``claim.created`` and
``evidence.attached`` are SEPARATE events (each its own row).

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
from sibling layers and no import of ``jarvis.decision.llm`` /
``jarvis.decision.reviewer``. The reviewer LLM call happens at the
caller (``jarvis/decision/__init__.py::_dispatch_one_tool_call``) which
hands the :class:`ReviewerVerdict` into
:func:`interpret_verify_diff_bundle` as a plain dataclass — this module
stays import-light and pure (no LLM in the unit-test reach).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

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
        RawResultBundle,
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

# Day-2 F2 ladder relation defaults. The Day-1 single-slot path derives
# ``relation`` from the semantics + error state alone; the Day-2
# dual-slot path overrides via :func:`interpret_verify_diff_bundle`.
_RELATION_FOR_SEMANTICS: dict[ResultSemantics, Literal["supports", "refutes", "limits"]] = {
    "ack": "supports",
    "observation": "supports",
    "verification": "supports",
    "report": "supports",
    "error": "limits",
}


# --- result_interpreter (Day-1 single-slot path) ---------------------------


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
    relation = _RELATION_FOR_SEMANTICS[raw.semantics]

    subject_ref = subject_ref_override or action_request.target_entity_ref or raw.action_id
    statement = statement_override or _render_statement(raw.semantics, raw, action_request)

    correlation = _build_correlation(action_request)

    claim_event, evidence_event = _emit_claim_and_evidence(
        conn=conn,
        source_event_id=source_event_id,
        correlation=correlation,
        claim_type=claim_type,
        statement=statement,
        subject_ref=subject_ref,
        relation=relation,
        level=evidence_level,
        evidence_payload_extras=_evidence_payload_extras(raw),
    )

    return (claim_event, evidence_event)


def _emit_claim_and_evidence(  # noqa: PLR0913 — every argument is load-bearing for the (Claim, Evidence) pair.
    *,
    conn: sqlite3.Connection,
    source_event_id: str,
    correlation: Mapping[str, str],
    claim_type: ClaimType,
    statement: str,
    subject_ref: str,
    relation: Literal["supports", "refutes", "limits"],
    level: EvidenceLevel,
    evidence_payload_extras: Mapping[str, Any] | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
) -> tuple[Event, Event]:
    """Emit one ``claim.created`` + one ``evidence.attached`` pair.

    Shared helper for the Day-1 single-slot path and the Day-2 F2
    ladder. Returns the ``(claim_event, evidence_event)`` tuple so
    callers can chain ``task.verified`` etc. off the evidence row.
    """
    claim_id = "C" + uuid.uuid4().hex[:8]
    evidence_id = "E" + uuid.uuid4().hex[:8]

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
        "relation": relation,
        "level": level,
    }
    if source_type is not None:
        evidence_payload["source_type"] = source_type
    if source_id is not None:
        evidence_payload["source_id"] = source_id
    if evidence_payload_extras:
        for key, value in evidence_payload_extras.items():
            if key not in evidence_payload:
                evidence_payload[key] = value

    evidence_event = emit_event(
        conn,
        type="evidence.attached",
        payload=evidence_payload,
        source_event_id=claim_event.event_uid,
        correlation=correlation,
    )

    return claim_event, evidence_event


def _build_correlation(action_request: ActionRequest) -> Mapping[str, str]:
    """Build the standard ``{action_id, run_id?, turn_id?}`` correlation."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


# --- Day-2 F2 ladder (verify_diff dual-slot) -------------------------------


VerifyVerdict = Literal["verified", "no_op", "neither"]
"""Tri-state outcome the dispatcher uses to pick the trailing event.

- ``"verified"`` — verification slot produced a Postcondition Claim at
  ``level=verified``; caller emits ``task.verified``.
- ``"no_op"`` — verification slot's exit code passed but
  ``diff_nonempty == False``. Per Fix 2 Option A (amended ADR-0002
  § Evidence ladder paradox row), the verify_command alone cannot
  promote to ``level=verified`` absent an artifact-change signal
  (spec §8.9 — code task requires artifact changed + verification
  passed). Caller emits ``task.no_op`` to record the honest "no work
  done" outcome.
- ``"neither"`` — verify failed, no verify slot, or no subject; caller
  emits neither completion event.
"""


def interpret_verify_diff_bundle(  # noqa: PLR0913 - F2 ladder inputs are all load-bearing per ADR-0002 lines 279-301.
    bundle: RawResultBundle,
    *,
    source_event_ids_by_semantics: Mapping[str, str],
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    subject_ref: str,
    task_goal: str,
    reviewer_verdict: ReviewerVerdictLike | None,
) -> tuple[tuple[Event, ...], VerifyVerdict]:
    """Interpret a ``verify_diff`` :class:`RawResultBundle` per the F2 ladder.

    Implements ADR-0002 § Evidence ladder lines 279-301 (amended by
    Fix 2 Option A for the empty-diff + verify-pass paradox row).
    Each row of the ladder maps a (slot semantics, side condition)
    tuple to one or more (Claim, Evidence) pairs. The bundle is
    iterated in-order; the reviewer verdict (one verdict per bundle,
    fed by the caller because the reviewer LLM lives outside this
    module's import surface) attaches a SEPARATE Report-grade
    evidence row to whichever claim the diff/verify slots produced.

    The function decomposes into three private helpers — one per ladder
    section (observation slot, verification slot, reviewer row) — so
    each branch is small enough to read and the F2 table maps line-for-
    line onto the helper bodies.

    Returns ``(events, verdict)`` — ``verdict`` is a :data:`VerifyVerdict`
    literal the dispatcher uses to pick the trailing completion event:
    ``"verified"`` → ``task.verified``, ``"no_op"`` → ``task.no_op``,
    ``"neither"`` → nothing.
    """
    ctx = _BundleCtx(
        action_request=action_request,
        conn=conn,
        subject_ref=subject_ref,
        task_goal=task_goal,
        correlation=_build_correlation(action_request),
        source_event_ids_by_semantics=source_event_ids_by_semantics,
    )

    observation_slot = bundle.slots[0]
    diff_nonempty = bool(observation_slot.payload.get("diff_nonempty", False))

    verification_slot: RawResult | None = bundle.slots[1] if len(bundle.slots) > 1 else None

    emitted: list[Event] = []

    # === Observation slot row(s) =========================================
    active_claim_id = _emit_observation_rows(
        ctx=ctx,
        observation_slot=observation_slot,
        diff_nonempty=diff_nonempty,
        emitted=emitted,
    )

    # === Verification slot row(s) ========================================
    active_claim_id, active_relation, verdict = _emit_verification_rows(
        ctx=ctx,
        observation_slot=observation_slot,
        verification_slot=verification_slot,
        diff_nonempty=diff_nonempty,
        observation_active_claim_id=active_claim_id,
        emitted=emitted,
    )

    # === Reviewer row + paradox contrast =================================
    if reviewer_verdict is not None and active_claim_id is not None:
        _emit_reviewer_rows(
            ctx=ctx,
            reviewer_verdict=reviewer_verdict,
            active_claim_id=active_claim_id,
            active_relation=active_relation,
            did_verify=verdict == "verified",
            emitted=emitted,
        )

    return tuple(emitted), verdict


@dataclass(frozen=True)
class _BundleCtx:
    """Bundle of arguments shared across the F2-ladder helpers.

    Bundled because each helper would otherwise carry a 6-argument
    signature and trip PLR0913. The dataclass is internal-only; the
    public entry point unpacks the user-facing arguments into one.
    """

    action_request: ActionRequest
    conn: sqlite3.Connection
    subject_ref: str
    task_goal: str
    correlation: Mapping[str, str]
    source_event_ids_by_semantics: Mapping[str, str]

    def source_event_id(self, semantics: str, *, fallback: str | None = None) -> str:
        """Return the ``action.result_observed.event_uid`` for ``semantics``."""
        return self.source_event_ids_by_semantics.get(
            semantics,
            fallback or self.action_request.action_id,
        )


def _emit_observation_rows(
    *,
    ctx: _BundleCtx,
    observation_slot: RawResult,
    diff_nonempty: bool,
    emitted: list[Event],
) -> str | None:
    """Emit the observation-slot Claim+Evidence row(s); return active claim_id."""
    obs_uid = ctx.source_event_id("observation")

    if diff_nonempty:
        # Spec §8.5 rule 3 — diff capture is a tool observation. Ceiling
        # is `level=observed`; relation=`supports` because the diff
        # supports the Artifact Claim that work was produced.
        artifact_claim, artifact_evidence = _emit_claim_and_evidence(
            conn=ctx.conn,
            source_event_id=obs_uid,
            correlation=ctx.correlation,
            claim_type="Artifact",
            statement=f"diff captured for {ctx.subject_ref}: {ctx.task_goal[:80]}",
            subject_ref=ctx.subject_ref,
            relation="supports",
            level="observed",
            source_type="tool",
            source_id="diff_capture",
            evidence_payload_extras=_diff_evidence_extras(observation_slot),
        )
        emitted.extend((artifact_claim, artifact_evidence))
        return str(artifact_claim.payload["claim_id"])

    # Empty diff — Codex produced nothing. F2 ladder row "diff_nonempty
    # == False": Execution Claim at level=executed for the spawn_worker
    # run + §8.5 rule 6 Limitation row for the missing artifact.
    execution_claim, execution_evidence = _emit_claim_and_evidence(
        conn=ctx.conn,
        source_event_id=obs_uid,
        correlation=ctx.correlation,
        claim_type="Execution",
        statement=f"spawn_worker ran but produced no diff for {ctx.subject_ref}",
        subject_ref=ctx.subject_ref,
        relation="supports",
        level="executed",
        source_type="run",
        source_id="spawn_worker",
    )
    emitted.extend((execution_claim, execution_evidence))
    rule6_claim, rule6_evidence = _emit_claim_and_evidence(
        conn=ctx.conn,
        source_event_id=obs_uid,
        correlation=ctx.correlation,
        claim_type="Limitation",
        statement=(
            f"no diff artifact produced - section 8.5 rule 6 absence of "
            f"evidence for {ctx.subject_ref}"
        ),
        subject_ref=ctx.subject_ref,
        relation="limits",
        level="reported",
        source_type="absence",
        source_id="missing_diff_artifact",
    )
    emitted.extend((rule6_claim, rule6_evidence))
    # Empty-diff path leaves the active claim on the Execution row so a
    # reviewer-fail (paradox case row in §8.5) still attaches usefully.
    return str(execution_claim.payload["claim_id"])


def _emit_verification_rows(  # noqa: PLR0913 - verification ladder needs ctx, observation, verification, diff state, active claim, and accumulator.
    *,
    ctx: _BundleCtx,
    observation_slot: RawResult,
    verification_slot: RawResult | None,
    diff_nonempty: bool,
    observation_active_claim_id: str | None,
    emitted: list[Event],
) -> tuple[str | None, Literal["supports", "refutes", "limits"], VerifyVerdict]:
    """Emit the verification-slot row(s); return ``(active_claim_id, active_relation, verdict)``.

    Spec §3.4.11 maps verification semantics to ``level=verified`` AND
    error semantics to ``level=executed`` — the verify_command ran in
    both cases; only the predicate differs.

    Fix 2 Option A (amended ADR-0002 § Evidence ladder paradox row):
    when the verification slot exits 0 BUT ``diff_nonempty == False``
    (Codex produced no artifact), do NOT emit a Postcondition Claim
    at ``level=verified``. Spec §8.9 requires both an artifact-change
    signal AND verification for a code task to be ``task.verified``;
    the verify_command alone cannot supply the artifact-change half.
    Return ``verdict="no_op"`` so the dispatcher emits ``task.no_op``
    instead — an honest "no work done" event that preserves audit
    clarity without leaking a false-positive completion claim.
    """
    if verification_slot is None:
        # No verify_command on the task. §8.5 rule 6 contrast Limitation
        # (only meaningful when a diff was captured — empty-diff paths
        # already have a §8.5 row from the observation handler).
        if diff_nonempty:
            missing_claim, missing_evidence = _emit_claim_and_evidence(
                conn=ctx.conn,
                source_event_id=ctx.source_event_id("observation"),
                correlation=ctx.correlation,
                claim_type="Limitation",
                statement=(
                    f"no verify_command for {ctx.subject_ref} - postcondition "
                    f"cannot reach level=verified (section 8.5 rule 6)"
                ),
                subject_ref=ctx.subject_ref,
                relation="limits",
                level="reported",
                source_type="absence",
                source_id="missing_verify_command",
            )
            emitted.extend((missing_claim, missing_evidence))
        return observation_active_claim_id, "supports", "neither"

    if verification_slot.semantics == "verification":
        # Fix 2 Option A short-circuit: verify_command exited 0 but
        # Codex produced no diff. Suppress the Postcondition Claim and
        # signal no_op so the dispatcher emits task.no_op rather than
        # task.verified. The Execution Claim + §8.5 rule-6 missing-diff
        # Limitation row from the observation branch already record the
        # "tool ran but produced nothing" audit trail.
        if not diff_nonempty:
            return observation_active_claim_id, "supports", "no_op"

        postcondition_claim, postcondition_evidence = _emit_claim_and_evidence(
            conn=ctx.conn,
            source_event_id=ctx.source_event_id("verification"),
            correlation=ctx.correlation,
            claim_type="Postcondition",
            statement=(
                f"verify_command predicate satisfied for {ctx.subject_ref}"
            ),
            subject_ref=ctx.subject_ref,
            relation="supports",
            level="verified",
            source_type="tool",
            source_id="verify_command",
            evidence_payload_extras=_verify_evidence_extras(
                verification_slot,
                observation_slot=observation_slot,
            ),
        )
        emitted.extend((postcondition_claim, postcondition_evidence))
        return str(postcondition_claim.payload["claim_id"]), "supports", "verified"

    # exit_code != 0 or timeout — semantics="error". Limitation Claim at
    # level=executed: tool DID run; predicate failed.
    limitation_claim, limitation_evidence = _emit_claim_and_evidence(
        conn=ctx.conn,
        source_event_id=ctx.source_event_id(
            "error",
            fallback=ctx.source_event_id("verification"),
        ),
        correlation=ctx.correlation,
        claim_type="Limitation",
        statement=(
            f"verify_command predicate refuted for {ctx.subject_ref} "
            f"(exit_code != 0)"
        ),
        subject_ref=ctx.subject_ref,
        relation="limits",
        level="executed",
        source_type="tool",
        source_id="verify_command",
        evidence_payload_extras=_verify_evidence_extras(
            verification_slot,
            observation_slot=observation_slot,
        ),
    )
    emitted.extend((limitation_claim, limitation_evidence))
    return str(limitation_claim.payload["claim_id"]), "limits", "neither"


def _emit_reviewer_rows(  # noqa: PLR0913 - reviewer row needs (ctx + verdict + active-claim trio + emitted accumulator).
    *,
    ctx: _BundleCtx,
    reviewer_verdict: ReviewerVerdictLike,
    active_claim_id: str,
    active_relation: Literal["supports", "refutes", "limits"],
    did_verify: bool,
    emitted: list[Event],
) -> None:
    """Emit the reviewer Report-grade evidence row + optional paradox Limitation.

    Spec §8.5 rule 1 + §13.2 I10 — reviewer is LLM, ceiling=reported,
    relation derived from verdict (supports/refutes) when attached to a
    supporting claim; locks to ``limits`` when attached to a Limitation
    Claim. Reviewer-fail-while-verify_command-passes (F2 ladder row 285)
    emits an extra contrast Limitation Claim.
    """
    reviewer_relation: Literal["supports", "refutes", "limits"]
    if active_relation == "limits":
        reviewer_relation = "limits"
    elif reviewer_verdict.verdict == "ok":
        reviewer_relation = "supports"
    else:
        reviewer_relation = "refutes"

    reviewer_evidence_id = "E" + uuid.uuid4().hex[:8]
    reviewer_payload: dict[str, Any] = {
        "evidence_id": reviewer_evidence_id,
        "claim_id": active_claim_id,
        "relation": reviewer_relation,
        "level": "reported",
        "source_type": "llm",
        "source_id": "reviewer",
        "summary": ",".join(reviewer_verdict.reasons[:3])[:200],
    }
    if reviewer_verdict.malformed:
        reviewer_payload["limitations"] = "malformed_reviewer_json"
    reviewer_evidence_event = emit_event(
        ctx.conn,
        type="evidence.attached",
        payload=reviewer_payload,
        source_event_id=_find_claim_event_uid(emitted, active_claim_id),
        correlation=ctx.correlation,
    )
    emitted.append(reviewer_evidence_event)

    # Paradox contrast: verify_command passed BUT reviewer disagrees.
    # task.verified still fires (canonical exit-code proof); this is a
    # parallel audit row only.
    if (
        did_verify
        and reviewer_verdict.verdict == "fail"
        and active_relation == "supports"
    ):
        contrast_claim, contrast_evidence = _emit_claim_and_evidence(
            conn=ctx.conn,
            source_event_id=ctx.source_event_id("observation"),
            correlation=ctx.correlation,
            claim_type="Limitation",
            statement=(
                f"reviewer disagrees with verify_command pass for "
                f"{ctx.subject_ref}: "
                f"{','.join(reviewer_verdict.reasons[:2])[:120]}"
            ),
            subject_ref=ctx.subject_ref,
            relation="limits",
            level="reported",
            source_type="llm",
            source_id="reviewer",
        )
        emitted.extend((contrast_claim, contrast_evidence))


def _diff_evidence_extras(observation_slot: RawResult) -> dict[str, Any]:
    """Pull useful diff metadata onto the Evidence payload."""
    extras: dict[str, Any] = {}
    artifact_ref = observation_slot.payload.get("artifact_ref")
    if isinstance(artifact_ref, str):
        extras["artifact_ref"] = artifact_ref
        extras["artifact_path"] = artifact_ref
    artifact_path = observation_slot.payload.get("artifact_path")
    if isinstance(artifact_path, str):
        extras["artifact_path"] = artifact_path
    content_hash = observation_slot.payload.get("content_hash")
    if isinstance(content_hash, str):
        extras["content_hash"] = content_hash
    return extras


def _verify_evidence_extras(
    verification_slot: RawResult,
    *,
    observation_slot: RawResult | None = None,
) -> dict[str, Any]:
    """Pull useful verify_command metadata onto the Evidence payload."""
    extras: dict[str, Any] = {}
    exit_code = verification_slot.payload.get("exit_code")
    if isinstance(exit_code, int):
        extras["scope"] = f"exit_code={exit_code}"
    if observation_slot is not None:
        for key, value in _diff_evidence_extras(observation_slot).items():
            extras.setdefault(key, value)
    return extras


def _find_claim_event_uid(events: list[Event], claim_id: str) -> str:
    """Return the ``event_uid`` of the ``claim.created`` row for ``claim_id``."""
    for evt in events:
        if (
            evt.type == "claim.created"
            and evt.payload.get("claim_id") == claim_id
        ):
            return evt.event_uid
    # Fallback — should never happen because we just appended the
    # claim row above. Return claim_id itself so emit_event still
    # accepts (the cause-chain canary won't be happy but the event
    # registry won't reject).
    return claim_id


class ReviewerVerdictLike(Protocol):
    """Structural protocol mirroring :class:`jarvis.decision.reviewer.ReviewerVerdict`.

    Defined as a Protocol so :func:`interpret_verify_diff_bundle` names
    a stable type for callers without importing the reviewer module
    (whose import surface pulls in ``jarvis.decision.llm`` and bloats
    the interpreter's import graph). Callers pass the real
    :class:`ReviewerVerdict` dataclass — duck-typing via
    ``verdict`` / ``reasons`` / ``malformed`` attributes.
    """

    @property
    def verdict(self) -> Literal["ok", "fail"]:
        """One of ``"ok"`` / ``"fail"`` — see ``ReviewerVerdict.verdict``."""
        ...

    @property
    def reasons(self) -> tuple[str, ...]:
        """Reviewer rationale tuple — see ``ReviewerVerdict.reasons``."""
        ...

    @property
    def malformed(self) -> bool:
        """True iff the LLM's response was not valid JSON."""
        ...


__all__ = [
    "ReviewerVerdictLike",
    "VerifyVerdict",
    "interpret_verify_diff_bundle",
    "result_interpreter",
]
