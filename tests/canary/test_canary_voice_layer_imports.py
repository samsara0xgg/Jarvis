"""ADR-0005 §11 canary: jarvis/surface/voice_*.py do not name forbidden layers.

`lint-imports` (gate 3) already enforces this at the package level; this
canary makes the failure surface inside pytest with a per-file diagnostic
so the autonomous loop can see exactly which file violated.
"""
from __future__ import annotations

import ast

from tests.canary._helpers import repo_root

_FORBIDDEN_PREFIXES = (
    "jarvis.decision",
    "jarvis.execution",
    "jarvis.deployment",
    "jarvis.runtime",
    "jarvis.cli",
)


def test_canary_voice_layer_imports() -> None:
    """voice_*.py must not import from forbidden L3/L4/L6 layers."""
    surface_dir = repo_root() / "jarvis" / "surface"
    violations: list[str] = []
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str]
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            else:
                continue
            violations.extend(
                f"{path}:{node.lineno} imports {mod}"
                for mod in mods
                if any(mod.startswith(p) for p in _FORBIDDEN_PREFIXES)
            )
    assert not violations, (
        "L5 voice modules name forbidden layers: " + "\n  ".join(violations)
    )
