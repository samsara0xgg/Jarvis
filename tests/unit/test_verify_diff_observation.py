"""Unit tests for the Day-2 dual-slot ``verify_diff_handler``.

Per ADR-0002 § Verify_diff contract (spec §3.4.11 + §3.5.7). Six
scenarios:

* observation only — no ``verify_command`` in payload → one-slot bundle.
* verify_command exit 0 → slot 2 carries ``semantics="verification"``.
* verify_command exit non-zero → slot 2 carries ``semantics="error"``.
* verify_command timeout → slot 2 carries ``semantics="error"`` and
  ``timed_out=True`` with exit_code=124 (GNU ``timeout(1)`` convention).
* missing artifact file → observation slot with ``diff_nonempty=False``.
* empty diff → observation slot with ``diff_nonempty=False``.

All tests use real ``tmp_path`` artifacts and real ``/bin/sh -c true|
false`` subprocesses where possible; only the timeout test mocks
``subprocess.run`` because the 600s default timeout is too long for a
unit test. The dispatcher envelope is exercised via ``ToolRegistry.
dispatch`` rather than calling the handler directly so the
``running_event_uid`` stash + lifecycle transitions stay realistic.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution.tools import (
    ActionLifecycle,
    build_default_registry,
)
from jarvis.shared import ActionRequest, CallerPrincipal, RawResultBundle
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _open_runtime(tmp_path: Path) -> tuple[RuntimePaths, sqlite3.Connection]:
    """Bootstrap runtime under ``tmp_path`` and open the event log."""
    paths = bootstrap_runtime(root=tmp_path)
    conn = open_event_log(paths.event_log)
    return paths, conn


def _build_request(
    *,
    artifact_path: Path,
    payload: dict[str, Any] | None = None,
    action_id: str = "A_verify",
    target_entity_ref: str | None = "task_X",
) -> ActionRequest:
    """Build a Day-2 verify_diff ActionRequest with ``arguments.artifact_path``."""
    return ActionRequest(
        action_id=action_id,
        tool_name="verify_diff",
        target_entity_ref=target_entity_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments={"artifact_path": str(artifact_path), "run_id": "R_test"},
        authorization_lease=None,
        run_id="R_test",
        turn_id="T1",
        payload=payload,
    )


def _seed_lifecycle(lifecycle: ActionLifecycle, action_id: str) -> None:
    """Bring ``action_id`` to ``authorized`` so dispatch's precondition passes."""
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")


# --- Observation-only branch (no verify_command) --------------------------


