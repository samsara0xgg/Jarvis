"""Wave 4B acceptance: ActionRunner, resource leases, quiescence and cleanup.

ADR-0008 §8 Step 3 (D9, §4.2, §6, §9.4). Every check runs against a real
on-disk Event Log, the real :class:`~jarvis.execution.tools.ToolRegistry` and
the real :class:`~jarvis.execution.action_runner.ActionRunner`; only the tool
handlers are fixtures, because the property under test is the execution
boundary rather than any particular tool's body.

The headline properties are the three the ADR row names:
``test_unrelated_same_repo_action_waits_for_cleanup`` (a repository stays
quarantined through verify-then-restore), ``test_child_borrows_parent_scope_
without_self_wait`` (a verification child never blocks on the lease its own
turn holds), and ``test_cancel_and_result_cannot_both_terminalize`` (one
canonical terminal, whoever wins).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis import runtime as runtime_module
from jarvis.decision.gates import ResponsePlan
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.action_runner import (
    ActionRunner,
    CancelAccepted,
    CancelAlreadyTerminal,
    CancelUnsupported,
    ResourceKeyResolutionError,
    ResourceLeaseTable,
    ResourceScopeEscalationError,
    ToolConcurrency,
    UnknownResourceScopeError,
    current_execution_context,
)
from jarvis.execution.tools import (
    ActionLifecycle,
    RawResult,
    ToolDefinition,
    ToolRegistry,
    canonical_resource_key,
    default_resource_key_resolver,
    turn_action_ids,
)
from jarvis.runtime import JarvisRuntime, _wave4_action_flags
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.shared.realtime import Wave4ActionFlags
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_action
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.execution.tools import RuntimePathsLike
    from jarvis.shared import RawResultBundle


# --- harness ---------------------------------------------------------------


def _event_count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


def _payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def _ordered_types(conn: sqlite3.Connection) -> list[str]:
    """Return every event type in append order."""
    return [str(row[0]) for row in conn.execute("SELECT type FROM events ORDER BY id ASC")]


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> bool:
    """Poll ``predicate`` until true or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _request(
    tool_name: str,
    action_id: str,
    *,
    turn_id: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> ActionRequest:
    """Build one ActionRequest for a fixture tool."""
    return ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L1",
        arguments=arguments if arguments is not None else {},
        authorization_lease=None,
        run_id=None,
        turn_id=turn_id,
    )


def _fixture_tool(
    name: str,
    body: Callable[[ActionRequest, sqlite3.Connection], RawResult],
    *,
    read_only: bool = False,
    cancellation_mode: str = "unsupported",
) -> ToolDefinition:
    """Register-able ToolDefinition around a plain callable body."""

    def _handler(
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        _runtime_paths: RuntimePathsLike,
        _lifecycle: ActionLifecycle,
    ) -> RawResult:
        return body(action_request, conn)

    return ToolDefinition(
        name=name,
        description=f"fixture tool {name}",
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        result_semantics="ack",
        is_async=False,
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=_handler,
        domain="test",
        read_only=read_only,
        requires_entity=False,
        requires_confirmation=False,
        cancellation_mode=cancellation_mode,  # type: ignore[arg-type]
    )


def _ack(action_request: ActionRequest) -> RawResult:
    """Return a bare ack slot, emitting no terminal of its own."""
    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload={"ok": True},
        tool_output="{}",
        error=None,
    )


def _authorize(lifecycle: ActionLifecycle, action_id: str) -> None:
    """Bring one action to the state ``dispatch`` requires."""
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")


def _fixed_resolver(
    mapping: dict[str, ToolConcurrency],
) -> Callable[[ActionRequest, ToolDefinition, sqlite3.Connection], ToolConcurrency]:
    """Return a resolver that answers from a per-tool-name table."""

    def _resolve(
        action_request: ActionRequest,
        tool_def: ToolDefinition,
        conn: sqlite3.Connection,
    ) -> ToolConcurrency:
        declared = mapping.get(tool_def.name)
        if declared is None:
            return default_resource_key_resolver(action_request, tool_def, conn)
        return declared

    return _resolve


