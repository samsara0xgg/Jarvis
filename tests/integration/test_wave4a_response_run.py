"""Wave 4A acceptance: ResponseRun lifecycle, per-run client, response cancel.

ADR-0008 §8 Step 2 (D1 / D4 / D10, §4.2, §4.4, §6, §9.4). Every check here
runs against a real on-disk Event Log and the real composition-root
``drive_turn``; only ``decide`` and the provider SDK handle are scripted, so
the lifecycle, the terminal CAS, the cancel path and the flag gating are
exercised as shipped.

The headline property is
``test_cancel_during_action_wait_leaves_action_alive``: cancelling a response
mid-action writes exactly one ``response.cancelled``, zero ``action.*``
terminals, and the dispatched worker still reaches its own terminal.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis import runtime as runtime_module
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import (
    ImmutableRequestClientError,
    LLMSessionFactory,
    UnknownRequestPresetError,
)
from jarvis.decision.response_run import (
    CancelRejected,
    IllegalResponseTransitionError,
    ResponseCancelledError,
    ResponseCancelRequest,
    ResponseRunFacts,
    ResponseRunRegistry,
    ResponseTerminalizer,
    evidence_snapshot_hash,
    legacy_full_text_policy,
    reconcile_open_responses,
    request_response_cancel,
    start_response_run,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import (
    ActionLifecycle,
    build_default_registry,
    register_live_action,
)
from jarvis.runtime import (
    JarvisRuntime,
    _wave4_response_activation,
    _wave4_response_flags,
    drive_turn,
    make_response_cancel_callable,
)
from jarvis.shared.realtime import (
    AlreadyTerminal,
    TerminalCommitted,
    Wave1FeatureFlags,
    Wave4ResponseFlags,
    new_response_id,
    stable_legacy_presentation_binding,
    stable_response_group_id,
)
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state import event_log as event_log_module
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.response_runs import append_response_started, open_response_runs
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from jarvis.decision import DecideContext, DecideResult
    from jarvis.shared import Event
    from jarvis.shared.realtime import TerminalOutcome

_RESPONSE_TERMINALS = ("response.completed", "response.cancelled", "response.failed")
_PLAN_TEXT = "已完成。"
_PLAN_HASH = "hash-fixed"


# --- harness ---------------------------------------------------------------


def _raw_connection(path: Path) -> sqlite3.Connection:
    """Open a bare SQLite connection with the Wave-1 busy timeout."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _rows(conn: sqlite3.Connection, event_type: str) -> list[tuple[Any, ...]]:
    """Return ``(payload_json, source_event_id, event_uid)`` rows of a type."""
    return list(
        conn.execute(
            "SELECT payload_json, source_event_id, event_uid FROM events "
            "WHERE type = ? ORDER BY id ASC",
            (event_type,),
        ),
    )


def _event_count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


def _payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [json.loads(row[0]) for row in _rows(conn, event_type)]


def _publish_sink(sink: list[str]) -> Callable[[Event], None]:
    """Return a CommittedEventBus subscriber that records published UIDs."""

    def _record(event: Event) -> None:
        sink.append(event.event_uid)

    return _record


def _ordered_types(conn: sqlite3.Connection) -> list[str]:
    """Return every event type in append order."""
    return [str(row[0]) for row in conn.execute("SELECT type FROM events ORDER BY id ASC")]


def _plan(text: str = _PLAN_TEXT, response_hash: str = _PLAN_HASH) -> ResponsePlan:
    """Build a fixed approved ResponsePlan."""
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=response_hash,
        output_risk_class="routine",
        required_gate_mode="full_text",
    )


_LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "fast",
    "presets": {
        "fast": {
            "provider": "openai",
            "model": "gpt-fast",
            "base_url": "https://example.invalid/fast",
            "max_tokens": 64,
        },
        "deep": {
            "provider": "openai",
            "model": "gpt-deep",
            "base_url": "https://example.invalid/deep",
            "max_tokens": 128,
        },
    },
}


def _realtime_config(
    *,
    lifecycle: bool,
    cancel: bool,
    wave1: bool = True,
    enabled: bool = True,
    cancel_timeout_ms: int = 500,
) -> dict[str, Any]:
    """Build a config mapping for the Wave-4A flag graph."""
    return {
        "realtime": {
            "enabled": enabled,
            "concurrency_safety": {
                "transactional_event_append": wave1,
                "lifecycle_terminal_cas": wave1,
                "confirmation_dispatch_outbox": False,
                "exactly_once_cost_accounting": False,
            },
            "response": {
                "response_run_lifecycle": lifecycle,
                "independent_response_cancel": cancel,
                "cancel_timeout_ms": cancel_timeout_ms,
            },
        },
    }


