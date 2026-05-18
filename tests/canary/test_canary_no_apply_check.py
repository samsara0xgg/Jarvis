"""Canary — no ``git apply --check`` anywhere under L3 / L4.

Per ADR-0002 Step 11 Tier-1 canary list (lines 1643-1645):

    ``test_canary_no_apply_check`` — AST scan: no string
    ``"git apply --check"`` (or equivalent) appears under
    ``jarvis/execution/`` or ``jarvis/decision/``.

Rationale: Day-2 ``verify_diff`` reads the worker's diff artifact as
text and (optionally) runs the bound task's ``verify_command``. Codex
already applied the patch in-place upstream (``spawn_worker`` writes
to the live working tree), so a ``git apply --check`` pass would be
redundant at best and a tautology at worst. This canary keeps any
future "extra safety" patch from quietly re-introducing the check at
the **code-level**.

Scope: only **non-docstring** string literals count. Module and
function docstrings legitimately mention ``git apply --check`` to
explain why the check is intentionally absent (e.g. in
``diff_capture.py``'s module docstring); those references are
documentation and must not trip the canary. Only call arguments / list
elements / format strings — i.e. literals that could become a real
subprocess command — are scanned.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

# Forbidden substrings. The bare ``apply --check`` catch covers any
# future shorthand (e.g. ``git apply --check --3way``) that drops the
# ``git`` prefix; both forms are equivalent shells of the same legacy
# Day-1 idea and must not appear in executable code.
_FORBIDDEN_LITERALS: tuple[str, ...] = (
    "git apply --check",
    "apply --check",
)

# Scan scope: every ``.py`` under L3 (jarvis/decision/) and L4
# (jarvis/execution/). The canary is intentionally narrow — `tests/`
# may legitimately reference the string in docstrings explaining why
# the canary exists.
_SCAN_SUBPACKAGES: tuple[str, ...] = ("jarvis/decision", "jarvis/execution")


def _collect_docstring_nodes(module: ast.Module) -> set[int]:
    """Return the ``id()`` of every ``ast.Constant`` node that is a docstring.

    Docstrings live as the FIRST statement inside a module, ClassDef,
    FunctionDef, or AsyncFunctionDef body, when that statement is an
    ``ast.Expr`` whose value is an ``ast.Constant`` string. Skipping
    them lets the canary scan executable string literals only.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(module):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        body = node.body
        if not body:
            continue
        head = body[0]
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            docstring_ids.add(id(head.value))
    return docstring_ids


def _collect_executable_string_literals(module: ast.Module) -> list[str]:
    """Return every non-docstring ``str`` constant in ``module``."""
    skip_ids = _collect_docstring_nodes(module)
    return [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip_ids
    ]


def test_canary_no_apply_check_under_l3_and_l4() -> None:
    """No executable string literal under L3 / L4 contains ``apply --check``."""
    offenses: list[str] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        if not any(rel.startswith(prefix) for prefix in _SCAN_SUBPACKAGES):
            continue
        for literal in _collect_executable_string_literals(parse(path)):
            offenses.extend(
                f"{rel}: {forbidden!r} in literal {literal!r}"
                for forbidden in _FORBIDDEN_LITERALS
                if forbidden in literal
            )

    assert not offenses, (
        "ADR-0002 Step 11: no `git apply --check` literal is allowed in "
        "executable code under jarvis/decision/ or jarvis/execution/. "
        "Codex applies in place and Day-2 verify_diff reads the diff "
        "artifact directly. Docstrings may reference the phrase; only "
        "executable literals trip this canary. Offending literals:\n  "
        + "\n  ".join(offenses)
    )
