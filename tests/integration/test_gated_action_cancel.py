"""ADR-0008 D10 — gated action cancellation (docs/goals/gated-action-cancel.md).

The ``action:`` EntityRegistry kind and its admission lookup (L2), the
Pre-action Gate's ``cancel_action`` arm (L3), the ``cancel_action`` tool
(L4), and the resolved / ambiguous / none answers — the latter driven
through the real ``decide()`` with a scripted LLM and a real ActionRunner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.gates import pre_action_gate
from jarvis.decision.packet import assemble_packet
from jarvis.decision.policy import effective_policy, validate_requires_confirmation
from jarvis.execution.action_runner import ActionRunner
from jarvis.execution.tools import (
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
    default_resource_key_resolver,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision.gates import GateResult
    from jarvis.shared import Event
    from jarvis.state.projections import ActionAdmissions, ProjectionSet

_ACTION_TERMINALS: tuple[str, ...] = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
"""The four canonical action terminals; each must evict the `action:` entry."""


# --- helpers -----------------------------------------------------------------


def _admit(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    lease_id: str | None = None,
    outcome: str = "pass",
) -> tuple[Event, Event]:
    """Emit the pre-action verdict and the dispatch that open one action."""
    gate_payload: dict[str, object] = {
        "gate": "pre_action",
        "outcome": outcome,
        "reasons": [],
        "action_id": action_id,
    }
    if lease_id is not None:
        gate_payload["lease_id"] = lease_id
    gate = emit_event(conn, type="gate.evaluated", payload=gate_payload)
    dispatched = emit_event(
        conn,
        type="action.dispatched",
        payload={"action_id": action_id},
        correlation={"action_id": action_id},
    )
    return gate, dispatched


def _terminal_payload(event_type: str, action_id: str) -> dict[str, object]:
    """The minimal registry-valid payload for one action terminal."""
    payload: dict[str, object] = {"action_id": action_id}
    if event_type == "action.result_observed":
        payload["semantics"] = "ack"
    return payload


def _projections(conn: sqlite3.Connection) -> ProjectionSet:
    """Fold the whole log the way `assemble_packet` does."""
    return read_decision_snapshot(conn).projections


# --- (8) the `action:` fold and its admission lookup --------------------------


def test_action_dispatched_registers_the_entity_and_its_admission(tmp_path: Path) -> None:
    """`action.dispatched` opens an exact `action:` entry keyed to its passing gate."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, dispatched = _admit(conn, "A1")
        projections = _projections(conn)

        entry = projections.entity_registry.get("action:A1")
        assert entry is not None
        assert entry.entity_type == "action"
        assert entry.canonical == "A1"
        assert entry.aliases == ()
        assert entry.confidence == "exact"
        assert entry.source_event_id == dispatched.event_uid
        assert entry.last_seen_ms == dispatched.ts_epoch_ms

        admission = projections.action_admissions.get("A1")
        assert admission is not None
        assert admission.dispatched_event_uid == dispatched.event_uid
        assert admission.admission_gate_uid == gate.event_uid
        assert admission.lease_id is None
        assert admission.run_id is None
    finally:
        conn.close()


def test_a_leased_admission_carries_its_lease_id(tmp_path: Path) -> None:
    """D10's leased case: the `lease_id` on the passing verdict rides along."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A1", lease_id="lease-1")
        admission = _projections(conn).action_admissions.get("A1")
        assert admission is not None
        assert admission.lease_id == "lease-1"
    finally:
        conn.close()


def test_only_a_passing_pre_action_verdict_counts_as_the_admission(tmp_path: Path) -> None:
    """A dispatch whose last verdict refused (or that had none) has no gate uid."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A-refused", outcome="refuse")
        emit_event(conn, type="action.dispatched", payload={"action_id": "A-ungated"})
        admissions = _projections(conn).action_admissions
        refused = admissions.get("A-refused")
        ungated = admissions.get("A-ungated")
        assert refused is not None
        assert refused.admission_gate_uid is None
        assert ungated is not None
        assert ungated.admission_gate_uid is None
    finally:
        conn.close()


def test_run_id_joins_only_through_run_started(tmp_path: Path) -> None:
    """`run.started` joins by its source (the `action.running` uid) or correlation.

    `action.running` carries no `run_id` and is never read for one.
    """
    conn = open_event_log(tmp_path / "events.db")
    try:
        _, dispatched = _admit(conn, "A1")
        running = emit_event(
            conn,
            type="action.running",
            payload={"action_id": "A1"},
            source_event_id=dispatched.event_uid,
        )
        assert _projections(conn).action_admissions.get("A1").run_id is None  # type: ignore[union-attr]
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R1", "task_id": "T1", "runner": "fixture"},
            source_event_id=running.event_uid,
        )
        _admit(conn, "A2")
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R2", "task_id": "T2", "runner": "fixture"},
            correlation={"action_id": "A2"},
        )
        admissions = _projections(conn).action_admissions
        assert admissions.get("A1").run_id == "R1"  # type: ignore[union-attr]
        assert admissions.get("A2").run_id == "R2"  # type: ignore[union-attr]
    finally:
        conn.close()


