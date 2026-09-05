"""Hermetic check for the ADR-0006 D9 AEC spike analysis script.

The live acoustic run is the spike's real exercise; this pins the arithmetic
underneath it on WAVs the test writes itself, so a broken residual formula or
a broken IDLE->ACTIVE edge count fails without hardware. The Silero session is
the same energy-following stub the other endpointing tests use, which makes
the crossing count a property of the shipped state machine rather than of the
ONNX model.
"""

from __future__ import annotations

import json
import wave
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.surface import voice_audio
from scripts import spike_voiceprocessingio_aec as spike

if TYPE_CHECKING:
    from pathlib import Path

SAMPLE_RATE_HZ = 16000
FRAME = voice_audio.SILERO_CHUNK_SAMPLES


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def run(self, _output_names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


def _tone(frames: int, dbfs: float) -> np.ndarray:
    """Return ``frames`` Silero frames of a 440 Hz sine at ``dbfs`` RMS."""
    samples = frames * FRAME
    amplitude = (10.0 ** (dbfs / 20.0)) * np.sqrt(2.0)
    t = np.arange(samples, dtype=np.float64) / SAMPLE_RATE_HZ
    wave_int16: np.ndarray = np.round(
        amplitude * np.sin(2 * np.pi * 440.0 * t) * 32767.0
    ).astype(np.int16)
    return wave_int16


def _write_capture(
    path: Path,
    *,
    pcm: np.ndarray,
    silence: tuple[int, int],
    playback: tuple[int, int],
    facts: dict[str, object],
) -> None:
    """Write a 16 kHz mono PCM16 WAV plus the sidecar the Swift host writes."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(pcm.tobytes())
    sidecar = {
        "windows": {
            "silence_start": silence[0],
            "silence_end": silence[1],
            "play_start": playback[0],
            "play_end": playback[1],
        },
        "format_facts": facts,
    }
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(sidecar), encoding="utf-8")


def _two_burst_capture() -> tuple[np.ndarray, tuple[int, int], tuple[int, int]]:
    """Silence at -60 dBFS, then two -20 dBFS bursts split by a long gap."""
    silence = _tone(60, -60.0)
    burst = _tone(30, -20.0)
    gap = _tone(40, -60.0)  # > required_misses (24) frames, so ACTIVE drops out
    playback = np.concatenate([burst, gap, burst])
    pcm = np.concatenate([silence, playback])
    return (
        pcm,
        (0, silence.size),
        (silence.size, silence.size + playback.size),
    )


def test_residual_echo_is_playback_minus_silence(tmp_path: Path) -> None:
    """An echo-like -20 dBFS playback window over -60 dBFS silence reads +40 dB."""
    silence = _tone(60, -60.0)
    echo = _tone(60, -20.0)
    pcm = np.concatenate([silence, echo])
    wav = tmp_path / "capture-aec-off.wav"
    _write_capture(
        wav,
        pcm=pcm,
        silence=(0, silence.size),
        playback=(silence.size, pcm.size),
        facts={},
    )

    capture = spike.load_capture("aec-off", wav)

    assert spike.rms_dbfs(capture.pcm[: silence.size]) == pytest.approx(-60.0, abs=0.5)
    assert spike.rms_dbfs(capture.pcm[silence.size :]) == pytest.approx(-20.0, abs=0.5)
    assert spike.residual_echo_db(capture) == pytest.approx(40.0, abs=0.5)


def test_vad_crossings_count_two_bursts_on_both_profiles(tmp_path: Path) -> None:
    """Two -20 dBFS bursts split by a 40-frame gap are two IDLE->ACTIVE edges."""
    pcm, silence, playback = _two_burst_capture()
    wav = tmp_path / "capture-aec-on.wav"
    _write_capture(
        wav,
        pcm=pcm,
        silence=silence,
        playback=playback,
        facts={"int16_16k_mono_obtainable": True},
    )

    capture = spike.load_capture("aec-on", wav)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        results = spike.profile_results(capture, model_path=None)

    assert [r.profile for r in results] == ["record", "tts"]
    assert [r.crossings for r in results] == [2, 2]
    for result in results:
        assert result.window_seconds == pytest.approx(100 * FRAME / SAMPLE_RATE_HZ)
        assert result.per_minute == pytest.approx(2 * 60.0 / result.window_seconds)
    assert capture.facts == {"int16_16k_mono_obtainable": True}


def test_quiet_playback_clears_no_gate(tmp_path: Path) -> None:
    """A -60 dBFS playback window trips neither profile's dB gate."""
    silence = _tone(30, -60.0)
    quiet = _tone(60, -60.0)
    pcm = np.concatenate([silence, quiet])
    wav = tmp_path / "capture-quiet.wav"
    _write_capture(
        wav,
        pcm=pcm,
        silence=(0, silence.size),
        playback=(silence.size, pcm.size),
        facts={},
    )

    capture = spike.load_capture("quiet", wav)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        results = spike.profile_results(capture, model_path=None)

    assert [r.crossings for r in results] == [0, 0]
