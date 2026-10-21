"""Real MCP connections through desktop controls, dynamic registry and continuation."""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.execution.tools import ToolRegistry
from jarvis.state.event_log import emit_event
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.support import plugin_connections as plugin_connections_support
from tests.support.mcp import ECHO
from tests.support.mcp import (
    oauth_url as oauth_url,  # noqa: PLC0414 — re-export fixture.
)
from tests.support.plugin_connections import (
    PANEL_TOOLS,
    make_service,
    open_plugin,
    plugin_tool_request,
    wait_for_request,
    write_package,
)
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
    from pathlib import Path

fixture = plugin_connections_support.fixture  # the fixture, found by name in this module


def test_connect_publishes_real_tools_skills_and_disable_removes_them(
    tmp_path: Path, fixture: ToolWorld
) -> None:
    """Connecting publishes callable tools and skills; changing policy needs no network."""
    write_package(tmp_path, "echo", ECHO)
    service = make_service(tmp_path, fixture)
    try:
        assert {t.name for t in fixture.registry.get_definitions()} == PANEL_TOOLS
        assert service.connected_apps_line() is None
        request_id = open_plugin(service, "echo")
        service.action("connect", {"request_id": request_id})
        wait_for_request(service, "ready")
        assert "echo:workflow" in service.skills_prompt()
        assert service.connected_apps_line() == "Connected apps: echo"
        fixture.dispatch(
            tool_request("mcp__echo__echo", "E1", arguments={"text": "connected live"})
        )
        assert json.loads(tool_chain(fixture, "E1")["tool_output"])["result"] == "connected live"
        service.action("approval", {"request_id": request_id, "mode": "prompt"})
        assert all(
            t.requires_confirmation
            for t in fixture.registry.get_definitions()
            if t.name.startswith("mcp__")
        )
        service.action("disable", {"request_id": request_id})
        assert {t.name for t in fixture.registry.get_definitions()} == PANEL_TOOLS
        assert service.skills_prompt() == ""
        assert service.connected_apps_line() is None
        assert (
            json.loads((fixture.paths.root / "plugin-settings.json").read_text())["echo"]["enabled"]
            is False
        )
    finally:
        service.stop()


@pytest.mark.parametrize("new_input", [False, True])
def test_original_task_resumes_once_only_if_still_current(
    tmp_path: Path, fixture: ToolWorld, *, new_input: bool
) -> None:
    """Continuation uses the recorded input exactly once and newer intent wins."""
    write_package(tmp_path, "echo", ECHO)
    service = make_service(tmp_path, fixture)
    try:
        request_id = plugin_tool_request(service, fixture, "echo", channel="gpt_live")
        if new_input:
            emit_event(
                fixture.conn,
                type="surface.user_intent",
                payload={"transcript": "Do something else", "turn_id": "T2"},
                correlation={"turn_id": "T2"},
            )
        service.action("connect", {"request_id": request_id})
        result = wait_for_request(service, "ready")
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
    tmp_path: Path, fixture: ToolWorld, oauth_url: str
) -> None:
    """The real bearer server connects, without putting credentials in visible state."""
    write_package(
        tmp_path,
        "private",
        {"private": {"url": oauth_url, "bearer_token_env_var": "PLUGIN_FIXTURE_TOKEN"}},
    )
    service = make_service(tmp_path, fixture)
    try:
        request_id = open_plugin(service, "private")
        service.action(
            "connect",
            {"request_id": request_id, "credentials": {"PLUGIN_FIXTURE_TOKEN": "static-secret"}},
        )
        wait_for_request(service, "ready")
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
    tmp_path: Path, fixture: ToolWorld, oauth_url: str
) -> None:
    """Cancel tears down a real OAuth callback so the next login can use its port."""
    write_package(tmp_path, "oauth", {"oauth": {"url": oauth_url, "auth": "oauth"}})
    urls: list[str] = []
    service = make_service(tmp_path, fixture, open_url=urls.append)
    try:
        request_id = open_plugin(service, "oauth")
        service.action("connect", {"request_id": request_id})
        wait_for_request(service, "authorizing")
        assert len(urls) == 1
        service.action("cancel", {"request_id": request_id})
        assert service.read()["request"]["state"] == "cancelled"
        assert not any(t.name.startswith("mcp__oauth") for t in fixture.registry.get_definitions())
        request_id = open_plugin(service, "oauth")
        service.action("connect", {"request_id": request_id})
        wait_for_request(service, "authorizing")
        assert len(urls) == 2
        with urllib.request.urlopen(urls[-1], timeout=8) as response:  # noqa: S310 — local test authorization server
            assert response.status == 200
        wait_for_request(service, "ready")
        assert any(t.name == "mcp__oauth__whoami" for t in fixture.registry.get_definitions())
        assert "access_token" not in json.dumps(service.read())
    finally:
        service.stop()


def test_gateway_only_plugin_is_not_connectable(tmp_path: Path, fixture: ToolWorld) -> None:
    """A hosted gateway component is an explicit unsupported state."""
    write_package(tmp_path, "gateway", {}, app_only=True)
    service = make_service(tmp_path, fixture)
    try:
        request_id = open_plugin(service, "gateway")
        assert service.catalog()["plugins"][0]["supported"] is False
        with pytest.raises(ValueError, match="网关"):
            service.action("connect", {"request_id": request_id})
        assert service.read()["request"]["state"] == "offered"
    finally:
        service.stop()


