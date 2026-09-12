"""ADR-0008 D10 — gated action cancellation (docs/goals/gated-action-cancel.md).

The ``action:`` EntityRegistry kind and its admission lookup (L2), the
Pre-action Gate's ``cancel_action`` arm (L3), the ``cancel_action`` tool
(L4), and the resolved / ambiguous / none answers — the latter driven
through the real ``decide()`` with a scripted LLM and a real ActionRunner.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast, get_type_hints

import pytest

from jarvis.decision import DecideContext, decide
from jarvis.decision.action_cancel import resolve_cancellable_action
from jarvis.decision.gates import pre_action_gate
from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.packet import assemble_packet
from jarvis.decision.policy import effective_policy
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.action_runner import (
    ActionRunner,
    ToolConcurrency,
    current_execution_context,
)
from jarvis.execution.tools import (
    ActionLifecycle,
    RawResult,
    ToolDefinition,
    build_default_registry,
    default_resource_key_resolver,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.shared.realtime import ResponseInterruptPolicy
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_action
from tests.integration.test_wave5_background_actions import _ack, _async_tool

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from jarvis.decision import DecideResult
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


# --- the decide()-driven rig: real L3, real L4, real runner, scripted LLM ----

_STASH_REF = "f" * 40
_NONE_ANSWER = "现在没有正在跑的动作，没有可以取消的。"  # noqa: RUF001 — Chinese punctuation.
_NOT_STARTED_ANSWER = "还没真正开始，没能停下。"  # noqa: RUF001 — Chinese punctuation.
_STOPPED_ANSWER = "已经停下了。"


def _text_result(text: str) -> ChatResult:
    """A final-text LLM reply."""
    return ChatResult(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        input_tokens=0,
        output_tokens=0,
        raw={},
        model_used="scripted",
        tokens_in=0,
        tokens_out=0,
    )


def _tool_call(name: str, arguments: Mapping[str, Any]) -> ChatResult:
    """A pure tool-call LLM reply."""
    return ChatResult(
        text=None,
        tool_calls=(
            ToolCall(call_id=f"call-{name}", name=name, arguments_json=json.dumps(arguments)),
        ),
        finish_reason="tool_calls",
        input_tokens=0,
        output_tokens=0,
        raw={},
        model_used="scripted",
        tokens_in=0,
        tokens_out=0,
    )


class _ScriptedLLM:
    """Replays one reply per `chat`; a `None` step echoes the last tool result's message.

    The echo is what a cooperative real LLM does with the deterministic
    `message` L3 hands it, so the spoken text under test is the code's,
    not the script's.
    """

    def __init__(self) -> None:
        self.script: list[ChatResult | None] = []
        self.model = "scripted"
        self.seen: list[list[dict[str, Any]]] = []

    @property
    def last_input_tokens(self) -> int | None:
        return 0

    @property
    def last_output_tokens(self) -> int | None:
        return 0

    @property
    def last_finish_reason(self) -> str | None:
        return "stop"

    @contextmanager
    def fresh_context(self) -> Iterator[_ScriptedLLM]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        assert self.script, "the scripted LLM ran out of replies"
        self.seen.append([dict(m) for m in messages])
        step = self.script.pop(0)
        if step is not None:
            return step
        last_tool = next(m for m in reversed(messages) if m.get("role") == "tool")
        return _text_result(str(json.loads(last_tool["content"]).get("message", "")))


@dataclass
class _Worker:
    """A declared-async, cancellable fixture worker.

    Emits `run.started` the way `spawn_worker_handler` does (sourced at
    its `action.running` uid), records its worker identity, optionally a
    stash ref, then waits until it is cancelled or released. A released
    worker writes its own `action.result_observed` terminal.
    """

    name: str
    stash: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def tool(self) -> ToolDefinition:
        """The ToolDefinition around this worker's body."""

        def _body(request: ActionRequest, conn: sqlite3.Connection) -> RawResult:
            context = current_execution_context()
            assert context is not None
            run_id = f"R-{request.action_id}"
            task_id = f"T-{request.action_id}"
            emit_event(
                conn,
                type="run.started",
                payload={"run_id": run_id, "task_id": task_id, "runner": "fixture"},
                source_event_id=context.running_event_uid,
                correlation={"action_id": request.action_id},
            )
            if self.stash:
                context.record_stash_ref(_STASH_REF)
            context.record_worker_identity(run_id=run_id, task_id=task_id)
            self.entered.set()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if context.is_cancel_requested:
                    return RawResult(
                        action_id=request.action_id,
                        semantics="error",
                        payload={"status": "cancelled"},
                        tool_output="{}",
                        error="cancelled",
                    )
                if self.release.is_set():
                    terminalize_action(
                        conn,
                        event_type="action.result_observed",
                        payload={
                            "action_id": request.action_id,
                            "semantics": "ack",
                            "tool_output": "{}",
                        },
                        source_event_id=context.running_event_uid,
                        correlation={"action_id": request.action_id},
                    )
                    return _ack(request)
                time.sleep(0.005)
            return _ack(request)

        return _async_tool(self.name, _body, cancellation_mode="terminate_process")


