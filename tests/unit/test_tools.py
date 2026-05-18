"""Unit tests for `jarvis.execution.tools` (ADR-0001 Acceptance B + ADR-0002 Step 10).

Covers:
    - Default registry shape (names + caller scope + risk + semantics + async flag).
    - `spawn_worker` dispatcher events (dispatched + running + run.started)
      with the Day-2 Codex path mocked.
    - `verify_diff` three paths (match / fail / missing).
    - Caller scoping (CallerNotAllowedError), UnknownToolError,
      DuplicateToolError, dispatch precondition (lifecycle == authorized).

The Day-2 spawn_worker happy / negative / timeout / version-too-low / dirty-tree
chains live in ``tests/unit/test_spawn_worker_real.py`` so the dispatcher tests
in this file stay focused on the registry envelope.
"""

from __future__ import annotations

import dataclasses
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution.codex_action import CodexActionResult
from jarvis.execution.tools import (
    ActionLifecycle,
    CallerNotAllowedError,
    DuplicateToolError,
    IllegalLifecycleTransition,
    RawResult,
    ToolDefinition,
    ToolRegistry,
    UnknownToolError,
    build_default_registry,
    spawn_worker_handler,
    verify_diff_handler,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _build_action_request(  # noqa: PLR0913 — all kwargs map 1-to-1 to ActionRequest fields.
    *,
    tool_name: str,
    action_id: str = "A1",
    caller: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
    arguments: dict[str, Any] | None = None,
    run_id: str | None = None,
    target_entity_ref: str | None = "task_X",
    risk_level: str = "L2",
) -> ActionRequest:
    """Build an ActionRequest with sensible defaults for Step 6 tests."""
    return ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=target_entity_ref,
        caller_principal=caller,
        risk_level=risk_level,  # type: ignore[arg-type]
        arguments=arguments or {},
        authorization_lease=None,
        run_id=run_id,
        turn_id="T1",
    )


def _open_runtime(tmp_path: Path) -> tuple[RuntimePaths, sqlite3.Connection]:
    """Bootstrap runtime under tmp_path and open the event log."""
    paths = bootstrap_runtime(root=tmp_path)
    conn = open_event_log(paths.event_log)
    return paths, conn


def _seed_lifecycle(lifecycle: ActionLifecycle, action_id: str) -> None:
    """Register + transition to authorized — what L3 would do pre-dispatch."""
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")


def _stub_codex_result(  # noqa: PLR0913 — six kwargs map 1-to-1 to CodexActionResult fixture knobs; collapsing them defeats the per-test override pattern.
    *,
    submit_report: dict[str, Any] | None = None,
    error: str | None = None,
    interrupted: bool = False,
    diff_text: str = "diff --git a/x b/x\n+hello\n",
    tokens_in: int = 10,
    tokens_out: int = 12,
) -> CodexActionResult:
    """Return a CodexActionResult with sensible Day-2 defaults for happy path."""
    report = submit_report if submit_report is not None else {
        "status": "ok",
        "summary": "codex completed the task",
        "changed_files": ["x"],
    }
    return CodexActionResult(
        final_text="codex did the thing",
        diff_text=diff_text,
        diff_path=None,
        turn_id="TID-1",
        error=error,
        interrupted=interrupted,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        elapsed_ms=12_000,
        submit_report=report if (not interrupted and error is None) else None,
        submit_report_calls=((report,) if (not interrupted and error is None) else ()),
    )


# --- Default registry shape ------------------------------------------------


def test_default_registry_registers_day1_and_day2_step4_tools() -> None:
    """`build_default_registry()` registers the Day-1 pair + Day-2 Step-4 create_task."""
    registry = build_default_registry()
    defs = registry.get_definitions()
    names = sorted(d.name for d in defs)
    assert names == ["create_task", "spawn_worker", "verify_diff"]


def test_default_registry_spawn_worker_shape() -> None:
    """spawn_worker is L2, async, ack semantics, JARVIS_LLM-only."""
    registry = build_default_registry()
    (spawn,) = (d for d in registry.get_definitions() if d.name == "spawn_worker")
    assert spawn.risk_level == "L2"
    assert spawn.result_semantics == "ack"
    assert spawn.is_async is True
    assert spawn.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert spawn.input_schema["required"] == ["task_id"]
    assert "task_id" in spawn.input_schema["properties"]


def test_default_registry_verify_diff_shape() -> None:
    """verify_diff is L0, sync, observation semantics (Day-2), LLM+OBSERVER scope.

    Step 11 flips ``result_semantics`` from Day-1's ``"verification"`` to
    Day-2's ``"observation"`` per ADR-0002 § Verify_diff contract — the
    primary slot is the diff capture; the chained verification slot is
    declared via ``post_action_check`` (canary
    ``test_canary_verify_diff_post_action_check`` enforces).
    """
    registry = build_default_registry()
    (verify,) = (d for d in registry.get_definitions() if d.name == "verify_diff")
    assert verify.risk_level == "L0"
    assert verify.result_semantics == "observation"
    assert verify.is_async is False
    assert verify.allowed_callers == frozenset(
        {CallerPrincipal.JARVIS_LLM, CallerPrincipal.OBSERVER}
    )
    assert verify.input_schema["required"] == ["run_id"]
    assert verify.post_action_check is not None
    assert verify.post_action_check.mode == "inline"
    assert verify.post_action_check.check_tool == "verify_command"
    assert verify.post_action_check.expected_predicate == "exit_code == 0"
    assert verify.post_action_check.result_semantics_on_match == "verification"
    assert verify.post_action_check.timeout_ms == 600_000