def _make_runtime(
    tmp_path: Path,
    *,
    lifecycle: bool = False,
    cancel: bool = False,
    cancel_timeout_ms: int = 500,
) -> JarvisRuntime:
    """Assemble a JarvisRuntime wired exactly the way bootstrap wires one."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    config = _realtime_config(
        lifecycle=lifecycle,
        cancel=cancel,
        cancel_timeout_ms=cancel_timeout_ms,
    )
    flags = _wave4_response_flags(config)
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(_LLM_CONFIG),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        response_flags=flags,
        llm_session_factory=(
            LLMSessionFactory(_LLM_CONFIG) if flags.response_run_lifecycle else None
        ),
        response_runs=(ResponseRunRegistry() if flags.independent_response_cancel else None),
        committed_event_bus=CommittedEventBus(),
    )


def _drive_turn_on_own_connection(
    runtime: JarvisRuntime,
    **kwargs: Any,  # noqa: ANN401 - forwarded verbatim to drive_turn.
) -> Any:  # noqa: ANN401 - RunTurnResult; the tests only assert on the log.
    """Run ``drive_turn`` the way the daemon's turn worker thread runs it.

    ``open_event_log`` uses ``check_same_thread=True``, so the connection has
    to be opened *inside* the worker thread — this mirrors
    ``inherent_loop._drive_turn_in_worker_thread`` exactly. The registry and
    the committed-event bus are shared by reference through
    :func:`dataclasses.replace`, which is what lets the cancelling thread see
    the run this one registers.
    """
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(replace(runtime, conn=conn), **kwargs)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _emit_intent(conn: sqlite3.Connection, turn_id: str, transcript: str = "你好") -> Event:
    """Emit the ``surface.user_intent`` row a turn is driven from."""
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )


def _script_decide(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[Event, DecideContext], DecideResult],
) -> None:
    """Replace the composition root's ``decide`` with a scripted stub."""
    monkeypatch.setattr(runtime_module, "decide", behavior)


def _final_result(plan: ResponsePlan | None = None) -> Callable[..., Any]:
    """Return a decide stub that finishes immediately with one plan."""

    def _decide(_trigger: Event, _ctx: DecideContext) -> Any:  # noqa: ANN401
        return SimpleNamespace(
            response_plan=plan if plan is not None else _plan(),
            events_emitted=(),
            stream_failure=None,
            route=None,
            last_gate_event_uid=None,
            turn_id=None,
            attention_channel="voice_notify",
        )

    return _decide


# --- A1: flag-off parity ----------------------------------------------------


def test_flag_off_drive_turn_matches_legacy_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flags off: zero response.* rows and the legacy uuid5 L5 identity."""
    runtime = _make_runtime(tmp_path)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-fixed")

    result = drive_turn(
        runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )

    for terminal in (*_RESPONSE_TERMINALS, "response.started"):
        assert _event_count(runtime.conn, terminal) == 0, terminal
    assert _ordered_types(runtime.conn) == [
        "surface.user_intent",
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ]
    legacy = stable_legacy_presentation_binding(turn_id="T-fixed", response_hash=_PLAN_HASH)
    opened = _payloads(runtime.conn, "surface.response_open")[0]
    assert opened["response_id"] == legacy.response_id
    assert opened["response_group_id"] == legacy.response_group_id
    assert result.response_plan.text == _PLAN_TEXT
    # The extraction refactor must not move the group derivation.
    assert stable_response_group_id("T-fixed") == legacy.response_group_id
    assert legacy.response_group_id.startswith("RGRP")
    runtime.conn.close()


# --- lifecycle --------------------------------------------------------------


def test_flag_on_emits_started_then_completed_with_one_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One started, one completed, and the ADR §4.2 causal chain holds."""
    runtime = _make_runtime(tmp_path, lifecycle=True)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-life")

    drive_turn(
        runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )

    started_rows = _rows(runtime.conn, "response.started")
    completed_rows = _rows(runtime.conn, "response.completed")
    assert len(started_rows) == 1
    assert len(completed_rows) == 1
    started = _payloads(runtime.conn, "response.started")[0]
    completed = _payloads(runtime.conn, "response.completed")[0]
    assert started["response_id"] == completed["response_id"]
    assert started_rows[0][1] == intent.event_uid
    assert completed_rows[0][1] == started_rows[0][2]
    assert completed["response_hash"] == _PLAN_HASH
    for required in (
        "response_id",
        "response_group_id",
        "turn_id",
        "phase",
        "channel",
        "emission_mode",
        "output_risk_class",
        "required_gate_mode",
        "policy_hash",
        "active_subject_ref",
        "evidence_snapshot_hash",
    ):
        assert started.get(required), required
    assert started["provider"] == "openai"
    assert started["model"] == "gpt-fast"
    runtime.conn.close()


def test_flag_on_l5_events_carry_the_l3_response_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0014 identity: surface.response_* name the L3 run, not the uuid5."""
    runtime = _make_runtime(tmp_path, lifecycle=True)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-ident")

    drive_turn(
        runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )

    started = _payloads(runtime.conn, "response.started")[0]
    legacy = stable_legacy_presentation_binding(turn_id="T-ident", response_hash=_PLAN_HASH)
    for event_type in (
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ):
        payloads = _payloads(runtime.conn, event_type)
        assert payloads, event_type
        for payload in payloads:
            assert payload["response_id"] == started["response_id"], event_type
            assert payload["response_group_id"] == started["response_group_id"], event_type
            assert payload["response_id"] != legacy.response_id, event_type
    runtime.conn.close()


def test_response_events_never_go_through_emit_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A8 runtime half: no response.* row is appended by ``emit_event``."""
    real_emit = event_log_module.emit_event
    recorded: list[str] = []

    def _spy(conn: sqlite3.Connection, **kwargs: Any) -> Event:  # noqa: ANN401
        recorded.append(str(kwargs.get("type")))
        return real_emit(conn, **kwargs)

    runtime = _make_runtime(tmp_path, lifecycle=True)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-spy")
    # Every caller does `from jarvis.state.event_log import emit_event`, so the
    # name is bound per module at import time; patching only the defining
    # module would observe nothing at all. Patch each binding instead.
    bound = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("jarvis.") and getattr(module, "emit_event", None) is real_emit
    ]
    assert bound, "no jarvis module holds an emit_event binding"
    for module in bound:
        monkeypatch.setattr(module, "emit_event", _spy)

    drive_turn(
        runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )

    assert recorded, "the spy observed no emit_event call at all"
    assert not [name for name in recorded if name.startswith("response.")]
    assert _event_count(runtime.conn, "response.started") == 1
    assert _event_count(runtime.conn, "response.completed") == 1
    runtime.conn.close()


