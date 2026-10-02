"""ADR-0008 D6 / ADR 0116 acceptance: the one wait line of a turn Allen spoke.

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
import operator
import secrets
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
    commentary_speech_text,
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
    tool_status,
)
from jarvis.shared import Event, lang
from jarvis.shared.lang import LONG_WAIT_TOOLS, SLOW_TOOLS
from jarvis.shared.realtime import (
    Wave1FeatureFlags,
    new_response_id,
    stable_response_group_id,
)
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state import projections
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


_WAIT_ZH: Final = lang.variants("commentary.wait", "zh")
_WAIT_EN: Final = lang.variants("commentary.wait", "en")
_LONG_ZH: Final = lang.variants("commentary.long_wait", "zh")
_LONG_EN: Final = lang.variants("commentary.long_wait", "en")
_STILL_ZH: Final = lang.variants("commentary.still", "zh")
_STILL_EN: Final = lang.variants("commentary.still", "en")


@pytest.fixture(autouse=True)
def _no_acknowledge_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows here follow their turn at once; the 1.5 s floor has its own test.

    The clocks are parked far away so a turn only speaks for a dispatch;
    the clock's own tests bring it in.
    """
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_EARLIEST_S", 0.0)
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", 600.0)
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_STILL_AFTER_S", (1200.0, 1800.0))


def _first(phrases: tuple[str, ...]) -> str:
    return phrases[0]


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


def _utterance_event(*, text: str = "现在几点") -> Event:
    """One committed-shaped row carrying his words, without touching the log."""
    return Event(
        event_uid="uid-utterance-1",
        type="utterance.received",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"transcript": text, "turn_id": "T-1"},
        source_event_id=None,
        correlation={"turn_id": "T-1"},
    )


def test_two_rows_speak_a_long_wait_tools_dispatch_and_his_words() -> None:
    """A long-wait tool's dispatch (the action id is the subject) and his words (the row is).

    Which phrase the picker takes is the runtime's business; the pin is that
    the phrase comes from the row's declared pool and nothing else.
    """
    assert set(_D6_ROWS) == {"action.dispatched", "utterance.received"}
    intent = commentary_intent_for(
        _action_event("action.dispatched", action_id="ACT-7"),
        tool_name="refresh_work_state",
        pick=_first,
    )
    assert intent is not None
    assert intent.intent_type == "acknowledge"
    assert intent.content_hint == _LONG_ZH[0]
    assert intent.subject_ref == "ACT-7"
    assert intent.surface_hint == "speech"
    assert intent.freshness_required is True

    spoken = _utterance_event()
    by_words = commentary_intent_for(spoken, user_text="现在几点", pick=_first)
    assert by_words is not None
    assert by_words.subject_ref == spoken.event_uid
    assert by_words.content_hint == _WAIT_ZH[0]


def test_the_slow_list_feeds_the_status_line_and_the_long_wait_tools_the_dispatch() -> None:
    """ADR 0115's five tools show a status line; of them only the two slowest speak (ADR 0121)."""
    assert set(SLOW_TOOLS) == {
        "web_search", "web_fetch", "screen_look", "refresh_work_state", "daily_work_report",
    }
    for tool_name in SLOW_TOOLS:
        assert tool_status.plan(tool_name) == (SLOW_TOOLS[tool_name], 0.0)
        intent = commentary_intent_for(
            _action_event("action.dispatched"), tool_name=tool_name, pick=_first,
        )
        assert (intent is not None) == (tool_name in LONG_WAIT_TOOLS), tool_name


@pytest.mark.parametrize(
    "tool_name",
    [
        "web_search", "web_fetch", "screen_look", "tool_search", "get_current_time",
        "calendar_list", "remember", "ask_user", "write_file", "spawn_worker", None,
    ],
)
def test_a_tool_off_the_long_wait_list_says_nothing_at_dispatch(tool_name: str | None) -> None:
    """A wait of about 2 s needs no voice: the text under the orb is enough (ADR 0121).

    Quick tools, the clock, a kept fact or a card are covered by the 4.0 s clock:
    nothing is said unless the turn is still silent then.
    """
    event = _action_event("action.dispatched")
    assert commentary_intent_for(event, tool_name=tool_name, pick=_first) is None


@pytest.mark.parametrize(("text", "pool"), [("现在几点", _WAIT_ZH), ("What time is it", _WAIT_EN)])
def test_the_pool_follows_the_language_of_his_words(text: str, pool: tuple[str, ...]) -> None:
    """Every phrase of the language is reachable and none of the other's is."""
    seen = set()
    for index in range(len(pool)):
        intent = commentary_intent_for(
            _utterance_event(text=text), user_text=text, pick=operator.itemgetter(index),
        )
        assert intent is not None
        seen.add(intent.content_hint)
    assert seen == set(pool)


def test_the_owners_pool_is_what_the_table_holds() -> None:
    """2026-10-02, Allen: these four in each language, 「马上好」 included."""
    assert _WAIT_ZH == ("稍等。", "等一下。", "正在办。", "马上好。")
    assert _WAIT_EN == ("One moment.", "Hold on.", "On it.", "Almost there.")


def test_the_long_wait_pool_and_its_tools_are_what_the_owner_chose() -> None:
    """2026-10-02, Allen: the two slowest jobs say it will take a while."""
    assert len(_LONG_ZH) == 3
    assert all("别的" in line for line in _LONG_ZH)
    assert _LONG_EN == (
        "This will take a little while. You can ask me something else meanwhile.",
        "This one takes a bit. Feel free to ask me something else.",
    )
    assert set(LONG_WAIT_TOOLS) == {"daily_work_report", "refresh_work_state"}
    assert set(SLOW_TOOLS) >= LONG_WAIT_TOOLS


@pytest.mark.parametrize(("text", "pool"), [("还没好", _STILL_ZH), ("Is it ready", _STILL_EN)])
def test_a_follow_up_says_the_still_pool_in_the_language_of_his_words(
    text: str, pool: tuple[str, ...],
) -> None:
    """The follow-up row is the utterance row with ``still``: every phrase, none of the other's."""
    seen = {
        commentary_intent_for(
            _utterance_event(text=text), user_text=text, still=True, pick=operator.itemgetter(i),
        ).content_hint  # type: ignore[union-attr]
        for i in range(len(pool))
    }
    assert seen == set(pool)
    assert len(_STILL_ZH) == 3
    assert _STILL_EN == ("Still working on it.", "Still on it, one more moment.")


