"""Tests for audio recording, quality validation, and VAD."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from scipy.io import wavfile

from core.audio_recorder import AudioRecorder
from tests.helpers import load_config


def _make_config(**audio_overrides) -> dict:
    """Make a minimal config with optional audio overrides."""
    base = {
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "default_duration": 3.0,
            "min_duration": 1.0,
            "low_volume_threshold": 0.02,
            "block_duration": 0.1,
            "volume_bar_width": 24,
            "vad_enabled": False,
        }
    }
    base["audio"].update(audio_overrides)
    return base


def test_save_wav_roundtrip(tmp_path: Path) -> None:
    """Saved WAV data should load back with the expected sample rate and content."""

    recorder = AudioRecorder(load_config())
    duration_seconds = 0.5
    sample_count = int(recorder.sample_rate * duration_seconds)
    time_axis = np.arange(sample_count, dtype=np.float32) / recorder.sample_rate
    original_audio = 0.5 * np.sin(2.0 * np.pi * 440.0 * time_axis, dtype=np.float32)

    output_path = tmp_path / "sample.wav"
    recorder.save_wav(original_audio, str(output_path))

    sample_rate, loaded_audio = wavfile.read(output_path)
    reloaded_audio = loaded_audio.astype(np.float32) / np.iinfo(np.int16).max

    assert sample_rate == recorder.sample_rate
    assert loaded_audio.dtype == np.int16
    np.testing.assert_allclose(reloaded_audio, original_audio, atol=5e-5)


@pytest.mark.parametrize(
    ("audio", "expected_ok", "expected_message_fragment"),
    [
        (np.zeros(0, dtype=np.float32), False, "empty"),
        (np.full(3000, 0.5, dtype=np.float32), False, "duration"),
        (np.full(16000, 0.001, dtype=np.float32), False, "volume"),
        (np.full(16000, 0.1, dtype=np.float32), True, "passed"),
    ],
)
def test_is_quality_ok(
    audio: np.ndarray,
    expected_ok: bool,
    expected_message_fragment: str,
) -> None:
    """Quality validation should flag empty, short, and quiet clips."""

    recorder = AudioRecorder(load_config())

    is_ok, message = recorder.is_quality_ok(audio)

    assert is_ok is expected_ok
    assert expected_message_fragment in message


# --- VAD configuration tests ---


class TestVADConfig:
    def test_vad_disabled_by_default(self) -> None:
        recorder = AudioRecorder(_make_config())
        assert recorder.vad_enabled is False
        assert recorder._vad is None


# --- record() with mocked sounddevice ---


class _FakeInputStream:
    """Simulate sounddevice.InputStream by calling the callback with fake audio."""

    def __init__(self, chunks, **kwargs):
        self._callback = kwargs.get("callback")
        self._chunks = chunks
        self._blocksize = kwargs.get("blocksize", 1600)

    def __enter__(self):
        # Feed chunks to callback in a background thread
        def _feed():
            for chunk in self._chunks:
                indata = chunk.reshape(-1, 1)
                try:
                    self._callback(indata, len(chunk), None, None)
                except Exception:
                    break
        threading.Thread(target=_feed, daemon=True).start()
        return self

    def __exit__(self, *args):
        pass


class TestRecord:
    def _setup_fake_sd(self, chunks):
        """Install a fake sounddevice module that yields given chunks."""
        fake_sd = types.ModuleType("sounddevice")
        fake_sd.InputStream = lambda **kw: _FakeInputStream(chunks, **kw)

        class _FakeStop(Exception):
            pass
        fake_sd.CallbackStop = _FakeStop
        return fake_sd

    def test_record_returns_audio(self) -> None:
        # 3 chunks of 1600 samples = 0.3s at 16kHz
        chunks = [np.random.randn(1600).astype(np.float32) * 0.1 for _ in range(50)]
        fake_sd = self._setup_fake_sd(chunks)

        config = _make_config(default_duration=0.5)
        recorder = AudioRecorder(config)

        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            audio = recorder.record(duration=0.5)

        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert len(audio) > 0

    def test_record_invalid_duration_raises(self) -> None:
        config = _make_config()
        recorder = AudioRecorder(config)
        with pytest.raises(ValueError, match="greater than zero"):
            recorder.record(duration=-1)

    def test_record_zero_duration_raises(self) -> None:
        config = _make_config()
        recorder = AudioRecorder(config)
        with pytest.raises(ValueError, match="greater than zero"):
            recorder.record(duration=0)

    def test_record_uses_default_duration(self) -> None:
        chunks = [np.random.randn(1600).astype(np.float32) * 0.1 for _ in range(50)]
        fake_sd = self._setup_fake_sd(chunks)

        config = _make_config(default_duration=0.3)
        recorder = AudioRecorder(config)

        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            audio = recorder.record()  # no duration arg → uses default

        assert len(audio) > 0


# --- Additional utility tests ---


class TestVolumeAndNormalize:
    def test_volume_level_loud(self) -> None:
        recorder = AudioRecorder(_make_config())
        loud = np.full(1600, 0.5, dtype=np.float32)
        assert recorder.get_volume_level(loud) > 0.4

    def test_volume_level_silence(self) -> None:
        recorder = AudioRecorder(_make_config())
        assert recorder.get_volume_level(np.zeros(1600, dtype=np.float32)) == 0.0

    def test_volume_level_empty(self) -> None:
        recorder = AudioRecorder(_make_config())
        assert recorder.get_volume_level(np.array([], dtype=np.float32)) == 0.0

    def test_normalize_int16(self) -> None:
        recorder = AudioRecorder(_make_config())
        pcm = np.array([0, 16384, -16384], dtype=np.int16)
        normalized = recorder._normalize_audio(pcm)
        assert normalized.dtype == np.float32
        assert -1.0 <= normalized.min() <= normalized.max() <= 1.0

    def test_normalize_stereo_to_mono(self) -> None:
        recorder = AudioRecorder(_make_config())
        stereo = np.column_stack([np.ones(100), np.ones(100) * -1]).astype(np.float32)
        mono = recorder._normalize_audio(stereo)
        assert mono.ndim == 1
        assert len(mono) == 100

    def test_save_wav_empty_raises(self) -> None:
        recorder = AudioRecorder(_make_config())
        with pytest.raises(ValueError, match="empty"):
            recorder.save_wav(np.array([], dtype=np.float32), "/tmp/empty.wav")
