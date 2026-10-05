"""Canary — a per-turn, per-tool-call or per-audio-event path never folds the whole log (ADR 0164).

The real log is ~49k events and grows; a whole-log read costs ~0.4-0.5 s. The
decision path advances a shared incremental cache instead, and the lead-in
commentary guard, open_path's ranking and the turn-claim lookups read only the
rows they need. The regression is silent: a call to ``rebuild_projections``,
``make_snapshot`` or a bare ``iter_events`` still returns the right answer, only
a few hundred milliseconds late, on the voice path.

So this pins, for the modules that run on a turn's hot path, that none of those
three is called except where the allowlist names it. The one allowed call is the
fallback for a hand-built runtime that holds no cache.

Canary house style: stdlib ``ast`` only, no import of the module under test.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import Final

from tests.canary._helpers import parse, repo_root

_WHOLE_LOG_READS: Final[frozenset[str]] = frozenset(
    {"rebuild_projections", "make_snapshot", "iter_events"},
)

_HOT_PATH_MODULES: Final[tuple[str, ...]] = (
    "jarvis/runtime/__init__.py",
    "jarvis/runtime/inherent_loop.py",
    "jarvis/runtime/decision_state.py",
    "jarvis/decision/__init__.py",
    "jarvis/decision/packet.py",
    "jarvis/execution/path_resolver.py",
    "jarvis/execution/tools.py",
    "jarvis/surface/voice_media.py",
)

# (module, name) -> calls allowed. Each entry needs a reason; remove it when the call goes.
_ALLOWED: Final[dict[tuple[str, str], int]] = {
    # No shared cache (hand-built runtime, tests): the commentary guard folds the whole log.
    ("jarvis/runtime/inherent_loop.py", "rebuild_projections"): 1,
}


def _called_names(source_path: str) -> Counter[str]:
    calls: Counter[str] = Counter()
    for node in ast.walk(parse(repo_root() / source_path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _WHOLE_LOG_READS:
            calls[name] += 1
    return calls


def test_hot_path_modules_do_not_fold_the_whole_log() -> None:
    """Only the allowlisted fallback call remains, with exactly the allowed count."""
    found = {
        (module, name): count
        for module in _HOT_PATH_MODULES
        for name, count in _called_names(module).items()
    }
    assert found == _ALLOWED