class _Fixture:
    """One runtime root, registry, runner and lifecycle wired together."""

    def __init__(  # noqa: PLR0913 — five independent knobs, each exercised by a different acceptance row.
        self,
        tmp_path: Path,
        *,
        tools: tuple[ToolDefinition, ...],
        with_runner: bool = True,
        max_concurrent_runs: int = 4,
        lease_timeout_s: float = 5.0,
        resolver: Callable[
            [ActionRequest, ToolDefinition, sqlite3.Connection],
            ToolConcurrency,
        ]
        | None = None,
    ) -> None:
        """Build the runtime paths, the Event Log and the registry."""
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.runner = (
            ActionRunner(
                event_log_path=self.paths.event_log,
                max_concurrent_runs=max_concurrent_runs,
                lease_timeout_s=lease_timeout_s,
            )
            if with_runner
            else None
        )
        self.registry = ToolRegistry(
            action_runner=self.runner,
            resource_key_resolver=resolver,
        )
        for tool in tools:
            self.registry.register(tool)
        self.lifecycle = ActionLifecycle()

    def dispatch(self, request: ActionRequest) -> RawResultBundle:
        """Authorize and dispatch one request on the caller's thread."""
        _authorize(self.lifecycle, request.action_id)
        return self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)

    def submit(self, request: ActionRequest) -> Any:  # noqa: ANN401 - ActionSubmission.
        """Authorize and submit one request, returning its live submission."""
        _authorize(self.lifecycle, request.action_id)
        return self.registry.submit(request, self.conn, self.paths, self.lifecycle)

    def close(self) -> None:
        """Drain the runner and close the Event Log connection."""
        if self.runner is not None:
            self.runner.shutdown()
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


@pytest.fixture
def repo_a(tmp_path: Path) -> Path:
    """One canonical repository path."""
    path = tmp_path / "repo-a"
    path.mkdir()
    return path


@pytest.fixture
def repo_b(tmp_path: Path) -> Path:
    """A second, unrelated canonical repository path."""
    path = tmp_path / "repo-b"
    path.mkdir()
    return path


# --- B1: flag-off parity ----------------------------------------------------


def test_inline_dispatch_is_unchanged_without_a_runner(tmp_path: Path) -> None:
    """No runner: the handler runs on the calling thread, with no lease rows."""
    caller_thread = threading.get_ident()
    seen: list[int] = []

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        seen.append(threading.get_ident())
        return _ack(request)

    fixture = _Fixture(tmp_path, tools=(_fixture_tool("inline", _body),), with_runner=False)
    try:
        bundle = fixture.dispatch(_request("inline", "A-inline", turn_id="T-inline"))
        assert bundle.slots[0].semantics == "ack"
        assert seen == [caller_thread]
        assert _ordered_types(fixture.conn) == ["action.dispatched", "action.running"]
        assert _payloads(fixture.conn, "action.running")[0] == {"action_id": "A-inline"}
        assert _event_count(fixture.conn, "worker.quiesced") == 0
        assert fixture.lifecycle.state_of("A-inline") == "running"
    finally:
        fixture.close()


# --- B2: runner path --------------------------------------------------------


def test_runner_path_records_the_lease_and_quiesces(tmp_path: Path, repo_a: Path) -> None:
    """The handler moves off the caller's thread; lease + quiescence are durable."""
    caller_thread = threading.get_ident()
    seen: list[int] = []

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        seen.append(threading.get_ident())
        return _ack(request)

    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("mutating", _body),),
        resolver=_fixed_resolver(
            {"mutating": ToolConcurrency(resource_keys=(key,), mode="write_exclusive")},
        ),
    )
    try:
        bundle = fixture.dispatch(_request("mutating", "A-run", turn_id="T-run"))
        assert bundle.slots[0].semantics == "ack"
        assert seen
        assert seen[0] != caller_thread
        assert _ordered_types(fixture.conn) == [
            "action.dispatched",
            "action.running",
            "worker.quiesced",
        ]
        running = _payloads(fixture.conn, "action.running")[0]
        assert running["resource_keys"] == key
        assert running["resource_mode"] == "write_exclusive"
        quiesced = _payloads(fixture.conn, "worker.quiesced")[0]
        assert quiesced == {"action_id": "A-run", "worker_epoch": 0}
        assert fixture.lifecycle.state_of("A-run") == "running"
        assert fixture.runner is not None
        # Mutating + turn-owned: the lease survives quiescence as cleanup debt.
        assert [s.action_id for s in fixture.runner.leases.live_scopes()] == ["A-run"]

        emitted = fixture.runner.finalize_turn_cleanup(
            "T-run",
            verification_outcome="verification_skipped",
        )
        assert len(emitted) == 1
        cleanup = _payloads(fixture.conn, "action.cleanup_completed")[0]
        assert cleanup["action_id"] == "A-run"
        assert cleanup["worker_epoch"] == 0
        assert cleanup["verification_outcome"] == "verification_skipped"
        assert cleanup["resource_keys"] == key
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        fixture.close()


def test_read_only_action_takes_no_lease(tmp_path: Path) -> None:
    """A read-only tool needs no resource key, so it never contends."""
    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("peek", lambda r, _c: _ack(r), read_only=True),),
    )
    try:
        fixture.dispatch(_request("peek", "A-peek", turn_id="T-peek"))
        assert _payloads(fixture.conn, "action.running")[0]["resource_keys"] == ""
        assert fixture.runner is not None
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        fixture.close()


# --- B3: serialization and parallelism --------------------------------------


def _gated_tool(
    name: str,
    entered: threading.Event,
    release: threading.Event,
) -> ToolDefinition:
    """A tool that announces it started and then blocks until released."""

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        entered.set()
        release.wait(timeout=10)
        return _ack(request)

    return _fixture_tool(name, _body)


