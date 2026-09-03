"""Wave 5 acceptance: background workers and the runner-owned finalizer.

ADR-0008 §8 Step 4, first half of the row: "Make ``spawn_worker`` truly
background; move quiescence/verify/stash/live-action cleanup and quarantine
release to an owned finalizer."

Everything runs against a real on-disk Event Log, the real
:class:`~jarvis.execution.tools.ToolRegistry`, the real
:class:`~jarvis.execution.action_runner.ActionRunner` and the real
``drive_turn``; only the tool handler and ``decide`` are fixtures, because
the property under test is ownership, not any particular tool's body.

The headline properties:

* ``test_background_dispatch_returns_before_the_worker_finishes`` — the "long
  fake action" half of the row.
* ``test_turn_cleanup_waits_for_a_background_worker`` — the release that used
  to happen in ``drive_turn``'s ``finally`` now cannot happen until the
  worker is quiescent, which is the bug the row exists to prevent.
* ``test_cancelling_a_live_background_worker_cleans_up`` — the "cancel live-
  process cleanup" half: one canonical terminal, the stash ref on it, the
  finalizer released the lease.
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

from jarvis import runtime as runtime_module
from jarvis.decision.gates import ResponsePlan
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.action_runner import (
    ActionRunner,
    CancelAccepted,
    CancelAlreadyTerminal,
    CancelUnconfirmed,
    ToolConcurrency,
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
from jarvis.runtime import JarvisRuntime
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import emit_event, open_event_log

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


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    """Poll ``predicate`` until true or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _request(tool_name: str, action_id: str, *, turn_id: str | None = None) -> ActionRequest:
    """Build one ActionRequest for a fixture tool."""
    return ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L1",
        arguments={},
        authorization_lease=None,
        run_id=None,
        turn_id=turn_id,
    )


def _async_tool(
    name: str,
    body: Callable[[ActionRequest, sqlite3.Connection], RawResult],
    *,
    cancellation_mode: str = "unsupported",
) -> ToolDefinition:
    """A declared-async ToolDefinition around a plain callable body."""

    def _handler(
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        _runtime_paths: RuntimePathsLike,
        _lifecycle: ActionLifecycle,
    ) -> RawResult:
        return body(action_request, conn)

    return ToolDefinition(
        name=name,
        description=f"fixture async tool {name}",
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        result_semantics="ack",
        is_async=True,
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=_handler,
        domain="test",
        read_only=False,
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
    """One runtime root, registry, runner and lifecycle with background dispatch."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        tools: tuple[ToolDefinition, ...],
        resolver: Callable[
            [ActionRequest, ToolDefinition, sqlite3.Connection],
            ToolConcurrency,
        ]
        | None = None,
        background_async: bool = True,
    ) -> None:
        """Build the runtime paths, the Event Log and the registry."""
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.runner = ActionRunner(
            event_log_path=self.paths.event_log,
            max_concurrent_runs=4,
            lease_timeout_s=10.0,
        )
        self.registry = ToolRegistry(
            action_runner=self.runner,
            resource_key_resolver=resolver,
            background_async=background_async,
        )
        for tool in tools:
            self.registry.register(tool)
        self.lifecycle = ActionLifecycle()

    def dispatch(self, request: ActionRequest) -> RawResultBundle:
        """Authorize and dispatch one request on the caller's thread."""
        _authorize(self.lifecycle, request.action_id)
        return self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)

    def close(self) -> None:
        """Drain the runner and close the Event Log connection."""
        self.runner.shutdown()
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


@pytest.fixture
def repo_a(tmp_path: Path) -> Path:
    """One canonical repository path."""
    path = tmp_path / "repo-a"
    path.mkdir()
    return path


# --- the row's "long fake action" -------------------------------------------


