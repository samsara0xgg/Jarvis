"""Real model opens the connection UI, then uses a newly connected MCP tool."""

from pathlib import Path

import pytest

from jarvis.decision.llm import LLMClient, load_llm_config
from jarvis.deployment import load_env_file
from jarvis.runtime import JarvisRuntime, drive_turn, run_turn
from jarvis.state.event_log import get_event
from tests.integration.test_flat_tool_dispatch import _Fixture
from tests.integration.test_mcp_tools import ECHO
from tests.integration.test_plugin_connections import _package, _service, _wait


@pytest.mark.live_llm
def test_conversation_opens_plugin_and_resumes_with_live_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the production decision loop, cloud model and a real isolated MCP process."""
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "catalog"))
    load_env_file(Path.home() / ".jarvis")
    _package(tmp_path, "echo", ECHO)
    fx = _Fixture(tmp_path / "runtime", tools=())
    service = _service(tmp_path, fx)
    runtime = JarvisRuntime(
        config={},
        runtime_paths=fx.paths,
        conn=fx.conn,
        tool_registry=fx.registry,
        lifecycle=fx.lifecycle,
        llm_client=LLMClient(load_llm_config(Path("config/jarvis.yaml"))),
        system_prompt=Path("prompts/jarvis_v1.md").read_text(),
        plugin_connections=service,
    )
    try:
        first = run_turn(
            runtime,
            utterance="请用 echo 插件原样回显这段文字: resonance-plugin-live-accepted",
            max_iterations=8,
        )
        request = service.read()["request"]
        assert request, first.response_text
        assert request["plugin_id"] == "echo", first.response_text
        assert request["state"] == "offered"
        assert request["continue_task"] is True
        assert not any(t.name.startswith("mcp__") for t in fx.registry.get_definitions())
        service.action("connect", {"request_id": request["id"]})
        assert _wait(service, "ready")["resume_status"] == "continued"
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
