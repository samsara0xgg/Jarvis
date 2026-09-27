"""Claude Code state-to-dashboard scenarios over HTTP; a fake `claude` and home."""

from __future__ import annotations

import json
import os
import sys
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
    assert (bg["job_id"], inter["job_id"]) == ("job1", "")  # what `claude attach` takes


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


IDLE, BUSY = "1a2b3c4d-0000-4000-8000-000000000001", "1a2b3c4d-0000-4000-8000-000000000002"


def _said(content: object, **extra: object) -> dict[str, object]:
    return {"type": "user", "message": {"content": content}, **extra}


def _answer(*texts: str, **extra: object) -> dict[str, object]:
    blocks = [{"type": "text", "text": t} for t in texts]
    return {"type": "assistant", "message": {"content": blocks}, **extra}


def test_island_page_reads_the_conversation_and_types_a_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0068: each turn's final answer only; a reply lands through a hidden attach."""
    session = {"kind": "background", "cwd": "/x", "startedAt": NOW_MS}
    (tmp_path / "agents.json").write_text(
        json.dumps(
            [
                {**session, "sessionId": IDLE, "id": "job1", "status": "idle", "name": "overlay"},
                {**session, "sessionId": BUSY, "id": "job2", "status": "busy", "name": "audit"},
            ]
        )
    )
    project = tmp_path / ".claude" / "projects" / "-x"
    project.mkdir(parents=True)
    transcript = project / f"{IDLE}.jsonl"
    tool = {"type": "tool_use", "name": "Read", "input": {}}
    entries = [
        _said("continue"),
        _said("fix the overlay"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}, tool]}},
        _said([{"type": "tool_result", "content": "..."}]),
        _answer("a subagent's words", isSidechain=True),
        _answer("**Fixed.**\n- one line"),
        _said("<task-notification>done</task-notification>"),
        _said("Base directory for this skill", isMeta=True),
        _said('<pasted_content id="1">long log</pasted_content id="1"> why this?'),
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries))
    (project / f"{BUSY}.jsonl").write_text(json.dumps(_said("audit it")))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    # `claude attach` stand-in: the line typed into its terminal lands in the session's record.
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"if sys.argv[1] == 'agents': print(open({str(tmp_path / 'agents.json')!r}).read())\n"
        "elif sys.argv[1] == 'attach':\n"
        "    line = sys.stdin.readline().strip()\n"
        "    if line == 'continue': sys.exit()\n"
        f"    open({str(transcript)!r}, 'a').write('\\n' + json.dumps("
        "{'type': 'user', 'message': {'content': line}}))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    client = TestClient(
        create_app(InherentDeps(
            submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True
        ))
    )
    reply = f"/inherent/claude-sessions/{IDLE}/reply"

    rows = {r["session_id"]: r for r in client.get("/inherent/claude-sessions").json()["sessions"]}
    page = client.get(f"/inherent/claude-sessions/{IDLE}/conversation").json()["messages"]
    busy = client.post(f"/inherent/claude-sessions/{BUSY}/reply", json={"text": "hi"})
    blank = client.post(reply, json={"text": " "})
    sent = client.post(reply, json={"text": "ship  it"})
    # Typed but never landed: an older line that reads the same does not count.
    lost = client.post(reply, json={"text": "continue"})
    after = client.get(f"/inherent/claude-sessions/{IDLE}/conversation").json()["messages"]

    assert (rows[IDLE]["replyable"], rows[BUSY]["replyable"]) == (True, False)
    assert page == [
        {"who": "you", "text": "continue"},
        {"who": "you", "text": "fix the overlay"},
        {"who": "it", "text": "**Fixed.**\n- one line"},
        {"who": "you", "text": "[Pasted text] why this?"},
    ]
    assert client.get("/inherent/claude-sessions/nope/conversation").status_code == 404
    assert (busy.status_code, blank.status_code, sent.json()) == (409, 400, {"ok": True})
    assert lost.status_code == 502
    assert after[-1] == {"who": "you", "text": "ship it"}


def test_agent_marks_are_one_file_every_surface_shares(tmp_path: Path) -> None:
    """ADR 0067: unread, parked and archived per session, kept across a restart."""
    path = tmp_path / "agent-marks.json"

    def app() -> TestClient:
        return TestClient(
            create_app(InherentDeps(
                submit_callable=_noop, broadcaster=InherentBroadcaster(), agent_marks_path=path
            ))
        )

    client = app()
    client.post("/inherent/agent-marks/a", json={"unread": True, "park": False, "archive": False})
    client.post("/inherent/agent-marks/b", json={"unread": False, "park": True, "archive": False})
    client.post("/inherent/agent-marks/c", json={"archive": True})
    client.post("/inherent/agent-marks/a", json={"seen": True})
    marks = app().get("/inherent/agent-marks").json()["marks"]

    assert (marks["a"]["unread"], marks["a"]["seen_ms"] > 0) == (False, True)
    assert (marks["b"]["parked_ms"] > 0, marks["b"].get("archived_ms")) == (True, None)
    assert marks["c"]["archived_ms"] > 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"
