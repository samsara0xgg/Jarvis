"""Surface-user-intent swap canary: only the voice surface emits ``utterance.received``.

The Day-2 CLI emits ``surface.user_intent`` per spec §3.4.1 trigger taxonomy.
``utterance.received`` is the voice-surface event (ADR-0005 §4.2,
``jarvis/surface/voice_pipeline.py``) — every OTHER production emit site
must use ``surface.user_intent`` instead so the CLI/voice swap stays clean.

This canary AST-scans every ``.py`` file under ``jarvis/`` and asserts
that the only file calling ``emit_event(..., type="utterance.received", ...)``
is the ADR-0005 voice pipeline. The string literal can still appear in
other contexts — e.g. the registry definition in ``event_log.py`` keeping
the event type registered — so the scan is narrowly scoped to ``emit_event``
callsites with a ``type=`` keyword.

Scope note: uses :func:`iter_jarvis_py_files` (not ``iter_all_py_files``)
because the canary guards production emit sites, not test fixtures.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

# ADR-0005 §4.2: the voice pipeline is the canonical emit site for
# ``utterance.received``. Adding new emit sites requires a separate ADR.
_VOICE_EMIT_ALLOWLIST: frozenset[str] = frozenset(
    {"jarvis/surface/voice_pipeline.py"},
)


def _is_emit_event_call(node: ast.AST) -> TypeGuard[ast.Call]:
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
        "Only the ADR-0005 voice pipeline (jarvis/surface/voice_pipeline.py) "
        "may emit utterance.received."
    )


def test_canary_surface_user_intent_swap() -> None:
    """Fail if any non-voice ``.py`` under ``jarvis/`` emits utterance.received."""
    violations: list[str] = [
        _format_violation(rel, getattr(node, "lineno", 0))
        for path in iter_jarvis_py_files()
        for rel in (relative_to_repo(path),)
        if rel not in _VOICE_EMIT_ALLOWLIST
        for node in ast.walk(parse(path))
        if _is_emit_event_call(node) and _type_kwarg_is_legacy_utterance(node)
    ]

    assert not violations, (
        "surface-user-intent-swap canary — Day-2 CLI emits surface.user_intent; "
        "only jarvis/surface/voice_pipeline.py (ADR-0005 §4.2) may emit "
        "utterance.received:\n  " + "\n  ".join(violations)
    )
