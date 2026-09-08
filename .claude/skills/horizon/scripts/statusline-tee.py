#!/usr/bin/env python3
"""statusLine wrapper: save the latest context snapshot, then run the real
status line command with the same stdin.

settings.json:  "command": "python3 <this file> -- <original command...>"
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402


def snapshot(raw: bytes) -> None:
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
    raw = sys.stdin.buffer.read()
    try:
        snapshot(raw)
    except Exception:  # never break the status line
        pass
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        return 0
    return subprocess.run(args, input=raw).returncode


if __name__ == "__main__":
    sys.exit(main())
