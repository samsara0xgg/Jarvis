"""A terminal's voice link on a loop of its own, the brain's listening end, and their stand-ins."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from jarvis.runtime import inherent_loop
from jarvis.runtime import terminal as terminal_module
from jarvis.runtime.terminal import _Speaking, _Speech, _start_speech, _stop_speech
from jarvis.state.device_tokens import pair_device
from jarvis.surface import voice_asr, voice_audio, voice_backend, voice_tts, voice_wake
from jarvis.surface.terminal_events import EventOutbox
from jarvis.surface.terminal_listen import BrainListening, ListenHooks
from jarvis.surface.terminal_speaker import Journal, VoiceLink
from tests.support.terminal import (
    SPEECH_CONFIG,
    TERMINAL_WS,
    bearer,
    brain_client,
    hello_frame,
    open_stream,
)
from tests.support.voice_input import EnergySession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient

    from jarvis.surface.terminal_link import TerminalHub


class ListenBench:
    """A terminal's VoiceLink on a loop of its own; ``answer`` plays the brain, or is silent."""

    def __init__(
        self,
        tmp_path: Path,
        answer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.frames: list[dict[str, Any]] = []
        self.answer = answer
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.link = VoiceLink(Journal(tmp_path / "journal.db"))
        self.link.up(self._send, self.loop, {"type": "ready", "voice": True, "listen": True})

    async def _send(self, text: str) -> None:
        frame = json.loads(text)
        self.frames.append(frame)
        rid = frame.get("id")
        if rid is not None and self.answer is not None:
            reply = self.answer(frame)
            if reply is not None:
                self.link.on_frame({"type": "reply", "id": rid, **reply})

    def ops(self) -> list[str]:
        return [str(frame["op"]) for frame in self.frames]

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def ok_reply(value: object) -> dict[str, Any]:
    return {"ok": True, "value": value}


def listening_client(
    tmp_path: Path,
    **hooks: Any,  # noqa: ANN401 — whichever hooks a test binds.
) -> tuple[TestClient, TerminalHub, Path]:
    client, hub, log = brain_client(tmp_path)
    listening = BrainListening(hub, hub.events)  # type: ignore[arg-type]
    listening.hooks = ListenHooks(**hooks)
    hub.listening = listening
    return client, hub, log


def ask_op(
    ws: Any,  # noqa: ANN401
    op: str,
    args: dict[str, Any] | None = None,
    rid: str | None = "r" * 32,
) -> None:
    frame: dict[str, Any] = {"type": "ask", "op": op, "args": args or {}}
    if rid is not None:
        frame["id"] = rid
    ws.send_text(json.dumps(frame))


def utterance_frame(turn: str, said: str = "turn on the light", **extra: object) -> dict[str, Any]:
    return {
        "transcript": said,
        "turn_id": turn,
        "utterance_id": f"U-{turn}",
        "channel": "inherent_wake",
        "confidence": 0.9,
        "language": "zh",
        **extra,
    }


def logged_utterances(log: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(log) as conn:
        return conn.execute(
            "SELECT event_uid, ingestion_node, payload_json FROM events "
            "WHERE type = 'utterance.received' ORDER BY id",
        ).fetchall()


def connect_listening(client: TestClient, tmp_path: Path, name: str, stack: Any) -> Any:  # noqa: ANN401
    ws = stack.enter_context(
        client.websocket_connect(TERMINAL_WS, headers=bearer(pair_device(tmp_path, name))),
    )
    ws.send_text(hello_frame(voice=True, rows_after=None))
    ready = ws.receive_json()
    assert ready["listen"] is True
    return ws


class _Ears:
    """The final recognizer, replaced: always hears the same words."""

    def __init__(self, **_ignored: object) -> None:
        pass

    def prewarm(self) -> None:
        return None

    def warm(self) -> None:
        return None

    def partial_text(self, audio: bytes) -> str:
        del audio
        return "把灯打开。"

    def recognize(self, audio: bytes) -> voice_asr.TranscriptionResult:
        del audio
        return voice_asr.TranscriptionResult("把灯打开。", 0.9, "zh", None)


class _Wake:
    """A wake engine that always hears the wake word."""

    model_name = "replay"

    def start(self) -> None:
        return None

    def predict(self, _frame: bytes) -> dict[str, float]:
        return {self.model_name: 1.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


LISTEN_CONFIG: dict[str, Any] = {
    "realtime": {
        **SPEECH_CONFIG["realtime"],
        "single_audio_ingress": {
            "enabled": True,
            "echo_cancellation": False,
            "pre_roll_ms": 64,
            "max_utterance_s": 4.0,
            "min_voiced_s": 0.2,
            "session_worker_poll_s": 0.001,
            "partial_asr": {
                "enabled": True,
                "interval_ms": 32,
                "candidate_ms": 64,
                "max_hold_ms": 160,
                "post_roll_ms": 32,
            },
        },
    },
}
"""This terminal's own config: wake, VAD and endpointing knobs live here, not on the brain."""


class _Power:
    """The system's power registration, replaced: keeps the hooks it was given and the shutdown."""

    def __init__(self) -> None:
        self.installs = 0
        self.hooks: tuple[Any, Any] | None = None  # bound methods of the coordinator
        self.log: sqlite3.Connection | None = None
        self.shutdowns = 0

    def install(
        self,
        event_log: sqlite3.Connection,
        *,
        loop: asyncio.AbstractEventLoop,
        before_sleep_hook: Callable[[], object],
        on_wake_hook: Callable[[], object],
    ) -> _Power:
        assert loop.is_running()
        self.installs += 1
        self.log = event_log
        self.hooks = (before_sleep_hook, on_wake_hook)
        return self

    def shutdown(self) -> None:
        self.shutdowns += 1


def heard_session(
    monkeypatch: pytest.MonkeyPatch,
    backend: Callable[[], voice_backend.AudioDuplexBackend],
) -> _Power:
    """Replace the machine under the capture session: models, microphone, speaker, power.

    CoreAudio lists no devices (the device watch has nothing to watch and ends) and the system's
    power registration is a stand-in, so no test registers with the real machine.
    """
    power = _Power()
    monkeypatch.setattr(inherent_loop, "install_power_observer", power.install)
    monkeypatch.setattr(voice_backend, "coreaudio_devices", lambda _kind: None)
    monkeypatch.setattr(terminal_module, "voice_models_preflight", lambda **_k: (True, []))
    monkeypatch.setattr(voice_asr, "SenseVoiceRecognizer", _Ears)
    monkeypatch.setattr(voice_wake, "MicroWakeWordEngine", _Wake)
    monkeypatch.setattr(voice_audio, "_load_silero_session", lambda *_a, **_k: EnergySession())
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,  # noqa: SLF001
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    monkeypatch.setattr(voice_backend, "SoundDeviceDuplexBackend", lambda **_k: backend())
    monkeypatch.setattr(voice_tts, "_open_output_stream", open_stream(pull=True))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    return power


@contextlib.asynccontextmanager
async def terminal_without_a_brain(root: Path) -> AsyncIterator[tuple[_Speaking, _Speech]]:
    """A voice terminal's media actor as ``_run`` builds it, with no brain on its link.

    What a capture session is started from: the test starts it (or the wait before its next
    try) and ends it, on this loop, with the injected clock it passes.
    """
    speaking = _Speaking(LISTEN_CONFIG, root)
    speech = _start_speech(speaking, EventOutbox())
    try:
        yield speaking, speech
    finally:
        await _stop_speech(speech)
