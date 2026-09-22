"""MCP acceptance: a real stdio server, the real dispatcher and the SQLite log (ADR 0031)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.mcp_tools import McpServers
from tests.integration.test_flat_tool_dispatch import _chain, _Fixture, _request

if TYPE_CHECKING:
    from collections.abc import Iterator

SERVER = Path(__file__).with_name("mcp_echo_server.py")
ECHO = {"echo": {"command": sys.executable, "args": [str(SERVER)]}}


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread; stopped after the test so the subprocess exits."""
    mcp = McpServers(timeout_s=20)
    yield mcp
    mcp.stop()


def test_listed_tools_dispatch_through_the_real_log(tmp_path: Path, servers: McpServers) -> None:
    """echo/add/boom arrive named and risk-mapped; each call ends in one result_observed row."""
    tools = servers.connect(ECHO)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__boom"}
    echo, add = by_name["mcp__echo__echo"], by_name["mcp__echo__add"]
    assert (echo.read_only, echo.risk_level) == (True, "L0")
    assert (add.read_only, add.risk_level) == (False, "L1")
    assert add.input_schema["required"] == ["a", "b"]

    fx = _Fixture(tmp_path, tools=tools)
    try:
        fx.dispatch(_request("mcp__echo__echo", "M1", arguments={"text": "你好 MCP"}))
        observed = _chain(fx, "M1")
        assert observed["semantics"] == "observation"
        # mcp 2.x wraps a bare `str` return as structured content; that wins over the text block.
        assert json.loads(observed["tool_output"]) == {"result": "你好 MCP"}

        fx.dispatch(_request("mcp__echo__add", "M2", arguments={"a": 1, "b": 2}))
        observed = _chain(fx, "M2")
        assert json.loads(observed["tool_output"]) == {"sum": 3}

        fx.dispatch(_request("mcp__echo__boom", "M3"))
        observed = _chain(fx, "M3")
        assert observed["semantics"] == "error"
        assert observed["error"] == "mcp_tool_error"
        # mcp 2.x masks the server-side exception; the model sees the SDK's generic text.
        assert json.loads(observed["tool_output"])["error"] == "Error executing tool boom"
    finally:
        fx.close()


def test_unreachable_server_contributes_nothing(servers: McpServers) -> None:
    """A missing binary is a warning, not a boot failure; the reachable server still lists."""
    tools = servers.connect({"ghost": {"command": "/nonexistent/mcp-server"}, **ECHO})
    assert {t.name.split("__")[1] for t in tools} == {"echo"}
