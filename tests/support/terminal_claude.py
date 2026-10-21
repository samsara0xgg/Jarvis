"""The Mac's Claude Code as the terminal sees it: a fake claude and its transcripts."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import TYPE_CHECKING

from jarvis.surface import claude_sessions

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

NOW_MS = int(time.time() * 1000)


IDLE = "1a2b3c4d-0000-4000-8000-000000000001"


BUSY = "1a2b3c4d-0000-4000-8000-000000000002"


WAITING = "1a2b3c4d-0000-4000-8000-000000000003"


ASKING = "Fixed it. Want me to ship it?"


def _said(content: object, *, at: str | None = "2026-10-07T10:00:00+00:00") -> dict[str, object]:
    return {"type": "user", "message": {"content": content}, "timestamp": at}


def _answer(text: str) -> dict[str, object]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def device_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Mac's Claude Code: a fake ``claude`` (agents and attach) and the transcripts."""
    tmp_path.mkdir()
    session = {"kind": "background", "cwd": "/x/jarvis", "startedAt": NOW_MS}
    (tmp_path / "agents.json").write_text(
        json.dumps(
            [
                {**session, "sessionId": IDLE, "id": "job1", "status": "idle", "name": "overlay"},
                {**session, "sessionId": BUSY, "id": "job2", "status": "busy", "name": "audit"},
                {
                    "kind": "interactive",
                    "sessionId": WAITING,
                    "status": "waiting",
                    "name": "build",
                    "cwd": "/x/jarvis",
                    "startedAt": NOW_MS - 1000,
                    "pid": os.getpid(),
                },
            ]
        )
    )
    project = tmp_path / ".claude" / "projects" / "-x"
    project.mkdir(parents=True)
    transcript = project / f"{IDLE}.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                _said("fix the overlay"),
                _answer("Looking."),
                _answer(ASKING),
            ]
        )
    )
    (project / f"{BUSY}.jsonl").write_text(json.dumps(_said("audit it")))
    (project / f"{WAITING}.jsonl").write_text(
        json.dumps({"type": "last-prompt", "lastPrompt": "build it"})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"if sys.argv[1] == 'agents': print(open({str(tmp_path / 'agents.json')!r}).read())\n"
        "elif sys.argv[1] == 'attach':\n"
        "    line = sys.stdin.readline().strip()\n"
        f"    open({str(transcript)!r}, 'a').write('\\n' + json.dumps("
        "{'type': 'user', 'message': {'content': line}}))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(claude_sessions, "REFRESH_S", 0.0)
    return transcript
