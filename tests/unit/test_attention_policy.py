"""Unit tests for ``jarvis.decision.gates.attention_policy`` (ADR § Stub strategy).

Day-1 attention rules (incl. the B-0005/B-0006 Limitation routing pinned
by the ADR-0002 amendment, 2026-08-10):

- Verified Postcondition just emitted for the current subject →
  ``"voice_notify"``.
- ``worker.reported`` trigger + a Limitation Claim emitted this turn →
  ``"voice_notify"`` (K5 row: verify-fail / reviewer-fail must speak).
- ``worker.reported`` trigger without verified evidence → ``"silent_log"``.
- ``action.timeout_assumed`` / ``action.failed`` + Limitation →
  ``"queue_review"`` (pinned: Allen is typically away after a long wait;
  badge escalation deferred until the Inherent cockpit exists).
- Default → ``"queue_review"``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import jarvis.decision as decision_pkg
from jarvis.decision.gates import attention_policy
from jarvis.decision.packet import SituationPacket
from jarvis.shared import Event
from jarvis.state.projections import (
    ClaimEvidenceProjection,
    StatusBoard,
    TaskLedger,
)


def _utterance_event(*, turn_id: str = "T1") -> Event:
    """Build a surface.user_intent trigger."""
    return Event(
        event_uid="utt-uid-1",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=1_000_000,
        payload={"transcript": "hi", "turn_id": turn_id},
        source_event_id=None,
        correlation={"turn_id": turn_id},
    )


def _worker_reported_event(
    *,
    task_id: str | None = "task_X",
    run_id: str = "R1",
    action_id: str = "A1",
) -> Event:
    """Build a worker.reported trigger with the conventional correlation."""
    correlation: dict[str, str] = {"run_id": run_id, "action_id": action_id}
    if task_id is not None:
        correlation["task_id"] = task_id
    return Event(
        event_uid="worker-uid-1",
        type="worker.reported",
        schema_version=1,
        ts_epoch_ms=1_000_001,
        payload={"run_id": run_id, "action_id": action_id, "status": "reported_complete"},
        source_event_id=None,
        correlation=correlation,
    )


def _claim_event(claim_id: str, subject_ref: str, *, claim_type: str = "Postcondition") -> Event:
    """Build a claim.created event for projection seeding."""
    return Event(
        event_uid=f"claim-uid-{claim_id}",
        type="claim.created",
        schema_version=1,
        ts_epoch_ms=1_000_002,
        payload={
            "claim_id": claim_id,
            "type": claim_type,
            "statement": "stmt",
            "subject_ref": subject_ref,
            "produced_by_event_id": "source-uid",
        },
        source_event_id="source-uid",
        correlation=None,
    )


def _evidence_event(evidence_id: str, claim_id: str, level: str) -> Event:
    """Build an evidence.attached event for projection seeding."""
    return Event(
        event_uid=f"evidence-uid-{evidence_id}",
        type="evidence.attached",
        schema_version=1,
        ts_epoch_ms=1_000_003,
        payload={
            "evidence_id": evidence_id,
            "claim_id": claim_id,
            "relation": "supports",
            "level": level,
        },
        source_event_id=f"claim-uid-{claim_id}",
        correlation=None,
    )


def _task_event(task_id: str) -> Event:
    """Build a task.created event."""
    return Event(
        event_uid=f"task-uid-{task_id}",
        type="task.created",
        schema_version=1,
        ts_epoch_ms=900_000,
        payload={"task_id": task_id, "goal": f"goal {task_id}"},
        source_event_id=None,
        correlation=None,
    )


def _build_packet(trigger: Event, *seed_events: Event) -> SituationPacket:
    """Build a SituationPacket from seed events for attention tests."""
    ledger = TaskLedger.from_events(seed_events)
    snapshot = ledger.snapshot()
    return SituationPacket(
        trigger_event=trigger,
        recent_trace=tuple(seed_events),
        task_ledger_snapshot=snapshot,
        open_tasks=snapshot.open_tasks(),
        current_turn_id="T1",
        current_run_id=None,
        status_board=StatusBoard(),
    )


def test_attention_voice_notify_on_verified_postcondition():
    """Verified Postcondition for active subject → voice_notify."""
    task = _task_event("task_X")
    claim = _claim_event("C1", "task_X", claim_type="Postcondition")
    evidence = _evidence_event("E1", "C1", "verified")
    projection = ClaimEvidenceProjection.from_events([claim, evidence])
    packet = _build_packet(_utterance_event(), task, claim, evidence)

    assert attention_policy(packet, projection) == "voice_notify"


def test_attention_silent_log_on_worker_reported_without_verified():
    """worker.reported trigger + only reported evidence → silent_log."""
    task = _task_event("task_X")
    claim = _claim_event("C1", "task_X", claim_type="Report")
    evidence = _evidence_event("E1", "C1", "reported")
    projection = ClaimEvidenceProjection.from_events([claim, evidence])
    packet = _build_packet(_worker_reported_event(), task, claim, evidence)

    assert attention_policy(packet, projection) == "silent_log"


def test_attention_default_is_queue_review():
    """Utterance trigger with no verified evidence → queue_review."""
    task = _task_event("task_X")
    projection = ClaimEvidenceProjection.from_events([])
    packet = _build_packet(_utterance_event(), task)

    assert attention_policy(packet, projection) == "queue_review"


def _timeout_event(*, action_id: str = "A1", run_id: str = "R1") -> Event:
    """Build an action.timeout_assumed trigger with the conventional correlation."""
    return Event(
        event_uid="timeout-uid-1",
        type="action.timeout_assumed",
        schema_version=1,
        ts_epoch_ms=1_000_004,
        payload={"action_id": action_id, "reason": "result_expected_by exceeded"},
        source_event_id=None,
        correlation={"run_id": run_id, "action_id": action_id, "task_id": "task_X"},
    )


def test_attention_voice_notify_on_worker_reported_limitation():
    """B-0006: worker.reported + Limitation emitted this turn → voice_notify.

    The K5 ADR row mandates the reviewer/verify-fail limitation utterance
    delivers through ``say``; before the 2026-08-10 amendment this path
    fell into the ``silent_log`` rule and ``delivered_via=[]``.
    """
    task = _task_event("task_X")
    claim = _claim_event("C1", "task_X", claim_type="Limitation")
    evidence = _evidence_event("E1", "C1", "executed")
    projection = ClaimEvidenceProjection.from_events([claim, evidence])
    packet = _build_packet(_worker_reported_event(), task, claim, evidence)

    assert (
        attention_policy(packet, projection, limitation_emitted=True) == "voice_notify"
    )


def test_attention_queue_review_on_timeout_limitation():
    """B-0005 pinned: timeout/failed trigger + Limitation → queue_review.

    Quiet-first: after a worker timeout Allen has typically walked away;
    the limitation is queued for review rather than spoken. Escalation to
    a badge surface is deferred until the Inherent cockpit exists.
    """
    task = _task_event("task_X")
    claim = _claim_event("C1", "task_X", claim_type="Limitation")
    evidence = _evidence_event("E1", "C1", "reported")
    projection = ClaimEvidenceProjection.from_events([claim, evidence])
    packet = _build_packet(_timeout_event(), task, claim, evidence)

    assert (
        attention_policy(packet, projection, limitation_emitted=True) == "queue_review"
    )


def test_finalize_response_passes_limitation_emitted() -> None:
    """Wiring: ``_finalize_response`` computes ``limitation_emitted`` from scratch.

    AST walk (same pattern as ``test_finalize_response_promotes_silent_log_on
    _hard_refusal``): the finalizer must scan ``scratch.events`` for a
    ``claim.created`` event with ``type == "Limitation"`` and pass the flag
    into ``attention_policy``. Without this, the gates-level rule above can
    never fire and the K5 burn regresses to ``delivered_via=[]``.
    """
    source_path = Path(decision_pkg.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    finalize_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_response":
            finalize_fn = node
            break
    assert finalize_fn is not None, "_finalize_response not found in module AST"

    string_constants: set[str] = set()
    names: set[str] = set()
    for sub in ast.walk(finalize_fn):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            string_constants.add(sub.value)
        elif isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.keyword) and sub.arg is not None:
            names.add(sub.arg)

    assert "limitation_emitted" in names, (
        "B-0005/B-0006 regression: _finalize_response no longer computes or "
        "passes limitation_emitted — Limitation routing can never fire."
    )
    assert "Limitation" in string_constants, (
        "B-0005/B-0006 regression: _finalize_response no longer matches "
        "claim.created type 'Limitation' when computing limitation_emitted."
    )
    assert "claim.created" in string_constants, (
        "B-0005/B-0006 regression: _finalize_response no longer scans "
        "scratch.events for 'claim.created' rows."
    )
