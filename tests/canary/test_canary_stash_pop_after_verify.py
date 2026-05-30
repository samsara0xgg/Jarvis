"""Canary — ``restore_pretask_changes`` lives in runtime, NOT in spawn_worker.

Per ADR-0002 Step 11 / Step 17 Tier-1 canary list (lines 1692-1699) +
§ Dirty-tree policy (lines 663-713):

    ``test_canary_stash_pop_after_verify`` — AST scan: the symbol
    ``restore_pretask_changes`` does NOT appear inside the body of
    ``spawn_worker_handler`` in ``jarvis/execution/tools.py``; the only
    call site is ``jarvis/runtime/__init__.py``, where the linearized
    source orders ``verify_diff_handler(...)`` strictly before
    ``restore_pretask_changes(...)`` along the happy path. Enforces the
    § Dirty-tree policy ordering contract — the stash-pop must not
    pollute the tree that ``verify_command`` reads.

Implementation: this canary is the conservative half of the contract.

* L4 (``jarvis/execution/tools.py``) — assert that no
  ``restore_pretask_changes(...)`` call appears ANYWHERE in
  ``spawn_worker_handler`` (recursing into any nested defs / lambdas).
  If L4 ever pops the stash, the verify_command subprocess (which runs
  ``cwd=repo_path`` inside ``verify_diff_handler``) would see Allen's
  pre-task work layered on top of Codex's edits and the exit-code
  predicate would be polluted (ADR § Dirty-tree policy lines 663-713).

* L7 composition root (``jarvis/runtime/__init__.py``) — assert that
  ``restore_pretask_changes`` is BOTH imported AND called at least
  once. This pairs with the absence guard above: by L3 / L4 layer
  isolation, the only legitimate caller is the runtime composition.

The dispatch site for ``verify_diff`` is the L3 ``decide(...)`` call
inside ``drive_turn``; lexically that call appears in the source before
the ``restore_pretask_changes(...)`` finalizer. We assert the line-
number ordering as a hardening on top of the import + presence checks.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_RESTORE_CALL_NAME: str = "restore_pretask_changes"
_VERIFY_DISPATCH_CALL_NAME: str = "decide"
_TOOLS_MODULE_RELPATH: str = "jarvis/execution/tools.py"
_RUNTIME_MODULE_RELPATH: str = "jarvis/runtime/__init__.py"
_SPAWN_HANDLER_NAME: str = "spawn_worker_handler"
_DRIVE_TURN_NAME: str = "drive_turn"


def _find_function_def(module: ast.Module, *, name: str) -> ast.FunctionDef | None:
    """Return the top-level ``def <name>(...)`` node, or ``None``."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _call_name(call: ast.Call) -> str | None:
    """Return the dotted-name suffix of ``call.func`` (``a.b.c`` -> ``"c"``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_calls_with_name(node: ast.AST, *, name: str) -> list[ast.Call]:
    """Return every ``ast.Call`` under ``node`` whose call-suffix == ``name``."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and _call_name(sub) == name
    ]


def _imports_name(module: ast.Module, *, name: str) -> bool:
    """Return True iff ``module`` imports the bare symbol ``name``.

    Matches ``from <pkg> import <name>`` (with or without an alias whose
    asname == name) AND any ``import <pkg>`` followed by a deeper
    qualified reference. The latter is implicit for runtime's call-site
    style; the explicit ``from <pkg> import restore_pretask_changes``
    is the canonical form per ``jarvis/runtime/__init__.py``.
    """
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == name:
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == name:
                    return True
    return False


def test_canary_spawn_worker_does_not_restore_pretask_changes() -> None:
    """``spawn_worker_handler`` MUST NOT call ``restore_pretask_changes``."""
    module = parse(repo_root() / _TOOLS_MODULE_RELPATH)
    handler = _find_function_def(module, name=_SPAWN_HANDLER_NAME)
    assert handler is not None, (
        f"{_TOOLS_MODULE_RELPATH} must define {_SPAWN_HANDLER_NAME!r} at "
        "module top level (ADR-0002 Step 10)."
    )

    offending_calls = _iter_calls_with_name(handler, name=_RESTORE_CALL_NAME)
    offending_lines = sorted(call.lineno for call in offending_calls)
    assert not offending_calls, (
        f"{_SPAWN_HANDLER_NAME}: {_RESTORE_CALL_NAME}(...) call(s) at line(s) "
        f"{offending_lines} — ADR-0002 § Dirty-tree policy (lines 663-713) "
        "forbids L4 from popping the pre-task stash. The runtime composition "
        "owns the pop, AFTER verify_diff_handler has read the tree."
    )