def test_unrelated_same_repo_action_waits_for_cleanup(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """Headline: a second turn's same-repo action is blocked through cleanup.

    Quiescence is not enough — the repository is still mid verify-then-restore
    when the worker stops. Only the cleanup terminal frees it (ADR-0008 F18 /
    F26 / F23).
    """
    key = canonical_resource_key(repo_a)
    first_entered, first_release = threading.Event(), threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(
            _gated_tool("first", first_entered, first_release),
            _fixture_tool("second", lambda r, _c: _ack(r)),
        ),
        resolver=_fixed_resolver(
            {
                "first": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
                "second": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
            },
        ),
    )
    try:
        assert fixture.runner is not None
        first = fixture.submit(_request("first", "A-first", turn_id="T-1"))
        assert first_entered.wait(timeout=5)

        second = fixture.submit(_request("second", "A-second", turn_id="T-2"))
        first_release.set()
        assert first.handle.result(timeout=10).slots[0].semantics == "ack"
        assert _wait_until(lambda: _event_count(fixture.conn, "worker.quiesced") == 1)

        # Quiesced, but not cleaned up: the second action must still be parked.
        time.sleep(0.2)
        assert not second.handle.is_done()
        running_ids = [p["action_id"] for p in _payloads(fixture.conn, "action.running")]
        assert running_ids == ["A-first"]

        fixture.runner.finalize_cleanup("A-first", verification_outcome="verified")
        assert second.handle.result(timeout=10).slots[0].semantics == "ack"

        types = _ordered_types(fixture.conn)
        running_positions = [i for i, name in enumerate(types) if name == "action.running"]
        assert len(running_positions) == 2
        assert types.index("action.cleanup_completed") < running_positions[1]
        assert [p["action_id"] for p in _payloads(fixture.conn, "action.running")] == [
            "A-first",
            "A-second",
        ]
    finally:
        first_release.set()
        fixture.close()


def test_different_repo_actions_run_in_parallel(
    tmp_path: Path,
    repo_a: Path,
    repo_b: Path,
) -> None:
    """Two write-exclusive actions on different repos overlap in time."""
    barrier = threading.Barrier(2)

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        # Only reachable if both actions genuinely hold their leases at once.
        barrier.wait(timeout=5)
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("repo_a", _body), _fixture_tool("repo_b", _body)),
        max_concurrent_runs=2,
        resolver=_fixed_resolver(
            {
                "repo_a": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
                "repo_b": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_b),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        a = fixture.submit(_request("repo_a", "A-a", turn_id="T-a"))
        b = fixture.submit(_request("repo_b", "A-b", turn_id="T-b"))
        assert a.handle.result(timeout=10).slots[0].semantics == "ack"
        assert b.handle.result(timeout=10).slots[0].semantics == "ack"
    except threading.BrokenBarrierError:  # pragma: no cover - the failure mode
        pytest.fail("different-repo actions serialized instead of running in parallel")
    finally:
        fixture.close()


def test_child_borrows_parent_scope_without_self_wait(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """A same-turn verification child borrows instead of blocking on its parent.

    ADR-0008 F26: the child runs while the parent's write-exclusive lease is
    still quarantining the repository, releases nothing, and an unrelated
    turn's same-repo action stays blocked behind the root cleanup.
    """
    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(
            _fixture_tool("parent", lambda r, _c: _ack(r)),
            _fixture_tool("child", lambda r, _c: _ack(r), read_only=True),
            _fixture_tool("stranger", lambda r, _c: _ack(r)),
        ),
        resolver=_fixed_resolver(
            {
                "parent": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
                "child": ToolConcurrency(resource_keys=(key,), mode="read_shared"),
                "stranger": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
            },
        ),
    )
    try:
        assert fixture.runner is not None
        fixture.dispatch(_request("parent", "A-parent", turn_id="T-same"))
        assert [s.action_id for s in fixture.runner.leases.live_scopes()] == ["A-parent"]

        # Same turn, same repo, no declared parent: the runner's no-self-wait
        # guard must resolve this to a borrow rather than a lease wait.
        started = time.monotonic()
        fixture.dispatch(_request("child", "A-child", turn_id="T-same"))
        assert time.monotonic() - started < 2.0
        # The borrow released nothing: the parent still owns the repository.
        assert [s.action_id for s in fixture.runner.leases.live_scopes()] == ["A-parent"]
        assert _event_count(fixture.conn, "action.cleanup_completed") == 0

        stranger = fixture.submit(_request("stranger", "A-stranger", turn_id="T-other"))
        time.sleep(0.2)
        assert not stranger.handle.is_done()

        fixture.runner.finalize_turn_cleanup("T-same", verification_outcome="verified")
        assert stranger.handle.result(timeout=10).slots[0].semantics == "ack"
        # Exactly one cleanup: the borrowed child never carried its own debt.
        assert _event_count(fixture.conn, "action.cleanup_completed") == 1
    finally:
        fixture.close()


# --- B4: resolver and scope escalation --------------------------------------


def test_resource_key_resolution_failure_never_runs_the_handler(tmp_path: Path) -> None:
    """A `spawn_worker` with no task_id fails as an action, not unlocked."""
    ran: list[str] = []

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        ran.append(request.action_id)
        return _ack(request)

    fixture = _Fixture(tmp_path, tools=(_fixture_tool("spawn_worker", _body),))
    try:
        with pytest.raises(ResourceKeyResolutionError):
            fixture.dispatch(_request("spawn_worker", "A-noresolve", turn_id="T-x"))
        assert ran == []
        assert _ordered_types(fixture.conn) == ["action.dispatched", "action.failed"]
        failed = _payloads(fixture.conn, "action.failed")[0]
        assert failed["error"] == "resource_key_resolution"
        assert fixture.lifecycle.state_of("A-noresolve") == "failed"
        assert fixture.runner is not None
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("keys", "mode", "expected"),
    [
        (("k1", "k2"), "read_shared", "extra key"),
        (("k1",), "global_exclusive", "escalate"),
    ],
)
def test_child_scope_escalation_is_locked_out(
    keys: tuple[str, ...],
    mode: str,
    expected: str,
) -> None:
    """A borrow may narrow its parent's scope, never widen or strengthen it."""
    leases = ResourceLeaseTable()
    leases.acquire(
        action_id="A-root",
        keys=frozenset({"k1"}),
        mode="write_exclusive",
        timeout_s=1.0,
    )
    with pytest.raises(ResourceScopeEscalationError):
        leases.borrow(
            action_id="A-child",
            keys=frozenset(keys),
            mode=mode,  # type: ignore[arg-type]
            parent_action_id="A-root",
        )
    assert expected  # names the refused shape in the failure report
    assert [s.action_id for s in leases.live_scopes()] == ["A-root"]


