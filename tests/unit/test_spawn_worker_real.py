"""Day-2 unit tests for ``spawn_worker_handler`` -- real Codex flow (ADR-0002 Step 10).

These tests run with a mocked :func:`run_codex_action` plus mocked
:func:`ensure_codex_version_supported` and :func:`isolate_pretask_changes`
so Tier 1 stays LLM-free + subprocess-free. The live Codex path is
exercised by Tier 2 (Step 20, ``--live-codex``).

Coverage:
    - Happy path: full event chain (task.executor_assigned, run.started,
      worker.artifact_observed, worker.reported, task.executor_reported)
      with cost + stash_ref metadata.
    - ``submit_report`` missing: ``worker.report_missing`` fires + degraded
      ``worker.reported`` with ``status="report_missing"``.
    - Codex subprocess crash: ``action.failed`` fires; no worker.reported.
    - Codex turn timeout: ``action.timeout_assumed`` fires; no worker.reported.
    - codex --version too low: ``action.failed`` + RawResult(semantics="error").
    - Dirty-tree stash_ref forwarding through RawResult.metadata.

Hard rules enforced by these tests:
    - ``restore_pretask_changes`` is NOT called from the handler body
      (verified by import-search of the source file).
    - ``RawResult.metadata["cost"]`` carries kind=codex / model /
      tokens_in / tokens_out / run_id.
"""

from __future__ import annotations

import ast
import inspect
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution.codex_action import (
    CodexActionResult,
    CodexVersionTooLowError,
)
from jarvis.execution.tools import (
    ActionLifecycle,
    build_default_registry,
    spawn_worker_handler,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _open_runtime(tmp_path: Path) -> tuple[RuntimePaths, sqlite3.Connection]:
    """Bootstrap a tmp runtime + open the event log."""
    paths = bootstrap_runtime(root=tmp_path)
    conn = open_event_log(paths.event_log)
    return paths, conn


def _seed_task(conn: sqlite3.Connection, *, task_id: str, repo_path: str | None) -> None:
    """Seed one ``task.created`` event so the handler can load goal + repo_path."""
    payload: dict[str, Any] = {
        "task_id": task_id,
        "goal": "do the thing",
        "source": "manual",
    }
    if repo_path is not None:
        payload["repo_path"] = repo_path
    emit_event(conn, type="task.created", payload=payload)


def _build_request(*, task_id: str = "task_X") -> ActionRequest:
    return ActionRequest(
        action_id="A1",
        tool_name="spawn_worker",
        target_entity_ref=task_id,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L2",
        arguments={"task_id": task_id},
        authorization_lease=None,
        run_id=None,
        turn_id="T1",
    )


def _stub_result(  # noqa: PLR0913 — six kwargs map 1-to-1 to CodexActionResult fixture knobs; collapsing them defeats the per-test override pattern.
    *,
    submit_report: dict[str, Any] | None = None,
    error: str | None = None,
    interrupted: bool = False,
    diff_text: str = "diff --git a/x b/x\n+hello\n",
    tokens_in: int = 10,
    tokens_out: int = 12,
) -> CodexActionResult:
    """Build a CodexActionResult fixture with sensible defaults."""
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
        submit_report=submit_report,
        submit_report_calls=((submit_report,) if submit_report else ()),
    )


def _dispatch(
    paths: RuntimePaths,
    conn: sqlite3.Connection,
    *,
    task_id: str = "task_X",
) -> Any:  # noqa: ANN401 — returns RawResult; precise import not load-bearing.
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    req = _build_request(task_id=task_id)
    lifecycle.register(req.action_id)
    lifecycle.transition(req.action_id, "authorized")
    result = registry.dispatch(req, conn, paths, lifecycle)
    return result, lifecycle, req


def _types(conn_path: Path) -> list[str]:
    """Return all event types in order from a fresh read-only connection."""
    with closing(open_event_log(conn_path)) as ro_conn:
        return [e.type for e in iter_events(ro_conn)]


# --- Happy path ------------------------------------------------------------


