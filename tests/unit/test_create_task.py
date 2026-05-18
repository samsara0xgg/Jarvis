"""Unit tests for `create_task` L4 tool (ADR-0002 Step 4).

Covers:
    - Tool registration: `build_default_registry()` exposes `create_task`
      with the right caller scope / risk / semantics / async flag.
    - Dispatch happy path: handler emits ONE `task.created` with the
      expected payload (task_id + goal + source default + optional
      keys propagated when supplied).
    - Lifecycle: `action.result_observed(semantics="ack")` is emitted
      and lifecycle transitions to terminal.
    - verify_command auto-detection: when `repo_path` is a Python repo,
      `task.created.payload["verify_command"] == "uv run pytest -x"`.
    - verify_command absent: when no `repo_path` is supplied, the key
      simply doesn't appear (per the optional-payload contract).
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import (
    ActionLifecycle,
    RawResult,
    build_default_registry,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.deployment import RuntimePaths


# --- Helpers ----------------------------------------------------------------


def _build_action_request(
    *,
    arguments: dict[str, Any],
    action_id: str = "A_ct",
    turn_id: str | None = "T1",
) -> ActionRequest:
    """Build an ActionRequest for create_task."""
    return ActionRequest(
        action_id=action_id,
        tool_name="create_task",
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L1",
        arguments=arguments,
        authorization_lease=None,
        run_id=None,
        turn_id=turn_id,
    )


def _open_runtime(tmp_path: Path) -> tuple[RuntimePaths, sqlite3.Connection]:
    """Bootstrap runtime + open event log."""
    paths = bootstrap_runtime(root=tmp_path)
    conn = open_event_log(paths.event_log)
    return paths, conn


def _seed_lifecycle(lifecycle: ActionLifecycle, action_id: str) -> None:
    """Register + authorize an action (what L3 does pre-dispatch)."""
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")


def _make_python_repo(repo: Path) -> None:
    """Build a Python repo that triggers `"uv run pytest -x"` detection."""
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")


# --- Registration -----------------------------------------------------------


def test_create_task_registered_in_default_registry() -> None:
    """`create_task` is present and L1 / ack / sync / JARVIS_LLM-only."""
    registry = build_default_registry()
    names = {d.name for d in registry.get_definitions()}
    assert "create_task" in names

    (ct,) = (d for d in registry.get_definitions() if d.name == "create_task")
    assert ct.risk_level == "L1"
    assert ct.result_semantics == "ack"
    assert ct.is_async is False
    assert ct.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert ct.input_schema["required"] == ["goal"]
    # Optional fields present in schema (no required entry).
    properties = ct.input_schema["properties"]
    for field in ("goal", "repo_path", "deadline", "source"):
        assert field in properties, f"missing property: {field}"


def test_create_task_visible_only_to_jarvis_llm() -> None:
    """OBSERVER and WORKER_AGENT never see create_task in their tool list."""
    registry = build_default_registry()
    llm_tools = {d.name for d in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    observer_tools = {d.name for d in registry.for_caller(CallerPrincipal.OBSERVER)}
    worker_tools = {d.name for d in registry.for_caller(CallerPrincipal.WORKER_AGENT)}
    assert "create_task" in llm_tools
    assert "create_task" not in observer_tools
    assert "create_task" not in worker_tools


# --- Happy path: dispatch emits task.created --------------------------------


def test_dispatch_emits_task_created_with_goal_and_default_source(
    tmp_path: Path,
) -> None:
    """A minimal `{"goal": ...}` call emits task.created with source=manual."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(arguments={"goal": "ship the thing"})
        _seed_lifecycle(lifecycle, req.action_id)

        result = registry.dispatch(req, conn, paths, lifecycle)

        assert isinstance(result, RawResult)
        assert result.semantics == "ack"
        assert result.error is None
        task_id = result.payload["task_id"]
        assert isinstance(task_id, str)
        assert task_id.startswith("T_")
        # verify_command absent in the ack payload when repo_path was omitted.
        assert "verify_command" not in result.payload

        # Exactly one task.created row, with the expected payload.
        with closing(open_event_log(paths.event_log)) as ro_conn:
            tc = [e for e in iter_events(ro_conn) if e.type == "task.created"]
        assert len(tc) == 1
        tc_payload = tc[0].payload
        assert tc_payload["task_id"] == task_id
        assert tc_payload["goal"] == "ship the thing"
        assert tc_payload["source"] == "manual"
        # Optional fields not provided → not on the payload.
        assert "deadline" not in tc_payload
        assert "repo_path" not in tc_payload
        assert "verify_command" not in tc_payload
    finally:
        conn.close()


