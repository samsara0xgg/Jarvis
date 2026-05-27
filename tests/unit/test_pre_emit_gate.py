"""Unit tests for the Pre-emit Gate (ADR § Gate contracts).

Covers:

- Happy: Postcondition Claim with verified evidence on the active
  subject → allow_completion_language.
- Missing verified evidence → force_limitation_language.
- Text with completion keyword "完成" + no verified evidence →
  force_limitation AND downgrade_required=True.
- Empty draft → permission=force, downgrade_required=False (nothing
  to downgrade).
- response_hash is sha256 hex of the draft text.
"""

from __future__ import annotations

import hashlib

from jarvis.decision.gates import pre_emit_gate
from jarvis.shared import Event
from jarvis.state.projections import ClaimEvidenceProjection


def _make_projection(*events: Event) -> ClaimEvidenceProjection:
    """Fold a sequence of events into a Claim/Evidence projection."""
    return ClaimEvidenceProjection.from_events(events)


def _claim_event(
    *,
    claim_id: str,
    subject_ref: str,
    claim_type: str = "Postcondition",
    ts_epoch_ms: int = 1_000_000,
) -> Event:
    """Build a `claim.created` event for projection seeding."""
    return Event(
        event_uid=f"claim-uid-{claim_id}",
        type="claim.created",
        schema_version=1,
        ts_epoch_ms=ts_epoch_ms,
        payload={
            "claim_id": claim_id,
            "type": claim_type,
            "statement": f"{claim_type} for {subject_ref}",
            "subject_ref": subject_ref,
            "produced_by_event_id": "source-uid",
        },
        source_event_id="source-uid",
        correlation=None,
    )


def _evidence_event(
    *,
    evidence_id: str,
    claim_id: str,
    level: str,
    ts_epoch_ms: int = 1_000_001,
) -> Event:
    """Build an `evidence.attached` event for projection seeding."""
    return Event(
        event_uid=f"evidence-uid-{evidence_id}",
        type="evidence.attached",
        schema_version=1,
        ts_epoch_ms=ts_epoch_ms,
        payload={
            "evidence_id": evidence_id,
            "claim_id": claim_id,
            "relation": "supports",
            "level": level,
        },
        source_event_id=f"claim-uid-{claim_id}",
        correlation=None,
    )


# --- Happy path ------------------------------------------------------------


def test_pre_emit_allows_completion_when_verified_postcondition_exists():
    """Postcondition + verified evidence → allow_completion_language."""
    projection = _make_projection(
        _claim_event(claim_id="C1", subject_ref="task_X"),
        _evidence_event(evidence_id="E1", claim_id="C1", level="verified"),
    )
    plan = pre_emit_gate(
        "已完成对昨天 task 的审核。",
        projection,
        active_subject_ref="task_X",
    )

    assert plan.permission == "allow_completion_language"
    assert plan.downgrade_required is False
    assert "verified" in plan.active_claim_levels


# --- Force limitation paths ------------------------------------------------


def test_pre_emit_forces_limitation_when_no_verified_evidence():
    """Only reported evidence → force_limitation_language."""
    projection = _make_projection(
        _claim_event(claim_id="C1", subject_ref="task_X", claim_type="Report"),
        _evidence_event(evidence_id="E1", claim_id="C1", level="reported"),
    )
    plan = pre_emit_gate(
        "the agent reported back",
        projection,
        active_subject_ref="task_X",
    )

    assert plan.permission == "force_limitation_language"


def test_pre_emit_downgrade_required_when_completion_language_without_verification():
    """'完成' in draft + no verified evidence → downgrade_required=True."""
    projection = _make_projection(
        _claim_event(claim_id="C1", subject_ref="task_X", claim_type="Report"),
        _evidence_event(evidence_id="E1", claim_id="C1", level="reported"),
    )
    plan = pre_emit_gate(
        "昨天的 task 已完成。",
        projection,
        active_subject_ref="task_X",
    )

    assert plan.permission == "force_limitation_language"
    assert plan.downgrade_required is True