def test_spawn_worker_happy_path_event_chain(tmp_path: Path) -> None:
    """Full happy chain: assigned + run.started + artifact + reported + executor_reported."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(
                    submit_report={
                        "status": "ok",
                        "summary": "did it",
                        "changed_files": ["x"],
                    },
                ),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            result, _lifecycle, _req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    types = _types(paths.event_log)
    # Core Day-2 happy chain (ordering rooted in action.running).
    assert "task.executor_assigned" in types
    assert "run.started" in types
    assert "worker.artifact_observed" in types
    assert "worker.reported" in types
    assert "task.executor_reported" in types
    assert "worker.report_missing" not in types
    # Failure events absent.
    assert "action.failed" not in types
    assert "action.timeout_assumed" not in types

    # RawResult shape.
    assert result.semantics == "ack"
    assert result.error is None
    assert result.payload["status"] == "ok"
    assert result.payload["summary"] == "did it"
    assert result.metadata is not None
    cost = result.metadata["cost"]
    assert cost["kind"] == "codex"
    assert cost["model"] == "gpt-5.5"
    assert cost["tokens_in"] == 10
    assert cost["tokens_out"] == 12
    assert cost["run_id"].startswith("R")
    # Stash ref forwarded (None on clean tree).
    assert "stash_ref" in result.metadata
    assert result.metadata["stash_ref"] is None


def test_spawn_worker_happy_path_worker_reported_uses_submit_report_payload(
    tmp_path: Path,
) -> None:
    """worker.reported's status + summary come from the submit_report payload."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        report = {
            "status": "ok",
            "summary": "applied the patch",
            "changed_files": ["x.py"],
        }
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(submit_report=report),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            _result, _lifecycle, _req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    with closing(open_event_log(paths.event_log)) as ro_conn:
        wr = [e for e in iter_events(ro_conn) if e.type == "worker.reported"]
    assert len(wr) == 1
    payload = wr[0].payload
    assert payload["status"] == "ok"
    assert payload["summary"] == "applied the patch"
    assert payload["action_id"] == "A1"
    assert "artifact_path" in payload


# --- submit_report missing -------------------------------------------------


def test_spawn_worker_submit_report_missing_emits_report_missing(tmp_path: Path) -> None:
    """No submit_report -> worker.report_missing + degraded worker.reported."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(submit_report=None),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            result, _lifecycle, _req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    types = _types(paths.event_log)
    assert "worker.report_missing" in types
    # worker.reported still emitted, with degraded status.
    assert "worker.reported" in types
    with closing(open_event_log(paths.event_log)) as ro_conn:
        wr = [e for e in iter_events(ro_conn) if e.type == "worker.reported"]
    assert wr[0].payload["status"] == "report_missing"

    # RawResult is still ack — degraded report is not a hard failure here;
    # the Limitation Claim is L3's responsibility (Step 12).
    assert result.semantics == "ack"


# --- Codex crash -----------------------------------------------------------


def test_spawn_worker_codex_crash_emits_action_failed(tmp_path: Path) -> None:
    """A Codex crash tag maps to action.failed + RawResult(semantics=error)."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(error="codex_subprocess_crashed"),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            result, lifecycle, req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    types = _types(paths.event_log)
    assert "action.failed" in types
    assert "worker.reported" not in types  # crash path skips worker.reported
    # task.executor_reported still emitted with status=failed so the
    # projection has a terminal row for this run.
    assert "task.executor_reported" in types
    assert "task.verified" not in types

    assert result.semantics == "error"
    assert result.error == "codex_subprocess_crashed"
    assert result.metadata is not None
    assert result.metadata["cost"]["kind"] == "codex"

    # Lifecycle moved running -> failed.
    assert lifecycle.state_of(req.action_id) == "failed"
    assert lifecycle.is_terminal(req.action_id) is True


# --- Codex timeout ---------------------------------------------------------


