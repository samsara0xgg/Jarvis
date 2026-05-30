"""Canary - every ``ResponsePlan(...)`` construction populates the spec §3.4.13 fields.

Per ADR-0002 Step 12 canary list (lines 1708-1712):

    ``test_canary_response_plan_carries_gate_mode`` - AST scan: every
    ``ResponsePlan(...)`` instantiation under ``jarvis/decision/`` sets
    both ``output_risk_class`` and ``required_gate_mode`` keyword
    arguments (or the equivalent dataclass field assignment); ensures
    spec §3.4.13 fields are populated rather than defaulted.

The canary scans every ``.py`` file under ``jarvis/decision/`` and
flags any ``ResponsePlan(...)`` call expression that omits either
kwarg. The default value path (``"routine"`` / ``"sentence"``) is
allowed, but the kwarg must be PRESENT - the field cannot rely on the
default for production sites because spec §3.4.13 mandates the gate
chooses the class explicitly.

Test fixtures under ``tests/`` are out of scope: they may or may not
provide the kwargs depending on what behavior they exercise; the
production rule lives at ``jarvis/decision/``.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from pathlib import Path

_REQUIRED_KWARGS: frozenset[str] = frozenset(
    ("output_risk_class", "required_gate_mode"),
)


def _decision_py_files() -> list[Path]:
    """Yield every production ``.py`` under ``jarvis/decision/``."""
    decision_dir = repo_root() / "jarvis" / "decision"
    return [
        path
        for path in sorted(decision_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _is_response_plan_call(call: ast.Call) -> bool:
    """True iff ``call`` is ``ResponsePlan(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "ResponsePlan"
    if isinstance(func, ast.Attribute):
        return func.attr == "ResponsePlan"
    return False


def _kwarg_names(call: ast.Call) -> set[str]:
    """Return the set of keyword-argument names on ``call``."""
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def test_canary_response_plan_carries_gate_mode() -> None:
    """Every ResponsePlan(...) site under jarvis/decision/ sets the two fields."""
    violations: list[str] = []
    for path in _decision_py_files():
        module = parse(path)
        rel = relative_to_repo(path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not _is_response_plan_call(node):
                continue
            present = _kwarg_names(node)
            missing = _REQUIRED_KWARGS - present
            if missing:
                violations.append(
                    f"{rel}:{node.lineno}: ResponsePlan(...) missing kwarg(s): "
                    f"{sorted(missing)} - spec §3.4.13 / ADR-0002 § ResponsePlan "
                    "schema extension requires output_risk_class + "
                    "required_gate_mode to be populated explicitly"
                )

    assert not violations, (
        "ResponsePlan output_risk_class + required_gate_mode canary - "
        "every construction site under jarvis/decision/ must set both:\n  "
        + "\n  ".join(violations)
    )
