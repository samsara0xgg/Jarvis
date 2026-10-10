"""ADR 0202: she starts a connection, turns a plugin off, and removes one on his yes.

Real plugin connections, the real registry dispatcher and, for the removal, the real ``decide()``
loop with its confirmation card; the OAuth server is the local test one, no outside network.
What is asserted is what is visible: the request a paired device reads, the tool list, the files
on the runtime root, the card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
)
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.decision.llm import ChatResult, LLMClient, ToolCall
from jarvis.execution.tools import (
    ActionLifecycle,
    ToolContext,
    ToolRegistry,
    build_default_registry,
)
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import open_event_log
from tests.integration.test_flat_tool_dispatch import _chain, _Fixture, _request
from tests.integration.test_mcp_tools import ECHO
from tests.integration.test_mcp_tools import (
    oauth_url as oauth_url,  # noqa: PLC0414 — re-export fixture.
)
from tests.integration.test_plugin_connections import (
    PANEL_TOOLS,
    _open,
    _package,
    _service,
    _tool_request,
    _wait,
)
from tests.integration.test_plugin_connections import (
    fixture as fixture,  # noqa: PLC0414 — re-export fixture.
)
from tests.integration.test_plugin_tool_approval import _Paths, _press, _rows, _say

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_open_plugin_starts_a_credential_free_plugin_and_only_opens_the_panel_for_a_key(
    tmp_path: Path, fixture: _Fixture
) -> None:
    """The echo plugin connects at once; one that needs a typed key waits for its field."""
    _package(tmp_path, "echo", ECHO)
    _package(
        tmp_path,
        "private",
        {"private": {"url": "http://127.0.0.1:1/mcp", "bearer_token_env_var": "ADR0202_NO_KEY"}},
    )
    service = _service(tmp_path, fixture)
    try:
        _tool_request(service, fixture, "echo")
        assert service.read()["request"]["state"] in {"connecting", "ready"}
        _wait(service, "ready")
        assert service.connected_apps_line() == "Connected apps: echo"

        reply = service.request_from_tool(
            {"plugin_id": "private", "continue_task": False},
            ToolContext(fixture.conn, fixture.paths, "A1"),
        )
        assert reply["state"] == "offered"
        assert "enter the key" in reply["instruction"]
        assert service.read()["request"]["state"] == "offered"
    finally:
        service.stop()


def test_a_credential_free_start_keeps_the_task_to_continue(
    tmp_path: Path, fixture: _Fixture
) -> None:
    """continue_task rides on the immediately started connection exactly as on a click."""
    _package(tmp_path, "echo", ECHO)
    service = _service(tmp_path, fixture)
    try:
        _tool_request(service, fixture, "echo", channel="gpt_live")
        done = _wait(service, "ready")
        assert done["resume_status"] == "continued"
    finally:
        service.stop()


def test_disable_plugin_needs_no_request_id_and_waits_for_a_busy_connection(
    tmp_path: Path, fixture: _Fixture, oauth_url: str
) -> None:
    """Off by name: tools leave and the choice is kept; refused while it is connecting."""
    _package(tmp_path, "echo", ECHO)
    _package(tmp_path, "oauth", {"oauth": {"url": oauth_url, "auth": "oauth"}})
    service = _service(tmp_path, fixture, open_url=None)
    try:
        service.action("connect", {"request_id": _open(service, "echo")})
        _wait(service, "ready")
        assert any(t.name == "mcp__echo__echo" for t in fixture.registry.get_definitions())
        fixture.dispatch(_request("disable_plugin", "D1", arguments={"plugin_id": "echo"}))
        assert json.loads(_chain(fixture, "D1")["tool_output"])["enabled"] is False
        assert {t.name for t in fixture.registry.get_definitions()} == PANEL_TOOLS
        settings = json.loads((fixture.paths.root / "plugin-settings.json").read_text())
        assert settings["echo"]["enabled"] is False

        service.action("connect", {"request_id": _open(service, "oauth")})
        _wait(service, "authorizing")
        fixture.dispatch(_request("disable_plugin", "D2", arguments={"plugin_id": "oauth"}))
        assert _chain(fixture, "D2")["error"] == "plugin"
        fixture.dispatch(_request("disable_plugin", "D3", arguments={"plugin_id": "nobody"}))
        assert _chain(fixture, "D3")["error"] == "plugin"
    finally:
        service.stop()


class _RemovingClient:
    """Calls remove_plugin on the first model call, then only talks."""

    model = "stub-model"
    last_input_tokens = 0
    last_output_tokens = 0
    last_finish_reason = "stop"

    def __init__(self) -> None:
        self.offered: list[list[str]] = []

    def fresh_context(self) -> Any:  # noqa: ANN401 — the client protocol's context manager
        import contextlib  # noqa: PLC0415

        return contextlib.nullcontext(self)

    def chat(self, *, tools: list[dict[str, Any]] | None = None, **_kw: Any) -> ChatResult:  # noqa: ANN401
        self.offered.append([t["name"] for t in tools or []])
        call = (
            ToolCall(
                call_id="c1",
                name="remove_plugin",
                arguments_json=json.dumps({"plugin_id": "echo"}),
            )
            if len(self.offered) == 1
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


@dataclass
class _Rig:
    ctx: DecideContext
    service: PluginConnections
    registry: ToolRegistry
    token: Path
    root: Path


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    """A connected echo plugin with a stored login and credential, behind the real decide loop."""
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "empty-catalog"))
    _package(tmp_path, "echo", ECHO)
    root = tmp_path / "runtime"
    paths = _Paths(event_log=root / "events.db", artifacts_root=root / "artifacts")
    paths.artifacts_root.mkdir(parents=True)
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    service = PluginConnections(
        repo_root=tmp_path,
        runtime_root=root,
        event_log=paths.event_log,
        registry=registry,
        config={"tools": {"mcp": {"timeout_s": 3}}},
    )
    service.initialize()
    service.action("connect", {"request_id": _open(service, "echo")})
    _wait(service, "ready")
    token = root / "mcp" / "echo.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"tokens": {"access_token": "at"}}))
    service.settings.save_credentials("echo", {"ECHO_KEY": "secret"})
    repo = Path(__file__).parent.parent.parent
    ctx = DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", _RemovingClient()),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(repo / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
            confirmation_dispatch_outbox=True,
        ),
    )
    yield _Rig(ctx, service, registry, token, root)
    ctx.conn.close()
    service.stop()


def _stored(rig: _Rig) -> bool:
    saved = json.loads((rig.root / "plugin-credentials.json").read_text())
    return rig.token.exists() or "echo" in saved


def test_remove_plugin_asks_first_and_on_yes_deletes_the_login_and_the_credentials(
    rig: _Rig,
) -> None:
    """The card waits; his button runs it; then the token file and the saved key are gone."""
    _say(rig.ctx, "把 echo 删掉", "T1")
    asked = _rows(rig.ctx.conn, "confirmation.requested")
    assert [r["action_snapshot"]["tool_name"] for r in asked] == ["remove_plugin"]
    assert _stored(rig)
    assert rig.service.read()["plugins"][0]["status"] == "ready"

    card = asked[0]["confirmation_id"]
    _press(rig.ctx, {"confirmation_id": card, "decision": "accept"}, "T2")
    assert not rig.token.exists()
    assert "echo" not in json.loads((rig.root / "plugin-credentials.json").read_text())
    plugin = rig.service.read()["plugins"][0]
    assert (plugin["enabled"], plugin["status"], plugin["credentials_saved"]) == (
        False,
        "disabled",
        False,
    )
    assert not any(t.name.startswith("mcp__echo") for t in rig.registry.get_definitions())


def test_remove_plugin_does_nothing_on_no(rig: _Rig) -> None:
    """A rejected card leaves the login, the key and the connection as they were."""
    _say(rig.ctx, "把 echo 删掉", "T1")
    card = _rows(rig.ctx.conn, "confirmation.requested")[0]["confirmation_id"]
    _press(rig.ctx, {"confirmation_id": card, "decision": "reject"}, "T2")
    assert _stored(rig)
    assert rig.service.read()["plugins"][0]["status"] == "ready"
