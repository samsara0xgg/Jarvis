"""Tests for the wake word detector with mocked Porcupine."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from core.wake_word import WakeWordDetector


def _make_config(access_key="test-key"):
    return {
        "wake_word": {
            "picovoice_access_key": access_key,
            "keyword": "jarvis",
            "sensitivity": 0.5,
        }
    }


def _install_fake_porcupine():
    """Install a fake pvporcupine module."""
    fake_module = types.ModuleType("pvporcupine")

    class FakePorcupine:
        frame_length = 512
        sample_rate = 16000
        _detect_next = False

        def process(self, pcm):
            return 0 if self._detect_next else -1

        def delete(self):
            pass

    def create(access_key=None, keywords=None, sensitivities=None):
        return FakePorcupine()

    fake_module.create = create
    sys.modules["pvporcupine"] = fake_module
    return FakePorcupine


def test_start_and_stop():
    FakePorcupine = _install_fake_porcupine()
    try:
        detector = WakeWordDetector(_make_config())
        detector.start()
        assert detector.frame_length == 512
        assert detector.sample_rate == 16000
        detector.stop()
    finally:
        sys.modules.pop("pvporcupine", None)


def test_process_frame_detects_wake_word():
    FakePorcupine = _install_fake_porcupine()
    try:
        detector = WakeWordDetector(_make_config())
        detector.start()

        # No detection
        assert detector.process_frame([0] * 512) is False

        # Simulate detection
        detector._porcupine._detect_next = True
        assert detector.process_frame([0] * 512) is True

        detector.stop()
    finally:
        sys.modules.pop("pvporcupine", None)


def test_process_frame_raises_if_not_started():
    detector = WakeWordDetector(_make_config())
    with pytest.raises(RuntimeError, match="not been started"):
        detector.process_frame([0] * 512)


def test_start_raises_without_access_key():
    _install_fake_porcupine()
    try:
        detector = WakeWordDetector(_make_config(access_key=""))
        with pytest.raises(RuntimeError, match="access key"):
            detector.start()
    finally:
        sys.modules.pop("pvporcupine", None)
