"""An isolated plugin catalogue, its service and the requests it takes."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.tools import ToolContext
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.state.event_log import emit_event
from tests.support.clock import FakeClock
from tests.support.net import free_port
from tests.support.tool_world import ToolWorld

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

PANEL_TOOLS = {"list_plugins", "open_plugin", "disable_plugin", "remove_plugin"}


def write_package(
    root: Path, name: str, servers: dict[str, Any], *, app_only: bool = False
) -> None:
    path = root / "plugins" / name
    (path / ".codex-plugin").mkdir(parents=True)
    (path / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": name, "description": f"{name} test plugin"})
    )
    if servers:
        (path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    if app_only:
        (path / ".app.json").write_text("{}")
    skill = path / "skills" / "workflow"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: workflow\ndescription: A connected workflow\n---\nUse the connected tools."
    )


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ToolWorld]:
    """An isolated catalogue and real event log; never touch the user's runtime."""
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "empty-catalog"))
    fx = ToolWorld(tmp_path / "runtime", tools=())
    yield fx
    fx.close()


def make_service(
    root: Path, fx: ToolWorld, *, open_url: Callable[[str], object] | None = lambda _url: None
) -> PluginConnections:
    service = PluginConnections(
        repo_root=root,
        runtime_root=fx.paths.root,
        event_log=fx.paths.event_log,
        registry=fx.registry,
        config={"tools": {"mcp": {"timeout_s": 3, "oauth_callback_port": free_port()}}},
        open_url=open_url,
    )
    service.initialize()
    return service


def open_plugin(service: PluginConnections, plugin: str) -> str:
    return str(service.action("open", {"plugin_id": plugin})["request"]["id"])


def wait_for_request(service: PluginConnections, state: str, timeout: float = 12) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = service.read()["request"]
        if request and request["state"] == state:
            assert isinstance(request, dict)
            return request
        time.sleep(0.025)
    pytest.fail(f"expected {state}: {service.read()['request']}")


def plugin_tool_request(
    service: PluginConnections, fx: ToolWorld, plugin: str, *, channel: str = "desktop"
) -> str:
    emit_event(
        fx.conn,
        type="surface.user_intent",
        payload={"transcript": "Read my echo task", "turn_id": "T1", "channel": channel},
        correlation={"turn_id": "T1"},
    )
    emit_event(
        fx.conn, type="action.running", payload={"action_id": "A1"}, correlation={"turn_id": "T1"}
    )
    service.request_from_tool(
        {"plugin_id": plugin, "continue_task": True, "purpose": "Read my echo task"},
        ToolContext(fx.conn, fx.paths, "A1", FakeClock()),
    )
    return str(service.read()["request"]["id"])
