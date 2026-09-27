"""ADR 0033/0034/0061: a plugin tool is searched onto the menu, asks Allen, and runs on his yes.

Drives the real ``decide()`` loop, dispatcher, confirmation grammar and event log
(with production's atomic confirmation-dispatch outbox on) against the real echo
MCP server as a subprocess and a scripted model. Asserts on the tool lists the
model is offered, the consent line, and the ``confirmation.*`` /
``action.result_observed`` rows. A card's button is the ``surface.user_intent``
the desktop's ``POST /inherent/confirmation`` appends.
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
ASK = "要执行 echo add吗？"  # noqa: RUF001 — the fixed Chinese ask under the card (ADR 0061).
LETTER = {"to": "allen@example.com", "subject": "周六见", "body": "周六早上八点停车场见。"}


@dataclass(frozen=True)
class _Paths:
    event_log: Path
    artifacts_root: Path

    def pending_write_path(self, confirmation_id: str) -> Path:
        return self.artifacts_root / "pending_writes" / confirmation_id


class _ScriptedClient:
    """Searches for a tool, calls it, then only talks; records what it was offered."""

    model = "stub-model"
    last_input_tokens = 0
    last_output_tokens = 0
    last_finish_reason = "stop"

    def __init__(
        self,
        query: str = "add two integers",
        call: tuple[str, object] = ("add", {"a": 17, "b": 25}),
    ) -> None:
        self.offered: list[list[str]] = []
        self.seen: list[str] = []
        self.query, self.call = query, call

    @contextmanager
    def fresh_context(self) -> Iterator[_ScriptedClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.offered.append([t["name"] for t in tools or []])
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        name, arguments = self.call
        calls = {
            1: ToolCall(
                call_id="c1", name="tool_search", arguments_json=json.dumps({"query": self.query})
            ),
            2: ToolCall(
                call_id="c2",
                name=f"mcp__echo__{name}",
                arguments_json=json.dumps(arguments, ensure_ascii=False),
            ),
        }
        call = calls.get(len(self.offered))
        return ChatResult(
            text=None if call else "现在是下午三点。要我顺便把明天的日程也念一下吗？",  # noqa: RUF001
            tool_calls=(call,) if call else (),
            finish_reason="tool_calls" if call else "stop",
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


def _press(ctx: DecideContext, decision: dict[str, Any], turn_id: str) -> str:
    """A card's button: an empty-transcript intent naming the confirmation (ADR 0061)."""
    trigger = emit_event(
        ctx.conn,
        type="surface.user_intent",
        payload={"transcript": "", "turn_id": turn_id, "confirmation_decision": decision},
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
    # ADR 0056: the hit's server brings its read-only echo along; boom has no hint, so it stays off.
    assert "mcp__echo__echo" in second
    assert "mcp__echo__boom" not in second
    assert _rows(ctx.conn, "confirmation.requested")[0]["template_line"] == ASK
    assert len(_rows(ctx.conn, "action.result_observed")) == 1  # only the search ran
    return ctx, llm


def test_searched_plugin_tool_asks_then_runs_on_yes(tmp_path: Path, servers: McpServers) -> None:
    """Allen's 可以 re-proposes the frozen arguments through the outbox and the adder runs once."""
    ctx, llm = _ask(tmp_path, servers)
    try:
        assert _say(ctx, "可以", "T2") == "已执行 echo add。"
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
        assert _say(ctx, "不要", "T2") == "好，不执行 echo add了。"  # noqa: RUF001 — the fixed Chinese rejection.
        assert len(_rows(ctx.conn, "confirmation.rejected")) == 1
        assert len(_rows(ctx.conn, "action.result_observed")) == 1  # still only the search
    finally:
        ctx.conn.close()


def test_an_unrelated_turn_leaves_the_card_to_its_button(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0061: a 好 meant for the model's own question never fires the card; its button does."""
    ctx, llm = _ask(tmp_path, servers)
    try:
        _say(ctx, "现在几点", "T2")
        assert "A card is waiting for the user's button: mcp__echo__add" in llm.seen[-1]
        assert _rows(ctx.conn, "confirmation.rejected") == []  # the card still waits
        _say(ctx, "好", "T3")
        assert len(llm.offered) == 4  # the 好 reached the model, not the grammar
        assert _rows(ctx.conn, "confirmation.accepted") == []
        assert len(_rows(ctx.conn, "action.result_observed")) == 1  # still only the search
        card = _rows(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        pressed = _press(ctx, {"confirmation_id": card, "decision": "accept"}, "T4")
        assert pressed == "已执行 echo add。"
        assert len(llm.offered) == 4  # the button never reaches the model
        assert [r["grammar_rule_id"] for r in _rows(ctx.conn, "confirmation.accepted")] == [
            "card_button"
        ]
        assert json.loads(_rows(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "sum": 42
        }
        # Pressed again, the card is gone: nothing runs twice.
        assert _press(ctx, {"confirmation_id": card, "decision": "accept"}, "T5") == (
            "这张卡已经处理过或被换掉了，没有执行。"  # noqa: RUF001 — the fixed Chinese stale line.
        )
        assert len(_rows(ctx.conn, "action.result_observed")) == 2
    finally:
        ctx.conn.close()


def test_a_letter_card_sends_what_allen_edited(tmp_path: Path, servers: McpServers) -> None:
    """ADR 0061: the ask names the letter; send with an edited body re-freezes it, then runs it."""
    llm = _ScriptedClient(query="send a letter", call=("send", LETTER))
    ctx = _context(tmp_path, servers, llm)
    try:
        ask = _say(ctx, "给我自己发一封信", "T1")
        assert ask == "信写好了，发给 allen@example.com，主题「周六见」。要发吗？"  # noqa: RUF001
        card = _rows(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        edits = {"body": "周六早上九点停车场见。", "to": 42, "cc": "someone@else"}
        pressed = _press(ctx, {"confirmation_id": card, "decision": "accept", "edits": edits}, "T2")
        assert pressed == "已执行 echo send。"
        edited = {**LETTER, "body": "周六早上九点停车场见。"}
        # Only the existing string field changed; the new snapshot is exactly what ran.
        asked = _rows(ctx.conn, "confirmation.requested")
        assert [r["action_snapshot"]["args_meta"] for r in asked] == [LETTER, edited]
        assert _rows(ctx.conn, "confirmation.accepted")[0]["confirmation_id"] == asked[1][
            "confirmation_id"
        ]
        proposed = _rows(ctx.conn, "action.proposed")
        sent = [r for r in proposed if r["tool_name"] == "mcp__echo__send"]
        assert sent[-1]["arguments"] == edited
        assert json.loads(_rows(ctx.conn, "action.result_observed")[-1]["tool_output"]) == edited
    finally:
        ctx.conn.close()


def test_the_cards_x_dismisses_it(tmp_path: Path, servers: McpServers) -> None:
    """ADR 0061: the card's close button is a rejection bound to its id; nothing runs."""
    ctx, _ = _ask(tmp_path, servers)
    try:
        card = _rows(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        assert _press(ctx, {"confirmation_id": card, "decision": "reject"}, "T2") == (
            "好，不执行 echo add了。"  # noqa: RUF001 — the fixed Chinese rejection.
        )
        assert [r["grammar_rule_id"] for r in _rows(ctx.conn, "confirmation.rejected")] == [
            "card_dismiss"
        ]
        assert len(_rows(ctx.conn, "action.result_observed")) == 1  # still only the search
    finally:
        ctx.conn.close()
