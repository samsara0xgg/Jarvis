"""reviewer-fresh-context canary: every reviewer ``.chat(...)`` is wrapped.

Per ADR-0002 § Reviewer contract (line 738): the reviewer LLM prompt
**must not** see jarvis's session history — fresh context per review.
The structural enforcement is :meth:`LLMClient.fresh_context`, a
contextmanager that the reviewer's chat call must sit inside. This
canary AST-scans ``jarvis/decision/reviewer.py`` and asserts that every
``.chat(...)`` call inside :func:`review_diff` is lexically wrapped by
a ``with <expr>.fresh_context() ...`` statement.

For Step 9 the only ``.chat(...)`` call site lives inside
``review_diff``; Step 12 will add a caller in the L3 Result Interpreter
that calls ``review_diff(...)`` (not ``.chat(...)``) so the canary keeps
its narrow focus. The "wrap at the chat site" form is the architectural
invariant the canary enforces — if a future refactor moves the chat
elsewhere or skips the fresh_context wrap, the canary fires.

Scope: ``jarvis/decision/reviewer.py`` only. The canary is conservative
— it does not try to chase ``review_diff`` call sites through the rest
of the codebase; the wrap-at-chat-site invariant inside ``reviewer.py``
is sufficient because the chat call itself is the architectural sin.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, relative_to_repo, repo_root


def _is_chat_call(node: ast.AST) -> bool:
    """Return True iff ``node`` is a call whose method name is ``chat``.

    Matches ``fresh.chat(...)``, ``llm_client.chat(...)``, ``self._llm.chat(...)``
    — any attribute-style call ending in ``.chat``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "chat"


def _with_expr_calls_fresh_context(with_node: ast.With) -> bool:
    """True iff any ``with`` item's expression is a ``<expr>.fresh_context()`` call.

    Matches ``with llm_client.fresh_context() as fresh:`` and
    ``with self._llm.fresh_context():``. Conservative: requires the
    contextmanager to be an attribute-style call so a bare ``fresh_context()``
    helper name does not satisfy the contract (the contextmanager lives
    on the LLM client object).
    """
    for item in with_node.items:
        expr = item.context_expr
        if not isinstance(expr, ast.Call):
            continue
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr == "fresh_context":
            return True
    return False


def _find_chat_calls_with_with_stack(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[ast.Call, list[ast.With]]]:
    """Yield ``(chat_call, with_stack)`` for each chat call in ``func``.

    ``with_stack`` is the list of ``ast.With`` ancestors that lexically
    enclose the chat call (innermost last). Walking with a manual stack
    keeps the canary stdlib-only (no third-party AST utilities).
    """
    out: list[tuple[ast.Call, list[ast.With]]] = []

    def walk(node: ast.AST, with_stack: list[ast.With]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not func:
            # Skip nested function definitions — they are independent
            # scopes and would be checked under their own canary call.
            return
        if isinstance(node, ast.With):
            with_stack = [*with_stack, node]
        if _is_chat_call(node):
            assert isinstance(node, ast.Call)
            out.append((node, list(with_stack)))
        for child in ast.iter_child_nodes(node):
            walk(child, with_stack)

    for stmt in func.body:
        walk(stmt, [])
    return out


def test_canary_reviewer_fresh_context() -> None:
    """Every ``.chat(...)`` inside ``review_diff`` must sit in a fresh_context ``with``."""
    root = repo_root()
    reviewer_path = root / "jarvis" / "decision" / "reviewer.py"
    assert reviewer_path.is_file(), (
        "ADR-0002 Step 9: jarvis/decision/reviewer.py must exist before "
        "this canary can scan it (companion canary "
        "test_canary_reviewer_module_exists_at_l3 covers the file-presence "
        "case explicitly)"
    )

    module = parse(reviewer_path)
    rel = relative_to_repo(reviewer_path)

    review_diff_funcs = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "review_diff"
    ]
    assert review_diff_funcs, (
        f"{rel}: ADR-0002 Step 9 expects a ``review_diff`` function in "
        "jarvis/decision/reviewer.py; none found"
    )

    violations: list[str] = []
    for func in review_diff_funcs:
        chat_sites = _find_chat_calls_with_with_stack(func)
        if not chat_sites:
            violations.append(
                f"{rel}:{func.lineno}: review_diff has no .chat(...) call — "
                "the reviewer must hit the LLM exactly once per call"
            )
            continue
        violations.extend(
            f"{rel}:{call.lineno}: .chat(...) inside review_diff is not wrapped "
            "in `with <expr>.fresh_context() ...:` — reviewer prompts MUST NOT "
            "inherit decision-LLM session history (ADR-0002 § Reviewer contract)"
            for call, with_stack in chat_sites
            if not any(_with_expr_calls_fresh_context(w) for w in with_stack)
        )

    assert not violations, (
        "fresh-context wrap canary — every reviewer chat call must sit inside "
        "`with llm_client.fresh_context():`:\n  " + "\n  ".join(violations)
    )