class _Rig:
    """One runtime root with the default registry (+ cancel_action) and fixture workers."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        workers: tuple[_Worker, ...],
        keys: Mapping[str, str],
    ) -> None:
        """Wire the log, the runner, the registry and a `decide()` context."""
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.runner = ActionRunner(
            event_log_path=self.paths.event_log,
            max_concurrent_runs=4,
            lease_timeout_s=10.0,
        )
        self.workers = workers

        def _resolver(
            request: ActionRequest,
            tool_def: ToolDefinition,
            conn: sqlite3.Connection,
        ) -> ToolConcurrency:
            key = keys.get(tool_def.name)
            if key is None:
                return default_resource_key_resolver(request, tool_def, conn)
            # No cleanup debt: the lease frees at quiescence, so a queued
            # sibling can run without a driver-side turn finalizer.
            return ToolConcurrency(resource_keys=(key,), mode="write_exclusive")

        self.registry = build_default_registry(
            action_runner=self.runner,
            resource_key_resolver=_resolver,
            background_async=True,
        )
        for worker in workers:
            self.registry.register(worker.tool())
        self.llm = _ScriptedLLM()
        self.ctx = DecideContext(
            conn=self.conn,
            runtime_paths=cast("Any", self.paths),
            tool_registry=cast("Any", self.registry),
            lifecycle=cast("Any", ActionLifecycle()),
            llm_client=cast("Any", self.llm),
            system_prompt="rig",
        )
        self._turns = 0

    def turn(self, transcript: str, *steps: ChatResult | None) -> DecideResult:
        """Run one real `decide()` on a fresh `surface.user_intent`."""
        self._turns += 1
        turn_id = f"T{self._turns}"
        trigger = emit_event(
            self.conn,
            type="surface.user_intent",
            payload={
                "transcript": transcript,
                "turn_id": turn_id,
                "channel": "cli_stdin",
                "language": "zh-CN",
            },
            correlation={"turn_id": turn_id},
        )
        self.llm.script = list(steps)
        return decide(trigger, self.ctx)

    def start(self, worker: _Worker, *, wait_running: bool = True) -> str:
        """Dispatch `worker` through a gated turn; return its action_id."""
        self.turn("跑一下", _tool_call(worker.name, {}))
        proposed = [p for p in self.payloads("action.proposed") if p["tool_name"] == worker.name]
        action_id = str(proposed[-1]["action_id"])
        if wait_running:
            assert worker.entered.wait(timeout=10), f"{worker.name} never entered"
        return action_id

    def cancel(self, target: str | None = None) -> DecideResult:
        """One cancel turn: the LLM calls `cancel_action`, then echoes the ack."""
        arguments: dict[str, Any] = {"reason": "user_stop"}
        if target is not None:
            arguments["target_action_id"] = target
        return self.turn("取消刚才那个", _tool_call("cancel_action", arguments), None)

    def events(self, event_type: str) -> list[Event]:
        """Every row of `event_type`, in log order."""
        return [e for e in iter_events(self.conn) if e.type == event_type]

    def payloads(self, event_type: str, action_id: str | None = None) -> list[dict[str, Any]]:
        """Payloads of `event_type`, optionally only for one action."""
        return [
            dict(e.payload)
            for e in self.events(event_type)
            if action_id is None or e.payload.get("action_id") == action_id
        ]

    def terminals_of(self, action_id: str) -> list[str]:
        """The canonical terminal types written for `action_id`."""
        return [t for t in _ACTION_TERMINALS if self.payloads(t, action_id)]

    def cancel_ids(self) -> set[str]:
        """The action_ids of every `cancel_action` proposal."""
        proposed = self.payloads("action.proposed")
        return {p["action_id"] for p in proposed if p["tool_name"] == "cancel_action"}

    def cancel_gate(self) -> dict[str, Any]:
        """The `pre_action` verdict for the one `cancel_action` proposal."""
        cancel_ids = self.cancel_ids()
        gates = [
            p
            for p in self.payloads("gate.evaluated")
            if p["gate"] == "pre_action" and p.get("action_id") in cancel_ids
        ]
        assert len(gates) == 1, gates
        return gates[0]

    def close(self) -> None:
        """Release every worker, drain the runner, close the log."""
        for worker in self.workers:
            worker.release.set()
        self.runner.shutdown(cancel=True, reason="teardown", timeout_s=10.0)
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


def _spoken(result: DecideResult) -> str:
    """The turn's final text."""
    assert result.response_plan is not None
    return result.response_plan.text


