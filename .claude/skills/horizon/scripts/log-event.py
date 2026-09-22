#!/usr/bin/env python3
"""Append one high-signal event.  usage: log-event.py <type> "<summary>" """

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402

TYPES = {"user_feedback", "report", "decision", "drift", "failure", "rotation"}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in TYPES:
        print(f"usage: log-event.py <{'|'.join(sorted(TYPES))}> \"<summary>\"", file=sys.stderr)
        return 2
    root = _harness.harness_dir()
    sid = _harness.session_id()
    _harness.append_jsonl(
        root / "events.jsonl",
        {
            "ts": _harness.now(),
            "type": sys.argv[1],
            "session": sid,
            "role": _harness.role_label(_harness.meta(root, sid)),
            "summary": " ".join(sys.argv[2:]),
        },
    )
    print("logged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
