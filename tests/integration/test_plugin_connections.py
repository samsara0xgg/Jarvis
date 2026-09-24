"""Real MCP connections through desktop controls, dynamic registry and continuation."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.execution.tools import ToolContext, ToolRegistry
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.state.event_log import emit_event
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_flat_tool_dispatch import _chain, _Fixture, _request
from tests.integration.test_mcp_tools import (
    ECHO,
    _free_port,
)
from tests.integration.test_mcp_tools import (
    oauth_url as oauth_url,  # noqa: PLC0414 — re-export fixture.
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def _package(root: Path, name: str, servers: dict[str, Any], *, app_only: bool = False) -> None:
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
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Fixture]:
    """An isolated catalogue and real event log; never touch the user's runtime."""
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "empty-catalog"))
    fx = _Fixture(tmp_path / "runtime", tools=())
    yield fx
    fx.close()


def _service(
    root: Path, fx: _Fixture, *, open_url: Callable[[str], object] = lambda _url: None
) -> PluginConnections:
    service = PluginConnections(
        repo_root=root,
        runtime_root=fx.paths.root,
        event_log=fx.paths.event_log,
        registry=fx.registry,
        config={"tools": {"mcp": {"timeout_s": 3, "oauth_callback_port": _free_port()}}},
        open_url=open_url,
    )
    service.initialize()
    return service


def _open(service: PluginConnections, plugin: str) -> str:
    return str(service.action("open", {"plugin_id": plugin})["request"]["id"])


def _wait(service: PluginConnections, state: str, timeout: float = 12) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = service.read()["request"]
        if request and request["state"] == state:
            return request
        time.sleep(0.025)
    pytest.fail(f"expected {state}: {service.read()['request']}")


def _tool_request(
    service: PluginConnections, fx: _Fixture, plugin: str, *, channel: str = "desktop"
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
        ToolContext(fx.conn, fx.paths, "A1"),
    )
    return str(service.read()["request"]["id"])


def test_connect_publishes_real_tools_skills_and_disable_removes_them(
    tmp_path: Path, fixture: _Fixture
) -> None:
    """Connecting publishes callable tools and skills; changing policy needs no network."""
    _package(tmp_path, "echo", ECHO)
    service = _service(tmp_path, fixture)
    try:
        assert {t.name for t in fixture.registry.get_definitions()} == {
            "list_plugins",
            "open_plugin",
        }
        request_id = _open(service, "echo")
        service.action("connect", {"request_id": request_id})
        _wait(service, "ready")
        assert "echo:workflow" in service.skills_prompt()
        fixture.dispatch(_request("mcp__echo__echo", "E1", arguments={"text": "connected live"}))
        assert json.loads(_chain(fixture, "E1")["tool_output"])["result"] == "connected live"
        service.action("approval", {"request_id": request_id, "mode": "prompt"})
        assert all(
            t.requires_confirmation
            for t in fixture.registry.get_definitions()
            if t.name.startswith("mcp__")
        )
        service.action("disable", {"request_id": request_id})
        assert {t.name for t in fixture.registry.get_definitions()} == {
            "list_plugins",
            "open_plugin",
        }
        assert service.skills_prompt() == ""
        assert (
            json.loads((fixture.paths.root / "plugin-settings.json").read_text())["echo"]["enabled"]
            is False
        )
    finally:
        service.stop()


@pytest.mark.parametrize("new_input", [False, True])
def test_original_task_resumes_once_only_if_still_current(
    tmp_path: Path, fixture: _Fixture, *, new_input: bool
) -> None:
    """Continuation uses the recorded input exactly once and newer intent wins."""
    _package(tmp_path, "echo", ECHO)
    service = _service(tmp_path, fixture)
    try:
        request_id = _tool_request(service, fixture, "echo", channel="gpt_live")
        if new_input:
            emit_event(
                fixture.conn,
                type="surface.user_intent",
                payload={"transcript": "Do something else", "turn_id": "T2"},
                correlation={"turn_id": "T2"},
            )
        service.action("connect", {"request_id": request_id})
        result = _wait(service, "ready")
        assert result["resume_status"] == ("superseded" if new_input else "continued")
        service.action("connect", {"request_id": request_id})
        rows = fixture.conn.execute(
            "SELECT payload_json FROM events "
            "WHERE json_extract(payload_json, '$.channel') = 'plugin_resume'"
        ).fetchall()
        assert len(rows) == (0 if new_input else 1)
        if rows:
            assert json.loads(rows[0][0])["transcript"] == "Read my echo task"
            assert json.loads(rows[0][0])["plugin_origin_channel"] == "gpt_live"
    finally:
        service.stop()


def test_bearer_credentials_stay_private_and_work_without_restart(
    tmp_path: Path, fixture: _Fixture, oauth_url: str
) -> None:
    """The real bearer server connects, without putting credentials in visible state."""
    _package(
        tmp_path,
        "private",
        {"private": {"url": oauth_url, "bearer_token_env_var": "PLUGIN_FIXTURE_TOKEN"}},
    )
    service = _service(tmp_path, fixture)
    try:
        request_id = _open(service, "private")
        service.action(
            "connect",
            {"request_id": request_id, "credentials": {"PLUGIN_FIXTURE_TOKEN": "static-secret"}},
        )
        _wait(service, "ready")
        assert "static-secret" not in json.dumps(service.read())
        assert "static-secret" not in json.dumps(service.catalog())
        assert "static-secret" not in str(
            fixture.conn.execute("SELECT payload_json FROM events").fetchall()
        )
        path = fixture.paths.root / "plugin-credentials.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["private"]["PLUGIN_FIXTURE_TOKEN"] == "static-secret"  # noqa: S105 — local test server credential
        assert any(t.name == "mcp__private__whoami" for t in fixture.registry.get_definitions())
    finally:
        service.stop()


