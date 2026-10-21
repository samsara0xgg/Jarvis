"""A fake Claude Code home: its agents listing and two sessions' files."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

NOW_MS = int(time.time() * 1000)


def noop(text: str) -> None:
    del text


def board_home(tmp_path: Path, agents: list[dict[str, object]]) -> Path:
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
