"""Takeover acceptance against real SQLite connections and execution threads."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from jarvis.execution import codex_action
from jarvis.execution.action_runner import ActionRunner, ActionRunnerError, ToolConcurrency
from jarvis.execution.codex_client import CodexAppServerClient
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_wave4b_action_runner import (
    _ack,
    _fixed_resolver,
    _Fixture,
    _fixture_tool,
    _payloads,
    _request,
    _wait_until,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import ActionRequest, RawResult


@pytest.mark.parametrize("first_finished", [False, True])
def test_same_turn_mutators_refuse_cleanup_self_wait(
    tmp_path: Path, *, first_finished: bool,
) -> None:
    """Reject an unsupported root dependency so production turn cleanup can run."""
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        if request.action_id == "first":
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("write", body),),
        max_concurrent_runs=2,
        resolver=_fixed_resolver({"write": ToolConcurrency(("repo",), "write_exclusive")}),
    )
    try:
        assert fixture.runner is not None
        first = fixture.submit(_request("write", "first", turn_id="same"))
        assert entered.wait(3)
        if first_finished:
            release.set()
            first.handle.result(timeout=3)
        second = fixture.submit(_request("write", "second", turn_id="same"))
        with pytest.raises(ActionRunnerError, match="same-turn root blocked by cleanup debt"):
            second.handle.result(timeout=.5)
        finalized = threading.Event()
        fixture.runner.finalize_turn_cleanup(
            "same", verification_outcome="verified", on_finalize=finalized.set,
        )
        if not first_finished:
            assert not finalized.is_set()
            assert len(fixture.runner.leases.live_scopes()) == 1
        release.set()
        first.handle.result(timeout=3)
        assert finalized.wait(1)
        assert not second_entered.is_set()
        assert fixture.runner.leases.live_scopes() == ()
        assert len(_payloads(fixture.conn, "action.failed")) == 1
        # A subsequent turn can use the repository after real turn cleanup.
        third = fixture.submit(_request("write", "third", turn_id="next"))
        third.handle.result(timeout=3)
        assert second_entered.is_set()
    finally:
        release.set()
        fixture.close()


def test_explicit_verification_waits_for_parent_physical_quiescence(tmp_path: Path) -> None:
    """A reported-but-active parent cannot be read until it physically returns."""
    entered = threading.Event()
    release = threading.Event()
    verified = threading.Event()

    def body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        if request.action_id == "parent":
            entered.set()
            assert release.wait(3)
        else:
            verified.set()
        return _ack(request)

    fixture = _Fixture(tmp_path, tools=(
        _fixture_tool("worker", body), _fixture_tool("verify", body, read_only=True),
    ), max_concurrent_runs=2, resolver=_fixed_resolver({
        "worker": ToolConcurrency(("repo",), "write_exclusive"),
        "verify": ToolConcurrency(("repo",), "read_shared", parent_action_id="parent"),
    }))
    try:
        parent = fixture.submit(_request("worker", "parent", turn_id="turn"))
        assert entered.wait(3)
        child = fixture.submit(_request("verify", "child", turn_id="turn"))
        assert not verified.wait(.05)
        release.set()
        parent.handle.result(timeout=3)
        child.handle.result(timeout=3)
        assert verified.is_set()
    finally:
        release.set()
        fixture.close()


def test_failed_codex_close_retains_physical_worker_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real SIGTERM-resistant process plus failed kill cannot become quiescence."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(30)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    kill = process.kill
    client = CodexAppServerClient.__new__(CodexAppServerClient)
    client._proc = process  # noqa: SLF001 - real OS process with failure injection
    client._closed = False  # noqa: SLF001
    close = client.close

    def fail_kill() -> None:
        message = "injected kill failure"
        raise OSError(message)

    def fail_initialize(**_kwargs: object) -> None:
        message = "injected handshake failure"
        raise OSError(message)

    def short_close(**_kwargs: object) -> None:
        close(timeout=0.02)

    monkeypatch.setattr(process, "kill", fail_kill)
    monkeypatch.setattr(client, "initialize", fail_initialize)
    monkeypatch.setattr(client, "close", short_close)
    monkeypatch.setattr(codex_action, "CodexAppServerClient", lambda **_kwargs: client)

    def body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        codex_action.run_codex_action(
            task_goal="fixture", cwd=tmp_path, env={"CODEX_HOME": str(tmp_path)}, timeout_s=0.1
        )
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("worker", body),),
        resolver=_fixed_resolver(
            {
                "worker": ToolConcurrency(("repo",), "write_exclusive"),
            }
        ),
    )
    finalized = threading.Event()
    try:
        assert fixture.runner is not None
        submission = fixture.submit(_request("worker", "unconfirmed", turn_id="physical"))
        with pytest.raises(RuntimeError, match="shutdown is unconfirmed"):
            submission.handle.result(timeout=3)
        assert process.poll() is None
        assert not client._closed  # noqa: SLF001
        context = fixture.runner.context_of("unconfirmed")
        assert context is not None
        assert not context.is_quiesced
        assert fixture.runner.turn_has_inflight("physical")
        assert _payloads(fixture.conn, "worker.quiesced") == []
        fixture.runner.finalize_turn_cleanup(
            "physical", verification_outcome="verification_skipped", on_finalize=finalized.set
        )
        assert not finalized.is_set()
        assert len(fixture.runner.leases.live_scopes()) == 1
    finally:
        kill()
        process.wait(timeout=3)
        process.stdout.close()
        fixture.close()


@pytest.mark.parametrize("failure_site", ["heartbeat", "notification", "server_request"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_worker_loop_exceptions_settle_physical_ownership(  # noqa: PLR0915 - process lifecycle oracle
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str, *, close_fails: bool,
) -> None:
    """Unexpected loop failures close a real child or retain its physical debt."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; "
         "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "print('ready',flush=True); time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == b"ready\n"
    kill = process.kill
    client = CodexAppServerClient.__new__(CodexAppServerClient)
    client._proc = process  # noqa: SLF001 - actual child with protocol failure injection
    client._closed = False  # noqa: SLF001
    close = client.close
    close_calls: list[int] = []

    def fail(**_kwargs: object) -> None:
        message = "injected SQLite worker-loop failure"
        raise sqlite3.OperationalError(message)

    def close_child(**_kwargs: object) -> None:
        close_calls.append(1)
        close(timeout=.02)

    def heartbeat(_payload: object) -> None:
        fail()

    if close_fails:
        monkeypatch.setattr(process, "kill", fail)
    monkeypatch.setattr(client, "initialize", lambda **_kwargs: None)
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: {"thread": {"id": "test"}})
    monkeypatch.setattr(client, "take_server_request",
                        fail if failure_site == "server_request" else lambda **_kwargs: None)
    monkeypatch.setattr(client, "take_notification",
                        fail if failure_site == "notification" else lambda **_kwargs: None)
    monkeypatch.setattr(client, "close", close_child)
    monkeypatch.setattr(codex_action, "CodexAppServerClient", lambda **_kwargs: client)

    def body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        codex_action.run_codex_action(
            task_goal="fixture", cwd=tmp_path, env={"CODEX_HOME": str(tmp_path)},
            timeout_s=2, on_heartbeat=heartbeat, heartbeat_interval_s=0,
        )
        return _ack(request)

    fixture = _Fixture(
        tmp_path, tools=(_fixture_tool("worker", body),),
        resolver=_fixed_resolver({"worker": ToolConcurrency(("repo",), "write_exclusive")}),
    )
    finalized = threading.Event()
    try:
        assert fixture.runner is not None
        submission = fixture.submit(_request("worker", "loop-error", turn_id="loop-turn"))
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            submission.handle.result(timeout=3)
        assert close_calls == [1]
        context = fixture.runner.context_of("loop-error")
        assert context is not None
        assert context.is_quiesced is not close_fails
        assert (process.poll() is None) is close_fails
        assert len(_payloads(fixture.conn, "worker.quiesced")) == (0 if close_fails else 1)
        fixture.runner.finalize_turn_cleanup(
            "loop-turn", verification_outcome="verification_skipped", on_finalize=finalized.set,
        )
        assert finalized.is_set() is not close_fails
        assert fixture.runner.turn_has_inflight("loop-turn") is close_fails
        assert len(fixture.runner.leases.live_scopes()) == (1 if close_fails else 0)
    finally:
        if process.poll() is None:
            kill()
        process.wait(timeout=3)
        process.stdout.close()
        fixture.close()