@pytest.mark.parametrize("terminal", _ACTION_TERMINALS)
def test_each_action_terminal_evicts_the_entry(tmp_path: Path, terminal: str) -> None:
    """The `action:` universe is exactly the non-terminal set."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A1")
        _admit(conn, "A2")
        assert "action:A1" in _projections(conn).entity_registry
        emit_event(conn, type=terminal, payload=_terminal_payload(terminal, "A1"))
        projections = _projections(conn)
        assert "action:A1" not in projections.entity_registry
        assert projections.action_admissions.get("A1") is None
        # The sibling is untouched.
        assert "action:A2" in projections.entity_registry
        assert projections.action_admissions.get("A2") is not None
    finally:
        conn.close()


# --- (6)/(8) the Pre-action Gate's `cancel_action` arm ------------------------

_CANCEL_SURFACE = {CallerPrincipal.JARVIS_LLM: frozenset({"cancel_action", "create_task"})}
_CANCEL_TOOL_LIKE = SimpleNamespace(requires_entity=True)
_MISMATCH_CASES: tuple[tuple[str, str, str | None], ...] = (
    # (case, target_action_id, claimed uid — "<gate>" for the real one, None for absent)
    ("stale", "A1", "evt-stale"),
    ("absent", "A1", None),
    ("unknown_target", "A-nope", "<gate>"),
    ("no_admissions", "A1", "<gate>"),
    ("ungated_target", "A-ungated", "evt-any"),
)


def _cancel_request(
    target_action_id: str,
    *,
    claimed_gate_uid: str | None,
    tool_name: str = "cancel_action",
) -> ActionRequest:
    """One L2 `cancel_action` request shaped the way L3 builds it."""
    return ActionRequest(
        action_id="C1",
        tool_name=tool_name,
        target_entity_ref=f"action:{target_action_id}",
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L2",
        arguments={"target_action_id": target_action_id, "reason": "user_stop"},
        authorization_lease=None,
        run_id=None,
        turn_id="T-cancel",
        payload=(
            None
            if claimed_gate_uid is None
            else {"authorization_gate_event_uid": claimed_gate_uid}
        ),
    )


def _gate(
    conn: sqlite3.Connection,
    request: ActionRequest,
    *,
    admissions: ActionAdmissions | None,
) -> GateResult:
    """Run the real gate against the folded registry and the given lookup."""
    projections = _projections(conn)
    return pre_action_gate(
        request,
        effective_policy(_CANCEL_SURFACE),
        projections.task_ledger.snapshot(),
        tool_def=_CANCEL_TOOL_LIKE,
        entity_registry=projections.entity_registry,
        action_admissions=admissions,
    )


def test_the_cancel_arm_passes_when_the_request_names_the_admitting_gate(
    tmp_path: Path,
) -> None:
    """A well-formed L2 cancel passes with no lease and no confirmation."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, _ = _admit(conn, "A1")
        admissions = _projections(conn).action_admissions
        result = _gate(
            conn, _cancel_request("A1", claimed_gate_uid=gate.event_uid), admissions=admissions,
        )
        assert result.outcome == "pass", result.reasons
        assert result.check_results["admission_matched"] is True
        assert result.check_results["entity_trusted"] is True
        assert result.check_results["lease_validated"] is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("case", "target", "claimed"),
    _MISMATCH_CASES,
    ids=[case[0] for case in _MISMATCH_CASES],
)
def test_a_mismatched_admission_refuses_and_never_asks(
    tmp_path: Path,
    case: str,
    target: str,
    claimed: str | None,
) -> None:
    """Absent, unknown or mismatched gate uid → `refuse`, never `confirm_required`."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, _ = _admit(conn, "A1")
        emit_event(conn, type="action.dispatched", payload={"action_id": "A-ungated"})
        # The unknown target still needs to be trusted as an entity so the
        # refusal is attributable to the arm under test, not to check 2.
        emit_event(conn, type="action.dispatched", payload={"action_id": "A-nope"})
        projections = _projections(conn)
        admissions = None if case == "no_admissions" else projections.action_admissions
        if case == "unknown_target":
            admissions = type(projections.action_admissions)(
                by_action_id={"A1": projections.action_admissions.by_action_id["A1"]},
            )
        claimed_uid = gate.event_uid if claimed == "<gate>" else claimed
        result = _gate(
            conn, _cancel_request(target, claimed_gate_uid=claimed_uid), admissions=admissions,
        )
        assert result.outcome == "refuse", (case, result.reasons)
        assert result.check_results["admission_matched"] is False
        assert result.check_results["entity_trusted"] is True
    finally:
        conn.close()


def test_a_non_cancel_request_never_sees_the_arm(tmp_path: Path) -> None:
    """Checks 1-4 are unchanged: the arm runs only for `cancel_action`."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A1")
        request = _cancel_request("A1", claimed_gate_uid=None, tool_name="create_task")
        result = _gate(conn, request, admissions=_projections(conn).action_admissions)
        assert "admission_matched" not in result.check_results
    finally:
        conn.close()