def test_verify_diff_observation_only_no_verify_command(tmp_path: Path) -> None:
    """No ``verify_command`` in payload → one-slot bundle (``observation``)."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("diff --git a/x b/x\n+hello world\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(artifact_path=artifact, payload=None)
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        assert isinstance(bundle, RawResultBundle)
        assert len(bundle.slots) == 1
        observation = bundle.slots[0]
        assert observation.semantics == "observation"
        assert observation.payload["diff_nonempty"] is True
        assert observation.payload["artifact_ref"] == str(artifact)
        assert "hello world" in observation.payload["diff_text_preview"]
        assert observation.error is None
    finally:
        conn.close()


# --- verify_command exit 0 → verification slot ----------------------------


def test_verify_diff_verification_exit_zero(tmp_path: Path) -> None:
    """``verify_command="true"`` exits 0 → slot 2 carries ``verification``."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("diff content\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(
            artifact_path=artifact,
            payload={"verify_command": "true", "repo_path": str(tmp_path)},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        assert len(bundle.slots) == 2
        assert bundle.slots[0].semantics == "observation"
        verification = bundle.slots[1]
        assert verification.semantics == "verification"
        assert verification.payload["exit_code"] == 0
        assert verification.payload["verify_command"] == "true"
        assert verification.payload["timed_out"] is False
        assert verification.error is None
    finally:
        conn.close()


# --- verify_command exit non-zero → error slot ----------------------------


def test_verify_diff_error_on_nonzero_exit(tmp_path: Path) -> None:
    """``verify_command="false"`` exits 1 → slot 2 carries ``error`` semantics."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("diff content\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(
            artifact_path=artifact,
            payload={"verify_command": "false", "repo_path": str(tmp_path)},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        assert len(bundle.slots) == 2
        observation = bundle.slots[0]
        verification = bundle.slots[1]
        assert observation.semantics == "observation"
        assert verification.semantics == "error"
        assert verification.payload["exit_code"] != 0
        assert verification.payload["timed_out"] is False
        assert verification.error == "verify_command_exit_1"
    finally:
        conn.close()


def test_verify_diff_error_captures_stderr_tail(tmp_path: Path) -> None:
    """A failing verify_command's stderr tail is recorded on the slot."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("diff\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        # Echo a recognizable token to stderr then exit non-zero.
        req = _build_request(
            artifact_path=artifact,
            payload={
                "verify_command": "echo VERIFY_FAILED_TOKEN >&2; exit 7",
                "repo_path": str(tmp_path),
            },
        )
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        verification = bundle.slots[1]
        assert verification.semantics == "error"
        assert verification.payload["exit_code"] == 7
        assert "VERIFY_FAILED_TOKEN" in verification.payload["stderr_tail"]
        assert verification.error == "verify_command_exit_7"
    finally:
        conn.close()


# --- verify_command timeout → error slot with timed_out=True --------------


def test_verify_diff_timeout_synthesizes_124_exit(tmp_path: Path) -> None:
    """``subprocess.TimeoutExpired`` → slot 2 carries error + ``timed_out=True``."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("c\n", encoding="utf-8")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(
            artifact_path=artifact,
            payload={"verify_command": "sleep 5", "repo_path": str(tmp_path)},
        )
        _seed_lifecycle(lifecycle, req.action_id)

        # Patch subprocess.run inside jarvis.execution.tools to raise
        # TimeoutExpired without actually waiting 600s. The decoded
        # stdout is None because the timeout fires before any output.
        with patch(
            "jarvis.execution.tools.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["/bin/sh", "-c", "sleep 5"],
                timeout=0.1,
                output=b"",
                stderr=b"",
            ),
        ):
            bundle = registry.dispatch(req, conn, paths, lifecycle)

        assert len(bundle.slots) == 2
        verification = bundle.slots[1]
        assert verification.semantics == "error"
        assert verification.payload["exit_code"] == 124  # GNU timeout(1) convention.
        assert verification.payload["timed_out"] is True
        assert verification.error == "verify_command_timeout"
    finally:
        conn.close()


# --- Edge cases: missing artifact / empty diff ----------------------------


def test_verify_diff_missing_artifact_returns_empty_observation(
    tmp_path: Path,
) -> None:
    """A diff path that does not exist → observation slot, ``diff_nonempty=False``.

    Day-2 drops Day-1's ``artifact_missing`` error: a no-op diff is a
    real Day-2 outcome (Codex completed but did not edit), and L3's
    reviewer / verify_command path will treat ``diff_nonempty=False``
    as the Limitation signal.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "nonexistent_diff.txt"  # never written
        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(artifact_path=artifact)
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        assert len(bundle.slots) == 1
        observation = bundle.slots[0]
        assert observation.semantics == "observation"
        assert observation.payload["diff_nonempty"] is False
        assert observation.payload["diff_text_preview"] == ""
        assert observation.error is None
    finally:
        conn.close()


def test_verify_diff_empty_diff_returns_diff_nonempty_false(tmp_path: Path) -> None:
    """An empty/whitespace-only diff → ``diff_nonempty=False`` on the slot."""
    paths, conn = _open_runtime(tmp_path)
    try:
        artifact = tmp_path / "diff.txt"
        artifact.write_text("   \n  \n", encoding="utf-8")  # whitespace-only

        registry = build_default_registry()
        lifecycle = ActionLifecycle()
        req = _build_request(artifact_path=artifact)
        _seed_lifecycle(lifecycle, req.action_id)

        bundle = registry.dispatch(req, conn, paths, lifecycle)
        assert len(bundle.slots) == 1
        observation = bundle.slots[0]
        assert observation.semantics == "observation"
        assert observation.payload["diff_nonempty"] is False
    finally:
        conn.close()
