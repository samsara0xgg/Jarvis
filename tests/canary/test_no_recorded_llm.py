"""H7 — no recorded-LLM playback library imports.

Per ADR 0001 § Acceptance criterion H7:

> AST scan: no imports from ``vcrpy``, ``responses``, ``betamax``,
> ``pytest-recording``; no file path containing ``cassette`` or
> ``recording`` is opened during scenario tests.

This file implements **Part A (static)** of H7: walk every ``.py``
under ``tests/`` and ``jarvis/`` with ``ast.parse``; reject any
:class:`ast.Import` / :class:`ast.ImportFrom` referencing
``vcrpy`` / ``vcr`` / ``responses`` / ``betamax`` / ``pytest_recording``
(any submodule of these top-level names counts).

**Part B (runtime open() audit)** is deferred to Step 12's scenario
conftest, where it can plug into the live run; Tier 1 cannot meaningfully
exercise it without firing the scenario. ADR § H7 splits these by
explicit allowance. The scenario conftest in Step 12 will enforce Part
B with an ``open()`` audit hook.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_all_py_files, parse, relative_to_repo

# Top-level module names that indicate recorded-LLM playback. Any
# submodule beneath these (e.g. ``vcr.cassette``) is also rejected.
_FORBIDDEN_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "vcrpy",
        "vcr",
        "responses",
        "betamax",
        "pytest_recording",
    }
)


def _module_top_level(name: str) -> str:
    """Return the top-level package name (everything before the first ``.``)."""
    return name.split(".", 1)[0]


def test_no_recorded_llm_imports() -> None:
    """Fail if any source / test file imports a recorded-LLM playback module."""
    violations: list[str] = []

    for path in iter_all_py_files():
        rel = relative_to_repo(path)
        module = parse(path)
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = _module_top_level(alias.name)
                    if top in _FORBIDDEN_TOP_LEVEL:
                        violations.append(
                            f"{rel}:{node.lineno}: forbidden `import {alias.name}` "
                            "(recorded-LLM playback library)"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    # `from . import foo` — relative, can't reference a
                    # forbidden top-level package.
                    continue
                top = _module_top_level(node.module)
                if top in _FORBIDDEN_TOP_LEVEL:
                    violations.append(
                        f"{rel}:{node.lineno}: forbidden `from {node.module} import ...` "
                        "(recorded-LLM playback library)"
                    )

    assert not violations, (
        "H7 — recorded-LLM playback imports detected:\n  " + "\n  ".join(violations)
    )
