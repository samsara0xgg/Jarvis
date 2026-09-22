"""Hook-to-dashboard wire scenarios; no LLM or external Codex instance needed."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app


def _noop(text: str) -> None:
    del text


def test_activity_retention_and_turn_identity_over_http() -> None:
    """New work never evicts running work; archive tokens change only per turn."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    with patch("jarvis.surface.inherent_server.time.time", return_value=1000):
        for index in range(12):
            assert client.post("/inherent/codex-hook", json={
                "session_id": str(index), "hook_event_name": "UserPromptSubmit",
                "prompt": "scenario", "cwd": "/test",
            }).status_code == 200
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        assert len(rows) == 12
        original = next(r for r in rows if r["session_id"] == "0")["turn_started_ms"]
    with patch("jarvis.surface.inherent_server.time.time", return_value=1001):
        for event in ["PermissionRequest", "PostToolUse", "Stop", "SessionEnd"]:
            client.post("/inherent/codex-hook", json={
                "session_id": "0", "hook_event_name": event,
                "last_assistant_message": "done", "tool_name": "shell",
            })
        row = next(r for r in client.get("/inherent/codex-sessions").json()["sessions"]
                   if r["session_id"] == "0")
        assert row["state"] == "finished"
        assert row["last_message"] == "done"
        assert row["turn_started_ms"] == original
    with patch("jarvis.surface.inherent_server.time.time", return_value=1002):
        client.post("/inherent/codex-hook", json={
            "session_id": "0", "hook_event_name": "UserPromptSubmit", "prompt": "scenario",
        })
        row = next(r for r in client.get("/inherent/codex-sessions").json()["sessions"]
                   if r["session_id"] == "0")
        assert row["turn_started_ms"] > original
        client.post("/inherent/codex-hook", json={
            "session_id": "0", "hook_event_name": "Stop",
        })
    with patch("jarvis.surface.inherent_server.time.time", return_value=1002 + 86400):
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        assert len(rows) == 11
        assert all(r["state"] == "running" for r in rows)
        client.post("/inherent/codex-hook", json={
            "session_id": "unexpected", "hook_event_name": "Unknown",
        })
        assert len(client.get("/inherent/codex-sessions").json()["sessions"]) == 11
