"""ADR-0008 D6 acceptance: deterministic lifecycle commentary.

Every check runs against a real on-disk Event Log and the shipped runtime
observer; nothing about the commentary path is faked, because the property
under test is precisely that a phrase is spoken only when a durable action
row justifies it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import yaml

from jarvis import runtime as runtime_module
from jarvis.decision.commentary import COMMENTARY_ATTENTION_CHANNEL, commentary_intent_for
from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, _wave4_response_activation, drive_turn, inherent_loop
from jarvis.shared import Event
from jarvis.shared.realtime import Wave1FeatureFlags, stable_response_group_id
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_media
from tests.canary._helpers import repo_root
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _FakeProvider,
    _player,
    _submit_response,
    _terminal_rows,
)
from tests.integration.test_wave2_streaming_media import _config as _media_config

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path
    from typing import Self

    import pytest

# --- helpers ---------------------------------------------------------------


def _action_event(event_type: str, *, action_id: str = "ACT-1") -> Event:
    """Build one committed-shaped action event without touching the log."""
    return Event(
        event_uid=f"uid-{event_type}-{action_id}",
        type=event_type,
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"action_id": action_id, "tool_name": "get_current_time"},
        source_event_id=None,
        correlation={"action_id": action_id, "turn_id": "T-1"},
    )


# --- D6 mapping ------------------------------------------------------------


def test_four_d6_rows_map_to_their_exact_intent_and_phrase() -> None:
    """ADR-0008 D6's action table, verbatim, with the action id as subject."""
    expected = {
        "action.dispatched": ("acknowledge", "我开始处理了。"),
        "action.running": ("progress", "任务已经在运行。"),
        "action.result_observed": ("progress", "结果回来了，我整理一下。"),  # noqa: RUF001 — intentional Chinese punctuation.
        "action.failed": ("error", "这一步失败了，我告诉你具体原因。"),  # noqa: RUF001 — intentional Chinese punctuation.
    }
    for event_type, (intent_type, phrase) in expected.items():
        intent = commentary_intent_for(_action_event(event_type, action_id="ACT-7"))
        assert intent is not None, event_type
        assert intent.intent_type == intent_type
        assert intent.content_hint == phrase
        assert intent.subject_ref == "ACT-7"
        assert intent.surface_hint == "speech"
        assert intent.freshness_required is True


def test_non_mapped_event_types_return_none() -> None:
    """Only the four D6 action rows speak; every other row is silent."""
    for event_type in ("run.started", "gate.evaluated", "action.cancelled",
                       "action.timeout_assumed", "turn.started", "response.completed"):
        assert commentary_intent_for(_action_event(event_type)) is None, event_type


def test_mapped_row_without_action_id_returns_none() -> None:
    """``subject_ref`` is the action id; without one there is nothing to say."""
    event = Event(
        event_uid="uid-no-action",
        type="action.dispatched",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"tool_name": "get_current_time"},
        source_event_id=None,
        correlation=None,
    )
    assert commentary_intent_for(event) is None


def test_intent_is_frozen_and_never_an_event() -> None:
    """Spec §3.6.3: PresentationIntent is a contract object, not a log row."""
    intent = commentary_intent_for(_action_event("action.dispatched"))
    assert intent is not None
    assert tuple(intent.__dataclass_fields__) == (
        "intent_type",
        "surface_hint",
        "subject_ref",
        "content_hint",
        "freshness_required",
    )
    assert COMMENTARY_ATTENTION_CHANNEL == "voice_notify"


# --- flag graph ------------------------------------------------------------


def _config(*, enabled: bool, lifecycle: bool, commentary: bool) -> dict[str, object]:
    """Build the `realtime` mapping the activation graph reads."""
    return {
        "realtime": {
            "enabled": enabled,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
            },
            "response": {"response_run_lifecycle": lifecycle},
            "commentary": {"enabled": commentary},
        },
    }


