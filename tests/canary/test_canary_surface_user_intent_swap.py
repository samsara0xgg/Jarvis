"""Surface-user-intent swap canary: no production ``emit_event`` site uses ``utterance.received``.

Day-2 CLI emits ``surface.user_intent`` per spec §3.4.1 trigger taxonomy.
``utterance.received`` is reserved for a future voice surface and remains
in the registry (``jarvis.state.event_log``), but no production emit site
may use it Day-2.

This canary AST-scans every ``.py`` file under ``jarvis/`` and asserts
ZERO calls of the form ``emit_event(..., type="utterance.received", ...)``.
The string literal can still appear in other contexts — e.g. the registry
definition in ``event_log.py`` keeping the event type registered — so the
scan is narrowly scoped to ``emit_event`` callsites with a ``type=`` keyword.

Scope note: uses :func:`iter_jarvis_py_files` (not ``iter_all_py_files``)
because the canary guards production emit sites, not test fixtures.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo


def _is_emit_event_call(node: ast.AST) -> bool:
    """Return True iff ``node`` is a call to a function named ``emit_event``.

    Accepts both bare ``emit_event(...)`` and attribute-style
    ``mod.emit_event(...)`` so a future re-export shape is still caught.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "emit_event"
    if isinstance(func, ast.Attribute):
        return func.attr == "emit_event"
    return False


def _type_kwarg_is_legacy_utterance(call: ast.Call) -> bool:
    """Return True iff one of ``call.keywords`` is ``type="utterance.received"``."""
    for kw in call.keywords:
        if kw.arg != "type":
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and value.value == "utterance.received":
            return True
    return False


def _format_violation(rel: str, lineno: int) -> str:
    """Render one violation message."""
    return (
        f'{rel}:{lineno}: emit_event(..., type="utterance.received", ...) '
        "— Day-2 CLI emits surface.user_intent per spec §3.4.1 trigger taxonomy. "
        "utterance.received is reserved for a future voice surface and stays in "
        "the registry, but no production emit site may use it Day-2."
    )


def test_canary_surface_user_intent_swap() -> None:
    """Fail if any ``.py`` under ``jarvis/`` calls emit_event with type=utterance.received."""
    violations: list[str] = [
        _format_violation(relative_to_repo(path), getattr(node, "lineno", 0))
        for path in iter_jarvis_py_files()
        for node in ast.walk(parse(path))
        if _is_emit_event_call(node) and _type_kwarg_is_legacy_utterance(node)
    ]

    assert not violations, (
        "surface-user-intent-swap canary — Day-2 CLI emits surface.user_intent "
        "(utterance.received remains reserved in the registry for a future voice "
        "surface but no production emit site may use it):\n  " + "\n  ".join(violations)
    )
