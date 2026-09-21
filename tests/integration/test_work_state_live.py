"""Opt-in acceptance: real TimeSink text + DeepSeek, isolated Jarvis state.

--live-llm sends bounded app/window/OCR material from a read-only snapshot of
this machine's TimeSink database to the configured fast preset. No screenshots,
production conversation memory, writes to TimeSink, or daemon restarts occur.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.decision.llm import LLMClient
from jarvis.runtime.work_state import WorkStateService, build_analyst
from jarvis.state.work_state import current_state
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_work_state import Rig

pytestmark = pytest.mark.live_llm


def test_live_work_state_refresh_and_conversation(tmp_path: Path) -> None:  # noqa: PLR0915 — one live end-to-end acceptance.
    """A real model analyses real data through HTTP, then chooses the conversation tool."""
    config = yaml.safe_load(Path("config/jarvis.yaml").read_text())
    source = Path(config["observer"]["timesink"]["db_path"]).expanduser()
    if not source.exists():
        pytest.skip("Real TimeSink database is not available on this host")
    frozen = tmp_path / "timesink.sqlite"
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(frozen)) as dst,
    ):
        src.backup(dst)
    rig = Rig(tmp_path, timesink=frozen)
    analyst = build_analyst(config, "fast", pricing_path=None, account_cost=False)
    assert analyst is not None
    rig.service = WorkStateService(
        event_log_path=rig.fx.paths.event_log,
        memory_path=rig.memory,
        timesink_path=frozen,
        repos=(),
        analyst=analyst,
        model="fast",
    )

    async def refresh() -> dict[str, Any]:
        return await asyncio.to_thread(rig.service.refresh_in_own_connection)

    def read() -> dict[str, Any]:
        with closing(sqlite3.connect(rig.fx.paths.event_log)) as conn:
            return rig.service.read(conn)

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        work_state_read=read,
        work_state_refresh=refresh,
    )
    try:
        with TestClient(create_app(deps)) as http:
            assert http.get("/inherent/work-state").json()["state"] is None
            response = http.post("/inherent/work-state/refresh")
            assert response.status_code == 200
            view = response.json()
            assert view["outcome"] == "analyzed", view.get("error")
            assert view["state"]["activities"] or view["state"]["now"]
            assert view["freshness"]["latest_observed_at"]
            assert http.post("/inherent/work-state/refresh").json()["outcome"] == "reused"

        client = LLMClient(config["llm"])
        allowed = {
            "refresh_work_state",
            "query_activity",
            "read_activity",
            "search_records",
            "read_records",
        }
        catalog = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in rig.tools
            if t.name in allowed
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": "我今天主要做了什么?"}]
        invoked: list[str] = []
        system = "你是 Jarvis。使用工具调查用户的工作状态。有证据才回答。缺少数据时明确说明。"
        for _ in range(6):
            answer = client.chat(system=system, messages=messages, tools=catalog)
            calls = answer.tool_calls
            if not calls:
                assert answer.text
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": answer.text or "",
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": c.arguments_json,
                            },
                        }
                        for c in calls
                    ],
                }
            )
            for call in calls:
                assert call.name in allowed
                invoked.append(call.name)
                result = rig.call(call.name, json.loads(call.arguments_json))
                assert "code" not in result, result
                assert result.get("truncated") is not True
                if call.name == "refresh_work_state":
                    assert result["outcome"] == "analyzed", result.get("error")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        else:
            pytest.fail("Conversation did not finish within six model rounds")
        assert "refresh_work_state" in invoked
        state = current_state(rig.fx.conn)
        assert state is not None
        assert state["version"] == 2
        assert state["question"]
        assert rig.service.read(rig.fx.conn)["state"] == state
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "dashboard_outcome": view["outcome"],
                    "conversation_tools": invoked,
                    "version": state["version"],
                    "coverage": state["evidence"]["coverage"],
                    "evidence_counts": state["evidence"]["counts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        rig.fx.close()
