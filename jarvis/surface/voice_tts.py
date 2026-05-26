"""L5 voice TTS pipeline — MiniMax streaming WS + PortAudio playback.

ADR-0005 §4.2 / §5.3 / §10 (F6-F7 fallback chain).

Layer rules: imports only stdlib, third-party (`websockets`, `sounddevice`,
`numpy`), `jarvis.shared`, and `jarvis.state.event_log`. Does NOT name
`jarvis.decision`, `jarvis.execution`, `jarvis.deployment`,
`jarvis.runtime`, or `jarvis.cli`.

This file lands in 4 commits per ADR-0005 §14:
  Task 13: _preprocess_for_speech (this one)
  Task 14: AudioStreamPlayer
  Task 15: MiniMaxWSClient + MiniMaxUnavailableError
  Task 16: TTSPipeline + macos_say_fallback
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger(__name__)


# --- Text preprocessor (ported inline from legacy core/tts_preprocessor.py) ---

# Emoji + symbol Unicode ranges that should be stripped from TTS input.
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000027bf"  # dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator (flags)
    "]+",
    flags=re.UNICODE,
)

_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def _preprocess_for_speech(text: str) -> str:
    """Strip TTS-hostile chars (emoji, markdown markers) before synthesis.

    Verbatim behavior port of legacy `core/tts_preprocessor.py`. Does NOT
    apply content safety; the Pre-emit Gate has already vetted the text.
    """
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _MARKDOWN_BOLD_RE.sub(r"\1", out)
    out = _MARKDOWN_LINK_RE.sub(r"\1", out)
    out = _MARKDOWN_ITALIC_RE.sub(r"\1", out)
    return out.strip()


# --- Audio stream playback (ported from legacy core/audio_stream_player.py) ---
#
# Persistent-stream PCM player — replaces per-sentence subprocess playback. A
# single long-lived ``sounddevice.OutputStream`` plus a lockless SPSC float32
# ring buffer carries audio; gain ducking is applied sample-accurately inside
# the PortAudio callback so user-speech ducking has no inter-sentence gap.
#
# Two intentional deviations from the legacy port (see ADR-0005 §14 Task 14):
#   1. ``sounddevice`` is lazy-imported in ``_open_output_stream`` so tests can
#      construct an :class:`AudioStreamPlayer` without PortAudio installed.
#   2. The OutputStream itself is opened lazily on the first ``write()`` call
#      (``lazy_open=True`` default). This lets unit tests probe the bytes /
#      gain API surface without ever touching the real audio device.


class _RingBuffer:
    """Single-producer single-consumer lockless ring of float32 samples.

    Capacity rounds up to the next power of 2 so wrap-around is a bit-AND.
    Underrun policy: short reads zero-pad — silence is the right output when
    we've got nothing. Ported verbatim from the legacy
    ``core/audio_stream_player.RingBuffer``.
    """

    def __init__(self, size_samples: int) -> None:
        n = 1
        while n < size_samples:
            n <<= 1
        self._size = n
        self._mask = n - 1
        self._buf = np.zeros(n, dtype=np.float32)
        self._write_idx = 0
        self._read_idx = 0

    def available_read(self) -> int:
        return self._write_idx - self._read_idx

    def available_write(self) -> int:
        return self._size - (self._write_idx - self._read_idx)

    def read_into(self, out: np.ndarray, n: int) -> int:
        avail = self.available_read()
        actual = min(n, avail)
        if actual > 0:
            ri = self._read_idx & self._mask
            end = ri + actual
            if end <= self._size:
                out[:actual] = self._buf[ri:end]
            else:
                first = self._size - ri
                out[:first] = self._buf[ri:]
                out[first:actual] = self._buf[: actual - first]
            self._read_idx += actual
        if actual < n:
            out[actual:n] = 0.0
        return actual

    def write(self, data: np.ndarray) -> int:
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
        self._write_idx = 0
        self._read_idx = 0


class _GainRamp:
    """Linear gain ramp applied inside the PortAudio callback.

    Scratch buffers are preallocated to avoid numpy allocation on the audio
    hot path. Ported verbatim from the legacy
    ``core/audio_stream_player.GainRamp``.
    """

    def __init__(self, max_block_size: int = 4096) -> None:
        self._current: float = 1.0
        self._target: float = 1.0
        self._remaining: int = 0
        self._scratch = np.empty(max_block_size, dtype=np.float32)
        self._arange = np.arange(max_block_size, dtype=np.float32)

    @property
    def current(self) -> float:
        return self._current

    def set_target(self, target: float, ramp_samples: int) -> None:
        self._target = float(target)
        self._remaining = max(0, int(ramp_samples))
        if self._remaining == 0:
            self._current = self._target

    def apply(self, pcm_block: np.ndarray) -> None:
        n = len(pcm_block)
        if self._remaining == 0:
            if self._current != 1.0:
                pcm_block *= self._current
            return

        step = min(n, self._remaining)
        frac_end = step / self._remaining
        next_gain = self._current + (self._target - self._current) * frac_end

        if step == 1:
            scratch = self._scratch[:1]
            scratch[0] = next_gain
        else:
            slope = (next_gain - self._current) / (step - 1)
            scratch = self._scratch[:step]
            np.multiply(self._arange[:step], slope, out=scratch)
            scratch += self._current
        pcm_block[:step] *= scratch

        if step < n:
            pcm_block[step:] *= next_gain

        self._current = next_gain
        self._remaining -= step
        if self._remaining == 0:
            self._current = self._target


def _open_output_stream(  # noqa: PLR0913 — passthrough to sd.OutputStream
    *,
    sample_rate_hz: int,
    channels: int,
    blocksize: int,
    latency: str | float,
    device: Any | None,  # noqa: ANN401
    callback: Callable[..., None],
) -> Any:  # noqa: ANN401
    """Open a PortAudio output stream.

    Module-level so tests can :func:`unittest.mock.patch.object` it without
    needing PortAudio installed. ``sounddevice`` is imported lazily so the
    surrounding module stays importable in environments where it isn't
    available (CI, headless test runners).
    """
    import sounddevice as sd  # type: ignore[import-not-found]  # noqa: PLC0415

    return sd.OutputStream(
        samplerate=sample_rate_hz,
        channels=channels,
        dtype="float32",
        blocksize=blocksize,
        latency=latency,
        device=device,
        callback=callback,
    )


class AudioStreamPlayer:
    """Persistent-stream PCM player with sample-accurate duckable gain.

    Public surface required by ADR-0005 §4.2:

    * ``write(pcm: bytes)`` — feed float32 mono PCM bytes
    * ``bytes_pending()`` — queued bytes not yet played
    * ``flush()`` — drop everything queued (abort)
    * ``duck(target_gain, ramp_ms)`` — ramp gain down for user speech
    * ``current_gain()`` — instantaneous output gain
    * ``close()`` — close the OutputStream

    The OutputStream is opened lazily (``lazy_open=True`` default): construction
    does NOT touch PortAudio, and ``write()`` only feeds the ring buffer.
    Production callers invoke :meth:`start` once (or pass ``lazy_open=False`` at
    construction) before they expect audio to actually leave the speaker. This
    lets unit tests exercise the bytes / gain API surface without sounddevice
    installed.
    """

    _BYTES_PER_SAMPLE = 4  # float32 mono

    def __init__(  # noqa: PLR0913 — keyword-only audio + lifecycle config
        self,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 1,
        ring_seconds: float = 2.0,
        blocksize: int = 0,
        latency: str | float = "low",
        device: Any | None = None,  # noqa: ANN401
        on_first_chunk: Callable[[], None] | None = None,
        lazy_open: bool = True,
    ) -> None:
        """Construct an idle player; does not open the OutputStream by default."""
        if channels != 1:
            msg = "only mono supported for now"
            raise NotImplementedError(msg)
        self._sample_rate_hz = int(sample_rate_hz)
        self._channels = channels
        self._ring = _RingBuffer(int(sample_rate_hz * ring_seconds))
        self._gain = _GainRamp(max_block_size=4096)
        self._blocksize = int(blocksize)
        self._latency = latency
        self._device = device

        self._stream: Any | None = None
        self._underflow_count = 0
        self._callback_calls = 0
        self._drained = threading.Event()
        self._drained.set()
        self._abort = threading.Event()
        self._played_samples: int = 0
        self._on_first_chunk: Callable[[], None] | None = on_first_chunk
        self._first_chunk_fired: bool = False

        if not lazy_open:
            self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the PortAudio OutputStream if not already running."""
        if self._stream is not None:
            return
        self._stream = _open_output_stream(
            sample_rate_hz=self._sample_rate_hz,
            channels=self._channels,
            blocksize=self._blocksize,
            latency=self._latency,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        LOGGER.info(
            "AudioStreamPlayer started: %dHz ch=%d blocksize=%s latency=%s",
            self._sample_rate_hz,
            self._channels,
            self._blocksize,
            self._latency,
        )

    def stop(self) -> None:
        """Stop and close the OutputStream. Safe to call repeatedly."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("stream close error (ignored): %s", exc)
        self._stream = None
        self._ring.reset()
        self._played_samples = 0
        self._drained.set()
        LOGGER.info(
            "AudioStreamPlayer stopped; lifetime callbacks=%d underflows=%d",
            self._callback_calls,
            self._underflow_count,
        )

    def close(self) -> None:
        """Alias for :meth:`stop` — matches ADR-0005 §4.2 surface."""
        self.stop()

    def restart(self) -> None:
        """Close + reopen — used by watchdog when device change detected."""
        LOGGER.warning("AudioStreamPlayer restart (likely device change)")
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def write(
        self,
        pcm: bytes,
        *,
        wait_if_full: bool = True,
        timeout_s: float = 10.0,
    ) -> None:
        """Feed mono float32 PCM ``bytes`` into the ring.

        Does NOT open the OutputStream — callers must :meth:`start` once
        (or construct with ``lazy_open=False``) before they expect audio
        to actually leave the speaker. The ring still accepts samples
        even when the stream is closed; this is what lets unit tests
        exercise the bytes / gain API without touching PortAudio.

        Blocks until every byte is committed to the ring, unless
        ``wait_if_full=False`` or ``timeout_s`` elapses. Clears any stale
        abort signal at entry; re-checks it on each ring-full retry so a
        mid-write abort exits promptly.
        """
        samples = np.frombuffer(pcm, dtype=np.float32)
        self._drained.clear()
        self._abort.clear()
        deadline = time.monotonic() + timeout_s
        offset = 0
        while offset < len(samples):
            if self._abort.is_set():
                return
            written = self._ring.write(samples[offset:])
            offset += written
            if offset >= len(samples):
                break
            if not wait_if_full:
                LOGGER.warning("ring full, dropping %d samples", len(samples) - offset)
                return
            if time.monotonic() > deadline:
                LOGGER.warning("write timeout, dropping %d samples", len(samples) - offset)
                return
            time.sleep(0.01)

    def bytes_pending(self) -> int:
        """Queued bytes not yet read by the PortAudio callback."""
        return self._ring.available_read() * self._BYTES_PER_SAMPLE

    def flush(self) -> None:
        """Drop every queued sample and signal in-flight writes to bail."""
        self._ring.reset()
        self._drained.set()
        self._abort.set()

    def drain(self, timeout_s: float = 30.0) -> bool:
        """Block until the ring is empty (or timeout/abort). Returns True if drained."""
        deadline = time.monotonic() + timeout_s
        while self._ring.available_read() > 0:
            if self._abort.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        self._drained.set()
        return True

    # ------------------------------------------------------------------
    # Gain / ducking
    # ------------------------------------------------------------------

    def set_gain(self, target: float, ramp_ms: float = 30.0) -> None:
        """Smoothly ramp current gain to ``target`` over ``ramp_ms``."""
        ramp_samples = int(self._sample_rate_hz * ramp_ms / 1000.0)
        self._gain.set_target(target, ramp_samples)

    def duck(self, target_gain: float = 0.3, ramp_ms: int = 30) -> None:
        """Ramp gain down to ``target_gain`` over ``ramp_ms`` (user-speech ducking)."""
        self.set_gain(target_gain, float(ramp_ms))

    def unduck(self, ramp_ms: float = 10.0) -> None:
        """Restore gain to 1.0. Use when user stops speaking."""
        self.set_gain(1.0, ramp_ms)

    def current_gain(self) -> float:
        """Instantaneous output gain (post-ramp tick)."""
        return self._gain.current

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def underflow_count(self) -> int:
        """Lifetime PortAudio output-underflow callbacks (watchdog signal)."""
        return self._underflow_count

    @property
    def callback_calls(self) -> int:
        """Lifetime PortAudio callback invocations (liveness signal)."""
        return self._callback_calls

    @property
    def is_running(self) -> bool:
        """True iff the OutputStream is open and PortAudio reports it active."""
        return self._stream is not None and self._stream.active

    @property
    def played_samples(self) -> int:
        """Monotonic count of real samples written to the output. Resets on stop()."""
        return self._played_samples

    def reset_first_chunk(self) -> None:
        """Re-arm the first-chunk callback for the next TTS turn."""
        self._first_chunk_fired = False

    # ------------------------------------------------------------------
    # Callback — runs on PortAudio thread, keep it tight
    # ------------------------------------------------------------------

    def _callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,  # noqa: ANN401, ARG002
        status: Any,  # noqa: ANN401
    ) -> None:
        """PortAudio calls this when it needs ``frames`` samples.

        No allocation on the hot path: ``outdata`` is preallocated by
        PortAudio; we read from the ring (which zero-pads on underrun), then
        apply gain in place.
        """
        self._callback_calls += 1
        if status and getattr(status, "output_underflow", False):
            self._underflow_count += 1

        view = outdata[:, 0] if outdata.ndim > 1 else outdata
        actual = self._ring.read_into(view, frames)
        self._played_samples += actual

        if actual > 0 and not self._first_chunk_fired and self._on_first_chunk is not None:
            # never crash the audio thread due to caller bugs
            with contextlib.suppress(Exception):
                self._on_first_chunk()
            self._first_chunk_fired = True

        self._gain.apply(view)


__all__ = ["AudioStreamPlayer", "_preprocess_for_speech"]
