"""Unit tests for `jarvis.execution.tools` (ADR § Acceptance B, Step 6).

Covers:
    - Default registry shape (names + caller scope + risk + semantics + async flag).
    - `spawn_worker` happy path — artifact written, lifecycle stays at running,
      `worker.reported` row appears (off the main thread).
    - `spawn_worker` negative — `_SPAWN_WORKER_ARTIFACT_STATUS = "fail"` flows
      through to the artifact file.
    - `verify_diff` three paths (match / fail / missing).
    - Caller scoping (CallerNotAllowedError), UnknownToolError,
      DuplicateToolError, dispatch precondition (lifecycle == authorized).
"""

from __future__ import annotations

import dataclasses
import json
import threading
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution import tools as tools_module
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


def _wait_for_event_type(
    db_path: Path, event_type: str, *, timeout: float = 2.0, poll: float = 0.01
) -> int:
    """Poll the event log until at least one row of `event_type` exists.

    Returns the count. Raises AssertionError if `timeout` elapses with
    none found. Uses `threading.Event.wait` for non-blocking sleep (the
    project bans `time.sleep`).
    """
    sentinel = threading.Event()
    deadline_iters = int(timeout / poll) + 1
    for _ in range(deadline_iters):
        with closing(open_event_log(db_path)) as conn:
            count = sum(1 for e in iter_events(conn) if e.type == event_type)
        if count > 0:
            return count
        sentinel.wait(timeout=poll)
    msg = f"event type {event_type!r} never appeared within {timeout}s"
    raise AssertionError(msg)


# --- Default registry shape ------------------------------------------------


def test_default_registry_registers_spawn_worker_and_verify_diff() -> None:
    """`build_default_registry()` registers exactly the two Day-1 tools."""
    registry = build_default_registry()
    defs = registry.get_definitions()
    names = sorted(d.name for d in defs)
    assert names == ["spawn_worker", "verify_diff"]


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
    """verify_diff is L0, sync, verification semantics, LLM+OBSERVER scope."""
    registry = build_default_registry()
    (verify,) = (d for d in registry.get_definitions() if d.name == "verify_diff")
    assert verify.risk_level == "L0"
    assert verify.result_semantics == "verification"
    assert verify.is_async is False
    assert verify.allowed_callers == frozenset(
        {CallerPrincipal.JARVIS_LLM, CallerPrincipal.OBSERVER}
    )
    assert verify.input_schema["required"] == ["run_id"]