@pytest.mark.parametrize("wait_kind", ["lease", "slot"])
def test_shutdown_refuses_queued_handlers(tmp_path: Path, wait_kind: str) -> None:
    """Shutdown wins while the real execution thread waits for lease or slot."""
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        if request.action_id == "first":
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return _ack(request)

    scopes = {
        "first": ToolConcurrency(("repo",), "write_exclusive"),
        "second": ToolConcurrency(
            ("repo" if wait_kind == "lease" else "other",), "write_exclusive"
        ),
    }
    fixture = _Fixture(
        tmp_path,
        tools=tuple(_fixture_tool(name, body) for name in scopes),
        max_concurrent_runs=1,
        resolver=_fixed_resolver(scopes),
    )
    try:
        assert fixture.runner is not None
        runner = fixture.runner
        first = fixture.submit(_request("first", "first"))
        assert entered.wait(3)
        second = fixture.submit(_request("second", "second"))
        if wait_kind == "slot":
            assert _wait_until(lambda: len(runner.leases.live_scopes()) == 2)
        fixture.runner.shutdown(wait=False, cancel=True)
        release.set()
        first.handle.result(timeout=3)
        with pytest.raises(ActionRunnerError, match=r"shut down|cancelled"):
            second.handle.result(timeout=3)
        assert not second_entered.is_set()
        assert [p["action_id"] for p in _payloads(fixture.conn, "action.running")] == ["first"]
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        release.set()
        fixture.close()


