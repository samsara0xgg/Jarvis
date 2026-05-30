"""ADR-0005 voice_audio — Silero VAD state machine."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

from jarvis.surface import voice_audio


def _fake_silero_session(prob_for_call: list[float]) -> MagicMock:
    """Return a MagicMock that emulates the Silero ONNX session.

    Each call to .run() returns the next probability from `prob_for_call`.
    """
    session = MagicMock()
    call_counter = {"i": 0}

    def _run(*args: object, **kwargs: object) -> list[Any]:  # noqa: ARG001
        i = call_counter["i"]
        call_counter["i"] += 1
        prob = prob_for_call[min(i, len(prob_for_call) - 1)]
        # Silero ONNX output shape: [[prob]], plus updated state tensors.
        return [np.array([[prob]], dtype=np.float32), MagicMock(), MagicMock()]

    session.run.side_effect = _run
    return session


def test_vad_record_mode_yields_speech_then_silence() -> None:
    """A run of high-prob frames followed by low-prob frames flips to silence."""
    silent_frame = np.zeros(512, dtype=np.int16).tobytes()
    speech_frame = (np.ones(512, dtype=np.int16) * 8000).tobytes()
    fake_session = _fake_silero_session([0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    with patch.object(voice_audio, "_load_silero_session", return_value=fake_session):
        vad = voice_audio.SileroVad(mode="record")
        for _ in range(3):
            assert vad.feed(speech_frame) is voice_audio.VadEvent.SPEECH_ACTIVE
        for _ in range(3):
            vad.feed(silent_frame)
        # After at least one silence frame post-speech, vad.empty() flips True.
        assert vad.empty() is True


def test_vad_chunk_size_is_512_samples() -> None:
    """The chunk size constant matches the legacy fixed-size silero contract."""
    assert voice_audio.SILERO_CHUNK_SAMPLES == 512


def test_vad_record_mode_uses_lower_prob_threshold_than_tts_mode() -> None:
    """Record mode prob threshold (0.4) < TTS mode (0.5) per legacy vad_silero.py."""
    rec = voice_audio.SileroVad.thresholds(mode="record")
    tts = voice_audio.SileroVad.thresholds(mode="tts")
    assert rec.prob_threshold < tts.prob_threshold
