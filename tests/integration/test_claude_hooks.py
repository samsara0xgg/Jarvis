"""Claude Code hook scenarios end to end (ADR 0049).

The real ``scripts/claude_hook.py`` talks to the daemon over HTTP while a
stand-in companion reads ``/inherent/claude-sessions`` and answers through
``/inherent/claude-requests``; ``claude agents --json`` is a fake binary.
The daemon requires the local key, which the hook reads from the runtime root.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn

from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import claude_hooks, claude_sessions
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "claude_hook.py"
SUGGESTION = {
    "type": "addRules",
    "rules": [{"toolName": "Bash", "ruleContent": "npm run build:*"}],
    "behavior": "allow",
    "destination": "localSettings",
}
QUESTIONS = [
    {
        "question": "Which layout?",
        "header": "Layout",
        "multiSelect": False,
        "options": [
            {"label": "One column", "description": ""},
            {"label": "Two", "description": ""},
        ],
    }
]


def _noop(text: str) -> None:
    del text


class _Rig:
    """A fake Claude Code home, the daemon on a real port, and a companion's HTTP client."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'agents.json'}'\n")
        fake.chmod(0o755)
        self.transcript = tmp_path / ".claude" / "projects" / "-x" / "s1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "last-prompt", "lastPrompt": "build it"}))
        self.status("waiting")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(claude_sessions, "REFRESH_S", 0.0)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = str(sock.getsockname()[1])
        self.root = tmp_path / ".jarvis"
        key = local_key(self.root)
        app = create_app(InherentDeps(
            submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True
        ))
        require_local_key(app, functools.partial(local_key_matches, key))
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=int(self.port), log_level="warning")
        )
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.02)
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
        )

    def status(self, status: str) -> None:
        agent = {
            "sessionId": "s1",
            "kind": "interactive",
            "status": status,
            "name": "build",
            "cwd": "/x/jarvis",
            "startedAt": 1,
        }
        (self.tmp / "agents.json").write_text(json.dumps([agent]))

    def row(self) -> dict[str, Any]:
        rows = self.http.get("/inherent/claude-sessions").json()["sessions"]
        return next(r for r in rows if r["session_id"] == "s1")

    def hook(self, event: str, **fields: object) -> subprocess.Popen[str]:
        payload = {"hook_event_name": event, "session_id": "s1", "cwd": "/x/jarvis", **fields}
        proc = subprocess.Popen(  # noqa: S603 — the repo's own hook script
            [sys.executable, str(HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "JARVIS_INHERENT_BRIDGE_PORT": self.port,
                "JARVIS_RUNTIME_ROOT": str(self.root),
            },
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        return proc

    def held(self) -> dict[str, Any]:
        """The prompt as the companion sees it on the session's row."""
        for _ in range(100):
            request = self.row()["request"]
            if request:
                return dict(request)
            time.sleep(0.05)
        msg = "the prompt never reached the board"
        raise AssertionError(msg)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        yield rig
    finally:
        rig.server.should_exit = True


def _decision(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    out = proc.stdout.read()
    proc.wait(timeout=10)
    return dict(json.loads(out)["hookSpecificOutput"]["decision"]) if out else {}


def test_no_companion_reading_leaves_the_prompt_to_claude_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody has read the board: the hook answers at once with no decision."""
    for rig in _rig(tmp_path, monkeypatch):
        started = time.monotonic()
        proc = rig.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
        assert _decision(proc) == {}
        assert time.monotonic() - started < 5


def test_answers_from_the_card_become_the_hook_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer on the card becomes the decision the hook prints.

    Don't-ask-again echoes Claude Code's suggestion; a question comes back with
    its answers; a plan kept in planning is a deny carrying what to change.
    """
    for rig in _rig(tmp_path, monkeypatch):
        rig.row()  # the companion is reading
        cases: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = [
            (
                "Bash",
                {"command": "npm run build", "description": "Build"},
                {"decision": "always"},
                {"behavior": "allow", "updatedPermissions": [SUGGESTION]},
            ),
            (
                "AskUserQuestion",
                {"questions": QUESTIONS},
                {"decision": "allow", "answers": {"Which layout?": "One column"}},
                {
                    "behavior": "allow",
                    "updatedInput": {
                        "questions": QUESTIONS,
                        "answers": {"Which layout?": "One column"},
                    },
                },
            ),
            (
                "ExitPlanMode",
                {"plan": "# Plan\n1. marks"},
                {"decision": "deny", "message": "Do the wing first."},
                {"behavior": "deny", "message": "Do the wing first."},
            ),
        ]
        for tool, tool_input, answer, expected in cases:
            proc = rig.hook(
                "PermissionRequest",
                tool_name=tool,
                tool_input=tool_input,
                permission_suggestions=[SUGGESTION],
            )
            request = rig.held()
            assert (request["tool"], request["input"], request["cwd"]) == (
                tool,
                tool_input,
                "/x/jarvis",
            )
            assert request["always"] == "Don't ask again for Bash(npm run build:*)"
            assert rig.row()["phase"] == "needs_input"
            assert rig.http.post(
                f"/inherent/claude-requests/{request['id']}", json=answer
            ).json() == {"ok": True}
            assert _decision(proc) == expected, tool
            assert rig.row()["request"] is None


def test_a_prompt_answered_in_the_terminal_leaves_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session writes on after the prompt came in: Jarvis lets go; a late answer is refused."""
    for rig in _rig(tmp_path, monkeypatch):
        rig.row()
        proc = rig.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
        request = rig.held()
        rig.status("busy")
        later = time.time() + 10
        os.utime(rig.transcript, (later, later))
        assert rig.row()["request"] is None
        assert _decision(proc) == {}
        assert (
            rig.http.post(
                f"/inherent/claude-requests/{request['id']}", json={"decision": "allow"}
            ).status_code
            == 404
        )


def test_compacting_and_stopped_marks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compacting lasts from PreCompact to PostCompact; a stop lasts until work resumes."""
    for rig in _rig(tmp_path, monkeypatch):
        rig.status("busy")
        rig.hook("PreCompact", trigger="auto").wait(timeout=5)
        assert rig.row()["compacting"] is True
        rig.hook("PostCompact", trigger="auto", compact_summary="…").wait(timeout=5)
        assert rig.row()["compacting"] is False
        rig.status("idle")
        rig.hook("StopFailure", error="rate_limit", error_details="429 Too Many Requests").wait(
            timeout=5
        )
        assert rig.row()["error"] == "Rate limited: 429 Too Many Requests"
        rig.status("busy")
        later = time.time() + 10
        os.utime(rig.transcript, (later, later))
        assert rig.row()["error"] == ""


def test_a_companion_that_stops_reading_lets_the_prompt_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The companion quits while a prompt is held: no decision; Claude Code's dialog decides."""
    monkeypatch.setattr(claude_hooks, "LISTENER_S", 1.0)
    for rig in _rig(tmp_path, monkeypatch):
        rig.row()
        started = time.monotonic()
        proc = rig.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
        assert _decision(proc) == {}
        assert 1.0 <= time.monotonic() - started < 5


def test_a_prompt_from_a_session_off_the_board_is_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``claude -p`` run is never on the board, so no card could answer it: no decision."""
    for rig in _rig(tmp_path, monkeypatch):
        rig.row()
        proc = rig.hook("PermissionRequest", session_id="p-run", tool_name="Bash", tool_input={})
        for _ in range(80):
            rig.row()
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert _decision(proc) == {}