def test_background_dispatch_returns_before_the_worker_finishes(
    tmp_path: Path,
    repo_a: Path,
) -> None:
    """`dispatch` on a declared-async tool returns while the worker still runs."""
    entered = threading.Event()
    release = threading.Event()

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        entered.set()
        assert release.wait(timeout=10)
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_async_tool("slow", _body),),
        resolver=_fixed_resolver(
            {
                "slow": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        began = time.monotonic()
        bundle = fixture.dispatch(_request("slow", "A-slow", turn_id="T-bg"))
        returned_ms = (time.monotonic() - began) * 1000

        # The handler is still inside its body.
        assert entered.wait(timeout=5)
        assert not release.is_set()
        # Sub-second: this is the acknowledgement, not the result.
        assert returned_ms < 1000
        assert bundle.slots[0].semantics == "ack"
        assert bundle.slots[0].payload == {"action_id": "A-slow", "dispatch": "background"}
        # The ActionRun is genuinely under way, holding its lease.
        assert _wait_until(lambda: _event_count(fixture.conn, "action.running") == 1)
        assert fixture.runner.leases.live_scopes() != ()
        assert _event_count(fixture.conn, "worker.quiesced") == 0

        release.set()
        assert _wait_until(lambda: _event_count(fixture.conn, "worker.quiesced") == 1)
    finally:
        release.set()
        fixture.close()


def test_foreground_dispatch_still_awaits_the_handle(tmp_path: Path, repo_a: Path) -> None:
    """With the switch off, a declared-async tool is awaited exactly as in Wave 4B."""
    finished = threading.Event()

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        time.sleep(0.05)
        finished.set()
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_async_tool("slow", _body),),
        background_async=False,
        resolver=_fixed_resolver(
            {
                "slow": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        bundle = fixture.dispatch(_request("slow", "A-sync", turn_id="T-fg"))
        assert finished.is_set()
        assert bundle.slots[0].payload == {"ok": True}
    finally:
        fixture.close()


def test_background_async_requires_a_runner() -> None:
    """A registry cannot promise background dispatch with nothing to own it."""
    with pytest.raises(Exception, match="requires an ActionRunner"):
        ToolRegistry(background_async=True)


# --- the row's "owned finalizer" --------------------------------------------


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


def _turn_plan() -> ResponsePlan:
    """A minimal approved plan so ``drive_turn`` can finalize."""
    return ResponsePlan(
        text="好了。",
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash="hash-turn",
        output_risk_class="routine",
        required_gate_mode="full_text",
    )


def _drive_one_turn(  # noqa: PLR0913 — one keyword per identity the scripted turn needs.
    fixture: _Fixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn_id: str,
    tool_name: str,
    action_id: str,
    after_dispatch: Callable[[], object] | None = None,
) -> None:
    """Run one real ``drive_turn`` whose single decide() dispatches one action.

    ``after_dispatch`` runs inside the scripted ``decide``, still inside the
    turn, so a test can pin what the driver's finalizer sees: called or not
    called is the difference between the inline and the deferred cleanup path.
    """
    intent = emit_event(
        fixture.conn,
        type="surface.user_intent",
        payload={
            "transcript": "跑一下",
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )

    def _decide(_trigger: Any, ctx: Any) -> Any:  # noqa: ANN401 - scripted DecideResult.
        request = _request(tool_name, action_id, turn_id=turn_id)
        _authorize(ctx.lifecycle, action_id)
        ctx.tool_registry.dispatch(request, ctx.conn, ctx.runtime_paths, ctx.lifecycle)
        if after_dispatch is not None:
            after_dispatch()
        return SimpleNamespace(
            response_plan=_turn_plan(),
            events_emitted=(),
            turn_id=turn_id,
            attention_channel="voice_notify",
        )

    monkeypatch.setattr(runtime_module, "decide", _decide)
    runtime_module.drive_turn(
        _turn_runtime(fixture),
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=False,
    )


def test_turn_cleanup_waits_for_a_background_worker(
    tmp_path: Path,
    repo_a: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's cleanup request cannot release a repo the worker still holds.

    This is the defect a truly background worker introduces and the reason
    ADR-0008 D9 moves cleanup ownership into the runner: with the Wave-4B
    finalizer, ``drive_turn``'s ``finally`` would have emitted
    ``action.cleanup_completed`` and freed the lease while the handler was
    still writing to the tree.
    """
    release = threading.Event()
    entered = threading.Event()

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        entered.set()
        assert release.wait(timeout=10)
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_async_tool("slow", _body),),
        resolver=_fixed_resolver(
            {
                "slow": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        _drive_one_turn(
            fixture,
            monkeypatch,
            turn_id="T-bg",
            tool_name="slow",
            action_id="A-bg",
            after_dispatch=lambda: entered.wait(timeout=10),
        )
        # The turn is over and the worker is not.
        assert entered.is_set()
        assert _event_count(fixture.conn, "action.cleanup_completed") == 0
        assert fixture.runner.leases.live_scopes() != ()

        release.set()
        assert _wait_until(lambda: _event_count(fixture.conn, "action.cleanup_completed") == 1)
        # The runner ran the driver's finalizer on the worker's own thread.
        types = _ordered_types(fixture.conn)
        assert types.index("worker.quiesced") < types.index("action.cleanup_completed")
        assert fixture.runner.leases.live_scopes() == ()
        cleanup = _payloads(fixture.conn, "action.cleanup_completed")[0]
        assert cleanup["action_id"] == "A-bg"
        assert cleanup["verification_outcome"] == "verification_skipped"
    finally:
        release.set()
        fixture.close()


def test_turn_cleanup_runs_inline_when_nothing_is_still_running(
    tmp_path: Path,
    repo_a: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose actions all finished cleans up on the driver's own thread."""
    fixture = _Fixture(
        tmp_path,
        tools=(_async_tool("quick", lambda r, _c: _ack(r)),),
        resolver=_fixed_resolver(
            {
                "quick": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        _drive_one_turn(
            fixture,
            monkeypatch,
            turn_id="T-quick",
            tool_name="quick",
            action_id="A-quick",
            # Let the worker genuinely finish first, so the driver's request
            # finds nothing in flight and runs the finalizer on its own thread.
            after_dispatch=lambda: _wait_until(
                lambda: _event_count(fixture.conn, "worker.quiesced") == 1,
            ),
        )
        # No waiting: the cleanup terminal was already durable when drive_turn
        # returned.
        assert _event_count(fixture.conn, "action.cleanup_completed") == 1
        assert fixture.runner.leases.live_scopes() == ()
        assert turn_action_ids("T-quick") == frozenset()
    finally:
        fixture.close()


# --- the row's "cancel / timeout live-process cleanup" ----------------------


def _cancellable_tool(name: str, entered: threading.Event) -> ToolDefinition:
    """A declared-async tool that stops when its execution context says to.

    It also records a stash ref the way ``spawn_worker_handler`` does, so the
    cancel terminal has something to carry.
    """

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        context = current_execution_context()
        assert context is not None
        context.record_stash_ref("f" * 40)
        entered.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if context.is_cancel_requested:
                return RawResult(
                    action_id=request.action_id,
                    semantics="error",
                    payload={"status": "cancelled"},
                    tool_output="{}",
                    error="cancelled",
                )
            time.sleep(0.005)
        return _ack(request)

    return _async_tool(name, _body, cancellation_mode="terminate_process")


def test_cancelling_a_live_background_worker_cleans_up(
    tmp_path: Path,
    repo_a: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel writes one terminal, carries the stash ref, and frees the repo."""
    entered = threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(_cancellable_tool("stoppable", entered),),
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
        _drive_one_turn(
            fixture,
            monkeypatch,
            turn_id="T-cancel",
            tool_name="stoppable",
            action_id="A-cancel",
            after_dispatch=lambda: entered.wait(timeout=10),
        )
        assert entered.is_set()
        assert _event_count(fixture.conn, "action.cleanup_completed") == 0

        requested_at = time.monotonic()
        outcome = fixture.runner.cancel_action(
            "A-cancel",
            reason="user_stop",
            timeout_s=10.0,
            cancellation_mode="terminate_process",
        )
        assert isinstance(outcome, CancelAccepted)
        cancel_ms = (time.monotonic() - requested_at) * 1000
        assert cancel_ms < 5000

        # Exactly one canonical terminal, and it is the cancel.
        assert _event_count(fixture.conn, "action.cancelled") == 1
        for other in ("action.result_observed", "action.failed", "action.timeout_assumed"):
            assert _event_count(fixture.conn, other) == 0, other
        cancelled = _payloads(fixture.conn, "action.cancelled")[0]
        assert cancelled["stash_ref"] == "f" * 40
        assert cancelled["cancellation_mode"] == "terminate_process"
        assert cancelled["requested_by_turn_id"] == "T-cancel"
        # The in-process FSM agrees with the durable fold.
        assert fixture.lifecycle.state_of("A-cancel") == "cancelled"

        # The finalizer released the repository, after quiescence.
        assert _wait_until(lambda: _event_count(fixture.conn, "action.cleanup_completed") == 1)
        types = _ordered_types(fixture.conn)
        assert types.index("worker.quiesced") < types.index("action.cleanup_completed")
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        fixture.close()


def test_assumed_timeout_keeps_the_repo_until_the_worker_stops(
    tmp_path: Path,
    repo_a: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor timeout may be canonical while the process is still alive.

    ADR-0008 D9 says exactly that, and adds that the lease stays quarantined
    until quiescence and cleanup finish. Both halves are checked here.
    """
    entered = threading.Event()
    release = threading.Event()

    def _body(request: ActionRequest, _conn: sqlite3.Connection) -> RawResult:
        context = current_execution_context()
        assert context is not None
        context.record_stash_ref("a" * 40)
        entered.set()
        assert release.wait(timeout=10)
        return _ack(request)

    fixture = _Fixture(
        tmp_path,
        tools=(_async_tool("slow", _body),),
        resolver=_fixed_resolver(
            {
                "slow": ToolConcurrency(
                    resource_keys=(canonical_resource_key(repo_a),),
                    mode="write_exclusive",
                ),
            },
        ),
    )
    try:
        _drive_one_turn(
            fixture,
            monkeypatch,
            turn_id="T-timeout",
            tool_name="slow",
            action_id="A-timeout",
            after_dispatch=lambda: entered.wait(timeout=10),
        )
        assert entered.is_set()

        outcome = fixture.runner.assume_timeout("A-timeout", reason="supervisor_sweep")
        assert isinstance(outcome, CancelAccepted)
        assert _payloads(fixture.conn, "action.timeout_assumed")[0]["stash_ref"] == "a" * 40
        # Terminal, but the repository is NOT free.
        assert _event_count(fixture.conn, "action.cleanup_completed") == 0
        assert fixture.runner.leases.live_scopes() != ()

        release.set()
        assert _wait_until(lambda: _event_count(fixture.conn, "action.cleanup_completed") == 1)
        assert fixture.runner.leases.live_scopes() == ()
    finally:
        release.set()
        fixture.close()


def test_cancel_after_the_context_is_reaped_says_already_terminal(
    tmp_path: Path,
    repo_a: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late cancel on a finished action is `already_terminal`, not `no_live_context`.

    Wave 4 shipped the honest-but-wrong answer: the context was gone, so the
    runner said it could not confirm anything, even though the durable fold
    held a terminal the whole time.
    """
    entered = threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(_cancellable_tool("stoppable", entered),),
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
        _drive_one_turn(
            fixture,
            monkeypatch,
            turn_id="T-late",
            tool_name="stoppable",
            action_id="A-late",
            after_dispatch=lambda: entered.wait(timeout=10),
        )
        assert entered.is_set()
        assert isinstance(
            fixture.runner.cancel_action(
                "A-late",
                reason="user_stop",
                timeout_s=10.0,
                cancellation_mode="terminate_process",
            ),
            CancelAccepted,
        )
        assert _wait_until(lambda: fixture.runner.context_of("A-late") is None)

        late = fixture.runner.cancel_action(
            "A-late",
            reason="user_stop",
            timeout_s=1.0,
            cancellation_mode="terminate_process",
        )
        assert isinstance(late, CancelAlreadyTerminal)
        assert late.terminal_type == "action.cancelled"
        assert _event_count(fixture.conn, "action.cancelled") == 1

        unknown = fixture.runner.cancel_action(
            "A-never-existed",
            reason="user_stop",
            timeout_s=1.0,
            cancellation_mode="terminate_process",
        )
        assert isinstance(unknown, CancelUnconfirmed)
        assert unknown.reason == "no_live_context"
    finally:
        fixture.close()


def test_shutdown_drains_a_live_cancellable_worker(tmp_path: Path, repo_a: Path) -> None:
    """`shutdown(cancel=True)` stops in-flight work instead of abandoning it."""
    entered = threading.Event()
    fixture = _Fixture(
        tmp_path,
        tools=(_cancellable_tool("stoppable", entered),),
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
        fixture.dispatch(_request("stoppable", "A-drain", turn_id="T-drain"))
        assert entered.wait(timeout=5)

        drained = fixture.runner.shutdown(cancel=True, timeout_s=10.0)
        assert drained == ("A-drain",)
        assert _event_count(fixture.conn, "action.cancelled") == 1
        assert _payloads(fixture.conn, "action.cancelled")[0]["reason"] == "daemon_shutdown"
    finally:
        with contextlib.suppress(sqlite3.Error):
            fixture.conn.close()


def test_a_cancelled_run_still_has_its_pre_task_stash_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`action.cancelled` is a stash-bearing terminal the finalizer must read.

    Once ``spawn_worker`` is truly background the ActionRunner, not the
    handler, writes the cancel terminal — so that row is the *only* durable
    record naming a cancelled run's pre-task stash. The finalizer walked
    ``worker.reported`` / ``action.failed`` / ``action.timeout_assumed`` only,
    so cancelling a Codex worker shelved the user's uncommitted work and left
    nothing able to restore it. ADR-0008 §12 lists "cancelled-stash cleanup"
    among the paths that must reach a cleanup terminal.

    Restoring also needs two ids the runner's canonical correlation has no
    room for: ``task_id`` resolves the repository and ``run_id`` keys the
    conflict artifact. Both ride the payload, so both are asserted here.
    """
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    repo = tmp_path / "cancelled-repo"
    repo.mkdir()
    calls: list[tuple[str, str, str]] = []

    def _spy(
        repo_path: Path,
        stash_ref: str,
        *,
        artifact_dir: Path,
        run_id: str,
    ) -> None:
        """Record the restore the finalizer asked for, and report no conflict."""
        assert artifact_dir == paths.artifacts_root
        calls.append((str(repo_path), stash_ref, run_id))

    monkeypatch.setattr(runtime_module, "restore_pretask_changes", _spy)
    try:
        proposed = emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": "A-cancel",
                "tool_name": "spawn_worker",
                "caller_principal": "jarvis_llm",
                "risk_level": "L2",
            },
            correlation={"turn_id": "T-cancel"},
        )
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "TK-cancel", "goal": "改点东西", "repo_path": str(repo)},
            correlation={"turn_id": "T-cancel"},
        )
        emit_event(
            conn,
            type="action.cancelled",
            payload={
                "action_id": "A-cancel",
                "reason": "user_stop",
                "cancellation_mode": "terminate_process",
                # The three fields `_stamp_worker_identity` puts on this row.
                "stash_ref": "stash@{0}",
                "run_id": "RUN-cancel",
                "task_id": "TK-cancel",
            },
            source_event_id=proposed.event_uid,
            correlation={"turn_id": "T-cancel", "action_id": "A-cancel"},
        )

        runtime_module._pop_pending_stashes(  # noqa: SLF001 — the finalizer under test.
            conn,
            artifacts_root=paths.artifacts_root,
            turn_id="T-cancel",
        )

        assert calls == [(str(repo), "stash@{0}", "RUN-cancel")]
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