def test_borrow_from_an_unknown_parent_is_refused() -> None:
    """A child that names no live scope gets no lease at all."""
    leases = ResourceLeaseTable()
    with pytest.raises(UnknownResourceScopeError):
        leases.borrow(
            action_id="A-orphan",
            keys=frozenset({"k1"}),
            mode="read_shared",
            parent_action_id="A-missing",
        )
    assert leases.live_scopes() == ()


def test_releasing_a_borrowed_scope_never_frees_the_root() -> None:
    """The child holds no token, so its release cannot unblock the repository."""
    leases = ResourceLeaseTable()
    root = leases.acquire(
        action_id="A-root",
        keys=frozenset({"k1"}),
        mode="write_exclusive",
        timeout_s=1.0,
    )
    child = leases.borrow(
        action_id="A-child",
        keys=frozenset({"k1"}),
        mode="read_shared",
        parent_action_id="A-root",
    )
    assert child.is_borrowed
    leases.release(child)
    assert [s.scope_id for s in leases.live_scopes()] == [root.scope_id]
    leases.release(root)
    assert leases.live_scopes() == ()


def test_undeclared_mutating_tool_defaults_to_global_exclusive(tmp_path: Path) -> None:
    """The fail-closed default conflicts with every other lease."""
    conn = open_event_log(tmp_path / "resolve.db")
    try:
        tool = _fixture_tool("mutator", lambda r, _c: _ack(r))
        resolved = default_resource_key_resolver(_request("mutator", "A-1"), tool, conn)
        assert resolved.mode == "global_exclusive"
        assert resolved.resource_keys == ("*",)

        read_only = _fixture_tool("peek", lambda r, _c: _ack(r), read_only=True)
        assert default_resource_key_resolver(
            _request("peek", "A-2"),
            read_only,
            conn,
        ) == ToolConcurrency(resource_keys=(), mode="read_shared")
    finally:
        conn.close()


# --- B5: result / cancel / timeout races ------------------------------------


def _cooperative_tool(name: str, entered: threading.Event) -> ToolDefinition:
    """A tool that polls its execution context and stops when asked to."""

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        entered.set()
        context = current_execution_context()
        assert context is not None, "a runner job must expose its execution context"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if context.is_cancel_requested:
                return RawResult(
                    action_id=request.action_id,
                    semantics="error",
                    payload={"stopped": True},
                    tool_output="{}",
                    error="cancelled",
                )
            time.sleep(0.005)
        return _ack(request)

    return _fixture_tool(name, _body, cancellation_mode="cooperative")


