"""reviewer-in-L3 canary: the reviewer module lives in L3, never L4.

Per ADR-0002 § Decision D12 (line 245) + § Reviewer contract (line 743):
the reviewer takes :class:`LLMClient`, which is L3-owned. Locating
``reviewer.py`` under ``jarvis/execution/`` would force L4 to import
L3 — a layer violation that ``.importlinter`` would catch at the DAG
level, but this canary catches the structural mistake one step earlier
(the file's existence is itself the regression).

The canary asserts three things, AST-only:

1. :mod:`jarvis.decision.reviewer` exists (Step 9 lands it).
2. ``jarvis/execution/reviewer.py`` does NOT exist (would be a layer
   violation by file placement).
3. No ``.py`` file under ``jarvis/execution/`` imports a reviewer symbol
   (``from jarvis.decision.reviewer import review_diff`` from L4 is also
   a layer violation; ``.importlinter`` would catch it, but this canary
   is the structural early warning).

Importing the reviewer from runtime is fine (runtime is the composition
root); importing it from tests is fine; only ``jarvis/execution/`` is
forbidden as a caller.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, relative_to_repo, repo_root


def _module_targets_reviewer(module_name: str) -> bool:
    """True iff ``module_name`` refers to the L3 reviewer module."""
    return module_name == "jarvis.decision.reviewer" or module_name.startswith(
        "jarvis.decision.reviewer."
    )


def test_canary_reviewer_module_exists_at_l3() -> None:
    """``jarvis/decision/reviewer.py`` must exist (Step 9 contract)."""
    root = repo_root()
    decision_path = root / "jarvis" / "decision" / "reviewer.py"
    assert decision_path.is_file(), (
        "ADR-0002 Step 9: jarvis/decision/reviewer.py must exist (D12 — "
        "reviewer is an L3 module because it takes the L3-owned LLMClient)"
    )


def test_canary_reviewer_module_not_at_l4() -> None:
    """``jarvis/execution/reviewer.py`` must NOT exist (D12 layer rule)."""
    root = repo_root()
    execution_path = root / "jarvis" / "execution" / "reviewer.py"
    assert not execution_path.exists(), (
        "ADR-0002 D12 violation: jarvis/execution/reviewer.py exists; the "
        "reviewer must live at L3 because it takes LLMClient (L3-owned). "
        "Move the file to jarvis/decision/reviewer.py."
    )


def test_canary_no_l4_module_imports_reviewer() -> None:
    """No ``.py`` under ``jarvis/execution/`` may import the reviewer."""
    root = repo_root()
    execution_dir = root / "jarvis" / "execution"
    violations: list[str] = []
    for path in sorted(execution_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = parse(path)
        rel = relative_to_repo(path)
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom):
                if node.module is not None and _module_targets_reviewer(node.module):
                    violations.append(
                        f"{rel}:{node.lineno}: from {node.module!r} import ..."
                    )
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{rel}:{node.lineno}: import {alias.name!r}"
                    for alias in node.names
                    if _module_targets_reviewer(alias.name)
                )

    assert not violations, (
        "ADR-0002 D12 violation: jarvis/execution/ imports the L3 reviewer; "
        "L4 must not import L3 (.importlinter enforces the layer DAG). "
        "Reviewer wiring lives in jarvis.decision (Step 12 Result Interpreter):\n  "
        + "\n  ".join(violations)
    )
