"""ADR 0033/0034: a plugin tool is searched onto the menu, asks Allen, and runs on his yes.

Drives the real ``decide()`` loop, dispatcher, confirmation grammar and event log
(with production's atomic confirmation-dispatch outbox on) against the real echo
MCP server as a subprocess and a scripted model. Asserts on the tool lists the
model is offered, the consent line, and the ``confirmation.*`` /
``action.result_observed`` rows.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.execution.mcp_tools import McpServers
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

HERE = Path(__file__).parent
REPO = HERE.parent.parent
ECHO = {"echo": {"command": sys.executable, "args": [str(HERE / "mcp_echo_server.py")]}}
ASK = '待确认：echo add（{"a": 17, "b": 25}）。回复「可以」执行，「不要」取消。'  # noqa: RUF001 — the fixed Chinese consent line.


@dataclass(frozen=True)
class _Paths:
    event_log: Path
    artifacts_root: Path

    def pending_write_path(self, confirmation_id: str) -> Path:
        return self.artifacts_root / "pending_writes" / confirmation_id


class _ScriptedClient:
    """Searches for the adder, then calls it; records every tool list it was offered."""

    model = "stub-model"
    last_input_tokens = 0
    last_output_tokens = 0
    last_finish_reason = "stop"

    def __init__(self) -> None:
        self.offered: list[list[str]] = []

    @contextmanager
    def fresh_context(self) -> Iterator[_ScriptedClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.offered.append([t["name"] for t in tools or []])
        if len(self.offered) == 1:
            call = ToolCall(
                call_id="c1", name="tool_search", arguments_json='{"query": "add two integers"}'
            )
        else:
            call = ToolCall(
                call_id="c2", name="mcp__echo__add", arguments_json='{"a": 17, "b": 25}'
            )
        return ChatResult(
            text=None,
            tool_calls=(call,),
            finish_reason="tool_calls",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread; stopped after the test so the subprocess exits."""
    mcp = McpServers(timeout_s=20)
    yield mcp
    mcp.stop()


def _context(tmp_path: Path, servers: McpServers, llm: _ScriptedClient) -> DecideContext:
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    mcp_tools = servers.connect(ECHO)
    for one in (*mcp_tools, *build_tool_search(mcp_tools, {"echo": "Echo test server"})):
        registry.register(one)
    paths = _Paths(event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts")
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
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


def _ask(tmp_path: Path, servers: McpServers) -> tuple[DecideContext, _ScriptedClient]:
    """Turn 1: the adder is not offered until searched, and calling it asks instead of running."""
    llm = _ScriptedClient()
    ctx = _context(tmp_path, servers, llm)
    assert _say(ctx, "用 add 工具算 17 加 25", "T1") == ASK
    first, second = llm.offered
    assert "tool_search" in first
    assert not [name for name in first if name.startswith("mcp__")]
    assert "mcp__echo__add" in second
    assert _rows(ctx.conn, "confirmation.requested")[0]["template_line"] == ASK
    assert len(_rows(ctx.conn, "action.result_observed")) == 1  # only the search ran
    return ctx, llm


def test_searched_plugin_tool_asks_then_runs_on_yes(tmp_path: Path, servers: McpServers) -> None:
    """Allen's 可以 re-proposes the frozen arguments through the outbox and the adder runs once."""
    ctx, llm = _ask(tmp_path, servers)
    try:
        assert _say(ctx, "可以", "T2") == 'echo add 已执行。结果：{"sum": 42}'  # noqa: RUF001 — the fixed Chinese ack.
        assert len(llm.offered) == 2  # the yes is matched by grammar, never by the model
        assert len(_rows(ctx.conn, "confirmation.accepted")) == 1
        added = [
            r for r in _rows(ctx.conn, "action.proposed") if r["tool_name"] == "mcp__echo__add"
        ]
        assert [r["arguments"] for r in added] == [{"a": 17, "b": 25}, {"a": 17, "b": 25}]
        assert json.loads(_rows(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "sum": 42
        }
    finally:
        ctx.conn.close()


def test_searched_plugin_tool_does_nothing_on_no(tmp_path: Path, servers: McpServers) -> None:
    """Allen's 不要 closes the ask; the adder never runs."""
    ctx, _ = _ask(tmp_path, servers)
    try:
        assert _say(ctx, "不要", "T2") == f"好，已取消：{ASK}"  # noqa: RUF001 — the fixed Chinese rejection.
        assert len(_rows(ctx.conn, "confirmation.rejected")) == 1
        assert len(_rows(ctx.conn, "action.result_observed")) == 1  # still only the search
    finally:
        ctx.conn.close()
