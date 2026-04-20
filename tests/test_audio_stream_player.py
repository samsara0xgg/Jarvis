"""Unit tests for ``core.audio_stream_player``.

Tests split by module:
    RingBuffer          — write / read / wrap-around / underrun zero-pad
    GainRamp            — instant set / mid-ramp stop / full ramp / tail
    AudioStreamPlayer   — lifecycle + public API (sd mocked)

The player's sd.OutputStream is mocked so tests don't need real audio
hardware. Real-device behavior is covered by scripts/bench_voice_pipeline.py
(``--only live_sigstop_probe``).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.audio_stream_player import AudioStreamPlayer, GainRamp, RingBuffer


# =====================================================================
# RingBuffer
# =====================================================================


class TestRingBuffer:
    def test_size_rounds_up_to_power_of_2(self):
        rb = RingBuffer(1000)
        assert rb._size == 1024

    def test_exact_power_of_2_unchanged(self):
        assert RingBuffer(512)._size == 512
        assert RingBuffer(1024)._size == 1024

    def test_round_trip_simple(self):
        rb = RingBuffer(64)
        data = np.arange(10, dtype=np.float32)
        assert rb.write(data) == 10
        assert rb.available_read() == 10
        out = np.empty(10, dtype=np.float32)
        assert rb.read_into(out, 10) == 10
        assert np.array_equal(out, data)
        assert rb.available_read() == 0

    def test_wrap_around(self):
        rb = RingBuffer(8)  # size=8 exactly
        # Fill 6, read 4, write 6 → last 2 samples wrap to indices 0,1
        rb.write(np.arange(6, dtype=np.float32))
        out = np.empty(4, dtype=np.float32)
        rb.read_into(out, 4)
        rb.write(np.arange(100, 106, dtype=np.float32))
        # Remaining: [4,5] + [100..105] = 8 samples
        rem = np.empty(8, dtype=np.float32)
        rb.read_into(rem, 8)
        expected = np.concatenate(
            [np.array([4, 5], dtype=np.float32), np.arange(100, 106, dtype=np.float32)]
        )
        assert np.array_equal(rem, expected)

    def test_underrun_zero_pads(self):
        rb = RingBuffer(16)
        rb.write(np.ones(4, dtype=np.float32))
        out = np.empty(8, dtype=np.float32)
        n_real = rb.read_into(out, 8)
        assert n_real == 4
        assert np.array_equal(out[:4], np.ones(4, dtype=np.float32))
        assert np.array_equal(out[4:], np.zeros(4, dtype=np.float32))

    def test_write_full_returns_partial(self):
        rb = RingBuffer(8)
        rb.write(np.ones(8, dtype=np.float32))  # full
        assert rb.available_write() == 0
        assert rb.write(np.ones(4, dtype=np.float32)) == 0  # nothing fits

    def test_reset_clears_indices(self):
        rb = RingBuffer(8)
        rb.write(np.ones(4, dtype=np.float32))
        rb.reset()
        assert rb.available_read() == 0
        assert rb.available_write() == 8


# =====================================================================
# GainRamp
# =====================================================================


class TestGainRamp:
    def test_default_is_unity(self):
        g = GainRamp()
        block = np.ones(10, dtype=np.float32)
        g.apply(block)
        assert np.array_equal(block, np.ones(10, dtype=np.float32))

    def test_instant_set_snaps(self):
        g = GainRamp()
        g.set_target(0.5, 0)  # zero ramp → snap
        assert g.current == 0.5
        block = np.ones(10, dtype=np.float32)
        g.apply(block)
        assert np.allclose(block, 0.5)

    def test_full_ramp_within_one_block(self):
        g = GainRamp()
        g.set_target(0.0, 10)  # fade to 0 over 10 samples
        block = np.ones(10, dtype=np.float32)
        g.apply(block)
        # Linear ramp from 1.0 (at index 0) to 0.0 (at index 9) — endpoint
        # is INCLUSIVE so last sample is exactly next_gain. 10 values:
        # [1.0, 8/9, 7/9, ..., 1/9, 0.0]
        expected = 1.0 - np.arange(10, dtype=np.float32) / 9.0
        assert np.allclose(block, expected)
        assert g.current == 0.0

    def test_ramp_shorter_than_block_tail_uses_new_gain(self):
        """Ramp for 4 samples, block is 10 — last 6 at target gain, no step."""
        g = GainRamp()
        g.set_target(0.0, 4)
        block = np.ones(10, dtype=np.float32)
        g.apply(block)
        # First 4: ramp 1.0 → 0.0 inclusive (denom=3): [1, 2/3, 1/3, 0]
        # Last 6: target 0.0. Last ramp sample already 0 → continuous.
        expected = np.concatenate([
            np.array([1.0, 2/3, 1/3, 0.0], dtype=np.float32),
            np.zeros(6, dtype=np.float32),
        ])
        assert np.allclose(block, expected)

    def test_ramp_end_matches_target_exactly(self):
        """Regression: scratch[step-1] must equal next_gain (no pop on unduck).

        Prior to the (step-1) denominator fix, the ramp ended ~1/step short
        of target, then the steady tail multiplied by the exact target —
        a faint audible pop on 10ms ramps at 48kHz. This asserts the
        no-discontinuity property directly.
        """
        g = GainRamp()
        g.set_target(1.0, 480)  # 10ms unduck at 48kHz
        block = np.ones(256, dtype=np.float32)  # typical CoreAudio block
        g.apply(block)
        block2 = np.ones(256, dtype=np.float32)
        g.apply(block2)
        # By end of block2, the 480-sample ramp is done. The last sample
        # of the ramp-region in block2 must equal target (1.0), making the
        # tail (block2[224:256]) continuous with it.
        # Sample 223 is the last ramp sample; it should be == 1.0.
        assert np.isclose(block2[223], 1.0, atol=1e-5), (
            f"Last ramp sample should equal next_gain=1.0, got {block2[223]}"
        )
        # And the tail at 224 is also 1.0 (steady). Continuous transition.
        assert np.isclose(block2[224], 1.0, atol=1e-5)

    def test_partial_ramp_across_multiple_blocks(self):
        """10-sample ramp fed as 2x 5-sample blocks continues correctly."""
        g = GainRamp()
        g.set_target(0.0, 10)
        block1 = np.ones(5, dtype=np.float32)
        g.apply(block1)
        # First block of the ramp: 5 samples go from 1.0 to 0.5 (halfway point).
        # With denom=step-1=4, slope=-0.125: [1.0, 0.875, 0.75, 0.625, 0.5]
        assert np.allclose(block1, [1.0, 0.875, 0.75, 0.625, 0.5])
        assert np.isclose(g.current, 0.5)
        assert g._remaining == 5

        block2 = np.ones(5, dtype=np.float32)
        g.apply(block2)
        # Block2 finishes the ramp to 0.0 (denom=4): [0.5, 0.375, 0.25, 0.125, 0.0]
        assert np.allclose(block2, [0.5, 0.375, 0.25, 0.125, 0.0])
        assert g.current == 0.0

    def test_retargeting_mid_ramp_redirects(self):
        """If target changes mid-ramp, the ramp turns around from current state."""
        g = GainRamp()
        g.set_target(0.0, 10)
        block1 = np.ones(5, dtype=np.float32)
        g.apply(block1)  # now current=0.5
        # Mid-flight, change mind: ramp back up to 1.0 over 5 more samples
        g.set_target(1.0, 5)
        block2 = np.ones(5, dtype=np.float32)
        g.apply(block2)
        # 0.5 → 1.0 over 5 samples (denom=4): [0.5, 0.625, 0.75, 0.875, 1.0]
        assert np.allclose(block2, [0.5, 0.625, 0.75, 0.875, 1.0])


# =====================================================================
# AudioStreamPlayer
# =====================================================================


@pytest.fixture
def mock_sd():
    """Patch sounddevice.OutputStream to a MagicMock; yield the mock."""
    with patch("core.audio_stream_player.sd") as mock:
        mock.OutputStream.return_value = MagicMock()
        mock.OutputStream.return_value.active = True
        mock.CallbackFlags = MagicMock()
        yield mock


class TestAudioStreamPlayer:
    def test_start_opens_stream(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000, channels=1, ring_seconds=1.0)
        assert not p.is_running
        p.start()
        mock_sd.OutputStream.assert_called_once()
        kwargs = mock_sd.OutputStream.call_args.kwargs
        assert kwargs["samplerate"] == 48000
        assert kwargs["channels"] == 1
        assert kwargs["dtype"] == "float32"
        assert callable(kwargs["callback"])

    def test_start_idempotent(self, mock_sd):
        p = AudioStreamPlayer()
        p.start()
        p.start()
        assert mock_sd.OutputStream.call_count == 1

    def test_stop_closes_stream(self, mock_sd):
        p = AudioStreamPlayer()
        p.start()
        stream = p._stream
        p.stop()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        assert p._stream is None

    def test_write_fills_ring(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        pcm = np.ones(1024, dtype=np.float32)
        p.write(pcm, wait_if_full=False)
        assert p._ring.available_read() == 1024

    def test_flush_clears_ring(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        p.write(np.ones(1024, dtype=np.float32), wait_if_full=False)
        p.flush()
        assert p._ring.available_read() == 0

    def test_duck_sets_gain_target_below_1(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000)
        p.start()
        p.duck(0.3, ramp_ms=30)
        assert p._gain._target == 0.3
        # ramp_samples = 48000 * 30 / 1000 = 1440
        assert p._gain._remaining == 1440

    def test_unduck_targets_1(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000)
        p.start()
        p.duck(0.3)
        p.unduck(ramp_ms=10)
        assert p._gain._target == 1.0
        assert p._gain._remaining == 480  # 48000 * 10 / 1000

    def test_set_gain_zero_ramp_snaps(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000)
        p.start()
        p.set_gain(0.5, ramp_ms=0)
        assert p._gain.current == 0.5
        assert p._gain._remaining == 0

    def test_callback_reads_ring_and_applies_gain(self, mock_sd):
        """End-to-end callback behavior: data from ring + gain → outdata."""
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        p.write(np.ones(256, dtype=np.float32), wait_if_full=False)
        p.set_gain(0.5, ramp_ms=0)  # instant

        outdata = np.zeros((256, 1), dtype=np.float32)
        # status with no underflow (MagicMock bool-ish)
        status = MagicMock()
        status.output_underflow = False
        p._callback(outdata, 256, None, status)

        # All 256 samples should be 0.5 (ring had 1.0, gain 0.5 applied)
        assert np.allclose(outdata[:, 0], 0.5)
        # ring should be empty now
        assert p._ring.available_read() == 0

    def test_callback_zero_pads_underrun(self, mock_sd):
        """When ring has fewer samples than asked, tail is silence."""
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        p.write(np.ones(100, dtype=np.float32), wait_if_full=False)
        outdata = np.zeros((256, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = False
        p._callback(outdata, 256, None, status)
        assert np.allclose(outdata[:100, 0], 1.0)
        assert np.allclose(outdata[100:, 0], 0.0)

    def test_callback_counts_underflow(self, mock_sd):
        p = AudioStreamPlayer()
        p.start()
        outdata = np.zeros((128, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = True
        p._callback(outdata, 128, None, status)
        assert p.underflow_count == 1

    def test_drain_returns_true_when_empty(self, mock_sd):
        p = AudioStreamPlayer()
        p.start()
        assert p.drain(timeout_s=0.5) is True

    def test_drain_times_out_when_not_drained(self, mock_sd):
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        # Fill ring; callback is mocked so nothing drains.
        p.write(np.ones(1024, dtype=np.float32), wait_if_full=False)
        assert p.drain(timeout_s=0.2) is False

    def test_flush_sets_abort_so_drain_exits_early(self, mock_sd):
        """Regression: flush must signal in-flight drain to return.

        Without this, a concurrent flush (from pipeline.abort) would
        empty the ring but a subsequent drain call could still block.
        """
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        p.write(np.ones(1024, dtype=np.float32), wait_if_full=False)
        p.flush()  # empties ring + sets _abort
        assert p._ring.available_read() == 0
        # drain returns immediately (ring already empty after flush)
        assert p.drain(timeout_s=0.5) is True
        # and _abort is set, visible to anyone checking
        assert p._abort.is_set()

    def test_abort_mid_write_exits_loop(self, mock_sd):
        """Regression: write must poll abort so abort-while-playing stops.

        Simulates the hot path: _play_worker is in write() pushing the
        tail of a long sentence when pipeline.abort fires. Without the
        abort check in write's retry loop, write keeps refilling the
        just-flushed ring and audio keeps playing.
        """
        import threading

        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=0.05)  # tiny ring
        p.start()

        # Large PCM that won't fit in the tiny ring → write() will loop
        # waiting for callback to drain (which never happens with mock sd).
        big_pcm = np.ones(200_000, dtype=np.float32)

        # Abort from a sidecar thread after a short delay
        def _abort_later() -> None:
            time.sleep(0.2)
            p.flush()  # flush sets _abort

        t = threading.Thread(target=_abort_later, daemon=True)
        t.start()
        t0 = time.perf_counter()
        p.write(big_pcm)  # should return early, not hang 10s
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"write did not respect abort (took {elapsed:.2f}s)"
        t.join(timeout=1)

    def test_write_clears_stale_abort(self, mock_sd):
        """A fresh write() call starts clean — stale abort from prior flush
        shouldn't short-circuit it.
        """
        p = AudioStreamPlayer(sample_rate=48000, ring_seconds=1.0)
        p.start()
        p.flush()  # leaves _abort set
        assert p._abort.is_set()
        # Fresh write should clear it and push samples
        p.write(np.ones(1024, dtype=np.float32), wait_if_full=False)
        assert not p._abort.is_set()
        assert p._ring.available_read() == 1024

    # ------------------------------------------------------------------
    # on_first_chunk callback tests
    # ------------------------------------------------------------------

    def test_first_chunk_fires_on_first_real_audio(self, mock_sd):
        """Callback fires once when _callback reads non-silent samples."""
        fired = []
        p = AudioStreamPlayer(
            sample_rate=48000, ring_seconds=1.0,
            on_first_chunk=lambda: fired.append(1),
        )
        p.start()
        p.reset_first_chunk()
        p.write(np.ones(256, dtype=np.float32), wait_if_full=False)

        outdata = np.zeros((256, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = False
        p._callback(outdata, 256, None, status)

        assert fired == [1], "callback must fire exactly once on first real chunk"

    def test_first_chunk_no_fire_without_write(self, mock_sd):
        """Constraint 4: no-TTS turn — callback never fires if write() is never called.

        In jarvis, turns that skip TTS (farewell shortcut, memory_l1 direct answers)
        must leave ttfs_ms = None. This test verifies the player never fires the hook
        when the ring stays empty, so the caller's None is not overwritten.
        """
        fired = []
        p = AudioStreamPlayer(
            sample_rate=48000, ring_seconds=1.0,
            on_first_chunk=lambda: fired.append(1),
        )
        p.start()
        p.reset_first_chunk()
        # Do NOT call write() — simulates a no-TTS turn.

        # Drive several empty callback invocations (ring stays empty → actual == 0).
        outdata = np.zeros((256, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = False
        for _ in range(4):
            p._callback(outdata, 256, None, status)

        assert fired == [], "callback must NOT fire when no audio was written"

    def test_reset_allows_callback_to_fire_again(self, mock_sd):
        """Per-turn reset: after reset_first_chunk(), the callback fires again."""
        fired = []
        p = AudioStreamPlayer(
            sample_rate=48000, ring_seconds=1.0,
            on_first_chunk=lambda: fired.append(1),
        )
        p.start()
        outdata = np.zeros((256, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = False

        # Turn 1
        p.reset_first_chunk()
        p.write(np.ones(256, dtype=np.float32), wait_if_full=False)
        p._callback(outdata, 256, None, status)
        assert len(fired) == 1, "turn 1: fired once"

        # Turn 2 — without reset the flag stays True; with reset it clears.
        p.reset_first_chunk()
        p.write(np.ones(256, dtype=np.float32), wait_if_full=False)
        p._callback(outdata, 256, None, status)
        assert len(fired) == 2, "turn 2: fired again after reset"

    def test_first_chunk_fires_only_once_per_turn(self, mock_sd):
        """The callback fires exactly once even across multiple _callback invocations."""
        fired = []
        p = AudioStreamPlayer(
            sample_rate=48000, ring_seconds=1.0,
            on_first_chunk=lambda: fired.append(1),
        )
        p.start()
        p.reset_first_chunk()

        status = MagicMock()
        status.output_underflow = False

        # Write enough for three callback rounds.
        p.write(np.ones(768, dtype=np.float32), wait_if_full=False)
        for _ in range(3):
            outdata = np.zeros((256, 1), dtype=np.float32)
            p._callback(outdata, 256, None, status)

        assert fired == [1], "callback must fire exactly once per turn regardless of chunk count"

    def test_exception_in_callback_does_not_crash_audio_thread(self, mock_sd):
        """A buggy on_first_chunk must not stall or crash the audio stream.

        The player must keep producing audio (outdata filled correctly) even
        when the user's callback raises an exception.
        """
        def bad_callback() -> None:
            raise RuntimeError("user bug")

        p = AudioStreamPlayer(
            sample_rate=48000, ring_seconds=1.0,
            on_first_chunk=bad_callback,
        )
        p.start()
        p.reset_first_chunk()
        p.write(np.ones(256, dtype=np.float32), wait_if_full=False)

        outdata = np.zeros((256, 1), dtype=np.float32)
        status = MagicMock()
        status.output_underflow = False

        # Must not raise; audio data must still be filled correctly.
        p._callback(outdata, 256, None, status)
        assert np.allclose(outdata[:, 0], 1.0), "audio output intact despite callback exception"
        # Flag flipped — exception happened after try, so check it was set.
        assert p._first_chunk_fired, "flag must be set even when callback raised"