def _open_actions_notes(rig: _Rig) -> list[str]:
    """Every open-actions system note the LLM saw on its last chat call."""
    return [
        str(m["content"])
        for m in rig.llm.seen[-1]
        if isinstance(m.get("content"), str) and "Open actions" in m["content"]
    ]


def _assert_no_confirmation_and_no_lease(rig: _Rig) -> None:
    """The cancel path asks nothing and mints nothing."""
    assert not [e for e in iter_events(rig.conn) if e.type.startswith("confirmation.")]
    assert rig.payloads("authorization.lease_granted") == []
    for gate in rig.payloads("gate.evaluated"):
        assert "lease_id" not in gate


# --- (1) resolution -----------------------------------------------------------


def test_resolve_cancellable_action_single_ambiguous_none(tmp_path: Path) -> None:
    """One open → resolved; two → ambiguous with both; zero → none."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        trigger = emit_event(
            conn, type="surface.user_intent", payload={"transcript": "停", "turn_id": "T0"},
        )
        assert resolve_cancellable_action(assemble_packet(trigger, conn)).kind == "none"

        _admit(conn, "A1")
        single = resolve_cancellable_action(assemble_packet(trigger, conn))
        assert single.kind == "resolved"
        assert (single.action_id, single.action_ref) == ("A1", "action:A1")

        _admit(conn, "A2")
        packet = assemble_packet(trigger, conn)
        both = resolve_cancellable_action(packet)
        assert both.kind == "ambiguous"
        assert set(both.candidates) == {"A1", "A2"}
        # A raw id is trusted only when it names a current entry.
        assert resolve_cancellable_action(packet, requested="A2").action_id == "A2"
        assert resolve_cancellable_action(packet, requested="action:A1").action_id == "A1"
        assert resolve_cancellable_action(packet, requested="A-forged").kind == "ambiguous"

        emit_event(conn, type="action.cancelled", payload={"action_id": "A1"})
        emit_event(conn, type="action.failed", payload={"action_id": "A2"})
        gone = resolve_cancellable_action(assemble_packet(trigger, conn), requested="A1")
        assert gone.kind == "none"
    finally:
        conn.close()


# --- (9) response cancellation never touches an action ------------------------


def test_response_cancellation_never_cancels_an_action(tmp_path: Path) -> None:
    """`action_action` is still `Literal["never"]`; a `response.cancelled` writes no terminal."""
    assert get_type_hints(ResponseInterruptPolicy)["action_action"] == Literal["never"]
    worker = _Worker("worker_a")
    rig = _Rig(tmp_path, workers=(worker,), keys={"worker_a": "repo:a"})
    try:
        target = rig.start(worker)
        emit_event(
            rig.conn,
            type="response.cancelled",
            payload={
                "response_id": "RESP-1",
                "response_group_id": "RG-1",
                "turn_id": "T1",
                "reason": "allen_stop",
            },
            correlation={"turn_id": "T1"},
        )
        time.sleep(0.05)
        assert rig.payloads("action.cancelled") == []
        assert rig.runner.context_of(target) is not None
        assert rig.terminals_of(target) == []
    finally:
        rig.close()
