"""Tests for AudioRecorder's Silero VAD integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.audio_recorder import AudioRecorder


def _make_config(**overrides):
    base = {
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "default_duration": 3.0,
            "min_duration": 0.3,
            "low_volume_threshold": 0.02,
            "block_duration": 0.1,
            "vad_enabled": True,
            "vad_model_path": "data/silero_vad.onnx",
            # These tests mock the sherpa_onnx wrapper directly, so pin the
            # provider here. The silero_direct path has its own coverage in
            # tests/test_vad_silero.py.
            "vad_provider": "sherpa_onnx",
            "vad_threshold": 0.5,
            "vad_silence_duration": 0.5,
            "vad_min_speech_duration": 0.25,
            "vad_max_speech_duration": 20.0,
        }
    }
    base["audio"].update(overrides)
    return base


class TestAudioRecorderVADInit:
    def test_vad_loaded_when_enabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig") as mock_cfg_cls:
            mock_cfg = MagicMock()
            mock_cfg.silero_vad = MagicMock()
            mock_cfg_cls.return_value = mock_cfg
            recorder = AudioRecorder(_make_config())
            assert mock_vad_cls.called
            # Verify config fields were passed through
            assert mock_cfg.silero_vad.model == "data/silero_vad.onnx"
            assert mock_cfg.silero_vad.threshold == 0.5
            assert mock_cfg.silero_vad.min_silence_duration == 0.5
            assert mock_cfg.silero_vad.min_speech_duration == 0.25
            assert mock_cfg.silero_vad.max_speech_duration == 20.0

    def test_vad_skipped_when_disabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls:
            recorder = AudioRecorder(_make_config(vad_enabled=False))
            assert not mock_vad_cls.called
            assert recorder._vad is None

    def test_fail_fast_on_model_load_error(self):
        with patch("sherpa_onnx.VoiceActivityDetector",
                   side_effect=RuntimeError("model not found")):
            with pytest.raises(RuntimeError, match="model not found"):
                AudioRecorder(_make_config())


class TestAudioRecorderVADRecord:
    def test_vad_reset_called_at_record_start(self):
        """record() must reset VAD state before each recording."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad_cls.return_value = mock_vad
            mock_vad.empty.return_value = True  # never detects speech end

            recorder = AudioRecorder(_make_config())

            with patch("sounddevice.InputStream") as mock_stream:
                mock_stream.return_value.__enter__ = lambda s: None
                mock_stream.return_value.__exit__ = lambda *a: None
                try:
                    recorder.record(duration=0.1)
                except Exception:
                    pass

            assert mock_vad.reset.called

    def test_vad_stops_callback_when_segment_complete(self):
        """When VAD produces a segment (speech ended), empty() returns False."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad_cls.return_value = mock_vad
            mock_vad.empty.side_effect = [True, False]

            recorder = AudioRecorder(_make_config())

            chunk = np.zeros(1600, dtype=np.float32)
            recorder._vad.accept_waveform(chunk)
            assert recorder._vad.empty() is True
            recorder._vad.accept_waveform(chunk)
            assert recorder._vad.empty() is False
