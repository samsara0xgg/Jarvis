"""Hook-to-dashboard wire scenarios; no LLM or external Codex instance needed."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

from fastapi.testclient import TestClient

from jarvis.shared.lang import t
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path


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


def _rollout_line(
    path: Path, at_s: float, kind: str, *, entry_type: str = "event_msg", **payload: object,
) -> None:
    """Append one rollout line the way Codex writes it (compact JSON, UTC ISO time)."""
    stamp = datetime.fromtimestamp(at_s, UTC).isoformat(timespec="milliseconds")
    entry: dict[str, object] = {
        "timestamp": stamp.replace("+00:00", "Z"), "type": entry_type,
        "payload": {"type": kind, **payload},
    }
    if kind == "turn_context":
        entry.update(type="turn_context", payload=payload)
    with path.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def test_rollout_settles_what_hooks_miss(tmp_path: Path) -> None:
    """Auto-reviewed approvals never ask Allen; a turn ended without a Stop leaves running."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    auto, mine = tmp_path / "auto.jsonl", tmp_path / "mine.jsonl"
    for path, reviewer in ((auto, "auto_review"), (mine, "user")):
        _rollout_line(path, 999, "turn_context", turn_id="t1", approvals_reviewer=reviewer)
        _rollout_line(path, 999, "task_started", turn_id="t1")

    def hook(sid: str, path: Path, event: str, **extra: object) -> None:
        client.post("/inherent/codex-hook", json={
            "session_id": sid, "hook_event_name": event, "transcript_path": str(path),
            "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "git push"}, **extra,
        })

    def row(sid: str) -> dict[str, object]:
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        return next(r for r in rows if r["session_id"] == sid)

    with patch("jarvis.surface.inherent_server.time.time", return_value=1000):
        hook("auto", auto, "PermissionRequest")
        hook("mine", mine, "PermissionRequest")
        assert row("auto")["state"] == "running"
        assert row("mine")["state"] == "needs_input"
    # No Stop arrives: an interrupt, or a Stop handler Codex skips.
    _rollout_line(auto, 1001, "task_complete", turn_id="t1", last_agent_message="Pushed.")
    _rollout_line(mine, 1001, "turn_aborted", turn_id="t1", reason="interrupted")
    with patch("jarvis.surface.inherent_server.time.time", return_value=1002):
        assert (row("auto")["state"], row("auto")["last_message"]) == ("finished", "Pushed.")
        assert (row("mine")["state"], row("mine")["detail"]) == ("idle", t("codex.turn_stopped"))
        # A new prompt is not closed by the end of the turn before it.
        hook("auto", auto, "UserPromptSubmit", prompt="next")
        assert row("auto")["state"] == "running"


def test_codex_question_waits_on_allen_until_he_writes(tmp_path: Path) -> None:
    """Codex asks and works on: the row asks until Allen sends anything, past the turn's end."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    path = tmp_path / "rollout.jsonl"
    _rollout_line(path, 999, "task_started", turn_id="t1")
    asked = t("codex.asks", question="Web prototype or SwiftUI?")

    def hook(event: str, **extra: object) -> None:
        client.post("/inherent/codex-hook", json={
            "session_id": "s", "hook_event_name": event, "transcript_path": str(path),
            "turn_id": "t1", "tool_name": "Bash", **extra,
        })

    def row() -> dict[str, object]:
        return dict(client.get("/inherent/codex-sessions").json()["sessions"][0])

    with patch("jarvis.surface.inherent_server.time.time", return_value=1000):
        hook("UserPromptSubmit", prompt="polish the app")
    _rollout_line(
        path, 1001, "function_call", entry_type="response_item", call_id="c1",
        name="request_user_input_async",
        arguments=json.dumps({"questions": [{"title": "Web prototype or SwiftUI?"}]}),
    )
    with patch("jarvis.surface.inherent_server.time.time", return_value=1002):
        assert (row()["state"], row()["detail"]) == ("needs_input", asked)
        # Codex keeps working; its tool calls neither answer nor hide the question,
        # not even once the call has scrolled out of the rollout's tail.
        hook("PostToolUse")
        _rollout_line(path, 1002, "function_call_output", entry_type="response_item",
                      output="x" * 300_000)
        assert (row()["state"], row()["detail"]) == ("needs_input", asked)
    _rollout_line(path, 1003, "task_complete", turn_id="t1", last_agent_message="Went with web.")
    with patch("jarvis.surface.inherent_server.time.time", return_value=1004):
        assert (row()["state"], row()["last_message"]) == ("needs_input", "Went with web.")
    # His answer, as Codex records any message he sends in the thread.
    _rollout_line(path, 1005, "task_started", turn_id="t2")
    _rollout_line(path, 1005, "item_completed", item={"type": "UserMessage"})
    with patch("jarvis.surface.inherent_server.time.time", return_value=1006):
        assert (row()["state"], row()["detail"]) == ("running", "")
