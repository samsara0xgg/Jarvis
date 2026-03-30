"""Tests for audio recording persistence and basic quality validation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml
from scipy.io import wavfile

from core.audio_recorder import AudioRecorder


def _load_config() -> dict:
    """Load the project configuration for test fixtures."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def test_save_wav_roundtrip(tmp_path: Path) -> None:
    """Saved WAV data should load back with the expected sample rate and content."""

    recorder = AudioRecorder(_load_config())
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
        (np.full(8000, 0.5, dtype=np.float32), False, "duration"),
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

    recorder = AudioRecorder(_load_config())

    is_ok, message = recorder.is_quality_ok(audio)

    assert is_ok is expected_ok
    assert expected_message_fragment in message