def test_shipped_config_leaves_commentary_off() -> None:
    """config/jarvis.yaml ships the switch off, like every other realtime flag."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    assert shipped["realtime"]["commentary"] == {"enabled": False}
    assert _wave4_response_activation(shipped).flags.lifecycle_commentary is False


def test_commentary_requires_the_response_run_lifecycle() -> None:
    """Without the lifecycle switch there is no ResponseRun to open at all."""
    activation = _wave4_response_activation(
        _config(enabled=True, lifecycle=False, commentary=True),
    )
    assert activation.reason == "lifecycle_flag_disabled"
    assert activation.requested.lifecycle_commentary is True
    assert activation.flags.lifecycle_commentary is False


def test_commentary_requires_realtime_enabled() -> None:
    """The parent switch off downgrades the whole graph, commentary included."""
    activation = _wave4_response_activation(
        _config(enabled=False, lifecycle=True, commentary=True),
    )
    assert activation.reason == "realtime_parent_disabled"
    assert activation.flags.lifecycle_commentary is False


def test_commentary_validates_with_its_two_preconditions() -> None:
    """Both preconditions met: the switch survives the graph."""
    activation = _wave4_response_activation(
        _config(enabled=True, lifecycle=True, commentary=True),
    )
    assert activation.reason == "validated"
    assert activation.flags.lifecycle_commentary is True


def test_non_boolean_truthy_never_enables_commentary() -> None:
    """Fail-closed `is True`, the same rule every other realtime flag uses."""
    config = _config(enabled=True, lifecycle=True, commentary=True)
    config["realtime"]["commentary"] = {"enabled": "yes"}  # type: ignore[index]
    assert _wave4_response_activation(config).flags.lifecycle_commentary is False


# --- observer harness ------------------------------------------------------


def _observer_config(*, commentary: bool) -> dict[str, Any]:
    """Config for a runtime with the ResponseRun lifecycle and D6 commentary."""
    return {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
            },
            "response": {"response_run_lifecycle": True},
            "commentary": {"enabled": commentary},
        },
    }


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
    },
}


def _make_runtime(tmp_path: Path, *, commentary: bool = True) -> JarvisRuntime:
    """Assemble the runtime the daemon's watchers receive."""
    paths = bootstrap_runtime(tmp_path)
    config = _observer_config(commentary=commentary)
    flags = _wave4_response_activation(config).flags
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(_LLM_CONFIG),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        response_flags=flags,
        llm_session_factory=LLMSessionFactory(_LLM_CONFIG),
        committed_event_bus=CommittedEventBus(),
    )


def _user_turn(conn: sqlite3.Connection, turn_id: str) -> Event:
    """Emit the user-intent trigger and the turn claim it originates."""
    intent = emit_event(
        conn,
        type="surface.user_intent",
        payload={
            "transcript": "现在几点",
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )
    emit_event(
        conn,
        type="turn.started",
        payload={"turn_id": turn_id, "trigger": intent.event_uid},
        source_event_id=intent.event_uid,
        correlation={"turn_id": turn_id},
    )
    return intent


def _action_row(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    action_id: str,
    turn_id: str | None,
    source_event_id: str | None = None,
) -> Event:
    """Emit one L4-shaped action lifecycle row."""
    payload: dict[str, Any] = {"action_id": action_id}
    if event_type == "action.result_observed":
        payload["semantics"] = "success"
    correlation: dict[str, str] = {"action_id": action_id}
    if turn_id is not None:
        correlation["turn_id"] = turn_id
    return emit_event(
        conn,
        type=event_type,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
    )


def _typed_payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


class _Observer:
    """Run the shipped observer the way the daemon does: its own loop and conn.

    The watcher anchors at the log's high-water mark when it starts, so a row
    only reaches it if it is emitted inside the ``with`` block — which is also
    the property that keeps a historical action from speaking fake progress.
    """

    def __init__(self, runtime: JarvisRuntime) -> None:
        """Bind the runtime whose event log the observer will poll."""
        self._runtime = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="test-commentary-observer")

    def _run(self) -> None:
        conn = open_event_log(self._runtime.runtime_paths.event_log)
        runtime = replace(self._runtime, conn=conn)

        async def _main() -> None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(
                inherent_loop._commentary_watcher(runtime, poll_interval_s=0.005),  # noqa: SLF001
            )
            self._ready.set()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

        try:
            asyncio.run(_main())
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def __enter__(self) -> Self:
        """Start the watcher and wait for it to take its boot anchor."""
        self._thread.start()
        assert self._ready.wait(timeout=5.0)
        time.sleep(0.1)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Cancel the watcher the way ``serve_inherent``'s teardown does."""
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        self._thread.join(timeout=5.0)
        assert not self._thread.is_alive()


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Block until ``predicate`` holds, or fail the test."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    msg = "observer did not reach the expected state in time"
    raise AssertionError(msg)


def _settle(seconds: float = 0.3) -> None:
    """Give the observer time to write something, for a must-stay-silent check."""
    time.sleep(seconds)


def _reader(runtime: JarvisRuntime) -> sqlite3.Connection:
    """Open a second connection for assertions while the observer runs."""
    return open_event_log(runtime.runtime_paths.event_log)


# --- observer: the happy path ----------------------------------------------


