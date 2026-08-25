"""Canary — the supervisor sweep skips live turns, and no turn stays live forever.

ADR-0009 D4 § "Active-turn exclusion, defined" + F7. The sweep closes
open actions by fold, and the only thing standing between it and an
action a turn is still driving is the live action-id set. That guard has
two halves, and BOTH must hold or the invariant inverts:

* **The sweep must consult the set.** ``sweep_overdue_actions`` skips
  every ``action_id`` in ``active_action_ids``. Drop the skip and a 30 s
  tick lands ``action.timeout_assumed`` on an action Codex is still
  running — the turn then ends twice, with a limitation it did not earn.

* **A crashed turn must not pin its actions "active" forever.** This is
  the half the ADR calls out by name. ``drive_turn`` releases the turn's
  whole entry from a ``finally`` that runs even when the turn raises. Move
  that release onto the happy path and the very crash that orphans an
  action also makes the sweep permanently refuse to close it — the ghost
  action this ADR exists to kill, now un-killable for the life of the
  process.

A third assertion pins the freshness rule that makes the first half
meaningful: the sweep's caller passes ``active_action_ids=`` a **call**
to ``live_action_ids()``, not a name bound once at wiring time. A
captured snapshot would keep protecting actions from turns that ended
minutes ago.

Implementation: stdlib AST only — no imports of jarvis internals, no
mock. We assert on structure (a membership test that ``continue``s, a
``Try`` whose ``finalbody`` calls the release, a keyword whose value is a
``Call``), which is what actually encodes the contract.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_SLEEP_WAKE_RELPATH: str = "jarvis/deployment/sleep_wake.py"
_RUNTIME_RELPATH: str = "jarvis/runtime/__init__.py"
_INHERENT_LOOP_RELPATH: str = "jarvis/runtime/inherent_loop.py"

_SWEEP_FN: str = "sweep_overdue_actions"
_DRIVE_TURN_FN: str = "drive_turn"
_RUN_SWEEP_FN: str = "_run_supervisor_sweep"
_ACTIVE_SET_PARAM: str = "active_action_ids"
_RELEASE_CALL: str = "release_turn_actions"
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


def _has_skip_on_membership(node: ast.AST, *, container: str) -> bool:
    """True iff ``node`` contains ``if <x> in <container>: ... continue``.

    The body is walked rather than index-matched so an intervening log
    line before the ``continue`` still counts — the contract is "membership
    short-circuits the loop body", not a particular statement count.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        test = sub.test
        if not isinstance(test, ast.Compare):
            continue
        if not any(isinstance(op, ast.In) for op in test.ops):
            continue
        matches_container = any(
            isinstance(cmp_, ast.Name) and cmp_.id == container
            for cmp_ in test.comparators
        )
        if not matches_container:
            continue
        if any(isinstance(stmt, ast.Continue) for stmt in ast.walk(sub)):
            return True
    return False


def test_canary_sweep_skips_actions_in_the_active_set() -> None:
    """``sweep_overdue_actions`` MUST short-circuit on ``active_action_ids``."""
    module = parse(repo_root() / _SLEEP_WAKE_RELPATH)
    sweep = _find_function_def(module, name=_SWEEP_FN)
    assert sweep is not None, (
        f"{_SLEEP_WAKE_RELPATH} must define a top-level {_SWEEP_FN!r} "
        "(ADR-0009 D4 — the fold lives in the one deployment file whose "
        "jarvis.state.event_log import is excepted by H13)."
    )

    kwonly = [arg.arg for arg in sweep.args.kwonlyargs]
    assert _ACTIVE_SET_PARAM in kwonly, (
        f"{_SWEEP_FN}: keyword-only parameter {_ACTIVE_SET_PARAM!r} is gone "
        f"(saw {kwonly!r}). The composition root passes the live set down as "
        "a value so `deployment` never imports `execution`; without the "
        "parameter the sweep has no way to know which actions are live."
    )

    assert _has_skip_on_membership(sweep, container=_ACTIVE_SET_PARAM), (
        f"{_SWEEP_FN}: no `if <action_id> in {_ACTIVE_SET_PARAM}: ... continue` "
        "guard found. ADR-0009 F7 — without it a 30 s sweep tick emits "
        "action.timeout_assumed on an action a live turn is still driving, "
        "ending that turn with a limitation it did not earn."
    )


