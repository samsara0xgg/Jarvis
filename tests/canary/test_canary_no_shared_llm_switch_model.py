"""Runtime routing never mutates a shared LLM client's pinned preset.

Per ADR-0008 §3 D4 / F19 and the Wave 4A plan §8.4: provider identity belongs
to one ResponseRun, not to a process-wide client.  ``LLMClient.switch_model``
rebinds provider, model and base URL in place, so a single call site would let
two overlapping runs read each other's ``_last_metadata`` and cross-attribute
``cost.recorded``.  The structural fix is per-run construction; this canary
pins the two shapes that keep it structural:

- zero ``.switch_model(`` call sites anywhere under ``jarvis/``;
- ``LLMClient(`` is constructed only in the provider adapter itself, in the
  composition root, and in the per-run session factory.

``jarvis/decision/llm_session.py`` is allow-listed ahead of the module that
creates it; an allow-listed path that does not exist yet is vacuously
satisfied, so this canary is green before and after the Wave 4A slice.
"""

from __future__ import annotations

import ast
from typing import Final

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

_LLM_CLIENT_CONSTRUCTION_SITES: Final[frozenset[str]] = frozenset(
    {
        "jarvis/decision/llm.py",
        "jarvis/decision/llm_session.py",
        "jarvis/runtime/__init__.py",
        # ADR-0019: the compaction summariser's own preset-pinned client,
        # built once in the composition root and never switched.
        "jarvis/runtime/session_compaction.py",
    },
)


def test_canary_no_switch_model_call_sites() -> None:
    """Fail if any production module calls ``switch_model``."""
    violations: list[str] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "switch_model":
                violations.append(f"{rel}:{node.lineno}: calls switch_model")

    assert not violations, (
        "no-shared-llm-switch-model canary — ADR-0008 D4 forbids runtime "
        "routing from mutating a shared client; mint an LLMRequestClient per "
        "ResponseRun instead:\n  " + "\n  ".join(violations)
    )


def test_canary_llm_client_constructed_only_at_known_sites() -> None:
    """Fail if ``LLMClient(...)`` is constructed outside the allowed modules."""
    violations: list[str] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        if rel in _LLM_CLIENT_CONSTRUCTION_SITES:
            continue
        violations.extend(
            f"{rel}:{node.lineno}: constructs LLMClient"
            for node in ast.walk(parse(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LLMClient"
        )

    assert not violations, (
        "no-shared-llm-switch-model canary — LLMClient may only be constructed "
        f"in {sorted(_LLM_CLIENT_CONSTRUCTION_SITES)}:\n  " + "\n  ".join(violations)
    )
