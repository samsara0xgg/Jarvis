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
import threading
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

    def __init__(
        self, tmp_path: Path, *, ring_seconds: float = 0.5, tap: bool = False,
    ) -> None:
        self.capture = tmp_path / "rendered.f32"
        self.tapped: list[np.ndarray] = []
        self.stamps: list[int] = []  # presentation_ns of each tapped block
        self.frame_types: set[int] = set()
        self.player = NativeAudioStreamPlayer(
            sample_rate_hz=RATE,
            ring_seconds=ring_seconds,
            generation_safe=True,
            estimated_output_latency_s=0.0,
            playback_tap=self._tap if tap else None,
            extra_args=("--null-device", "--null-capture", str(self.capture)),
        )
        on_frame = self.player._on_frame  # noqa: SLF001

        def recording_on_frame(frame_type: int, body: memoryview) -> None:
            self.frame_types.add(frame_type)
            on_frame(frame_type, body)

        self.player._on_frame = recording_on_frame  # type: ignore[method-assign]  # noqa: SLF001
        self.reports: list[_CallbackReport] = []
        drain = self.player._callback_reports.drain  # noqa: SLF001

        def recording_drain() -> list[_CallbackReport]:
            drained = drain()
            self.reports.extend(drained)
            return drained

        self.player._callback_reports.drain = recording_drain  # type: ignore[method-assign]  # noqa: SLF001

    def _tap(self, block: np.ndarray, _rate: int, presentation_ns: int) -> None:
        self.tapped.append(block.copy())
        self.stamps.append(presentation_ns)

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
def tap_rig(tmp_path: Path) -> Iterator[_Rig]:
    """A started native player on the null device with a playback tap."""
    made = _Rig(tmp_path, tap=True)
    started = made.player.start()
    assert started.started, started.reason
    yield made
    made.player.stop()


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


def test_the_next_answer_after_a_helper_crash_starts_it_again(rig: _Rig) -> None:
    """Nothing else restarts the helper, so her next answer must, or she stays silent."""
    first = rig.speak(_tone(0.6))
    rig.until(lambda: rig.player.played_samples > 0, "audio playing")
    pid = rig.player._proc.pid  # type: ignore[union-attr]  # noqa: SLF001
    os.kill(pid, signal.SIGKILL)
    rig.until(lambda: not rig.player.is_running, "player noticing the dead helper")
    rig.player.settle_interrupted_generation(expected_playback_generation_id=first)
    again = rig.speak(_tone(0.2), "R2")
    assert rig.player.is_running
    rig.presented(again)


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



def test_the_playback_tap_gets_every_rendered_block_silence_included(tap_rig: _Rig) -> None:
    """The far end is what the device was handed: silence, then the tone, then silence, in order."""
    made = tap_rig
    made.until(lambda: sum(map(len, made.tapped)) >= RATE // 10, "silence reaching the tap")
    samples = _tone(0.4)
    generation = made.speak(samples)
    made.presented(generation)
    made.until(lambda: sum(map(len, made.tapped)) >= RATE, "trailing silence")
    made.player.stop()
    tap = np.concatenate(made.tapped)
    rendered = made.rendered()
    assert len(tap) > 0
    assert np.array_equal(tap, rendered[: len(tap)])  # in order, nothing missing or doubled
    assert len(rendered) - len(tap) < RATE // 10  # only the stop-time tail is unseen
    assert made.player.rendered_dropped_samples == 0
    # Every block carries the host time its first sample is heard: one callback per frame,
    # so consecutive stamps are one block apart (the null device paces by sleep: a few ms of
    # jitter), and the clock is the daemon's own.
    stamps = np.diff(np.array(made.stamps, dtype=np.int64))
    block_ns = len(made.tapped[0]) * 1_000_000_000 // RATE
    assert np.all(stamps > 0)
    assert abs(float(np.median(stamps)) - block_ns) < block_ns * 0.1
    assert abs(made.stamps[-1] - time.monotonic_ns()) < 2_000_000_000
    lit = np.flatnonzero(tap != 0.0)
    assert lit[0] > 0  # silence before the tone
    assert lit[-1] < len(tap) - 1000  # and after it
    tone = np.count_nonzero(tap == np.float32(0.5))
    assert tone >= len(samples) - 2 * 128  # the tone, less ramps
    print(  # noqa: T201
        f"A1 tap: {len(made.tapped)} blocks, {len(tap)} samples == rendered prefix, "
        f"tone {tone}/{len(samples)}, "
        f"silence {lit[0]} before / {len(tap) - 1 - lit[-1]} after",
    )


def test_without_a_tap_the_helper_sends_no_rendered_frames(rig: _Rig) -> None:
    """The cost is zero when echo cancellation is off: no RENDERED frame ever crosses the pipe."""
    generation = rig.speak(_tone(0.2))
    rig.presented(generation)
    time.sleep(0.1)
    assert 0x85 not in rig.frame_types
    assert 0x82 in rig.frame_types  # reports did flow, so the recorder is live


def test_a_stalled_tap_drops_blocks_and_never_stalls_the_render_thread(tmp_path: Path) -> None:
    """The daemon stops reading: pipe and ring fill, blocks drop (counted), rendering goes on."""
    made = _Rig(tmp_path, tap=True)
    release = threading.Event()
    stalled = made.tapped.append

    def stalling_tap(block: np.ndarray, _rate: int, _at: int) -> None:
        stalled(block.copy())
        release.wait(5)

    made.player._playback_tap = stalling_tap  # noqa: SLF001
    assert made.player.start().started
    try:
        time.sleep(3.5)  # past the pipe (~0.3 s) and the 1.4 s ring
        assert made.rendered().size > 3.0 * RATE  # the render thread never waited
        release.set()
        made.until(lambda: made.player.rendered_dropped_samples > 0, "a counted drop")
        dropped = made.player.rendered_dropped_samples
        assert dropped > 0
        assert dropped % 512 == 0  # whole blocks
        print(f"A1 tap stall: {dropped} samples dropped, rendered {made.rendered().size}")  # noqa: T201
    finally:
        release.set()
        made.player.stop()
