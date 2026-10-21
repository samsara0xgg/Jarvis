"""Real model opens the connection UI, then uses a newly connected MCP tool."""

from pathlib import Path

import pytest

from jarvis.decision.llm import LLMClient, load_llm_config
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.deployment import load_env_file
from jarvis.runtime import JarvisRuntime
from jarvis.runtime.turn.drive import drive_turn
from jarvis.runtime.turn.start import new_turn_id
from jarvis.state.event_log import get_event
from jarvis.surface.cli import emit_surface_user_intent
from tests.support.mcp import ECHO
from tests.support.plugin_connections import make_service, wait_for_request, write_package
from tests.support.runtime_config import runtime_config
from tests.support.tool_world import ToolWorld


@pytest.mark.live_llm
def test_conversation_opens_plugin_and_resumes_with_live_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the production decision loop, cloud model and a real isolated MCP process."""
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "catalog"))
    load_env_file(Path.home() / ".jarvis")
    write_package(tmp_path, "echo", ECHO)
    fx = ToolWorld(tmp_path / "runtime", tools=())
    service = make_service(tmp_path, fx)
    llm_config = load_llm_config(Path("config/jarvis.yaml"))
    runtime = JarvisRuntime(
        config=runtime_config(root=tmp_path / "runtime"),
        runtime_paths=fx.paths,
        conn=fx.conn,
        tool_registry=fx.registry,
        lifecycle=fx.lifecycle,
        llm_client=LLMClient(llm_config),
        system_prompt=Path("prompts/jarvis_v1.md").read_text(),
        llm_session_factory=LLMSessionFactory(llm_config),
        plugin_connections=service,
    )
    try:
        asked = emit_surface_user_intent(
            runtime.conn,
            transcript="请用 echo 插件原样回显这段文字: resonance-plugin-live-accepted",
            turn_id=new_turn_id(),
        )
        first = drive_turn(runtime, user_intent_event=asked, max_iterations=8)
        request = service.read()["request"]
        assert request, first.response_text
        assert request["plugin_id"] == "echo", first.response_text
        assert request["state"] == "offered"
        assert request["continue_task"] is True
        assert not any(t.name.startswith("mcp__") for t in fx.registry.get_definitions())
        service.action("connect", {"request_id": request["id"]})
        assert wait_for_request(service, "ready")["resume_status"] == "continued"
        resumed = get_event(fx.conn, f"plugin-resume-{request['id']}")
        assert resumed is not None
        second = drive_turn(runtime, user_intent_event=resumed, max_iterations=8)
        outputs = fx.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'action.result_observed'"
        ).fetchall()
        assert any("resonance-plugin-live-accepted" in row[0] for row in outputs)
        assert "resonance-plugin-live-accepted" in second.response_text
    finally:
        service.stop()
        fx.close()
