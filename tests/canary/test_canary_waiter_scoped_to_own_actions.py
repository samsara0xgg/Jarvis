"""Canary — an in-flight waiter never returns a foreign action's terminal event.

ADR-0009 D4 § "Double-consumption excluded, both directions" / F9. Once
the supervisor sweep exists, ``action.timeout_assumed`` rows appear in
the log that belong to NO turn — orphans of a crash or a sleep. The
pre-D4 waiter selected on ``type`` + ``id > after_id`` and nothing else,
so the next in-flight turn would swallow the first such row, adopt the
orphan's correlation, and end with a limitation about work it never did.

Two filters, one key. This canary pins all three parts:

1. ``_wait_for_next_trigger`` takes ``action_ids`` and tests membership
   against it before returning a row — the turn accepts only actions it
   dispatched.
2. ``_system_trigger_watcher`` skips rows whose action_id IS in
   ``live_action_ids()`` — the exact complement.
3. Both read the action_id through the same ``_event_action_id`` helper.
   Two hand-rolled extractors that disagree (correlation-only here,
   payload-fallback there) would reopen the hole from the other side: a
   row both filters reject is a turn that hangs until its 5 s timeout and
   an orphan that closes silently with no Limitation claim.

A fourth assertion keeps the fix honest about its own blast radius: the
waiter must gain a *filter*, never a trigger type. ``drive_turn`` has to
pass ``action_ids=`` at the call site, and ``_RUNTIME_TRIGGER_TYPES``
stays the business of ``test_canary_runtime_trigger_types``.

Implementation: stdlib AST only — no jarvis imports, no mock.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_RUNTIME_RELPATH: str = "jarvis/runtime/__init__.py"
_INHERENT_LOOP_RELPATH: str = "jarvis/runtime/inherent_loop.py"

_WAITER_FN: str = "_wait_for_next_trigger"
_DRIVE_TURN_FN: str = "drive_turn"
_SYSTEM_WATCHER_FN: str = "_system_trigger_watcher"
_ACTION_IDS_PARAM: str = "action_ids"
_ACTION_ID_EXTRACTOR: str = "_event_action_id"
_LIVE_SET_CALL: str = "live_action_ids"


def _find_function_def(
    module: ast.Module,
    *,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the top-level ``def``/``async def`` named ``name``, or ``None``."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
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


def _iter_in_comparisons(node: ast.AST) -> list[ast.Compare]:
    """Return every ``ast.Compare`` under ``node`` using the ``in`` operator."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Compare)
        and any(isinstance(op, ast.In) for op in sub.ops)
    ]


def _compares_against_name(compare: ast.Compare, *, name: str) -> bool:
    """True iff any comparator of ``compare`` is the bare ``ast.Name`` ``name``."""
    return any(
        isinstance(cmp_, ast.Name) and cmp_.id == name for cmp_ in compare.comparators
    )


def _compares_against_call(compare: ast.Compare, *, name: str) -> bool:
    """True iff any comparator of ``compare`` is a call to ``name``."""
    return any(
        isinstance(cmp_, ast.Call) and _call_name(cmp_) == name
        for cmp_ in compare.comparators
    )


