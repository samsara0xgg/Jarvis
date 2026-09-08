#!/usr/bin/env python3
"""Append this generation's one-line session log: measured context +
mechanical meta + the semantic summary given on the command line."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True, choices=["planned_rotation", "complete", "blocked"])
    ap.add_argument("--outcome", required=True, help="what this generation completed, one line")
    ap.add_argument("--successor", default=None, help="e.g. hub-g9; omit when --end complete")
    ap.add_argument("--mix", default=None, help="context_mix_estimate, free text, an estimate")
    ap.add_argument("--drift", default=None, help="card-external work done and why, or none")
    ap.add_argument("--feedback", action="append", default=[], help="extra user feedback, one sentence each")
    a = ap.parse_args()

    root = _harness.harness_dir()
    sid = _harness.session_id()
    m = _harness.meta(root, sid)
    if not m:
        print("not bootstrapped; nothing to log", file=sys.stderr)
        return 1
    sessions = root / "sessions.jsonl"
    if any(e.get("session") == sid for e in _harness.read_jsonl(sessions)):
        print("already logged for this session", file=sys.stderr)
        return 1

    snap = _harness.context_snapshot(root, sid)
    feedback = [
        e["summary"] for e in _harness.read_jsonl(root / "events.jsonl")
        if e.get("session") == sid and e.get("type") == "user_feedback"
    ] + a.feedback

    entry = {
        "ts": _harness.now(),
        "session": sid,
        "logical_role": _harness.role_label(m),
        "generation": m.get("generation"),
        "predecessor": m.get("predecessor"),
        "end": a.end,
        "context_used_pct": snap.get("used_pct"),
        "context_tokens": {
            "input": snap.get("input_tokens"),
            "cache_create": snap.get("cache_creation_input_tokens"),
            "cache_read": snap.get("cache_read_input_tokens"),
            "output": snap.get("output_tokens"),
        },
        "cost_usd": snap.get("cost_usd"),
        "context_mix_estimate": a.mix,
        "user_feedback": feedback,
        "drift": a.drift,
        "outcome": a.outcome,
        "successor": a.successor,
        "transcript_path": snap.get("transcript_path"),
    }
    _harness.append_jsonl(sessions, entry)
    if a.end == "planned_rotation":
        _harness.append_jsonl(
            root / "events.jsonl",
            {"ts": entry["ts"], "type": "rotation", "session": sid, "role": entry["logical_role"],
             "summary": f"planned rotation -> {a.successor or '?'}"},
        )
    print(f"logged {entry['logical_role']} end={a.end} context={entry['context_used_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
