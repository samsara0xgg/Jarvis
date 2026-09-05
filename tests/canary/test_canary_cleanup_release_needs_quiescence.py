"""Canary — only the ActionRunner releases a lease, and only after quiescence.

ADR-0008 D9 (Step 4) splits three facts that used to be collapsed: the
canonical action terminal, worker quiescence, and repository cleanup. Once
``spawn_worker`` runs truly in the background, the driver's ``finally`` can
reach the cleanup site while the worker is still writing to the tree, so the
release had to move behind a quiescence gate the runner owns.

Three structural properties are pinned here, all by AST scan:

1. **Nobody outside the runner releases a lease.** ``ResourceLeaseTable.
   release`` may be called only from ``jarvis/execution/action_runner.py``.
   A release from ``jarvis/runtime`` or from a tool handler would be a
   process that frees a repository without knowing whether the worker
   holding it has stopped.
2. **The driver asks; it does not finalize.** ``drive_turn`` may call
   ``finalize_turn_cleanup`` (the request) but never ``finalize_cleanup``
   (the write). The distinction is the whole of Step 4's ownership move: the
   request is safe at any time, the write is not.
3. **The write is reachable only through the quiescence gate.** Inside the
   runner, ``finalize_cleanup`` may be called only from
   ``_run_turn_cleanup_if_ready``, and that function must consult
   ``self._inflight`` before it finalizes anything. This is the one property
   here that Wave 5 actually *changed*: at the Step-4 parent commit
   ``finalize_turn_cleanup`` called ``finalize_cleanup`` itself, with no gate
   at all. Properties 1 and 2 already held there, so they are forward-looking
   anti-bypass guards rather than pins on this step.

The behavioural half — that an armed request really does wait — lives in
``tests/integration/test_wave5_background_actions.py``. This canary exists
because that behaviour is easy to reintroduce a bypass around: a single
``leases.release(...)`` in the composition root would restore the Wave-4B
bug with every integration test still green. Both scans cover ``async def``
as well as ``def``, so a coroutine cannot be the hiding place.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, repo_root

if TYPE_CHECKING:
    from pathlib import Path

_RUNNER_MODULE_RELPATH: str = "jarvis/execution/action_runner.py"
_DRIVE_TURN_NAME: str = "drive_turn"
_RUNTIME_MODULE_RELPATH: str = "jarvis/runtime/__init__.py"
_RELEASE_ATTR: str = "release"
_LEASE_RECEIVERS: frozenset[str] = frozenset({"leases", "_leases"})
_CLEANUP_WRITE_ATTR: str = "finalize_cleanup"
_QUIESCENCE_GATE_NAME: str = "_run_turn_cleanup_if_ready"
_INFLIGHT_ATTR: str = "_inflight"


def _call_attr(call: ast.Call) -> tuple[str, str] | None:
    """Return ``(receiver, attribute)`` for ``<name>.<attr>(...)`` calls."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id, func.attr
    if isinstance(value, ast.Attribute):
        return value.attr, func.attr
    return None


