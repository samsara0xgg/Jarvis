r"""H11 — Task Ledger ``status`` is derived, never stored.

Per ADR 0001 § Acceptance criterion H11:

> AST scan of ``jarvis/state/projections.py``: no assignment statement
> targeting a ``status`` field on Task Ledger rows (e.g.
> ``task["status"] = ...``, ``task.status = ...``, SQL
> ``UPDATE ... SET status = ...``) exists outside the body of
> ``derive_status()``. Status is computed, not written.

Implementation:

- Open ``jarvis/state/projections.py`` and ``ast.parse`` it.
- Walk every :class:`ast.Assign` / :class:`ast.AugAssign` /
  :class:`ast.AnnAssign`. For each, fail if the target is
  ``Subscript(value=..., slice=Constant("status"))`` or
  ``Attribute(value=..., attr="status")``.
- Walk every :class:`ast.Constant` for SQL strings containing
  ``UPDATE \w+ SET status``.
- Exclude any node whose AST ancestor is a ``FunctionDef`` named
  ``derive_status`` — that function's body is the legitimate consumer.
"""

from __future__ import annotations

import ast
import re

from tests.canary._helpers import parse, repo_root

_DERIVE_STATUS_FN_NAMES: frozenset[str] = frozenset({"derive_status", "_derive_status"})

_UPDATE_STATUS_RE = re.compile(
    r"\bUPDATE\s+\w+\s+SET\s+status\b",
    re.IGNORECASE,
)


def _function_descendants(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Return the ``id()`` set of every AST node inside ``fn`` (inclusive)."""
    return {id(n) for n in ast.walk(fn)}


def _targets_status_field(target: ast.AST) -> bool:
    """True if ``target`` writes a ``status`` attribute or subscript."""
    if isinstance(target, ast.Attribute) and target.attr == "status":
        return True
    if isinstance(target, ast.Subscript):
        slice_node = target.slice
        # Python 3.9+ stores constants directly on `slice` (no Index wrapper).
        if isinstance(slice_node, ast.Constant) and slice_node.value == "status":
            return True
    # Tuple-unpacking on the LHS: `task["status"], task["other"] = ...`
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_targets_status_field(elt) for elt in target.elts)
    return False


def _derive_status_descendants(module: ast.Module) -> set[int]:
    """Collect node ids inside every ``derive_status`` function in ``module``.

    There may be both the public ``derive_status`` (method on TaskLedger
    / TaskLedgerSnapshot) and the module-level helper ``_derive_status``;
    both are legitimate consumers.
    """
    out: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name in _DERIVE_STATUS_FN_NAMES
        ):
            out.update(_function_descendants(node))
    return out


def _assignment_targets(node: ast.AST) -> list[ast.AST]:
    """Return LHS targets for an assignment-like node, or ``[]`` otherwise."""
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return [node.target]
    return []


def _assignment_violations(
    module: ast.Module, derive_status_ids: set[int]
) -> list[str]:
    """Return one message per ``status = ...`` assignment outside derive_status."""
    out: list[str] = []
    for node in ast.walk(module):
        if id(node) in derive_status_ids:
            continue
        if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        # `node` is now a stmt; ``lineno`` is in the stmt mixin.
        out.extend(
            f"jarvis/state/projections.py:{node.lineno}: "
            "assignment to a `status` field on Task Ledger rows "
            "outside derive_status()"
            for target in _assignment_targets(node)
            if _targets_status_field(target)
        )
    return out


def _sql_string_violations(
    module: ast.Module, derive_status_ids: set[int]
) -> list[str]:
    """Return one message per ``UPDATE ... SET status`` string outside derive_status."""
    out: list[str] = []
    for node in ast.walk(module):
        if id(node) in derive_status_ids:
            continue
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if not isinstance(value, str):
            continue
        if _UPDATE_STATUS_RE.search(value):
            out.append(
                f"jarvis/state/projections.py:{node.lineno}: "
                "SQL update of `status` outside derive_status() body"
            )
    return out


def test_no_status_field_writes_outside_derive_status() -> None:
    """No ``status = ...`` on Task Ledger rows outside ``derive_status``."""
    path = repo_root() / "jarvis" / "state" / "projections.py"
    module = parse(path)
    derive_status_ids = _derive_status_descendants(module)

    violations: list[str] = []
    violations.extend(_assignment_violations(module, derive_status_ids))
    violations.extend(_sql_string_violations(module, derive_status_ids))

    assert not violations, (
        "H11 — Task Ledger status was written instead of derived:\n  "
        + "\n  ".join(violations)
    )
