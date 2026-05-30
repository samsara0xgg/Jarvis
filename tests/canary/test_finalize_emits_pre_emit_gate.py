"""H3.b — Pre-emit Gate verdicts must emit ``gate.evaluated(pre_emit)``.

Per ADR 0001 § Gate contracts + spec § Cross-Layer Invariant 1 (State
flows through events): every Pre-emit Gate verdict that produces a
``ResponsePlan`` must also produce a durable ``gate.evaluated`` event on
the Event Log. H3.a (``test_pre_emit_required.py``) covers the
Surface-side runtime check — ``write_output`` rejects stale or missing
tokens. H3.b is the L3-side companion: AST scan asserts that
``_finalize_response`` either directly emits, or invokes a helper that
emits, at least one ``emit_event(...)`` call where
``type="gate.evaluated"`` and the payload literal carries
``gate="pre_emit"``. Without this guard a future refactor could remove
the emit and Tier 1 would still pass — the gate verdict would silently
vanish from the audit trail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import jarvis.decision as decision_pkg
from tests.canary._helpers import parse


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    """Return the top-level FunctionDef with ``name``, or None."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _is_emit_event_call(call: ast.Call) -> bool:
    """Match calls of the form ``emit_event(...)`` (bare name or attribute)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "emit_event"
    if isinstance(func, ast.Attribute):
        return func.attr == "emit_event"
    return False


def _keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    """Return the AST value for keyword argument ``name``, or None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_string_literal(node: ast.expr | None, expected: str) -> bool:
    """True iff ``node`` is a ``Constant`` string equal to ``expected``."""
    return isinstance(node, ast.Constant) and node.value == expected


def _payload_has(payload: ast.expr | None, key: str, value: str) -> bool:
    """True iff ``payload`` is a dict literal containing ``key=value``."""
    if not isinstance(payload, ast.Dict):
        return False
    for k, v in zip(payload.keys, payload.values, strict=False):
        if (
            isinstance(k, ast.Constant)
            and k.value == key
            and isinstance(v, ast.Constant)
            and v.value == value
        ):
            return True
    return False


def _function_body_has_pre_emit_gate_emit(fn: ast.FunctionDef) -> bool:
    """True iff ``fn``'s body contains a matching gate.evaluated emit."""
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call) or not _is_emit_event_call(sub):
            continue
        type_kw = _keyword_value(sub, "type")
        payload_kw = _keyword_value(sub, "payload")
        if _is_string_literal(type_kw, "gate.evaluated") and _payload_has(
            payload_kw,
            "gate",
            "pre_emit",
        ):
            return True
    return False


def _called_name_set(fn: ast.FunctionDef) -> set[str]:
    """Return every bare-name callee invoked inside ``fn``."""
    names: set[str] = set()
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            names.add(sub.func.id)
    return names


def test_finalize_response_emits_pre_emit_gate() -> None:
    """``_finalize_response`` must emit (or transitively emit) Pre-emit Gate verdicts."""
    tree = parse(Path(decision_pkg.__file__))
    finalize_fn = _find_function(tree, "_finalize_response")
    assert finalize_fn is not None, "_finalize_response not found in jarvis.decision module"

    # Collect every top-level function whose body contains the matching emit.
    emit_helpers: set[str] = set()
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.FunctionDef) and _function_body_has_pre_emit_gate_emit(child):
            emit_helpers.add(child.name)

    assert emit_helpers, (
        "H3.b — no function in jarvis.decision contains "
        "emit_event(type='gate.evaluated', payload={'gate': 'pre_emit', ...})."
    )

    # Direct emit inside _finalize_response satisfies the contract.
    if _function_body_has_pre_emit_gate_emit(finalize_fn):
        return

    # Otherwise the contract is satisfied via a helper invocation.
    called = _called_name_set(finalize_fn)
    transitive = called & (emit_helpers - {"_finalize_response"})
    assert transitive, (
        "H3.b — _finalize_response must either emit "
        "gate.evaluated(pre_emit) directly OR call a helper that does so "
        f"(emit-capable helpers found: {sorted(emit_helpers)!r}). "
        "Without this emit the Pre-emit Gate verdict vanishes from the audit "
        "trail and only Tier 2 scenario tests would catch the regression."
    )
