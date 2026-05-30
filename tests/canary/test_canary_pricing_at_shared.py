"""Pricing-at-shared canary: pricing imports never reference jarvis.state.

Per ADR-0002 § Module map and § Reference sources, the pricing module is
lifted to ``jarvis.shared.pricing`` (not ``jarvis.state.pricing``) so that
both L3 (``jarvis.decision``) and L4 (``jarvis.execution``) may import it
without crossing the layer DAG enforced by ``.importlinter``.

This canary AST-scans every ``.py`` file under ``jarvis/`` and asserts ZERO
imports of the form:

- ``from jarvis.state import pricing``
- ``from jarvis.state.pricing import ...``
- ``import jarvis.state.pricing``

A hit means somebody put pricing back under ``jarvis.state`` (or wired
through that path), which would re-introduce the L3/L4-cross-layer
violation ADR-0002 explicitly designs around.

Scope note: uses :func:`iter_jarvis_py_files` (not ``iter_all_py_files``)
because pricing is consumed inside the ``jarvis`` package; importing it
from ``tests/`` is not what this canary guards. Equally, ``iter_all_py_files``
silently no-ops under ``.claude/worktrees/...`` paths (dot-prefix filter at
the helper), which would mask a real violation.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo


def _imports_pricing_from_state(node: ast.AST) -> bool:
    """Return True iff ``node`` is a state.pricing import in any of three shapes."""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        # `from jarvis.state import pricing` — module is jarvis.state, alias is pricing.
        if module == "jarvis.state":
            return any(alias.name == "pricing" for alias in node.names)
        # `from jarvis.state.pricing import ...` — module is jarvis.state.pricing.
        return module == "jarvis.state.pricing" or module.startswith("jarvis.state.pricing.")
    if isinstance(node, ast.Import):
        # `import jarvis.state.pricing` (with or without `as` alias).
        return any(
            alias.name == "jarvis.state.pricing"
            or alias.name.startswith("jarvis.state.pricing.")
            for alias in node.names
        )
    return False


def _format_violation(rel: str, lineno: int) -> str:
    """Render one violation message."""
    return (
        f"{rel}:{lineno}: imports pricing from jarvis.state — pricing lives at "
        "jarvis.shared.pricing per ADR-0002 § Module map (so L3 + L4 can both "
        "import without crossing the layer DAG)"
    )


def test_canary_pricing_at_shared() -> None:
    """Fail if any ``.py`` under ``jarvis/`` imports pricing from L2."""
    violations: list[str] = [
        _format_violation(relative_to_repo(path), getattr(node, "lineno", 0))
        for path in iter_jarvis_py_files()
        for node in ast.walk(parse(path))
        if _imports_pricing_from_state(node)
    ]

    assert not violations, (
        "pricing-at-shared canary — pricing must live at jarvis.shared.pricing, "
        "not jarvis.state.pricing:\n  " + "\n  ".join(violations)
    )
