"""Canary — CLI ack is printed (and flushed) BEFORE ``fork_detach`` is called.

Per ADR-0002 Step 11 / Step 17 Tier-1 canary list (lines 1632-1635):

    ``test_canary_daemon_ack_before_fork`` — AST scan ensures
    ``fork_detach()`` is preceded by a ``print(...)`` + ``sys.stdout.flush()``
    + that no ``bootstrap_runtime_app(...)`` precedes the fork in the
    parent code path.

This guards three slightly different concerns that all map to the same
ordering invariant:

1. **User experience** — Allen must see Jarvis acknowledged the request
   BEFORE the long-running detached path starts (which may take minutes
   to produce its first output line). The ack is the "I heard you" signal.

2. **Fork safety** — SQLite connections cannot cross ``os.fork()``. The
   parent process MUST hold zero SQLite handles when ``fork_detach`` is
   called. The canary enforces this by asserting NO call to
   ``bootstrap_runtime_app`` appears in the parent code path before the
   fork (per § Daemon / CLI contract hard rule line 1091-1094).

3. **Single ack site** — exactly one ``print(...)`` + ``sys.stdout.flush()``
   pair should precede the fork; spurious extra prints (e.g. log lines
   leaked into the parent path) would dilute the contract.

Scope: only the body of ``main_with_detach`` (the Day-2 CLI entry per
ADR-0002 lines 1064-1087). The body MUST contain an
``if _utterance_implies_long_run(...)`` block; within that block, the
fork_detach call MUST be lexically preceded (smaller line number) by a
``print(...)`` call AND a ``sys.stdout.flush()`` call, and MUST NOT be
preceded by any ``bootstrap_runtime_app(...)`` call.

ADR-0009 D2 amendment (§9 "Canary impact" — this canary is the one
listed as *affected*). The branch's precondition used to be "the
classifier matched"; the fork path is now additionally gated on the
LaunchAgent NOT being installed. With the agent installed, a detached
child races the launchd-respawned daemon: the child's action ids live
in its own process's live-action set, so the daemon's
``_system_trigger_watcher`` reads the child's terminal rows as orphans
and drives a second turn for the same action. The ordering assertions
above are unchanged; :func:`test_canary_fork_gated_on_agent_not_installed`
adds the new half of the precondition so the guard cannot be dropped
without the canary noticing.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_CLI_MODULE_RELPATH: str = "jarvis/cli/__init__.py"
_ENTRY_FUNCTION_NAME: str = "main_with_detach"
_FORK_CALL_NAME: str = "fork_detach"
_BOOTSTRAP_CALL_NAME: str = "bootstrap_runtime_app"
_FLUSH_TARGET_ATTR: str = "flush"
_FLUSH_TARGET_STREAM: str = "stdout"
# ADR-0009 D2: the agent-installed probe that must gate the fork.
_AGENT_INSTALLED_CALL_NAME: str = "is_agent_installed"


def _find_function_def(module: ast.Module, *, name: str) -> ast.FunctionDef | None:
    """Return the top-level ``def <name>(...)`` node, or ``None``."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _call_name(call: ast.Call) -> str | None:
    """Return the dotted-name suffix of ``call.func`` (``a.b.c`` -> ``"c"``).

    Bare names return their id; attribute calls return the attribute
    name. We match on the suffix so ``fork_detach()`` matches whether
    the source spells it ``fork_detach(...)`` or
    ``jarvis.runtime.daemon.fork_detach(...)`` (Day-2 uses the former
    via a local lazy import).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_calls_with_name(node: ast.AST, *, name: str) -> list[ast.Call]:
    """Return every ``ast.Call`` under ``node`` whose suffix == ``name``."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and _call_name(sub) == name
    ]