def test_spawn_worker_codex_timeout_emits_action_timeout_assumed(tmp_path: Path) -> None:
    """An interrupted Codex turn maps to action.timeout_assumed."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(
                    interrupted=True, error="codex_turn_timeout",
                ),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            result, lifecycle, req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    types = _types(paths.event_log)
    assert "action.timeout_assumed" in types
    assert "action.failed" not in types
    assert "worker.reported" not in types

    assert result.semantics == "error"
    assert result.error == "codex_turn_timeout"

    assert lifecycle.state_of(req.action_id) == "timeout_assumed"
    assert lifecycle.is_terminal(req.action_id) is True


# --- Version pre-flight ----------------------------------------------------


def test_spawn_worker_version_too_low_emits_action_failed(tmp_path: Path) -> None:
    """Pre-flight version-too-low -> action.failed + lifecycle terminal."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch(
                "jarvis.execution.tools.ensure_codex_version_supported",
                side_effect=CodexVersionTooLowError("codex 0.124.0 < 0.125.0"),
            ),
            patch("jarvis.execution.tools.run_codex_action") as mock_codex,
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
            ) as mock_stash,
        ):
            result, lifecycle, req = _dispatch(paths, conn)
            # Codex is NEVER spawned when the pre-flight gate rejects.
            mock_codex.assert_not_called()
            mock_stash.assert_not_called()
        conn.commit()
    finally:
        conn.close()

    types = _types(paths.event_log)
    assert "action.failed" in types
    # Pre-flight fails BEFORE the run is born; no run.started / executor_assigned.
    assert "run.started" not in types
    assert "task.executor_assigned" not in types

    assert result.semantics == "error"
    assert result.error == "codex_version_too_low"
    assert lifecycle.state_of(req.action_id) == "failed"


# --- Dirty-tree stash forwarding -------------------------------------------


def test_spawn_worker_dirty_tree_forwards_stash_ref_via_metadata(tmp_path: Path) -> None:
    """When isolate_pretask_changes returns a ref, it lands on metadata + payload.

    The handler MUST NOT pop the stash itself — the runtime composition
    (Step 17) owns the pop ordering after verify_diff exits. This test
    only asserts the forwarding path; the no-pop static invariant is the
    Step 11/17 canary's concern.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(
                    submit_report={"status": "ok", "summary": "ok"},
                ),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value="stash@{0}",
            ),
        ):
            result, _lifecycle, _req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    assert result.metadata is not None
    assert result.metadata["stash_ref"] == "stash@{0}"
    # Also surfaced via the success payload so legacy clients reading
    # tool_output can see the stash handle.
    assert result.payload["stash_ref"] == "stash@{0}"


# --- Static no-pop invariant -----------------------------------------------


def test_spawn_worker_handler_does_not_call_restore_pretask_changes() -> None:
    """Hard rule: ``restore_pretask_changes`` MUST NOT be invoked from this handler.

    AST-scan: walk ``spawn_worker_handler``'s body, fail if any Call
    node names ``restore_pretask_changes``. Step 11/17 lands the full
    canonical canary (``test_canary_stash_pop_after_verify``); this
    test is the Step-10 textual guard until that canary exists.
    """
    src = inspect.getsource(spawn_worker_handler)
    tree = ast.parse(src)
    bad_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "restore_pretask_changes":
                bad_calls.append(ast.unparse(node))
    assert not bad_calls, (
        "spawn_worker_handler must not call restore_pretask_changes; "
        "the runtime composition (Step 17) owns the pop after "
        f"verify_diff. Found: {bad_calls!r}"
    )


# --- Cost metadata shape ---------------------------------------------------


@pytest.mark.parametrize(
    ("tokens_in", "tokens_out"),
    [(0, 0), (1, 2), (1234, 5678)],
)
def test_spawn_worker_cost_metadata_carries_codex_token_counts(
    tmp_path: Path, tokens_in: int, tokens_out: int,
) -> None:
    """metadata.cost is shaped {kind, model, tokens_in, tokens_out, run_id}."""
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_task(conn, task_id="task_X", repo_path=str(tmp_path))
        with (
            patch("jarvis.execution.tools.ensure_codex_version_supported"),
            patch(
                "jarvis.execution.tools.run_codex_action",
                return_value=_stub_result(
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    submit_report={"status": "ok", "summary": "ok"},
                ),
            ),
            patch(
                "jarvis.execution.tools.isolate_pretask_changes",
                return_value=None,
            ),
        ):
            result, _lifecycle, _req = _dispatch(paths, conn)
        conn.commit()
    finally:
        conn.close()

    assert result.metadata is not None
    cost = result.metadata["cost"]
    assert set(cost) == {"kind", "model", "tokens_in", "tokens_out", "run_id"}
    assert cost["tokens_in"] == tokens_in
    assert cost["tokens_out"] == tokens_out
