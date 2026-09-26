"""Claude Code state-to-dashboard scenarios over HTTP; a fake `claude` and home."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

NOW_MS = int(time.time() * 1000)


def _noop(text: str) -> None:
    del text


def _home(tmp_path: Path, agents: list[dict[str, object]]) -> Path:
    """A home with one fake ``claude agents --json`` and two sessions' files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "agents.json").write_text(json.dumps(agents))
    fake = bin_dir / "claude"
    fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'agents.json'}'\n")
    fake.chmod(0o755)
    job = tmp_path / ".claude" / "jobs" / "job1"
    job.mkdir(parents=True)
    (job / "state.json").write_text(
        json.dumps(
            {
                "state": "blocked",
                "tempo": "blocked",
                "detail": "Pick A or B",
                "intent": "first ask",
            }
        )
    )
    project = tmp_path / ".claude" / "projects" / "-x-jarvis"
    project.mkdir(parents=True)
    tool_use = {
        "type": "tool_use",
        "name": "Edit",
        "input": {"file_path": "/x/jarvis/OverlayWindow.swift"},
    }
    (project / "s-inter.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"type": "last-prompt", "lastPrompt": "the overlay blocks clicks"},
                {
                    "type": "assistant",
                    "gitBranch": "fix-overlay",
                    "message": {"content": [{"type": "text", "text": "Found it."}, tool_use]},
                },
            ]
        )
    )
    (project / "s-bg.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "gitBranch": "main",
                "message": {"content": [{"type": "text", "text": "Two options."}]},
            },
        )
    )
    return bin_dir


def test_claude_sessions_rows_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live status and job state pick the phase; transcript tail fills prompt, tool, branch."""
    bin_dir = _home(
        tmp_path,
        [
            {
                "sessionId": "s-inter",
                "kind": "interactive",
                "pid": os.getpid(),
                "status": "waiting",
                "waitingFor": "dialog open",
                "name": "overlay",
                "cwd": "/x/jarvis/.claude/worktrees/fix",
                "startedAt": NOW_MS,
            },
            {
                "sessionId": "s-bg",
                "id": "job1",
                "kind": "background",
                "status": "idle",
                "state": "blocked",
                "name": "brief",
                "cwd": "/x/jarvis",
                "startedAt": NOW_MS - 1000,
            },
            {
                "sessionId": "s-old",
                "kind": "background",
                "state": "done",
                "name": "old",
                "cwd": "/x",
                "startedAt": 1,
            },
            {
                "sessionId": "s-busy-old",
                "kind": "interactive",
                "status": "busy",
                "name": "live",
                "cwd": "/x",
                "startedAt": 1,
            },
        ],
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = TestClient(
        create_app(InherentDeps(
            submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True
        ))
    )

    body = client.get("/inherent/claude-sessions").json()

    assert body["error"] is None
    rows = {r["session_id"]: r for r in body["sessions"]}
    assert set(rows) == {
        "s-inter",
        "s-bg",
        "s-busy-old",
    }  # a day-old finished job drops, a busy one stays
    inter = rows["s-inter"]
    assert (inter["phase"], inter["project"], inter["branch"]) == (
        "needs_input",
        "jarvis",
        "fix-overlay",
    )
    assert inter["prompt"] == "the overlay blocks clicks"
    assert inter["activity"] == "Edit OverlayWindow.swift"  # the pending tool, not "dialog open"
    assert inter["last_message"] == "Found it."
    bg = rows["s-bg"]
    assert (bg["phase"], bg["where"], bg["activity"], bg["prompt"]) == (
        "needs_input",
        "background",
        "Pick A or B",
        "first ask",
    )
    assert rows["s-busy-old"]["phase"] == "working"


def test_claude_sessions_missing_binary_reports_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``claude`` on PATH or in ~/.local/bin: an empty board that says why."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    client = TestClient(
        create_app(InherentDeps(
            submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True
        ))
    )

    body = client.get("/inherent/claude-sessions").json()

    assert body["sessions"] == []
    assert body["error"].startswith("claude agents --json")
