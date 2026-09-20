#!/usr/bin/env python3
"""Log-only Codex hook: append the event payload as one JSONL line, print nothing.

Codex (`~/.codex/hooks.json`) pipes the hook payload as JSON on stdin and
reads stdout for a decision; an empty stdout means "no decision"
(`codex-rs/hooks/src/engine/output_parser.rs`), so this script never
writes to stdout. Every payload lands in
``~/.jarvis/codex-hooks/<hook_event_name>.jsonl`` with a UTC timestamp,
one line per event. Installed copy: ``~/.jarvis/codex-hooks/log_hook.py``
(ADR 0019 step 4 — the Stop listener that lets Jarvis see what Allen's
own ChatGPT.app sessions did).
"""

import json
import sys
import time
from pathlib import Path

LOG_DIR = Path.home() / ".jarvis" / "codex-hooks"


def main() -> int:
    """Append stdin to the per-event log; a bad payload lands in unknown.jsonl."""
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