def test_action_row_speaks_one_commentary_run_in_the_turn_group(tmp_path: Path) -> None:
    """One acknowledge run, in the turn's own group, sourced on the action row."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    intent = _user_turn(runtime.conn, "T-ack")
    with _Observer(runtime):
        dispatched = _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-ack", turn_id="T-ack",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)

    started = _typed_payloads(reader, "response.started")
    assert len(started) == 1
    assert started[0]["phase"] == "commentary"
    assert started[0]["channel"] == "speech"
    assert started[0]["emission_mode"] == "deterministic"
    assert started[0]["required_gate_mode"] == "sentence"
    assert started[0]["active_subject_ref"] == "ACT-ack"
    assert started[0]["response_group_id"] == stable_response_group_id("T-ack")
    source = reader.execute(
        "SELECT source_event_id FROM events WHERE type = 'response.started'",
    ).fetchone()
    assert source[0] == dispatched.event_uid
    assert source[0] != intent.event_uid

    for event_type in ("surface.response_open", "surface.response_chunk",
                       "surface.response_emitted"):
        payloads = _typed_payloads(reader, event_type)
        assert len(payloads) == 1, event_type
        assert payloads[0]["phase"] == "commentary", event_type
        assert payloads[0]["response_id"] == started[0]["response_id"], event_type
        assert payloads[0]["response_group_id"] == started[0]["response_group_id"]
    assert _typed_payloads(reader, "surface.response_chunk")[0]["text"] == "我开始处理了。"
    assert _typed_payloads(reader, "surface.response_open")[0]["attention_channel"] == (
        COMMENTARY_ATTENTION_CHANNEL
    )
    assert _count(reader, "cost.recorded") == 0


# --- observer: origin filter -----------------------------------------------


def test_action_on_a_turn_with_no_turn_started_stays_silent(tmp_path: Path) -> None:
    """No claim row means no user asked; a system turn never gets commentary."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-sys", turn_id="T-sys")
        _action_row(runtime.conn, "action.running", action_id="ACT-sys", turn_id="T-sys")
        _settle()
    assert _count(reader, "response.started") == 0
    assert _count(reader, "surface.response_open") == 0


def test_reconciliation_originated_turn_stays_silent(tmp_path: Path) -> None:
    """A turn claimed from a reconciliation terminal is not a user question."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    sweep = emit_event(
        runtime.conn,
        type="action.timeout_assumed",
        payload={"action_id": "ACT-old", "reason": "supervisor_sweep"},
        correlation={"action_id": "ACT-old"},
    )
    emit_event(
        runtime.conn,
        type="turn.started",
        payload={"turn_id": "T-recon", "trigger": sweep.event_uid},
        source_event_id=sweep.event_uid,
        correlation={"turn_id": "T-recon"},
    )
    with _Observer(runtime):
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-r", turn_id="T-recon")
        _settle()
    assert _count(reader, "response.started") == 0


# --- observer: confirmation guard ------------------------------------------


def test_live_pending_confirmation_silences_commentary(tmp_path: Path) -> None:
    """An unresolved ask owns the surface; commentary waits for its answer."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-conf")
    emit_event(
        runtime.conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": "CONF-1",
            "action_snapshot": {"tool_name": "write_file", "risk_level": "high"},
            "template_line": "要写入吗",
            "expires_at_ms": int(time.time() * 1000) + 600_000,
        },
        correlation={"turn_id": "T-conf"},
    )
    with _Observer(runtime):
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-c", turn_id="T-conf")
        _settle()
        assert _count(reader, "response.started") == 0

        emit_event(
            runtime.conn,
            type="confirmation.rejected",
            payload={
                "confirmation_id": "CONF-1",
                "utterance_raw": "算了",
                "grammar_rule_id": "reject.zh.suan_le",
            },
            correlation={"turn_id": "T-conf"},
        )
        _action_row(runtime.conn, "action.running", action_id="ACT-c", turn_id="T-conf")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    assert _typed_payloads(reader, "surface.response_chunk")[0]["text"] == "任务已经在运行。"


# --- observer: coalescing and supersession ---------------------------------