def _defs(node: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function definition under ``node``, sync and async alike.

    Both kinds are walked because a bypass is a bypass: an ``async def`` that
    releases a lease frees the same repository as a ``def`` that does, and a
    scan that only knew ``ast.FunctionDef`` would wave it through.
    """
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _lease_release_calls(module: ast.Module) -> list[int]:
    """Return the line numbers of every ``<lease-table>.release(...)`` call."""
    return [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and (pair := _call_attr(node)) is not None
        and pair[0] in _LEASE_RECEIVERS
        and pair[1] == _RELEASE_ATTR
    ]


def _python_sources() -> list[Path]:
    """Return every production module under ``jarvis/``."""
    return sorted(p for p in (repo_root() / "jarvis").rglob("*.py"))


def test_canary_only_the_runner_releases_a_resource_lease() -> None:
    """No module but the ActionRunner may free a repository lease."""
    offenders: dict[str, list[int]] = {}
    for path in _python_sources():
        relative = path.relative_to(repo_root()).as_posix()
        if relative == _RUNNER_MODULE_RELPATH:
            continue
        lines = _lease_release_calls(parse(path))
        if lines:
            offenders[relative] = lines

    assert not offenders, (
        f"ADR-0008 D9: lease release(s) outside {_RUNNER_MODULE_RELPATH!r}: "
        f"{offenders!r}. Only the ActionRunner knows whether the worker that "
        "holds a repository has quiesced; a release anywhere else hands a "
        "tree that is still being written to the next same-repo action."
    )


def test_canary_runner_release_sites_are_the_two_owned_ones() -> None:
    """The runner itself releases in exactly two places, both quiescence-gated.

    ``_quiesce`` frees a scope that carries no cleanup debt (read-shared or
    borrowed), and ``finalize_cleanup`` frees a write-exclusive one after the
    cleanup terminal. A third site would mean a path that skipped one of the
    two gates.
    """
    module = parse(repo_root() / _RUNNER_MODULE_RELPATH)
    owners = {
        node.name
        for node in _defs(module)
        if _lease_release_calls(ast.Module(body=list(node.body), type_ignores=[]))
    }
    assert owners == {"_quiesce", "finalize_cleanup"}, (
        f"ADR-0008 D9: lease release moved. Expected exactly `_quiesce` and "
        f"`finalize_cleanup` to release; found {sorted(owners)!r}."
    )


def _callers_of(module: ast.Module, *, attribute: str) -> set[str]:
    """Return the names of the functions containing a ``*.<attribute>(...)`` call."""
    return {
        node.name
        for node in _defs(module)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (pair := _call_attr(call)) is not None
        and pair[1] == attribute
    }


def test_canary_the_cleanup_write_is_reachable_only_through_the_gate() -> None:
    """``finalize_cleanup`` is called from the quiescence gate and nowhere else.

    The Step-4 parent commit had ``finalize_turn_cleanup`` call
    ``finalize_cleanup`` inline and unconditionally, so a driver whose turn
    was over could close a background worker's cleanup debt while that worker
    was still writing. Step 4 interposes ``_run_turn_cleanup_if_ready``, which
    refuses while any action of the turn is in flight. Two facts keep that
    interposition real: nothing else may call the write, and the gate must
    actually read the in-flight table.
    """
    module = parse(repo_root() / _RUNNER_MODULE_RELPATH)
    callers = _callers_of(module, attribute=_CLEANUP_WRITE_ATTR)
    assert callers == {_QUIESCENCE_GATE_NAME}, (
        f"ADR-0008 D9 (Step 4): {_CLEANUP_WRITE_ATTR!r} must be called only "
        f"from {_QUIESCENCE_GATE_NAME!r}; found {sorted(callers)!r}. Any other "
        "caller is a path that closes cleanup debt without first proving the "
        "turn's workers have stopped."
    )

    gate = next(
        (node for node in _defs(module) if node.name == _QUIESCENCE_GATE_NAME),
        None,
    )
    assert gate is not None, (
        f"{_RUNNER_MODULE_RELPATH} must define {_QUIESCENCE_GATE_NAME!r} — it "
        "is the gate the cleanup write sits behind."
    )
    reads_inflight = any(
        isinstance(node, ast.Attribute) and node.attr == _INFLIGHT_ATTR
        for node in ast.walk(gate)
    )
    assert reads_inflight, (
        f"{_QUIESCENCE_GATE_NAME} never reads `self.{_INFLIGHT_ATTR}`. Without "
        "that read it is a gate in name only: it would finalize a turn whose "
        "background worker is still running, which is the Wave-4B bug Step 4 "
        "exists to remove."
    )


def _find_function_def(
    module: ast.Module,
    *,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the top-level ``(async) def <name>(...)`` node, or ``None``."""
    return next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ),
        None,
    )


def test_canary_drive_turn_requests_cleanup_and_never_writes_it() -> None:
    """``drive_turn`` calls the request, not the write."""
    module = parse(repo_root() / _RUNTIME_MODULE_RELPATH)
    drive_turn = _find_function_def(module, name=_DRIVE_TURN_NAME)
    assert drive_turn is not None, (
        f"{_RUNTIME_MODULE_RELPATH} must define a top-level {_DRIVE_TURN_NAME!r}."
    )

    called = {
        pair[1]
        for node in ast.walk(drive_turn)
        if isinstance(node, ast.Call) and (pair := _call_attr(node)) is not None
    }
    assert "finalize_turn_cleanup" in called, (
        f"{_DRIVE_TURN_NAME}: no `finalize_turn_cleanup(...)` call. The driver "
        "still owns asking for its turn's cleanup, even though it no longer "
        "owns performing it."
    )
    assert "finalize_cleanup" not in called, (
        f"{_DRIVE_TURN_NAME}: calls `finalize_cleanup(...)` directly. That is "
        "the write, and ADR-0008 D9 (Step 4) gives it to the runner: a driver "
        "that writes the cleanup terminal itself can do so while a background "
        "worker is still running."
    )
