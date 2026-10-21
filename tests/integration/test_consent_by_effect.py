"""ADR 0321: the declared effect of a plugin tool decides whether it needs a card.

The real ``decide()`` loop, dispatcher and event log run against a real stdio MCP server that
stands in for a smart-home bridge, with a scripted model. The manifest side is the shipped Hue
plugin's own ``tools`` map. Asserts on the rows a turn leaves: whether a card was asked, and
the receipt each call ended in.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.mcp_tools import McpServers
from jarvis.execution.tools import Tool
from jarvis.shared import CallerPrincipal
from tests.support import consent
from tests.support.clock import FakeClock
from tests.support.model import ScriptedModel, tool_call
from tests.support.plugin_approval import logged_dicts, say
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

DONE = "客厅的灯关了。"
OFF = {"light": "living room", "on": False}


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread with the stand-in's tools on the menu; stopped after the test."""
    mcp = McpServers(timeout_s=20, always_loaded=consent.LIGHTS_TOOLS)
    yield mcp
    mcp.stop()


def _turn(
    tmp_path: Path,
    servers: McpServers,
    tools: dict[str, Any],
    tool: str,
    arguments: dict[str, Any],
) -> tuple[str, Any]:
    """One turn in which the model calls ``tool`` once; returns her words and the context."""
    llm = ScriptedModel(tool_call(f"mcp__lights__{tool}", arguments, call_id="c1"), DONE)
    ctx = consent.context(tmp_path, servers, llm, consent.lights(tools))
    return say(ctx, "把客厅的灯关了", "T1"), ctx


def test_a_hue_write_runs_without_a_card_and_ends_in_a_done_receipt(
    tmp_path: Path, servers: McpServers,
) -> None:
    """Hue's manifest declares its writes non-destructive and closed-world: no card stands there."""
    answer, ctx = _turn(tmp_path, servers, consent.hue_manifest_tools(), "set_light", OFF)
    try:
        assert answer == DONE
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        observed = logged_dicts(ctx.conn, "action.result_observed")[-1]
        assert observed["receipt"]["status"] == "done"
        assert '"on": false' in observed["tool_output"]
    finally:
        ctx.conn.close()


def test_his_per_tool_prompt_mode_still_makes_a_card(tmp_path: Path, servers: McpServers) -> None:
    """ADR 0033: his setting overrides what the manifest declares."""
    tools = consent.hue_manifest_tools()
    tools["set_light"] = {**tools["set_light"], "approval_mode": "prompt"}
    answer, ctx = _turn(tmp_path, servers, tools, "set_light", OFF)
    try:
        assert "light：living room，on：否" in answer  # noqa: RUF001 — the Chinese ask names its arguments.
        assert len(logged_dicts(ctx.conn, "confirmation.requested")) == 1
        assert logged_dicts(ctx.conn, "action.result_observed") == []
    finally:
        ctx.conn.close()


def test_a_server_that_marks_a_tool_destructive_keeps_its_card_whatever_the_manifest_says(
    tmp_path: Path, servers: McpServers,
) -> None:
    """The server's own hint wins; the manifest only fills what the server left unset."""
    tools = {"wipe_bridge": {"annotations": {"destructiveHint": False, "openWorldHint": False}}}
    _, ctx = _turn(tmp_path, servers, tools, "wipe_bridge", {})
    try:
        assert len(logged_dicts(ctx.conn, "confirmation.requested")) == 1
        assert logged_dicts(ctx.conn, "action.result_observed") == []
    finally:
        ctx.conn.close()


def test_a_hint_the_server_gives_wins_field_by_field(tmp_path: Path, servers: McpServers) -> None:
    """The server says open-world; the manifest's non-destructive fills the missing field only."""
    tools = {"send_to_cloud": {"annotations": {"destructiveHint": False, "openWorldHint": False}}}
    _, ctx = _turn(tmp_path, servers, tools, "send_to_cloud", {"text": "hi"})
    try:
        assert len(logged_dicts(ctx.conn, "confirmation.requested")) == 1
    finally:
        ctx.conn.close()


def test_a_tool_the_manifest_declares_nothing_for_still_asks(
    tmp_path: Path, servers: McpServers,
) -> None:
    """A missing hint counts as the risky value (ADR 0033): no declaration, no change."""
    _, ctx = _turn(tmp_path, servers, {}, "set_light", OFF)
    try:
        assert len(logged_dicts(ctx.conn, "confirmation.requested")) == 1
    finally:
        ctx.conn.close()


def _send_it(ran: list[str]) -> Tool:
    """A confirm-required fixture tool that notes each run in ``ran``."""

    def handler(_args: Any, _ctx: Any) -> dict[str, Any]:  # noqa: ANN401
        ran.append("ran")
        return {}

    return Tool(
        name="send_it",
        description="Sends it.",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L3",
        read_only=False,
        requires_confirmation=True,
    )


def test_l4_refuses_a_call_to_a_confirm_required_tool_that_carries_no_lease(
    tmp_path: Path,
) -> None:
    """I7 at dispatch: without a valid lease the handler never runs and the receipt is failed."""
    ran: list[str] = []
    fx = ToolWorld(tmp_path, tools=(_send_it(ran),))
    try:
        fx.dispatch(tool_request("send_it", "A1"))
        refused = tool_chain(fx, "A1")
        assert refused["receipt"]["status"] == "failed"
        assert refused["receipt"]["failure"] == "dont_retry"
        assert json.loads(refused["tool_output"])["code"] == "no_lease"
        assert ran == []
        fx.dispatch(tool_request("send_it", "A2", lease=True))
        assert tool_chain(fx, "A2")["receipt"]["status"] == "done"
        assert ran == ["ran"]
    finally:
        fx.close()


def test_l4_ends_a_lease_at_its_expiry_on_the_dispatcher_s_clock(tmp_path: Path) -> None:
    """A lease runs its call until its expiry on the dispatcher's clock, never the wall clock's."""
    ran: list[str] = []
    clock = FakeClock()  # far from the wall clock, so a wall-clock read answers differently
    ends = clock.time_ms() + 60_000
    fx = ToolWorld(tmp_path, tools=(_send_it(ran),), clock=clock)
    try:
        fx.dispatch(tool_request("send_it", "A1", lease=True, lease_expires_at_ms=ends))
        assert tool_chain(fx, "A1")["receipt"]["status"] == "done"
        clock.advance(61)
        fx.dispatch(tool_request("send_it", "A2", lease=True, lease_expires_at_ms=ends))
        late = tool_chain(fx, "A2")
        assert json.loads(late["tool_output"])["code"] == "no_lease"
        assert ran == ["ran"]
    finally:
        fx.close()
