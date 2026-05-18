"""Canary — ``_RUNTIME_TRIGGER_TYPES`` pins the 4-trigger set (B-0003b).

Per B-0003b, the runtime waiter must wake on:

- ``worker.reported`` (happy-path spawn_worker Timer event)
- ``action.result_observed`` (defensive — sync tools emit inline)
- ``action.timeout_assumed`` (Codex 10-min turn timeout)
- ``action.failed`` (Codex subprocess crash)

Drift away from this 4-tuple — e.g. removing the terminal-failure
events to "simplify" the waiter, or adding a Stage-2 trigger
without thinking through the deadlock implications — would silently
regress the B-0003 fix and leave decide() blind to the terminal
failure paths.

Implementation: AST-parse :mod:`jarvis.runtime.__init__`, locate the
top-level ``_RUNTIME_TRIGGER_TYPES`` assignment, and assert its
:class:`ast.Tuple` value contains exactly the four expected string
constants in the documented order.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_EXPECTED_TRIGGER_TYPES: tuple[str, ...] = (
    "worker.reported",
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
)


def _find_trigger_types_assignment(module: ast.Module) -> ast.Assign | ast.AnnAssign | None:
    """Return the top-level ``_RUNTIME_TRIGGER_TYPES = (...)`` node.

    Handles both ``ast.Assign`` (bare ``_RUNTIME_TRIGGER_TYPES = ...``)
    and ``ast.AnnAssign`` (``_RUNTIME_TRIGGER_TYPES: tuple[...] = ...``)
    — the runtime module uses the annotated form.
    """
    for node in module.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "_RUNTIME_TRIGGER_TYPES":
                return node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_RUNTIME_TRIGGER_TYPES":
                    return node
    return None


def _string_constants_in_tuple(value: ast.AST) -> tuple[str, ...]:
    """Return the literal string values inside ``value`` (an ``ast.Tuple``).

    Raises an :class:`AssertionError` when ``value`` is not a tuple of
    pure string constants — drift toward dynamic construction (e.g.
    spreading a constant or computing names) would break the static
    canary contract, so we refuse the shape.
    """
    assert isinstance(value, ast.Tuple), (
        f"_RUNTIME_TRIGGER_TYPES must be assigned an ast.Tuple literal; "
        f"got {type(value).__name__}"
    )
    out: list[str] = []
    for elt in value.elts:
        assert isinstance(elt, ast.Constant), (
            f"_RUNTIME_TRIGGER_TYPES entries must be ast.Constant nodes; "
            f"got {ast.dump(elt)!r}"
        )
        assert isinstance(elt.value, str), (
            f"_RUNTIME_TRIGGER_TYPES entries must be string literals; "
            f"got {ast.dump(elt)!r}"
        )
        out.append(elt.value)
    return tuple(out)


def test_canary_runtime_trigger_types_exact_set() -> None:
    """``_RUNTIME_TRIGGER_TYPES`` is exactly the 4-trigger B-0003b set."""
    path = repo_root() / "jarvis" / "runtime" / "__init__.py"
    module = parse(path)

    assignment = _find_trigger_types_assignment(module)
    assert assignment is not None, (
        "B-0003b: _RUNTIME_TRIGGER_TYPES assignment is missing from "
        "jarvis/runtime/__init__.py — the runtime waiter has lost its "
        "trigger set."
    )

    # ``ast.AnnAssign.value`` and ``ast.Assign.value`` share the same
    # attribute name, so a single read covers both shapes.
    value: ast.AST | None = assignment.value
    assert value is not None, (
        "_RUNTIME_TRIGGER_TYPES assignment has no value expression."
    )

    actual = _string_constants_in_tuple(value)
    assert actual == _EXPECTED_TRIGGER_TYPES, (
        "B-0003b: _RUNTIME_TRIGGER_TYPES drifted from the canonical "
        f"4-tuple. Expected {_EXPECTED_TRIGGER_TYPES!r}, got {actual!r}. "
        "Removing action.timeout_assumed / action.failed leaves the "
        "runtime waiter deadlocked on the spawn_worker terminal failure "
        "paths; adding new triggers without updating this canary risks "
        "silent regression."
    )
