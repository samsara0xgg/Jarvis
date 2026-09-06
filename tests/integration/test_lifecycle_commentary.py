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
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis import runtime as runtime_module
from jarvis.decision.commentary import (
    _D6_ROWS,
    COMMENTARY_ATTENTION_CHANNEL,
    commentary_intent_for,
)
from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.packet import assemble_packet
from jarvis.decision.pre_route import pre_route
from jarvis.decision.response_run import (
    ResponseRunRegistry,
    legacy_full_text_policy,
    start_response_run,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import (
    JarvisRuntime,
    _wave4_response_activation,
    drive_turn,
    inherent_loop,
    make_barge_in_interrupt_callable,
    make_response_cancel_callable,
)
from jarvis.shared import Event
from jarvis.shared.realtime import (
    Wave1FeatureFlags,
    new_response_id,
    stable_response_group_id,
)
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.surface import voice_media
from jarvis.surface.cli import parse_response_channels
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
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


def test_four_d6_rows_map_to_their_declared_intent_and_variant_set() -> None:
    """ADR-0008 D6's action table, with the action id as subject.

    The phrase is no longer one literal per row, so the pin is membership in
    the row's declared set plus the intent type it carries. Which member a
    given id selects is pinned by
    ``test_six_turns_speak_variants_from_the_acknowledge_set``.
    """
    expected_types = {
        "action.dispatched": "acknowledge",
        "action.running": "progress",
        "action.result_observed": "progress",
        "action.failed": "error",
    }
    assert set(_D6_ROWS) == set(expected_types)
    for event_type, intent_type in expected_types.items():
        intent = commentary_intent_for(_action_event(event_type, action_id="ACT-7"))
        assert intent is not None, event_type
        assert intent.intent_type == intent_type
        assert intent.content_hint in _D6_ROWS[event_type][1], event_type
        assert intent.subject_ref == "ACT-7"
        assert intent.surface_hint == "speech"
        assert intent.freshness_required is True


def test_a_phrase_is_stable_across_processes_for_one_action_id() -> None:
    """sha256, not builtin `hash()`: the choice is the same on every run.

    Builtin `hash()` is seeded per process by ``PYTHONHASHSEED``, so the same
    action would say different things across daemon restarts and this literal
    could not exist. A change to the acknowledge set is expected to fail this
    line — update it deliberately.
    """
    intent = commentary_intent_for(_action_event("action.dispatched", action_id="ACT-7"))
    assert intent is not None
    assert intent.content_hint == "这就去办。"


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
    with pytest.raises(FrozenInstanceError):
        intent.content_hint = "别的话"  # type: ignore[misc]
    assert COMMENTARY_ATTENTION_CHANNEL == "voice_notify"


def test_no_jarvis_module_appends_a_presentation_intent() -> None:
    """The Contract-vs-Event note, enforced: no emit call site names the type."""
    naming = [
        path
        for path in (repo_root() / "jarvis").rglob("*.py")
        if "PresentationIntent" in path.read_text(encoding="utf-8")
    ]
    assert naming, "the type should exist somewhere"
    for path in naming:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if callee not in {"emit_event", "append_event_in_transaction"}:
                continue
            rendered = ast.unparse(node)
            assert "PresentationIntent" not in rendered, path


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


def _observer_config(*, commentary: bool, cancel: bool = False) -> dict[str, Any]:
    """Config for a runtime with the ResponseRun lifecycle and D6 commentary."""
    return {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
            },
            "response": {
                "response_run_lifecycle": True,
                "independent_response_cancel": cancel,
                "cancel_timeout_ms": 500,
            },
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


def _make_runtime(
    tmp_path: Path,
    *,
    commentary: bool = True,
    cancel: bool = False,
) -> JarvisRuntime:
    """Assemble the runtime the daemon's watchers receive."""
    paths = bootstrap_runtime(tmp_path)
    config = _observer_config(commentary=commentary, cancel=cancel)
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
        response_runs=ResponseRunRegistry() if flags.independent_response_cancel else None,
        committed_event_bus=CommittedEventBus(),
    )


