"""ADR 0041: wave mode listens without a wake word and stops Jarvis when Allen talks.

The duplex session runs on the scripted ingress of the Wave 3 tests with a wake
engine that never fires, so every commit here is one no wake hit armed. The
controls checks drive the real ``/inherent/controls`` and ``/inherent/ws``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jarvis.surface import voice_audio, voice_session
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_wave3_single_audio_ingress import (
    _EnergySession,
    _FakeBackend,
    _FakeWakeEngine,
    _ingress,
    _RecordingPipeline,
    _wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SPEECH = [0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]


class _Session:
    """One duplex session whose switches the test flips while it runs."""

    def __init__(  # noqa: PLR0913 - each switch a test flips
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        speaking: bool = False,
        pipeline: _RecordingPipeline | None = None,
        hold_output: Callable[[bool], None] | None = None,
        supersede_unspoken: Callable[[str], None] | None = None,
        conversation: bool = True,
        wake: bool = False,
        idle_exit_s: float = 30.0,
        turn_working: Callable[[], bool] | None = None,
        set_quiet: Callable[[str], None] | None = None,
        answer_words: Callable[[str, str, str], None] | None = None,
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
        self.backend = _FakeBackend()
        self.ingress = _ingress(self.backend)
        self.wake_engine = _FakeWakeEngine(detections=None if wake else set())
        self.pipeline = pipeline or _RecordingPipeline()
        with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
            self.session = voice_session.DuplexVoiceSession(
                ingress=self.ingress,
                wake_engine=self.wake_engine,
                vad=voice_audio.SileroVad(mode="record"),
                pipeline=self.pipeline,
                broadcaster=None,
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


def test_wave_mode_commits_speech_with_no_wake_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sentences, no wake word: both reach ASR as their own utterances."""
    rig = _Session(monkeypatch)
    rig.speak()
    _wait_until(lambda: len(rig.pipeline.calls) == 1)
    rig.speak()
    _wait_until(lambda: len(rig.pipeline.calls) == 2)
    assert rig.session.metrics().wake_detections == 0
    assert rig.pipeline.calls[0]["utterance_id"] != rig.pipeline.calls[1]["utterance_id"]
    # No wake word opened them, so a "Hey Jarvis" in them is a greeting, kept.
    assert [call["wake_lead"] for call in rig.pipeline.calls] == [False, False]
    assert not rig.stopped.is_set()
    rig.close()


def test_speech_over_jarvis_stops_it_and_is_still_heard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allen talks while Jarvis speaks: the stop fires at onset, the words commit."""
    rig = _Session(monkeypatch, speaking=True)
    rig.speak()
    _wait_until(rig.stopped.is_set)
    _wait_until(lambda: len(rig.pipeline.calls) == 1)
    rig.close()


@pytest.mark.parametrize("switch", ["conversation", "muted"])
def test_leaving_wave_mode_or_muting_needs_the_wake_word_again(
    monkeypatch: pytest.MonkeyPatch, switch: str,
) -> None:
    """An arm that heard nothing yet is dropped, so later speech commits nothing."""
    rig = _Session(monkeypatch)
    epoch = rig.ingress.stream_epoch
    assert epoch is not None
    for _ in range(4):  # idle frames: the conversation arm is in place
        rig.backend.emit(epoch=epoch, value=0)
        time.sleep(0.002)
    if switch == "conversation":
        rig.conversation = False
    else:
        rig.muted = True
    rig.speak()
    time.sleep(0.2)
    assert rig.pipeline.calls == []
    # Positive control: back in wave mode, the same speech commits.
    rig.conversation, rig.muted = True, False
    rig.speak()
    _wait_until(lambda: len(rig.pipeline.calls) == 1)
    rig.close()


def test_the_surface_sets_the_switch_and_its_last_disconnect_clears_it() -> None:
    """Resonance drives the switch; a daemon with no surface left stops listening."""
    controls = VoiceControls()
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            controls=controls,
        ),
    )
    with TestClient(app) as client:
        with client.websocket_connect("/inherent/ws"):
            state = client.post("/inherent/controls", json={"conversation": True}).json()
            assert state["conversation"] is True
            assert client.post("/inherent/controls", json={}).json()["conversation"] is True
        _wait_until(lambda: not controls.conversation)
        assert client.post("/inherent/controls", json={}).json()["conversation"] is False


def test_the_wake_word_opens_conversation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0102: a wake hit flips the switch as a tap would; later speech needs no wake."""
    rig = _Session(monkeypatch, conversation=False, wake=True)
    rig.idle(0.1)
    _wait_until(lambda: rig.changes == [(True, "wake")])
    rig.wake_engine._detections = set()  # noqa: SLF001 - no more wake hits from here
    rig.speak()
    _wait_until(lambda: len(rig.pipeline.calls) >= 1)
    rig.close()
    assert rig.changes == [(True, "wake")]


def test_quiet_ends_conversation_mode_but_her_speech_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0102: nobody talking and Jarvis silent ends it; while she talks it stays."""
    rig = _Session(monkeypatch, speaking=True, idle_exit_s=0.05)
    rig.idle(0.2)
    assert rig.changes == []
    rig.speaking = False
    rig.idle(0.2)
    rig.close()
    assert rig.changes == [(False, "idle")]


def test_a_turn_still_working_keeps_conversation_mode_open_past_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0102: a turn working past the window ends nothing; quiet after it does."""
    working = [True]
    calls = [0]

    def turn_working() -> bool:
        calls[0] += 1
        return working[0]

    rig = _Session(monkeypatch, idle_exit_s=0.05, turn_working=turn_working)
    try:
        rig.idle(0.2)
        assert rig.changes == []
        assert 1 <= calls[0] <= 6  # asked when the window runs out, not on every frame
        working[0] = False
        rig.idle(0.2)
        assert rig.changes == [(False, "idle")]
    finally:
        rig.close()