# --- A2 / F20: terminal CAS -------------------------------------------------


def _race_two_connections(
    path: Path,
    calls: tuple[Callable[[sqlite3.Connection], TerminalOutcome], ...],
) -> tuple[TerminalOutcome, ...]:
    """Run two terminal writers against one DB from two real connections."""
    barrier = threading.Barrier(len(calls) + 1)

    def _worker(call: Callable[[sqlite3.Connection], TerminalOutcome]) -> TerminalOutcome:
        conn = _raw_connection(path)
        try:
            barrier.wait()
            return call(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_worker, call) for call in calls]
        barrier.wait()
        return tuple(future.result(timeout=10) for future in futures)


def _seed_started(path: Path, response_id: str, turn_id: str) -> ResponseRunFacts:
    """Seed one durable ``response.started`` and return its facts."""
    conn = open_event_log(path)
    try:
        intent = _emit_intent(conn, turn_id)
        group_id = stable_response_group_id(turn_id)
        started = append_response_started(
            conn,
            payload={
                "response_id": response_id,
                "response_group_id": group_id,
                "turn_id": turn_id,
                "phase": "final",
                "channel": "both",
                "emission_mode": "full_text",
                "output_risk_class": "unknown",
                "required_gate_mode": "full_text",
                "policy_hash": "policy",
                "active_subject_ref": "unknown",
                "evidence_snapshot_hash": "evidence",
            },
            source_event_id=intent.event_uid,
            correlation={"turn_id": turn_id},
        )
    finally:
        conn.close()
    return ResponseRunFacts(
        response_id=response_id,
        response_group_id=group_id,
        turn_id=turn_id,
        started_event_uid=started.event_uid,
    )


@pytest.mark.parametrize("pair", ["complete_cancel", "cancel_fail"])
def test_response_terminal_two_connection_race_has_one_winner(
    tmp_path: Path,
    pair: str,
) -> None:
    """Two ResponseTerminalizers race; exactly one terminal, one publish."""
    path = tmp_path / f"{pair}.db"
    facts = _seed_started(path, "RESP-race", "T-race")
    bus = CommittedEventBus()
    published: list[str] = []
    publish_lock = threading.Lock()

    def _published(event: Event) -> None:
        with publish_lock:
            published.append(event.event_uid)

    bus.subscribe(_published)

    def _terminalizer(conn: sqlite3.Connection) -> ResponseTerminalizer:
        return ResponseTerminalizer(
            lambda: conn,
            close_after=False,
            committed_event_bus=bus,
        )

    if pair == "complete_cancel":
        calls = (
            lambda conn: _terminalizer(conn).complete(facts, response_hash="done"),
            lambda conn: _terminalizer(conn).cancel(
                facts,
                reason="user_stop",
                cancel_scope="generation",
            ),
        )
    else:
        calls = (
            lambda conn: _terminalizer(conn).cancel(
                facts,
                reason="user_stop",
                cancel_scope="generation",
            ),
            lambda conn: _terminalizer(conn).fail(facts, reason="daemon_restart"),
        )

    outcomes = _race_two_connections(path, calls)
    winners = [o for o in outcomes if isinstance(o, TerminalCommitted)]
    losers = [o for o in outcomes if isinstance(o, AlreadyTerminal)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].event.event_uid == losers[0].event.event_uid
    assert published == [winners[0].event.event_uid]

    observer = _raw_connection(path)
    total = sum(_event_count(observer, terminal) for terminal in _RESPONSE_TERMINALS)
    assert total == 1
    observer.close()


def _assert_terminal_injection_is_atomic(path: Path, stage: str) -> None:
    """Inject one failure stage into the terminal CAS and prove it left nothing."""
    facts = _seed_started(path, f"RESP-{stage}", f"T-{stage}")
    bus = CommittedEventBus()
    published: list[str] = []
    bus.subscribe(_publish_sink(published))

    def _boom(observed: str) -> None:
        if observed == stage:
            msg = f"injected failure at {stage}"
            raise RuntimeError(msg)

    conn = open_event_log(path)
    try:
        terminalizer = ResponseTerminalizer(
            lambda: conn,
            close_after=False,
            committed_event_bus=bus,
            failure_injector=_boom,
        )
        with pytest.raises(RuntimeError, match="injected failure"):
            terminalizer.complete(facts, response_hash="done")
        assert sum(_event_count(conn, t) for t in _RESPONSE_TERMINALS) == 0
        assert published == []
    finally:
        conn.close()


