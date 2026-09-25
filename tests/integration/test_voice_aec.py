"""Hermetic check: Jarvis's playback leaves the mic, Allen's voice stays.

Drives :class:`EchoCanceller` exactly as the daemon does: the player's output
callback hands over 1024-sample 48 kHz float blocks, the ingress worker cleans
512-sample 16 kHz frames, interleaved in wall-clock order. The "room" is a
60 ms delay plus a short decaying reflection; the near end is a second voice
talking over the far end. The live acoustic run on the laptop speakers is the
real exercise; this pins the plumbing and the WebRTC module underneath it.
"""

from __future__ import annotations

import numpy as np
import soxr

from jarvis.surface.voice_aec import EchoCanceller

MIC_HZ = 16_000
PLAY_HZ = 48_000
MIC_FRAME = 512
PLAY_BLOCK = 1024


def _voice(seconds: float, pitch_hz: float, seed: int) -> np.ndarray:
    """Speech-like 16 kHz signal: gliding harmonics under a syllable envelope."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * MIC_HZ)) / MIC_HZ
    f0 = pitch_hz * (1 + 0.15 * np.sin(2 * np.pi * 0.7 * t))
    phase = 2 * np.pi * np.cumsum(f0) / MIC_HZ
    tone = sum(np.sin(k * phase) / k for k in range(1, 12))
    envelope = np.clip(np.sin(2 * np.pi * 3.1 * t + rng.uniform(0, 6)), 0, None) ** 0.5
    return (tone * envelope + 0.05 * rng.standard_normal(t.size)).astype(np.float32)


def _db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x.astype(np.float64)))) + 1e-9))


def test_echo_is_removed_and_talk_over_survives() -> None:
    """Echo-only stretch drops >= 20 dB; the talk-over keeps within 8 dB."""
    seconds = 8.0
    far = 0.25 * _voice(seconds, 190.0, seed=1)  # what Jarvis plays, full scale = 1.0
    room = np.zeros(int(0.06 * MIC_HZ) + 400, dtype=np.float32)
    room[int(0.06 * MIC_HZ)] = 0.5
    room[-400:] += 0.15 * np.exp(-np.arange(400) / 80.0) * np.sign(np.sin(np.arange(400)))
    echo = np.convolve(far, room)[: far.size]
    near = np.zeros_like(far)
    talk = slice(int(5.0 * MIC_HZ), int(7.0 * MIC_HZ))  # Allen talks over Jarvis
    near[talk] = 0.08 * _voice(2.0, 120.0, seed=2)
    mic = np.clip((echo + near) * 32767, -32768, 32767).astype("<i2")
    playback = soxr.resample(far, MIC_HZ, PLAY_HZ).astype(np.float32)

    canceller = EchoCanceller()
    cleaned = bytearray()
    played = 0
    for start in range(0, mic.size - MIC_FRAME + 1, MIC_FRAME):
        # The output callback runs ahead of the mic: everything due by the end
        # of this mic frame has been handed to the device already.
        due = (start + MIC_FRAME) * (PLAY_HZ // MIC_HZ)
        while played < due:
            canceller.add_playback(playback[played : played + PLAY_BLOCK], PLAY_HZ)
            played += PLAY_BLOCK
        frame = mic[start : start + MIC_FRAME].tobytes()
        out = canceller.clean(frame, stream_epoch=1, discontinuity=False)
        assert len(out) == len(frame)
        cleaned.extend(out)
    result = np.frombuffer(bytes(cleaned), dtype="<i2").astype(np.float32) / 32767
    lag = MIC_HZ // 100  # clean() returns audio one 10 ms chunk late
    result = result[lag:]
    raw = mic[: result.size].astype(np.float32) / 32767

    echo_only = slice(int(1.5 * MIC_HZ), int(4.5 * MIC_HZ))
    reduction = _db(raw[echo_only]) - _db(result[echo_only])
    kept = _db(result[talk]) - _db(near[talk])
    assert reduction >= 20.0, f"echo only reduced {reduction:.1f} dB"
    assert kept >= -8.0, f"talk-over lost {-kept:.1f} dB"