def test_repeats_are_dropped_and_an_unheard_commentary_is_superseded(
    tmp_path: Path,
) -> None:
    """One phrase per (action_id, D6 row); a newer row cancels an unheard one."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-coal")
    with _Observer(runtime):
        dispatched = _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-c", turn_id="T-coal",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        _action_row(
            runtime.conn, "action.running", action_id="ACT-c", turn_id="T-coal",
            source_event_id=dispatched.event_uid,
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 2)
        _action_row(
            runtime.conn, "action.running", action_id="ACT-c", turn_id="T-coal",
            source_event_id=dispatched.event_uid,
        )
        _settle()

    started = _typed_payloads(reader, "response.started")
    assert len(started) == 2
    chunks = [payload["text"] for payload in _typed_payloads(reader, "surface.response_chunk")]
    assert chunks == ["我开始处理了。", "任务已经在运行。"]
    superseded = [
        payload
        for payload in _typed_payloads(reader, "response.cancelled")
        if payload["reason"] == "superseded"
    ]
    assert [payload["response_id"] for payload in superseded] == [started[0]["response_id"]]


def test_a_commentary_that_reached_the_speaker_finishes(tmp_path: Path) -> None:
    """Playback closes the run through `complete`; a later row cannot unspeak it."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-heard")
    with _Observer(runtime):
        dispatched = _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-h", turn_id="T-heard",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        first = _typed_payloads(reader, "response.started")[0]
        emit_event(
            runtime.conn,
            type="surface.playback_started",
            payload={
                "session_id": "SESS-1",
                "response_id": first["response_id"],
                "turn_id": "T-heard",
                "playback_generation_id": 1,
                "phase": "commentary",
                "channel": "speech",
                "speech_text_hash": "deadbeef",
            },
            correlation={"turn_id": "T-heard"},
        )
        _wait_until(lambda: _count(reader, "response.completed") == 1)
        _action_row(
            runtime.conn, "action.running", action_id="ACT-h", turn_id="T-heard",
            source_event_id=dispatched.event_uid,
        )
        _wait_until(lambda: _count(reader, "response.started") == 2)

    completed = _typed_payloads(reader, "response.completed")
    assert [payload["response_id"] for payload in completed] == [first["response_id"]]
    assert all(
        payload["response_id"] != first["response_id"]
        for payload in _typed_payloads(reader, "response.cancelled")
    )


def test_a_non_terminal_row_is_silent_once_its_action_terminal_committed(
    tmp_path: Path,
) -> None:
    """An acknowledge for work that already finished is stale, not truthful."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-late")
    dispatched = _action_row(
        runtime.conn, "action.dispatched", action_id="ACT-l", turn_id="T-late",
    )
    _action_row(
        runtime.conn, "action.result_observed", action_id="ACT-l", turn_id="T-late",
        source_event_id=dispatched.event_uid,
    )
    opened = inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime,
        action_event=dispatched,
        previous=None,
    )
    assert opened is None
    assert _count(reader, "response.started") == 0


# --- observer: the final is never delayed, dropped or superseded ------------


def _plan(text: str = "现在是下午三点。") -> ResponsePlan:
    """A fixed approved plan for the scripted final turn."""
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash="hash-final",
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


def _script_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the composition root's ``decide`` with a one-shot final."""

    def _decide(_trigger: Event, _ctx: object) -> object:
        return SimpleNamespace(
            response_plan=_plan(),
            events_emitted=(),
            stream_failure=None,
            route=None,
            last_gate_event_uid=None,
            turn_id=None,
            attention_channel="voice_notify",
        )

    monkeypatch.setattr(runtime_module, "decide", _decide)


def _drive_final(runtime: JarvisRuntime, intent: Event) -> None:
    """Drive the turn on its own connection, the way the turn worker does."""
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        drive_turn(
            replace(runtime, conn=conn),
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _surface_stream(conn: sqlite3.Connection) -> list[tuple[int, Event]]:
    """Return the whole `surface.response_*` stream as the TTS watcher sees it."""
    return inherent_loop._fetch_events_after(  # noqa: SLF001
        conn,
        after_id=0,
        event_types=(
            "surface.response_open",
            "surface.response_chunk",
            "surface.response_emitted",
        ),
    )


def test_final_shares_the_group_keeps_phase_final_and_plays_after_the_commentary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0006 §261: the commentary-to-final handoff is enqueue-after-drain."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    intent = _user_turn(runtime.conn, "T-both")
    _script_final(monkeypatch)
    with _Observer(runtime):
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-b", turn_id="T-both")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        _drive_final(runtime, intent)
        _settle()

    opens = _typed_payloads(reader, "surface.response_open")
    assert [payload["phase"] for payload in opens] == ["commentary", "final"]
    assert [payload["phase"] for payload in _typed_payloads(reader, "surface.response_chunk")] == [
        "commentary",
        "final",
    ]
    emitted = _typed_payloads(reader, "surface.response_emitted")
    assert [payload["phase"] for payload in emitted] == ["commentary", "final"]
    group = stable_response_group_id("T-both")
    assert {payload["response_group_id"] for payload in opens} == {group}
    assert opens[0]["response_id"] != opens[1]["response_id"]
    assert all(
        payload["reason"] != "superseded"
        for payload in _typed_payloads(reader, "response.cancelled")
    )

    # The media owner's own disposition for that exact pair of rows.
    reset_realtime_trace()
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(runtime.runtime_paths.event_log),
        boot_high_water_id=0,
        config=_media_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _surface_stream(reader)))
            assert pipeline.wait_until_idle(timeout_s=5.0)
    finally:
        assert pipeline.close()
    enqueued = [
        point.attributes
        for point in realtime_trace_snapshot()
        if point.name == "media_after_drain_enqueued"
    ]
    assert [attributes["phase"] for attributes in enqueued] == ["final"]
    assert [attributes["response_group_id"] for attributes in enqueued] == [group]
    terminals = {
        str(payload["response_id"]): kind for kind, payload in _terminal_rows(reader)
    }
    assert terminals[str(opens[0]["response_id"])] == "surface.playback_completed"
    assert terminals[str(opens[1]["response_id"])] == "surface.playback_completed"


