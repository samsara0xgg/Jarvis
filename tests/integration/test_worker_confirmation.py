"""Codex workers are off by default, ask before each start, and stay in their folders.

Drives the real ``decide()`` loop, dispatcher, confirmation grammar and event log
(with production's atomic confirmation-dispatch outbox on) against a scripted
model and a stand-in for the app-server. Asserts on the consent line, the
``confirmation.*`` rows, and whether a worker was actually started.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.execution.workers import Workers, make_worker_tools
from jarvis.runtime import _register_workers
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[2]
ASK = "要派这个后台任务吗？"  # noqa: RUF001 — the fixed Chinese ask under the card (ADR 0062).


@dataclass(frozen=True)
class _Paths:
    root: Path
    event_log: Path
    artifacts_root: Path

    def pending_write_path(self, confirmation_id: str) -> Path:
        return self.artifacts_root / "pending_writes" / confirmation_id


class _Spawns:
    """Stands in for the app-server: records every worker that would start."""

    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []

    def spawn(self, message: str, cwd: Path, *, parent: str, conn: object) -> str:
        del parent, conn
        self.started.append((message, cwd))
        return "W1"


class _ScriptedClient:
    """Asks for one worker in ``cwd``, then only talks."""

    model = "stub-model"
    last_input_tokens = 0
    last_output_tokens = 0
    last_finish_reason = "stop"

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.calls = 0

    @contextmanager
    def fresh_context(self) -> Iterator[_ScriptedClient]:
        yield self

    def chat(self, **_: Any) -> ChatResult:  # noqa: ANN401 — the decide() loop's keyword call.
        self.calls += 1
        args = {"message": "fix the typo in README", "cwd": str(self.cwd)}
        call = (
            ToolCall(call_id="c1", name="spawn_worker", arguments_json=json.dumps(args))
            if self.calls == 1
            else None
        )
        return ChatResult(
            text=None if call else "好的。",
            tool_calls=(call,) if call else (),
            finish_reason="tool_calls" if call else "stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _context(tmp_path: Path, cwd: Path, spawns: _Spawns) -> DecideContext:
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    for one in make_worker_tools(cast("Workers", spawns), ((tmp_path / "projects").resolve(),)):
        registry.register(one)
    paths = _Paths(
        root=tmp_path, event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", _ScriptedClient(cwd)),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(REPO / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
            confirmation_dispatch_outbox=True,
        ),
    )


def _say(ctx: DecideContext, text: str, turn_id: str) -> str:
    trigger = emit_event(
        ctx.conn,
        type="surface.user_intent",
        payload={"transcript": text, "turn_id": turn_id},
        correlation={"turn_id": turn_id},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return result.response_plan.text


def _rows(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type=? ORDER BY id", (event_type,)
        )
    ]


def test_workers_are_off_unless_enabled(tmp_path: Path) -> None:
    """The shipped config registers no worker tool; the user's settings turn them on."""
    shipped = build_default_registry()
    paths = cast("Any", _Paths(tmp_path, tmp_path / "events.db", tmp_path / "artifacts"))
    assert _register_workers(shipped, paths, {"tools": {"workers": {"enabled": False}}}) is None
    assert _register_workers(shipped, paths, {}) is None
    assert "spawn_worker" not in {t.name for t in shipped.get_definitions()}
    enabled = build_default_registry()
    workers = _register_workers(enabled, paths, {"tools": {"workers": {"enabled": True}}})
    assert workers is not None
    spawn = next(t for t in enabled.get_definitions() if t.name == "spawn_worker")
    assert (spawn.risk_level, spawn.requires_confirmation) == ("L3", True)
    send = next(t for t in enabled.get_definitions() if t.name == "send_input")
    assert "approval" not in send.input_schema["properties"]  # nothing answers for a worker


def test_a_worker_starts_only_after_the_user_says_yes(tmp_path: Path) -> None:
    """The ask ends the turn with nothing started; 可以 starts exactly that worker."""
    project = tmp_path / "projects" / "site"
    project.mkdir(parents=True)
    spawns = _Spawns()
    ctx = _context(tmp_path, project, spawns)
    try:
        ask = _say(ctx, "让 Codex 修一下 README", "T1")
        assert ask.endswith(ASK), ask
        assert spawns.started == []
        _say(ctx, "可以", "T2")
        assert len(_rows(ctx.conn, "confirmation.accepted")) == 1
        assert spawns.started == [("fix the typo in README", project.resolve())]
    finally:
        ctx.conn.close()


def test_no_leaves_nothing_started(tmp_path: Path) -> None:
    """不要 closes the ask and no worker starts."""
    project = tmp_path / "projects" / "site"
    project.mkdir(parents=True)
    spawns = _Spawns()
    ctx = _context(tmp_path, project, spawns)
    try:
        _say(ctx, "让 Codex 修一下 README", "T1")
        _say(ctx, "不要", "T2")
        assert len(_rows(ctx.conn, "confirmation.rejected")) == 1
        assert spawns.started == []
    finally:
        ctx.conn.close()


def test_a_yes_cannot_send_a_worker_outside_the_folders(tmp_path: Path) -> None:
    """Even after 可以, a cwd outside the configured roots (or escaping by ..) never starts."""
    outside = tmp_path / "projects" / ".." / "secrets"
    outside.mkdir(parents=True)
    spawns = _Spawns()
    ctx = _context(tmp_path, outside, spawns)
    try:
        _say(ctx, "让 Codex 看看这个文件夹", "T1")
        _say(ctx, "可以", "T2")
        observed = _rows(ctx.conn, "action.result_observed")[-1]
        assert json.loads(observed["tool_output"])["code"] == "cwd_not_allowed"
        assert spawns.started == []
    finally:
        ctx.conn.close()
