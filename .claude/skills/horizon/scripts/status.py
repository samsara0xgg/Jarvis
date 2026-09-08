#!/usr/bin/env python3
"""One line: who am I, how full is my context, do I still hold the lease."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402

PREPARE, HARD = 60, 75


def main() -> int:
    root = _harness.harness_dir()
    sid = _harness.session_id()
    m = _harness.meta(root, sid)
    if not m:
        print(f"session={sid} not bootstrapped; run bootstrap.py first")
        return 1
    key = "hub" if m["role"] == "hub" else f"lane-{m['lane']}"
    lease_path = root / "lease" / key
    holder = lease_path.read_text().split()[0] if lease_path.exists() else None
    lease = "held" if holder == sid else ("lost" if holder else "none")
    pct = _harness.context_snapshot(root, sid).get("used_pct")
    inbox = len([p for p in (root / "inbox").iterdir() if p.is_file()])
    line = f"session={sid} role={_harness.role_label(m)} context={pct if pct is not None else '?'}% lease={lease} inbox={inbox}"
    if pct is not None and pct >= HARD:
        line += "  -> HARD threshold: checkpoint and rotate now"
    elif pct is not None and pct >= PREPARE:
        line += "  -> prepare threshold: rotate at the next atomic boundary"
    if lease == "lost":
        line += "  -> lease lost: end your turn"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