def _user_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    *,
    transcript: str = "现在几点",
) -> Event:
    """Emit the user-intent trigger and the turn claim it originates."""
    intent = emit_event(
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


def _spoken(conn: sqlite3.Connection) -> list[str]:
    """Return the voice text of every commentary response, tags stripped."""
    return [
        parse_response_channels(payload["text"]).voice
        for payload in _typed_payloads(conn, "surface.response_emitted")
        if payload.get("phase") == "commentary"
    ]


def _only_phrase(conn: sqlite3.Connection) -> str:
    """The single commentary phrase this log holds, asserting there is one."""
    spoken = _spoken(conn)
    assert len(spoken) == 1, spoken
    return spoken[0]


def opens_chunks_emitted(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every `surface.response_*` payload in append order."""
    return [
        payload
        for event_type in ("surface.response_open", "surface.response_chunk",
                           "surface.response_emitted")
        for payload in _typed_payloads(conn, event_type)
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

    assert _count(reader, "surface.response_open") == 1
    assert _count(reader, "surface.response_emitted") == 1
    for payload in opens_chunks_emitted(reader):
        assert payload["phase"] == "commentary"
        assert payload["response_id"] == started[0]["response_id"]
        assert payload["response_group_id"] == started[0]["response_group_id"]
    assert _spoken(reader) == [
        commentary_intent_for(dispatched).content_hint,  # type: ignore[union-attr]
    ]
    assert _spoken(reader)[0] in _D6_ROWS["action.dispatched"][1]
    assert _typed_payloads(reader, "surface.response_open")[0]["attention_channel"] == (
        COMMENTARY_ATTENTION_CHANNEL
    )
    # The run says speech; L5 must derive the same channel from the text, or
    # `ConversationHistory` folds the disagreement into `consistent=False`.
    assert {payload["channel"] for payload in opens_chunks_emitted(reader)} == {"speech"}
    assert _typed_payloads(reader, "surface.response_emitted")[0]["document_text"] == ""
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
        # Positive control: the same observer, alive, does speak for a claimed
        # turn — the silence above is the filter, not a stalled watcher.
        _user_turn(runtime.conn, "T-user")
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-user", turn_id="T-user")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    assert _typed_payloads(reader, "response.started")[0]["active_subject_ref"] == "ACT-user"


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
        # Positive control, as above.
        _user_turn(runtime.conn, "T-user")
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-user", turn_id="T-user")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    assert _typed_payloads(reader, "response.started")[0]["active_subject_ref"] == "ACT-user"


# --- observer: confirmation guard ------------------------------------------


def test_live_pending_confirmation_silences_commentary(tmp_path: Path) -> None:
    """An unresolved ask owns the surface; commentary waits for its answer.

    Also the pin for "a suppressed row does not consume the turn's one slot":
    the dispatched row is silenced here, and the turn still speaks on the
    later `action.running`. A cap recorded on suppression would mute the turn
    entirely, which is a worse defect than the one it replaces.
    """
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
    assert _spoken(reader) == [_only_phrase(reader)]
    assert _only_phrase(reader) in _D6_ROWS["action.running"][1]


# --- observer: one phrase per turn -----------------------------------------


def _commentary_emitted(conn: sqlite3.Connection, turn_id: str) -> list[dict[str, Any]]:
    """Every commentary phrase this turn actually put on a surface.

    `surface.playback_started` is the live observable and is deliberately not
    counted here: the hermetic harness runs no TTS actor and never emits one,
    so a count over it would count only the rows a test wrote by hand and
    would pass whatever the observer did.
    """
    return [
        payload
        for payload in _typed_payloads(conn, "surface.response_emitted")
        if payload.get("phase") == "commentary" and payload.get("turn_id") == turn_id
    ]


def _wait_until_turn_spoke(conn: sqlite3.Connection, turn_id: str) -> None:
    """Block until this turn has emitted its one commentary phrase."""
    _wait_until(lambda: len(_commentary_emitted(conn, turn_id)) == 1)


def _cancel_reasons(conn: sqlite3.Connection) -> list[str]:
    """The reason of every cancelled response, in append order."""
    return [str(payload["reason"]) for payload in _typed_payloads(conn, "response.cancelled")]


def test_one_turn_with_three_actions_speaks_exactly_one_commentary(
    tmp_path: Path,
) -> None:
    """The owner's measured bug shape, capped: nine lifecycle rows, one phrase.

    `Tceebc265` dispatched three actions, each emitting `action.dispatched` /
    `action.running` / `action.result_observed`; seven commentary responses
    were opened and four reached the speaker, three of them the identical
    sentence. Supersession is keyed per action, so it could never see across
    the three. The count is the whole assertion — not the phrase text — so a
    regression reports the number it produced and nothing else.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-cap")
    with _Observer(runtime):
        for action_id in ("ACT-c1", "ACT-c2", "ACT-c3"):
            dispatched = _action_row(
                runtime.conn, "action.dispatched", action_id=action_id, turn_id="T-cap",
            )
            _action_row(
                runtime.conn, "action.running", action_id=action_id, turn_id="T-cap",
                source_event_id=dispatched.event_uid,
            )
            _action_row(
                runtime.conn, "action.result_observed", action_id=action_id,
                turn_id="T-cap", source_event_id=dispatched.event_uid,
            )
        _wait_until(lambda: len(_commentary_emitted(reader, "T-cap")) >= 1)
        _settle()

    assert len(_commentary_emitted(reader, "T-cap")) == 1


def test_six_turns_speak_variants_from_the_acknowledge_set(tmp_path: Path) -> None:
    """Six turns, six action ids: every phrase is declared, and they differ.

    Under the cap the acknowledge is the phrase actually heard, so one fixed
    acknowledge would be the same sentence on every single turn. Selection is
    a sha256 digest of the action id, so this is not a coin flip — it passes
    always or fails always, on every process and platform. The six ids were
    chosen for that reason: `ACT-v1..ACT-v6` land on three of the three
    declared variants, and a change to the set is expected to move them.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    action_ids = [f"ACT-v{index}" for index in range(1, 7)]
    with _Observer(runtime):
        for index, action_id in enumerate(action_ids):
            turn_id = f"T-v{index}"
            _user_turn(runtime.conn, turn_id)
            _action_row(
                runtime.conn, "action.dispatched", action_id=action_id, turn_id=turn_id,
            )
            _wait_until_turn_spoke(reader, turn_id)

    spoken = _spoken(reader)
    assert len(spoken) == len(action_ids)
    variants = _D6_ROWS["action.dispatched"][1]
    assert all(phrase in variants for phrase in spoken), spoken
    assert len(set(spoken)) >= 2, spoken


def test_a_second_row_in_the_same_turn_opens_nothing(tmp_path: Path) -> None:
    """The cap wins before supersession, so no phrase is ever cut off for a newer one.

    Per-action supersession is retained as the safety net if the cap is ever
    loosened, but an action belongs to exactly one turn, so under the cap it
    is unreachable from the watcher: no `superseded` row can appear.
    """
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
        _settle()

    assert len(_typed_payloads(reader, "response.started")) == 1
    assert len(_commentary_emitted(reader, "T-coal")) == 1
    assert _cancel_reasons(reader) == ["shutdown"]


def test_a_commentary_that_reached_the_speaker_finishes(tmp_path: Path) -> None:
    """Playback closes the run through `complete`; the turn then says nothing more."""
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
        _settle()
        # Positive control: the observer is alive and the silence above is
        # the cap, not a stalled watcher.
        _user_turn(runtime.conn, "T-heard-2")
        _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-h2", turn_id="T-heard-2",
        )
        _wait_until(lambda: len(_commentary_emitted(reader, "T-heard-2")) == 1)

    assert len(_commentary_emitted(reader, "T-heard")) == 1
    completed = _typed_payloads(reader, "response.completed")
    assert [payload["response_id"] for payload in completed] == [first["response_id"]]
    # Only the control turn's still-open phrase is released, at teardown.
    assert _cancel_reasons(reader) == ["shutdown"]
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
    assert {
        payload["phase"] for payload in _typed_payloads(reader, "surface.response_chunk")
    } == {"commentary", "final"}
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
    drop_response_hashes: frozenset[str] = frozenset(),
) -> list[tuple[str, dict[str, Any]]]:
    """Return `(type, payload)` for the whole log, in append order."""
    shape: list[tuple[str, dict[str, Any]]] = []
    for event_type, raw in conn.execute(
        "SELECT type, payload_json FROM events ORDER BY id ASC",
    ):
        payload = json.loads(raw)
        if str(payload.get("response_id", "")) in drop_response_ids:
            continue
        if str(payload.get("response_hash", "")) in drop_response_hashes:
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


def _commentary_response_hashes(conn: sqlite3.Connection) -> frozenset[str]:
    """The plan hashes of those runs; the only way to name their gate rows.

    `gate.evaluated(pre_emit)` carries no `response_id`, so the commentary's
    own verdict row has to be identified by the hash it approved.
    """
    return frozenset(
        str(payload["response_hash"])
        for payload in _typed_payloads(conn, "surface.response_emitted")
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
        drop_response_hashes=_commentary_response_hashes(on_reader),
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


def test_a_terminal_row_with_no_correlation_finds_its_turn_through_dispatch(
    tmp_path: Path,
) -> None:
    """The runner's inline terminals carry no correlation; the chain still holds.

    Observed live: `terminalize_action` commits `action.result_observed` with
    an empty correlation on the synchronous path, so the turn has to come from
    the action's own `action.dispatched` row.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-join")
    # Emitted before the observer takes its boot anchor, so the only row that
    # reaches the watcher is the correlation-less terminal below and the turn
    # has to be recovered through this row.
    dispatched = _action_row(
        runtime.conn, "action.dispatched", action_id="ACT-j", turn_id="T-join",
    )
    with _Observer(runtime):
        emit_event(
            runtime.conn,
            type="action.result_observed",
            payload={"action_id": "ACT-j", "semantics": "observation"},
            source_event_id=dispatched.event_uid,
            correlation=None,
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)

    started = _typed_payloads(reader, "response.started")
    assert [payload["turn_id"] for payload in started] == ["T-join"]
    assert _only_phrase(reader) in _D6_ROWS["action.result_observed"][1]


# --- the per-turn assumption the card asked the lane to prove ---------------


def test_two_presentation_records_under_one_turn_stay_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commentary plus its final fold into one consistent ConversationTurn.

    The card's disposition table names this: `MAX_RESPONSES_PER_TURN = 8` and
    the fold is keyed by `response_id`, so two records are admissible. What is
    NOT free is agreement — `_bind_identity` marks a record inconsistent if a
    response's `channel` changes between its `response.started` and its
    surface rows, one inconsistent record makes the whole history
    inconsistent, and `pre_route` then answers `unknown` for every later turn
    in the window, silently switching routine streaming off. That is exactly
    what an untagged commentary phrase used to do.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    intent = _user_turn(runtime.conn, "T-fold")
    _script_final(monkeypatch)
    with _Observer(runtime):
        _action_row(runtime.conn, "action.dispatched", action_id="ACT-f", turn_id="T-fold")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        _drive_final(runtime, intent)
        _settle()

    history = fold_conversation_history(iter_events(reader))
    assert history.consistent is True
    assert len(history.turns) == 1
    records = history.turns[0].responses
    assert [record.phase for record in records] == ["commentary", "final"]
    assert [record.channel for record in records] == ["speech", "both"]
    assert len({record.response_id for record in records}) == 2
    assert all(record.consistent for record in records)


def test_commentary_leaves_the_next_turns_pre_route_alone(tmp_path: Path) -> None:
    """The regression that motivates the `<voice>` tag, pinned end to end."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-route")
    with _Observer(runtime):
        dispatched = _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-rt", turn_id="T-route",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    # Close the action so the status board is quiet; the history is then the
    # only thing that could still force `unknown`.
    _action_row(
        runtime.conn, "action.result_observed", action_id="ACT-rt", turn_id="T-route",
        source_event_id=dispatched.event_uid,
    )
    later = _user_turn(reader, "T-next", transcript="随便说点什么吧")
    packet = assemble_packet(later, reader, entity_bookmarks=())
    assert fold_conversation_history(iter_events(reader)).consistent is True
    assert pre_route(
        packet,
        tier0_table=runtime.tier0_table,
        tool_cues=runtime.tool_cues,
        now_ms=int(time.time() * 1000),
    ) == "casual_or_explanatory"


def test_a_playing_commentary_is_completed_not_cut_off(tmp_path: Path) -> None:
    """D6: a commentary already playing finishes; only an unheard one is cut.

    Deterministic reconstruction of a race the live run hit. The action row
    that supersedes a phrase is written *before* that phrase's playback
    begins, so it has the lower row id — a retirement that judged "already
    playing" from the observer's own cursor position would always decide too
    early, and cut off speech that was coming out of the speaker.

    Under the per-turn cap the watcher no longer reaches that retirement at
    all: the second call below returns `None` because the turn already spoke.
    The safety net is kept for the day the cap is loosened, so it is driven
    here through its own entry point rather than claimed to be exercised.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-cut")
    dispatched = _action_row(
        runtime.conn, "action.dispatched", action_id="ACT-cut", turn_id="T-cut",
    )
    first = inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, action_event=dispatched, previous=None,
    )
    assert first is not None
    emit_event(
        runtime.conn,
        type="surface.playback_started",
        payload={
            "session_id": "SESS-cut",
            "response_id": first.run.response_id,
            "turn_id": "T-cut",
            "playback_generation_id": 1,
            "phase": "commentary",
            "channel": "speech",
            "speech_text_hash": "deadbeef",
        },
        correlation={"turn_id": "T-cut"},
    )
    running = _action_row(
        runtime.conn, "action.running", action_id="ACT-cut", turn_id="T-cut",
        source_event_id=dispatched.event_uid,
    )
    second = inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, action_event=running, previous=first,
    )
    assert second is None
    assert len(_commentary_emitted(reader, "T-cut")) == 1

    # The retained safety net, driven directly: a phrase that reached the
    # speaker is completed, never cancelled.
    inherent_loop._retire_superseded_commentary(runtime, reader, first)  # noqa: SLF001

    assert _typed_payloads(reader, "response.cancelled") == []
    completed = [
        payload["response_id"] for payload in _typed_payloads(reader, "response.completed")
    ]
    assert completed == [first.run.response_id]