def test_for_caller_filters_by_principal() -> None:
    """OBSERVER only sees verify_diff; JARVIS_LLM sees both."""
    registry = build_default_registry()
    llm_tools = {d.name for d in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    observer_tools = {d.name for d in registry.for_caller(CallerPrincipal.OBSERVER)}
    worker_tools = {d.name for d in registry.for_caller(CallerPrincipal.WORKER_AGENT)}
    assert llm_tools == {"spawn_worker", "verify_diff"}
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


# --- spawn_worker happy path -----------------------------------------------


def test_spawn_worker_writes_artifact_and_returns_ack(tmp_path: Path) -> None:
    """spawn_worker writes diff.json with the configured status and returns ack."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        result = registry.dispatch(req, conn, paths, lifecycle)

        assert isinstance(result, RawResult)
        assert result.semantics == "ack"
        assert result.error is None
        run_id = result.payload["run_id"]
        assert isinstance(run_id, str)
        assert run_id.startswith("R")

        # Artifact file written with the configured status.
        diff_path = paths.artifact_dir_for_run(run_id) / "diff.json"
        assert diff_path.exists()
        data = json.loads(diff_path.read_text(encoding="utf-8"))
        assert data["status"] == "ok"  # default _SPAWN_WORKER_ARTIFACT_STATUS
        assert data["run_id"] == run_id
        assert data["task_id"] == "task_X"
    finally:
        conn.close()


def test_spawn_worker_leaves_lifecycle_at_running(tmp_path: Path) -> None:
    """Immediately after dispatch, lifecycle is `running` (async pattern)."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        registry.dispatch(req, conn, paths, lifecycle)
        # State is `running` — NOT terminal yet (worker.reported delivery
        # is what L3 Result Interpreter, Step 9, transitions to terminal).
        assert lifecycle.state_of(req.action_id) == "running"
        assert lifecycle.is_terminal(req.action_id) is False
    finally:
        conn.close()


def test_spawn_worker_emits_dispatched_running_run_started(tmp_path: Path) -> None:
    """The dispatcher + handler emit dispatched + running + run.started in order."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        registry.dispatch(req, conn, paths, lifecycle)
        conn.commit()

        with closing(open_event_log(paths.event_log)) as ro_conn:
            seen = [e for e in iter_events(ro_conn) if e.type in {
                "action.dispatched", "action.running", "run.started"
            }]
        types = [e.type for e in seen]
        assert types == ["action.dispatched", "action.running", "run.started"]
        # source chain: running ← dispatched; run.started ← running.
        assert seen[1].source_event_id == seen[0].event_uid
        assert seen[2].source_event_id == seen[1].event_uid
        # run.started payload carries runner=codex_stub and the new run_id.
        assert seen[2].payload["runner"] == "codex_stub"
        assert seen[2].payload["task_id"] == "task_X"
    finally:
        conn.close()


def test_spawn_worker_emits_worker_reported_on_separate_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker.reported is delivered by a different thread than the main flow.

    Implements ADR acceptance B4 readiness. Uses the module-level
    `_TEST_MODE_THREAD_CAPTURE` hook to record the callback's thread,
    then asserts it differs from the dispatcher's thread.
    """
    captured: list[threading.Thread] = []
    monkeypatch.setattr(tools_module, "_TEST_MODE_THREAD_CAPTURE", captured)

    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        main_thread = threading.current_thread()
        baseline_capture_len = len(captured)
        registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()

    # Wait until at least this dispatch's worker.reported event lands.
    _wait_for_event_type(paths.event_log, "worker.reported", timeout=2.0)
    # Acceptance B4: at least one captured thread is NOT the main thread.
    # Stragglers from prior tests' Timers may also append, so we don't
    # assert `len(captured) == 1`; the load-bearing invariant is that no
    # entry equals `main_thread`.
    assert len(captured) >= baseline_capture_len + 1
    assert all(t is not main_thread for t in captured)


def test_spawn_worker_worker_reported_payload_shape(tmp_path: Path) -> None:
    """worker.reported carries run_id / action_id / status / summary / artifact_path."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        result = registry.dispatch(req, conn, paths, lifecycle)
        run_id = result.payload["run_id"]
    finally:
        conn.close()

    _wait_for_event_type(paths.event_log, "worker.reported", timeout=2.0)
    with closing(open_event_log(paths.event_log)) as ro_conn:
        wr = [e for e in iter_events(ro_conn) if e.type == "worker.reported"]
    assert len(wr) == 1
    payload = wr[0].payload
    assert payload["run_id"] == run_id
    assert payload["action_id"] == req.action_id
    assert payload["status"] == "reported_complete"
    assert "diff.json" in payload["artifact_path"]
    # source_event_id chain rooted in action.running event (per dispatcher).
    assert wr[0].source_event_id is not None


# --- spawn_worker negative path --------------------------------------------


def test_spawn_worker_writes_fail_when_constant_flipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatching `_SPAWN_WORKER_ARTIFACT_STATUS = "fail"` flows through."""
    monkeypatch.setattr(tools_module, "_SPAWN_WORKER_ARTIFACT_STATUS", "fail")
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="spawn_worker",
            arguments={"task_id": "task_X"},
        )
        _seed_lifecycle(lifecycle, req.action_id)
        result = registry.dispatch(req, conn, paths, lifecycle)
        run_id = result.payload["run_id"]
        diff_path = paths.artifact_dir_for_run(run_id) / "diff.json"
        data = json.loads(diff_path.read_text(encoding="utf-8"))
    finally:
        conn.close()
    assert data["status"] == "fail"
    _wait_for_event_type(paths.event_log, "worker.reported", timeout=2.0)


