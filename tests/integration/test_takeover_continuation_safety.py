"""SQLite/thread barriers for response cancellation and worker continuations."""

# ruff: noqa: ANN401, SLF001 - instrument real provider, terminal, and watcher boundaries

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import DecideContext, _append_cost_recorded, _emit_cost_recorded_for_run
from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.llm_session import LLMRequestClient
from jarvis.decision.response_run import ResponseCancelledError
from jarvis.execution import tools as execution_tools
from jarvis.runtime import (
    _start_drive_turn_response,
    drive_turn,
    inherent_loop,
    make_response_cancel_callable,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_action
from jarvis.state.trigger_consumption import trigger_was_consumed
from tests.integration.test_wave4a_response_run import (
    _drive_turn_on_own_connection,
    _emit_intent,
    _event_count,
    _make_runtime,
    _payloads,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_cancel_wins_against_late_provider_tool_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real runtime/L3 drops late tools while retaining the provider's cost."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    intent = _emit_intent(runtime.conn, "cancel-late-tools", "现在时间")
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def chat(_self: LLMRequestClient, **_kwargs: Any) -> ChatResult:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return ChatResult(
            text=None,
            tool_calls=(
                ToolCall(call_id="clock-call", name="get_current_time", arguments_json="{}"),
            ),
            finish_reason="tool_calls",
            input_tokens=10,
            output_tokens=5,
            raw={},
            tokens_in=10,
            tokens_out=5,
            model_used="gpt-fast",
        )

    monkeypatch.setattr(LLMRequestClient, "chat", chat)
    cancel = make_response_cancel_callable(runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
        )
        try:
            assert entered.wait(5)
            response_id = _payloads(runtime.conn, "response.started")[0]["response_id"]
            outcome = cancel(response_id, "generation", "user_stop")
            assert outcome == "cancelled"
        finally:
            release.set()
        with pytest.raises(ResponseCancelledError):
            future.result(timeout=5)
    assert calls == [1]
    assert _event_count(runtime.conn, "cost.recorded") == 1
    assert _event_count(runtime.conn, "action.dispatched") == 0
    assert _event_count(runtime.conn, "surface.response_emitted") == 0
    runtime.conn.close()


def test_cancel_deadline_includes_admission_lock(tmp_path: Path) -> None:
    """A blocked admission fence consumes the same total budget as SQLite setup."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True, cancel_timeout_ms=50)
    intent = _emit_intent(runtime.conn, "lock-budget", "fixture")
    opened = _start_drive_turn_response(runtime, user_intent_event=intent, turn_id="lock-budget")
    assert opened is not None
    run, _terminalizer = opened
    cancel = make_response_cancel_callable(runtime)
    with run.admission_lock, ThreadPoolExecutor(max_workers=1) as pool:
        start = time.monotonic()
        outcome = pool.submit(cancel, run.response_id, "generation", "user_stop").result(1)
        assert outcome == "timeout"
        assert time.monotonic() - start < .25
        assert not run.cancellation_token.is_cancelled
        assert _event_count(runtime.conn, "response.cancelled") == 0
    assert cancel(run.response_id, "generation", "user_stop") == "cancelled"
    runtime.conn.close()


def test_worker_cost_reentry_observes_facts_before_failure_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause real L4 immediately before terminal commit; L3 already sees cost."""
    runtime = _make_runtime(tmp_path)
    source = _emit_intent(runtime.conn, "worker-cost-turn", "fixture")
    entered = threading.Event()
    release = threading.Event()
    original = terminalize_action

    def terminal_barrier(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_tools, "terminalize_action", terminal_barrier)

    def worker() -> None:
        conn = open_event_log(runtime.runtime_paths.event_log)
        lifecycle = execution_tools.ActionLifecycle()
        for state in ("authorized", "dispatched", "running"):
            if state == "authorized":
                lifecycle.register("worker-cost-action")
            lifecycle.transition("worker-cost-action", state)
        try:
            execution_tools._spawn_worker_emit_terminal_failure(
                conn=conn,
                action_id="worker-cost-action",
                event_type="action.failed",
                error_code="worker_crash",
                error_message="fixture process failed",
                source_event_id=source.event_uid,
                lifecycle=lifecycle,
                run_id="worker-cost-run",
                stash_ref=None,
                task_id="worker-cost-task",
                turn_id="worker-cost-turn",
                cost={"kind": "codex", "model": "gpt-fast", "tokens_in": 70, "tokens_out": 9},
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert entered.wait(5)
            assert _event_count(runtime.conn, "action.failed") == 0
            assert _event_count(runtime.conn, "task.executor_reported") == 1
            cost = _emit_cost_recorded_for_run(
                cast("DecideContext", SimpleNamespace(conn=runtime.conn)),
                run_id="worker-cost-run",
                turn_id="worker-cost-turn",
            )
            assert cost is not None
        finally:
            release.set()
        future.result(timeout=5)
    assert _event_count(runtime.conn, "cost.recorded") == 1
    runtime.conn.close()


def test_two_reentries_record_one_worker_cost(tmp_path: Path) -> None:
    """Two independent SQLite writers cannot charge a worker run twice."""
    runtime = _make_runtime(tmp_path)
    barrier = threading.Barrier(2)

    def record() -> None:
        conn = open_event_log(runtime.runtime_paths.event_log)
        try:
            barrier.wait(timeout=5)
            _append_cost_recorded(
                cast("DecideContext", SimpleNamespace(conn=conn)),
                {"run_id": "same-run", "model": "gpt-fast", "tokens_in": 50, "tokens_out": 2},
                turn_id="same-turn",
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert _event_count(runtime.conn, "cost.recorded") == 1
    runtime.conn.close()


def test_held_terminal_consumed_by_original_turn_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the real asynchronous watcher and its driver on isolated state."""
    asyncio.run(_held_terminal_scenario(tmp_path, monkeypatch))


async def _held_terminal_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual watcher distinguishes a consumed held row from a true orphan."""
    runtime = _make_runtime(tmp_path)
    execution_tools.register_live_action(turn_id="held-turn", action_id="held-action")
    execution_tools.register_running_action("held-action")
    terminal = emit_event(
        runtime.conn,
        type="action.failed",
        payload={"action_id": "held-action", "error": "worker_crash", "reason": "fixture failure"},
        correlation={"turn_id": "held-turn"},
    )
    seen_live = asyncio.Event()
    real_live = execution_tools.live_action_ids

    def live() -> frozenset[str]:
        seen_live.set()
        return real_live()

    monkeypatch.setattr(inherent_loop, "live_action_ids", live)
    driven: list[str] = []
    original_drive = inherent_loop._drive_turn_in_worker_thread

    def watch_drive(*args: Any, **kwargs: Any) -> Any:
        driven.append(kwargs["user_intent_event"].event_uid)
        return original_drive(*args, **kwargs)

    monkeypatch.setattr(inherent_loop, "_drive_turn_in_worker_thread", watch_drive)
    watcher = asyncio.create_task(
        inherent_loop._system_trigger_watcher(
            runtime,
            anchor_id=0,
            anchored=asyncio.Event(),
            poll_interval_s=0.005,
        )
    )
    try:
        await asyncio.wait_for(seen_live.wait(), 5)
        trigger = replace(terminal, payload={**terminal.payload, "turn_id": "held-turn"})
        drive_turn(runtime, user_intent_event=trigger, available_surfaces=frozenset())
        assert trigger_was_consumed(runtime.conn, terminal.event_uid)
        execution_tools.release_running_action("held-action")
        await asyncio.sleep(0.03)
        assert driven == []
        orphan = emit_event(
            runtime.conn,
            type="action.failed",
            payload={
                "action_id": "orphan-action",
                "error": "worker_crash",
                "reason": "fixture failure",
            },
        )
        for _ in range(100):
            if trigger_was_consumed(runtime.conn, orphan.event_uid):
                break
            await asyncio.sleep(0.01)
        assert driven == [orphan.event_uid]
        assert trigger_was_consumed(runtime.conn, orphan.event_uid)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        execution_tools.release_running_action("held-action")
        execution_tools.release_turn_actions("held-turn")
        runtime.conn.close()
