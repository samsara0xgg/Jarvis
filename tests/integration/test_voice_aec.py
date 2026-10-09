"""Hermetic check: Jarvis's playback leaves the mic, Allen's voice stays.

Drives :class:`EchoCanceller` exactly as the daemon does: the player's output
callback hands over 1024-sample 48 kHz float blocks stamped with the host time
they are heard, the ingress worker cleans 512-sample 16 kHz frames stamped with
the host time they were captured, interleaved in wall-clock order. The "room"
is a 60 ms delay plus a short decaying reflection; the near end is a second
voice talking over the far end. The live acoustic run on the laptop speakers is
the real exercise; this pins the plumbing and the WebRTC module underneath it,
including the two failures the host-time pairing removes (ADR 0193): a far end
that arrives 200 ms late in one burst, and a mic that loses 100 ms mid-echo.
"""

from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import soxr

from jarvis.surface.voice_aec import EchoCanceller

if TYPE_CHECKING:
    from pathlib import Path

MIC_HZ = 16_000
PLAY_HZ = 48_000
MIC_FRAME = 512
PLAY_BLOCK = 1024
AHEAD_S = 0.1  # the player hands a block over this long before it is heard


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


@dataclass(frozen=True)
class _Run:
    """What one pass through the synthetic room measured, in dB."""

    reduction: float  # echo-only stretch
    worst: float  # the worst 200 ms of it
    kept: float  # talk-over, relative to what Allen said

    def __str__(self) -> str:
        return (
            f"echo-only -{self.reduction:.1f} dB (worst 200 ms -{self.worst:.1f} dB), "
            f"talk-over {self.kept:+.1f} dB"
        )


