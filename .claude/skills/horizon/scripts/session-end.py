#!/usr/bin/env python3
"""SessionEnd hook: if a harness session ends without a session log, append
a mechanical fallback line. Reads the hook JSON on stdin."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    sid = data.get("session_id")
    if not sid:
        return 0
    try:
        root = _harness.harness_dir(data.get("cwd"))
    except Exception:
        return 0  # not a git repo; nothing to do
    m = _harness.meta(root, sid)
    if not m:
        return 0  # not a harness role
    sessions = root / "sessions.jsonl"
    if any(e.get("session") == sid for e in _harness.read_jsonl(sessions)):
        return 0
    snap = _harness.context_snapshot(root, sid)
    _harness.append_jsonl(
        sessions,
        {
            "ts": _harness.now(),
            "session": sid,
            "logical_role": _harness.role_label(m),
            "generation": m.get("generation"),
            "predecessor": m.get("predecessor"),
            "end": "unexpected",
            "reason": data.get("reason"),
            "context_used_pct": snap.get("used_pct"),
            "context_tokens": {
                "input": snap.get("input_tokens"),
                "cache_create": snap.get("cache_creation_input_tokens"),
                "cache_read": snap.get("cache_read_input_tokens"),
                "output": snap.get("output_tokens"),
            },
            "cost_usd": snap.get("cost_usd"),
            "semantic_summary": None,
            "transcript_path": data.get("transcript_path") or snap.get("transcript_path"),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