def test_non_mapped_event_types_return_none() -> None:
    """Only the two rows speak: a tool's progress, result or failure is silent.

    ADR 0045: the result row's 「结果回来了」 followed a quick tool with nothing
    before it.
    """
    for event_type in ("action.running", "action.result_observed", "action.failed",
                       "memo.captured", "gate.evaluated", "action.cancelled",
                       "action.timeout_assumed", "turn.started", "response.completed"):
        event = _action_event(event_type)
        assert commentary_intent_for(event, tool_name="web_search", pick=_first) is None, event_type


def test_mapped_row_without_action_id_returns_none() -> None:
    """``subject_ref`` is the action id; without one there is nothing to say."""
    event = Event(
        event_uid="uid-no-action",
        type="action.dispatched",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"tool_name": "web_search"},
        source_event_id=None,
        correlation=None,
    )
    assert commentary_intent_for(event, tool_name="web_search", pick=_first) is None


def test_intent_is_frozen_and_never_an_event() -> None:
    """Spec §3.6.3: PresentationIntent is a contract object, not a log row."""
    intent = commentary_intent_for(
        _action_event("action.dispatched"), tool_name="daily_work_report", pick=_first,
    )
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


def test_shipped_config_turns_commentary_on() -> None:
    """ADR 0116: the shipped switch is on, so a slow turn says its wait line."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    assert shipped["realtime"]["commentary"] == {"enabled": True}
    assert _wave4_response_activation(shipped).flags.lifecycle_commentary is True


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
    typed: str | None = None,
) -> Event:
    """Emit the trigger and the turn claim it originates.

    By default Allen spoke (``utterance.received``); ``typed`` names the
    channel of a ``surface.user_intent`` instead (the keyboard, or GPT-Live).
    """
    intent = emit_event(
        conn,
        type="surface.user_intent" if typed else "utterance.received",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": typed or "inherent_wake",
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


def _dispatch(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    turn_id: str,
    tool_name: str = "refresh_work_state",
    lead_in: str | None = None,
) -> Event:
    """Emit one tool call's proposal and its dispatch; the dispatch is returned."""
    emit_event(
        conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": tool_name,
            "turn_id": turn_id,
            "caller_principal": "jarvis_llm",
            "risk_level": "L1",
            **({"lead_in": lead_in} if lead_in else {}),
        },
        correlation={"action_id": action_id, "turn_id": turn_id},
    )
    return _action_row(conn, "action.dispatched", action_id=action_id, turn_id=turn_id)


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
        dispatched = _dispatch(runtime.conn, action_id="ACT-ack", turn_id="T-ack",
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
    assert len(_spoken(reader)) == 1
    assert _spoken(reader)[0] in _LONG_ZH
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
        _dispatch(runtime.conn, action_id="ACT-sys", turn_id="T-sys")
        _action_row(runtime.conn, "action.running", action_id="ACT-sys", turn_id="T-sys")
        _settle()
        assert _count(reader, "response.started") == 0
        assert _count(reader, "surface.response_open") == 0
        # Positive control: the same observer, alive, does speak for a claimed
        # turn — the silence above is the filter, not a stalled watcher.
        _user_turn(runtime.conn, "T-user")
        _dispatch(runtime.conn, action_id="ACT-user", turn_id="T-user")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    assert _typed_payloads(reader, "response.started")[0]["active_subject_ref"] == "ACT-user"


def test_a_turn_whose_answer_is_out_stays_silent(tmp_path: Path) -> None:
    """2026-09-24: a Tier 0 time question said the time, then "result's back, let me look".

    Tier 0 ends the turn before its tool's rows reach the watcher, so a
    phrase opened now would follow the answer it announces.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-t0")
        emit_event(
            runtime.conn,
            type="turn.ended",
            payload={
                "turn_id": "T-t0",
                "final_response_hash": "0" * 64,
                "consumed_trigger_event_uid": "uid-t0",
            },
            correlation={"turn_id": "T-t0"},
        )
        _dispatch(runtime.conn, action_id="ACT-t0", turn_id="T-t0")
        _settle()
        assert _count(reader, "response.started") == 0
        # Positive control: a turn still at work speaks.
        _user_turn(runtime.conn, "T-live")
        _dispatch(runtime.conn, action_id="ACT-live", turn_id="T-live")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)


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
        _dispatch(runtime.conn, action_id="ACT-r", turn_id="T-recon")
        _settle()
        assert _count(reader, "response.started") == 0
        # Positive control, as above.
        _user_turn(runtime.conn, "T-user")
        _dispatch(runtime.conn, action_id="ACT-user", turn_id="T-user")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    assert _typed_payloads(reader, "response.started")[0]["active_subject_ref"] == "ACT-user"


# --- observer: confirmation guard ------------------------------------------


def test_live_pending_confirmation_silences_commentary(tmp_path: Path) -> None:
    """An unresolved ask owns the surface; commentary waits for its answer.

    Also the pin for "a suppressed row does not consume the turn's one slot":
    the first dispatch is silenced here, and the turn still speaks on its next
    one. A cap recorded on suppression would mute the turn entirely, which is
    a worse defect than the one it replaces.
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
        _dispatch(runtime.conn, action_id="ACT-c", turn_id="T-conf")
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
        _dispatch(runtime.conn, action_id="ACT-c2", turn_id="T-conf")
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    # `_only_phrase` asserts there is exactly one; the second dispatch spoke and
    # the silenced first one did not take the turn's slot with it.
    assert _only_phrase(reader) in _LONG_ZH
    assert _typed_payloads(reader, "response.started")[0]["active_subject_ref"] == "ACT-c2"


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
            dispatched = _dispatch(runtime.conn, action_id=action_id, turn_id="T-cap",
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


def test_every_phrase_of_the_pool_can_be_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four turns, a picker that walks the pool: each phrase is one turn's line.

    The runtime picks with ``secrets.choice``, so the walk replaces it for the
    test only; a phrase outside the pool, or one the runtime never offers,
    shows up here. The line is the clock's, one turn at a time so the walk is
    in order.
    """
    walk = iter(range(len(_WAIT_ZH)))
    monkeypatch.setattr(secrets, "choice", lambda phrases: phrases[next(walk)])
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        for index in range(len(_WAIT_ZH)):
            turn_id = f"T-v{index}"
            _user_turn(runtime.conn, turn_id)
            _wait_until_turn_spoke(reader, turn_id)

    assert _spoken(reader) == list(_WAIT_ZH)


@pytest.mark.parametrize(
    ("transcript", "tool_name", "pool"),
    [
        ("Write today's report", "daily_work_report", _LONG_EN),
        ("把今天的工作报告写一下", "daily_work_report", _LONG_ZH),
        ("Refresh my work state", "refresh_work_state", _LONG_EN),
        ("刷新一下工作状态", "refresh_work_state", _LONG_ZH),
    ],
)
def test_a_long_wait_tool_says_one_long_line_in_the_language_allen_used(
    tmp_path: Path, transcript: str, tool_name: str, pool: tuple[str, ...],
) -> None:
    """2026-09-24 live: an English web search heard "这就去办。"; the long pool follows it too."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-lang", transcript=transcript)
        _dispatch(runtime.conn, action_id="ACT-lang", turn_id="T-lang", tool_name=tool_name)
        _wait_until_turn_spoke(reader, "T-lang")
        _settle()

    assert _only_phrase(reader) in pool


def test_a_spoken_turn_hears_the_models_own_line_unless_it_claims_a_result(
    tmp_path: Path,
) -> None:
    """docs/plans/speak-as-written-proposal.md: the model's line replaces the phrase.

    A line written with a bookkeeping call speaks at the next long-wait dispatch
    (the 2026-09-30 live run heard it 5 s later, at the first working one); one that
    already states a result, or runs past one sentence, gives way to the fixed
    phrase.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    line = "我查一下明天维多利亚的天气。"
    cases: list[tuple[str, list[tuple[str, str | None]], str | None]] = [
        ("T-line", [("refresh_work_state", line)], line),
        ("T-early", [("tool_search", line), ("refresh_work_state", None)], line),
        ("T-claim", [("refresh_work_state", "已经查到了。")], None),
        ("T-long", [("refresh_work_state", "我查一下" + "明天维多利亚的天气" * 7)], None),
    ]
    with _Observer(runtime):
        for turn_id, calls, _ in cases:
            _user_turn(runtime.conn, turn_id, transcript="明天维多利亚天气怎么样")
            for index, (tool_name, lead_in) in enumerate(calls):
                _dispatch(
                    runtime.conn,
                    action_id=f"ACT-{turn_id}-{index}",
                    turn_id=turn_id,
                    tool_name=tool_name,
                    lead_in=lead_in,
                )
            _wait_until_turn_spoke(reader, turn_id)

    for turn_id, _, spoken in cases:
        (payload,) = _commentary_emitted(reader, turn_id)
        allowed = [spoken] if spoken else _LONG_ZH
        assert parse_response_channels(payload["text"]).voice in allowed, turn_id
    subjects = {
        started["response_group_id"]: started["active_subject_ref"]
        for started in _typed_payloads(reader, "response.started")
    }
    # The first call is not a long-wait tool: its line speaks at the second.
    assert subjects[stable_response_group_id("T-early")] == "ACT-T-early-1"


def test_a_second_row_in_the_same_turn_opens_nothing(tmp_path: Path) -> None:
    """The cap: a later dispatch in the turn neither speaks nor cuts the first off."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-coal")
    with _Observer(runtime):
        dispatched = _dispatch(runtime.conn, action_id="ACT-c", turn_id="T-coal",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
        _dispatch(runtime.conn, action_id="ACT-c2", turn_id="T-coal")
        _settle()

    assert [p["active_subject_ref"] for p in _typed_payloads(reader, "response.started")] == [
        dispatched.payload["action_id"],
    ]
    assert len(_commentary_emitted(reader, "T-coal")) == 1
    assert _cancel_reasons(reader) == ["shutdown"]


def test_a_commentary_that_reached_the_speaker_finishes(tmp_path: Path) -> None:
    """Playback closes the run through `complete`; the turn then says nothing more."""
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-heard")
    with _Observer(runtime):
        _dispatch(runtime.conn, action_id="ACT-h", turn_id="T-heard")
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
        _dispatch(runtime.conn, action_id="ACT-h1", turn_id="T-heard")
        _settle()
        # Positive control: the observer is alive and the silence above is
        # the cap, not a stalled watcher.
        _user_turn(runtime.conn, "T-heard-2")
        _dispatch(runtime.conn, action_id="ACT-h2", turn_id="T-heard-2",
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


def test_the_acknowledge_outlives_its_tool_until_the_answer_is_out(tmp_path: Path) -> None:
    """The turn is still at work after a quick tool: its answer comes seconds later.

    2026-09-26 to 09-29: tools returned within 0.5 s, and from a turn's last
    tool result to its audio took 3.9 s median.
    """
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    _user_turn(runtime.conn, "T-late")
    dispatched = _dispatch(runtime.conn, action_id="ACT-l", turn_id="T-late",
    )
    _action_row(
        runtime.conn, "action.result_observed", action_id="ACT-l", turn_id="T-late",
        source_event_id=dispatched.event_uid,
    )
    opened = inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime,
        trigger_event=dispatched,
    )
    assert opened is not None
    assert _count(reader, "response.started") == 1
    inherent_loop._cancel_unheard_commentary(runtime, opened, reason="shutdown")  # noqa: SLF001


def test_an_answer_out_within_the_floor_hears_no_acknowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A light switched in 0.4 s needs no 「这就去办」 before its 「好了」.

    A turn still at work when the floor passes speaks, and not before it.
    """
    floor_s = 0.5
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_EARLIEST_S", floor_s)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-quick")
        _dispatch(runtime.conn, action_id="ACT-q", turn_id="T-quick")
        time.sleep(floor_s / 3)
        emit_event(
            runtime.conn,
            type="turn.ended",
            payload={
                "turn_id": "T-quick",
                "final_response_hash": "0" * 64,
                "consumed_trigger_event_uid": "uid-quick",
            },
            correlation={"turn_id": "T-quick"},
        )
        _settle(floor_s + 0.2)
        assert _count(reader, "response.started") == 0
        asked = _user_turn(runtime.conn, "T-slow")
        _dispatch(runtime.conn, action_id="ACT-s", turn_id="T-slow")
        _wait_until(lambda: _count(reader, "response.started") == 1)
    (spoke_at,) = reader.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = 'response.started'",
    ).fetchone()
    assert spoke_at - asked.ts_epoch_ms >= floor_s * 1000


# --- observer: the clocks and the answer-started rule (ADR 0116, ADR 0121) --

_CLOCK_S: Final = 0.4
"""The clock the tests run at, in place of the owner's 4.0 s."""


def _answer_started(conn: sqlite3.Connection, turn_id: str, *, phase: str = "final") -> None:
    """The row a spoken_streaming turn commits with its first answer segment."""
    emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": turn_id,
            "query": "",
            "kind": "stream",
            "required_gate_mode": "sentence",
            "attention_channel": "voice_notify",
            "response_id": f"R-{turn_id}-{phase}",
            "response_group_id": stable_response_group_id(turn_id),
            "phase": phase,
            "channel": "both",
        },
        correlation={"turn_id": turn_id},
    )


