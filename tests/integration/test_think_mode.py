"""ADR 0108 — an on-word makes its own turn think, and only that turn; response.started says so.

Six turns of one evening, driven through ``drive_turn`` with the model scripted: the turn whose own
words hold an on-word gets the think preset, the next turn without one gets the default again, and
an on-word in a later turn thinks again. The observables are ``response.started.reasoning_effort``
and the ``on`` the companion reads from ``GET /inherent/think`` (ADR 0108) once the words are in
and again once the answer is done.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

import jarvis.decision.think_mode as think_mode_module
import jarvis.runtime as runtime_module
import jarvis.state.event_log as event_log_module
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.think_mode import ThinkMode, ThinkModeConfigError, load_think_mode
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, _wave4_response_flags, drive_turn
from jarvis.shared import Event
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root
from tests.integration.test_wave4a_response_run import _final_result, _realtime_config

if TYPE_CHECKING:
    from pathlib import Path

_PRESET = {"model": "gpt-5.6-luna", "base_url": "https://example.invalid", "max_tokens": 64}
_LLM: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "luna",
    # The shipped words, read as YAML the way the daemon reads them.
    "think": yaml.safe_load((repo_root() / "config/jarvis.yaml").read_text())["llm"]["think"],
    "presets": {
        "luna": {**_PRESET, "reasoning_effort": "none"},
        "luna-think": {**_PRESET, "reasoning_effort": "medium", "api": "responses"},
    },
}
_MINUTE = 60_000
# (minutes after the first turn, what Allen says, the effort its answer must use)
_EVENING = (
    (0, "今天天气怎么样", "none"),
    (1, "想想吧 这周先做哪个项目", "medium"),
    (2, "那第二个呢", "none"),
    (3, "不用想了 几点了", "none"),
    (4, "好好想想晚饭吃什么", "medium"),
    (5, "你好", "none"),
)


@dataclass
class _Clock:
    """The ``time`` module with a settable wall clock."""

    now_ms: int

    def time(self) -> float:
        return self.now_ms / 1000

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — every other time function is real.
        return getattr(time, name)


def _words(conn: sqlite3.Connection, said: str, turn_id: str, at_ms: int) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={
            "transcript": said,
            "turn_id": turn_id,
            "channel": "inherent_wake",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
        ts_epoch_ms=at_ms,
    )


def _clocked(monkeypatch: pytest.MonkeyPatch, start_ms: int) -> _Clock:
    """One wall clock for the runtime, the event log and the mode, so every row is stamped by it."""
    clock = _Clock(start_ms)
    for module in (runtime_module, think_mode_module, event_log_module):
        monkeypatch.setattr(module, "time", clock)
    return clock


def test_on_word_thinks_its_own_turn_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer's response.started names the effort its own words called for."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    config = _realtime_config(lifecycle=True, cancel=False)
    runtime = JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(_LLM),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True, lifecycle_terminal_cas=True
        ),
        response_flags=_wave4_response_flags(config),
        llm_session_factory=LLMSessionFactory(_LLM),
        committed_event_bus=CommittedEventBus(),
        think_mode=load_think_mode(_LLM),
    )
    monkeypatch.setattr(runtime_module, "decide", _final_result())
    start = int(time.time() * 1000)
    clock = _clocked(monkeypatch, start)
    think = runtime.think_mode
    assert think is not None

    def status() -> dict[str, Any]:
        # The route runs on its own thread, so it reads the log over a connection of its own.
        with closing(sqlite3.connect(paths.event_log)) as reader:
            return think.status(reader)

    # The companion's read, over the real route.
    wire = TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                think_read=status,
            )
        )
    )
    shown: list[tuple[str | None, str | None]] = []

    def read() -> str | None:
        """The turn the route says is thinking, with the rest of its shape checked."""
        body = wire.get("/inherent/think").json()
        assert body == {
            "on": body["turn_id"] is not None,
            "on_words": think.on.pattern,
            "turn_id": body["turn_id"],
        }
        turn_id: str | None = body["turn_id"]
        return turn_id

    for minute, said, _ in _EVENING:
        clock.now_ms = start + minute * _MINUTE
        intent = _words(conn, said, f"T{minute}", clock.now_ms)
        words_in = read()
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        shown.append((words_in, read()))

    efforts = [
        json.loads(p)["reasoning_effort"]
        for (p,) in conn.execute(
            "SELECT payload_json FROM events WHERE type='response.started' ORDER BY id"
        )
    ]
    assert efforts == [effort for _, _, effort in _EVENING]
    # Deep, and named, from the words going in; normal again once that turn's answer is done.
    assert shown == [
        (f"T{minute}" if effort == "medium" else None, None) for minute, _, effort in _EVENING
    ]
    conn.close()


@pytest.mark.parametrize(
    ("event_type", "said", "preset"),
    [
        ("surface.user_intent", "想想吧 这周先做哪个项目", "luna-think"),
        ("utterance.received", "你帮我想想晚饭吃什么", "luna-think"),
        ("surface.user_intent", "Think it over: tea or coffee?", "luna-think"),
        ("surface.user_intent", "那第二个呢", None),
        ("surface.user_intent", "不用想了 几点了", None),
        ("surface.user_intent", "", None),
        # Only Allen's own words choose; nothing else carries a transcript that can.
        ("action.result_observed", "想想吧", None),
    ],
)
def test_preset_is_chosen_by_the_turns_own_words(
    tmp_path: Path, event_type: str, said: str, preset: str | None
) -> None:
    """The preset follows the words that opened this turn, never an earlier one."""
    mode = load_think_mode(_LLM)
    assert mode is not None
    turn = Event(
        event_uid="e1",
        type=event_type,
        schema_version=1,
        ts_epoch_ms=0,
        payload={"transcript": said, "turn_id": "T"},
        source_event_id=None,
        correlation=None,
    )
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        assert mode.preset_for(conn, turn) == preset


def test_a_turn_that_never_ended_stops_looking_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash writes no end event, so the pending look gives up on its own."""
    conn = open_event_log(tmp_path / "events.db")
    start = int(time.time() * 1000)
    clock = _clocked(monkeypatch, start)
    mode = load_think_mode(_LLM)
    assert mode is not None
    _words(conn, "想想吧 这周先做哪个项目", "T0", start)
    seen = []
    for minutes in (0, 9, 11):
        clock.now_ms = start + minutes * _MINUTE
        seen.append(mode.status(conn)["turn_id"])
    assert seen == ["T0", "T0", None]
    conn.close()


