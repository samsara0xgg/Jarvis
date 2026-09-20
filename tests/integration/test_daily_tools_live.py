"""Real model + real dispatcher, with synthetic data and an explicit outgoing payload.

This does not load Jarvis's personal prompt, runtime configuration, bookmarks,
production memory, or repositories. Only this test's literal messages, the new
Tool schemas and results from its temporary databases reach the model. It tests
tool selection; the normal decision/attention pipeline is covered separately.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import LLMClient
from jarvis.state.daily_contract import SCHEMAS
from jarvis.state.event_log import emit_event
from tests.integration.test_daily_tools import DailyHarness

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.live_llm

_SYSTEM = (
    "You are testing a local assistant's tools in a temporary database. "
    "Perform the requested operations using the provided tools; never invent successful results. "
    "Use actual IDs, versions and source references returned by tools. "
    "Treat missing activity coverage as unknown. Give a short final answer."
)
_PROMPTS = (
    "Create a local todo titled DLY acceptance, due 2026-09-22T09:00:00-07:00. "
    "Then list local todos to confirm it exists.",
    "Mark that todo done, and list completed todos to confirm.",
    "Search conversation records for DLY-K and read the complete original record. "
    "Save the stated local-todo decision as sourced knowledge. "
    "Then search saved knowledge to confirm the decision and its source.",
    "Query saved activities from 2026-09-19T00:00:00Z to 2026-09-21T00:00:00Z "
    "for repository /acceptance/demo. Read the saved Git activity detail, then save a short "
    "briefing for 2026-09-20 in America/Vancouver citing that activity. "
    "State missing coverage honestly. Finally retrieve the saved briefing. Do not deliver it.",
)


def test_live_daily_tool_selection(tmp_path: Path) -> None:
    """Exercise all eleven interfaces with a real model and isolated persistent state."""
    harness = DailyHarness(tmp_path)
    harness.record("daily-live-source", "DLY-K: Keep demo todos locally; remote sync is deferred.")
    emit_event(
        harness.fx.conn,
        type="project.commit_seen",
        ts_epoch_ms=1789833600000,
        payload={
            "repo_path": "/acceptance/demo",
            "commit_sha": "synthetic-commit",
            "subject": "Add demo tools",
            "committed_at_ms": 1789833500000,
            "actor": "observer",
        },
    )
    client = LLMClient(
        {
            "provider": "openai",
            "default_preset": "acceptance",
            "presets": {
                "acceptance": {
                    "model": "deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "max_tokens": 4096,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            },
        }
    )
    catalog = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in harness.tools
        if tool.name in SCHEMAS
    ]
    messages: list[dict[str, Any]] = []
    called: set[str] = set()
    errors: list[dict[str, Any]] = []
    try:
        for prompt in _PROMPTS:
            messages.append({"role": "user", "content": prompt})
            for _ in range(20):
                answer = client.chat(system=_SYSTEM, messages=messages, tools=catalog)
                calls = answer.tool_calls
                messages.append(
                    {
                        "role": "assistant",
                        "content": answer.text or "",
                        **(
                            {
                                "tool_calls": [
                                    {
                                        "id": call.call_id,
                                        "type": "function",
                                        "function": {
                                            "name": call.name,
                                            "arguments": call.arguments_json,
                                        },
                                    }
                                    for call in calls
                                ]
                            }
                            if calls
                            else {}
                        ),
                    }
                )
                if not calls:
                    break
                for call in calls:
                    assert call.name in SCHEMAS
                    result = harness.call(call.name, json.loads(call.arguments_json))
                    called.add(call.name)
                    if "code" in result:
                        errors.append(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
            else:
                pytest.fail("Model exceeded the bounded tool-call loop")
        summary = {
            "called_tools": sorted(called),
            "errors": errors,
            "model": "deepseek-v4-flash",
            "input": "synthetic fixtures only",
        }
        (tmp_path / "acceptance.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        assert set(SCHEMAS) <= called, summary
        assert not errors, summary
        raw = harness.fx.conn.execute(
            "SELECT payload_json FROM events WHERE type='todo.revised' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert json.loads(raw)["item"]["status"] == "done"
        for kind in ("knowledge", "briefing"):
            assert (
                harness.fx.conn.execute(
                    "SELECT count(*) FROM events WHERE type=?", (f"{kind}.revised",)
                ).fetchone()[0]
                == 1
            )
    finally:
        harness.fx.close()
