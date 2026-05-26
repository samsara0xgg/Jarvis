"""ADR-0005 voice_audio.capture_utterance — VAD-gated PortAudio recorder."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.surface import voice_audio


@pytest.fixture
def vad_yielding_speech_then_silence() -> MagicMock:
    """SileroVad stub that says speech for first 30 frames then silence."""
    vad = MagicMock(spec=voice_audio.SileroVad)
    call = {"i": 0}

    def _feed(_frame: bytes) -> voice_audio.VadEvent:
        call["i"] += 1
        return (
            voice_audio.VadEvent.SPEECH_ACTIVE
            if call["i"] <= 30
            else voice_audio.VadEvent.SILENCE
        )

    vad.feed.side_effect = _feed
    vad.empty.side_effect = lambda: call["i"] > 35  # flips after ~5 silence frames
    return vad


def test_capture_utterance_returns_audio_when_vad_completes(
    vad_yielding_speech_then_silence: MagicMock,
) -> None:
    """A VAD-cut recording returns the accumulated PCM bytes."""
    fake_stream = MagicMock()
    # Make context-manager protocol work (sd.RawInputStream is used with `with`).
    fake_stream.__enter__.return_value = fake_stream
    fake_stream.__exit__.return_value = False
    fake_stream.read.return_value = (
        np.zeros(voice_audio.SILERO_CHUNK_SAMPLES, dtype=np.int16).tobytes(),
        False,
    )
    with patch.object(voice_audio, "_open_input_stream", return_value=fake_stream):
        audio = voice_audio.capture_utterance(
            vad=vad_yielding_speech_then_silence,
            max_duration_s=5.0,
            min_voiced_s=1.0,
            sample_rate_hz=16000,
        )
    # Each frame is 512 samples = 1024 bytes; ~35 frames captured.
    assert isinstance(audio, bytes)
    assert len(audio) >= 30 * voice_audio.SILERO_CHUNK_SAMPLES * 2


def test_capture_utterance_respects_max_duration_cap() -> None:
    """If VAD never says silence, capture stops at max_duration_s."""
    vad = MagicMock(spec=voice_audio.SileroVad)
    vad.feed.return_value = voice_audio.VadEvent.SPEECH_ACTIVE
    vad.empty.return_value = False

    fake_stream = MagicMock()
    fake_stream.__enter__.return_value = fake_stream
    fake_stream.__exit__.return_value = False
    fake_stream.read.return_value = (
        np.zeros(voice_audio.SILERO_CHUNK_SAMPLES, dtype=np.int16).tobytes(),
        False,
    )
    with patch.object(voice_audio, "_open_input_stream", return_value=fake_stream):
        audio = voice_audio.capture_utterance(
            vad=vad,
            max_duration_s=1.0,
            min_voiced_s=0.5,
            sample_rate_hz=16000,
        )
    # ~1 second at 16 kHz mono PCM16 ≈ 32000 bytes (allow ±15% slack).
    assert 27000 <= len(audio) <= 37000