def test_the_entity_check_refuses_a_terminated_action(tmp_path: Path) -> None:
    """A live `action:` id passes check 2; the same id after its terminal refuses."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, _ = _admit(conn, "A1")
        request = _cancel_request("A1", claimed_gate_uid=gate.event_uid)
        live = _gate(conn, request, admissions=_projections(conn).action_admissions)
        assert live.outcome == "pass"
        emit_event(conn, type="action.cancelled", payload={"action_id": "A1"})
        after = _gate(conn, request, admissions=_projections(conn).action_admissions)
        assert after.outcome == "refuse"
        assert after.check_results["entity_trusted"] is False
    finally:
        conn.close()


def test_the_packet_carries_the_admission_lookup(tmp_path: Path) -> None:
    """`assemble_packet` threads `ProjectionSet.action_admissions` onto the packet."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, _ = _admit(conn, "A1")
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "取消刚才那个", "turn_id": "T1"},
            correlation={"turn_id": "T1"},
        )
        packet = assemble_packet(trigger, conn)
        admission = packet.action_admissions.get("A1")
        assert admission is not None
        assert admission.admission_gate_uid == gate.event_uid
        assert "action:A1" in packet.entity_registry
    finally:
        conn.close()


# --- (10) the L4 tool: registered only with a runner, no resource key --------


def _cancel_tool_def(registry: ToolRegistry) -> ToolDefinition:
    """The registered `cancel_action` definition."""
    return registry.for_caller(CallerPrincipal.JARVIS_LLM)[-1]


def test_cancel_action_is_registered_only_when_a_runner_is_supplied(tmp_path: Path) -> None:
    """Without an ActionRunner the LLM's tool list is byte-identical to today's."""
    without = build_default_registry()
    runner = ActionRunner(event_log_path=tmp_path / "events.db", max_concurrent_runs=1)
    try:
        with_runner = build_default_registry(action_runner=runner)
        names_without = [t.name for t in without.for_caller(CallerPrincipal.JARVIS_LLM)]
        names_with = [t.name for t in with_runner.for_caller(CallerPrincipal.JARVIS_LLM)]
        assert "cancel_action" not in names_without
        assert names_with == [*names_without, "cancel_action"]

        tool = _cancel_tool_def(with_runner)
        assert tool.name == "cancel_action"
        assert tool.risk_level == "L2"
        assert tool.requires_confirmation is False
        assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
        assert tool.result_semantics == "ack"
        assert tool.is_async is False
        assert tool.domain == "agent_control"
        assert tool.read_only is False
        assert tool.requires_entity is True
        assert tool.post_action_check is None
        assert tool.result_budget_s is None
        assert tool.input_schema["required"] == ["target_action_id", "reason"]
        assert tool.input_schema["additionalProperties"] is False
        assert set(tool.input_schema["properties"]) == {"target_action_id", "reason"}
        # Boot validation: L2 under the L3 threshold must declare False.
        validate_requires_confirmation(
            with_runner.for_caller(CallerPrincipal.JARVIS_LLM),
            effective_policy().confirmation_threshold,
        )
    finally:
        runner.shutdown(wait=False)


def test_cancel_action_takes_no_resource_key(tmp_path: Path) -> None:
    """The cancel signals a handle; it must not queue behind the target's lease."""
    runner = ActionRunner(event_log_path=tmp_path / "events.db", max_concurrent_runs=1)
    conn = open_event_log(tmp_path / "events.db")
    try:
        tool = _cancel_tool_def(build_default_registry(action_runner=runner))
        request = _cancel_request("A1", claimed_gate_uid="evt-1")
        concurrency = default_resource_key_resolver(request, tool, conn)
        assert concurrency.resource_keys == ()
        assert concurrency.mode == "read_shared"
    finally:
        conn.close()
        runner.shutdown(wait=False)
