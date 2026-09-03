"""Every production ``chat``/``chat_stream`` site has an L3 cost guard.

Per ADR-0002 § Tier 1 canaries, ADR-0008 §4.1, and spec §5.4.1
(``cost.recorded.owner_layer == L3``): every full or streamed provider call
under :mod:`jarvis.decision` must share a function body with either the legacy
cost emitter or the Wave 1 exactly-once ``CostRecorder`` guard. Forgetting the
guard silently drops spend attribution or an explicit unavailable disposition.

The scan recursively covers every Python module under ``jarvis/decision``,
``jarvis/runtime`` and ``jarvis/surface`` — no file list is maintained.  Only
the provider adapter implementation module is excluded; the two CostRecorder
implementation methods are checked against their exact internal commit seam.
Adding a future ``response_stream.py`` with an unguarded ``chat_stream``
therefore fails this canary, and so does a nested ``def`` that hides a raw
call inside an otherwise-guarded outer function.

The "same function body" pairing is conservative: the canary does not require
strict adjacency, only a direct ``CostRecorder.chat``/``chat_stream`` branch in
the same body as a raw call.  It deliberately does not accept broad helper
names such as ``_commit`` or ``_emit_cost_recorded`` as exemptions.

Scope: production code only; tests and provider SDK internals are excluded.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDER_ADAPTER_MODULES = frozenset({"jarvis/decision/llm.py"})
_COST_RECORDER_IMPLEMENTATION_METHODS = frozenset(
    {
        ("jarvis/decision/cost_guard.py", "CostRecorder.chat"),
        ("jarvis/decision/cost_guard.py", "CostRecorder.chat_stream"),
    },
)
_SCANNED_PACKAGES = ("jarvis/decision", "jarvis/runtime", "jarvis/surface")


def _modules_in_scope() -> list[Path]:
    """Recursively discover every module in the scanned packages."""
    root = repo_root()
    found: set[Path] = set()
    for package in _SCANNED_PACKAGES:
        for path in (root / package).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if relative_to_repo(path) in _PROVIDER_ADAPTER_MODULES:
                continue
            found.add(path)
    return sorted(found)


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


def _is_cost_recorder_call(node: ast.AST) -> bool:
    """Match only a direct call through the concrete ``CostRecorder`` seam."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("chat", "chat_stream"):
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id in {"cost_recorder", "reviewer_cost_recorder"}
    if isinstance(owner, ast.Attribute):
        return owner.attr == "_cost_recorder"
    return (
        isinstance(owner, ast.Call)
        and isinstance(owner.func, ast.Name)
        and owner.func.id == "CostRecorder"
    )


def _is_cost_recorder_internal_commit(node: ast.AST) -> bool:
    """Match ``self._commit`` only inside the two exact guard methods."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_commit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    )


def _function_walks(
    module: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return every function scope, nested ones included, with a dotted name.

    A nested ``def`` becomes its own scope named
    ``outer.<locals>.inner`` so a raw ``chat`` call hidden inside a closure
    cannot borrow its enclosing function's CostRecorder guard.
    """
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    _collect_function_walks(module.body, prefix="", found=found)
    return found


def _collect_function_walks(
    body: list[ast.stmt],
    *,
    prefix: str,
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> None:
    """Append every function scope in ``body`` under ``prefix`` to ``found``."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = f"{prefix}{node.name}"
            found.append((qualified, node))
            _collect_function_walks(
                node.body,
                prefix=f"{qualified}.<locals>.",
                found=found,
            )
        elif isinstance(node, ast.ClassDef):
            _collect_function_walks(
                node.body,
                prefix=f"{prefix}{node.name}.",
                found=found,
            )


def _calls(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Collect direct-body calls without inheriting nested-function guards."""
    collector = _DirectCallCollector()
    for stmt in func.body:
        collector.visit(stmt)
    return collector.calls


class _DirectCallCollector(ast.NodeVisitor):
    """Visit one function body while pruning nested definitions."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, _node: ast.ClassDef) -> None:
        return


def test_canary_cost_recorded_emitted_per_llm_call() -> None:
    """Fail if any L3 LLM-caller function lacks a cost.recorded emit."""
    violations: list[str] = []
    for path in _modules_in_scope():
        module = parse(path)
        rel = relative_to_repo(path)
        for qualified_name, func in _function_walks(module):
            calls = _calls(func)
            raw_chat_calls = [
                call
                for call in calls
                if _is_chat_call(call) and not _is_cost_recorder_call(call)
            ]
            if not raw_chat_calls:
                continue
            if any(_is_cost_recorder_call(call) for call in calls):
                continue
            if (rel, qualified_name) in _COST_RECORDER_IMPLEMENTATION_METHODS and any(
                _is_cost_recorder_internal_commit(call) for call in calls
            ):
                continue
            violations.append(
                f"{rel}:{func.lineno}: function {qualified_name!r} calls chat/stream "
                "without a direct CostRecorder guard in the same body"
            )

    assert not violations, (
        "cost-recorded-per-llm-call canary — every production chat/stream site "
        "must use the L3 CostRecorder seam:\n  "
        + "\n  ".join(violations)
    )