def _end_turn(conn: sqlite3.Connection, turn_id: str) -> None:
    emit_event(
        conn,
        type="turn.ended",
        payload={
            "turn_id": turn_id,
            "final_response_hash": "0" * 64,
            "consumed_trigger_event_uid": f"uid-{turn_id}",
        },
        correlation={"turn_id": turn_id},
    )


@pytest.mark.parametrize(
    ("transcript", "pool"),
    [("明天天气怎么样", _WAIT_ZH), ("What's the weather tomorrow", _WAIT_EN)],
)
def test_a_turn_with_no_answer_at_the_clock_says_one_line_with_or_without_a_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transcript: str, pool: tuple[str, ...],
) -> None:
    """No tool at all, and a quick one: the line comes from the clock, once, in his language."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        spoke = _user_turn(runtime.conn, "T-bare", transcript=transcript)
        _user_turn(runtime.conn, "T-quick", transcript=transcript)
        _dispatch(
            runtime.conn, action_id="ACT-quick", turn_id="T-quick", tool_name="get_current_time",
        )
        _wait_until(lambda: len(_spoken(reader)) == 2)
        _settle()

    assert len(_commentary_emitted(reader, "T-bare")) == 1
    assert len(_commentary_emitted(reader, "T-quick")) == 1
    assert set(_spoken(reader)) <= set(pool)
    started = {p["turn_id"]: p for p in _typed_payloads(reader, "response.started")}
    assert started["T-bare"]["active_subject_ref"] == spoke.event_uid
    source = reader.execute(
        "SELECT source_event_id FROM events WHERE type = 'response.started' "
        "AND json_extract(payload_json, '$.turn_id') = 'T-bare'",
    ).fetchone()
    assert source[0] == spoke.event_uid
    (said_at,) = reader.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = 'response.started' "
        "AND json_extract(payload_json, '$.turn_id') = 'T-bare'",
    ).fetchone()
    assert said_at - spoke.ts_epoch_ms >= _CLOCK_S * 1000


def test_an_answer_that_starts_before_the_clock_hears_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spoken streaming: the first segment's open row is "the answer started".

    The turn has not ended and is still writing; a line now would talk over its
    first sentence. A commentary-phase open of the same turn is not an answer.
    """
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-streaming")
        _answer_started(runtime.conn, "T-streaming")
        _settle(_CLOCK_S + 0.3)
        assert _count(reader, "response.started") == 0
        # Positive controls: the clock is alive, and a commentary open is no answer.
        _user_turn(runtime.conn, "T-silent")
        _answer_started(runtime.conn, "T-silent", phase="commentary")
        _wait_until_turn_spoke(reader, "T-silent")

    assert _commentary_emitted(reader, "T-streaming") == []