# --- verify_diff happy path ------------------------------------------------


def _write_artifact(
    paths: RuntimePaths, run_id: str, status: str = "ok", *, task_id: str = "task_X"
) -> None:
    """Pre-write a diff.json artifact for verify_diff to read."""
    run_dir = paths.artifact_dir_for_run(run_id)
    payload = {"status": status, "run_id": run_id, "task_id": task_id}
    (run_dir / "diff.json").write_text(json.dumps(payload, sort_keys=True))


def test_verify_diff_match_emits_verification(tmp_path: Path) -> None:
    """verify_diff with status==ok emits verification semantics + content hash."""
    paths, conn = _open_runtime(tmp_path)
    try:
        run_id = "R00000001"
        _write_artifact(paths, run_id, status="ok")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            action_id="A2",
            arguments={"run_id": run_id},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, req.action_id)
        result = registry.dispatch(req, conn, paths, lifecycle)

        assert result.semantics == "verification"
        assert result.error is None
        assert result.payload["predicate"] == "status==ok"
        assert isinstance(result.payload["content_hash"], str)
        assert len(result.payload["content_hash"]) == 64  # SHA-256 hex

        # Lifecycle terminal.
        assert lifecycle.state_of(req.action_id) == "result_observed"
        assert lifecycle.is_terminal(req.action_id) is True

        # Event emitted with semantics=verification.
        with closing(open_event_log(paths.event_log)) as ro_conn:
            results = [
                e
                for e in iter_events(ro_conn)
                if e.type == "action.result_observed"
                and e.payload.get("action_id") == req.action_id
            ]
        assert len(results) == 1
        assert results[0].payload["semantics"] == "verification"
    finally:
        conn.close()


def test_verify_diff_predicate_fail_emits_error(tmp_path: Path) -> None:
    """verify_diff with status==fail emits error semantics + predicate_failed."""
    paths, conn = _open_runtime(tmp_path)
    try:
        run_id = "R00000002"
        _write_artifact(paths, run_id, status="fail")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            action_id="A2",
            arguments={"run_id": run_id},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, req.action_id)
        result = registry.dispatch(req, conn, paths, lifecycle)

        assert result.semantics == "error"
        assert result.error == "predicate_failed"
        assert result.payload["actual_status"] == "fail"

        assert lifecycle.state_of(req.action_id) == "result_observed"
        assert lifecycle.is_terminal(req.action_id) is True

        with closing(open_event_log(paths.event_log)) as ro_conn:
            results = [
                e
                for e in iter_events(ro_conn)
                if e.type == "action.result_observed"
                and e.payload.get("action_id") == req.action_id
            ]
        assert len(results) == 1
        assert results[0].payload["semantics"] == "error"
        assert results[0].payload["error"] == "predicate_failed"
    finally:
        conn.close()


def test_verify_diff_missing_artifact_emits_error(tmp_path: Path) -> None:
    """verify_diff without an artifact emits error with code=artifact_missing."""
    paths, conn = _open_runtime(tmp_path)
    try:
        run_id = "R00000003"
        # Note: do NOT write any artifact. The artifact_dir_for_run call
        # in the handler creates the directory; that's fine, no diff.json
        # exists.

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            tool_name="verify_diff",
            action_id="A2",
            arguments={"run_id": run_id},
            risk_level="L0",
        )
        _seed_lifecycle(lifecycle, req.action_id)
        result = registry.dispatch(req, conn, paths, lifecycle)

        assert result.semantics == "error"
        assert result.error == "artifact_missing"
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
