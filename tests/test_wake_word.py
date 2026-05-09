"""Tests for the openwakeword-based wake word detector."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core.wake_word import WakeWordDetector
from core.inherent_wake_listener import InherentWakeListener, inherent_wake_enabled


def _make_config():
    return {
        "wake_word": {
            "keyword": "jarvis",
            "sensitivity": 0.5,
        }
    }


def test_start_and_stop():
    detector = WakeWordDetector(_make_config())
    detector.start()
    assert detector._model is not None
    assert detector.frame_length == 1280
    assert detector.sample_rate == 16000
    detector.stop()
    assert detector._model is None


def test_process_frame_no_detection():
    detector = WakeWordDetector(_make_config())
    detector.start()
    # Silence should not trigger
    assert detector.process_frame([0] * 1280) is False
    detector.stop()


def test_process_frame_raises_if_not_started():
    detector = WakeWordDetector(_make_config())
    with pytest.raises(RuntimeError, match="not been started"):
        detector.process_frame([0] * 1280)


def test_vad_threshold_defaults_to_zero():
    detector = WakeWordDetector(_make_config())
    assert detector.vad_threshold == 0.0


def test_vad_threshold_read_from_config():
    config = {
        "wake_word": {
            "keyword": "jarvis",
            "sensitivity": 0.5,
            "vad_threshold": 0.4,
        }
    }
    detector = WakeWordDetector(config)
    assert detector.vad_threshold == 0.4


def test_inherent_wake_enabled_follows_wake_config():
    assert inherent_wake_enabled({"wake_word": {"enabled": True}}) is True
    assert inherent_wake_enabled({"wake_word": {"enabled": True, "inherent_enabled": False}}) is False
    assert inherent_wake_enabled({"wake_word": {"enabled": False}}) is False
    assert inherent_wake_enabled({}) is False


def test_inherent_wake_submit_does_not_block_listener():
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, str]] = []

    class FakeRecorder:
        def record(self, duration):
            return np.zeros(16000, dtype=np.float32)

    class FakeRecognizer:
        def transcribe(self, audio):
            return SimpleNamespace(text="在吗", language="zh", emotion="neutral")

    class FakeDucker:
        def __init__(self):
            self.calls = []

        def duck(self):
            self.calls.append("duck")
            return True

        def restore(self):
            self.calls.append("restore")

        def restore_all(self):
            self.calls.append("restore_all")

    class FakeJarvis:
        config = {"session": {"utterance_duration": 5}}
        audio_recorder = FakeRecorder()
        speech_recognizer = FakeRecognizer()

        def handle_text(self, text, session_id):
            calls.append((text, session_id))
            started.set()
            release.wait(timeout=2)

    events = []
    listener = InherentWakeListener(FakeJarvis(), lambda phase, payload: events.append(phase))
    ducker = FakeDucker()
    listener._audio_ducker = ducker
    t0 = time.monotonic()
    try:
        listener._capture_and_submit_one_turn()
        assert time.monotonic() - t0 < 0.5
        assert started.wait(timeout=1)
        assert calls == [("在吗", "_inherent")]
        assert events == ["transcribing", "accepted"]
        assert ducker.calls == ["duck", "restore"]
    finally:
        release.set()
        listener.stop()
