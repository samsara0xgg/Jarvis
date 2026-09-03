"""Response terminals are unwritable outside the L2 CAS primitive.

Per ADR-0008 §3 D1 and the Wave 4A plan §8.2: ``response.completed``,
``response.cancelled`` and ``response.failed`` are terminal facts whose
exactly-once guarantee comes from ``lifecycle_terminal._terminalize``'s
``BEGIN IMMEDIATE`` compare-and-set.  A plain ``emit_event`` or a bare
``append_event_in_transaction`` naming one of those types would append a
second terminal with no CAS, silently breaking F20.

Two static assertions over ``jarvis/``:

- no ``emit_event(...)`` / ``append_event_in_transaction(...)`` call may name a
  response terminal outside ``jarvis/state/lifecycle_terminal.py``;
- ``terminalize_response`` is called only from
  ``jarvis/decision/response_run.py`` — the single L3 owner.

Both are green on production code that predates Wave 4A, so this canary guards
the new lifecycle rather than documenting an existing debt.  The action-side
equivalent (``jarvis/execution/tools.py``) is Wave 4B's slice, deliberately not
asserted here so the two remain independently revertible.
"""

from __future__ import annotations

import ast
from typing import Final

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

_RESPONSE_TERMINAL_TYPES: Final[frozenset[str]] = frozenset(
    {"response.completed", "response.cancelled", "response.failed"},
)
_APPEND_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"emit_event", "append_event_in_transaction"},
)
_TERMINAL_CAS_OWNER: Final[str] = "jarvis/state/lifecycle_terminal.py"
_RESPONSE_TERMINALIZER_OWNER: Final[str] = "jarvis/decision/response_run.py"


def _callee_name(call: ast.Call) -> str | None:
    """Return the simple callee name for ``Name`` and ``Attribute`` calls."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _event_type_literals(call: ast.Call) -> list[str]:
    """Return every string literal this call binds to ``type``/``event_type``."""
    literals: list[str] = []
    for keyword in call.keywords:
        if keyword.arg not in ("type", "event_type"):
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(
            keyword.value.value,
            str,
        ):
            literals.append(keyword.value.value)
    literals.extend(
        argument.value
        for argument in call.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    )
    return literals


def test_canary_response_terminals_only_appended_by_the_cas_owner() -> None:
    """Fail if a response terminal is appended outside lifecycle_terminal.py."""
    violations: list[str] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        if rel == _TERMINAL_CAS_OWNER:
            continue
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            if _callee_name(node) not in _APPEND_FUNCTIONS:
                continue
            violations.extend(
                f"{rel}:{node.lineno}: appends {literal!r} without the terminal CAS"
                for literal in _event_type_literals(node)
                if literal in _RESPONSE_TERMINAL_TYPES
            )

    assert not violations, (
        "response-terminal-only-through-CAS canary — response.completed / "
        "response.cancelled / response.failed may only be appended by "
        f"{_TERMINAL_CAS_OWNER}:\n  " + "\n  ".join(violations)
    )


def test_canary_terminalize_response_has_one_owner() -> None:
    """Fail if any module other than the L3 owner calls terminalize_response."""
    violations: list[str] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        if rel in (_TERMINAL_CAS_OWNER, _RESPONSE_TERMINALIZER_OWNER):
            continue
        violations.extend(
            f"{rel}:{node.lineno}: calls terminalize_response"
            for node in ast.walk(parse(path))
            if isinstance(node, ast.Call)
            and _callee_name(node) == "terminalize_response"
        )

    assert not violations, (
        "response-terminal-only-through-CAS canary — terminalize_response has a "
        f"single L3 owner, {_RESPONSE_TERMINALIZER_OWNER}:\n  "
        + "\n  ".join(violations)
    )