def test_a_slow_tools_line_is_not_said_once_the_answer_has_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch trigger holds the same rule as the clock."""
    floor_s = 0.4
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_EARLIEST_S", floor_s)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-early-answer")
        _dispatch(runtime.conn, action_id="ACT-ea", turn_id="T-early-answer")
        _answer_started(runtime.conn, "T-early-answer")
        _settle(floor_s + 0.3)
        assert _count(reader, "response.started") == 0
        _user_turn(runtime.conn, "T-still-working")
        _dispatch(runtime.conn, action_id="ACT-sw", turn_id="T-still-working")
        _wait_until_turn_spoke(reader, "T-still-working")


@pytest.mark.parametrize("slow_first", [True, False])
def test_a_turn_never_hears_two_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, slow_first: bool,
) -> None:
    """The dispatch and the clock of one turn share its one slot, in either order."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-two")
        if slow_first:
            _dispatch(runtime.conn, action_id="ACT-two", turn_id="T-two")
            _wait_until_turn_spoke(reader, "T-two")
        else:
            _wait_until_turn_spoke(reader, "T-two")
            _dispatch(runtime.conn, action_id="ACT-two", turn_id="T-two")
        _settle(_CLOCK_S + 0.4)

    assert len(_commentary_emitted(reader, "T-two")) == 1


def test_a_long_tool_after_the_clock_line_adds_no_second_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0117: the clock spoke first, so the long line is not said; one line per turn."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-late-long")
        _wait_until_turn_spoke(reader, "T-late-long")
        _dispatch(
            runtime.conn, action_id="ACT-late", turn_id="T-late-long",
            tool_name="daily_work_report",
        )
        _settle(_CLOCK_S + 0.4)

    assert _only_phrase(reader) in _WAIT_ZH
    assert len(_commentary_emitted(reader, "T-late-long")) == 1


def test_a_typed_turn_and_a_live_turn_hear_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only words he spoke (`utterance.received`) earn a line, by either trigger."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        for turn_id, channel in (("T-typed", "cli_stdin"), ("T-live", "gpt_live")):
            _user_turn(runtime.conn, turn_id, typed=channel)
            _dispatch(runtime.conn, action_id=f"ACT-{turn_id}", turn_id=turn_id)
        _settle(_CLOCK_S + 0.3)
        assert _count(reader, "response.started") == 0
        # Positive control: the same observer speaks for a turn he spoke.
        _user_turn(runtime.conn, "T-voice")
        _wait_until_turn_spoke(reader, "T-voice")


def test_the_models_own_line_is_said_at_the_clock_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line written before a quick call speaks if the answer is still not out at the clock."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    line = "我看一下现在几点。"
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-lead")
        _dispatch(
            runtime.conn, action_id="ACT-lead", turn_id="T-lead",
            tool_name="get_current_time", lead_in=line,
        )
        _wait_until_turn_spoke(reader, "T-lead")

    assert _only_phrase(reader) == line


