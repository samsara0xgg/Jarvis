"""File-replay ``AudioDuplexBackend``: paced, contiguous, and endpoint-capable.

The replay backend is the seed of the ADR-0006 §10.3 Tier 2 corpus runner.
These checks feed synthetic WAVs through the real ``AudioIngress`` and, for
the end-to-end case, through the real ``DuplexVoiceSession`` with a patched
Silero session and a scripted partial decoder.
"""

from __future__ import annotations

import time
import wave
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.surface import voice_audio, voice_backend, voice_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_FRAME = voice_audio.SILERO_CHUNK_SAMPLES


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.002)


def _write_wav(path: Path, pcm: np.ndarray, *, rate: int = 16_000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.astype("<i2").tobytes())
    return path


def _ingress(backend: voice_backend.AudioDuplexBackend) -> voice_audio.AudioIngress:
    return voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            shutdown_timeout_s=1.0,
        ),
    )


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def run(self, _output_names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


class _AlwaysOnWake:
    model_name = "replay"

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        return {self.model_name: 1.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class _RecordingPipeline:
    """Whole-WAV adapter fixture with a scripted partial decoder."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(dict(kwargs))
        return MagicMock()

    def partial_text(self, audio_bytes: bytes) -> str:
        del audio_bytes
        return "把灯打开。"


def test_replay_feeds_real_ingress_contiguously_at_real_time_pace(tmp_path: Path) -> None:
    """File frames arrive gap-free and on the audio clock; silence follows EOF."""
    samples = 8_000  # 0.5 s
    pcm = (np.arange(samples) % 2_000).astype("<i2")
    path = _write_wav(tmp_path / "half_second.wav", pcm)
    backend = voice_backend.FileReplayBackend(path)
    ingress = _ingress(backend)
    subscription = ingress.subscribe(
        name="capture",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
        capacity=64,
    )
    started = time.monotonic()
    assert ingress.start().started
    file_frames = samples // _FRAME  # 15 full frames + one zero-padded tail
    frames: list[voice_audio.CanonicalAudioFrame] = []
    while len(frames) < file_frames + 4:
        frame = subscription.read(timeout_s=0.05)
        if frame is not None:
            frames.append(frame)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.4, f"replay ran faster than real time: {elapsed:.3f}s"
    cursors = [frame.sample_cursor for frame in frames]
    assert cursors == [index * _FRAME for index in range(len(frames))]
    assert not any(frame.discontinuity_before for frame in frames)
    expected = pcm.tobytes()
    for index in range(file_frames):
        assert frames[index].pcm16_mono == expected[index * _FRAME * 2 : (index + 1) * _FRAME * 2]
    assert frames[file_frames].pcm16_mono[: (samples - file_frames * _FRAME) * 2] == expected[
        file_frames * _FRAME * 2 :
    ]
    assert all(not any(frame.pcm16_mono) for frame in frames[file_frames + 1 :])
    assert backend.eof_reached
    assert not backend.capabilities().owns_default_input
    close = ingress.close()
    assert close.definitively_closed
    assert backend.ownership_snapshot().state is voice_backend.BackendLifecycleState.CLOSED


@pytest.mark.parametrize(
    ("rate", "channels", "match"),
    [
        (44_100, 1, "sample rate"),
        (16_000, 2, "mono"),
    ],
)
def test_replay_rejects_non_canonical_wav(
    tmp_path: Path,
    rate: int,
    channels: int,
    match: str,
) -> None:
    """Only 16 kHz mono PCM16 fixtures may enter the canonical ingress."""
    pcm = np.zeros(1_024 * channels, dtype="<i2")
    path = _write_wav(tmp_path / "bad.wav", pcm, rate=rate, channels=channels)
    with pytest.raises(ValueError, match=match):
        voice_backend.FileReplayBackend(path)


def test_two_burst_replay_commits_two_utterances_through_the_duplex_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2 seed: two 0.4 s bursts split by 0.3 s commit as two under shipped min_voiced."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,  # noqa: SLF001
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    tone = np.full(int(0.4 * 16_000), 10_000, dtype="<i2")
    gap = np.zeros(int(0.3 * 16_000), dtype="<i2")
    path = _write_wav(tmp_path / "two_bursts.wav", np.concatenate([tone, gap, tone]))
    backend = voice_backend.FileReplayBackend(path)
    ingress = _ingress(backend)
    pipeline = _RecordingPipeline()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=_AlwaysOnWake(),
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                max_utterance_s=2.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
                partial_asr=voice_session.PartialAsrConfig(
                    enabled=True,
                    interval_ms=32,
                    candidate_ms=64,
                    max_hold_ms=160,
                    post_roll_ms=32,
                ),
            ),
        )
        assert session.start().started
        _wait_until(lambda: len(pipeline.calls) == 2)
        time.sleep(0.2)
        close = session.close()
    assert close.definitively_closed
    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["utterance_id"] != pipeline.calls[1]["utterance_id"]
    assert [call["endpoint_reason"] for call in pipeline.calls] == [
        "stable_prefix_complete",
        "stable_prefix_complete",
    ]
    assert session.metrics().endpoint_commits == 2