def test_response_terminal_failure_injection_is_all_or_nothing(tmp_path: Path) -> None:
    """§9.4: an injected failure at any stage leaves zero rows and no publish."""
    for stage in ("after_terminal_check", "after_event_append", "before_commit"):
        _assert_terminal_injection_is_atomic(tmp_path / f"inject-{stage}.db", stage)

    # The same all-or-nothing contract holds for the opening append.
    path = tmp_path / "inject-start.db"
    conn = open_event_log(path)
    intent = _emit_intent(conn, "T-start-inject")
    started_published: list[str] = []
    start_bus = CommittedEventBus()
    start_bus.subscribe(_publish_sink(started_published))

    def _boom_start(observed: str) -> None:
        if observed == "before_commit":
            msg = "injected failure at before_commit"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="injected failure"):
        append_response_started(
            conn,
            payload={
                "response_id": "RESP-start",
                "response_group_id": "RGRP-start",
                "turn_id": "T-start-inject",
                "phase": "final",
                "channel": "both",
                "emission_mode": "full_text",
                "output_risk_class": "unknown",
                "required_gate_mode": "full_text",
                "policy_hash": "p",
                "active_subject_ref": "unknown",
                "evidence_snapshot_hash": "e",
            },
            source_event_id=intent.event_uid,
            committed_event_bus=start_bus,
            failure_injector=_boom_start,
        )
    assert _event_count(conn, "response.started") == 0
    assert started_published == []
    conn.close()


# --- cancel -----------------------------------------------------------------


def test_cancel_before_first_llm_call_produces_cancelled_and_no_surface_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9.4 cancellation-before-first-delta: nothing is rendered, no failure."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    intent = _emit_intent(runtime.conn, "T-early")
    entered = threading.Event()
    released = threading.Event()

    def _blocking_decide(_trigger: Event, _ctx: DecideContext) -> Any:  # noqa: ANN401
        entered.set()
        released.wait(timeout=5)
        return SimpleNamespace(
            response_plan=_plan(),
            events_emitted=(),
            stream_failure=None,
            route=None,
            last_gate_event_uid=None,
            turn_id=None,
            attention_channel="voice_notify",
        )

    _script_decide(monkeypatch, _blocking_decide)
    cancel = make_response_cancel_callable(runtime)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        assert entered.wait(timeout=5)
        assert runtime.response_runs is not None
        response_id = next(iter(runtime.response_runs.open_runs())).response_id
        assert cancel(response_id, "generation", "user_stop") == "cancelled"
        released.set()
        with pytest.raises(ResponseCancelledError):
            future.result(timeout=10)

    cancelled = _payloads(runtime.conn, "response.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["cancel_scope"] == "generation"
    assert cancelled[0]["reason"] == "user_stop"
    assert _event_count(runtime.conn, "response.completed") == 0
    for surface_type in (
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ):
        assert _event_count(runtime.conn, surface_type) == 0, surface_type
    assert _event_count(runtime.conn, "turn.failed") == 0
    assert runtime.response_runs.get(response_id) is None
    runtime.conn.close()


def test_cancel_during_action_wait_leaves_action_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5 headline: a response cancel never touches the action it is waiting on."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    intent = _emit_intent(runtime.conn, "T-action")
    action_id = "A-live"
    waiting = threading.Event()
    worker_done = threading.Event()
    event_log_path = runtime.runtime_paths.event_log

    def _late_worker() -> None:
        """Stand in for a dispatched Codex worker that finishes on its own."""
        time.sleep(0.4)
        conn = open_event_log(event_log_path)
        try:
            emit_event(
                conn,
                type="worker.reported",
                payload={"run_id": "RUN-live", "action_id": action_id, "status": "success"},
                correlation={"action_id": action_id, "turn_id": "T-action"},
            )
        finally:
            conn.close()
        worker_done.set()

    def _dispatching_decide(_trigger: Event, _ctx: DecideContext) -> Any:  # noqa: ANN401
        if not waiting.is_set():
            register_live_action(turn_id="T-action", action_id=action_id)
            threading.Thread(target=_late_worker, daemon=True).start()
            waiting.set()
            return SimpleNamespace(
                response_plan=None,
                events_emitted=(),
                stream_failure=None,
                route=None,
                last_gate_event_uid=None,
                turn_id="T-action",
                attention_channel="queue_review",
            )
        return SimpleNamespace(
            response_plan=_plan(),
            events_emitted=(),
            stream_failure=None,
            route=None,
            last_gate_event_uid=None,
            turn_id="T-action",
            attention_channel="voice_notify",
        )

    _script_decide(monkeypatch, _dispatching_decide)
    cancel = make_response_cancel_callable(runtime)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
            trigger_timeout_s=5.0,
        )
        assert waiting.wait(timeout=5)
        assert runtime.response_runs is not None
        # Wait until the run is actually parked in the trigger waiter.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runs = runtime.response_runs.open_runs()
            if runs and runs[0].state == "waiting_action":
                break
            time.sleep(0.005)
        run = runtime.response_runs.open_runs()[0]
        assert run.state == "waiting_action"
        started_at = time.monotonic()
        assert cancel(run.response_id, "generation", "user_stop") == "cancelled"
        with pytest.raises(ResponseCancelledError):
            future.result(timeout=10)
        assert time.monotonic() - started_at < 2.0

    assert _event_count(runtime.conn, "response.cancelled") == 1
    assert _event_count(runtime.conn, "action.cancelled") == 0
    assert worker_done.wait(timeout=5)
    assert _event_count(runtime.conn, "worker.reported") == 1
    assert runtime.response_runs.get(run.response_id) is None
    runtime.conn.close()