def test_a_confirmation_pending_at_the_clock_keeps_it_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask owns the surface (ADR-0014), whichever trigger would have spoken."""
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", _CLOCK_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    emit_event(
        runtime.conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": "CONF-clock",
            "action_snapshot": {"tool_name": "write_file", "risk_level": "high"},
            "template_line": "要写入吗",
            "expires_at_ms": int(time.time() * 1000) + 600_000,
        },
        correlation={"turn_id": "T-ask"},
    )
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-ask")
        _settle(_CLOCK_S + 0.3)
        assert _count(reader, "response.started") == 0


# --- observer: ADR 0121 -----------------------------------------------------

_SHIPPED_FOLLOW_UPS_S: Final = inherent_loop._COMMENTARY_STILL_AFTER_S  # noqa: SLF001
_SHIPPED_MAX_LINES: Final = inherent_loop._COMMENTARY_MAX_LINES  # noqa: SLF001
_SHIPPED_FLOOR_S: Final = inherent_loop._COMMENTARY_EARLIEST_S  # noqa: SLF001
_SHIPPED_CLOCK_S: Final = inherent_loop._COMMENTARY_AFTER_S  # noqa: SLF001
"""The owner's numbers as shipped; the autouse fixture parks them for the rest."""


def _lines(conn: sqlite3.Connection, turn_id: str) -> list[str]:
    """Every commentary phrase the turn put on a surface, in order, tags stripped."""
    return [
        parse_response_channels(payload["text"]).voice
        for payload in _commentary_emitted(conn, turn_id)
    ]


def _clocks(monkeypatch: pytest.MonkeyPatch, *, first: float, then: tuple[float, ...]) -> None:
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_AFTER_S", first)
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_STILL_AFTER_S", then)


def test_the_shipped_numbers_are_the_owners() -> None:
    """4.0 s clock, follow-ups at 12 s and 25 s, three lines at most, 1.5 s floor (ADR 0121)."""
    assert _SHIPPED_CLOCK_S == 4.0
    assert _SHIPPED_FOLLOW_UPS_S == (12.0, 25.0)
    assert _SHIPPED_MAX_LINES == 3
    assert _SHIPPED_FLOOR_S == 1.5


def _clock_at(runtime: JarvisRuntime, turn_id: str, *, left_s: float) -> Event:
    """His words, aged so that ``left_s`` of the shipped 4.0 s clock is still to run."""
    spoke = _user_turn(runtime.conn, turn_id)
    return replace(
        spoke, ts_epoch_ms=int(time.time() * 1000 - (_SHIPPED_CLOCK_S - left_s) * 1000),
    )


def test_the_shipped_clock_speaks_at_4_s_with_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.7 s after his words the line has not spoken yet; at 4.0 s it does."""
    _clocks(monkeypatch, first=_SHIPPED_CLOCK_S, then=_SHIPPED_FOLLOW_UPS_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    aged = _clock_at(runtime, "T-clock", left_s=0.3)
    started = time.monotonic()
    opened = inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, trigger_event=aged,
    )
    waited = time.monotonic() - started
    assert opened is not None
    assert waited >= 0.25
    assert _lines(reader, "T-clock")[0] in _WAIT_ZH
    inherent_loop._cancel_unheard_commentary(runtime, opened, reason="shutdown")  # noqa: SLF001


def test_the_shipped_clock_is_silent_when_the_answer_opened_at_3_6_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer that began before 4.0 s (the 3.6 s p75) takes the line's place."""
    _clocks(monkeypatch, first=_SHIPPED_CLOCK_S, then=_SHIPPED_FOLLOW_UPS_S)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    aged = _clock_at(runtime, "T-p75", left_s=0.3)
    _answer_started(runtime.conn, "T-p75")
    assert inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, trigger_event=aged,
    ) is None
    assert _count(reader, "response.started") == 0


@pytest.mark.parametrize("quick_tool", [None, "calendar_list", "web_search"])
def test_an_answer_within_the_clock_hears_no_line_with_or_without_a_quick_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick_tool: str | None,
) -> None:
    """A 2 s to 3.9 s answer needs no voice, and a quick tool dispatched on the way adds none."""
    _clocks(monkeypatch, first=_CLOCK_S, then=(_CLOCK_S * 3, _CLOCK_S * 6))
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_EARLIEST_S", 0.05)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-fast")
        if quick_tool is not None:
            _dispatch(runtime.conn, action_id="ACT-fast", turn_id="T-fast", tool_name=quick_tool)
        time.sleep(_CLOCK_S / 2)
        _answer_started(runtime.conn, "T-fast")
        _settle(_CLOCK_S * 6 + 0.3)
        assert _count(reader, "response.started") == 0


def test_a_turn_says_a_line_at_the_clock_and_a_follow_up_at_each_later_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First line from the wait pool, then two from the still pool, at their times."""
    _clocks(monkeypatch, first=0.3, then=(0.9, 1.5))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        spoke = _user_turn(runtime.conn, "T-slow")
        _wait_until(lambda: len(_lines(reader, "T-slow")) == _SHIPPED_MAX_LINES)
        _settle(0.6)
    lines = _lines(reader, "T-slow")
    assert len(lines) == _SHIPPED_MAX_LINES
    assert lines[0] in _WAIT_ZH
    assert all(line in _STILL_ZH for line in lines[1:])
    stamps = [
        at - spoke.ts_epoch_ms
        for (at,) in reader.execute(
            "SELECT ts_epoch_ms FROM events WHERE type = 'response.started' "
            "AND json_extract(payload_json, '$.phase') = 'commentary' ORDER BY id",
        )
    ]
    assert all(
        stamp >= bound * 1000 for stamp, bound in zip(stamps, (0.3, 0.9, 1.5), strict=True)
    )


def test_the_follow_ups_speak_english_to_english_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The still pool follows the language of his words like the first one."""
    _clocks(monkeypatch, first=0.2, then=(0.5, 0.8))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-en", transcript="What is on my calendar tomorrow")
        _wait_until(lambda: len(_lines(reader, "T-en")) == _SHIPPED_MAX_LINES)
    lines = _lines(reader, "T-en")
    assert lines[0] in _WAIT_EN
    assert all(line in _STILL_EN for line in lines[1:])


