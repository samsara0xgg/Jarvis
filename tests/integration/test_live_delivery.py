"""Acceptance for ADR 0026: a Live delegation's outcome outlives the session.

The first case drives the composition root's backend hooks against a real
Event Log on disk. The wire cases drive ``LiveVoice`` over a fake socket and
assert on what it sends and what it persists: the commentary a new session
is told at start, the delivery mark an ACK writes, a late result spoken as
commentary, a usage-less close kept truthful, and a dead microphone upload
that ends the session under its own reason.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from websockets.asyncio import client as ws_client

from jarvis.runtime.inherent_loop import _LiveBackend
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.surface import voice_live, voice_tts
from jarvis.surface.voice_live import (
    DelegationResult,
    GptLiveConfig,
    LiveVoice,
    UndeliveredResult,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_DAY_MS = 24 * 60 * 60 * 1000


def _answer(conn: sqlite3.Connection, turn_id: str, text: str) -> None:
    emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            "turn_id": turn_id,
            "response_id": f"R-{turn_id}",
            "text": text,
            "phase": "final",
            "voice_text": text,
        },
        correlation={"turn_id": turn_id},
    )


def test_backend_hooks_track_which_outcomes_a_session_was_told(tmp_path: Path) -> None:
    """Undelivered = finished, Live-channel, last day, no delivery mark; usage is one row."""
    path = tmp_path / "mac_events.db"
    open_event_log(path).close()
    backend = _LiveBackend(event_log_path=path, memory=None)
    conn = open_event_log(path)

    first = backend.delegate("明天温哥华天气", "d-1", "S-1", "rec-1")
    assert backend.undelivered_results() == []  # still running: no outcome to tell

    _answer(conn, first, "明天温哥华多云，最高十五度。")
    second = backend.delegate("查一下", "d-2", "S-1", "rec-2")
    emit_event(
        conn,
        type="turn.failed",
        payload={"turn_id": second, "exception_repr": "boom"},
        correlation={"turn_id": second},
    )
    assert backend.undelivered_results() == [
        UndeliveredResult(
            turn_id=first,
            request="明天温哥华天气",
            result=DelegationResult(
                status="answered",
                text="明天温哥华多云，最高十五度。",
                voice_text="明天温哥华多云，最高十五度。",
            ),
        ),
        UndeliveredResult(
            turn_id=second,
            request="查一下",
            result=DelegationResult(status="failed", reason="boom"),
        ),
    ]

    backend.mark_delivered(first, "S-2", "commentary")
    backend.mark_delivered(second, "S-2", "withheld")
    assert backend.undelivered_results() == []
    rows = list(iter_events_of_types(conn, ("live.result_delivered",)))
    assert [(r.payload, r.correlation) for r in rows] == [
        ({"turn_id": first, "session_id": "S-2", "kind": "commentary"}, {"turn_id": first}),
        ({"turn_id": second, "session_id": "S-2", "kind": "withheld"}, {"turn_id": second}),
    ]

    # A text-channel turn and a day-old Live turn are never offered.
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "文字提问", "turn_id": "T-text"},
        correlation={"turn_id": "T-text"},
    )
    _answer(conn, "T-text", "文字答案")
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "旧问题", "turn_id": "T-old", "channel": "gpt_live"},
        correlation={"turn_id": "T-old"},
        ts_epoch_ms=int(time.time() * 1000) - _DAY_MS - 60_000,
    )
    _answer(conn, "T-old", "旧答案")
    assert backend.undelivered_results() == []

    backend.record_usage("S-2", 12.0, "user", "close_requested", final=True)
    backend.record_usage("S-3", None, "send_failed", None, final=False)
    usage = list(iter_events_of_types(conn, ("live.session_usage",)))
    assert [r.payload for r in usage] == [
        {
            "session_id": "S-2",
            "seconds": 12.0,
            "final": True,
            "reason": "user",
            "server_reason": "close_requested",
        },
        {
            "session_id": "S-3",
            "seconds": None,
            "final": False,
            "reason": "send_failed",
            "server_reason": None,
        },
    ]
    conn.close()


# --- wire harness -----------------------------------------------------------


class _Wire:
    """Fake websocket: records every frame sent, serves queued server events."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.fail_audio = False
        self.closed = False

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        if self.fail_audio and frame["type"] == "session.input_audio.append":
            msg = "socket gone"
            raise OSError(msg)
        self.sent.append(frame)

    def __aiter__(self) -> _Wire:
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(None)

    def serve(self, event: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(event))

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.sent if f["type"] == kind]