# --- flag off: nothing this card added is reachable -------------------------

_VOLATILE_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"response_id", "trigger", "evidence_snapshot_hash", "policy_hash"},
)
"""Values that differ between two runs of the same scenario for reasons this
card does not own: a minted response id, the trigger's own uid, and the two
hashes derived from the log's byte position when a run opened.
"""


def _log_shape(
    conn: sqlite3.Connection,
    *,
    drop_response_ids: frozenset[str] = frozenset(),
) -> list[tuple[str, dict[str, Any]]]:
    """Return `(type, payload)` for the whole log, in append order."""
    shape: list[tuple[str, dict[str, Any]]] = []
    for event_type, raw in conn.execute(
        "SELECT type, payload_json FROM events ORDER BY id ASC",
    ):
        payload = json.loads(raw)
        if str(payload.get("response_id", "")) in drop_response_ids:
            continue
        shape.append(
            (
                str(event_type),
                {
                    key: "<volatile>" if key in _VOLATILE_PAYLOAD_KEYS else value
                    for key, value in sorted(payload.items())
                },
            ),
        )
    return shape


def _commentary_response_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Every response id this card's observer minted."""
    return frozenset(
        str(payload["response_id"])
        for payload in _typed_payloads(conn, "response.started")
        if payload.get("phase") == "commentary"
    )


def test_flag_off_event_log_is_what_the_observer_never_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off, the log is the flag-on log with the commentary rows removed.

    The observer is the only new production write site — `render_response`'s
    new `phase` argument defaults to the string it used to hardcode — so
    "byte-identical to today" is exactly this equality: subtract the rows the
    commentary runs own and nothing else moved, changed or reordered.
    """
    off = _make_runtime(tmp_path / "off", commentary=False)
    off_reader = _reader(off)
    _script_final(monkeypatch)
    off_intent = _user_turn(off.conn, "T-cmp")
    _action_row(off.conn, "action.dispatched", action_id="ACT-cmp", turn_id="T-cmp")
    _drive_final(off, off_intent)
    assert off.response_flags.lifecycle_commentary is False

    on = _make_runtime(tmp_path / "on", commentary=True)
    on_reader = _reader(on)
    on_intent = _user_turn(on.conn, "T-cmp")
    with _Observer(on):
        _action_row(on.conn, "action.dispatched", action_id="ACT-cmp", turn_id="T-cmp")
        _wait_until(lambda: _count(on_reader, "surface.response_emitted") == 1)
        _drive_final(on, on_intent)
        _settle()

    assert _commentary_response_ids(off_reader) == frozenset()
    assert len(_commentary_response_ids(on_reader)) == 1
    assert _log_shape(off_reader) == _log_shape(
        on_reader,
        drop_response_ids=_commentary_response_ids(on_reader),
    )
    off_phases = {
        payload.get("phase")
        for event_type in ("surface.response_open", "surface.response_chunk",
                           "surface.response_emitted")
        for payload in _typed_payloads(off_reader, event_type)
    }
    assert off_phases == {"final"}


def test_the_observer_task_is_created_only_under_the_flag() -> None:
    """`serve_inherent` names `_commentary_watcher` exactly once, under the flag."""
    tree = ast.parse((repo_root() / "jarvis" / "runtime" / "inherent_loop.py").read_text())
    serve = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_inherent"
    )
    references = [
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Name) and node.id == "_commentary_watcher"
    ]
    assert len(references) == 1
    guards = [
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "runtime.response_flags.lifecycle_commentary"
    ]
    assert len(guards) == 1
    assert "_commentary_watcher" in "\n".join(ast.unparse(stmt) for stmt in guards[0].body)
