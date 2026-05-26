"""ADR-0005 §11 canary: normalize-before-emit AST scan.

emit_event('utterance.received', ...) must be preceded by a call to
AsrNormalizer.normalize within the same function body in any
jarvis/surface/voice_*.py file.
"""
from __future__ import annotations

import ast

from tests.canary._helpers import repo_root

_TARGET_EVENT = "utterance.received"


class _OrderChecker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def _check(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for child in ast.walk(fn):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "emit_event"
            ):
                emits_utterance = False
                for kw in child.keywords:
                    if (
                        kw.arg == "type"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == _TARGET_EVENT
                    ):
                        emits_utterance = True
                if not emits_utterance:
                    continue
                emit_lineno = child.lineno
                saw_normalize = False
                for c2 in ast.walk(fn):
                    if (
                        isinstance(c2, ast.Call)
                        and isinstance(c2.func, ast.Attribute)
                        and c2.func.attr == "normalize"
                        and c2.lineno < emit_lineno
                    ):
                        saw_normalize = True
                        break
                if not saw_normalize:
                    self.violations.append((fn.name, emit_lineno))


def test_canary_voice_normalize_before_emit() -> None:
    """Assert normalize precedes emit_event(utterance.received) per ADR-0005 §11.

    Every emit_event(utterance.received, ...) in jarvis/surface/voice_*.py
    must be preceded by .normalize() in the same function body.
    """
    surface_dir = repo_root() / "jarvis" / "surface"
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        checker = _OrderChecker()
        checker.visit(tree)
        assert not checker.violations, (
            f"{path}: emit_event(utterance.received) without preceding normalize() at lines "
            f"{[v[1] for v in checker.violations]}"
        )