class _Player:
    def __init__(self, **_: object) -> None:
        self.gain: float | None = None

    def start(self) -> None:
        pass

    def set_gain(self, gain: float) -> None:
        self.gain = gain

    def bytes_pending(self) -> int:
        return 0

    def write(self, *_: object, **__: object) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Subscription:
    def __init__(self, frames: list[SimpleNamespace]) -> None:
        self._frames = frames

    def read(self, *, timeout_s: float) -> SimpleNamespace | None:
        if self._frames:
            return self._frames.pop(0)
        time.sleep(timeout_s)
        return None

    def close(self) -> None:
        pass


class _Ingress:
    def __init__(self, frames: list[SimpleNamespace] | None = None) -> None:
        self.frames = frames or []

    def subscribe(self, *, name: str, purpose: object) -> _Subscription:
        del name, purpose
        return _Subscription(self.frames)


class _Broadcaster:
    def __init__(self) -> None:
        self.ops: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_op(self, op: str, **kwargs: object) -> None:
        self.ops.append((op, kwargs))


def _config() -> GptLiveConfig:
    return GptLiveConfig(
        enabled=True,
        sample_rate_hz=24000,
        connect_timeout_s=1.0,
        close_timeout_s=0.3,
        delegation_timeout_s=0.3,
        transcript_settle_ms=50,
    )


async def _start(
    monkeypatch: pytest.MonkeyPatch,
    wire: _Wire,
    *,
    ingress: _Ingress | None = None,
    **hooks: Any,  # noqa: ANN401 - the LiveVoice callables under test
) -> LiveVoice:
    async def connect(*_: object, **__: object) -> _Wire:
        return wire

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ws_client, "connect", connect)
    monkeypatch.setattr(voice_tts, "AudioStreamPlayer", _Player)
    lane = ingress or _Ingress()
    live = LiveVoice(
        config=_config(),
        broadcaster=_Broadcaster(),  # type: ignore[arg-type]
        ingress=lambda: lane,  # type: ignore[arg-type,return-value]
        mic_muted=lambda: False,
        speech_muted=lambda: False,
        **hooks,
    )
    wire.serve({"type": "session.started", "session": {"id": "S-1"}})
    status = await live.start()
    assert status["state"] == "active", status
    return live


async def _until(check: Callable[[], Any], *, timeout_s: float = 3.0) -> Any:  # noqa: ANN401
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    msg = f"condition not met within {timeout_s}s"
    raise AssertionError(msg)


