"""H12 — gate function bodies mention the MUST-check primitives.

Per ADR 0001 § Acceptance criterion H12:

> AST scan: each gate module / function (``pre_action``,
> ``result_interpreter``, ``pre_emit``) contains the MUST-check
> primitives from § Gate contracts. Checked by presence of identifiers
> (``caller_principal``, ``risk_level``, ``result_semantics``,
> ``evidence`` / ``claim``) inside the function body. This is a soft
> canary — easily satisfied syntactically — but flags obvious no-op
> gate implementations.

Modules scanned:

- ``jarvis/decision/gates.py``: ``pre_action_gate`` + ``pre_emit_gate``.
- ``jarvis/decision/result_interpreter.py``: ``result_interpreter``.

For each :class:`ast.FunctionDef`, collect every ``Name(id=...)``,
``Attribute(attr=...)``, and string-literal token inside the function
body. Then assert the required primitives appear.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root


def _find_function_def(
    module: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate the (first) FunctionDef / AsyncFunctionDef with this name."""
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _identifier_tokens(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return every identifier-like token inside ``fn``.

    Includes:
    - ``Name.id`` (variable references).
    - ``Attribute.attr`` (attribute names).
    - String-literal contents (so reason strings count too).
    - Argument names (so ``def f(caller_principal: ...)`` counts).
    """
    tokens: set[str] = set()
    for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
        tokens.add(arg.arg)
    if fn.args.vararg is not None:
        tokens.add(fn.args.vararg.arg)
    if fn.args.kwarg is not None:
        tokens.add(fn.args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, ast.arg):
            tokens.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens.add(node.value)
    return tokens


def _assert_any_present(
    fn_name: str,
    tokens: set[str],
    required_any: tuple[tuple[str, ...], ...],
    file_label: str,
) -> list[str]:
    """For each tuple in ``required_any``, fail if NONE of its members appear.

    ``required_any`` is a list of OR-groups: each group is satisfied if
    at least one of its tokens (substring-match on identifier names AND
    on string-literal contents) is present in ``tokens``.
    """
    return [
        f"{file_label} `{fn_name}`: missing any of {group!r} "
        "(MUST-check identifier not referenced)"
        for group in required_any
        if not _any_token_match(group, tokens)
    ]


def _any_token_match(group: tuple[str, ...], tokens: set[str]) -> bool:
    """True iff any name in ``group`` matches an identifier in ``tokens``.

    The match is substring: for example ``"claim"`` matches the
    identifier ``"claim_id"`` and the string-literal token
    ``"claim.created"``. Substring is the right granularity for H12 (a
    soft canary checking the primitive is REFERENCED, not how).
    """
    for member in group:
        for token in tokens:
            if member in token:
                return True
    return False


def test_pre_action_gate_contains_must_check_primitives() -> None:
    """``pre_action_gate`` references caller_principal, risk_level, entity / lease."""
    gates_path = repo_root() / "jarvis" / "decision" / "gates.py"
    module = parse(gates_path)
    fn = _find_function_def(module, "pre_action_gate")
    assert fn is not None, "pre_action_gate function not found in jarvis/decision/gates.py"
    tokens = _identifier_tokens(fn)

    violations = _assert_any_present(
        "pre_action_gate",
        tokens,
        required_any=(
            ("caller_principal",),
            ("risk_level",),
            ("target_entity_ref", "entity", "lease", "authorization_lease"),
        ),
        file_label="jarvis/decision/gates.py",
    )
    assert not violations, (
        "H12 — pre_action_gate missing MUST-check primitives:\n  "
        + "\n  ".join(violations)
    )


def test_pre_emit_gate_contains_must_check_primitives() -> None:
    """``pre_emit_gate`` references claim / evidence and permission / limitation."""
    gates_path = repo_root() / "jarvis" / "decision" / "gates.py"
    module = parse(gates_path)
    fn = _find_function_def(module, "pre_emit_gate")
    assert fn is not None, "pre_emit_gate function not found in jarvis/decision/gates.py"
    tokens = _identifier_tokens(fn)

    violations = _assert_any_present(
        "pre_emit_gate",
        tokens,
        required_any=(
            ("claim", "evidence"),
            ("permission", "allow_completion_language", "force_limitation_language"),
        ),
        file_label="jarvis/decision/gates.py",
    )
    assert not violations, (
        "H12 — pre_emit_gate missing MUST-check primitives:\n  "
        + "\n  ".join(violations)
    )


def test_result_interpreter_contains_must_check_primitives() -> None:
    """``result_interpreter`` references semantics, claim, and evidence."""
    interp_path = repo_root() / "jarvis" / "decision" / "result_interpreter.py"
    module = parse(interp_path)
    fn = _find_function_def(module, "result_interpreter")
    assert fn is not None, (
        "result_interpreter function not found in jarvis/decision/result_interpreter.py"
    )
    tokens = _identifier_tokens(fn)

    violations = _assert_any_present(
        "result_interpreter",
        tokens,
        required_any=(
            ("semantics", "result_semantics"),
            ("claim", "Claim", "claim_id"),
            ("evidence", "Evidence", "evidence_id"),
        ),
        file_label="jarvis/decision/result_interpreter.py",
    )
    assert not violations, (
        "H12 — result_interpreter missing MUST-check primitives:\n  "
        + "\n  ".join(violations)
    )
