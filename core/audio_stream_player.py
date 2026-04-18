"""Persistent-stream PCM player — replaces per-sentence subprocess playback.

The legacy path (``TTSEngine._play_audio_file``) spawns ``afplay`` /
``mpv`` / ``ffplay`` per sentence, paying a fresh subprocess + audio-HAL
init cost each time (~30-150ms). It also relies on ``SIGSTOP`` for
soft-stop, which triggers a CoreAudio underrun loop-tail on macOS.

This module replaces both with a single long-lived
:class:`sounddevice.OutputStream` plus a lockless PCM ring buffer. Gain
ducking is applied sample-accurately inside the PortAudio callback —
no SIGSTOP, no subprocess restart, zero inter-sentence gap.

Architecture
------------

::

    main thread                      PortAudio callback thread
    ───────────                      ────────────────────────
    TTS → miniaudio decode
        ↓
    soxr resample to stream rate
        ↓
    AudioStreamPlayer.write(pcm) ─►  _callback() every blocksize samples:
    set_gain(target, ramp_ms)          RingBuffer.read_into(out)
                                       GainRamp.apply(out)
                                       → out of speaker

Modules
-------
``RingBuffer``   SPSC lockless float32 ring (numpy backing, power-of-2 mask)
``GainRamp``     Sample-accurate linear gain ramp, applied in callback
``AudioStreamPlayer``  Owns the stream, exposes write / duck / drain / flush

Pitfall mitigations (see inline ``[pitfall]`` tags):
  - No allocation in callback — all buffers preallocated
  - No locks in callback — SPSC ring is lockless, gain state is read-only
  - Idle feeds silence, not "stop writing" (prevents drift; sd #384)
  - ``output_underflow`` counted for watchdog (device-change recovery)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

LOGGER = logging.getLogger(__name__)


# =====================================================================
# RingBuffer — SPSC lockless float32 ring
# =====================================================================


class RingBuffer:
    """Single-producer single-consumer lockless ring of float32 samples.

    Contract: exactly ONE thread calls ``write``, and ONE (different)
    thread calls ``read_into``. The write index is owned by the writer
    thread; the read index by the reader. CPython guarantees int
    assignment is atomic under the GIL, so no mutex is required.

    Capacity is rounded up to a power of 2 so wrap-around is a bit-AND
    (``idx & mask``) instead of a modulo — ~3x faster on hot paths.

    Underrun policy: if the reader asks for more samples than available,
    the tail is zero-padded. Silence is the right thing to output when
    we've got nothing.
    """

    def __init__(self, size_samples: int) -> None:
        # Round up to the next power of 2 so wrap is (idx & mask).
        n = 1
        while n < size_samples:
            n <<= 1
        self._size = n
        self._mask = n - 1
        self._buf = np.zeros(n, dtype=np.float32)
        # Write and read indices are monotonically increasing ints.
        # The actual buffer slot is (idx & mask). Since Python ints are
        # arbitrary precision, we never overflow.
        self._write_idx = 0
        self._read_idx = 0

    # ------- reader-side (callback thread) -------

    def available_read(self) -> int:
        return self._write_idx - self._read_idx

    def read_into(self, out: np.ndarray, n: int) -> int:
        """Copy up to ``n`` samples into ``out[:n]``; zero-pad shortfall.

        Returns the count of REAL (non-padded) samples read. Caller
        (the callback) uses this to detect underruns.
        """
        avail = self.available_read()
        actual = min(n, avail)
        if actual > 0:
            ri = self._read_idx & self._mask
            end = ri + actual
            if end <= self._size:
                out[:actual] = self._buf[ri:end]
            else:
                # Wrap: split copy into two contiguous halves
                first = self._size - ri
                out[:first] = self._buf[ri:]
                out[first:actual] = self._buf[: actual - first]
            self._read_idx += actual
        if actual < n:
            # Zero-pad on underrun → silence, not garbage.
            out[actual:n] = 0.0
        return actual

    # ------- writer-side (main thread) -------

    def available_write(self) -> int:
        return self._size - (self._write_idx - self._read_idx)

    def write(self, data: np.ndarray) -> int:
        """Copy as much of ``data`` as fits; return samples accepted."""
        n = min(len(data), self.available_write())
        if n == 0:
            return 0
        wi = self._write_idx & self._mask
        end = wi + n
        if end <= self._size:
            self._buf[wi:end] = data[:n]
        else:
            first = self._size - wi
            self._buf[wi:] = data[:first]
            self._buf[: n - first] = data[first:n]
        self._write_idx += n
        return n

    def reset(self) -> None:
        """Reset both indices. Both threads must be stopped before calling."""
        self._write_idx = 0
        self._read_idx = 0


# =====================================================================
# GainRamp — sample-accurate linear gain with preallocated scratch
# =====================================================================


class GainRamp:
    """Linear gain ramp, applied inside the PortAudio callback.

    State:
      ``_current``  — gain we're outputting right now
      ``_target``   — gain we want to end up at
      ``_remaining``— samples left until we hit target (0 = steady)

    ``set_target`` is called from the main thread; ``apply`` from the
    callback. The two writes to ``_target`` and ``_remaining`` are not
    atomic as a pair — worst case the callback reads a stale pair and
    applies a slightly wrong ramp for one block (~5ms). Acceptable.

    Scratch buffers are preallocated to avoid numpy allocation in the
    callback hot path (``[pitfall]`` — alloc in callback causes
    GC/GIL pauses that trigger underruns).
    """

    def __init__(self, max_block_size: int = 4096) -> None:
        self._current: float = 1.0
        self._target: float = 1.0
        self._remaining: int = 0  # samples of ramp left
        # Preallocated scratch for computing per-sample gain values
        self._scratch = np.empty(max_block_size, dtype=np.float32)
        # Preallocated integer ramp [0, 1, 2, ...] as float for vectorized math
        self._arange = np.arange(max_block_size, dtype=np.float32)

    @property
    def current(self) -> float:
        return self._current

    def set_target(self, target: float, ramp_samples: int) -> None:
        """Schedule a ramp from current gain to ``target`` over N samples."""
        self._target = float(target)
        self._remaining = max(0, int(ramp_samples))
        # If user asked for ramp=0, snap instantly
        if self._remaining == 0:
            self._current = self._target

    def apply(self, pcm_block: np.ndarray) -> None:
        """Multiply ``pcm_block`` in place by the current gain (possibly ramping)."""
        n = len(pcm_block)
        if self._remaining == 0:
            # Steady-state: single multiply or no-op.
            if self._current != 1.0:
                pcm_block *= self._current
            return

        # Ramp in progress. Compute how many samples of this block are inside
        # the ramp, and what gain we reach at block end.
        step = min(n, self._remaining)
        # linear interpolation: fraction of full ramp completed by end of step
        frac_end = step / self._remaining
        next_gain = self._current + (self._target - self._current) * frac_end

        # Fill scratch[:step] with ramp values from current (at index 0) to
        # next_gain (at index step-1). Key: denominator is ``step - 1`` so the
        # LAST ramp sample exactly equals next_gain — no ~1/step jump to the
        # steady-state tail. Using ``step`` as the denominator leaves scratch
        # ending at ``current + (step-1)/step * delta`` and the tail-samples
        # multiplier at ``next_gain``, which differ by (delta/step) and sound
        # like a faint pop on fast ramps (e.g. 10ms unduck at 48kHz).
        if step == 1:
            scratch = self._scratch[:1]
            scratch[0] = next_gain
        else:
            slope = (next_gain - self._current) / (step - 1)
            scratch = self._scratch[:step]
            np.multiply(self._arange[:step], slope, out=scratch)
            scratch += self._current
        pcm_block[:step] *= scratch

        # If block was longer than remaining ramp, tail gets the new steady gain.
        # scratch[step-1] already equals next_gain, so this is continuous.
        if step < n:
            pcm_block[step:] *= next_gain

        self._current = next_gain
        self._remaining -= step
        if self._remaining == 0:
            # Snap to target to kill any float drift
            self._current = self._target


# =====================================================================
# AudioStreamPlayer — session-wide coordinator
# =====================================================================


class AudioStreamPlayer:
    """Persistent-stream PCM player with duckable gain.

    Lifecycle
    ---------
    ``start()`` opens one :class:`sounddevice.OutputStream` and keeps it
    open for the session. ``stop()`` closes it. Inside that window,
    ``write(pcm)`` feeds samples and ``duck()`` / ``unduck()`` change
    gain smoothly. Both are safe to call from any non-callback thread.

    Sample rate
    -----------
    Streams open at ``sample_rate``. Callers must resample inbound
    audio to this rate before ``write()``. We do not auto-resample —
    doing it in the callback would be an allocation + compute pitfall.

    Idle behavior
    -------------
    When ``write()`` is not called, the ring drains and the callback
    zero-pads. Silence keeps being emitted — that's intentional
    (``[pitfall]`` sd #384: "stop feeding" causes clock drift).

    Underflow watchdog
    ------------------
    The callback counts ``output_underflow`` occurrences. If they spike
    (e.g. user swaps headphones mid-playback), an external watchdog
    thread can call ``restart()`` to close + reopen the stream.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 1,
        ring_seconds: float = 2.0,
        blocksize: int = 0,
        latency: str | float = "low",
        device: Any | None = None,
    ) -> None:
        if channels != 1:
            raise NotImplementedError("only mono supported for now")
        self._sample_rate = int(sample_rate)
        self._channels = channels
        self._ring = RingBuffer(int(sample_rate * ring_seconds))
        self._gain = GainRamp(max_block_size=4096)
        self._blocksize = int(blocksize)
        self._latency = latency
        self._device = device

        self._stream: sd.OutputStream | None = None
        self._underflow_count = 0
        self._callback_calls = 0
        # Drain signaling — main thread can wait() on this event to know
        # when the ring has emptied (e.g. end of a sentence).
        self._drained = threading.Event()
        self._drained.set()  # starts "empty" → drained is True
        # Abort signaling — set by ``abort()``/``flush()``, cleared by the
        # next ``write()`` call. In-flight write/drain loops poll this and
        # return early. Without it, a pipeline.abort that flushes the ring
        # is silently undone by the play worker continuing to feed the
        # remaining PCM of the current sentence into the newly-empty ring.
        self._abort = threading.Event()
        # Monotonic count of samples that crossed the output stream. Updated
        # in the callback with the number of REAL (non-padded) samples read
        # from the ring. External readers (WP5 truncation) use this to
        # compute fraction-played.
        self._played_samples: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._stream is not None:
            return
        # [pitfall] On Mac, opening Output before Input can steal the
        # device's preferred sample rate. Callers who also open an
        # InputStream should start that FIRST, or specify both rates.
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="float32",
            blocksize=self._blocksize,  # 0 = PortAudio picks host-optimal
            latency=self._latency,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        LOGGER.info(
            "AudioStreamPlayer started: %dHz ch=%d blocksize=%s latency=%s",
            self._sample_rate, self._channels, self._blocksize, self._latency,
        )

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            LOGGER.warning("stream close error (ignored): %s", exc)
        self._stream = None
        self._ring.reset()
        self._played_samples = 0
        self._drained.set()
        LOGGER.info(
            "AudioStreamPlayer stopped; lifetime callbacks=%d underflows=%d",
            self._callback_calls, self._underflow_count,
        )

    def restart(self) -> None:
        """Close + reopen — used by watchdog when device change detected."""
        LOGGER.warning("AudioStreamPlayer restart (likely device change)")
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Write API (main thread)
    # ------------------------------------------------------------------

    def write(self, pcm: np.ndarray, wait_if_full: bool = True,
              timeout_s: float = 10.0) -> None:
        """Feed mono float32 PCM samples into the ring.

        Blocks and retries if the ring is full (the caller usually wants
        back-pressure so that ``write()`` returning means "committed to
        playback queue"). ``timeout_s`` guards against the callback
        having stopped (device error) so we don't hang forever.

        Clears the ``_abort`` event at entry so that any stale abort from
        a previous sentence doesn't short-circuit this call. Checks
        ``_abort`` on each ring-full retry so a mid-write abort exits
        promptly (abort-while-playing is the common case).
        """
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        if pcm.ndim > 1:
            pcm = pcm.reshape(-1)
        self._drained.clear()
        self._abort.clear()
        deadline = time.monotonic() + timeout_s
        offset = 0
        while offset < len(pcm):
            if self._abort.is_set():
                # Abort signaled mid-write (e.g. interrupt keyword fired
                # while we were still feeding the tail of a long sentence).
                # Drop remaining PCM and return — ring was flushed by
                # whoever set the abort, so nothing will play from it.
                return
            written = self._ring.write(pcm[offset:])
            offset += written
            if offset >= len(pcm):
                break
            if not wait_if_full:
                LOGGER.warning("ring full, dropping %d samples", len(pcm) - offset)
                return
            if time.monotonic() > deadline:
                LOGGER.warning("write timeout, dropping %d samples", len(pcm) - offset)
                return
            time.sleep(0.01)

    def flush(self) -> None:
        """Discard any queued PCM immediately (for abort / hard-stop).

        Also sets the abort event so any in-flight ``write()``/``drain()``
        exits early instead of continuing to push PCM into the freshly
        flushed ring. Without this, abort has no real effect on the
        currently-playing sentence — the play worker just keeps feeding
        until the sentence's PCM is exhausted.
        """
        self._ring.reset()
        self._drained.set()
        self._abort.set()

    def drain(self, timeout_s: float = 30.0) -> bool:
        """Block until the ring is empty (or timeout). Returns True if drained,
        False if aborted or timed out. Does NOT clear abort — an abort set
        during write() must still short-circuit drain.
        """
        deadline = time.monotonic() + timeout_s
        while self._ring.available_read() > 0:
            if self._abort.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Give the callback time to consume. 10ms is well below any
            # reasonable block size so we don't over-sleep.
            time.sleep(min(0.01, remaining))
        self._drained.set()
        return True

    # ------------------------------------------------------------------
    # Gain / ducking
    # ------------------------------------------------------------------

    def set_gain(self, target: float, ramp_ms: float = 30.0) -> None:
        """Smoothly ramp current gain to ``target`` over ``ramp_ms``."""
        ramp_samples = int(self._sample_rate * ramp_ms / 1000.0)
        self._gain.set_target(target, ramp_samples)

    def duck(self, volume: float = 0.3, ramp_ms: float = 30.0) -> None:
        """Shortcut for ``set_gain(volume, ramp_ms)``. Use on user speech."""
        self.set_gain(volume, ramp_ms)

    def unduck(self, ramp_ms: float = 10.0) -> None:
        """Restore gain to 1.0. Use when user stops speaking."""
        self.set_gain(1.0, ramp_ms)

    @property
    def current_gain(self) -> float:
        return self._gain.current

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def underflow_count(self) -> int:
        return self._underflow_count

    @property
    def callback_calls(self) -> int:
        return self._callback_calls

    @property
    def is_running(self) -> bool:
        return self._stream is not None and self._stream.active

    @property
    def played_samples(self) -> int:
        """Monotonic count of real (non-padded) samples written to the output.
        Resets only on stop(). Used by WP5 truncation."""
        return self._played_samples

    # ------------------------------------------------------------------
    # Callback — runs on PortAudio thread, keep it tight
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int,
                  time_info: Any, status: sd.CallbackFlags) -> None:
        """PortAudio calls this whenever it needs ``frames`` samples.

        Strictly no-alloc: outdata is preallocated by PortAudio. We
        copy from the ring, apply gain in-place, done.
        """
        self._callback_calls += 1
        if status:
            # ``status`` is a bitmask of CallbackFlags; underflow means
            # we couldn't feed fast enough (CPU, GIL contention, or
            # device glitch). Watchdog reads _underflow_count.
            if status.output_underflow:
                self._underflow_count += 1

        # outdata shape is (frames, channels). We're mono → flatten view.
        view = outdata[:, 0] if outdata.ndim > 1 else outdata
        # Read-into zero-pads on underrun, so view is always fully written.
        actual = self._ring.read_into(view, frames)
        self._played_samples += actual  # only real samples, not zero-padded
        self._gain.apply(view)
