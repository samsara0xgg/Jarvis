#!/usr/bin/env python3
"""Log-only Codex hook: append the payload as one JSONL line, POST it to Jarvis, print nothing.

Codex (`~/.codex/hooks.json`) pipes the hook payload as JSON on stdin and
reads stdout for a decision; an empty stdout means "no decision"
(`codex-rs/hooks/src/engine/output_parser.rs`), so this script never
writes to stdout. Every payload lands in
``~/.jarvis/codex-hooks/<hook_event_name>.jsonl`` with a UTC timestamp,
one line per event (past 10 MB a file rolls over to ``.1``, ``.1`` to
``.2``, and the older ``.2`` is dropped), and is then POSTed to the daemon's
``/inherent/codex-hook`` (port ``JARVIS_INHERENT_BRIDGE_PORT``, default
8006, with the key from ``plugin-access.json`` under ``JARVIS_RUNTIME_ROOT``)
so the Resonance Codex card sees it; a daemon that is away is ignored.
Installed copy: ``~/.jarvis/codex-hooks/log_hook.py``
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
MAX_BYTES = 10 * 1024 * 1024
KEEP = 2  # rolled-over files kept beside the live one
PORT = os.environ.get("JARVIS_INHERENT_BRIDGE_PORT", "8006")
ROOT = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis").expanduser()


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
    if target.exists() and target.stat().st_size > MAX_BYTES:
        for i in range(KEEP, 1, -1):
            older = target.with_name(f"{target.name}.{i - 1}")
            if older.exists():
                older.replace(target.with_name(f"{target.name}.{i}"))
        target.replace(target.with_name(f"{target.name}.1"))
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if isinstance(event, dict):
        # No key yet or daemon away: the JSONL line is the record.
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            key = json.loads((ROOT / "plugin-access.json").read_text())["token"]
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/inherent/codex-hook",
                data=json.dumps(event).encode(),
                headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
            )
            urllib.request.urlopen(req, timeout=1).close()  # noqa: S310 — loopback only
    return 0


if __name__ == "__main__":
    sys.exit(main())