def test_for_caller_filters_by_principal() -> None:
    """OBSERVER only sees verify_diff; JARVIS_LLM sees all three."""
    registry = build_default_registry()
    llm_tools = {d.name for d in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    observer_tools = {d.name for d in registry.for_caller(CallerPrincipal.OBSERVER)}
    worker_tools = {d.name for d in registry.for_caller(CallerPrincipal.WORKER_AGENT)}
    assert llm_tools == {"spawn_worker", "verify_diff", "create_task"}
    assert observer_tools == {"verify_diff"}
    assert worker_tools == set()


# --- ToolRegistry.register / dispatch error paths --------------------------


def test_register_duplicate_raises() -> None:
    """Registering the same name twice raises `DuplicateToolError`."""
    registry = build_default_registry()
    (spawn,) = (d for d in registry.get_definitions() if d.name == "spawn_worker")
    with pytest.raises(DuplicateToolError):
        registry.register(spawn)


def test_dispatch_unknown_tool_raises(tmp_path: Path) -> None:
    """Dispatching an unregistered tool_name raises `UnknownToolError`."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(tool_name="does_not_exist")
        _seed_lifecycle(lifecycle, req.action_id)
        with pytest.raises(UnknownToolError):
            registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()


def test_dispatch_caller_not_allowed_raises(tmp_path: Path) -> None:
    """WORKER_AGENT calling spawn_worker raises `CallerNotAllowedError`."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            caller=CallerPrincipal.WORKER_AGENT,
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        with pytest.raises(CallerNotAllowedError):
            registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()


def test_dispatch_lifecycle_must_be_authorized(tmp_path: Path) -> None:
    """Dispatch from `proposed` (skipping authorized) raises."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            arguments={"run_id": "R00000000"},
        )
        # Only register; don't transition to authorized.
        lifecycle.register(req.action_id)
        with pytest.raises(IllegalLifecycleTransition):
            registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()


# --- spawn_worker dispatcher events (Day-2 Codex path mocked) --------------


def test_spawn_worker_emits_dispatched_running_run_started(tmp_path: Path) -> None:
    """Dispatcher + handler emit dispatched + running + task.executor_assigned + run.started.

    Day-2 spawn_worker (Step 10) is synchronous; the test mocks
    :func:`run_codex_action` and :func:`ensure_codex_version_supported`
    so the dispatcher envelope can be asserted without a live Codex.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_codex_result(),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            registry.dispatch(req, conn, paths, lifecycle)
        conn.commit()

        with closing(open_event_log(paths.event_log)) as ro_conn:
            seen = [
                e
                for e in iter_events(ro_conn)
                if e.type in {
                    "action.dispatched",
                    "action.running",
                    "task.executor_assigned",
                    "run.started",
                }
            ]
        types = [e.type for e in seen]
        assert types == [
            "action.dispatched",
            "action.running",
            "task.executor_assigned",
            "run.started",
        ]
        # source chain: running <- dispatched; everything after running <- running.
        assert seen[1].source_event_id == seen[0].event_uid
        assert seen[2].source_event_id == seen[1].event_uid
        assert seen[3].source_event_id == seen[1].event_uid
        # Day-2 runner / executor labels.
        assert seen[3].payload["runner"] == "codex"
        assert seen[3].payload["task_id"] == "task_X"
        assert seen[2].payload["executor"] == "codex"
        assert seen[2].payload["model"] == "gpt-5.5"
    finally:
        conn.close()


def test_spawn_worker_returns_ack_with_cost_metadata(tmp_path: Path) -> None:
    """The Day-2 happy path returns RawResult(semantics="ack") with cost metadata."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_codex_result(),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            bundle = registry.dispatch(req, conn, paths, lifecycle)
        # Day-2 § RawResultBundle: dispatcher wraps single-slot
        # spawn_worker into a one-slot bundle.
        assert len(bundle.slots) == 1
        result = bundle.slots[0]
        assert isinstance(result, RawResult)
        assert result.semantics == "ack"
        assert result.error is None
        assert result.metadata is not None
        assert result.metadata["cost"]["kind"] == "codex"
        assert result.metadata["cost"]["model"] == "gpt-5.5"
        # Stash ref forwarded (None when the tree was clean).
        assert result.metadata["stash_ref"] is None
    finally:
        conn.close()


# --- verify_diff registry envelope (Day-2 dispatcher integration) ----------
#
# The Day-2 verify_diff handler returns a RawResultBundle (one or two
# slots). The exhaustive observation/verification/error/timeout matrix is
# covered by `tests/unit/test_verify_diff_observation.py`. The two tests
# below assert the dispatcher envelope: the registry-level dispatch path
# wraps + propagates the bundle correctly through L4.


def _write_diff_artifact_for_test(
    paths: RuntimePaths, run_id: str, diff_text: str = "diff --git a/x b/x\n+hi\n"
) -> None:
    """Pre-write a Day-2 diff.txt under the per-run artifact dir."""
    run_dir = paths.artifact_dir_for_run(run_id)
    (run_dir / "diff.txt").write_text(diff_text, encoding="utf-8")


def test_verify_diff_dispatch_returns_observation_bundle(tmp_path: Path) -> None:
    """Day-2 verify_diff (no verify_command in payload) → one-slot bundle."""
    paths, conn = _open_runtime(tmp_path)
    try:
        run_id = "R_obs"
        _write_diff_artifact_for_test(paths, run_id)

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            action_id="A_obs",
            arguments={"run_id": run_id},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, req.action_id)
        bundle = registry.dispatch(req, conn, paths, lifecycle)

        assert len(bundle.slots) == 1
        observation = bundle.slots[0]
        assert observation.semantics == "observation"
        assert observation.payload["diff_nonempty"] is True
        assert observation.payload["artifact_ref"].endswith("diff.txt")
        assert lifecycle.is_terminal(req.action_id) is True

        with closing(open_event_log(paths.event_log)) as ro_conn:
            results = [
                e
                for e in iter_events(ro_conn)
                if e.type == "action.result_observed"
                and e.payload.get("action_id") == req.action_id
            ]
        assert len(results) == 1
        assert results[0].payload["semantics"] == "observation"
    finally:
        conn.close()


def test_verify_diff_dispatch_missing_artifact_returns_empty_observation(
    tmp_path: Path,
) -> None:
    """Missing diff artifact → one-slot observation with ``diff_nonempty=False``.

    Day-2 drops Day-1's ``artifact_missing`` error: a no-op diff is a
    real outcome (Codex completed but did not edit), and L3's reviewer
    / verify_command path will treat ``diff_nonempty=False`` accordingly.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        run_id = "R_missing"
        # Don't write any artifact; the handler should still produce a
        # bundle with diff_nonempty=False rather than raising.
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            action_id="A_missing",
            arguments={"run_id": run_id},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, req.action_id)
        bundle = registry.dispatch(req, conn, paths, lifecycle)

        assert len(bundle.slots) == 1
        observation = bundle.slots[0]
        assert observation.semantics == "observation"
        assert observation.payload["diff_nonempty"] is False
        assert lifecycle.is_terminal(req.action_id) is True
    finally:
        conn.close()