def test_cancel_after_completion_keeps_completed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A6 / D10: after response.completed a cancel writes nothing."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-late")
    drive_turn(
        runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )
    response_id = _payloads(runtime.conn, "response.started")[0]["response_id"]
    assert runtime.response_runs is not None
    # drive_turn's finally unregisters, so re-register to reach the terminal path.
    facts = ResponseRunFacts(
        response_id=response_id,
        response_group_id=stable_response_group_id("T-late"),
        turn_id="T-late",
        started_event_uid=_rows(runtime.conn, "response.started")[0][2],
    )
    snapshot = LLMSessionFactory(_LLM_CONFIG).snapshot(None)
    terminalizer = ResponseTerminalizer(lambda: runtime.conn, close_after=False)
    outcome = terminalizer.cancel(facts, reason="user_stop", cancel_scope="generation")
    assert isinstance(outcome, AlreadyTerminal)
    assert outcome.event.type == "response.completed"
    assert _event_count(runtime.conn, "response.cancelled") == 0
    assert _event_count(runtime.conn, "surface.response_emitted") == 1
    assert snapshot.model == "gpt-fast"
    runtime.conn.close()


def test_cancel_times_out_without_writing_a_terminal(tmp_path: Path) -> None:
    """A-21/A-22: a contended CAS gives up, writes nothing, and retries clean."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True, cancel_timeout_ms=50)
    facts = _seed_started(runtime.runtime_paths.event_log, "RESP-busy", "T-busy")
    assert runtime.response_runs is not None
    factory = LLMSessionFactory(_LLM_CONFIG)
    snapshot = factory.snapshot(None)
    client = factory.create(snapshot, response_id=facts.response_id)
    policy = legacy_full_text_policy(
        evidence_snapshot_hash="e",
        preset_snapshot_hash=snapshot.snapshot_hash,
    )
    run = start_response_run(
        runtime.conn,
        turn_id="T-busy",
        trigger_event_uid=facts.started_event_uid,
        request_client=client,
        policy=policy,
        response_id="RESP-busy2",
    )
    runtime.response_runs.register(run)
    cancel = make_response_cancel_callable(runtime)

    holder = _raw_connection(runtime.runtime_paths.event_log)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, "
        "source_event_id, correlation_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "hold-uid",
            "surface.user_intent",
            1,
            1,
            '{"transcript":"x","turn_id":"T-busy"}',
            None,
            None,
        ),
    )
    try:
        started = time.monotonic()
        assert cancel(run.response_id, "generation", "user_stop") == "timeout"
        assert time.monotonic() - started < 0.25
        assert _event_count(runtime.conn, "response.cancelled") == 0
        # Read into locals: the token is a property, and mypy would narrow a
        # direct `is False` assertion into the later `is True` one.
        cancelled_after_timeout = run.cancellation_token.is_cancelled
        assert cancelled_after_timeout is False
    finally:
        holder.rollback()
        holder.close()

    assert cancel(run.response_id, "generation", "user_stop") == "cancelled"
    assert _event_count(runtime.conn, "response.cancelled") == 1
    cancelled_after_retry = run.cancellation_token.is_cancelled
    assert cancelled_after_retry is True
    runtime.conn.close()


def test_completed_without_delivery_when_render_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7 accepted gap, pinned: completed with no surface.response_emitted."""
    runtime = _make_runtime(tmp_path, lifecycle=True)
    _script_decide(monkeypatch, _final_result())

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "render exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(runtime_module, "render_response", _boom)
    intent = _emit_intent(runtime.conn, "T-render")

    with pytest.raises(RuntimeError, match="render exploded"):
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )

    assert _event_count(runtime.conn, "response.completed") == 1
    assert _event_count(runtime.conn, "surface.response_emitted") == 0
    # The completed terminal already won, so the except-arm's fail is a no-op.
    assert _event_count(runtime.conn, "response.failed") == 0
    runtime.conn.close()


# --- A3 / A4: per-run request clients ---------------------------------------


class _FakeCompletions:
    """Minimal OpenAI chat-completions stand-in with a rendezvous barrier."""

    def __init__(self, model: str, barrier: threading.Barrier | None = None) -> None:
        """Bind the model this fake reports and an optional overlap barrier."""
        self.model = model
        self.barrier = barrier

    def create(self, **_kwargs: object) -> object:
        """Return one batch completion, holding both runs open together."""
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        return SimpleNamespace(
            id=f"resp-{self.model}",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.model, tool_calls=None),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=11,
                completion_tokens=3,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
        )


