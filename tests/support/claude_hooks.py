"""The Claude Code hook script and a suggestion payload."""

from __future__ import annotations

from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "claude_hook.py"


SUGGESTION = {
    "type": "addRules",
    "rules": [{"toolName": "Bash", "ruleContent": "npm run build:*"}],
    "behavior": "allow",
    "destination": "localSettings",
}