# --- observer: the operator cancel seam reaches a commentary ----------------


def _cancel_over_http(
    runtime: JarvisRuntime,
    response_id: str,
    *,
    reason: str = "user_stop",
) -> dict[str, Any]:
    """POST ``/inherent/cancel-response`` over the real app and return the body."""
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T-http",
            broadcaster=InherentBroadcaster(),
            cancel_response_callable=make_response_cancel_callable(runtime),
        ),
    )
    with TestClient(app) as client:
        reply = client.post(
            "/inherent/cancel-response",
            json={"response_id": response_id, "scope": "generation", "reason": reason},
        )
    assert reply.status_code == 200
    body = reply.json()
    assert isinstance(body, dict)
    return body


def _open_commentary_id(runtime: JarvisRuntime, reader: sqlite3.Connection) -> str:
    """Wait for the observer's newest commentary run and return its id."""
    registry = runtime.response_runs
    assert registry is not None
    _wait_until(lambda: len(registry.open_runs()) >= 1)
    started = _typed_payloads(reader, "response.started")
    return str(started[-1]["response_id"])


def test_an_unheard_commentary_answers_cancel_response_and_writes_its_row(
    tmp_path: Path,
) -> None:
    """The operator seam can close a phrase that never reached the speaker.

    Before this change ``runtime.response_runs`` never held a commentary run,
    so this exact POST answered ``unknown_response`` and wrote nothing.
    """
    runtime = _make_runtime(tmp_path, cancel=True)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-opcancel")
    with _Observer(runtime):
        _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-op", turn_id="T-opcancel",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        response_id = _open_commentary_id(runtime, reader)

        assert _cancel_over_http(runtime, response_id) == {"outcome": "cancelled"}

    cancelled = _typed_payloads(reader, "response.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["response_id"] == response_id
    assert cancelled[0]["reason"] == "user_stop"
    assert cancelled[0]["cancel_scope"] == "generation"
    assert _count(reader, "response.completed") == 0


def test_barge_in_while_a_commentary_is_open_still_cancels_the_final_run(
    tmp_path: Path,
) -> None:
    """R1: a concurrently open commentary is not a barge-in target, nor noise.

    The final run of an action-dispatching turn is ``waiting_action`` — open —
    at the exact moment the commentary watcher opens its own run off the same
    action row.  Without the ``phase == "final"`` filter in ``_interrupt`` the
    registry holds two open runs and the barge-in returns
    ``ambiguous_open_runs``, cancelling nothing.
    """
    runtime = _make_runtime(tmp_path, cancel=True)
    reader = _reader(runtime)
    registry = runtime.response_runs
    assert registry is not None
    factory = runtime.llm_session_factory
    assert factory is not None

    _user_turn(runtime.conn, "T-comm")
    with _Observer(runtime):
        _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-bi", turn_id="T-comm",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        commentary_id = _open_commentary_id(runtime, reader)

        trigger = _user_turn(runtime.conn, "T-final", transcript="讲个长故事")
        final_id = new_response_id()
        final_run = start_response_run(
            runtime.conn,
            turn_id="T-final",
            trigger_event_uid=trigger.event_uid,
            request_client=factory.create(factory.snapshot(None), response_id=final_id),
            policy=legacy_full_text_policy(
                evidence_snapshot_hash="a" * 64,
                preset_snapshot_hash="b" * 64,
            ),
            response_id=final_id,
            committed_event_bus=runtime.committed_event_bus,
        )
        final_run.mark("waiting_action")
        registry.register(final_run)
        assert {run.phase for run in registry.open_runs()} == {"commentary", "final"}

        outcome = make_barge_in_interrupt_callable(runtime)("keyword")

        # The row is the observable; the outcome rides along as the failure
        # message so an unfiltered `open_runs()` reports itself.
        cancelled = _typed_payloads(reader, "response.cancelled")
        assert [payload["response_id"] for payload in cancelled] == [final_id], outcome
        assert outcome == "cancelled"
        assert cancelled[0]["reason"] == "barge_in"
        assert cancelled[0]["cancel_scope"] == "generation"
        assert all(payload["response_id"] != commentary_id for payload in cancelled)


def test_a_closed_commentary_is_no_longer_a_cancel_target(tmp_path: Path) -> None:
    """R4: both watcher release paths unregister, so a later POST finds nothing.

    ``already_terminal`` would mean the entry is still registered, so
    ``unknown_response`` is exactly the proof of unregistration.
    """
    runtime = _make_runtime(tmp_path, cancel=True)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-closed")
    with _Observer(runtime):
        # Close path 1: the phrase reached the speaker.
        _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-cl", turn_id="T-closed",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        heard_id = _open_commentary_id(runtime, reader)
        emit_event(
            runtime.conn,
            type="surface.playback_started",
            payload={
                "session_id": "SESS-cl",
                "response_id": heard_id,
                "turn_id": "T-closed",
                "playback_generation_id": 1,
                "phase": "commentary",
                "channel": "speech",
                "speech_text_hash": "deadbeef",
            },
            correlation={"turn_id": "T-closed"},
        )
        _wait_until(lambda: _count(reader, "response.completed") == 1)

        assert _cancel_over_http(runtime, heard_id) == {"outcome": "unknown_response"}

        # Close path 2: an unheard phrase is released at shutdown. Under the
        # per-turn cap the watcher never supersedes, so this is the reachable
        # `_cancel_unheard_commentary` caller, and it needs its own turn.
        _user_turn(runtime.conn, "T-closed-2")
        _action_row(
            runtime.conn, "action.dispatched", action_id="ACT-cl2", turn_id="T-closed-2",
        )
        _wait_until(lambda: len(_commentary_emitted(reader, "T-closed-2")) == 1)
        unheard_id = _open_commentary_id(runtime, reader)

    _wait_until(
        lambda: any(
            payload["reason"] == "shutdown"
            for payload in _typed_payloads(reader, "response.cancelled")
        ),
    )
    assert _cancel_over_http(runtime, unheard_id) == {"outcome": "unknown_response"}

    cancelled = [
        payload
        for payload in _typed_payloads(reader, "response.cancelled")
        if payload["reason"] == "shutdown"
    ]
    assert [payload["response_id"] for payload in cancelled] == [unheard_id]