def test_oauth_cancel_releases_callback_and_retry_succeeds(
    tmp_path: Path, fixture: _Fixture, oauth_url: str
) -> None:
    """Cancel tears down a real OAuth callback so the next login can use its port."""
    _package(tmp_path, "oauth", {"oauth": {"url": oauth_url, "auth": "oauth"}})
    urls: list[str] = []
    service = _service(tmp_path, fixture, open_url=urls.append)
    try:
        request_id = _open(service, "oauth")
        service.action("connect", {"request_id": request_id})
        _wait(service, "authorizing")
        assert len(urls) == 1
        service.action("cancel", {"request_id": request_id})
        assert service.read()["request"]["state"] == "cancelled"
        assert not any(t.name.startswith("mcp__oauth") for t in fixture.registry.get_definitions())
        request_id = _open(service, "oauth")
        service.action("connect", {"request_id": request_id})
        _wait(service, "authorizing")
        assert len(urls) == 2
        with urllib.request.urlopen(urls[-1], timeout=8) as response:  # noqa: S310 — local test authorization server
            assert response.status == 200
        _wait(service, "ready")
        assert any(t.name == "mcp__oauth__whoami" for t in fixture.registry.get_definitions())
        assert "access_token" not in json.dumps(service.read())
    finally:
        service.stop()


def test_gateway_only_plugin_is_not_connectable(tmp_path: Path, fixture: _Fixture) -> None:
    """A hosted gateway component is an explicit unsupported state."""
    _package(tmp_path, "gateway", {}, app_only=True)
    service = _service(tmp_path, fixture)
    try:
        request_id = _open(service, "gateway")
        assert service.catalog()["plugins"][0]["supported"] is False
        with pytest.raises(ValueError, match="网关"):
            service.action("connect", {"request_id": request_id})
        assert service.read()["request"]["state"] == "offered"
    finally:
        service.stop()


def test_runtime_choices_survive_restart(tmp_path: Path, fixture: _Fixture) -> None:
    """Enabled/disabled choices, approval and skills reload without rewriting YAML."""
    _package(tmp_path, "echo", ECHO)
    service = _service(tmp_path, fixture)
    try:
        request_id = _open(service, "echo")
        service.action("connect", {"request_id": request_id})
        _wait(service, "ready")
        service.action("approval", {"request_id": request_id, "mode": "prompt"})
    finally:
        service.stop()
    fixture.registry = ToolRegistry()
    restored = _service(tmp_path, fixture)
    try:
        plugin = restored.read()["plugins"][0]
        assert plugin["status"] == "ready"
        assert plugin["approval_mode"] == "prompt"
        assert all(t["requires_confirmation"] for t in plugin["tools"])
        assert "echo:workflow" in restored.skills_prompt()
        restored.action("disable", {"request_id": _open(restored, "echo")})
    finally:
        restored.stop()
    fixture.registry = ToolRegistry()
    disabled = _service(tmp_path, fixture)
    try:
        assert disabled.read()["plugins"][0]["enabled"] is False
        assert disabled.skills_prompt() == ""
        assert not any(t.name.startswith("mcp__") for t in fixture.registry.get_definitions())
    finally:
        disabled.stop()


def test_conflicting_plugin_cannot_break_boot_or_existing_tools(
    tmp_path: Path, fixture: _Fixture
) -> None:
    """One conflicting package is rejected while the first real MCP remains usable."""
    for name in ("first", "second"):
        _package(tmp_path, name, ECHO)
    (fixture.paths.root / "plugin-settings.json").write_text(
        json.dumps({"first": {"enabled": True}, "second": {"enabled": True}})
    )
    service = _service(tmp_path, fixture)
    try:
        assert [p["status"] for p in service.read()["plugins"]] == ["ready", "error"]
        fixture.dispatch(_request("mcp__echo__echo", "C1", arguments={"text": "still callable"}))
        assert json.loads(_chain(fixture, "C1")["tool_output"])["result"] == "still callable"
        assert "first:workflow" in service.skills_prompt()
        assert "second:workflow" not in service.skills_prompt()
    finally:
        service.stop()


def test_desktop_routes_require_local_credential_and_reject_stale_commands(
    tmp_path: Path, fixture: _Fixture
) -> None:
    """Desktop writes require the private credential and the current request identity."""
    _package(tmp_path, "echo", ECHO)
    service = _service(tmp_path, fixture)
    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        plugin_read=service.read,
        plugin_action=service.action,
        plugin_authorize=service.settings.matches,
    )
    try:
        with TestClient(create_app(deps)) as client:
            assert client.get("/inherent/plugins").status_code == 401
            assert (
                client.post(
                    "/inherent/plugins/action",
                    json={"operation": "open", "data": {"plugin_id": "echo"}},
                ).status_code
                == 401
            )
            token = json.loads((fixture.paths.root / "plugin-access.json").read_text())["token"]
            headers = {"Authorization": f"Bearer {token}"}
            assert client.get("/inherent/plugins", headers=headers).status_code == 200
            opened = client.post(
                "/inherent/plugins/action",
                headers=headers,
                json={"operation": "open", "data": {"plugin_id": "echo"}},
            )
            assert opened.status_code == 200
            bad = client.post(
                "/inherent/plugins/action",
                headers=headers,
                json={"operation": "connect", "data": {"request_id": "old"}},
            )
            assert bad.status_code == 400
    finally:
        service.stop()