def _run_room(  # noqa: PLR0913, PLR0915
    canceller: EchoCanceller,
    *,
    seconds: float = 6.0,
    realtime: bool = False,
    late_far_s: float = 0.0,
    gap_s: float = 0.0,
    event_s: float = 2.5,
    tmp_path: Path | None = None,
) -> _Run:
    """One pass: echo only until ``seconds - 1``, Allen talking over it for the last second.

    ``late_far_s``: from ``event_s`` the player's thread stalls that long, then hands over
    everything it held in one burst. ``gap_s``: at ``event_s`` the microphone loses that
    much audio. ``realtime`` paces the mic frames by the wall clock, which a stall needs.
    """
    far = 0.25 * _voice(seconds, 190.0, seed=1)  # what Jarvis plays, full scale = 1.0
    room = np.zeros(int(0.06 * MIC_HZ) + 400, dtype=np.float32)
    room[int(0.06 * MIC_HZ)] = 0.5
    room[-400:] += 0.15 * np.exp(-np.arange(400) / 80.0) * np.sign(np.sin(np.arange(400)))
    echo = np.convolve(far, room)[: far.size]
    near = np.zeros_like(far)
    talk_at = int((seconds - 1.0) * MIC_HZ)
    near[talk_at:] = 0.08 * _voice(1.0, 120.0, seed=2)[: far.size - talk_at]
    mic = np.clip((echo + near) * 32767, -32768, 32767).astype("<i2")
    gap_at = int(event_s * MIC_HZ) // MIC_FRAME * MIC_FRAME  # lost samples [gap_at, +gap)
    gap = int(gap_s * MIC_HZ)
    heard = np.delete(mic, np.s_[gap_at : gap_at + gap]) if gap else mic
    near_heard = np.delete(near, np.s_[gap_at : gap_at + gap]) if gap else near
    # What Jarvis plays runs on past the mic so the last chunks still find their far end.
    playback = soxr.resample(far, MIC_HZ, PLAY_HZ).astype(np.float32)
    playback = np.concatenate([playback, np.zeros(PLAY_HZ // 2, dtype=np.float32)])

    t0 = time.monotonic_ns()  # host time of sample 0, for the mic and for what Jarvis plays
    lock = threading.Lock()
    held: list[tuple[np.ndarray, int]] = []
    stalled = False

    def hand_over(block: np.ndarray, presentation_ns: int) -> None:
        with lock:
            if stalled:
                held.append((block, presentation_ns))
            else:
                canceller.add_playback(block, PLAY_HZ, presentation_ns)

    def unstall() -> None:
        nonlocal stalled
        with lock:
            stalled = False
            for block, presentation_ns in held:
                canceller.add_playback(block, PLAY_HZ, presentation_ns)
            held.clear()

    cleaned = bytearray()
    played = 0
    started = False
    for start in range(0, heard.size - MIC_FRAME + 1, MIC_FRAME):
        at = start + (gap if gap and start >= gap_at else 0)  # where the frame sits in time
        end_ns = t0 + (at + MIC_FRAME) * 1_000_000_000 // MIC_HZ
        if realtime:
            time.sleep(max(0.0, (end_ns - time.monotonic_ns()) / 1e9))
        if late_far_s and not started and at >= event_s * MIC_HZ:
            started = True
            with lock:
                stalled = True
            threading.Timer(late_far_s, unstall).start()
        # The output callback runs ahead of the mic: everything heard by the end of this
        # mic frame plus AHEAD_S has been handed to the device already.
        due = (at + MIC_FRAME + int(AHEAD_S * MIC_HZ)) * (PLAY_HZ // MIC_HZ)
        while played < due and played < playback.size:
            hand_over(
                playback[played : played + PLAY_BLOCK],
                t0 + played * 1_000_000_000 // PLAY_HZ,
            )
            played += PLAY_BLOCK
        frame = heard[start : start + MIC_FRAME].tobytes()
        out = canceller.clean(
            frame, stream_epoch=1, discontinuity=bool(gap) and start == gap_at,
            adc_time_s=(t0 + at * 1_000_000_000 // MIC_HZ) / 1e9,
        )
        assert len(out) == len(frame)
        cleaned.extend(out)
    unstall()
    result = np.frombuffer(bytes(cleaned), dtype="<i2").astype(np.float32) / 32767
    lag = MIC_HZ // 100  # clean() returns audio one 10 ms chunk late
    result = result[lag:]
    raw = heard[: result.size].astype(np.float32) / 32767

    # In heard-sample terms: the echo-only stretch ends before Allen starts.
    echo_only = slice(int(1.5 * MIC_HZ), talk_at - int(gap_s * MIC_HZ) - MIC_HZ // 2)
    talk = slice(talk_at - gap, result.size)
    step = MIC_HZ // 5
    worst = min(
        _db(raw[i : i + step]) - _db(result[i : i + step])
        for i in range(echo_only.start, echo_only.stop - step, step // 2)
    )
    if tmp_path is not None:
        # The barge-in diagnostic: one 16 kHz WAV, ch0 mic, ch1 played, ch2 cleaned.
        path = canceller.dump(tmp_path)
        assert path is not None
        with wave.open(str(path)) as dumped:
            assert (dumped.getnchannels(), dumped.getframerate()) == (3, MIC_HZ)
            tracks = np.frombuffer(dumped.readframes(dumped.getnframes()), dtype="<i2")
        mic_ch, played_ch, cleaned_ch = (
            tracks.reshape(-1, 3)[echo_only, i] / 32767 for i in range(3)
        )
        assert _db(played_ch) > -30.0, "the played track is silent"
        assert _db(mic_ch) - _db(cleaned_ch) >= 20.0, "the dump's mic and cleaned tracks match"
    return _Run(
        _db(raw[echo_only]) - _db(result[echo_only]), worst,
        _db(result[talk]) - _db(near_heard[: result.size][talk]),
    )


def test_echo_is_removed_and_talk_over_survives(tmp_path: Path) -> None:
    """Echo-only stretch drops >= 20 dB; talk-over keeps within 8 dB; dump holds all three."""
    run = _run_room(EchoCanceller(history_s=8.0), seconds=8.0, tmp_path=tmp_path)
    assert run.reduction >= 20.0, f"echo only reduced {run.reduction:.1f} dB"
    assert run.kept >= -8.0, f"talk-over lost {-run.kept:.1f} dB"
    print(f"A1 steady room: {run}")  # noqa: T201


def test_a_far_end_that_arrives_200_ms_late_still_cancels_the_echo() -> None:
    """The player's thread stalls mid-echo and delivers in one burst: the mic waits for it."""
    canceller = EchoCanceller()
    run = _run_room(canceller, realtime=True, late_far_s=0.2)
    assert run.reduction >= 20.0, f"echo only reduced {run.reduction:.1f} dB"
    assert run.kept >= -8.0, f"talk-over lost {-run.kept:.1f} dB"
    assert "far-end waits 0 " not in canceller.summary()  # it did wait, and did not give up
    assert "0 gave up" in canceller.summary()
    print(f"A1 late far end: {run} :: {canceller.summary()}")  # noqa: T201


def test_a_mic_gap_mid_echo_does_not_restart_the_canceller() -> None:
    """The mic loses 100 ms: both streams are spliced at the same instant, no fresh WebRTC."""
    canceller = EchoCanceller()
    run = _run_room(canceller, realtime=True, gap_s=0.1)
    assert run.reduction >= 20.0, f"echo only reduced {run.reduction:.1f} dB"
    assert run.kept >= -8.0, f"talk-over lost {-run.kept:.1f} dB"
    assert "capture gaps 1," in canceller.summary()
    assert canceller.summary().endswith("restarts 1")  # the stream's own first one
    print(f"A1 mic gap: {run} :: {canceller.summary()}")  # noqa: T201


def test_the_mic_waits_once_for_a_far_end_that_stalls_and_never_for_a_stopped_player() -> None:
    """A live player that stops delivering costs one 300 ms wait; a stopped one costs nothing."""
    block = np.zeros(PLAY_BLOCK, dtype=np.float32)
    frame = bytes(MIC_FRAME * 2)
    t0 = time.monotonic_ns()

    def feed(canceller: EchoCanceller, frames: int) -> float:
        began = time.monotonic()
        for k in range(frames):
            canceller.clean(
                frame, stream_epoch=1, discontinuity=False,
                adc_time_s=(t0 + k * 32_000_000) / 1e9,
            )
        return time.monotonic() - began

    stalled = EchoCanceller()
    stalled.add_playback(block, PLAY_HZ, t0)  # 21 ms of far end, then nothing more
    took = feed(stalled, 4)
    assert 0.25 < took < 0.6, f"4 frames took {took:.2f} s: one wait expected"
    assert stalled.summary().startswith("far-end waits 1 ")
    assert "1 gave up" in stalled.summary()

    stopped = EchoCanceller()
    stopped.add_playback(block, PLAY_HZ, t0)
    time.sleep(0.6)  # past the 500 ms a live player is heard within
    took = feed(stopped, 4)
    assert took < 0.1, f"4 frames took {took:.2f} s: no wait expected"
    assert stopped.summary().startswith("far-end waits 0 ")
    print(f"A1 waits: stalled player {stalled.summary()}; stopped player {stopped.summary()}")  # noqa: T201


def test_a_canonical_frame_carries_the_capture_time_of_its_first_sample() -> None:
    """Native blocks of 480 samples feed 512-sample frames; each is stamped where it starts."""
    from jarvis.surface.voice_audio import (  # noqa: PLC0415
        AudioIngressCanonicalizer,
        _OwnedPcmFrame,
    )

    canonicalizer = AudioIngressCanonicalizer(sample_rate_hz=MIC_HZ, frame_samples=MIC_FRAME)
    native = 480
    t0 = 1000.0
    stamps: list[float] = []
    for k in range(20):
        # The device clock runs 30 ppm fast: every block's own stamp is the truth.
        adc = t0 + k * native / MIC_HZ * (1 + 30e-6)
        block = _OwnedPcmFrame(
            stream_epoch=1, sequence=k, sample_cursor=k * native, sample_rate_hz=MIC_HZ,
            channels=1, frame_count=native, adc_time_s=adc, captured_monotonic_ns=0,
            discontinuity_before=False, pcm=bytes(native * 2),
        )
        stamps += [f.adc_time_s for f in canonicalizer.feed(block) if f.adc_time_s is not None]
    assert len(stamps) == 18  # 20 * 480 = 9600 samples = 18 frames and 384 left over
    off_ms = [
        1000 * (stamp - (t0 + k * MIC_FRAME / MIC_HZ * (1 + 30e-6)))
        for k, stamp in enumerate(stamps)
    ]
    assert max(map(abs, off_ms)) < 1.0, f"frame stamps off by {off_ms} ms"
    print(f"A1 frame stamps: {len(stamps)} frames, worst {max(map(abs, off_ms)):.3f} ms off")  # noqa: T201


def test_echo_cancellation_config_auto_true_false() -> None:
    """Auto follows the microphone, true cancels every one, false builds nothing."""
    from types import SimpleNamespace  # noqa: PLC0415

    from jarvis.runtime.inherent_loop import _build_echo_canceller  # noqa: PLC0415

    def built(*, setting: object) -> EchoCanceller | None:
        ingress = {} if setting is None else {"echo_cancellation": setting}
        runtime = SimpleNamespace(config={"realtime": {"single_audio_ingress": ingress}})
        return _build_echo_canceller(runtime)

    frame = np.full(512, 1000, dtype="<i2").tobytes()
    for setting, respeaker_passes in ((None, True), ("auto", True), (True, False)):
        canceller = built(setting=setting)
        assert canceller is not None
        canceller.set_input_device("reSpeaker XVF3800 4-Mic Array")
        out = canceller.clean(frame, stream_epoch=1, discontinuity=False)
        assert (out == frame) is respeaker_passes
    assert built(setting=False) is None
