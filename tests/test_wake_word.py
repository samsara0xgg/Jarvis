"""Tests for the openwakeword-based wake word detector."""

from __future__ import annotations

import pytest

from core.wake_word import WakeWordDetector


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
