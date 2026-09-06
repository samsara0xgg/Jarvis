"""Canary — the D6 commentary decision stays pure (ADR-0008 D6).

D6 forbids three hallucinations by name: "马上好" with no evidence,
"已经查到了" before ``action.result_observed``, and timer-based fake progress
when no lifecycle row changed. ``jarvis/decision/commentary.py`` makes all
three unreachable by construction rather than merely discouraged — it derives
the phrase from the committed event alone. That property is one import away
from being lost, and the loss is silent: a module that reads a clock still
returns a sentence, and every existing test still passes.

Adding the phrasing sets put a digest call in that module, which is the exact
moment a ``random.choice`` looks like the obvious way to vary a sentence. So
the two things this pins are:

1. **No source of non-determinism or ambient state is imported or called.**
   ``time``, ``random``, ``secrets``, ``datetime`` and ``os`` are absent, and
   builtin ``hash()`` — randomised per process by ``PYTHONHASHSEED``, so the
   same action would say different things across daemon restarts — is never
   called.
2. **No module-level mutable state.** A dict or list rebound at import time is
   a stored index by another name; the module's answer must depend on nothing
   but its argument.

Canary house style: stdlib ``ast`` only, no import of the module under test.
"""

from __future__ import annotations

import ast
from typing import Final

from tests.canary._helpers import parse, repo_root

_FORBIDDEN_MODULES: Final[frozenset[str]] = frozenset(
    {"time", "random", "secrets", "datetime", "os"},
)

_MUTATING_METHODS: Final[frozenset[str]] = frozenset(
    {"setdefault", "update", "pop", "popitem", "clear", "append", "extend", "add"},
)

_IMMUTABLE_LITERALS: Final[tuple[type[ast.expr], ...]] = (
    ast.Constant,
    ast.Tuple,
    ast.UnaryOp,
)


def _module() -> ast.Module:
    """Parse the one module this canary owns."""
    return parse(repo_root() / "jarvis" / "decision" / "commentary.py")


def test_commentary_imports_no_clock_randomness_or_environment() -> None:
    """No `time`, `random`, `secrets`, `datetime` or `os` reaches this module."""
    imported: set[str] = set()
    for node in ast.walk(_module()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert imported, "the module should import something"
    assert imported & _FORBIDDEN_MODULES == set(), sorted(imported & _FORBIDDEN_MODULES)
    assert "hashlib" in imported, "the digest is the sanctioned source of variation"


def test_commentary_never_calls_builtin_hash() -> None:
    """`hash()` is per-process randomised; no test could pin what it selects."""
    called = [
        node.func.id
        for node in ast.walk(_module())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "hash" not in called, called


def test_commentary_defines_no_module_level_mutable_state() -> None:
    """Every module-level binding is a constant, a tuple, or a frozen mapping.

    The one dict is `_D6_ROWS`, whose values are tuples: it is read-only by
    convention and by `Final`, and nothing in the module rebinds or mutates
    it; `__all__` is an export declaration. Any *other* module-level dict,
    list or set is a stored index, which is what "no stored index" forbids.
    """
    offenders: list[str] = []
    for node in _module().body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if names == ["_D6_ROWS"] or all(n.startswith("__") for n in names):
            # `_D6_ROWS`'s values are tuples and nothing rebinds it; `__all__`
            # is an export declaration, not state.
            continue
        if node.value is None or isinstance(node.value, _IMMUTABLE_LITERALS):
            continue
        offenders.extend(names)
    assert offenders == [], offenders

    mutating = [
        ast.unparse(node)
        for node in ast.walk(_module())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_D6_ROWS"
        and node.func.attr in _MUTATING_METHODS
    ]
    assert mutating == [], mutating