def test_canary_runtime_imports_restore_pretask_changes() -> None:
    """``jarvis/runtime/__init__.py`` MUST import ``restore_pretask_changes``."""
    module = parse(repo_root() / _RUNTIME_MODULE_RELPATH)
    assert _imports_name(module, name=_RESTORE_CALL_NAME), (
        f"{_RUNTIME_MODULE_RELPATH}: import for {_RESTORE_CALL_NAME!r} not "
        "found. The composition root must own the stash-pop call site so "
        "the canary's absence-in-L4 + presence-in-runtime pair pins the "
        "ordering at the layer boundary."
    )


def test_canary_runtime_calls_restore_pretask_changes_after_decide() -> None:
    """The runtime MUST call ``restore_pretask_changes`` AFTER ``decide()``.

    The verify_diff dispatch lives inside the L3 ``decide(...)`` call; the
    stash-pop must come after that dispatch in linearized source so the
    verify_command subprocess sees the post-Codex tree.
    """
    module = parse(repo_root() / _RUNTIME_MODULE_RELPATH)
    restore_calls = _iter_calls_with_name(module, name=_RESTORE_CALL_NAME)
    assert restore_calls, (
        f"{_RUNTIME_MODULE_RELPATH}: no {_RESTORE_CALL_NAME}(...) call found. "
        "The runtime composition is the SOLE legitimate call site (ADR-0002 "
        "§ Dirty-tree policy)."
    )

    decide_calls = _iter_calls_with_name(module, name=_VERIFY_DISPATCH_CALL_NAME)
    assert decide_calls, (
        f"{_RUNTIME_MODULE_RELPATH}: no {_VERIFY_DISPATCH_CALL_NAME}(...) call "
        "found — runtime composition must drive the L3 decide loop."
    )

    first_restore_line = min(call.lineno for call in restore_calls)
    last_decide_line = max(call.lineno for call in decide_calls)
    assert last_decide_line < first_restore_line, (
        f"{_RUNTIME_MODULE_RELPATH}: every {_RESTORE_CALL_NAME}(...) call "
        f"(earliest line={first_restore_line}) must be lexically AFTER the "
        f"last {_VERIFY_DISPATCH_CALL_NAME}(...) call (latest line="
        f"{last_decide_line}). ADR-0002 § Dirty-tree policy line 711-713: "
        "verify_diff_handler must run before the stash is popped."
    )


def test_canary_drive_turn_orders_pop_inside_function_body() -> None:
    """Within ``drive_turn``, the pop site MUST come after the decide loop."""
    module = parse(repo_root() / _RUNTIME_MODULE_RELPATH)
    drive_turn_def = _find_function_def(module, name=_DRIVE_TURN_NAME)
    assert drive_turn_def is not None, (
        f"{_RUNTIME_MODULE_RELPATH} must define a top-level {_DRIVE_TURN_NAME!r}."
    )

    # The runtime delegates the actual call to a private helper
    # (`_pop_pending_stashes`); that helper, in turn, calls
    # `restore_pretask_changes`. We assert drive_turn invokes the helper
    # AFTER `decide(...)` to keep the line-number ordering meaningful
    # even when the canonical pop site is one indirection away.
    helper_calls = _iter_calls_with_name(drive_turn_def, name="_pop_pending_stashes")
    decide_calls = _iter_calls_with_name(drive_turn_def, name=_VERIFY_DISPATCH_CALL_NAME)
    assert helper_calls, (
        f"{_DRIVE_TURN_NAME}: no `_pop_pending_stashes(...)` call found — the "
        "finalizer is the in-function bridge to the stash-pop site."
    )
    assert decide_calls, (
        f"{_DRIVE_TURN_NAME}: no `{_VERIFY_DISPATCH_CALL_NAME}(...)` call found."
    )
    earliest_helper = min(call.lineno for call in helper_calls)
    latest_decide = max(call.lineno for call in decide_calls)
    assert latest_decide < earliest_helper, (
        f"{_DRIVE_TURN_NAME}: `_pop_pending_stashes(...)` at line "
        f"{earliest_helper} must come after the last `decide(...)` call at "
        f"line {latest_decide}. ADR-0002 § Dirty-tree policy."
    )
