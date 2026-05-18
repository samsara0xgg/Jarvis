"""H2 — every ``emit_event(type=...)`` literal is in EventTypeRegistry.

Per ADR 0001 § Acceptance criterion H2:

> AST scan for all ``emit_event("<type>", ...)`` literals; type string
> must exist in EventTypeRegistry. Unregistered → fail.

Implementation: walk all ``.py`` under ``jarvis/`` and ``tests/`` with
``ast.parse``; for every :class:`ast.Call` to a function named
``emit_event`` whose first positional argument is ``ast.Constant(str)``
or whose ``type=`` keyword argument is ``ast.Constant(str)``, collect
the literal. Then import :class:`EventTypeRegistry` from
``jarvis.state.event_log`` and assert every collected literal is in
``EventTypeRegistry.iter_types()``.
"""

from __future__ import annotations

import ast

from jarvis.state.event_log import EventTypeRegistry
from tests.canary._helpers import iter_all_py_files, parse, relative_to_repo


def _emit_event_type_literal(call: ast.Call) -> str | None:
    """Return the ``type=`` literal for an emit_event call, or None."""
    # `emit_event` accepts `type` as a keyword (its signature is
    # keyword-only after `conn`) — match the kwarg form first.
    for kw in call.keywords:
        if kw.arg == "type" and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            if isinstance(value, str):
                return value
    # Defensive: also support the (unused but plausible) positional form
    # `emit_event(conn, "<type>", ...)` — first positional after `conn`.
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        if isinstance(value, str):
            return value
    return None


def _is_emit_event_call(call: ast.Call) -> bool:
    """True iff this call's callee is named ``emit_event``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "emit_event"
    if isinstance(func, ast.Attribute):
        return func.attr == "emit_event"
    return False


def test_emit_event_type_literals_are_registered() -> None:
    """Fail if any ``emit_event(type="X")`` uses an unregistered event type."""
    registered = set(EventTypeRegistry.iter_types())
    unregistered: list[str] = []
    seen: set[tuple[str, int, str]] = set()

    for path in iter_all_py_files():
        rel = relative_to_repo(path)
        module = parse(path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not _is_emit_event_call(node):
                continue
            literal = _emit_event_type_literal(node)
            if literal is None:
                continue
            if literal in registered:
                continue
            key = (rel, node.lineno, literal)
            if key in seen:
                continue
            seen.add(key)
            unregistered.append(
                f"{rel}:{node.lineno}: emit_event(type={literal!r}) is not in EventTypeRegistry"
            )

    assert not unregistered, (
        "H2 — unregistered emit_event type literals:\n  " + "\n  ".join(unregistered)
    )
