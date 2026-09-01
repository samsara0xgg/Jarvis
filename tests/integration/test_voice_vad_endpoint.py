"""Wave-0A integration regressions for reusable Silero endpoint state."""

from __future__ import annotations

from typing import Self
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.surface import voice_audio


class _EnergySession:
    """Minimal ONNX-shaped session whose probability follows frame energy."""

    def __init__(self) -> None:
        self.run_count = 0

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.run_count += 1
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


class _FrameStream:
    """Blocking-stream shape backed by deterministic PCM frames."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = iter(frames)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, blocksize: int) -> tuple[bytes, bool]:
        assert blocksize == voice_audio.SILERO_CHUNK_SAMPLES
        return next(self._frames), False


@pytest.mark.parametrize(
    ("speech_frames", "required_misses"),
    [
        pytest.param(4, 3, id="short-utterance-three-consecutive-misses"),
        pytest.param(9, 5, id="longer-utterance-five-consecutive-misses"),
    ],
)
def test_reused_vad_resets_prewarms_and_waits_for_consecutive_silence(
    monkeypatch: pytest.MonkeyPatch,
    speech_frames: int,
    required_misses: int,
) -> None:
    """Two captures on one VAD must have identical utterance-local endpoints."""
    reset_realtime_trace()
    thresholds = voice_audio.VadThresholds(
        prob_threshold=0.4,
        db_threshold=-45.0,
        smoothing_window=1,
        required_hits=1,
        required_misses=required_misses,
    )
    monkeypatch.setitem(voice_audio._MODE_THRESHOLDS, "record", thresholds)  # noqa: SLF001

    speech = np.full(voice_audio.SILERO_CHUNK_SAMPLES, 10_000, dtype=np.int16).tobytes()
    silence = np.zeros(voice_audio.SILERO_CHUNK_SAMPLES, dtype=np.int16).tobytes()
    utterance_frames = [speech] * speech_frames + [silence] * required_misses
    streams = [_FrameStream(utterance_frames.copy()), _FrameStream(utterance_frames.copy())]
    session = _EnergySession()

    with (
        patch.object(voice_audio, "_load_silero_session", return_value=session),
        patch.object(voice_audio, "_open_input_stream", side_effect=streams),
    ):
        vad = voice_audio.SileroVad(mode="record")
        captures = [
            voice_audio.capture_utterance(
                vad=vad,
                max_duration_s=2.0,
                min_voiced_s=voice_audio.SILERO_CHUNK_SAMPLES / 16_000,
            )
            for _ in range(2)
        ]

    expected_frames = speech_frames + required_misses
    assert [len(audio) // len(speech) for audio in captures] == [
        expected_frames,
        expected_frames,
    ]
    assert session.run_count == 2 * (5 + expected_frames), (
        "each utterance must run five silent prewarm inferences before real frames"
    )
    endpoint_points = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "vad_endpoint_candidate"
    ]
    assert [point.attributes["consecutive_silence_frames"] for point in endpoint_points] == [
        required_misses,
        required_misses,
    ]
    assert [
        point.attributes["consecutive_silence_audio_ms"] for point in endpoint_points
    ] == [
        pytest.approx(required_misses * voice_audio.SILERO_CHUNK_SAMPLES / 16_000 * 1_000)
    ] * 2
