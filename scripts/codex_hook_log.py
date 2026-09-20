#!/usr/bin/env python3
"""Log-only Codex hook: append the payload as one JSONL line, POST it to Jarvis, print nothing.

Codex (`~/.codex/hooks.json`) pipes the hook payload as JSON on stdin and
reads stdout for a decision; an empty stdout means "no decision"
(`codex-rs/hooks/src/engine/output_parser.rs`), so this script never
writes to stdout. Every payload lands in
``~/.jarvis/codex-hooks/<hook_event_name>.jsonl`` with a UTC timestamp,
one line per event, and is then POSTed to the daemon's
``/inherent/codex-hook`` (port ``JARVIS_INHERENT_BRIDGE_PORT``, default
8006) so the Resonance Codex card sees it; a daemon that is away is
ignored. Installed copy: ``~/.jarvis/codex-hooks/log_hook.py``
(ADR 0019 step 4 — the listener that lets Jarvis see what Allen's
own ChatGPT.app sessions do).
"""

import contextlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

LOG_DIR = Path.home() / ".jarvis" / "codex-hooks"
PORT = os.environ.get("JARVIS_INHERENT_BRIDGE_PORT", "8006")


def main() -> int:
    """Append stdin to the per-event log, then hand it to the daemon."""
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        event = {"raw": raw}
    name = event.get("hook_event_name") if isinstance(event, dict) else None
    target = LOG_DIR / f"{name if isinstance(name, str) and name else 'unknown'}.jsonl"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event},
        ensure_ascii=False,
    )
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if isinstance(event, dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/inherent/codex-hook",
            data=json.dumps(event).encode(),
            headers={"content-type": "application/json"},
        )
        with contextlib.suppress(OSError):  # daemon away; the JSONL line is the record
            urllib.request.urlopen(req, timeout=1).close()  # noqa: S310 — loopback only
    return 0


if __name__ == "__main__":
    sys.exit(main())