def test_canary_drive_turn_releases_live_actions_in_a_finally() -> None:
    """A crashed ``drive_turn`` MUST NOT pin its action_ids active forever."""
    module = parse(repo_root() / _RUNTIME_RELPATH)
    drive_turn = _find_function_def(module, name=_DRIVE_TURN_FN)
    assert drive_turn is not None, (
        f"{_RUNTIME_RELPATH} must define a top-level {_DRIVE_TURN_FN!r}."
    )

    released_in_finally = [
        try_node
        for try_node in ast.walk(drive_turn)
        if isinstance(try_node, ast.Try)
        and any(
            _iter_calls_with_name(stmt, name=_RELEASE_CALL)
            for stmt in try_node.finalbody
        )
    ]
    assert released_in_finally, (
        f"{_DRIVE_TURN_FN}: no `{_RELEASE_CALL}(...)` call inside a `finally:` "
        "block. ADR-0009 D4 pins the release there precisely because a turn "
        "that raises is the turn most likely to have orphaned an action: an "
        "entry nobody removes protects that orphan from the sweep for the "
        "life of the process."
    )

    # And nowhere else — a second release on the happy path would be
    # harmless, but a release that is ONLY on the happy path is the bug.
    all_release_calls = _iter_calls_with_name(drive_turn, name=_RELEASE_CALL)
    finally_release_calls = [
        call
        for try_node in released_in_finally
        for stmt in try_node.finalbody
        for call in _iter_calls_with_name(stmt, name=_RELEASE_CALL)
    ]
    assert len(all_release_calls) == len(finally_release_calls), (
        f"{_DRIVE_TURN_FN}: {len(all_release_calls)} `{_RELEASE_CALL}(...)` "
        f"call(s) but only {len(finally_release_calls)} inside a `finally:`. "
        "Every release site must be unconditional on the turn's outcome."
    )


def test_canary_sweep_reads_the_active_set_fresh_per_pass() -> None:
    """The sweep's caller MUST pass a live CALL, not a captured snapshot."""
    module = parse(repo_root() / _INHERENT_LOOP_RELPATH)
    runner = _find_function_def(module, name=_RUN_SWEEP_FN)
    assert runner is not None, (
        f"{_INHERENT_LOOP_RELPATH} must define {_RUN_SWEEP_FN!r} — the single "
        "chokepoint both the bootstrap sweep and the periodic task go "
        "through, so neither can drift from the freshness rule."
    )

    sweep_calls = _iter_calls_with_name(runner, name=_SWEEP_FN)
    assert sweep_calls, (
        f"{_RUN_SWEEP_FN}: no `{_SWEEP_FN}(...)` call found."
    )

    for call in sweep_calls:
        active_kw = next(
            (kw for kw in call.keywords if kw.arg == _ACTIVE_SET_PARAM),
            None,
        )
        assert active_kw is not None, (
            f"{_RUN_SWEEP_FN}: `{_SWEEP_FN}(...)` at line {call.lineno} does "
            f"not pass {_ACTIVE_SET_PARAM}=. Defaulting it to the empty "
            "frozenset would make the sweep close actions live turns own."
        )
        assert isinstance(active_kw.value, ast.Call), (
            f"{_RUN_SWEEP_FN}: {_ACTIVE_SET_PARAM}= at line {call.lineno} is "
            f"a {type(active_kw.value).__name__}, not a call. ADR-0009 D4 — "
            f"the set must be read fresh (`{_LIVE_SET_CALL}()`) on every "
            "pass; a name bound at wiring time keeps protecting actions from "
            "turns that ended long ago and misses every action dispatched "
            "since."
        )
        assert _call_name(active_kw.value) == _LIVE_SET_CALL, (
            f"{_RUN_SWEEP_FN}: {_ACTIVE_SET_PARAM}= is fed by "
            f"{_call_name(active_kw.value)!r}, expected {_LIVE_SET_CALL!r} — "
            "the L4-owned live set is the only source of truth for which "
            "actions a turn is driving."
        )