def test_concurrent_response_runs_use_isolated_request_clients(tmp_path: Path) -> None:
    """A3 / D-03: two overlapping runs never share provider identity or metadata."""
    db_path = tmp_path / "cost.db"
    # Create the log (schema + persisted WAL mode) before the threads open it.
    # `open_event_log` issues `PRAGMA journal_mode = WAL` *before* it sets
    # `busy_timeout`, so two threads racing to create the same file would hit a
    # bare "database is locked". Production never races that way: L6 bootstrap
    # provisions the log long before any worker thread opens one.
    open_event_log(db_path).close()
    factory = LLMSessionFactory(_LLM_CONFIG)
    barrier = threading.Barrier(2)

    clients = []
    for preset in ("fast", "deep"):
        snapshot = factory.snapshot(preset)
        client = factory.create(snapshot, response_id=new_response_id())
        client._openai_client = SimpleNamespace(  # noqa: SLF001 - provider fixture seam
            chat=SimpleNamespace(completions=_FakeCompletions(snapshot.model, barrier)),
        )
        clients.append(client)

    def _run(client: Any) -> Any:  # noqa: ANN401
        # One connection and one CostRecorder per thread: the Event Log is
        # opened with check_same_thread=True, so the two runs must contend
        # through SQLite the way two daemon turn workers really would.
        conn = open_event_log(db_path)
        try:
            return CostRecorder(conn, pricing_table={}).chat(
                client,
                messages=[{"role": "user", "content": "hi"}],
                system="",
                kind="decision",
                turn_id="T-iso",
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=15) for f in [pool.submit(_run, c) for c in clients]]

    assert clients[0] is not clients[1]
    assert clients[0].last_metadata is not clients[1].last_metadata
    assert clients[0].last_llm_request_id != clients[1].last_llm_request_id
    assert clients[0].model == "gpt-fast"
    assert clients[1].model == "gpt-deep"
    assert {r.llm_request_id for r in results} == {
        clients[0].last_llm_request_id,
        clients[1].last_llm_request_id,
    }

    conn = open_event_log(db_path)
    cost_rows = _payloads(conn, "cost.recorded")
    assert len(cost_rows) == 2
    assert {row["llm_request_id"] for row in cost_rows} == {
        clients[0].last_llm_request_id,
        clients[1].last_llm_request_id,
    }
    assert {row["model"] for row in cost_rows} == {"gpt-fast", "gpt-deep"}
    dispositions = conn.execute("SELECT COUNT(*) FROM cost_accounting_dispositions").fetchone()
    assert dispositions is not None
    assert int(dispositions[0]) == 2
    conn.close()


def test_request_client_refuses_switch_model_and_pins_its_snapshot() -> None:
    """A4: the per-run client is immutable and holds a deep config copy."""
    source: dict[str, Any] = {
        "provider": "openai",
        "default_preset": "fast",
        "presets": {"fast": {"provider": "openai", "model": "gpt-fast", "max_tokens": 8}},
    }
    factory = LLMSessionFactory(source)
    snapshot = factory.snapshot(None)
    client = factory.create(snapshot, response_id="RESP-pin")

    with pytest.raises(ImmutableRequestClientError):
        client.switch_model("deep")
    assert client.model == "gpt-fast"
    assert client.provider == "openai"
    assert client.active_preset == "fast"
    assert client.preset_snapshot == snapshot

    # Mutating the source config — including the NESTED preset dict — must
    # not reach either the client or a later snapshot.
    source["presets"]["fast"]["model"] = "mutated"
    source["default_preset"] = "gone"
    assert client.model == "gpt-fast"
    assert factory.snapshot(None).model == "gpt-fast"
    with pytest.raises(UnknownRequestPresetError):
        factory.snapshot("missing")


