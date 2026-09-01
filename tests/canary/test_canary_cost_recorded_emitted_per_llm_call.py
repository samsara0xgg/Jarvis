"""Every L3 ``chat``/``chat_stream`` site has an accounting disposition guard.

Per ADR-0002 § Tier 1 canaries, ADR-0008 §4.1, and spec §5.4.1
(``cost.recorded.owner_layer == L3``): every full or streamed provider call
under :mod:`jarvis.decision` must share a function body with either the legacy
cost emitter or the Wave 1 exactly-once ``CostRecorder`` guard. Forgetting the
guard silently drops spend attribution or an explicit unavailable disposition.

The scan covers the decision orchestrator, reviewer, and cost guard. It
recognizes ``chat`` and ``chat_stream`` calls plus the bounded helper calls
that commit or replay one request disposition. The reviewer retains its
legacy caller-owned emit when the Wave 1 flag is off and uses ``CostRecorder``
in its own function when the flag is on.

The "same function body" pairing is conservative: the canary does not
require strict adjacency, only co-presence within the same function (or
nested helper if a future refactor lifts the emit). Conservative because
the actual sites in :mod:`jarvis.decision` interleave loop control flow
between the chat call and the emit; a tighter adjacency rule would force
brittle refactors. Co-presence is sufficient — once a function uses the
LLM, the cost MUST be recorded somewhere in that function.

Scope: production code under ``jarvis/decision/`` only. Test fixtures
build ChatResult mocks that have no cost emit (they assert the emit
elsewhere) — using :func:`iter_jarvis_py_files` keeps the canary off
``tests/``.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from pathlib import Path

# Modules in scope: every production file that calls the L3 provider adapter
# or owns its exactly-once accounting commit.
_DECISION_LLM_CALLER_MODULES = (
    "jarvis/decision/__init__.py",
    "jarvis/decision/reviewer.py",
    "jarvis/decision/cost_guard.py",
)


def _modules_in_scope() -> list[Path]:
    """Return absolute paths of decision-layer LLM-caller modules that exist."""
    root = repo_root()
    return [root / rel for rel in _DECISION_LLM_CALLER_MODULES if (root / rel).is_file()]


def _is_chat_call(node: ast.AST) -> bool:
    """Return True for full or streamed LLM adapter calls.

    Matches ``ctx.llm_client.chat(...)``, ``self._llm.chat(...)``,
    ``client.chat(...)``. Does NOT match bare ``chat(...)`` (would
    over-flag local helpers); the LLM client's chat is always invoked
    as a method on an object.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in ("chat", "chat_stream")


def _is_cost_recorded_emit(node: ast.AST) -> bool:
    """Return True iff ``node`` is ``emit_event(..., type="cost.recorded", ...)``.

    Also matches calls to the L3-local helpers ``_emit_cost_recorded``
    and ``_emit_cost_recorded_from_metadata`` (which wrap ``emit_event``
    with the ``type="cost.recorded"`` kwarg) so a function that delegates
    its emit to one of those helpers still satisfies the canary.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    guarded_names = {
        "_commit",
        "_emit_cost_recorded",
        "_emit_cost_recorded_from_metadata",
        "record_cost_disposition_once",
    }
    if name in guarded_names:
        return True
    if name == "chat" and isinstance(func, ast.Attribute):
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in ("cost_recorder", "reviewer_cost_recorder"):
            return True
        if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name):
            return owner.func.id == "CostRecorder"
    return name == "emit_event" and any(
        kw.arg == "type"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == "cost.recorded"
        for kw in node.keywords
    )


def _function_walks(module: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function definition in ``module`` (top-level + nested)."""
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _has_chat_call(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff ``func``'s body (excluding nested defs) contains a chat call."""
    for stmt in func.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip into nested defs — they are checked independently.
                continue
            if _is_chat_call(node):
                return True
    return False


def _has_cost_emit(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff ``func``'s body (excluding nested defs) emits cost.recorded."""
    for stmt in func.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_cost_recorded_emit(node):
                return True
    return False


def test_canary_cost_recorded_emitted_per_llm_call() -> None:
    """Fail if any L3 LLM-caller function lacks a cost.recorded emit."""
    violations: list[str] = []
    for path in _modules_in_scope():
        module = parse(path)
        rel = relative_to_repo(path)
        for func in _function_walks(module):
            if not _has_chat_call(func):
                continue
            if _has_cost_emit(func):
                continue
            violations.append(
                f"{rel}:{func.lineno}: function {func.name!r} calls chat/stream but "
                "does not emit cost.recorded in the same body — L3 is the sole "
                "emit-site per spec §5.4.1; every LLM turn must be billed"
            )

    assert not violations, (
        "cost-recorded-per-llm-call canary — every L3 chat/stream site must be "
        "followed by a cost.recorded emit in the same function:\n  "
        + "\n  ".join(violations)
    )