@pytest.mark.parametrize("answer_after_s", [0.1, 0.5, 1.1])
def test_follow_ups_stop_once_the_answer_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer_after_s: float,
) -> None:
    """Answer before the first line: none. After it: no follow-up. After the second: no third."""
    _clocks(monkeypatch, first=0.3, then=(0.9, 1.5))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-stop")
        time.sleep(answer_after_s)
        _answer_started(runtime.conn, "T-stop")
        _settle(1.8 - answer_after_s + 0.3)
    expected = sum(answer_after_s > clock for clock in (0.3, 0.9))
    assert len(_lines(reader, "T-stop")) == expected


def test_a_turn_that_ends_gets_no_more_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 0: the turn ends within a second of his words; no clock speaks for it."""
    _clocks(monkeypatch, first=0.3, then=(0.6, 0.9))
    monkeypatch.setattr(inherent_loop, "_COMMENTARY_EARLIEST_S", 0.5)
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-tier0")
        _dispatch(runtime.conn, action_id="ACT-t0", turn_id="T-tier0", tool_name="get_current_time")
        _dispatch(
            runtime.conn, action_id="ACT-t0b", turn_id="T-tier0", tool_name="refresh_work_state",
        )
        _end_turn(runtime.conn, "T-tier0")
        _settle(1.4)
        assert _count(reader, "response.started") == 0
        # Positive control: the same observer speaks for a turn that stays open.
        _user_turn(runtime.conn, "T-open")
        _wait_until_turn_spoke(reader, "T-open")


@pytest.mark.parametrize("reason", ["superseded", "barge_in", "user_stop"])
def test_a_turn_whose_answer_he_cancelled_gets_no_more_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    """Live 2026-10-01: interrupted or stopped turns kept saying 'still working'."""
    _clocks(monkeypatch, first=0.3, then=(0.9, 1.5))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-cut")
        _wait_until(lambda: len(_lines(reader, "T-cut")) == 1)
        emit_event(
            runtime.conn,
            type="response.cancelled",
            payload={
                "response_id": "RESP-cut", "response_group_id": "RGRP-cut",
                "turn_id": "T-cut", "reason": reason,
            },
            correlation={"turn_id": "T-cut"},
        )
        _settle(1.6)
    assert len(_lines(reader, "T-cut")) == 1


def test_a_long_wait_tool_speaks_at_dispatch_and_the_follow_ups_still_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch line is the first; the clock stays quiet; 12 s and 25 s follow it."""
    _clocks(monkeypatch, first=0.3, then=(0.6, 0.9))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-report")
        _dispatch(
            runtime.conn, action_id="ACT-r", turn_id="T-report", tool_name="daily_work_report",
        )
        _wait_until(lambda: len(_lines(reader, "T-report")) == _SHIPPED_MAX_LINES)
        _settle(0.6)
    lines = _lines(reader, "T-report")
    assert len(lines) == _SHIPPED_MAX_LINES
    assert lines[0] in _LONG_ZH
    assert all(line in _STILL_ZH for line in lines[1:])


def test_a_quick_tool_dispatch_says_nothing_before_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calendar read or a web search is not a long wait: only the clock speaks for it."""
    _clocks(monkeypatch, first=0.8, then=(600.0, 1200.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-cal")
        for index, tool in enumerate(("calendar_list", "web_search", "ask_user")):
            _dispatch(runtime.conn, action_id=f"ACT-{index}", turn_id="T-cal", tool_name=tool)
        _settle(0.5)
        assert _count(reader, "response.started") == 0
        _wait_until_turn_spoke(reader, "T-cal")
    assert _lines(reader, "T-cal")[0] in _WAIT_ZH


def test_a_follow_up_keeps_off_a_pending_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask owns the surface (ADR-0014): the first line speaks, the follow-up waits it out."""
    _clocks(monkeypatch, first=0.2, then=(0.8, 1.2))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-ask2")
        _wait_until_turn_spoke(reader, "T-ask2")
        emit_event(
            runtime.conn,
            type="confirmation.requested",
            payload={
                "confirmation_id": "CONF-still",
                "action_snapshot": {"tool_name": "write_file", "risk_level": "high"},
                "template_line": "要写入吗",
                "expires_at_ms": int(time.time() * 1000) + 600_000,
            },
            correlation={"turn_id": "T-ask2"},
        )
        _settle(1.6)
    assert len(_lines(reader, "T-ask2")) == 1