def test_asking_again_for_the_offered_app_keeps_its_request_and_another_app_replaces_it(
    tmp_path: Path, fixture: ToolWorld
) -> None:
    """ADR 0206: the panel he is looking at keeps its id; only `presentation` moves."""
    write_package(tmp_path, "alpha", {}, app_only=True)
    write_package(tmp_path, "beta", {}, app_only=True)
    service = make_service(tmp_path, fixture)
    try:
        first = open_plugin(service, "alpha")
        shown = service.read()["request"]["presentation"]
        assert open_plugin(service, "alpha") == first
        assert plugin_tool_request(service, fixture, "alpha") == first
        request = service.read()["request"]
        assert (request["state"], request["presentation"]) == ("offered", shown + 2)
        other = open_plugin(service, "beta")
        assert other != first
        assert service.read()["request"]["plugin_id"] == "beta"
    finally:
        service.stop()


def test_runtime_choices_survive_restart(tmp_path: Path, fixture: ToolWorld) -> None:
    """Enabled/disabled choices, approval and skills reload without rewriting YAML."""
    write_package(tmp_path, "echo", ECHO)
    service = make_service(tmp_path, fixture)
    try:
        request_id = open_plugin(service, "echo")
        service.action("connect", {"request_id": request_id})
        wait_for_request(service, "ready")
        service.action("approval", {"request_id": request_id, "mode": "prompt"})
    finally:
        service.stop()
    fixture.registry = ToolRegistry()
    restored = make_service(tmp_path, fixture)
    try:
        plugin = restored.read()["plugins"][0]
        assert plugin["status"] == "ready"
        assert plugin["approval_mode"] == "prompt"
        assert all(t["requires_confirmation"] for t in plugin["tools"])
        assert "echo:workflow" in restored.skills_prompt()
        restored.action("disable", {"request_id": open_plugin(restored, "echo")})
    finally:
        restored.stop()
    fixture.registry = ToolRegistry()
    disabled = make_service(tmp_path, fixture)
    try:
        assert disabled.read()["plugins"][0]["enabled"] is False
        assert disabled.skills_prompt() == ""
        assert not any(t.name.startswith("mcp__") for t in fixture.registry.get_definitions())
    finally:
        disabled.stop()


def test_conflicting_plugin_cannot_break_boot_or_existing_tools(
    tmp_path: Path, fixture: ToolWorld
) -> None:
    """One conflicting package is rejected while the first real MCP remains usable."""
    for name in ("first", "second"):
        write_package(tmp_path, name, ECHO)
    (fixture.paths.root / "plugin-settings.json").write_text(
        json.dumps({"first": {"enabled": True}, "second": {"enabled": True}})
    )
    service = make_service(tmp_path, fixture)
    try:
        assert [p["status"] for p in service.read()["plugins"]] == ["ready", "error"]
        fixture.dispatch(
            tool_request("mcp__echo__echo", "C1", arguments={"text": "still callable"})
        )
        assert json.loads(tool_chain(fixture, "C1")["tool_output"])["result"] == "still callable"
        assert "first:workflow" in service.skills_prompt()
        assert "second:workflow" not in service.skills_prompt()
    finally:
        service.stop()


def test_desktop_routes_require_local_credential_and_reject_stale_commands(
    tmp_path: Path, fixture: ToolWorld
) -> None:
    """Desktop writes require the private credential and the current request identity."""
    write_package(tmp_path, "echo", ECHO)
    service = make_service(tmp_path, fixture)
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


def test_icon_route_serves_the_manifest_logo_from_inside_the_package(
    tmp_path: Path, fixture: ToolWorld
) -> None:
    """The panel gets each package's own logo; a logo path out of the package reads nothing."""
    png = b"\x89PNG\r\n\x1a\nlogo"
    (tmp_path / "outside.png").write_bytes(b"\x89PNG\r\n\x1a\nnot the plugin's")
    for name, logo in (("shown", "./assets/logo.png"), ("escape", "../../outside.png")):
        write_package(tmp_path, name, {})
        manifest = tmp_path / "plugins" / name / ".codex-plugin/plugin.json"
        manifest.write_text(json.dumps({"name": name, "interface": {"logo": logo}}))
    (tmp_path / "plugins/shown/assets").mkdir()
    (tmp_path / "plugins/shown/assets/logo.png").write_bytes(png)
    service = make_service(tmp_path, fixture)
    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        plugin_read=service.read,
        plugin_action=service.action,
        plugin_authorize=service.settings.matches,
        plugin_icon=service.icon,
    )
    try:
        with TestClient(create_app(deps)) as client:
            assert client.get("/inherent/plugins/shown/icon").status_code == 401
            token = json.loads((fixture.paths.root / "plugin-access.json").read_text())["token"]
            headers = {"Authorization": f"Bearer {token}"}

            def icon(plugin_id: str) -> object:
                response = client.get(f"/inherent/plugins/{plugin_id}/icon", headers=headers)
                assert response.status_code == 200
                return response.json()["icon"]

            assert icon("shown") == f"data:image/png;base64,{base64.b64encode(png).decode()}"
            assert icon("escape") is None
            assert icon("nobody") is None
    finally:
        service.stop()
