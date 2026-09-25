"""ADR 0041: wave mode listens without a wake word and stops Jarvis when Allen talks.

The duplex session runs on the scripted ingress of the Wave 3 tests with a wake
engine that never fires, so every commit here is one no wake hit armed. The
controls checks drive the real ``/inherent/controls`` and ``/inherent/ws``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
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

_SPEECH = [0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]


class _Session:
    """One duplex session whose switches the test flips while it runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, speaking: bool = False) -> None:
        monkeypatch.setitem(
            voice_audio._MODE_THRESHOLDS,  # noqa: SLF001 - loosened for one-frame onsets
            "record",
            voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
        )
        self.conversation = True
        self.muted = False
        self.speaking = speaking
        self.stopped = threading.Event()
        self.backend = _FakeBackend()
        self.ingress = _ingress(self.backend)
        self.wake_engine = _FakeWakeEngine(detections=set())
        self.pipeline = _RecordingPipeline()
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
                ),
                mic_muted=lambda: self.muted,
                conversation=lambda: self.conversation,
                stop_speaking=self.stopped.set,
            )
            assert self.session.start().started

    def speak(self) -> None:
        for value in _SPEECH:
            self.backend.emit(epoch=self.ingress.stream_epoch, value=value)
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
    for _ in range(4):  # idle frames: the conversation arm is in place
        rig.backend.emit(epoch=rig.ingress.stream_epoch, value=0)
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
