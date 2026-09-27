#!/usr/bin/env python3
"""Save the context snapshot and forward stdin to the real status line.

settings.json:  "command": "python3 <this file> -- <original command...>"
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _harness


def snapshot(raw: bytes) -> None:
    """Save the latest context and token usage from a status-line payload."""
    data = json.loads(raw)
    sid = data.get("session_id") or pathlib.Path(data.get("transcript_path", "")).stem
    if not sid:
        return
    root = _harness.harness_dir(data.get("cwd"))
    cw = data.get("context_window") or {}
    usage = cw.get("current_usage") or {}
    cost = data.get("cost") or {}
    _harness.write_json(
        root / "context" / f"{sid}.json",
        {
            "session": sid,
            "ts": _harness.now(),
            "used_pct": cw.get("used_percentage"),
            "context_window_size": cw.get("context_window_size"),
            "input_tokens": usage.get("input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": cost.get("total_cost_usd"),
            "transcript_path": data.get("transcript_path"),
        },
    )


def main() -> int:
    """Best-effort snapshot without disrupting the configured status command."""
    raw = sys.stdin.buffer.read()
    with contextlib.suppress(Exception):  # snapshot failure must never hide the real status line
        snapshot(raw)
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        return 0
    return subprocess.run(args, input=raw, check=False).returncode  # noqa: S603 - operator-configured argv


if __name__ == "__main__":
    sys.exit(main())