def test_dispatch_propagates_optional_fields(tmp_path: Path) -> None:
    """`source` override + `deadline` propagate verbatim to task.created."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            arguments={
                "goal": "deploy v2",
                "deadline": "2026-05-19",
                "source": "automated",
            },
        )
        _seed_lifecycle(lifecycle, req.action_id)

        result = registry.dispatch(req, conn, paths, lifecycle)
        assert result.semantics == "ack"

        with closing(open_event_log(paths.event_log)) as ro_conn:
            tc = [e for e in iter_events(ro_conn) if e.type == "task.created"]
        assert len(tc) == 1
        tc_payload = tc[0].payload
        assert tc_payload["goal"] == "deploy v2"
        assert tc_payload["deadline"] == "2026-05-19"
        assert tc_payload["source"] == "automated"
        # No repo_path → no verify_command.
        assert "verify_command" not in tc_payload
    finally:
        conn.close()


def test_dispatch_auto_detects_verify_command_when_repo_path_supplied(
    tmp_path: Path,
) -> None:
    """A Python repo at repo_path lands `verify_command="uv run pytest -x"`."""
    paths, conn = _open_runtime(tmp_path)
    try:
        py_repo = tmp_path / "my_python_repo"
        _make_python_repo(py_repo)

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            arguments={"goal": "fix the bug", "repo_path": str(py_repo)},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        result = registry.dispatch(req, conn, paths, lifecycle)
        assert result.payload["verify_command"] == "uv run pytest -x"

        with closing(open_event_log(paths.event_log)) as ro_conn:
            tc = [e for e in iter_events(ro_conn) if e.type == "task.created"]
        assert len(tc) == 1
        tc_payload = tc[0].payload
        assert tc_payload["repo_path"] == str(py_repo)
        assert tc_payload["verify_command"] == "uv run pytest -x"
    finally:
        conn.close()


def test_dispatch_unknown_repo_omits_verify_command(tmp_path: Path) -> None:
    """A repo with no recognized shape carries repo_path but no verify_command."""
    paths, conn = _open_runtime(tmp_path)
    try:
        unknown_repo = tmp_path / "unknown"
        unknown_repo.mkdir()
        (unknown_repo / "README.md").write_text("# nothing\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(
            arguments={"goal": "look around", "repo_path": str(unknown_repo)},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        result = registry.dispatch(req, conn, paths, lifecycle)
        assert "verify_command" not in result.payload

        with closing(open_event_log(paths.event_log)) as ro_conn:
            tc = [e for e in iter_events(ro_conn) if e.type == "task.created"]
        assert len(tc) == 1
        tc_payload = tc[0].payload
        assert tc_payload["repo_path"] == str(unknown_repo)
        assert "verify_command" not in tc_payload
    finally:
        conn.close()


# --- Lifecycle + action.result_observed -------------------------------------


def test_dispatch_emits_action_result_observed_ack_and_terminal(
    tmp_path: Path,
) -> None:
    """Handler emits action.result_observed(semantics=ack) + terminal-transitions."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(arguments={"goal": "test ack path"})
        _seed_lifecycle(lifecycle, req.action_id)

        registry.dispatch(req, conn, paths, lifecycle)

        assert lifecycle.state_of(req.action_id) == "result_observed"
        assert lifecycle.is_terminal(req.action_id) is True

        with closing(open_event_log(paths.event_log)) as ro_conn:
            ros = [
                e
                for e in iter_events(ro_conn)
                if e.type == "action.result_observed"
                and e.payload.get("action_id") == req.action_id
            ]
        assert len(ros) == 1
        evt = ros[0]
        assert evt.payload["semantics"] == "ack"
        # tool_output is JSON-encoded {task_id: ...}.
        tool_output = json.loads(evt.payload["tool_output"])
        assert "task_id" in tool_output
    finally:
        conn.close()


def test_dispatch_event_chain_dispatched_running_task_created_result_observed(
    tmp_path: Path,
) -> None:
    """Event order: dispatched → running → task.created → action.result_observed."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(arguments={"goal": "trace order"})
        _seed_lifecycle(lifecycle, req.action_id)

        registry.dispatch(req, conn, paths, lifecycle)

        with closing(open_event_log(paths.event_log)) as ro_conn:
            seen = [
                e
                for e in iter_events(ro_conn)
                if e.type
                in {
                    "action.dispatched",
                    "action.running",
                    "task.created",
                    "action.result_observed",
                }
            ]
        types = [e.type for e in seen]
        assert types == [
            "action.dispatched",
            "action.running",
            "task.created",
            "action.result_observed",
        ]
        # source-chain: running ← dispatched; task.created ← running;
        # action.result_observed ← running.
        assert seen[1].source_event_id == seen[0].event_uid
        assert seen[2].source_event_id == seen[1].event_uid
        assert seen[3].source_event_id == seen[1].event_uid
    finally:
        conn.close()


# --- Argument validation ----------------------------------------------------


def test_missing_goal_raises_keyerror(tmp_path: Path) -> None:
    """Required `goal` absent → KeyError surfaces from the handler."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(arguments={})
        _seed_lifecycle(lifecycle, req.action_id)
        with pytest.raises(KeyError):
            registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()


def test_non_string_goal_raises_typeerror(tmp_path: Path) -> None:
    """`goal` must be a string."""
    paths, conn = _open_runtime(tmp_path)
    try:
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_action_request(arguments={"goal": 42})
        _seed_lifecycle(lifecycle, req.action_id)
        with pytest.raises(TypeError):
            registry.dispatch(req, conn, paths, lifecycle)
    finally:
        conn.close()