@pytest.mark.parametrize("mode", ["write_exclusive", "global_exclusive"])
@pytest.mark.parametrize("ending", ["failed_cleanup", "crash_running"])
def test_restart_preserves_unresolved_scope(tmp_path: Path, mode: str, ending: str) -> None:
    """Fresh runner/connection restores unresolved debt including global mode."""
    path = tmp_path / "events.sqlite"
    conn = open_event_log(path)
    try:
        emit_event(
            conn,
            type="action.running",
            payload={
                "action_id": "old",
                "resource_keys": "*" if mode == "global_exclusive" else "repo",
                "resource_mode": mode,
            },
        )
        if ending == "failed_cleanup":
            emit_event(
                conn, type="action.timeout_assumed", payload={"action_id": "old", "reason": "crash"}
            )
            emit_event(
                conn,
                type="action.cleanup_failed",
                payload={
                    "action_id": "old",
                    "worker_epoch": 0,
                    "reason": "late_worker_write",
                    "quarantine_reason": "late_worker_write",
                    "resource_keys": "repo",
                },
            )
    finally:
        conn.close()
    conn = open_event_log(path)
    runner = ActionRunner(event_log_path=path)
    try:
        assert runner.reconcile_quarantine(conn) == ("old",)
        scopes = runner.leases.live_scopes()
        assert scopes[0].mode == mode
        with pytest.raises(TimeoutError):
            runner.leases.acquire(
                action_id="new", keys=frozenset({"repo"}), mode="write_exclusive", timeout_s=0.02
            )
    finally:
        runner.shutdown()
        conn.close()


def test_restart_retains_overlapping_global_debt(tmp_path: Path) -> None:
    """An earlier repo quarantine cannot hide a later global quarantine."""
    path = tmp_path / "overlap.db"
    conn = open_event_log(path)
    for action_id, mode, keys in (
        ("repo-debt", "write_exclusive", "repo"),
        ("global-debt", "global_exclusive", "*"),
    ):
        emit_event(
            conn,
            type="action.running",
            payload={
                "action_id": action_id,
                "resource_mode": mode,
                "resource_keys": keys,
            },
        )
    runner = ActionRunner(event_log_path=path)
    try:
        assert runner.reconcile_quarantine(conn) == ("repo-debt", "global-debt")
        assert runner.reconcile_quarantine(conn) == ("repo-debt", "global-debt")
        assert len(runner.leases.live_scopes()) == 2
        with pytest.raises(TimeoutError):
            runner.leases.acquire(
                action_id="other",
                keys=frozenset({"unrelated"}),
                mode="write_exclusive",
                timeout_s=0.02,
            )
    finally:
        runner.shutdown()
        conn.close()
