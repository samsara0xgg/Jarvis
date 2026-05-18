"""Unit tests for ``jarvis.decision.gates.attention_policy`` (ADR § Stub strategy).

Day-1 attention rules:

- Verified Postcondition just emitted for the current subject →
  ``"voice_notify"``.
- ``worker.reported`` trigger without verified evidence → ``"silent_log"``.
- Default → ``"queue_review"``.
"""

from __future__ import annotations

from jarvis.decision.gates import attention_policy
from jarvis.decision.packet import SituationPacket
from jarvis.shared import Event
from jarvis.state.projections import ClaimEvidenceProjection, TaskLedger


def _utterance_event(*, turn_id: str = "T1") -> Event:
    """Build an utterance.received trigger."""
    return Event(
        event_uid="utt-uid-1",
        type="utterance.received",
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
        payload={"evidence_id": evidence_id, "claim_id": claim_id, "level": level},
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
