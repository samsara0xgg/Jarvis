"""A duplex session whose mode switches a test flips while it runs."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from jarvis.surface import voice_audio, voice_session
from tests.support.voice_input import (
    FakeBackend,
    FakeWakeEngine,
    RecordingPipeline,
    StatefulEnergySession,
    make_ingress,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

_SPEECH = [0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]


class ModeSession:
    """One duplex session whose switches the test flips while it runs."""

    def __init__(  # noqa: PLR0913 - each switch a test flips
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        speaking: bool = False,
        pipeline: RecordingPipeline | None = None,
        hold_output: Callable[[bool], None] | None = None,
        supersede_unspoken: Callable[[str], None] | None = None,
        conversation: bool = True,
        wake: bool = False,
        idle_exit_s: float = 30.0,
        turn_working: Callable[[], bool] | None = None,
        set_quiet: Callable[[str], None] | None = None,
        answer_words: Callable[[str, str, str], None] | None = None,
        floor: Callable[[], str] | None = None,
        broadcaster: Any = None,  # noqa: ANN401 - a recorder with broadcast_voice_sync
        judged_on_send: bool = False,
    ) -> None:
        monkeypatch.setitem(
            voice_audio._MODE_THRESHOLDS,  # noqa: SLF001 - loosened for one-frame onsets
            "record",
            voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
        )
        self.conversation = conversation
        self.changes: list[tuple[bool, str]] = []
        self.muted = False
        self.speaking = speaking
        self.stopped = threading.Event()
        self.backend = FakeBackend()
        self.ingress = make_ingress(self.backend)
        self.wake_engine = FakeWakeEngine(detections=None if wake else set())
        self.pipeline = pipeline or RecordingPipeline()
        silero = StatefulEnergySession()
        with patch.object(voice_audio, "_load_silero_session", return_value=silero):
            self.session = voice_session.DuplexVoiceSession(
                ingress=self.ingress,
                wake_engine=self.wake_engine,
                vad=voice_audio.SileroVad(mode="record"),
                pipeline=self.pipeline,
                broadcaster=broadcaster,
                output_active=lambda: self.speaking,
                wake_threshold=0.5,
                config=replace(
                    voice_session.RealtimeInputSessionConfig(),
                    pre_roll_ms=64,
                    min_voiced_s=0.032,
                    max_utterance_s=2.0,
                    worker_poll_s=0.001,
                    shutdown_timeout_s=1.0,
                    conversation_idle_exit_s=idle_exit_s,
                ),
                mic_muted=lambda: self.muted,
                conversation=lambda: self.conversation,
                set_conversation=self._set_conversation,
                set_quiet=set_quiet,
                answer_words=answer_words,
                turn_working=turn_working,
                stop_speaking=self.stopped.set,
                hold_output=hold_output,
                supersede_unspoken=supersede_unspoken,
                floor=floor,
                judged_on_send=judged_on_send,
            )
            assert self.session.start().started

    def _set_conversation(self, on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
        self.changes.append((on, reason))
        self.conversation = on

    def idle(self, seconds: float) -> None:
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.backend.emit(epoch=epoch, value=0)
            time.sleep(0.002)

    def speak(self) -> None:
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        for value in _SPEECH:
            self.backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)

    def close(self) -> None:
        assert self.session.close().definitively_closed
