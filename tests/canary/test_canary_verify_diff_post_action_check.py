"""Canary — ``VERIFY_DIFF_TOOL_DEF.post_action_check`` is declared statically.

Per ADR-0002 Step 11 Tier-1 canary list (lines 1677-1684):

    ``test_canary_verify_diff_post_action_check`` — AST scan:
    ``jarvis/execution/tools.py`` defines ``VERIFY_DIFF_TOOL_DEF``
    (the ToolDefinition for ``verify_diff``) with a ``post_action_check``
    field whose ``result_semantics_on_match`` literal equals
    ``"verification"`` and whose ``mode`` literal equals ``"inline"``;
    verifies the spec §3.5.7 binding is present at the
    ToolDefinition level so the dual-slot RawResult path is wired
    statically rather than implicitly.

The canary walks the module AST, locates the
``VERIFY_DIFF_TOOL_DEF = ToolDefinition(...)`` assignment, asserts the
``post_action_check`` kwarg is a Call to ``PostActionCheck(...)``, and
inspects its kwargs for the two literal strings the spec pins. This
guards against:

* the ToolDefinition being removed or renamed,
* the ``post_action_check`` kwarg being dropped or set to ``None``,
* either spec-pinned kwarg being mutated away from its literal value.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_TARGET_NAME: str = "VERIFY_DIFF_TOOL_DEF"
_REQUIRED_KWARG_VALUES: dict[str, str] = {
    "mode": "inline",
    "result_semantics_on_match": "verification",
}


def _find_module_level_assignment(
    module: ast.Module, *, name: str
) -> ast.Assign | ast.AnnAssign | None:
    """Return the first top-level ``name = ...`` (or ``name: T = ...``) assignment."""
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                return node
    return None


def _extract_call(value: ast.expr) -> ast.Call | None:
    """Return ``value`` if it is a Call; else None."""
    return value if isinstance(value, ast.Call) else None


def _kwarg_value(call: ast.Call, *, name: str) -> ast.expr | None:
    """Return the keyword value bound to ``name`` on ``call``, or None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def test_canary_verify_diff_tool_def_assignment_exists() -> None:
    """A module-level ``VERIFY_DIFF_TOOL_DEF = ToolDefinition(...)`` exists."""
    path = repo_root() / "jarvis" / "execution" / "tools.py"
    module = parse(path)
    assignment = _find_module_level_assignment(module, name=_TARGET_NAME)
    assert assignment is not None, (
        f"jarvis/execution/tools.py must define a module-level "
        f"{_TARGET_NAME!r} = ToolDefinition(...) binding (ADR-0002 Step 11) "
        "so this canary and the L3 Result Interpreter can reference the "
        "ToolDefinition statically."
    )
    call = _extract_call(assignment.value)
    assert call is not None
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "ToolDefinition", (
        f"{_TARGET_NAME} must be assigned a `ToolDefinition(...)` call; "
        f"got {ast.unparse(call.func)!r}"
    )


def test_canary_verify_diff_post_action_check_kwargs() -> None:
    """``post_action_check=PostActionCheck(mode="inline", ...)`` is declared."""
    path = repo_root() / "jarvis" / "execution" / "tools.py"
    module = parse(path)
    assignment = _find_module_level_assignment(module, name=_TARGET_NAME)
    assert assignment is not None
    call = _extract_call(assignment.value)
    assert call is not None

    post_action_check_value = _kwarg_value(call, name="post_action_check")
    assert post_action_check_value is not None, (
        f"{_TARGET_NAME} must pass `post_action_check=...` to ToolDefinition "
        "(ADR-0002 § Verify_diff contract spec §3.5.7)."
    )

    inner_call = _extract_call(post_action_check_value)
    assert inner_call is not None, (
        "post_action_check kwarg must be a Call expression "
        "(PostActionCheck(...)); literal None or other forms break the "
        "static guard."
    )
    assert isinstance(inner_call.func, ast.Name)
    assert inner_call.func.id == "PostActionCheck", (
        f"post_action_check value must be `PostActionCheck(...)`; got "
        f"{ast.unparse(inner_call.func)!r}"
    )

    for kwarg_name, expected_literal in _REQUIRED_KWARG_VALUES.items():
        kwarg_value = _kwarg_value(inner_call, name=kwarg_name)
        assert kwarg_value is not None, (
            f"PostActionCheck(...) must declare `{kwarg_name}=...` "
            f"(ADR-0002 spec §3.5.7 pins {kwarg_name}={expected_literal!r})."
        )
        assert isinstance(kwarg_value, ast.Constant), (
            f"PostActionCheck(...).{kwarg_name} must be a literal string "
            f"constant; got {ast.unparse(kwarg_value)!r}."
        )
        assert kwarg_value.value == expected_literal, (
            f"PostActionCheck(...).{kwarg_name} must be the literal string "
            f"{expected_literal!r}; got {kwarg_value.value!r}."
        )
