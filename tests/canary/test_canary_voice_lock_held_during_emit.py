"""ADR-0005 §11 canary: lock-held-during-emit AST scan.

emit_event('utterance.received', ...) inside any jarvis/surface/voice_*.py
function must be lexically reachable only from within a
VOICE_INPUT_LOCK.acquire(...)-bracketed region, OR the function must
assert the lock is already held (VOICE_INPUT_LOCK.locked()).
"""
from __future__ import annotations

import ast

from tests.canary._helpers import repo_root


def _has_lock_acquire(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function body calls VOICE_INPUT_LOCK.acquire(...)."""
    for c in ast.walk(fn):
        if (
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "acquire"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "VOICE_INPUT_LOCK"
        ):
            return True
    return False


def _has_lock_locked_assert(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function body asserts <lock>.locked()."""
    for c in ast.walk(fn):
        if (
            isinstance(c, ast.Assert)
            and isinstance(c.test, ast.Call)
            and isinstance(c.test.func, ast.Attribute)
            and c.test.func.attr == "locked"
        ):
            return True
    return False


def _emits_utterance_received(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function body calls emit_event(type='utterance.received', ...)."""
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "emit_event":
            for kw in c.keywords:
                if (
                    kw.arg == "type"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "utterance.received"
                ):
                    return True
    return False


def test_canary_voice_lock_held_during_emit() -> None:
    """Assert utterance.received emit is guarded by VOICE_INPUT_LOCK per ADR-0005 §11/§8.2.

    Every function in jarvis/surface/voice_*.py that calls
    emit_event(type='utterance.received', ...) must either acquire
    VOICE_INPUT_LOCK in the same body or assert it is locked.
    """
    surface_dir = repo_root() / "jarvis" / "surface"
    violations: list[str] = []
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _emits_utterance_received(node):
                continue
            if _has_lock_acquire(node) or _has_lock_locked_assert(node):
                continue
            violations.append(f"{path}::{node.name}@{node.lineno}")
    assert not violations, (
        f"emit_event(utterance.received) without VOICE_INPUT_LOCK guard in: {violations}"
    )
