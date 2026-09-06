"""Canary: the playback budget and stream liveness stay separate parameters.

``_stream_with_prefix_fallback`` once derived ``live = budget is not None``,
which conflated "a per-segment deadline exists" with "the segment list may
still grow". The caller then withheld the budget on the non-live path, so the
per-segment reschedule never ran there and one deadline bounded the whole
answer. A leaked ``live=True`` on the non-live path has no observable
consequence today (``_await_segments`` returns False at once once the response
is emitted, and only the active response is ever tagged), so the split is
pinned statically rather than behaviourally.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import repo_root

_TARGET = "_stream_with_prefix_fallback"


def _find(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == _TARGET:
            return node
    msg = f"{_TARGET} not found in jarvis/surface/voice_media.py"
    raise AssertionError(msg)


def test_canary_playback_budget_not_liveness() -> None:
    """Assert a non-optional budget, a separate liveness flag, and no None compare."""
    path = repo_root() / "jarvis" / "surface" / "voice_media.py"
    fn = _find(ast.parse(path.read_text(encoding="utf-8")))
    names = [arg.arg for arg in fn.args.kwonlyargs]
    assert "budget" in names, f"{_TARGET} lost its keyword-only budget parameter"
    assert "live" in names, f"{_TARGET} must take liveness explicitly, not infer it"

    budget_arg = fn.args.kwonlyargs[names.index("budget")]
    annotation = ast.unparse(budget_arg.annotation) if budget_arg.annotation else ""
    assert "None" not in annotation, f"{_TARGET} budget must not be optional: {annotation!r}"
    assert "Optional" not in annotation, f"{_TARGET} budget must not be optional: {annotation!r}"
    assert fn.args.kw_defaults[names.index("budget")] is None, (
        f"{_TARGET} budget must not carry a default"
    )

    live_arg = fn.args.kwonlyargs[names.index("live")]
    live_annotation = ast.unparse(live_arg.annotation) if live_arg.annotation else ""
    assert live_annotation == "bool", f"{_TARGET} live must be a bool, got {live_annotation!r}"

    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        mentions_budget = any(
            isinstance(item, ast.Name) and item.id == "budget" for item in operands
        )
        mentions_none = any(
            isinstance(item, ast.Constant) and item.value is None for item in operands
        )
        assert not (mentions_budget and mentions_none), (
            f"{_TARGET} compares budget against None at line {node.lineno}; "
            "the budget is unconditional and liveness is its own parameter"
        )
