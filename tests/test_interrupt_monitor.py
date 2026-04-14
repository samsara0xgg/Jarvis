"""Tests for InterruptMonitor — streaming ASR keyword detection."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.interrupt_monitor import (
    InterruptMonitor,
    INTERRUPT_KEYWORDS,
    RESUME_KEYWORDS,
    strip_interrupt_prefix,
)


class TestInterruptMonitorKeywordMatch:
    def test_detects_interrupt_keyword_in_partial(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        assert len(detected) == 1

    def test_ignores_non_keyword(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("明天天气")
        assert len(detected) == 0

    def test_detects_keyword_as_substring(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停改成多伦多")
        assert len(detected) == 1

    def test_detects_resume_keyword(self):
        resume_detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_resume=lambda: resume_detected.append("resume"),
        )
        monitor._check_partial("继续说")
        assert len(resume_detected) == 1

    def test_fires_only_once_per_session(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        monitor._check_partial("停停停")
        assert len(detected) == 1

    def test_reset_allows_new_detection(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        assert len(detected) == 1
        monitor.reset()
        monitor._check_partial("停")
        assert len(detected) == 2


class TestInterruptMonitorAudio:
    def test_feed_audio_accepts_float32_array(self):
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        audio = np.zeros(1600, dtype=np.float32)
        monitor.feed_audio(audio, sample_rate=16000)

    def test_disabled_monitor_does_nothing(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": False}},
            on_interrupt=lambda: detected.append("x"),
        )
        monitor._check_partial("停")
        assert len(detected) == 0


class TestMicListener:
    def test_start_stop_mic_listener(self):
        """Mic listener should start/stop without errors (mocked sounddevice)."""
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        mock_stream = MagicMock()
        mock_stream.read.return_value = (np.zeros((1600, 1), dtype="float32"), None)
        with patch("sounddevice.InputStream", return_value=mock_stream):
            monitor.start()
            monitor.start_mic_listener()
            time.sleep(0.2)
            monitor.stop_mic_listener()
            monitor.stop()
            mock_stream.start.assert_called_once()
            mock_stream.stop.assert_called_once()


class TestInterruptMonitorVADGate:
    def _make_config(self, **overrides):
        base = {
            "interrupt": {
                "enabled": True,
                "vad_model_path": "data/silero_vad.onnx",
                "vad_threshold_during_tts": 0.8,
                "vad_min_speech_duration": 0.15,
                "vad_min_silence_duration": 0.2,
                "vad_max_speech_duration": 10.0,
            }
        }
        base["interrupt"].update(overrides)
        return base

    def test_vad_loaded_when_enabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig") as mock_cfg_cls:
            mock_cfg = MagicMock()
            mock_cfg.silero_vad = MagicMock()
            mock_cfg_cls.return_value = mock_cfg

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            monitor.start()

            assert mock_vad_cls.called
            assert mock_cfg.silero_vad.model == "data/silero_vad.onnx"
            assert mock_cfg.silero_vad.threshold == 0.8
            assert mock_cfg.silero_vad.min_speech_duration == 0.15
            assert mock_cfg.silero_vad.min_silence_duration == 0.2

    def test_feed_audio_skips_asr_when_not_speech(self):
        """When VAD says no speech, streaming ASR stream should not receive audio."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad.is_speech_detected.return_value = False
            mock_vad_cls.return_value = mock_vad

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            mock_stream = MagicMock()
            monitor._stream = mock_stream
            monitor._recognizer = MagicMock()
            monitor._recording = True
            monitor._vad = mock_vad

            audio = np.zeros(1600, dtype=np.float32)
            monitor.feed_audio(audio)

            mock_vad.accept_waveform.assert_called()
            mock_stream.accept_waveform.assert_not_called()

    def test_feed_audio_passes_to_asr_when_speech_detected(self):
        """When VAD says speech active, audio is forwarded to streaming ASR."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad.is_speech_detected.return_value = True
            mock_vad_cls.return_value = mock_vad

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            mock_stream = MagicMock()
            mock_recognizer = MagicMock()
            mock_recognizer.is_ready.return_value = False
            mock_recognizer.get_result.return_value = MagicMock(text="")
            monitor._stream = mock_stream
            monitor._recognizer = mock_recognizer
            monitor._recording = True
            monitor._vad = mock_vad

            audio = np.zeros(1600, dtype=np.float32)
            monitor.feed_audio(audio)

            mock_stream.accept_waveform.assert_called_once()
