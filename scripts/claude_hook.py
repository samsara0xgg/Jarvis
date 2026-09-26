#!/usr/bin/env python3
"""Claude Code hook: hand the event to Jarvis, print Jarvis's decision (ADR 0049).

Claude Code (``~/.claude/settings.json``) pipes the hook payload as JSON on
stdin and reads stdout for a decision. For ``PermissionRequest`` this waits
while Jarvis holds the prompt for the companion's notice card and prints the
decision it sends back; for ``PreCompact`` / ``PostCompact`` /
``StopFailure`` it only reports. Empty stdout is "no decision": a daemon
that is away, or a prompt nobody answered here, leaves Claude Code's own
dialog in charge. Port ``JARVIS_INHERENT_BRIDGE_PORT``, default 8006; the
daemon's key comes from ``plugin-access.json`` under ``JARVIS_RUNTIME_ROOT``
(default ``~/.jarvis``). Installed copy: ``~/.jarvis/claude-hooks/hook.py``.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

PORT = os.environ.get("JARVIS_INHERENT_BRIDGE_PORT", "8006")
ROOT = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis").expanduser()


def main() -> int:
    """POST stdin to the daemon; echo a decision if one came back."""
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return 0
    blocking = isinstance(event, dict) and event.get("hook_event_name") == "PermissionRequest"
    try:
        key = json.loads((ROOT / "plugin-access.json").read_text())["token"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/inherent/claude-hook",
            data=raw.encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        )
        # A held prompt answers when Allen does; the settings entry allows a day.
        with urllib.request.urlopen(req, timeout=86_400 if blocking else 2) as resp:  # noqa: S310 — loopback only
            body = resp.read().decode()
    except (OSError, ValueError, KeyError, TypeError):  # no key yet, or daemon away
        return 0
    if blocking and body.strip() not in {"", "{}"}:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