def test_provider_failure_records_cost_and_fails_the_response_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F27: a raised turn yields one response.failed and re-raises unmasked."""
    runtime = _make_runtime(tmp_path, lifecycle=True)

    def _raising_decide(_trigger: Event, _ctx: DecideContext) -> Any:  # noqa: ANN401
        msg = "provider exploded"
        raise RuntimeError(msg)

    _script_decide(monkeypatch, _raising_decide)
    intent = _emit_intent(runtime.conn, "T-fail")

    with pytest.raises(RuntimeError, match="provider exploded"):
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )

    failed = _payloads(runtime.conn, "response.failed")
    assert len(failed) == 1
    assert failed[0]["reason"] == "turn_raised"
    assert failed[0]["retryable"] is False
    assert _event_count(runtime.conn, "response.completed") == 0
    runtime.conn.close()


# --- A7: boot reconciliation ------------------------------------------------


def test_restart_reconciler_closes_open_response_exactly_once(tmp_path: Path) -> None:
    """A7 / F14: one daemon_restart per open run, idempotent across boots."""
    path = tmp_path / "reconcile.db"
    open_facts = _seed_started(path, "RESP-open", "T-open")
    closed_facts = _seed_started(path, "RESP-closed", "T-closed")
    conn = open_event_log(path)
    ResponseTerminalizer(lambda: conn, close_after=False).complete(
        closed_facts,
        response_hash="already-done",
    )
    assert {run.response_id for run in open_response_runs(conn)} == {open_facts.response_id}

    first = reconcile_open_responses(conn, ResponseTerminalizer(lambda: conn, close_after=False))
    assert len(first) == 1
    assert first[0].payload["reason"] == "daemon_restart"
    assert first[0].payload["response_id"] == open_facts.response_id

    second = reconcile_open_responses(conn, ResponseTerminalizer(lambda: conn, close_after=False))
    assert second == ()
    assert _event_count(conn, "response.failed") == 1
    assert _event_count(conn, "response.completed") == 1
    assert open_response_runs(conn) == ()
    conn.close()


# --- FSM --------------------------------------------------------------------


def _fresh_run(tmp_path: Path, response_id: str = "RESP-fsm") -> Any:  # noqa: ANN401
    """Build one live ResponseRun over a throwaway Event Log."""
    conn = open_event_log(tmp_path / f"{response_id}.db")
    intent = _emit_intent(conn, "T-fsm")
    factory = LLMSessionFactory(_LLM_CONFIG)
    snapshot = factory.snapshot(None)
    run = start_response_run(
        conn,
        turn_id="T-fsm",
        trigger_event_uid=intent.event_uid,
        request_client=factory.create(snapshot, response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash=evidence_snapshot_hash(conn),
            preset_snapshot_hash=snapshot.snapshot_hash,
        ),
        response_id=response_id,
    )
    conn.close()
    return run


@pytest.mark.parametrize(
    ("path_states", "illegal"),
    [
        ((), "completed"),
        (("waiting_action",), "completed"),
        (("finalizing",), "generating"),
        (("finalizing", "completed"), "failed"),
        (("failed",), "completed"),
    ],
)
def test_illegal_response_transitions_are_rejected(
    tmp_path: Path,
    path_states: tuple[str, ...],
    illegal: str,
) -> None:
    """§9.4: every legal edge is taken and every illegal edge is refused."""
    run = _fresh_run(tmp_path, f"RESP-{'-'.join(path_states) or 'root'}-{illegal}")
    assert run.state == "generating"
    for state in path_states:
        run.mark(state)
    with pytest.raises(IllegalResponseTransitionError):
        run.mark(illegal)


@pytest.mark.parametrize("attempted", ["generating", "waiting_action", "finalizing", "completed"])
def test_marking_a_cancelled_run_reports_cancellation(tmp_path: Path, attempted: str) -> None:
    """The cancel-wins-first race ends the turn as cancelled, not as a crash.

    ``request_response_cancel`` wins the terminal CAS, calls ``mark`` and only
    then sets the token, so an owning worker can pass its token check and
    still reach ``mark`` on an already-cancelled run. That interleaving must
    surface as :class:`ResponseCancelledError` — ``drive_turn`` re-raises it
    and the watcher skips ``turn.failed`` — never as an illegal transition.
    """
    run = _fresh_run(tmp_path, f"RESP-cancel-race-{attempted}")
    run.mark("cancelled")
    with pytest.raises(ResponseCancelledError):
        run.mark(attempted)
    assert run.state == "cancelled"
    assert run.is_open is False


def test_cancel_winning_the_completion_cas_never_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landing after ``finalizing`` skips render and reports cancelled.

    The window is between ``drive_turn``'s last token check and its completion
    CAS. It is driven deterministically by cancelling from inside
    ``render_response``'s stand-in's caller — the terminalizer — so no sleep
    or thread interleaving is involved.
    """
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    intent = _emit_intent(runtime.conn, "T-cas-race")
    rendered: list[object] = []
    def _record_render(*_args: object, **kwargs: object) -> None:
        rendered.append(kwargs)

    monkeypatch.setattr(runtime_module, "render_response", _record_render)

    real_complete = ResponseTerminalizer.complete

    def _cancel_first(
        self: ResponseTerminalizer,
        facts: ResponseRunFacts,
        **kwargs: Any,  # noqa: ANN401 - forwarded verbatim.
    ) -> Any:  # noqa: ANN401 - TerminalOutcome.
        """Let a cancel win the CAS immediately before the completion append."""
        self.cancel(facts, reason="user_stop", cancel_scope="generation")
        return real_complete(self, facts, **kwargs)

    monkeypatch.setattr(ResponseTerminalizer, "complete", _cancel_first)

    with pytest.raises(ResponseCancelledError):
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )

    assert rendered == []
    assert _event_count(runtime.conn, "response.cancelled") == 1
    assert _event_count(runtime.conn, "response.completed") == 0
    assert _event_count(runtime.conn, "response.failed") == 0
    assert _event_count(runtime.conn, "surface.response_open") == 0
    runtime.conn.close()


def test_response_run_legal_edges_all_succeed(tmp_path: Path) -> None:
    """The full ADR D1 happy path plus the waiting-action round trip."""
    run = _fresh_run(tmp_path, "RESP-legal")
    for state in ("waiting_action", "generating", "waiting_action", "finalizing", "completed"):
        run.mark(state)
    assert run.state == "completed"
    assert run.is_open is False


# --- surface seam -----------------------------------------------------------


