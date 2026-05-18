"""Unit tests for L3 invariant gates (ADR § Gate contracts).

Covers Pre-action Gate four MUST-checks, plus the integrity of
``GateResult`` fields:

- Pass case: caller in surface, entity in open_tasks, risk under
  ceiling.
- Refuse case: caller not in surface → caller_allowed=False.
- Refuse case: entity not in open_tasks → entity_trusted=False.
- Refuse case: risk above ceiling → risk_within_ceiling=False.
- All passes carry non-empty ``reasons``.
- ``check_results`` map carries the four canonical bools.
"""

from __future__ import annotations

from jarvis.decision.gates import GateResult, pre_action_gate
from jarvis.decision.policy import effective_policy
from jarvis.shared import ActionRequest, CallerPrincipal, Event
from jarvis.state.projections import TaskLedger, TaskLedgerSnapshot


def _build_snapshot(*events: Event) -> TaskLedgerSnapshot:
    """Build a TaskLedgerSnapshot from `task.created` events."""
    ledger = TaskLedger.from_events(events)
    return ledger.snapshot()


def _task_event(task_id: str, *, event_uid: str = "evt-0001") -> Event:
    """Synthesize a task.created event with reasonable defaults."""
    return Event(
        event_uid=event_uid,
        type="task.created",
        schema_version=1,
        ts_epoch_ms=1_000_000,
        payload={"task_id": task_id, "goal": f"goal for {task_id}"},
        source_event_id=None,
        correlation=None,
    )


def _action_request(
    *,
    tool_name: str = "spawn_worker",
    caller: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
    target: str | None = "task_X",
    risk: str = "L2",
) -> ActionRequest:
    """Build a default ActionRequest for gate inputs."""
    return ActionRequest(
        action_id="A1",
        tool_name=tool_name,
        target_entity_ref=target,
        caller_principal=caller,
        risk_level=risk,  # type: ignore[arg-type]
        arguments={},
        authorization_lease=None,
        run_id=None,
        turn_id="T1",
    )


# --- Happy path ------------------------------------------------------------


def test_pre_action_gate_passes_when_all_checks_pass():
    """spawn_worker by JARVIS_LLM on an open task at L2 → pass."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(_action_request(), policy, snapshot)

    assert isinstance(result, GateResult)
    assert result.outcome == "pass"
    assert result.check_results["caller_allowed"] is True
    assert result.check_results["entity_trusted"] is True
    assert result.check_results["risk_within_ceiling"] is True
    assert result.check_results["lease_validated"] is True


def test_pre_action_gate_reasons_non_empty_on_pass():
    """ADR Acceptance C5: passing gate MUST carry non-empty reasons."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(_action_request(), policy, snapshot)

    assert result.reasons, "reasons must be non-empty even on pass"
    assert len(result.reasons) >= 4  # one per MUST-check that ran


def test_pre_action_gate_check_results_contain_four_canonical_bools():
    """check_results carries the four canonical keys per ADR contract."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(_action_request(), policy, snapshot)

    for key in ("caller_allowed", "entity_trusted", "risk_within_ceiling", "lease_validated"):
        assert key in result.check_results


# --- Refuse paths ----------------------------------------------------------


def test_pre_action_gate_refuses_when_caller_not_allowed():
    """OBSERVER cannot call spawn_worker per Day-1 policy surface."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(
        _action_request(caller=CallerPrincipal.OBSERVER),
        policy,
        snapshot,
    )

    assert result.outcome == "refuse"
    assert result.check_results["caller_allowed"] is False


def test_pre_action_gate_refuses_when_entity_not_in_open_tasks():
    """Target task_id not in open ledger snapshot → entity_trusted=False."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(
        _action_request(target="task_nonexistent"),
        policy,
        snapshot,
    )

    assert result.outcome == "refuse"
    assert result.check_results["entity_trusted"] is False


def test_pre_action_gate_allows_none_target_entity():
    """No target → entity check is a no-op pass."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(
        _action_request(target=None),
        policy,
        snapshot,
    )

    assert result.check_results["entity_trusted"] is True


def test_pre_action_gate_refuses_when_risk_above_ceiling():
    """L3 risk exceeds Day-1 L2 autonomy_ceiling → refuse / confirm_required."""
    snapshot = _build_snapshot(_task_event("task_X"))
    policy = effective_policy()
    result = pre_action_gate(
        _action_request(risk="L4"),
        policy,
        snapshot,
    )

    assert result.outcome in ("refuse", "confirm_required")
    assert result.check_results["risk_within_ceiling"] is False