def _is_sys_stdout_flush_call(call: ast.Call) -> bool:
    """Return True for ``sys.stdout.flush(...)`` (or any ``stdout.flush(...)``).

    We match the shape ``<expr>.stdout.flush(...)`` so it accepts
    ``sys.stdout.flush()`` (the canonical form per the ADR sketch) and
    also tolerates a future ``getattr(sys, "stdout").flush()`` would
    not match, which is the intended strictness.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != _FLUSH_TARGET_ATTR:
        return False
    target = func.value
    if not isinstance(target, ast.Attribute):
        return False
    return target.attr == _FLUSH_TARGET_STREAM


def _find_long_run_branch(func: ast.FunctionDef) -> ast.If:
    """Return the ``if`` node whose test contains ``_utterance_implies_long_run``.

    The Day-2 CLI body has a single such top-level branch; we walk the
    function body (not nested defs) to find it. Raises AssertionError
    via the calling test when no such branch exists (i.e. the regex
    classifier was removed or the entry was rewritten).
    """
    for stmt in func.body:
        if not isinstance(stmt, ast.If):
            continue
        test_calls = _iter_calls_with_name(
            stmt.test, name="_utterance_implies_long_run",
        )
        if test_calls:
            return stmt
    msg = (
        f"{_CLI_MODULE_RELPATH}: {_ENTRY_FUNCTION_NAME!r} body must contain "
        "an `if _utterance_implies_long_run(...):` branch — the classifier "
        "is the Day-2 fork gate per ADR-0002 line 1070."
    )
    raise AssertionError(msg)


def test_canary_long_run_branch_prints_and_flushes_before_fork() -> None:
    """Ack ``print(...)`` + ``sys.stdout.flush()`` MUST precede ``fork_detach``."""
    module = parse(repo_root() / _CLI_MODULE_RELPATH)
    entry = _find_function_def(module, name=_ENTRY_FUNCTION_NAME)
    assert entry is not None, (
        f"{_CLI_MODULE_RELPATH} must define a top-level `def {_ENTRY_FUNCTION_NAME}` "
        "per ADR-0002 § Daemon / CLI contract."
    )

    long_run_branch = _find_long_run_branch(entry)

    fork_calls = _iter_calls_with_name(long_run_branch, name=_FORK_CALL_NAME)
    assert fork_calls, (
        f"{_ENTRY_FUNCTION_NAME}: long-run branch must call {_FORK_CALL_NAME}(...). "
        "Day-2 fork-detach is the whole point of the long-run path."
    )
    first_fork_lineno = min(call.lineno for call in fork_calls)

    print_calls = _iter_calls_with_name(long_run_branch, name="print")
    print_lines_before = [
        call.lineno for call in print_calls if call.lineno < first_fork_lineno
    ]
    assert print_lines_before, (
        f"{_ENTRY_FUNCTION_NAME}: long-run branch must include a print(...) "
        f"BEFORE the {_FORK_CALL_NAME}() call at line {first_fork_lineno}. "
        "The ack is the operator's 'I heard you' signal per ADR-0002 line 1072."
    )

    flush_lines_before = [
        call.lineno
        for call in ast.walk(long_run_branch)
        if isinstance(call, ast.Call)
        and _is_sys_stdout_flush_call(call)
        and call.lineno < first_fork_lineno
    ]
    assert flush_lines_before, (
        f"{_ENTRY_FUNCTION_NAME}: long-run branch must call sys.stdout.flush() "
        f"BEFORE the {_FORK_CALL_NAME}() call at line {first_fork_lineno}. "
        "Without flush the ack would stay buffered until the parent exited "
        "and could be lost on the fork."
    )


def test_canary_fork_gated_on_agent_not_installed() -> None:
    """The long-run branch MUST also require the LaunchAgent to be absent.

    ADR-0009 D2. The guard has to sit in the branch TEST (not merely in
    ``_main_oneshot``'s dispatch) because ``main_with_detach`` is a
    public entry point: anything calling it directly must not be able to
    fork a child alongside a resident daemon.
    """
    module = parse(repo_root() / _CLI_MODULE_RELPATH)
    entry = _find_function_def(module, name=_ENTRY_FUNCTION_NAME)
    assert entry is not None

    long_run_branch = _find_long_run_branch(entry)
    guard_calls = _iter_calls_with_name(
        long_run_branch.test, name=_AGENT_INSTALLED_CALL_NAME,
    )
    assert guard_calls, (
        f"{_ENTRY_FUNCTION_NAME}: the long-run branch test must call "
        f"{_AGENT_INSTALLED_CALL_NAME}(...) so fork-detach is disabled while "
        "the LaunchAgent is installed (ADR-0009 D2). Without it a detached "
        "child races the respawned daemon and its terminal events look like "
        "orphans to _system_trigger_watcher, which drives a duplicate turn."
    )

    negated = any(
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and _iter_calls_with_name(node.operand, name=_AGENT_INSTALLED_CALL_NAME)
        for node in ast.walk(long_run_branch.test)
    )
    assert negated, (
        f"{_ENTRY_FUNCTION_NAME}: {_AGENT_INSTALLED_CALL_NAME}(...) must appear "
        "NEGATED in the branch test (`not launchd.is_agent_installed()`); the "
        "fork path is the daemon-NOT-installed path."
    )


def test_canary_no_bootstrap_runtime_before_fork() -> None:
    """No ``bootstrap_runtime_app(...)`` call MUST appear before ``fork_detach``."""
    module = parse(repo_root() / _CLI_MODULE_RELPATH)
    entry = _find_function_def(module, name=_ENTRY_FUNCTION_NAME)
    assert entry is not None

    long_run_branch = _find_long_run_branch(entry)

    fork_calls = _iter_calls_with_name(long_run_branch, name=_FORK_CALL_NAME)
    assert fork_calls
    first_fork_lineno = min(call.lineno for call in fork_calls)

    bootstrap_calls = _iter_calls_with_name(
        long_run_branch, name=_BOOTSTRAP_CALL_NAME,
    )
    bootstrap_before = [
        call.lineno
        for call in bootstrap_calls
        if call.lineno < first_fork_lineno
    ]
    assert not bootstrap_before, (
        f"{_ENTRY_FUNCTION_NAME}: {_BOOTSTRAP_CALL_NAME}(...) called at line(s) "
        f"{bootstrap_before} BEFORE {_FORK_CALL_NAME}() at line {first_fork_lineno}. "
        "ADR-0002 § Daemon / CLI contract hard rule (lines 1091-1094): the "
        "parent process must hold zero SQLite handles when fork_detach() is "
        "called. Move the bootstrap into the child branch."
    )