def test_pre_emit_downgrade_not_required_when_no_completion_keyword():
    """No completion keywords + no verified evidence → no downgrade rewrite."""
    projection = _make_projection()
    plan = pre_emit_gate(
        "agent reported back; awaiting Allen review",
        projection,
        active_subject_ref="task_X",
    )

    assert plan.permission == "force_limitation_language"
    assert plan.downgrade_required is False


def test_pre_emit_negation_lookbehind_treats_negated_completion_as_limitation():
    """Negated ``完成`` MUST NOT trip the completion detector.

    B-0003 fix: the gate's bare ``完成`` pattern previously matched the
    substring inside ``未完成``, downgrading canonical limitation
    phrasings like ``"Codex 超时,未完成"`` (ADR-0002 Negative-path
    appendix). The ``(?<![未没不])`` lookbehind restores the
    "negation = limitation" semantics.
    """
    projection = _make_projection(
        _claim_event(claim_id="C1", subject_ref="task_X", claim_type="Limitation"),
        _evidence_event(evidence_id="E1", claim_id="C1", level="reported"),
    )
    for limitation_text in (
        "Codex 超时，未完成",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
        "任务还没完成",
        "Codex 不完成验证",
    ):
        plan = pre_emit_gate(
            limitation_text,
            projection,
            active_subject_ref="task_X",
        )
        assert plan.downgrade_required is False, (
            f"text={limitation_text!r} unexpectedly tripped the completion "
            "detector despite the ``(?<![未没不])`` negative lookbehind."
        )


def test_pre_emit_response_hash_matches_sha256_of_text():
    """response_hash MUST equal sha256 hex of the draft text."""
    projection = _make_projection()
    text = "some draft response"
    plan = pre_emit_gate(text, projection, active_subject_ref="task_X")

    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert plan.response_hash == expected


def test_pre_emit_treats_unverified_postcondition_as_limitation():
    """Postcondition + only reported evidence is NOT verified."""
    projection = _make_projection(
        _claim_event(claim_id="C1", subject_ref="task_X", claim_type="Postcondition"),
        _evidence_event(evidence_id="E1", claim_id="C1", level="reported"),
    )
    plan = pre_emit_gate(
        "done",
        projection,
        active_subject_ref="task_X",
    )

    assert plan.permission == "force_limitation_language"
    assert plan.downgrade_required is True


def test_pre_emit_detects_done_keyword_case_insensitive():
    r"""``\bdone\b`` is matched case-insensitively."""
    projection = _make_projection()
    plan = pre_emit_gate("All DONE here", projection, active_subject_ref="task_X")

    assert plan.downgrade_required is True


# --- None subject pass-through (§3.4.12 v0: only gate consequential claims) ---


def test_pre_emit_passes_through_when_no_active_subject():
    """``active_subject_ref=None`` → pass through unchanged.

    Per spec §3.4.12 v0 the Pre-emit Gate only gates consequential
    claims (task status, agent completion, test result, device result,
    memory write, current mutable state). When the caller signals no
    subject (None), there is no consequential claim being made about
    anyone — the gate has nothing to enforce. The draft is shipped
    verbatim with ``permission=allow_completion_language`` and
    ``downgrade_required=False``. §13.1 three checks are vacuously
    satisfied (no claim, routine output_form, no agent self-report).

    Mirrors the §3.4.4 LLMSituationPacket schema where ``active_task?``
    is optional — None at the gate boundary is the structural reflection
    of an empty packet, not a failure mode.
    """
    projection = _make_projection()  # empty
    draft = "我可以帮你完成各种任务。"  # contains "完成" — would trip the str branch
    plan = pre_emit_gate(draft, projection, active_subject_ref=None)

    assert plan.text == draft, "draft must be shipped verbatim"
    assert plan.permission == "allow_completion_language"
    assert plan.downgrade_required is False
    assert plan.active_claim_levels == ()
    assert plan.output_risk_class == "routine"
    assert plan.required_gate_mode == "sentence"
    assert plan.response_hash == hashlib.sha256(draft.encode("utf-8")).hexdigest()