def test_the_models_own_line_replaces_the_first_line_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lead_in is said at the clock; the follow-ups still come from the still pool."""
    _clocks(monkeypatch, first=0.2, then=(0.6, 1.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    line = "我看一下现在几点。"
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-lead2")
        _dispatch(
            runtime.conn, action_id="ACT-l2", turn_id="T-lead2",
            tool_name="calendar_list", lead_in=line,
        )
        _wait_until(lambda: len(_lines(reader, "T-lead2")) == _SHIPPED_MAX_LINES)
    lines = _lines(reader, "T-lead2")
    assert lines[0] == line
    assert all(item in _STILL_ZH for item in lines[1:])


# --- the answer and the line race (live test 2026-10-01, Tbd3d5d36) -------------


def test_a_line_whose_answer_starts_during_the_decision_is_never_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The projection rebuild between the check and the render took longer than the answer.

    Live: the final's first chunk was committed at 05.785, the line rendered at
    05.916. The checks now come last; here the answer starts inside the rebuild.
    """
    _clocks(monkeypatch, first=0.0, then=(600.0, 1200.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    spoke = _user_turn(runtime.conn, "T-race")
    real_rebuild = projections.rebuild_projections

    def _slow_rebuild(conn: sqlite3.Connection) -> object:
        _answer_started(runtime.conn, "T-race")
        return real_rebuild(conn)

    monkeypatch.setattr(inherent_loop, "rebuild_projections", _slow_rebuild)
    assert inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, trigger_event=spoke,
    ) is None
    assert _count(reader, "response.started") == 0
    assert _count(reader, "surface.response_emitted") == 0


@pytest.mark.parametrize("kind", ["surface.response_chunk", "surface.playback_started"])
def test_the_answers_first_chunk_or_playback_counts_as_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    """Not only its open row: a chunk or its playback start of any phase but commentary."""
    _clocks(monkeypatch, first=0.0, then=(600.0, 1200.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    spoke = _user_turn(runtime.conn, "T-chunk")
    payload: dict[str, Any] = {"turn_id": "T-chunk", "phase": "final", "response_id": "R-final"}
    if kind == "surface.response_chunk":
        payload |= {"text": "<voice>Yes.", "sequence": 0}
    else:
        payload |= {
            "session_id": "S", "playback_generation_id": 1, "channel": "both",
            "speech_text_hash": "0" * 64,
        }
    emit_event(runtime.conn, type=kind, payload=payload, correlation={"turn_id": "T-chunk"})
    assert inherent_loop._open_commentary_in_worker_thread(  # noqa: SLF001
        runtime, trigger_event=spoke,
    ) is None
    assert _count(reader, "response.started") == 0


def test_a_line_the_media_owner_dropped_unplayed_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`surface.speech_dropped` is the media owner discarding a line the answer overtook.

    The run must not stay open until teardown. The answer's own rows alone cancel
    nothing: only the media owner knows whether the line had started playing, and
    cancelling a playing line would purge the answer queued behind it.
    """
    _clocks(monkeypatch, first=0.2, then=(600.0, 1200.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-pending")
        _wait_until_turn_spoke(reader, "T-pending")
        _answer_started(runtime.conn, "T-pending")
        _settle()
        assert _cancel_reasons(reader) == []
        (started,) = _typed_payloads(reader, "response.started")
        emit_event(
            runtime.conn,
            type="surface.speech_dropped",
            payload={
                "response_id": started["response_id"], "turn_id": "T-pending",
                "reason": "answer_started",
            },
            correlation={"turn_id": "T-pending"},
        )
        _wait_until(lambda: _cancel_reasons(reader) == ["answer_started"])


def test_a_line_that_was_heard_is_not_cancelled_by_a_drop_of_another_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the line reached the speaker it completes; the answer queues behind it."""
    _clocks(monkeypatch, first=0.2, then=(600.0, 1200.0))
    runtime = _make_runtime(tmp_path)
    reader = _reader(runtime)
    with _Observer(runtime):
        _user_turn(runtime.conn, "T-heard")
        _wait_until_turn_spoke(reader, "T-heard")
        (started,) = _typed_payloads(reader, "response.started")
        emit_event(
            runtime.conn,
            type="surface.playback_started",
            payload={
                "turn_id": "T-heard", "phase": "commentary", "response_id": started["response_id"],
                "session_id": "S", "playback_generation_id": 1, "channel": "speech",
                "speech_text_hash": "0" * 64,
            },
            correlation={"turn_id": "T-heard"},
        )
        _wait_until(lambda: _count(reader, "response.completed") == 1)
        _answer_started(runtime.conn, "T-heard")
        _settle()
    assert _cancel_reasons(reader) == []


class _VoiceOps:
    """The ``voice`` envelopes the media owner sends, in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, object]]] = []

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        self.sent.append((phase, turn_id, payload))


def _replay(
    runtime: JarvisRuntime, reader: sqlite3.Connection, ops: _VoiceOps | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Run the media owner over this log, in its order; return its terminals and drops."""
    reset_realtime_trace()
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(candidate_count=1),
        player=player,
        conn_factory=lambda: open_event_log(runtime.runtime_paths.event_log),
        boot_high_water_id=0,
        config=_media_config(),
        broadcaster=ops,
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _surface_stream(reader)))
            assert pipeline.wait_until_idle(timeout_s=5.0)
    finally:
        assert pipeline.close()
    terminals = {str(p["response_id"]): kind for kind, p in _terminal_rows(reader)}
    return terminals, _typed_payloads(reader, "surface.speech_dropped")


def _line_rows(tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """The rows of a real wait line for turn T-queue, harvested from a scratch log."""
    (tmp_path / "scratch").mkdir()
    scratch = _make_runtime(tmp_path / "scratch")
    spoke = _user_turn(scratch.conn, "T-queue")
    intent = commentary_intent_for(spoke, user_text="现在几点", pick=_first)
    assert intent is not None
    inherent_loop._render_commentary(  # noqa: SLF001
        scratch, scratch.conn, intent=intent, trigger_event=spoke, turn_id="T-queue",
        speech=commentary_speech_text(intent),
    )
    return [
        (event.type, dict(event.payload))
        for _row_id, event in _surface_stream(_reader(scratch))
    ]


def _emit_rows(runtime: JarvisRuntime, rows: list[tuple[str, dict[str, Any]]]) -> None:
    for event_type, payload in rows:
        emit_event(
            runtime.conn, type=event_type, payload=payload, correlation={"turn_id": "T-queue"},
        )


def _one_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[JarvisRuntime, sqlite3.Connection, Event, str, list[tuple[str, dict[str, Any]]]]:
    """A runtime with turn T-queue claimed, a scripted final, and a line to emit by hand."""
    _script_final(monkeypatch)
    lines = _line_rows(tmp_path)
    (tmp_path / "main").mkdir()
    runtime = _make_runtime(tmp_path / "main")
    intent = _user_turn(runtime.conn, "T-queue")
    return runtime, _reader(runtime), intent, str(lines[0][1]["response_id"]), lines


def test_a_line_arriving_after_the_answer_started_is_never_played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live Tbd3d5d36: the answer's rows, then the line's; it played after the whole answer."""
    runtime, reader, intent, line_id, lines = _one_turn(tmp_path, monkeypatch)
    _drive_final(runtime, intent)
    _emit_rows(runtime, lines)
    terminals, dropped = _replay(runtime, reader)
    (final_id,) = {
        str(p["response_id"]) for p in _typed_payloads(reader, "surface.response_open")
        if p["phase"] == "final"
    }
    assert terminals == {final_id: "surface.playback_completed"}
    assert [(row["response_id"], row["reason"]) for row in dropped] == [(line_id, "answer_started")]


def test_a_pending_line_is_dropped_when_the_answer_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line is buffered, not yet playing, when the answer's open arrives."""
    runtime, reader, intent, line_id, lines = _one_turn(tmp_path, monkeypatch)
    _emit_rows(runtime, lines[:-1])  # open and chunks: nothing to schedule yet
    _drive_final(runtime, intent)
    _emit_rows(runtime, lines[-1:])  # emitted
    terminals, dropped = _replay(runtime, reader)
    (final_id,) = {
        str(p["response_id"]) for p in _typed_payloads(reader, "surface.response_open")
        if p["phase"] == "final"
    }
    assert terminals == {final_id: "surface.playback_completed"}
    assert [(row["response_id"], row["reason"]) for row in dropped] == [(line_id, "answer_started")]


def test_a_line_that_started_before_the_answer_still_plays_and_the_answer_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal order is untouched: line first, answer queued behind it, both completed."""
    runtime, reader, intent, line_id, lines = _one_turn(tmp_path, monkeypatch)
    _emit_rows(runtime, lines)
    _drive_final(runtime, intent)
    ops = _VoiceOps()
    terminals, dropped = _replay(runtime, reader, ops)
    (final_id,) = {
        str(p["response_id"]) for p in _typed_payloads(reader, "surface.response_open")
        if p["phase"] == "final"
    }
    assert terminals == {
        line_id: "surface.playback_completed", final_id: "surface.playback_completed",
    }
    assert dropped == []
    # The companion tells the line from the answer by `response_phase`, on every voice op.
    spoken = [payload for phase, _turn, payload in ops.sent if phase == "spoken"]
    assert [payload.get("response_phase") for payload in spoken] == ["commentary", None]
    playing = [payload for phase, _turn, payload in ops.sent if phase == "playing"]
    assert all(payload.get("response_phase") in ("commentary", None) for payload in playing)


class _FakeSocket:
    """One companion: every envelope the broadcaster sends it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


def test_the_wire_marks_a_wait_line_and_says_nothing_of_its_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion must tell a wait line from her answer, and a taken-back line from a cancel.

    Live 2026-10-02: the line's `open` cleared the wait the tool status line shows
    under, and a cancelled line would have cleared it as a cancelled turn.
    """
    _script_final(monkeypatch)
    runtime = _make_runtime(tmp_path)
    socket = _FakeSocket()
    broadcaster = InherentBroadcaster()

    async def _go() -> None:
        await broadcaster.register(socket)  # type: ignore[arg-type]
        conn = open_event_log(runtime.runtime_paths.event_log)
        task = asyncio.create_task(
            inherent_loop._response_watcher(  # noqa: SLF001
                replace(runtime, conn=conn), broadcaster, poll_interval_s=0.01,
            ),
        )
        await asyncio.sleep(0.1)
        spoke = _user_turn(runtime.conn, "T-wire")
        intent = commentary_intent_for(spoke, user_text="现在几点", pick=_first)
        assert intent is not None
        entry = inherent_loop._render_commentary(  # noqa: SLF001
            runtime, runtime.conn, intent=intent, trigger_event=spoke, turn_id="T-wire",
            speech=commentary_speech_text(intent),
        )
        inherent_loop._cancel_unheard_commentary(  # noqa: SLF001
            runtime, entry, reason="answer_started",
        )
        await asyncio.to_thread(_drive_final, runtime, spoke)
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        conn.close()

    asyncio.run(_go())
    ops = [(m["op"], m["payload"].get("response_phase")) for m in socket.sent]
    assert ops[:3] == [("open", "commentary"), ("append", "commentary"), ("append", "commentary")]
    assert ("done", "commentary") in ops
    # Her answer carries no mark, and the line's own cancel is not sent as a cancelled turn.
    assert ("open", None) in ops
    assert all(op != "cancelled" for op, _ in ops)


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
            written_apart=False,
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
        _dispatch(runtime.conn, action_id="ACT-b", turn_id="T-both")
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
    _dispatch(off.conn, action_id="ACT-cmp", turn_id="T-cmp")
    _drive_final(off, off_intent)
    assert off.response_flags.lifecycle_commentary is False

    on = _make_runtime(tmp_path / "on", commentary=True)
    on_reader = _reader(on)
    on_intent = _user_turn(on.conn, "T-cmp")
    with _Observer(on):
        _dispatch(on.conn, action_id="ACT-cmp", turn_id="T-cmp")
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
        _dispatch(runtime.conn, action_id="ACT-f", turn_id="T-fold")
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
        dispatched = _dispatch(runtime.conn, action_id="ACT-rt", turn_id="T-route",
        )
        _wait_until(lambda: _count(reader, "surface.response_emitted") == 1)
    # Close the action so the status board is quiet; the history is then the
    # only thing that could still force `unknown`.
    _action_row(
        runtime.conn, "action.result_observed", action_id="ACT-rt", turn_id="T-route",
        source_event_id=dispatched.event_uid,
    )
    later = _user_turn(reader, "T-next", transcript="随便说点什么吧")
    packet = assemble_packet(later, reader)
    assert fold_conversation_history(iter_events(reader)).consistent is True
    assert pre_route(
        packet,
        tier0_table=runtime.tier0_table,
        tool_cues=runtime.tool_cues,
        now_ms=int(time.time() * 1000),
    ) == "casual_or_explanatory"


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
        _dispatch(runtime.conn, action_id="ACT-op", turn_id="T-opcancel",
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
        _dispatch(runtime.conn, action_id="ACT-bi", turn_id="T-comm",
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
        _dispatch(runtime.conn, action_id="ACT-cl", turn_id="T-closed",
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
        _dispatch(runtime.conn, action_id="ACT-cl2", turn_id="T-closed-2",
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


@pytest.mark.parametrize(
    ("reason", "heard", "language"),
    [("wait", "等我一下。", "zh"), ("dismissed", "Okay, bye.", "en")],
)
def test_words_that_steer_the_mode_get_one_fixed_line_and_no_turn(
    tmp_path: Path, reason: str, heard: str, language: lang.Language,
) -> None:
    """ADR 0102: 「等我一下」 or a dismissal is answered by a fixed line, never a model."""
    runtime = _make_runtime(tmp_path, commentary=False)

    inherent_loop._say_conversation_line(runtime, "T-words", reason, heard)  # noqa: SLF001

    assert _typed_payloads(runtime.conn, "surface.conversation_words") == [
        {"turn_id": "T-words", "reason": reason, "transcript": heard},
    ]
    assert _only_phrase(runtime.conn) in lang.variants(f"conversation.{reason}", language)
    assert _count(runtime.conn, "response.completed") == 1
    assert _count(runtime.conn, "utterance.received") == 0
    assert fold_conversation_history(iter_events(runtime.conn)).turns == ()