def test_canary_waiter_takes_and_applies_an_action_id_filter() -> None:
    """``_wait_for_next_trigger`` MUST filter candidate rows by ``action_ids``."""
    module = parse(repo_root() / _RUNTIME_RELPATH)
    waiter = _find_function_def(module, name=_WAITER_FN)
    assert waiter is not None, (
        f"{_RUNTIME_RELPATH} must define a top-level {_WAITER_FN!r}."
    )

    kwonly = [arg.arg for arg in waiter.args.kwonlyargs]
    assert _ACTION_IDS_PARAM in kwonly, (
        f"{_WAITER_FN}: keyword-only parameter {_ACTION_IDS_PARAM!r} is gone "
        f"(saw {kwonly!r}). ADR-0009 F9 — without the turn's own action-id "
        "set the waiter is back to type+id selection and will hand a foreign "
        "orphan's action.timeout_assumed to whichever turn is in flight."
    )
    assert waiter.args.kw_defaults[kwonly.index(_ACTION_IDS_PARAM)] is None, (
        f"{_WAITER_FN}: {_ACTION_IDS_PARAM} has a default. It must stay "
        "required — a call site that forgets it would silently fall back to "
        "accepting nothing (deadlock) or everything (F9), depending on the "
        "default chosen."
    )

    scoped = [
        cmp_
        for cmp_ in _iter_in_comparisons(waiter)
        if _compares_against_name(cmp_, name=_ACTION_IDS_PARAM)
    ]
    assert scoped, (
        f"{_WAITER_FN}: parameter {_ACTION_IDS_PARAM!r} is accepted but never "
        "tested against. The filter is the fix; the signature alone is not."
    )

    extractor_calls = _iter_calls_with_name(waiter, name=_ACTION_ID_EXTRACTOR)
    assert extractor_calls, (
        f"{_WAITER_FN}: no `{_ACTION_ID_EXTRACTOR}(...)` call. Both consumers "
        "must read the action_id through the same helper or their predicates "
        "stop being complements."
    )


def test_canary_drive_turn_scopes_the_waiter_to_its_own_actions() -> None:
    """``drive_turn`` MUST pass ``action_ids=`` at every waiter call site."""
    module = parse(repo_root() / _RUNTIME_RELPATH)
    drive_turn = _find_function_def(module, name=_DRIVE_TURN_FN)
    assert drive_turn is not None, (
        f"{_RUNTIME_RELPATH} must define a top-level {_DRIVE_TURN_FN!r}."
    )

    waiter_calls = _iter_calls_with_name(drive_turn, name=_WAITER_FN)
    assert waiter_calls, (
        f"{_DRIVE_TURN_FN}: no `{_WAITER_FN}(...)` call found — the multi-"
        "trigger loop has lost its wait."
    )
    for call in waiter_calls:
        passed = {kw.arg for kw in call.keywords}
        assert _ACTION_IDS_PARAM in passed, (
            f"{_DRIVE_TURN_FN}: `{_WAITER_FN}(...)` at line {call.lineno} does "
            f"not pass {_ACTION_IDS_PARAM}= (saw {sorted(a for a in passed if a)!r})."
        )


def test_canary_system_watcher_takes_only_the_complement() -> None:
    """``_system_trigger_watcher`` MUST skip rows a live turn already owns."""
    module = parse(repo_root() / _INHERENT_LOOP_RELPATH)
    watcher = _find_function_def(module, name=_SYSTEM_WATCHER_FN)
    assert watcher is not None, (
        f"{_INHERENT_LOOP_RELPATH} must define {_SYSTEM_WATCHER_FN!r} "
        "(ADR-0009 D4 — orphan terminal events need a consumer, or the sweep "
        "emits into the void and no Limitation claim is ever folded)."
    )

    guards = [
        node
        for node in ast.walk(watcher)
        if isinstance(node, ast.If)
        and any(
            _compares_against_call(cmp_, name=_LIVE_SET_CALL)
            for cmp_ in _iter_in_comparisons(node.test)
        )
    ]
    assert guards, (
        f"{_SYSTEM_WATCHER_FN}: no `if ... in {_LIVE_SET_CALL}():` guard. "
        "Without it the watcher drives a second, system-triggered turn for a "
        "terminal event the owning turn's waiter is about to consume — the "
        "same event handled twice, one of the two directions ADR-0009 D4 "
        "exists to exclude."
    )
    assert any(
        isinstance(stmt, ast.Continue)
        for guard in guards
        for stmt in ast.walk(guard)
    ), (
        f"{_SYSTEM_WATCHER_FN}: the `{_LIVE_SET_CALL}()` guard does not "
        "short-circuit the loop body with `continue`."
    )

    extractor_calls = _iter_calls_with_name(watcher, name=_ACTION_ID_EXTRACTOR)
    assert extractor_calls, (
        f"{_SYSTEM_WATCHER_FN}: no `{_ACTION_ID_EXTRACTOR}(...)` call. The "
        "watcher must key on the action_id exactly the way the waiter does; "
        "two extractors that disagree let a row be dropped by both filters."
    )
