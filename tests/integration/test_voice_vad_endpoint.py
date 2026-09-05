"""Wave-0A integration regressions for reusable Silero endpoint state."""

from __future__ import annotations

from dataclasses import replace
from typing import Self
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.surface import voice_audio, voice_session


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


class _BetweenProfilesSession:
    """ONNX-shaped session whose probabilities all sit between the profiles.

    The probability varies frame to frame and the recurrent state counts
    frames, so a smoothing window or an LSTM cleared by a profile switch is
    observable rather than indistinguishable from a fresh one.
    """

    def __init__(self) -> None:
        self._probabilities = (0.44, 0.45, 0.46)
        self.calls = 0

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        probability = self._probabilities[self.calls % len(self._probabilities)]
        self.calls += 1
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"] + 1.0,
            inputs["c"] + 1.0,
        ]


def _between_profiles_frame(index: int) -> voice_audio.CanonicalAudioFrame:
    """A frame at -35 dBFS: speech under ``record``, silence under ``tts``."""
    return voice_audio.CanonicalAudioFrame(
        stream_epoch=1,
        sequence=index,
        sample_cursor=index * voice_audio.SILERO_CHUNK_SAMPLES,
        sample_rate_hz=16_000,
        frame_count=voice_audio.SILERO_CHUNK_SAMPLES,
        adc_time_s=None,
        captured_monotonic_ns=index,
        discontinuity_before=False,
        pcm16_mono=np.full(
            voice_audio.SILERO_CHUNK_SAMPLES,
            583,
            dtype="<i2",
        ).tobytes(),
    )


def _armed_assembler(
    speaking: list[bool],
) -> tuple[voice_session.UtteranceAssembler, voice_audio.SileroVad]:
    """One assembler armed at cursor 0 whose output state follows ``speaking``."""
    vad = voice_audio.SileroVad(mode="record")
    assembler = voice_session.UtteranceAssembler(
        vad=vad,
        # Pinned, not defaulted: the default is "record" and these tests are
        # about the switch, not about which profile ships.
        config=replace(
            voice_session.RealtimeInputSessionConfig(),
            output_active_vad_mode="tts",
        ),
        sample_rate_hz=16_000,
        frame_samples=voice_audio.SILERO_CHUNK_SAMPLES,
        session_id="S-vad-profile",
        output_active=lambda: speaking[0],
    )
    assembler.prepare()
    assembler.arm(
        voice_session.WakeDetection(
            stream_epoch=1,
            input_sample_cursor=0,
            observed_monotonic_ns=0,
            probability=1.0,
        ),
    )
    return assembler, vad


def test_strict_profile_classifies_playback_bleed_as_silence_while_output_active() -> None:
    """While output is active the tts profile rejects frames record accepts."""
    reset_realtime_trace()
    speaking = [True]
    with patch.object(
        voice_audio,
        "_load_silero_session",
        return_value=_BetweenProfilesSession(),
    ):
        assembler, vad = _armed_assembler(speaking)
        for index in range(12):
            assert assembler.feed(_between_profiles_frame(index)) is None

        assert not vad.is_speech_detected(), (
            "frames between the two profiles must not start speech under tts"
        )
        assert vad._mode == "tts"  # noqa: SLF001

        # The same frames under the record profile do start speech, and the
        # trace names the profile actually in effect for that frame.
        speaking[0] = False
        for index in range(12, 20):
            assembler.feed(_between_profiles_frame(index))

    assert vad.is_speech_detected()
    started = [
        point for point in realtime_trace_snapshot() if point.name == "vad_speech_started"
    ]
    assert [point.attributes["vad_mode"] for point in started] == ["record"]


def test_profile_switch_carries_detector_state_and_reported_mode() -> None:
    """Flipping the output state moves thresholds and vad_mode, nothing else."""
    reset_realtime_trace()
    speaking = [False]
    with patch.object(
        voice_audio,
        "_load_silero_session",
        return_value=_BetweenProfilesSession(),
    ):
        assembler, vad = _armed_assembler(speaking)
        for index in range(6):
            assembler.feed(_between_profiles_frame(index))
        assert vad.is_speech_detected()
        state_before = (vad._state, vad._hits, vad._misses)  # noqa: SLF001
        window_before = list(vad._prob_window)  # noqa: SLF001
        lstm_before = vad._h.copy()  # noqa: SLF001

        speaking[0] = True
        assembler.feed(_between_profiles_frame(6))

        # The switch moved the thresholds and the reported mode and left the
        # state machine, the counters and the smoothing windows running.
        assert vad._t == voice_audio._MODE_THRESHOLDS["tts"]  # noqa: SLF001
        assert vad._mode == "tts"  # noqa: SLF001
        assert (vad._state, vad._hits) == state_before[:2]  # noqa: SLF001
        assert vad._misses == state_before[2] + 1  # noqa: SLF001
        assert list(vad._prob_window)[:-1] == window_before[1:]  # noqa: SLF001
        assert np.array_equal(vad._h, lstm_before + 1.0)  # noqa: SLF001

        endpoint_frames = vad.endpoint_silence_frames
        for index in range(7, 6 + endpoint_frames):
            assembler.feed(_between_profiles_frame(index))
        assert not vad.is_speech_detected()

        # Reverting the output state makes the same frames speech again.
        speaking[0] = False
        for index in range(6 + endpoint_frames, 12 + endpoint_frames):
            assembler.feed(_between_profiles_frame(index))

    assert vad.is_speech_detected()
    assert vad._mode == "record"  # noqa: SLF001
    modes = [
        (point.name, point.attributes["vad_mode"])
        for point in realtime_trace_snapshot()
        if point.name in {"vad_speech_started", "vad_endpoint_candidate"}
    ]
    assert modes == [
        ("vad_speech_started", "record"),
        ("vad_endpoint_candidate", "tts"),
        ("vad_speech_started", "record"),
    ]