# --- Handler-direct sanity (without registry dispatch envelope) -------------


def test_handlers_callable_directly_through_registry_only(tmp_path: Path) -> None:
    """Handlers require dispatcher's stashed running_event_uid → standalone use raises.

    Confirms the L4-internal contract: a handler is NEVER meant to be
    called outside `ToolRegistry.dispatch`. Calling it directly without
    going through dispatch leaves the running_event_uid unset and
    surfaces as `IllegalLifecycleTransition`.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        # Direct handler call — no dispatch wrapper to stash running_event_uid.
        with pytest.raises(IllegalLifecycleTransition):
            spawn_worker_handler(req, conn, paths, lifecycle)

        # Same for verify_diff handler.
        verify_req = _build_action_request(
            tool_name="verify_diff",
            action_id="A2",
            arguments={"run_id": "R-missing"},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, verify_req.action_id)
        with pytest.raises(IllegalLifecycleTransition):
            verify_diff_handler(verify_req, conn, paths, lifecycle)
    finally:
        conn.close()


# --- Public dataclass immutability -----------------------------------------


def test_tool_definition_is_frozen() -> None:
    """ToolDefinition is a frozen dataclass — attribute mutation raises."""
    registry = build_default_registry()
    (spawn,) = (d for d in registry.get_definitions() if d.name == "spawn_worker")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spawn.name = "renamed"  # type: ignore[misc]


def test_raw_result_is_frozen() -> None:
    """RawResult is a frozen dataclass."""
    rr = RawResult(action_id="A1", semantics="ack", payload={}, tool_output=None, error=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rr.action_id = "A2"  # type: ignore[misc]


def test_tool_registry_register_accepts_tool_definition_object(tmp_path: Path) -> None:
    """A bare `ToolRegistry()` with a manual register sees the new tool."""
    _ = tmp_path  # tmp_path not needed but kept for fixture consistency.

    def _noop_handler(*_args: object, **_kwargs: object) -> RawResult:
        return RawResult(
            action_id="A?",
            semantics="ack",
            payload={},
            tool_output=None,
            error=None,
        )

    registry = ToolRegistry()
    td = ToolDefinition(
        name="noop",
        description="d",
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L0",
        result_semantics="ack",
        is_async=False,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_noop_handler,
    )
    registry.register(td)
    assert {d.name for d in registry.get_definitions()} == {"noop"}
