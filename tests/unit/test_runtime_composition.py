"""Unit tests for the composition root (``jarvis.runtime``).

Covers:
- ``bootstrap_runtime_app`` wires L1..L6 against the real
  ``config/jarvis.yaml`` + ``prompts/jarvis_v1.md`` and produces a
  frozen :class:`JarvisRuntime` with every attribute populated.
- ``_wait_for_next_trigger`` returns the first matching row when a
  background ``threading.Thread`` emits a ``worker.reported`` event
  between two polls (the multi-trigger-loop primitive).
- ``_wait_for_next_trigger`` raises :class:`TriggerWaitTimeout` when
  no trigger ever arrives.

No LLM is invoked. ``bootstrap_runtime_app`` instantiates an
:class:`LLMClient` lazily — the SDK is only constructed on the first
``chat()`` call, so the test never touches the network.
"""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path

import pytest

from jarvis.deployment import RuntimePaths
from jarvis.execution.diff_capture import isolate_pretask_changes
from jarvis.execution.tools import ActionLifecycle, ToolRegistry
from jarvis.runtime import (
    JarvisRuntime,
    RuntimeBootstrapError,
    TriggerWaitTimeout,
    _pop_pending_stashes,
    _wait_for_next_trigger,
    bootstrap_runtime_app,
)
from jarvis.state.event_log import emit_event, iter_events, open_event_log

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"


# --- bootstrap_runtime_app --------------------------------------------------


def test_bootstrap_runtime_app_returns_populated_runtime(tmp_path: Path) -> None:
    """Bootstrap against the real config + prompt; check every attribute."""
    runtime = bootstrap_runtime_app(
        config_path=_CONFIG_PATH,
        prompt_path=_PROMPT_PATH,
        runtime_root=tmp_path,
    )
    try:
        assert isinstance(runtime, JarvisRuntime)
        assert isinstance(runtime.runtime_paths, RuntimePaths)
        assert runtime.runtime_paths.root == tmp_path.resolve()
        assert runtime.runtime_paths.event_log == (tmp_path / "mac_events.db").resolve()
        assert runtime.runtime_paths.artifacts_root.is_dir()
        assert isinstance(runtime.conn, sqlite3.Connection)
        assert isinstance(runtime.tool_registry, ToolRegistry)
        assert isinstance(runtime.lifecycle, ActionLifecycle)
        assert runtime.system_prompt  # non-empty prompt text loaded
        assert "llm" in runtime.config
        # Default registry has the Day-1 pair, Day-2 Step 4's create_task,
        # F6's list_tasks read-only observation tool, plus the Tier-0
        # get_current_time clock read.
        tool_names = {t.name for t in runtime.tool_registry.get_definitions()}
        assert tool_names == {
            "spawn_worker",
            "verify_diff",
            "create_task",
            "list_tasks",
            "get_current_time",
        }
    finally:
        runtime.conn.close()


def test_bootstrap_runtime_app_raises_on_missing_config(tmp_path: Path) -> None:
    """A bogus config_path surfaces a RuntimeBootstrapError."""
    with pytest.raises(RuntimeBootstrapError):
        bootstrap_runtime_app(
            config_path=tmp_path / "does_not_exist.yaml",
            prompt_path=_PROMPT_PATH,
            runtime_root=tmp_path,
        )


def test_bootstrap_runtime_app_raises_on_missing_prompt(tmp_path: Path) -> None:
    """A bogus prompt_path surfaces a RuntimeBootstrapError."""
    with pytest.raises(RuntimeBootstrapError):
        bootstrap_runtime_app(
            config_path=_CONFIG_PATH,
            prompt_path=tmp_path / "missing.md",
            runtime_root=tmp_path,
        )


# --- _wait_for_next_trigger -------------------------------------------------


def _seed_utterance(conn: sqlite3.Connection) -> int:
    """Append a surface.user_intent row + return its SQLite id.

    Used as the ``after_id`` anchor for the trigger poll — we only
    want to see rows that landed AFTER this seed.
    """
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "seed", "turn_id": "T_seed"},
        correlation={"turn_id": "T_seed"},
    )
    cursor = conn.execute("SELECT MAX(id) FROM events")
    row = cursor.fetchone()
    return int(row[0])