def test_cancel_endpoint_registered_only_when_wired() -> None:
    """Flag-off surface proof plus the operator seam's outcome mapping."""
    broadcaster = InherentBroadcaster()
    bare = create_app(
        InherentDeps(submit_callable=lambda _text: "T1", broadcaster=broadcaster),
    )
    assert not [
        route for route in bare.routes if getattr(route, "path", "") == "/inherent/cancel-response"
    ]
    with TestClient(bare) as client:
        missing_route = client.post("/inherent/cancel-response", json={"response_id": "x"})
        assert missing_route.status_code == 404

    seen: list[tuple[str, str, str]] = []

    def _cancel(response_id: str, scope: str, reason: str) -> str:
        seen.append((response_id, scope, reason))
        if scope != "generation":
            return "unsupported_scope"
        return "cancelled" if response_id == "RESP-known" else "unknown_response"

    wired = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=broadcaster,
            cancel_response_callable=_cancel,
        ),
    )
    with TestClient(wired) as client:
        ok = client.post(
            "/inherent/cancel-response",
            json={"response_id": "RESP-known", "scope": "generation", "reason": "user_stop"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"outcome": "cancelled"}
        assert seen[-1] == ("RESP-known", "generation", "user_stop")

        missing = client.post("/inherent/cancel-response", json={"response_id": "nope"})
        assert missing.status_code == 200
        assert missing.json() == {"outcome": "unknown_response"}

        for scope in ("foreground_output", "not-a-scope"):
            reply = client.post(
                "/inherent/cancel-response",
                json={"response_id": "RESP-known", "scope": scope},
            )
            assert reply.status_code == 200
            assert reply.json() == {"outcome": "unsupported_scope"}


def test_unsupported_scope_is_rejected_before_any_write(tmp_path: Path) -> None:
    """Q2: ``foreground_output`` is refused until a playback lease exists."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    outcome = request_response_cancel(
        runtime.response_runs,
        ResponseTerminalizer(lambda: runtime.conn, close_after=False),
        ResponseCancelRequest(
            request_id="CREQ-1",
            response_id="RESP-any",
            scope="foreground_output",
            reason="user_stop",
        ),
    )
    assert isinstance(outcome, CancelRejected)
    assert outcome.reason == "unsupported_scope"
    assert sum(_event_count(runtime.conn, t) for t in _RESPONSE_TERMINALS) == 0
    runtime.conn.close()


# --- A11: flag graph --------------------------------------------------------


def test_response_activation_downgrades_without_wave1_primitives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A11: the lifecycle flag needs the Wave-1 primitives it writes through."""
    reset_realtime_trace()
    config = _realtime_config(lifecycle=True, cancel=True, wave1=False)
    with caplog.at_level("WARNING", logger="jarvis.runtime"):
        activation = _wave4_response_activation(config)

    assert activation.flags.all_disabled
    assert activation.reason == "wave1_primitives_disabled"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    downgrades = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "response_activation_downgraded"
    ]
    assert len(downgrades) == 1
    assert downgrades[0].attributes["reason"] == "wave1_primitives_disabled"


def test_cancel_flag_downgrades_without_lifecycle_flag(tmp_path: Path) -> None:
    """A11: the cancel seam needs a surviving run lifecycle, and stays unrouted."""
    config = _realtime_config(lifecycle=False, cancel=True)
    activation = _wave4_response_activation(config)
    assert activation.reason == "lifecycle_flag_disabled"
    assert activation.flags.independent_response_cancel is False

    runtime = _make_runtime(tmp_path, lifecycle=False, cancel=True)
    assert runtime.response_flags.all_disabled
    assert runtime.response_runs is None
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            cancel_response_callable=(
                make_response_cancel_callable(runtime)
                if runtime.response_flags.independent_response_cancel
                else None
            ),
        ),
    )
    assert not [
        route for route in app.routes if getattr(route, "path", "") == "/inherent/cancel-response"
    ]
    runtime.conn.close()


def test_realtime_parent_disabled_keeps_both_switches_off() -> None:
    """``realtime.enabled: false`` short-circuits the whole Wave-4A graph."""
    config = _realtime_config(lifecycle=True, cancel=True, enabled=False)
    activation = _wave4_response_activation(config)
    assert activation.flags.all_disabled
    assert activation.reason == "realtime_parent_disabled"
    assert _wave4_response_flags({}).all_disabled


def test_wave4_response_flags_ship_disabled() -> None:
    """Rollout safety: the shipped config leaves both Wave-4A switches off."""
    config_path = repo_root() / "config" / "jarvis.yaml"
    shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    response_block = shipped["realtime"]["response"]
    assert Wave4ResponseFlags.from_mapping(response_block).all_disabled
    assert response_block["cancel_timeout_ms"] == 500
    assert _wave4_response_flags(shipped).all_disabled
    assert replace(Wave4ResponseFlags(), response_run_lifecycle=True).all_disabled is False


def _unused_iterator() -> Iterator[int]:  # pragma: no cover - typing import anchor
    """Keep the ``Iterator`` typing import honest for the TYPE_CHECKING block."""
    yield 0


def test_run_client_keeps_the_presets_request_body_identity() -> None:
    """A run client sends the preset's extra_body / reasoning_effort, and the hash sees them.

    Found live 2026-09-04: with ``response_run_lifecycle`` on, every decision
    request went through a snapshot that had neither field, so DeepSeek's
    ``thinking: {type: disabled}`` never reached the wire and a 3 700-character
    reasoning trace consumed the whole ``max_tokens`` budget.
    """
    config = json.loads(json.dumps(_LLM_CONFIG))
    config["presets"]["fast"]["extra_body"] = {"thinking": {"type": "disabled"}}
    config["presets"]["fast"]["reasoning_effort"] = "minimal"
    factory = LLMSessionFactory(config)
    fast = factory.snapshot("fast")
    client = factory.create(fast, response_id=new_response_id())
    assert client._extra_body == {"thinking": {"type": "disabled"}}  # noqa: SLF001 - request identity seam
    assert client._reasoning_effort == "minimal"  # noqa: SLF001 - request identity seam
    plain = LLMSessionFactory(_LLM_CONFIG).snapshot("fast")
    assert fast.snapshot_hash != plain.snapshot_hash
    assert factory.create(plain, response_id=new_response_id())._extra_body == {}  # noqa: SLF001