def test_cancel_is_refused_for_a_tool_that_cannot_stop(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """ADR-0008 F9: no `action.cancelled` is ever written for an unsupported tool."""
    entered, release = threading.Event(), threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(_gated_tool("stubborn", entered, release),),
        resolver=_fixed_resolver(
            {
                "stubborn": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        submission = fixture.submit(_request("stubborn", "A-stubborn", turn_id="T-s"))
        assert entered.wait(timeout=5)
        outcome = submission.handle.cancel("user_stop", timeout_s=0.2)
        assert isinstance(outcome, CancelUnsupported)
        assert _event_count(fixture.conn, "action.cancelled") == 0
        assert not submission.handle.is_done()
        release.set()
        assert submission.handle.result(timeout=10).slots[0].semantics == "ack"
    finally:
        release.set()
        fixture.close()


def test_cooperative_cancel_terminalizes_once_after_quiescence(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """A confirmed-quiescent cancel writes exactly one canonical terminal."""
    entered = threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(_cooperative_tool("stoppable", entered),),
        resolver=_fixed_resolver(
            {
                "stoppable": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        submission = fixture.submit(_request("stoppable", "A-stop", turn_id="T-stop"))
        assert entered.wait(timeout=5)
        outcome = submission.handle.cancel("user_stop", timeout_s=5.0)
        assert isinstance(outcome, CancelAccepted)
        assert submission.handle.result(timeout=10).slots[0].error == "cancelled"

        cancelled = _payloads(fixture.conn, "action.cancelled")
        assert len(cancelled) == 1
        assert cancelled[0] == {"action_id": "A-stop", "reason": "user_stop"}
        assert _event_count(fixture.conn, "action.result_observed") == 0
        # Quiescence is recorded before the terminal is allowed to be true.
        types = _ordered_types(fixture.conn)
        assert types.index("worker.quiesced") < types.index("action.cancelled")

        # A repeat cancel finds the terminal already there and writes nothing.
        repeat = submission.handle.cancel("user_stop", timeout_s=1.0)
        assert isinstance(repeat, CancelAlreadyTerminal)
        assert repeat.terminal_type == "action.cancelled"
        assert _event_count(fixture.conn, "action.cancelled") == 1
    finally:
        fixture.close()


def test_a_completed_result_beats_a_later_cancel(tmp_path: Path, repo_a: Path) -> None:
    """ADR-0008 F8: the result won the CAS, so cancel reports already-terminal."""

    def _body(request: ActionRequest, conn: sqlite3.Connection) -> RawResult:
        terminalize_action(
            conn,
            event_type="action.result_observed",
            payload={"action_id": request.action_id, "semantics": "ack"},
            correlation={"action_id": request.action_id},
        )
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("fast", _body, cancellation_mode="cooperative"),),
        resolver=_fixed_resolver(
            {
                "fast": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        submission = fixture.submit(_request("fast", "A-fast", turn_id="T-fast"))
        assert submission.handle.result(timeout=10).slots[0].semantics == "ack"
        outcome = submission.handle.cancel("user_stop", timeout_s=2.0)
        assert isinstance(outcome, CancelAlreadyTerminal)
        assert outcome.terminal_type == "action.result_observed"
        assert _event_count(fixture.conn, "action.cancelled") == 0
        assert _event_count(fixture.conn, "action.result_observed") == 1
    finally:
        fixture.close()


def test_timeout_assumed_keeps_the_resource_quarantined(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """ADR-0008 F23: a supervisor terminal does not release the repository."""
    key = canonical_resource_key(repo_a)
    entered, release = threading.Event(), threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(
            _gated_tool("slow", entered, release),
            _fixture_tool("next_up", lambda r, _c: _ack(r)),
        ),
        resolver=_fixed_resolver(
            {
                "slow": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
                "next_up": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
            },
        ),
    )
    try:
        assert fixture.runner is not None
        slow = fixture.submit(_request("slow", "A-slow", turn_id="T-slow"))
        assert entered.wait(timeout=5)

        outcome = slow.handle.assume_timeout("codex_turn_timeout")
        assert isinstance(outcome, CancelAccepted)
        assert _event_count(fixture.conn, "action.timeout_assumed") == 1

        follower = fixture.submit(_request("next_up", "A-next", turn_id="T-next"))
        time.sleep(0.2)
        assert not follower.handle.is_done()

        release.set()
        assert slow.handle.result(timeout=10).slots[0].semantics == "ack"
        assert _wait_until(lambda: _event_count(fixture.conn, "worker.quiesced") == 1)
        # Still quarantined: the canonical terminal is not the cleanup terminal.
        time.sleep(0.2)
        assert not follower.handle.is_done()

        fixture.runner.finalize_cleanup("A-slow", verification_outcome="verification_skipped")
        assert follower.handle.result(timeout=10).slots[0].semantics == "ack"
    finally:
        release.set()
        fixture.close()


def test_a_late_worker_write_poisons_cleanup_and_holds_the_lease(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """ADR-0008 D9: late-worker telemetry forces cleanup_failed, never a release."""
    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("mutating", lambda r, _c: _ack(r)),),
        resolver=_fixed_resolver(
            {"mutating": ToolConcurrency(resource_keys=(key,), mode="write_exclusive")},
        ),
    )
    try:
        assert fixture.runner is not None
        fixture.dispatch(_request("mutating", "A-late", turn_id="T-late"))
        context = fixture.runner.context_of("A-late")
        assert context is not None
        context.record_late_write("worker wrote after its terminal")
        assert context.cleanup_state == "cleanup_failed"

        fixture.runner.finalize_cleanup("A-late", verification_outcome="verified")
        assert _event_count(fixture.conn, "action.cleanup_completed") == 0
        failed = _payloads(fixture.conn, "action.cleanup_failed")
        assert len(failed) == 1
        assert failed[0]["quarantine_reason"] == "late_worker_write"
        # The repository is deliberately still leased: a human must clear it.
        assert [s.action_id for s in fixture.runner.leases.live_scopes()] == ["A-late"]
    finally:
        fixture.close()


def test_late_worker_telemetry_is_bounded(tmp_path: Path, repo_a: Path) -> None:
    """The per-action late-write record set never grows past its cap."""
    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(_fixture_tool("mutating", lambda r, _c: _ack(r)),),
        resolver=_fixed_resolver(
            {"mutating": ToolConcurrency(resource_keys=(key,), mode="write_exclusive")},
        ),
    )
    try:
        assert fixture.runner is not None
        fixture.dispatch(_request("mutating", "A-bounded", turn_id="T-bounded"))
        context = fixture.runner.context_of("A-bounded")
        assert context is not None
        for index in range(250):
            context.record_late_write(f"write-{index}")
        assert len(context.late_writes) == 100
        assert context.late_writes[-1] == "write-249"
    finally:
        fixture.close()


# --- B6: boot quarantine ----------------------------------------------------


def test_boot_reconciliation_requarantines_an_uncleaned_repo(tmp_path: Path) -> None:
    """A terminated action with no cleanup event keeps its repo locked at boot."""
    path = tmp_path / "reconcile.db"
    conn = open_event_log(path)
    try:
        dispatched = emit_event(
            conn,
            type="action.dispatched",
            payload={"action_id": "A-crashed"},
            correlation={"action_id": "A-crashed"},
        )
        running = emit_event(
            conn,
            type="action.running",
            payload={
                "action_id": "A-crashed",
                "resource_keys": "repo:/tmp/crashed",
                "resource_mode": "write_exclusive",
            },
            source_event_id=dispatched.event_uid,
            correlation={"action_id": "A-crashed"},
        )
        emit_event(
            conn,
            type="action.timeout_assumed",
            payload={"action_id": "A-crashed", "reason": "daemon_died"},
            source_event_id=running.event_uid,
            correlation={"action_id": "A-crashed"},
        )
        # A second action that DID clean up must not be re-quarantined.
        clean_dispatched = emit_event(
            conn,
            type="action.dispatched",
            payload={"action_id": "A-clean"},
            correlation={"action_id": "A-clean"},
        )
        clean_running = emit_event(
            conn,
            type="action.running",
            payload={
                "action_id": "A-clean",
                "resource_keys": "repo:/tmp/clean",
                "resource_mode": "write_exclusive",
            },
            source_event_id=clean_dispatched.event_uid,
            correlation={"action_id": "A-clean"},
        )
        emit_event(
            conn,
            type="action.result_observed",
            payload={"action_id": "A-clean", "semantics": "ack"},
            source_event_id=clean_running.event_uid,
            correlation={"action_id": "A-clean"},
        )
        emit_event(
            conn,
            type="action.cleanup_completed",
            payload={
                "action_id": "A-clean",
                "worker_epoch": 0,
                "verification_outcome": "verified",
            },
            source_event_id=clean_running.event_uid,
            correlation={"action_id": "A-clean"},
        )

        runner = ActionRunner(event_log_path=path, lease_timeout_s=0.1)
        try:
            assert runner.reconcile_quarantine(conn) == ("A-crashed",)
            with pytest.raises(TimeoutError):
                runner.leases.acquire(
                    action_id="A-newcomer",
                    keys=frozenset({"repo:/tmp/crashed"}),
                    mode="write_exclusive",
                    timeout_s=0.1,
                )
            # The cleaned-up repository is free.
            runner.leases.acquire(
                action_id="A-newcomer",
                keys=frozenset({"repo:/tmp/clean"}),
                mode="write_exclusive",
                timeout_s=0.5,
            )
        finally:
            runner.shutdown()
    finally:
        conn.close()


# --- B7: flag graph ---------------------------------------------------------


def test_action_runner_flag_requires_the_wave1_primitives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requesting the runner without its L2 primitives downgrades once."""
    config = {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": False,
                "lifecycle_terminal_cas": False,
            },
            "actions": {"action_runner": True},
        },
    }
    with caplog.at_level("WARNING", logger="jarvis.runtime"):
        assert _wave4_action_flags(config).all_disabled
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    enabled = {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
            },
            "actions": {"action_runner": True},
        },
    }
    assert _wave4_action_flags(enabled).action_runner is True
    assert _wave4_action_flags({}).all_disabled


def test_wave4b_action_flag_ships_disabled() -> None:
    """Rollout safety: the shipped config leaves the runner switch off."""
    shipped = yaml.safe_load(
        (repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"),
    )
    actions = shipped["realtime"]["actions"]
    assert Wave4ActionFlags.from_mapping(actions).all_disabled
    assert actions["max_concurrent_runs"] == 1
    assert actions["lease_timeout_s"] == 900


# --- B8: the real drive_turn finalizer --------------------------------------


def test_drive_turn_finalizes_cleanup_after_the_stash_pop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root closes the cleanup debt its turn's actions opened.

    ADR-0008 D9's Wave 4B slice keeps verify-then-stash-restore with the
    legacy driver, so `drive_turn`'s finalizer is what releases the repository
    — and it must run lexically after `_pop_pending_stashes`, or an unrelated
    same-repo action could start mid-restore.
    """
    repo = tmp_path / "turn-repo"
    repo.mkdir()
    key = canonical_resource_key(repo)
    fixture = _Fixture(
        tmp_path / "root",
        tools=(_fixture_tool("mutating", lambda r, _c: _ack(r)),),
        resolver=_fixed_resolver(
            {"mutating": ToolConcurrency(resource_keys=(key,), mode="write_exclusive")},
        ),
    )
    order: list[str] = []
    real_pop = runtime_module._pop_pending_stashes  # noqa: SLF001 - ordering probe

    def _record_pop(*args: object, **kwargs: object) -> None:
        order.append("stash_pop")
        real_pop(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module, "_pop_pending_stashes", _record_pop)

    assert fixture.runner is not None
    real_finalize = fixture.runner.finalize_turn_cleanup

    def _record_finalize(turn_id: str, **kwargs: Any) -> tuple[str, ...]:  # noqa: ANN401
        order.append("cleanup")
        return real_finalize(turn_id, **kwargs)

    monkeypatch.setattr(fixture.runner, "finalize_turn_cleanup", _record_finalize)

    runtime = _turn_runtime(fixture)
    intent = emit_event(
        fixture.conn,
        type="surface.user_intent",
        payload={
            "transcript": "跑一下",
            "turn_id": "T-turn",
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": "T-turn"},
    )

    def _decide(_trigger: Any, ctx: Any) -> Any:  # noqa: ANN401 - scripted DecideResult.
        request = _request("mutating", "A-turn", turn_id="T-turn")
        _authorize(ctx.lifecycle, "A-turn")
        ctx.tool_registry.dispatch(request, ctx.conn, ctx.runtime_paths, ctx.lifecycle)
        return SimpleNamespace(
            response_plan=_turn_plan(),
            events_emitted=(),
            turn_id="T-turn",
            attention_channel="voice_notify",
        )

    monkeypatch.setattr(runtime_module, "decide", _decide)
    try:
        runtime_module.drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=False,
        )
        assert order == ["stash_pop", "cleanup"]
        cleanup = _payloads(fixture.conn, "action.cleanup_completed")
        assert len(cleanup) == 1
        assert cleanup[0]["action_id"] == "A-turn"
        # No verify_diff ran this turn, so the durable claim says so.
        assert cleanup[0]["verification_outcome"] == "verification_skipped"
        assert fixture.runner.leases.live_scopes() == ()
        assert turn_action_ids("T-turn") == frozenset()
    finally:
        fixture.close()


def _turn_plan() -> ResponsePlan:
    """Return the fixed approved plan the scripted decide finishes with."""
    return ResponsePlan(
        text="好了。",
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash="hash-turn",
        output_risk_class="routine",
        required_gate_mode="full_text",
    )


def _turn_runtime(fixture: _Fixture) -> JarvisRuntime:
    """Wrap the fixture in the JarvisRuntime shape ``drive_turn`` expects."""
    return JarvisRuntime(
        config={},
        runtime_paths=fixture.paths,
        conn=fixture.conn,
        tool_registry=fixture.registry,
        lifecycle=fixture.lifecycle,
        llm_client=None,  # type: ignore[arg-type]
        system_prompt="",
        action_runner=fixture.runner,
    )


def test_a_blocked_lease_waiter_never_occupies_a_run_slot(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """Regression: a queued same-repo action must not starve its own releaser.

    With the shipped ``max_concurrent_runs: 1`` and a fixed worker pool, the
    blocked waiter took the only worker thread, so the follow-up action whose
    turn would have released the lease could never start — both sides waited
    until the lease timeout. ADR-0008 D9 says ``max_concurrent_runs`` limits
    jobs "only after resource leases are respected", which is exactly the fix:
    waiting for a lease costs a thread, never a run slot.
    """
    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(
            _fixture_tool("holder", lambda r, _c: _ack(r)),
            _fixture_tool("blocked", lambda r, _c: _ack(r)),
            _fixture_tool("child", lambda r, _c: _ack(r), read_only=True),
        ),
        max_concurrent_runs=1,
        lease_timeout_s=30.0,
        resolver=_fixed_resolver(
            {
                "holder": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
                "blocked": ToolConcurrency(resource_keys=(key,), mode="write_exclusive"),
                "child": ToolConcurrency(resource_keys=(key,), mode="read_shared"),
            },
        ),
    )
    try:
        assert fixture.runner is not None
        # Turn A takes the repository and keeps it through cleanup.
        fixture.dispatch(_request("holder", "A-holder", turn_id="T-a"))
        assert [s.action_id for s in fixture.runner.leases.live_scopes()] == ["A-holder"]

        # Turn B queues behind it. Sleep so its thread is really parked on the
        # lease before the next dispatch — the whole point is what that parked
        # thread is holding while it waits.
        blocked = fixture.submit(_request("blocked", "A-blocked", turn_id="T-b"))
        time.sleep(0.3)
        assert not blocked.handle.is_done()

        # Turn A's own follow-up must still get a run slot; before the fix the
        # single worker was held by the blocked waiter and this call hung.
        started = time.monotonic()
        fixture.dispatch(_request("child", "A-child", turn_id="T-a"))
        assert time.monotonic() - started < 5.0

        fixture.runner.finalize_turn_cleanup("T-a", verification_outcome="verified")
        assert blocked.handle.result(timeout=10).slots[0].semantics == "ack"
    finally:
        fixture.close()


def test_verify_diff_key_comes_from_the_run_it_verifies(tmp_path: Path) -> None:
    """A verification with no explicit repo_path still names its parent's repo.

    Regression: the key used to fall back to ``Path.cwd()`` while the parent
    `spawn_worker` had leased the task's repo, so the subset check refused the
    borrow and a verification that used to run fine failed closed.
    """
    repo = tmp_path / "task-repo"
    repo.mkdir()
    conn = open_event_log(tmp_path / "provenance.db")
    try:
        created = emit_event(
            conn,
            type="task.created",
            payload={
                "task_id": "T_abc",
                "goal": "ship it",
                "source": "allen",
                "repo_path": str(repo),
            },
            correlation={"task_id": "T_abc"},
        )
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R_1", "task_id": "T_abc", "runner": "codex"},
            source_event_id=created.event_uid,
            correlation={"action_id": "A-parent", "run_id": "R_1", "task_id": "T_abc"},
        )

        tool = _fixture_tool("verify_diff", lambda r, _c: _ack(r), read_only=True)
        resolved = default_resource_key_resolver(
            _request("verify_diff", "A-verify", arguments={"run_id": "R_1"}),
            tool,
            conn,
        )
        assert resolved.resource_keys == (canonical_resource_key(repo),)
        assert resolved.mode == "read_shared"
        assert resolved.parent_action_id == "A-parent"

        # An explicit repo_path still wins, and naming another tree is then a
        # genuine cross-repo verify rather than a borrow of the parent's.
        other = tmp_path / "other-repo"
        other.mkdir()
        explicit = default_resource_key_resolver(
            _request(
                "verify_diff",
                "A-other",
                arguments={"run_id": "R_1", "repo_path": str(other)},
            ),
            tool,
            conn,
        )
        assert explicit.resource_keys == (canonical_resource_key(other),)
    finally:
        conn.close()


def test_same_turn_guard_never_dissolves_a_stronger_mode(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """The no-self-wait guard covers keys AND mode, so it cannot over-grant.

    A same-turn job that wants a stronger lease than its sibling holds is a
    real conflict, not a self-wait: it must queue on the normal acquire path
    and time out visibly rather than be handed a scope it did not earn.
    """
    key = canonical_resource_key(repo_a)
    fixture = _Fixture(
        tmp_path,
        tools=(
            _fixture_tool("reader", lambda r, _c: _ack(r), read_only=True),
            _fixture_tool("writer", lambda r, _c: _ack(r)),
        ),
        lease_timeout_s=0.3,
        resolver=_fixed_resolver(
            {
                "reader": ToolConcurrency(resource_keys=(key,), mode="read_shared"),
                "writer": ToolConcurrency(resource_keys=(key,), mode="global_exclusive"),
            },
        ),
    )
    try:
        assert fixture.runner is not None
        # A read_shared holder with cleanup debt keeps its scope alive.
        reader_submission = fixture.submit(_request("reader", "A-reader", turn_id="T-same"))
        assert reader_submission.handle.result(timeout=10).slots[0].semantics == "ack"
        # read_shared frees at quiescence, so pin the scope explicitly instead.
        held = fixture.runner.leases.acquire(
            action_id="A-holder",
            keys=frozenset({key}),
            mode="write_exclusive",
            timeout_s=1.0,
        )
        try:
            escalating = fixture.submit(_request("writer", "A-writer", turn_id="T-same"))
            with pytest.raises(TimeoutError):
                escalating.handle.result(timeout=10)
        finally:
            fixture.runner.leases.release(held)
    finally:
        fixture.close()