def test_wait_for_next_trigger_returns_worker_reported(tmp_path: Path) -> None:
    """A background thread emits ``worker.reported``; the poll picks it up."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        # Pre-seed an utterance so the trigger we are looking for is the
        # NEXT row after this one.
        last_id = _seed_utterance(conn)

        # Pre-emit a parent action.running row so the worker.reported
        # row can reference a real source_event_id. We don't actually
        # need a real chain here; the poll only checks (id, type).
        parent_event = emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": "A_test",
                "tool_name": "spawn_worker",
                "caller_principal": "jarvis_llm",
                "risk_level": "L2",
            },
        )

        def _bg_emit() -> None:
            # Opens its OWN connection to honor check_same_thread.
            bg_conn = open_event_log(db_path)
            try:
                emit_event(
                    bg_conn,
                    type="worker.reported",
                    payload={
                        "run_id": "R_test",
                        "action_id": "A_test",
                        "status": "reported_complete",
                        "summary": "ok",
                    },
                    source_event_id=parent_event.event_uid,
                    correlation={"action_id": "A_test", "run_id": "R_test"},
                )
            finally:
                bg_conn.close()

        thread = threading.Thread(target=_bg_emit)
        # Anchor the poll BEFORE we start the background thread — we
        # are testing the polling primitive against a true cross-thread
        # producer.
        thread.start()

        # Poll for a trigger after `last_id` (i.e. skipping the seed
        # utterance AND the action.proposed row). Generous timeout so a
        # slow CI doesn't flake.
        event, new_id = _wait_for_next_trigger(
            conn,
            after_id=last_id,
            timeout=2.0,
            poll_interval_s=0.01,
        )
        thread.join(timeout=2.0)

        assert event.type == "worker.reported"
        assert event.payload["action_id"] == "A_test"
        assert event.payload["run_id"] == "R_test"
        assert new_id > last_id


def test_wait_for_next_trigger_times_out_when_nothing_arrives(tmp_path: Path) -> None:
    """No matching trigger -> :class:`TriggerWaitTimeout`."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        last_id = _seed_utterance(conn)
        with pytest.raises(TriggerWaitTimeout):
            _wait_for_next_trigger(
                conn,
                after_id=last_id,
                timeout=0.05,
                poll_interval_s=0.01,
            )


# --- _pop_pending_stashes (J13 dirty-tree conflict surfacing) ---------------


def _git_q(repo: Path, *args: str) -> None:
    """Run a quiet git subcommand in ``repo`` (test fixture helper)."""
    subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), *args],  # noqa: S607 — `git` resolved via PATH is intentional.
        check=True,
        capture_output=True,
    )


def test_pop_pending_stashes_surfaces_conflict(tmp_path: Path) -> None:
    """J13: a stash-pop conflict during the post-verify pop is surfaced, not silent.

    Reproduces the deterministic conflict from
    ``test_diff_capture.py::test_dirty_tree_conflict_pop_writes_artifact``
    (Allen's stashed edit collides with Codex's committed edit), then drives
    the runtime's sole pop site. The conflict must become an observable
    ``worker.artifact_observed(kind=stash_conflict)`` + ``Limitation`` claim,
    not merely a ``conflict.patch`` file nothing references.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_q(repo, "init", "-q")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git_q(repo, "add", "a.txt")
    _git_q(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    # Allen's pre-task edit → stashed by isolate_pretask_changes.
    (repo / "a.txt").write_text("allen edit\n", encoding="utf-8")
    stash_ref = isolate_pretask_changes(repo, run_id="R1")
    assert stash_ref is not None

    # Codex's conflicting edit, committed (deterministic stash-apply conflict).
    (repo / "a.txt").write_text("codex edit\n", encoding="utf-8")
    _git_q(repo, "add", "a.txt")
    _git_q(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "codex")

    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        task_event = emit_event(
            conn,
            type="task.created",
            payload={
                "task_id": "task_X",
                "goal": "edit a.txt",
                "source": "manual",
                "repo_path": str(repo),
            },
        )
        emit_event(
            conn,
            type="worker.reported",
            payload={
                "run_id": "R1",
                "action_id": "A1",
                "status": "reported_complete",
                "summary": "ok",
                "stash_ref": stash_ref,
            },
            source_event_id=task_event.event_uid,
            correlation={
                "action_id": "A1",
                "run_id": "R1",
                "task_id": "task_X",
                "turn_id": "T1",
            },
        )

        _pop_pending_stashes(conn, artifacts_root=artifacts_root, turn_id="T1")

        rows = list(iter_events(conn))
        conflict_artifacts = [
            e
            for e in rows
            if e.type == "worker.artifact_observed"
            and e.payload.get("kind") == "stash_conflict"
        ]
        assert conflict_artifacts, "stash conflict produced no worker.artifact_observed"
        assert conflict_artifacts[0].payload["action_id"] == "A1"

        limitations = [
            e
            for e in rows
            if e.type == "claim.created" and e.payload.get("type") == "Limitation"
        ]
        assert limitations, "stash conflict produced no Limitation claim"

        assert (artifacts_root / "run_R1" / "conflict.patch").is_file()
