"""Jarvis talking in conversation mode while Allen makes one sound, and how long the sounds are."""

from __future__ import annotations

import contextlib
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_pipeline, voice_session
from tests.support.voice_input import (
    EnergySilero,
    FakeBackend,
    FakeWakeEngine,
    make_ingress,
    wait_until_true,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.state.voiceprint import VoiceprintBook
    from jarvis.surface.voice_cues import VoiceCues
    from jarvis.surface.voice_print import VoiceprintExtractor

VOICE, ROOM = 8_000, 30  # constant frames at about -12 and -60 dBFS


# With the shipped profile (5-frame smoothing, 3 hits) speech starts on the
# fifth voice frame and the last two voice frames' smoothing counts too, so a
# burst of N frames holds N - 2 voiced frames: 6 is a short sound, 25 is not.
SHORT, LONG = 6, 25


class _ScriptedAsr:
    """Final ASR that hears the next scripted transcript."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.heard: list[bytes] = []
        self.tags: dict[str, tuple[str | None, str | None]] = {}  # text -> (emotion, event)

    def recognize(self, audio_bytes: bytes) -> voice_asr.TranscriptionResult:
        self.heard.append(audio_bytes)
        text = self._texts.pop(0)
        emotion, event = self.tags.get(text, (None, None))
        return voice_asr.TranscriptionResult(
            text=text,
            confidence=0.9,
            language_detected=None,
            emotion=emotion,
            event=event,
        )


class BargeInRig:
    """Jarvis talking in conversation mode while Allen makes one sound."""

    def __init__(  # noqa: PLR0913 - each switch a test flips
        self,
        tmp_path: Path,
        heard: str,
        *,
        speaking: bool = True,
        confirm_voiced_s: float = 0.4,
        idle_exit_s: float = 10.0,
        wait_s: float = 60.0,
        recent_speech: Callable[[], str] | None = None,
        ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None,
        note_words: Callable[[str, str, str, bool, bool], None] | None = None,
        begin_line: Callable[[str, str, str, bool, bool], None] | None = None,
        backend: FakeBackend | None = None,
        wake_input_channel: int | None = None,
        output_active: Callable[[], bool] | None = None,
        stop_speaking: Callable[[], object] | None = None,
        answer_words: Callable[[str, str, str], None] | None = None,
        cancel_voice_runs: Callable[[], None] | None = None,
        cues: VoiceCues | None = None,
        heard_elsewhere: Callable[[str, str], bool] | None = None,
        emit: Callable[[Mapping[str, object], Mapping[str, str]], Event] | None = None,
        judged_on_send: bool = False,
        supersede_unspoken: Callable[[str], None] | None = None,
        voiceprints: VoiceprintBook | None = None,
        embedder: VoiceprintExtractor | None = None,
    ) -> None:
        self.output: list[str] = []
        self.phases: list[tuple[str, object]] = []
        self.conversation = True
        self.conversation_changes: list[tuple[bool, str]] = []
        self.answers: list[str] = []
        self.speaking = speaking
        self.db = tmp_path / "events.db"
        open_event_log(self.db).close()
        self.backend = backend or FakeBackend()
        self.ingress = make_ingress(self.backend, wake_input_channel=wake_input_channel)
        self.asr = _ScriptedAsr([heard])
        pipeline = voice_pipeline.VoicePipeline(
            conn_factory=None if emit else (lambda: open_event_log(self.db)),
            emit=emit,
            recognizer=self.asr,
            normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
            broadcaster=self,
            artifacts_dir=None,
            cues=cues,
            voiceprints=embedder,
        )
        with patch.object(voice_audio, "_load_silero_session", return_value=EnergySilero()):
            self.session = voice_session.DuplexVoiceSession(
                ingress=self.ingress,
                wake_engine=FakeWakeEngine(detections=set()),
                vad=voice_audio.SileroVad(mode="record"),
                pipeline=pipeline,
                broadcaster=self,
                output_active=output_active or (lambda: self.speaking),
                wake_threshold=0.5,
                config=replace(
                    voice_session.RealtimeInputSessionConfig(),
                    min_voiced_s=0.3,
                    pre_roll_ms=64,
                    worker_poll_s=0.001,
                    shutdown_timeout_s=1.0,
                    barge_in_confirm_voiced_s=confirm_voiced_s,
                    conversation_idle_exit_s=idle_exit_s,
                    conversation_wait_s=wait_s,
                ),
                mic_muted=lambda: False,
                conversation=lambda: self.conversation,
                set_conversation=self._set_conversation,
                answer_words=answer_words
                or (lambda _turn_id, reason, _text: self.answers.append(reason)),
                stop_speaking=stop_speaking or self._stop,
                supersede_unspoken=supersede_unspoken
                or (lambda _turn_id: self.output.append("supersede")),
                cancel_voice_runs=cancel_voice_runs or (lambda: self.output.append("cancel runs")),
                yield_speaking=lambda gain: self.output.append(f"gain {gain}"),
                pause_speaking=lambda paused: self.output.append("pause" if paused else "go on"),
                recent_speech=recent_speech,
                ask_words=ask_words,
                note_words=note_words,
                begin_line=begin_line,
                cues=cues,
                heard_elsewhere=heard_elsewhere,
                voiceprints=voiceprints,
                judged_on_send=judged_on_send,
            )
            assert self.session.start().started

    def _set_conversation(self, on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
        self.conversation_changes.append((on, reason))
        self.conversation = on

    def _stop(self) -> None:
        self.output.append("stop")
        self.speaking = False

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        del turn_id
        self.phases.append((phase, payload.get("reason")))

    def say(self, voice_frames: int, *, judged: str = "phase") -> None:
        """Room, then ``voice_frames`` of voice, then room until his words are judged.

        Judged is the surface's ``accepted`` or ``empty``; a wake phrase alone
        gets neither (the session listens on), so ``judged="gain"`` waits for
        her gain to come back instead.
        """
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        for value in [ROOM] * 8 + [VOICE] * voice_frames + [ROOM] * 60:
            self.backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        if judged == "gain":
            wait_until_true(lambda: self.output[-1:] == ["gain 1.0"])
        else:
            wait_until_true(lambda: any(phase in {"accepted", "empty"} for phase, _ in self.phases))

    def room(self, seconds: float) -> None:
        """Nobody talking for ``seconds``."""
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.backend.emit(epoch=epoch, value=ROOM)
            time.sleep(0.002)

    def room_until(self, done: Callable[[], object]) -> None:
        """Nobody talking, a frame at a time, until ``done`` holds."""
        epoch = self.ingress.stream_epoch
        assert epoch is not None

        def next_frame() -> bool:
            self.backend.emit(epoch=epoch, value=ROOM)
            return bool(done())

        wait_until_true(next_frame)

    def turns(self) -> list[str]:
        with contextlib.closing(open_event_log(self.db)) as conn:
            return [
                str(row[0])
                for row in conn.execute(
                    "SELECT json_extract(payload_json, '$.transcript') FROM events "
                    "WHERE type = 'utterance.received'",
                )
            ]

    def close(self) -> None:
        assert self.session.close().definitively_closed
