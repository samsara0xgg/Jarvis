"""Real pump/driver/runner barriers: quiet actions cannot occupy input slots."""

# ruff: noqa: SLF001, ANN401 - scripted L3 results around actual runtime ownership
from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis import runtime as runtime_module
from jarvis.execution.action_runner import ToolConcurrency
from jarvis.execution.tools import running_action_ids, turn_action_ids
from jarvis.runtime import inherent_loop
from jarvis.shared.realtime import Wave5InputFlags
from jarvis.state.event_log import emit_event
from tests.integration.test_wave4a_response_run import _make_runtime, _payloads
from tests.integration.test_wave5_background_actions import (
    _ack,
    _async_tool,
    _authorize,
    _fixed_resolver,
    _Fixture,
    _request,
    _turn_plan,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.gates import ResponsePlan
    from jarvis.execution.tools import RawResult
    from jarvis.shared import ActionRequest


@pytest.mark.parametrize(
    "outcome", [
        "finish", "cancel", "shutdown", "timeout", "poll_error", "resume_connect_error",
        "shutdown_in_step",
    ],
)
def test_waiting_actions_release_both_input_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    """A third real input renders before either action finishes, then both resume once."""
    asyncio.run(_three_inputs(tmp_path, monkeypatch, outcome))


async def _three_inputs(  # noqa: C901, PLR0912, PLR0915 — complete barrier scenario
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    release = threading.Event()
    step_release = threading.Event()
    entered: set[str] = set()
    decisions: list[tuple[str, str]] = []

    def body(request: ActionRequest, conn: sqlite3.Connection) -> RawResult:
        entered.add(str(request.turn_id))
        assert release.wait(10)
        emit_event(
            conn, type="action.failed",
            payload={"action_id": request.action_id, "error": "fixture", "reason": "finished"},
            correlation={"turn_id": str(request.turn_id)},
        )
        return _ack(request)

    fixture = _Fixture(
        tmp_path, tools=(_async_tool("slow", body),),
        resolver=_fixed_resolver({"slow": ToolConcurrency(resource_keys=(), mode="read_shared")}),
    )
    base = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    runtime = replace(
        base, tool_registry=fixture.registry, action_runner=fixture.runner,
        lifecycle=fixture.lifecycle, input_flags=Wave5InputFlags(intent_pump=True),
    )

    def decide(trigger: Any, ctx: Any) -> Any:
        turn_id = str((trigger.correlation or {})["turn_id"])
        decisions.append((turn_id, trigger.type))
        plan: ResponsePlan | None = _turn_plan()
        if trigger.type == "surface.user_intent" and turn_id != "third":
            action_id = "action-" + turn_id
            _authorize(ctx.lifecycle, action_id)
            ctx.tool_registry.dispatch(
                _request("slow", action_id, turn_id=turn_id),
                ctx.conn, ctx.runtime_paths, ctx.lifecycle,
            )
            if outcome == "shutdown_in_step":
                assert step_release.wait(10)
            plan = None
        return SimpleNamespace(
            response_plan=plan, events_emitted=(), attention_channel="voice_notify",
            stream_failure=None, route=None, last_gate_event_uid=None,
        )

    monkeypatch.setattr(runtime_module, "decide", decide)
    if outcome == "poll_error":
        poll = inherent_loop._poll_waiting_turn
        failed = False

        def transient(*args: Any) -> Any:
            nonlocal failed
            if not failed:
                failed = True
                message = "injected transient read failure"
                raise sqlite3.OperationalError(message)
            return poll(*args)

        monkeypatch.setattr(inherent_loop, "_poll_waiting_turn", transient)
    if outcome == "resume_connect_error":
        drive = inherent_loop._drive_turn_in_worker_thread
        failed_connect = False

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            nonlocal failed_connect
            if kwargs.get("continuation") is not None and not failed_connect:
                failed_connect = True
                message = "injected connection unavailable before ownership transfer"
                raise inherent_loop.TurnConnectionUnavailableError(message)
            return drive(*args, **kwargs)

        monkeypatch.setattr(inherent_loop, "_drive_turn_in_worker_thread", unavailable)
    if outcome == "timeout":
        monkeypatch.setattr(runtime_module, "_trigger_wait_budget", lambda *_a, **_kw: 0.2)
    tasks = await inherent_loop._start_intent_pump(
        runtime, poll_interval_s=0.001, queue_capacity=8, max_concurrent_turns=2,
    )

    async def until(predicate: Any) -> None:
        for _ in range(600):
            if predicate():
                return
            await asyncio.sleep(0.005)
        assert predicate()

    def submit(turn_id: str) -> None:
        emit_event(
            runtime.conn, type="surface.user_intent",
            payload={"transcript": "synthetic", "turn_id": turn_id,
                     "channel": "cli_stdin", "language": "en"},
            correlation={"turn_id": turn_id},
        )

    try:
        submit("first")
        submit("second")
        await until(lambda: len(entered) == 2)
        if outcome == "shutdown_in_step":
            for task in tasks:
                task.cancel()
            await asyncio.sleep(0.01)
            assert any(not task.done() for task in tasks)
            step_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert len(_payloads(runtime.conn, "response.failed")) == 2
            assert not turn_action_ids("first")
            assert not turn_action_ids("second")
            assert running_action_ids() == frozenset({"action-first", "action-second"})
            assert _payloads(runtime.conn, "surface.response_emitted") == []
            return
        submit("third")
        await until(lambda: len(_payloads(runtime.conn, "surface.response_emitted")) == 1)
        assert not release.is_set()
        assert turn_action_ids("first") == frozenset({"action-first"})
        assert turn_action_ids("second") == frozenset({"action-second"})
        assert len(_payloads(runtime.conn, "response.started")) == 3
        assert len(_payloads(runtime.conn, "response.completed")) == 1
        if outcome not in {"finish", "poll_error", "resume_connect_error"}:
            if outcome == "cancel":
                cancel = runtime_module.make_response_cancel_callable(runtime)
                for row in _payloads(runtime.conn, "response.started"):
                    if row["turn_id"] != "third":
                        assert cancel(row["response_id"], "generation", "user_stop") == "cancelled"
            elif outcome == "shutdown":
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            terminal = "response.cancelled" if outcome == "cancel" else "response.failed"
            await until(lambda: len(_payloads(runtime.conn, terminal)) == 2)
            await until(lambda: not turn_action_ids("first") and not turn_action_ids("second"))
            assert len(_payloads(runtime.conn, "surface.response_emitted")) == 1
            assert running_action_ids() == frozenset({"action-first", "action-second"})
            assert len(decisions) == 3
            return
        release.set()
        await until(lambda: len(_payloads(runtime.conn, "surface.response_emitted")) == 3)
        assert len(_payloads(runtime.conn, "response.started")) == 3
        assert len(_payloads(runtime.conn, "response.completed")) == 3
        assert decisions.count(("first", "surface.user_intent")) == 1
        assert decisions.count(("second", "surface.user_intent")) == 1
        assert decisions.count(("first", "action.failed")) == 1
        assert decisions.count(("second", "action.failed")) == 1
        await until(lambda: not turn_action_ids("first") and not turn_action_ids("second"))
        assert _payloads(runtime.conn, "turn.failed") == []
    finally:
        step_release.set()
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        fixture.close()
        base.conn.close()