def test_new_session_is_told_undelivered_outcomes_and_marks_them_on_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start sends the outcome as commentary; the ACK, not the send, records delivery."""
    marks: list[tuple[str, str, str]] = []
    usages: list[tuple[Any, ...]] = []
    pending = [
        UndeliveredResult(
            turn_id="T-9",
            request="明天温哥华天气",
            result=DelegationResult(status="answered", text="明天温哥华多云，最高十五度。"),
        ),
    ]

    async def scenario() -> None:
        wire = _Wire()
        live = await _start(
            monkeypatch,
            wire,
            undelivered=lambda: pending,
            mark_delivered=lambda t, s, k: marks.append((t, s, k)),
            record_usage=lambda *args: usages.append(args),
        )
        [offer] = await _until(lambda: wire.frames("session.commentary.append"))
        assert offer["delegation_id"] is None
        assert "明天温哥华天气" in offer["content"]
        assert "多云" in offer["content"]
        assert marks == []  # sent is not delivered: the ACK is the receipt
        wire.serve({"type": "session.commentary.appended", "client_event_id": offer["event_id"]})
        await _until(lambda: marks)
        assert marks == [("T-9", "S-1", "commentary")]

        # A usage figure, then a close without one: the figure stays, unconfirmed,
        # and the close is recorded under the reason that caused it.
        wire.serve({"type": "session.usage.updated", "usage": {"seconds": 5}})
        await _until(lambda: live.status()["usage_s"] == 5.0)
        stop = asyncio.ensure_future(live.stop())
        await _until(lambda: wire.frames("session.close"))
        wire.serve({"type": "session.closed", "reason": "close_requested"})
        status = await stop
        assert status["state"] == "idle"
        assert (status["reason"], status["server_reason"]) == ("user", "close_requested")
        assert (status["usage_s"], status["usage_final"]) == (5.0, False)
        assert usages == [("S-1", 5.0, "user", "close_requested", False)]

    asyncio.run(scenario())


def test_late_result_is_spoken_while_the_session_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the timeout notice, the answer still goes out as commentary and is marked."""
    outcome: dict[str, DelegationResult] = {}
    marks: list[tuple[str, str, str]] = []

    async def scenario() -> None:
        wire = _Wire()
        live = await _start(
            monkeypatch,
            wire,
            delegate=lambda *_: "T-2",
            record=lambda *_: None,
            lookup_result=outcome.get,
            undelivered=list,
            mark_delivered=lambda t, s, k: marks.append((t, s, k)),
        )
        wire.serve({
            "type": "session.input_transcript.delta",
            "start_ms": 500,
            "end_ms": 1400,
            "delta": "查一下明天温哥华天气",
        })
        wire.serve({
            "type": "session.delegation.created",
            "offset_ms": 1500,
            "delegation": {"id": "d-1"},
        })
        [progress] = await _until(lambda: wire.frames("session.thinking.append"))
        assert progress["content"].startswith("正在查")
        await _until(lambda: [
            f for f in wire.frames("session.commentary.append")
            if f["content"] == voice_live.TIMEOUT_COMMENTARY
        ])
        assert marks == []  # the timeout notice is not an outcome

        outcome["T-2"] = DelegationResult(status="answered", text="明天温哥华多云。")
        live.deliver("T-2")
        [spoken] = await _until(lambda: [
            f for f in wire.frames("session.commentary.append") if "多云" in f["content"]
        ])
        assert spoken["delegation_id"] == "d-1"
        assert not [f for f in wire.frames("session.thinking.append") if "多云" in f["content"]]
        wire.serve({"type": "session.commentary.appended", "client_event_id": spoken["event_id"]})
        await _until(lambda: marks)
        assert marks == [("T-2", "S-1", "commentary")]
        await live.stop()

    asyncio.run(scenario())


def test_dead_microphone_upload_ends_the_session_as_send_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed audio send closes the session under its own reason, with usage unconfirmed."""
    usages: list[tuple[Any, ...]] = []
    frame = SimpleNamespace(
        sample_rate_hz=24000,
        frame_count=2400,
        pcm16_mono=bytes(4800),
        discontinuity_before=False,
        sequence=1,
    )

    async def scenario() -> None:
        wire = _Wire()
        wire.fail_audio = True
        live = await _start(
            monkeypatch,
            wire,
            ingress=_Ingress([frame]),
            record_usage=lambda *args: usages.append(args),
        )
        await _until(lambda: live.status()["state"] == "idle")
        status = live.status()
        assert status["reason"] == "send_failed"
        assert str(status["error"]).startswith("send:")
        assert usages == [("S-1", None, "send_failed", None, False)]

    asyncio.run(scenario())
