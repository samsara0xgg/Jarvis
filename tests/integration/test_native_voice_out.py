"""The native voice-out helper (ADR 0129) against the real binary, rendering on a timer.

``--null-device`` makes the helper call its one render function every block at
real-time pace instead of CoreAudio, and ``--null-capture`` keeps every rendered
block, so these tests read both what the daemon is told (cursors, audibility,
acks) and what would have reached the speaker. Audio never leaves the process.
"""

from __future__ import annotations

import os
import shutil
import signal
import time
from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np
import pytest

from jarvis.surface import voice_native_out
from jarvis.surface.voice_ledger import GenerationLease, StalePlaybackGeneration
from jarvis.surface.voice_native_out import NativeAudioStreamPlayer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from jarvis.surface.voice_tts import _CallbackReport

pytestmark = pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")

RATE = 48_000


class _Rig:
    """A started native player whose callback reports are recorded as they drain."""

    def __init__(self, tmp_path: Path, *, ring_seconds: float = 0.5) -> None:
        self.capture = tmp_path / "rendered.f32"
        self.player = NativeAudioStreamPlayer(
            sample_rate_hz=RATE,
            ring_seconds=ring_seconds,
            generation_safe=True,
            estimated_output_latency_s=0.0,
            extra_args=("--null-device", "--null-capture", str(self.capture)),
        )
        self.reports: list[_CallbackReport] = []
        drain = self.player._callback_reports.drain  # noqa: SLF001

        def recording_drain() -> list[_CallbackReport]:
            drained = drain()
            self.reports.extend(drained)
            return drained

        self.player._callback_reports.drain = recording_drain  # type: ignore[method-assign]  # noqa: SLF001

    def rendered(self) -> np.ndarray:
        return np.fromfile(self.capture, dtype=np.float32)

    def speak(self, samples: np.ndarray, response_id: str = "R") -> int:
        lease = self.player.activate_generation(
            session_id="S", response_id=response_id, response_group_id="G", turn_id="T",
        )
        assert isinstance(lease, GenerationLease)
        generation = lease.playback_generation_id
        self.player.begin_generation_segment(
            expected_playback_generation_id=generation, sequence=0, text="words", segment_hash="h",
        )
        self.feed(generation, samples)
        self.player.finish_generation_segment(
            expected_playback_generation_id=generation, sequence=0,
        )
        return generation

    def feed(self, generation: int, samples: np.ndarray) -> None:
        """write_generation until every sample is accepted, as the media actor does."""
        offset = 0
        deadline = time.monotonic() + 10
        while offset < len(samples):
            result = self.player.write_generation(
                samples[offset:].tobytes(),
                expected_playback_generation_id=generation,
                segment_sequence=0,
            )
            if result is None:
                assert time.monotonic() < deadline
                self.player.poll_presentation()
                time.sleep(0.002)
                continue
            assert not isinstance(result, StalePlaybackGeneration)
            offset += result.sample_count

    def until(self, condition: Callable[[], bool], what: str, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            self.player.poll_presentation()
            time.sleep(0.002)

    def presented(self, generation: int) -> None:
        def done() -> bool:
            snap = self.player.poll_generation(generation)
            return not isinstance(snap, StalePlaybackGeneration) and snap.fully_presented

        self.until(done, "fully_presented")

    def of(self, generation: int) -> list[_CallbackReport]:
        return [r for r in self.reports if r.generation == generation]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[_Rig]:
    """A started native player on the null device."""
    made = _Rig(tmp_path)
    started = made.player.start()
    assert started.started, started.reason
    yield made
    made.player.stop()


def _tone(seconds: float, level: float = 0.5) -> np.ndarray:
    return np.full(int(RATE * seconds), level, dtype=np.float32)


def test_a_played_generation_reports_contiguous_cursors_and_is_fully_presented(rig: _Rig) -> None:
    """Each callback span starts where the last ended; the ledger reaches fully_presented."""
    samples = _tone(0.4)
    generation = rig.speak(samples)
    rig.presented(generation)
    reports = rig.of(generation)
    assert reports[0].output_start_cursor == 0
    assert reports[-1].output_end_cursor == len(samples)
    assert all(a.output_end_cursor == b.output_start_cursor for a, b in pairwise(reports))
    assert [r.first_for_generation for r in reports].count(True) == 1
    assert {r.audibility_class for r in reports} == {"normal"}
    snapshot = rig.player.poll_generation(generation)
    assert not isinstance(snapshot, StalePlaybackGeneration)
    assert snapshot.heard_text == "words"
    assert rig.player.played_samples == len(samples)
    assert rig.player.underflow_count == 0
    assert abs(rig.player.clock_skew_ns) < 1_000_000
    print(  # noqa: T201
        f"A1 cursors: {len(reports)} reports, played={rig.player.played_samples}, "
        f"clock_skew_ns={rig.player.clock_skew_ns}, buffer={rig.player._buffer_frames}",  # noqa: SLF001
    )


def test_interrupt_acks_the_discard_and_freezes_with_nothing_reported_after_it(rig: _Rig) -> None:
    """A discard is acked after every report it could have caused; nothing is reported after."""
    generation = rig.speak(_tone(0.6))
    rig.until(lambda: len(rig.of(generation)) >= 3, "three callbacks played")
    rig.player.interrupt_generation(expected_playback_generation_id=generation)
    assert rig.player.bytes_pending() == 0
    frozen = None
    deadline = time.monotonic() + 3
    while frozen is None:
        frozen = rig.player.settle_interrupted_generation(
            expected_playback_generation_id=generation,
        )
        assert time.monotonic() < deadline
        time.sleep(0.002)
    assert not isinstance(frozen, StalePlaybackGeneration)
    at_ack = len(rig.of(generation))
    mirror = rig.player._mirror  # noqa: SLF001
    assert mirror.acked_seq == mirror.discard_seq == 1
    time.sleep(0.15)
    rig.player.poll_presentation()
    assert len(rig.of(generation)) == at_ack
    assert frozen.submitted_samples < frozen.accepted_samples
    rig.until(lambda: mirror.read_idx >= mirror.discard_before, "helper read index past discard")
    # What the speaker gets ends in silence: the declick, then exact zeros.
    rendered = rig.rendered()
    assert np.all(rendered[-2_000:] == 0.0)
    print(  # noqa: T201
        f"A1 interrupt: {at_ack} reports before ack, accepted={frozen.accepted_samples} "
        f"submitted={frozen.submitted_samples}, ack seq={mirror.acked_seq}",
    )


def test_the_yield_gain_is_heard_and_a_fade_to_silence_is_not(rig: _Rig) -> None:
    """Mirror of test_words_under_her_barge_in_yield_still_count_as_heard, through the helper."""
    rig.player.set_gain(0.2, 10.0)
    generation = rig.speak(_tone(0.3))
    rig.presented(generation)
    assert {r.audibility_class for r in rig.of(generation)} == {"normal"}
    assert abs(rig.player.current_gain() - 0.2) < 1e-6
    rig.player.complete_generation(expected_playback_generation_id=generation)

    second = rig.speak(_tone(0.6), "R2")
    rig.until(lambda: len(rig.of(second)) >= 2, "second answer playing")
    rig.player.set_gain(0.0, 10.0)
    rig.presented(second)
    classes = [r.audibility_class for r in rig.of(second)]
    assert classes[0] == "normal"
    assert "attenuated" in classes
    assert classes[-1] == "muted"
    print(f"A1 gain: yield 0.2 -> all normal, fade to 0 -> {sorted(set(classes))}")  # noqa: T201


def test_hold_stops_the_cursors_and_release_fades_in_without_losing_samples(rig: _Rig) -> None:
    """A held answer keeps its place in silence and, released, ramps in and plays on."""
    samples = _tone(0.6)
    generation = rig.speak(samples)
    rig.until(lambda: rig.player.played_samples >= 4_800, "audio playing")
    rig.player.pause_generation(paused=True)
    time.sleep(0.05)
    rig.player.poll_presentation()
    frozen_at = rig.player.played_samples
    reports_at = len(rig.of(generation))
    time.sleep(0.2)
    rig.player.poll_presentation()
    assert rig.player.played_samples == frozen_at
    assert len(rig.of(generation)) == reports_at
    assert rig.player.generation_held(generation)
    rig.player.pause_generation(paused=False)
    rig.presented(generation)
    reports = rig.of(generation)
    assert all(a.output_end_cursor == b.output_start_cursor for a, b in pairwise(reports))
    assert reports[-1].output_end_cursor == len(samples)
    assert rig.player.played_samples == len(samples)

    rendered = rig.rendered()
    live = np.flatnonzero(rendered)
    hole = int(np.argmax(np.diff(live)))
    assert live[hole + 1] - live[hole] > 2_000  # the hold: silence
    resumed = int(live[hole + 1]) - 1  # ramp sample 0 is exactly 0.0
    ramp = rendered[resumed : resumed + 128]
    assert np.allclose(ramp, 0.5 * np.arange(128) / 127, atol=1e-6)
    assert np.all(rendered[resumed + 128 : resumed + 256] == 0.5)
    print(  # noqa: T201
        f"A1 hold: frozen at {frozen_at} samples for 0.2 s, resumed ramp 0 -> "
        f"{ramp[-1]:.3f} over 128 samples, played={rig.player.played_samples}/{len(samples)}",
    )


def test_a_full_mirrored_ring_returns_none_and_never_drops(tmp_path: Path) -> None:
    """At the helper ring's capacity write_generation answers None; nothing is lost."""
    made = _Rig(tmp_path, ring_seconds=0.25)  # 12000 samples -> a 16384-sample ring
    assert made.player.start().started
    try:
        lease = made.player.activate_generation(
            session_id="S", response_id="R", response_group_id="G", turn_id="T",
        )
        assert isinstance(lease, GenerationLease)
        generation = lease.playback_generation_id
        made.player.begin_generation_segment(
            expected_playback_generation_id=generation, sequence=0, text="words", segment_hash="h",
        )
        made.player.pause_generation(paused=True)
        offered = _tone(0.5)
        accepted = 0
        while True:
            result = made.player.write_generation(
                offered[accepted:].tobytes(),
                expected_playback_generation_id=generation,
                segment_sequence=0,
            )
            if result is None:
                break
            assert not isinstance(result, StalePlaybackGeneration)
            accepted += result.sample_count
        assert accepted == 16_384
        assert made.player.bytes_pending() == 16_384 * 4
        time.sleep(0.1)
        assert made.player.write_generation(
            offered[accepted:].tobytes(),
            expected_playback_generation_id=generation,
            segment_sequence=0,
        ) is None
        made.player.pause_generation(paused=False)
        made.feed(generation, offered[accepted:])
        made.player.finish_generation_segment(
            expected_playback_generation_id=generation, sequence=0,
        )
        made.presented(generation)
        assert made.player.played_samples == len(offered)
        print(  # noqa: T201
            f"A1 backpressure: ring full at {accepted} samples -> None, no drop, "
            f"played {made.player.played_samples}/{len(offered)}",
        )
    finally:
        made.player.stop()


def test_a_killed_helper_stops_the_player_and_restart_brings_it_back(rig: _Rig) -> None:
    """SIGKILL on the helper: is_running goes False, the lease ends, restart() recovers."""
    first = rig.speak(_tone(0.6))
    rig.until(lambda: rig.player.played_samples > 0, "audio playing")
    pid = rig.player._proc.pid  # type: ignore[union-attr]  # noqa: SLF001
    os.kill(pid, signal.SIGKILL)
    rig.until(lambda: not rig.player.is_running, "player noticing the dead helper")
    assert rig.player.active_lease is None  # its generation is terminal, not stuck
    # No ack will ever come: settle freezes what was reported instead of waiting forever.
    frozen = rig.player.settle_interrupted_generation(expected_playback_generation_id=first)
    assert frozen is not None
    assert not isinstance(frozen, StalePlaybackGeneration)
    restarted = rig.player.restart()
    assert restarted.started, restarted.reason
    assert rig.player.is_running
    assert rig.player._proc.pid != pid  # type: ignore[union-attr]  # noqa: SLF001
    again = rig.speak(_tone(0.2), "R2")
    rig.presented(again)
    print(f"A1 kill: pid {pid} killed, is_running False, restart -> {restarted.status}")  # noqa: T201


def test_the_helper_is_rebuilt_only_when_missing_or_older_than_its_source(tmp_path: Path) -> None:
    """The helper builds once and is rebuilt only when main.swift is newer than the binary."""
    source = tmp_path / "main.swift"
    source.write_text('import Foundation\nprint("built")\n')
    binary = voice_native_out.ensure_helper_binary(tmp_path)
    assert binary.exists()
    built_at = binary.stat().st_mtime_ns
    assert voice_native_out.ensure_helper_binary(tmp_path) == binary
    assert binary.stat().st_mtime_ns == built_at
    os.utime(source, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    voice_native_out.ensure_helper_binary(tmp_path)
    assert binary.stat().st_mtime_ns > built_at


def test_an_unknown_device_fails_closed_with_the_helper_message() -> None:
    """A device name the helper cannot find makes start() fail closed with its message."""
    player = NativeAudioStreamPlayer(
        sample_rate_hz=RATE, ring_seconds=0.25, generation_safe=True,
        device="No Such Output Device",
    )
    started = player.start()
    assert started.status == "failed_closed"
    assert "output device not found" in started.reason
    assert not player.is_running
    assert player.stop().status == "already_closed"
    print(f"A1 unknown device: {started.status} {started.reason}")  # noqa: T201

