"""cost-recorded-L3-only canary: L4 never emits ``cost.recorded``.

Per spec §5.4.1 the ``cost.recorded`` event has ``owner_layer=L3``. L4
returns cost data inside ``RawResult.metadata["cost"]`` (see ADR-0002 §
RawResult.metadata extension) and L3's Result Interpreter is the sole
emit-site. If any module under ``jarvis/execution/`` ever calls
``emit_event(..., type="cost.recorded", ...)``, it has violated the
owner_layer contract — that emit must move to L3.

This canary AST-scans every ``.py`` file under ``jarvis/execution/`` and
asserts ZERO ``emit_event`` calls with ``type="cost.recorded"``.

Scope note: only ``jarvis/execution/`` is scanned. Other layers
(``jarvis.decision`` / ``jarvis.runtime``) are governed by the
companion canary ``test_canary_cost_recorded_emitted_per_llm_call`` (L3
emit-presence) and by ``test_layer_ownership_boundaries`` (broader
owner_layer enforcement); this canary is the L4 negative guard.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from pathlib import Path


def _iter_execution_py_files() -> list[Path]:
    """Return every ``.py`` path under ``jarvis/execution/`` (recursive)."""
    execution_dir = repo_root() / "jarvis" / "execution"
    return sorted(execution_dir.rglob("*.py"))


def _is_emit_event_call(node: ast.AST) -> bool:
    """True iff ``node`` is a call to a function named ``emit_event``.

    Accepts bare ``emit_event(...)`` and attribute-style ``mod.emit_event(...)``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "emit_event"
    if isinstance(func, ast.Attribute):
        return func.attr == "emit_event"
    return False


def _type_kwarg_is_cost_recorded(call: ast.Call) -> bool:
    """True iff one of ``call.keywords`` is ``type="cost.recorded"``."""
    for kw in call.keywords:
        if kw.arg != "type":
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and value.value == "cost.recorded":
            return True
    return False


def _format_violation(rel: str, lineno: int) -> str:
    """Render one violation message."""
    return (
        f'{rel}:{lineno}: emit_event(..., type="cost.recorded", ...) '
        "in jarvis/execution/ — cost.recorded.owner_layer == L3 per spec "
        "§5.4.1. L4 returns cost in RawResult.metadata['cost']; only L3 "
        "(jarvis.decision) emits the event."
    )


def test_canary_cost_recorded_l3_only() -> None:
    """Fail if any module under jarvis/execution/ emits cost.recorded."""
    violations: list[str] = []
    for path in _iter_execution_py_files():
        if "__pycache__" in path.parts:
            continue
        module = parse(path)
        rel = relative_to_repo(path)
        violations.extend(
            _format_violation(rel, getattr(node, "lineno", 0))
            for node in ast.walk(module)
            if _is_emit_event_call(node) and _type_kwarg_is_cost_recorded(node)
        )

    assert not violations, (
        "cost-recorded-L3-only canary — L4 must NOT emit cost.recorded; "
        "cost data travels via RawResult.metadata['cost'] and L3 emits:\n  "
        + "\n  ".join(violations)
    )