def test_an_existing_config_with_off_words_still_boots() -> None:
    """An older settings.yaml may still carry ``off_words``; nothing reads it, nothing fails."""
    think = {**_LLM["think"], "off_words": "不用想了|stop thinking"}
    mode = load_think_mode({**_LLM, "think": think})
    assert isinstance(mode, ThinkMode)
    assert mode.on.pattern == _LLM["think"]["on_words"]
    with pytest.raises(ThinkModeConfigError):
        load_think_mode({**_LLM, "think": {**think, "preset": "missing"}})


def _run(
    conn: sqlite3.Connection,
    kind: str,
    turn_id: str,
    response_id: str,
    at_ms: int,
    **more: Any,  # noqa: ANN401
) -> None:
    """One response event the way the lifecycle writes it, without its other consequences."""
    payload: dict[str, Any] = {
        "response_id": response_id,
        "response_group_id": response_id,
        "turn_id": turn_id,
        **more,
    }
    if kind == "response.started":
        payload |= {
            "phase": more.get("phase", "final"),
            "channel": "voice_notify",
            "emission_mode": "full_text",
            "output_risk_class": "routine",
            "required_gate_mode": "full",
            "policy_hash": "p",
            "active_subject_ref": "s",
            "evidence_snapshot_hash": "e",
        }
    elif kind == "response.completed":
        payload["response_hash"] = "h"
    else:
        payload.setdefault("reason", "x")
    emit_event(
        conn, type=kind, payload=payload, correlation={"turn_id": turn_id}, ts_epoch_ms=at_ms
    )


def test_a_split_sentence_thinks_when_its_first_half_holds_the_on_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0074: 帮我想想, a pause, 明天的发布怎么排 is one sentence the second turn answers."""
    conn = open_event_log(tmp_path / "events.db")
    start = int(time.time() * 1000)
    clock = _clocked(monkeypatch, start)
    mode = load_think_mode(_LLM)
    assert mode is not None
    first = _words(conn, "帮我想想", "TA", start)
    _run(conn, "response.started", "TA", "RA", start)
    assert (mode.preset_for(conn, first), mode.status(conn)["turn_id"]) == ("luna-think", "TA")

    # The second half is accepted: the first one's run is cancelled as superseded.
    clock.now_ms = start + 2_000
    _run(conn, "response.cancelled", "TA", "RA", clock.now_ms, reason="superseded")
    second = _words(conn, "明天的发布怎么排", "TB", clock.now_ms)
    assert mode.preset_for(conn, second) == "luna-think"
    _run(conn, "response.started", "TB", "RB", clock.now_ms)
    assert mode.status(conn) == {"on": True, "on_words": mode.on.pattern, "turn_id": "TB"}
    _run(conn, "response.completed", "TB", "RB", clock.now_ms)
    assert mode.status(conn)["turn_id"] is None

    # The next sentence is no part of it: the first half's run was superseded, not answered.
    clock.now_ms = start + 4_000
    third = _words(conn, "那第二个呢", "TC", clock.now_ms)
    assert mode.preset_for(conn, third) is None
    conn.close()


def test_an_answered_on_word_turn_is_not_folded_into_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a superseded utterance joins the next turn; one that was answered stays its own."""
    conn = open_event_log(tmp_path / "events.db")
    start = int(time.time() * 1000)
    clock = _clocked(monkeypatch, start)
    mode = load_think_mode(_LLM)
    assert mode is not None
    _words(conn, "想想吧", "TA", start)
    _run(conn, "response.started", "TA", "RA", start)
    _run(conn, "response.completed", "TA", "RA", start)
    clock.now_ms = start + 1_000
    assert mode.preset_for(conn, _words(conn, "明天的发布怎么排", "TB", clock.now_ms)) is None
    conn.close()


def test_a_turn_thinks_until_every_final_run_it_opened_has_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commentary run ending, or a failed run awaiting its correction, does not end the look."""
    conn = open_event_log(tmp_path / "events.db")
    start = int(time.time() * 1000)
    _clocked(monkeypatch, start)
    mode = load_think_mode(_LLM)
    assert mode is not None
    _words(conn, "想想吧 这周先做哪个项目", "T0", start)
    _run(conn, "response.started", "T0", "R1", start)
    _run(conn, "response.started", "T0", "C1", start, phase="commentary")
    _run(conn, "response.completed", "T0", "C1", start)
    assert mode.status(conn)["turn_id"] == "T0"
    _run(conn, "response.started", "T0", "R2", start, corrects_response_id="R1")
    _run(conn, "response.failed", "T0", "R1", start)
    assert mode.status(conn)["turn_id"] == "T0"
    _run(conn, "response.completed", "T0", "R2", start)
    assert mode.status(conn)["turn_id"] is None
    conn.close()
