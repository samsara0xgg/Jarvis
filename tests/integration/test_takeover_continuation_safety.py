"""SQLite/thread barriers for response cancellation and worker continuations."""

# ruff: noqa: ANN401 - instrument real provider, terminal, and watcher boundaries

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import DecideContext, _append_cost_recorded
from jarvis.decision.response_run import ResponseCancelledError
from jarvis.execution import tools as execution_tools
from jarvis.runtime.daemon import intent_pump, sweep
from jarvis.runtime.turn import drive as drive_module
from jarvis.runtime.turn.cancel import make_response_cancel_callable
from jarvis.runtime.turn.drive import drive_turn
from jarvis.runtime.turn.start import start_drive_turn_response
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.trigger_consumption import mark_trigger_consumed, trigger_was_consumed
from tests.support.hold import HANG_S
from tests.support.model import Pause, ScriptedModel, tool_call
from tests.support.response_run import (
    drive_turn_on_own_connection,
    emit_intent,
    event_count,
    make_runtime,
    payloads_of,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_cancel_wins_against_late_provider_tool_batch(tmp_path: Path) -> None:
    """Real runtime/L3 drops late tools while retaining the provider's cost."""
    entered = threading.Event()
    release = threading.Event()
    model = ScriptedModel(Pause(entered, release, tool_call("kept", call_id="memo-call")))
    runtime = make_runtime(tmp_path, model=model)
    intent = emit_intent(runtime.conn, "cancel-late-tools", "我记过什么")
    cancel = make_response_cancel_callable(runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
        )
        try:
            assert entered.wait(5)
            response_id = payloads_of(runtime.conn, "response.started")[0]["response_id"]
            outcome = cancel(response_id, "generation", "user_stop")
            assert outcome == "cancelled"
        finally:
            release.set()
        with pytest.raises(ResponseCancelledError):
            future.result(timeout=5)
    assert len(model.requests) == 1
    assert event_count(runtime.conn, "cost.recorded") == 1
    assert event_count(runtime.conn, "action.dispatched") == 0
    assert event_count(runtime.conn, "surface.response_emitted") == 0
    runtime.conn.close()


def test_cancel_deadline_includes_admission_lock(tmp_path: Path) -> None:
    """A blocked admission fence consumes the same total budget as SQLite setup."""
    runtime = make_runtime(tmp_path, cancel_timeout_ms=50)
    intent = emit_intent(runtime.conn, "lock-budget", "fixture")
    opened = start_drive_turn_response(runtime, user_intent_event=intent, turn_id="lock-budget")
    assert opened is not None
    run, _terminalizer, _route = opened
    cancel = make_response_cancel_callable(runtime)
    # The pool ends after the fence is let go, so a cancel stuck behind it cannot hold the test.
    with ThreadPoolExecutor(max_workers=1) as pool, run.admission_lock:
        # The fence stays held until the cancel has answered: it gives up on the fence instead.
        outcome = pool.submit(cancel, run.response_id, "generation", "user_stop").result(HANG_S)
        assert outcome == "timeout"
        assert not run.cancellation_token.is_cancelled
        assert event_count(runtime.conn, "response.cancelled") == 0
    assert cancel(run.response_id, "generation", "user_stop") == "cancelled"
    runtime.conn.close()


def test_two_reentries_record_one_worker_cost(tmp_path: Path) -> None:
    """Two independent SQLite writers cannot charge a worker run twice."""
    runtime = make_runtime(tmp_path)
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
    assert event_count(runtime.conn, "cost.recorded") == 1
    runtime.conn.close()


@pytest.mark.parametrize("marker_fails", [False, True])
def test_terminal_consumed_by_original_turn_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker_fails: bool,
) -> None:
    """Run the real asynchronous watcher and its driver on isolated state."""
    asyncio.run(_held_terminal_scenario(tmp_path, monkeypatch, marker_fails=marker_fails))


async def _held_terminal_scenario(  # noqa: PLR0915 - live-turn and orphan watcher phases
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker_fails: bool = False,
) -> None:
    """The actual watcher distinguishes a live turn's consumed row from a true orphan."""
    runtime = make_runtime(tmp_path)
    mark = mark_trigger_consumed
    marker_calls: list[str] = []

    def marker(conn: sqlite3.Connection, uid: str, turn_id: str) -> None:
        marker_calls.append(uid)
        if marker_fails and len(marker_calls) == 1:
            message = "injected consumption marker failure after semantic completion"
            raise sqlite3.OperationalError(message)
        mark(conn, uid, turn_id)

    monkeypatch.setattr(drive_module, "mark_trigger_consumed", marker)
    execution_tools.register_live_action(turn_id="held-turn", action_id="held-action")
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

    monkeypatch.setattr(sweep, "live_action_ids", live)
    driven: list[str] = []
    original_drive = intent_pump.drive_turn_in_worker_thread

    def watch_drive(*args: Any, **kwargs: Any) -> Any:
        driven.append(kwargs["user_intent_event"].event_uid)
        return original_drive(*args, **kwargs)

    monkeypatch.setattr(sweep, "drive_turn_in_worker_thread", watch_drive)
    watcher = asyncio.create_task(
        sweep.system_trigger_watcher(
            runtime,
            anchor_id=0,
            anchored=asyncio.Event(),
            poll_interval_s=0.005,
        )
    )
    try:
        await asyncio.wait_for(seen_live.wait(), 5)
        trigger = replace(terminal, payload={**terminal.payload, "turn_id": "held-turn"})
        if marker_fails:
            with pytest.raises(sqlite3.OperationalError, match="marker failure"):
                drive_turn(runtime, user_intent_event=trigger, available_surfaces=frozenset())
        else:
            drive_turn(runtime, user_intent_event=trigger, available_surfaces=frozenset())
        assert event_count(runtime.conn, "turn.ended") == 1
        assert trigger_was_consumed(runtime.conn, terminal.event_uid)
        execution_tools.release_turn_actions("held-turn")
        await asyncio.sleep(0.03)
        assert driven == []
        assert event_count(runtime.conn, "turn.ended") == 1
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
        execution_tools.release_turn_actions("held-turn")
        runtime.conn.close()
