"""L5 voice TTS pipeline — MiniMax streaming WS + PortAudio playback.

ADR-0005 §4.2 / §5.3 / §10 (F6-F7 fallback chain).

Layer rules: imports only stdlib, third-party (`websockets`, `sounddevice`,
`numpy`), `jarvis.shared`, and `jarvis.state.event_log`. Does NOT name
`jarvis.decision`, `jarvis.execution`, `jarvis.deployment`,
`jarvis.runtime`, or `jarvis.cli`.

This file lands in 4 commits per ADR-0005 §14:
  Task 13: _preprocess_for_speech
  Task 14: AudioStreamPlayer
  Task 15: MiniMaxWSClient + MiniMaxUnavailableError (this one)
  Task 16: TTSPipeline + macos_say_fallback
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.surface import voice_ducking

from jarvis.shared.realtime_trace import (
    TraceValue,
    realtime_trace_context,
    record_realtime_trace,
)
from jarvis.surface.voice_ledger import (
    AcceptedSamples,
    AudibilityClass,
    ForegroundBusy,
    GenerationLease,
    OutputTimelineSnapshot,
    PlaybackLedger,
    StalePlaybackGeneration,
)

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


# --- Voice / document tag extraction (spec §3.6.6 structured response) -----
#
# Render-layer emits structured responses with ``<voice>...</voice>`` (spoken)
# and ``<document>...</document>`` (displayed) regions in one chunk stream.
# Without filtering, the pipeline synthesises tag characters literally
# (garbled "less-than voice greater-than" speech) and the document body bleeds
# into TTS. Bug 3 fix from post-ADR-0005 smoke.

_VOICE_REGION_RE = re.compile(r"<voice>(.*?)</voice>", re.DOTALL)
_UNCLOSED_VOICE_RE = re.compile(r"<voice>(.*)\Z", re.DOTALL)
_DOCUMENT_REGION_RE = re.compile(r"<document>.*?</document>", re.DOTALL)


def _extract_voice_content(text: str) -> str:
    """Return the concatenated content inside ``<voice>...</voice>`` regions.

    - When the joined chunk stream contains zero ``<voice>`` tags, returns
      ``text`` unchanged (legacy compat for plain-text unit fixtures);
      isolated ``<document>`` content yields ``""`` so the pipeline does
      not speak displayed-only content.
    - When the stream contains one or more closed ``<voice>...</voice>``
      regions, returns their joined inner text.
    - When the stream contains an *unclosed* ``<voice>`` (truncated mid-
      region), returns everything from ``<voice>`` to end. The TTSPipeline
      flushes on close OR ``end_turn``; the latter is the path that hits
      this case.
    Nested ``<document>`` regions inside the matched voice content are
    stripped before return so a misordered surface emission can't leak
    document chars into TTS.
    """
    if "<voice>" not in text:
        if "<document>" in text:
            return ""
        return text
    parts = _VOICE_REGION_RE.findall(text)
    if not parts:
        m = _UNCLOSED_VOICE_RE.search(text)
        if m is None:
            return ""
        parts = [m.group(1)]
    joined = "".join(parts)
    joined = _DOCUMENT_REGION_RE.sub("", joined)
    return joined.strip()


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


class _GenerationRingBuffer:
    """Preallocated SPSC PCM ring with generation/cursor metadata.

    The actor owns ``_write_idx`` and the callback owns ``_read_idx``.
    Interrupt never rewinds either cursor.  It publishes a monotonic discard
    boundary which the callback consumes before its next copy, avoiding the
    reset-vs-callback race that can leak N into N+1.
    """

    def __init__(self, size_samples: int) -> None:
        n = 1
        while n < size_samples:
            n <<= 1
        self._size = n
        self._mask = n - 1
        self._pcm = np.zeros(n, dtype=np.float32)
        self._generation = np.full(n, -1, dtype=np.int64)
        self._cursor = np.zeros(n, dtype=np.int64)
        self._write_idx = 0
        self._read_idx = 0
        self._discard_before_idx = 0

    def available_read(self) -> int:
        return self._write_idx - max(self._read_idx, self._discard_before_idx)

    def available_write(self) -> int:
        # Capacity uses the callback-owned read cursor, not the requested
        # discard boundary: the writer never overwrites slots a callback may
        # still be copying.
        return self._size - (self._write_idx - self._read_idx)

    def write(
        self,
        data: np.ndarray,
        *,
        generation: int,
        output_start_cursor: int,
    ) -> int:
        """Publish as much generation-tagged PCM as currently fits."""
        n = min(len(data), self.available_write())
        if n <= 0:
            return 0
        wi = self._write_idx & self._mask
        end = wi + n
        cursors = np.arange(
            output_start_cursor,
            output_start_cursor + n,
            dtype=np.int64,
        )
        if end <= self._size:
            self._pcm[wi:end] = data[:n]
            self._generation[wi:end] = generation
            self._cursor[wi:end] = cursors
        else:
            first = self._size - wi
            self._pcm[wi:] = data[:first]
            self._generation[wi:] = generation
            self._cursor[wi:] = cursors[:first]
            self._pcm[: n - first] = data[first:n]
            self._generation[: n - first] = generation
            self._cursor[: n - first] = cursors[first:n]
        self._write_idx += n
        return n

    def read_into(
        self,
        pcm_out: np.ndarray,
        generation_out: np.ndarray,
        cursor_out: np.ndarray,
        n: int,
    ) -> int:
        """Copy a callback block and zero-pad without allocating."""
        self._read_idx = max(self._read_idx, self._discard_before_idx)
        available = self._write_idx - self._read_idx
        actual = min(n, available)
        if actual > 0:
            ri = self._read_idx & self._mask
            end = ri + actual
            if end <= self._size:
                pcm_out[:actual] = self._pcm[ri:end]
                generation_out[:actual] = self._generation[ri:end]
                cursor_out[:actual] = self._cursor[ri:end]
            else:
                first = self._size - ri
                pcm_out[:first] = self._pcm[ri:]
                generation_out[:first] = self._generation[ri:]
                cursor_out[:first] = self._cursor[ri:]
                pcm_out[first:actual] = self._pcm[: actual - first]
                generation_out[first:actual] = self._generation[: actual - first]
                cursor_out[first:actual] = self._cursor[: actual - first]
            self._read_idx += actual
        if actual < n:
            pcm_out[actual:n] = 0.0
            generation_out[actual:n] = -1
            cursor_out[actual:n] = 0
        return actual

    def request_discard(self) -> int:
        """Publish a kill boundary without mutating the callback cursor."""
        self._discard_before_idx = self._write_idx
        return self._discard_before_idx


@dataclass(frozen=True)
class _CallbackReport:
    generation: int
    output_start_cursor: int
    output_end_cursor: int
    audibility_class: AudibilityClass
    callback_monotonic_ns: int
    presentation_delay_ns: int
    first_for_generation: bool


_AUDIBILITY_CODE: dict[AudibilityClass, int] = {
    "normal": 0,
    "attenuated": 1,
    "muted": 2,
    "unknown": 3,
}


class _CallbackReportRing:
    """Bounded callback-to-media-actor SPSC report ring."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._generation = np.zeros(capacity, dtype=np.int64)
        self._start = np.zeros(capacity, dtype=np.int64)
        self._end = np.zeros(capacity, dtype=np.int64)
        self._audibility = np.zeros(capacity, dtype=np.int8)
        self._callback_ns = np.zeros(capacity, dtype=np.int64)
        self._delay_ns = np.zeros(capacity, dtype=np.int64)
        self._first = np.zeros(capacity, dtype=np.bool_)
        self._write_idx = 0
        self._read_idx = 0
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def write(  # noqa: PLR0913 - fixed preallocated callback report shape
        self,
        *,
        generation: int,
        output_start_cursor: int,
        output_end_cursor: int,
        audibility_class: AudibilityClass,
        callback_monotonic_ns: int,
        presentation_delay_ns: int,
        first_for_generation: bool,
    ) -> None:
        """Write from the callback, dropping rather than blocking when full."""
        if self._write_idx - self._read_idx >= self._capacity:
            self._dropped += 1
            return
        slot = self._write_idx % self._capacity
        self._generation[slot] = generation
        self._start[slot] = output_start_cursor
        self._end[slot] = output_end_cursor
        self._audibility[slot] = _AUDIBILITY_CODE[audibility_class]
        self._callback_ns[slot] = callback_monotonic_ns
        self._delay_ns[slot] = presentation_delay_ns
        self._first[slot] = first_for_generation
        self._write_idx += 1

    def drain(self) -> list[_CallbackReport]:
        """Drain on the media owner; allocation is outside the callback."""
        reports: list[_CallbackReport] = []
        labels: tuple[AudibilityClass, ...] = (
            "normal",
            "attenuated",
            "muted",
            "unknown",
        )
        while self._read_idx < self._write_idx:
            slot = self._read_idx % self._capacity
            reports.append(
                _CallbackReport(
                    generation=int(self._generation[slot]),
                    output_start_cursor=int(self._start[slot]),
                    output_end_cursor=int(self._end[slot]),
                    audibility_class=labels[int(self._audibility[slot])],
                    callback_monotonic_ns=int(self._callback_ns[slot]),
                    presentation_delay_ns=int(self._delay_ns[slot]),
                    first_for_generation=bool(self._first[slot]),
                ),
            )
            self._read_idx += 1
        return reports


class _PCMCommitGate:
    """Linearize close-generation invalidation with ring-index publication.

    The gate is held only for one non-blocking ring write, never for the
    player's ring-full wait loop. The PortAudio consumer never takes this
    lock: it observes either the old ``_write_idx`` or the fully-published new
    value, while close waits only for the short producer publication.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False

    def invalidate(self) -> int:
        """Close the current generation and return the new generation."""
        with self._lock:
            self._closed = True
            self._generation += 1
            return self._generation

    def publish(self, generation: int, publish: Callable[[], int]) -> int | None:
        """Run one ring publish iff current; ``None`` means invalidated."""
        with self._lock:
            if self._closed or generation != self._generation:
                return None
            return publish()

    def is_current(self, generation: int) -> bool:
        """Return whether ``generation`` may still publish."""
        with self._lock:
            return not self._closed and generation == self._generation


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

    @property
    def is_normal(self) -> bool:
        """Return whether the whole next block is guaranteed unity gain."""
        return self._current == 1.0 and self._target == 1.0 and self._remaining == 0

    def _next_audibility_class(self) -> AudibilityClass:
        """Classify the next post-gain block before advancing the ramp."""
        if self.is_normal:
            return "normal"
        if self._current == 0.0 and self._target == 0.0 and self._remaining == 0:
            return "muted"
        return "attenuated"

    def set_target(self, target: float, ramp_samples: int) -> None:
        self._target = float(target)
        self._remaining = max(0, int(ramp_samples))
        if self._remaining == 0:
            self._current = self._target

    def apply(self, pcm_block: np.ndarray) -> AudibilityClass:
        """Apply the owned ramp and return the block's conservative class."""
        audibility_class = self._next_audibility_class()
        n = len(pcm_block)
        if self._remaining == 0:
            if self._current != 1.0:
                pcm_block *= self._current
            return audibility_class

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
        return audibility_class


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
    import sounddevice as sd  # noqa: PLC0415

    return sd.OutputStream(
        samplerate=sample_rate_hz,
        channels=channels,
        dtype="float32",
        blocksize=blocksize,
        latency=latency,
        device=device,
        callback=callback,
    )


@dataclass(frozen=True)
class PlayerStartResult:
    """Exact output-device start disposition."""

    status: Literal["started", "already_started", "failed_closed", "uncertain"]
    attempt_id: int
    reason: str

    @property
    def started(self) -> bool:
        """Return whether one current OutputStream is proven open."""
        return self.status in {"started", "already_started"}


@dataclass(frozen=True)
class PlayerStopResult:
    """Monotonic output-device close result with explicit ownership debt."""

    status: Literal["closed", "already_closed", "uncertain"]
    attempt_id: int
    reason: str
    helper_thread_alive: bool = False

    @property
    def definitively_closed(self) -> bool:
        """Return true only after the exact stream handle's close returned."""
        return self.status in {"closed", "already_closed"}


@dataclass
class _PlayerCloseAttempt:
    """One serialized close attempt against one exact physical stream."""

    attempt_id: int
    ownership_attempt_id: int
    stream: Any
    done: threading.Event
    stop_done: threading.Event
    close_error: BaseException | None = None
    stop_error: BaseException | None = None
    stop_helper_alive: bool = False


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
    _RECENT_TOMBSTONE_LIMIT = 4096
    _PENDING_AUDIBLE_LIMIT = 8192
    _DEFAULT_LIFECYCLE_TIMEOUT_S = 2.0
    # The largest output latency this system will act on.  A host reporting
    # more is clamped here, never replaced by something smaller: an unbounded
    # value would push the audible horizon past every deadline so
    # `fully_presented` never becomes true.
    _MAX_PLAUSIBLE_OUTPUT_LATENCY_S = 1.0
    _STOP_STAGE_WAIT_S = 0.05

    def __init__(  # noqa: PLR0913, PLR0915 — explicit audio/lifecycle state
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
        generation_safe: bool = False,
        callback_max_frames: int = 4096,
        estimated_output_latency_s: float = 0.12,
    ) -> None:
        """Construct an idle player; does not open the OutputStream by default."""
        if channels != 1:
            msg = "only mono supported for now"
            raise NotImplementedError(msg)
        self._sample_rate_hz = int(sample_rate_hz)
        self._channels = channels
        ring_samples = int(sample_rate_hz * ring_seconds)
        self._ring = _RingBuffer(ring_samples)
        self._generation_ring = _GenerationRingBuffer(ring_samples) if generation_safe else None
        self._gain = _GainRamp(max_block_size=callback_max_frames)
        # External threads publish immutable latest-wins commands.  Only the
        # PortAudio callback mutates/advances `_GainRamp`, closing the old
        # classify-before-apply race without putting a lock on the hot path.
        self._gain_command: tuple[float, int] = (1.0, 0)
        self._gain_consumed_command = self._gain_command
        self._blocksize = int(blocksize)
        self._latency = latency
        self._device = device

        self._stream: Any | None = None
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state: Literal[
            "closed",
            "opening",
            "open",
            "closing",
            "uncertain",
        ] = "closed"
        self._lifecycle_attempt_id = 0
        self._ownership_attempt_id = 0
        self._close_attempt: _PlayerCloseAttempt | None = None
        self._stop_before_final_cas_hook: Callable[[], None] | None = None
        self._underflow_count = 0
        self._callback_calls = 0
        self._drained = threading.Event()
        self._drained.set()
        self._abort = threading.Event()
        self._write_lock = threading.Lock()
        self._played_samples: int = 0
        self._on_first_chunk: Callable[[], None] | None = on_first_chunk
        self._first_chunk_fired: bool = False
        self._trace_attributes: dict[str, TraceValue] = {}
        self._trace_first_ring_accept_fired = False
        self._trace_first_callback_fired = False
        self._legacy_callback_report_pending = False
        self._legacy_first_chunk_pending = False
        self._generation_safe = generation_safe
        self._callback_max_frames = callback_max_frames
        self._callback_generations = np.full(callback_max_frames, -1, dtype=np.int64)
        self._callback_cursors = np.zeros(callback_max_frames, dtype=np.int64)
        self._callback_valid = np.zeros(callback_max_frames, dtype=np.bool_)
        self._callback_reports = _CallbackReportRing(capacity=2048)
        self._active_lease: GenerationLease | None = None
        self._next_generation = 0
        self._timeline_epoch = 0
        self._ledgers: dict[int, PlaybackLedger] = {}
        self._tombstoned_generations: set[int] = set()
        self._tombstone_order: deque[int] = deque()
        self._pending_audible: list[tuple[int, int, int]] = []
        self._default_output_latency_ns = max(
            0,
            int(estimated_output_latency_s * 1_000_000_000),
        )
        self._estimated_output_latency_ns = self._default_output_latency_ns
        self._callback_first_generation = -1
        self._callback_commit_generation = -1
        self._starvation_gaps = 0
        self._starvation_dry_generation = -1
        self._callback_report_drop_seen = 0
        self._presentation_horizon_coalesced = 0
        self._presentation_horizon_coalesced_seen = 0

        if not lazy_open:
            started = self.start()
            if not started.started:
                msg = f"output stream start failed: {started.reason}"
                raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> PlayerStartResult:  # noqa: PLR0911 - exact lifecycle outcomes
        """Open one OutputStream only when all earlier ownership is closed."""
        with self._lifecycle_lock:
            if self._lifecycle_state == "open":
                return PlayerStartResult(
                    "already_started",
                    self._ownership_attempt_id,
                    "already_open",
                )
            if self._lifecycle_state != "closed":
                return PlayerStartResult(
                    "uncertain",
                    self._ownership_attempt_id,
                    f"ownership_debt_{self._lifecycle_state}",
                )
            self._lifecycle_attempt_id += 1
            ownership_attempt_id = self._lifecycle_attempt_id
            self._ownership_attempt_id = ownership_attempt_id
            self._close_attempt = None
            self._lifecycle_state = "opening"
        try:
            stream = _open_output_stream(
                sample_rate_hz=self._sample_rate_hz,
                channels=self._channels,
                blocksize=self._blocksize,
                latency=self._latency,
                device=self._device,
                callback=self._callback,
            )
        except BaseException as exc:  # noqa: BLE001 - typed device boundary
            with self._lifecycle_lock:
                if (
                    self._lifecycle_state == "opening"
                    and self._ownership_attempt_id == ownership_attempt_id
                ):
                    self._lifecycle_state = "closed"
            named_device = f" device={self._device!r}" if self._device is not None else ""
            return PlayerStartResult(
                "failed_closed",
                ownership_attempt_id,
                f"open:{type(exc).__name__}{named_device}",
            )
        # PortAudio fills `latency` from Pa_GetStreamInfo() at open, so this is
        # read before `stream.start()` and can never race the realtime callback
        # that consumes `_estimated_output_latency_ns`.  A foreign host API may
        # report nothing or a degenerate 0.0; those carry no measurement and
        # fall back to the configured estimate.  A real report is never
        # replaced by a smaller number — an absurd one is clamped to the
        # ceiling.
        self._estimated_output_latency_ns = self._host_output_latency_ns(stream)
        with self._lifecycle_lock:
            if (
                self._lifecycle_state != "opening"
                or self._ownership_attempt_id != ownership_attempt_id
            ):
                self._stream = stream
                self._lifecycle_state = "uncertain"
                return PlayerStartResult(
                    "uncertain",
                    ownership_attempt_id,
                    "opening_identity_changed",
                )
            self._stream = stream
        try:
            stream.start()
        except BaseException as exc:  # noqa: BLE001 - retain exact close debt
            with self._lifecycle_lock:
                if self._ownership_attempt_id == ownership_attempt_id:
                    self._lifecycle_state = "uncertain"
            closed = self.stop(timeout_s=self._DEFAULT_LIFECYCLE_TIMEOUT_S)
            return PlayerStartResult(
                "failed_closed" if closed.definitively_closed else "uncertain",
                ownership_attempt_id,
                f"start:{type(exc).__name__};{closed.reason}",
            )
        with self._lifecycle_lock:
            if self._ownership_attempt_id != ownership_attempt_id:
                self._lifecycle_state = "uncertain"
                return PlayerStartResult(
                    "uncertain",
                    ownership_attempt_id,
                    "start_identity_changed",
                )
            self._lifecycle_state = "open"
        LOGGER.info(
            "AudioStreamPlayer started: %dHz ch=%d blocksize=%s latency=%s",
            self._sample_rate_hz,
            self._channels,
            self._blocksize,
            self._latency,
        )
        return PlayerStartResult("started", ownership_attempt_id, "stream_started")

    def stop(
        self,
        *,
        timeout_s: float | None = None,
    ) -> PlayerStopResult:
        """Stop software playback and prove or retain exact stream close debt."""
        self._terminalize_software_playback()
        timeout = (
            self._DEFAULT_LIFECYCLE_TIMEOUT_S
            if timeout_s is None
            else max(0.0, timeout_s)
        )
        deadline = time.monotonic() + timeout
        with self._lifecycle_lock:
            if self._lifecycle_state == "closed":
                return PlayerStopResult(
                    "already_closed",
                    self._ownership_attempt_id,
                    "already_closed",
                )
            if self._lifecycle_state == "opening":
                return PlayerStopResult(
                    "uncertain",
                    self._ownership_attempt_id,
                    "open_attempt_in_flight",
                    helper_thread_alive=True,
                )
            close_attempt = self._close_attempt
            # A returned close() is not enough to retire the exact attempt
            # while its stop helper still owns a call into the same foreign
            # stream.  Repeated/concurrent callers must join that attempt;
            # launching another helper here would call stop()/close() twice on
            # one OutputStream.
            if close_attempt is None or (
                close_attempt.done.is_set()
                and close_attempt.stop_done.is_set()
                and close_attempt.close_error is not None
            ):
                stream = self._stream
                if stream is None:
                    self._lifecycle_state = "uncertain"
                    return PlayerStopResult(
                        "uncertain",
                        self._ownership_attempt_id,
                        "stream_handle_missing_with_ownership_debt",
                    )
                self._lifecycle_attempt_id += 1
                close_attempt = _PlayerCloseAttempt(
                    attempt_id=self._lifecycle_attempt_id,
                    ownership_attempt_id=self._ownership_attempt_id,
                    stream=stream,
                    done=threading.Event(),
                    stop_done=threading.Event(),
                )
                self._close_attempt = close_attempt
                self._lifecycle_state = "closing"
                helper = threading.Thread(
                    target=self._close_exact_stream,
                    args=(close_attempt,),
                    name=f"jarvis-output-close-a{close_attempt.attempt_id}",
                    daemon=True,
                )
                try:
                    helper.start()
                except RuntimeError as exc:
                    close_attempt.close_error = exc
                    close_attempt.stop_helper_alive = False
                    self._lifecycle_state = "uncertain"
                    close_attempt.stop_done.set()
                    close_attempt.done.set()
            attempt_id = close_attempt.attempt_id
        if not close_attempt.done.wait(timeout=max(0.0, deadline - time.monotonic())):
            return PlayerStopResult(
                "uncertain",
                attempt_id,
                "close_timeout",
                helper_thread_alive=True,
            )
        if close_attempt.close_error is None and close_attempt.stop_helper_alive:
            close_attempt.stop_done.wait(
                timeout=max(0.0, deadline - time.monotonic()),
            )
        status, reason, stop_helper_alive = self._close_attempt_result(close_attempt)
        if status == "closed":
            LOGGER.info(
                "AudioStreamPlayer stopped; lifetime callbacks=%d underflows=%d",
                self._callback_calls,
                self._underflow_count,
            )
        return PlayerStopResult(
            status,
            attempt_id,
            reason,
            helper_thread_alive=stop_helper_alive,
        )

    def _close_attempt_result(
        self,
        attempt: _PlayerCloseAttempt,
    ) -> tuple[Literal["closed", "uncertain"], str, bool]:
        """Snapshot a late-helper-aware close result without stale narrowing."""
        with self._lifecycle_lock:
            if (
                attempt.close_error is None
                and not attempt.stop_helper_alive
                and self._lifecycle_state == "closed"
            ):
                reason = (
                    "close_returned_after_stop_error"
                    if attempt.stop_error is not None
                    else "close_returned"
                )
                return "closed", reason, False
            if attempt.close_error is not None:
                return (
                    "uncertain",
                    f"close:{type(attempt.close_error).__name__}",
                    attempt.stop_helper_alive,
                )
            return (
                "uncertain",
                "stop_helper_in_flight_after_close_return",
                attempt.stop_helper_alive,
            )

    def _close_exact_stream(self, attempt: _PlayerCloseAttempt) -> None:
        """Attempt stop and close once; close runs even if stop raises or hangs."""
        def _stop_stream() -> None:
            try:
                attempt.stream.stop()
            except BaseException as exc:  # noqa: BLE001 - recorded lifecycle debt
                attempt.stop_error = exc
            finally:
                with self._lifecycle_lock:
                    attempt.stop_helper_alive = False
                    hook = self._stop_before_final_cas_hook
                    if hook is not None:
                        hook()
                    if (
                        self._close_attempt is attempt
                        and attempt.close_error is None
                        and self._lifecycle_state == "uncertain"
                    ):
                        self._stream = None
                        self._lifecycle_state = "closed"
                    attempt.stop_done.set()

        stop_helper = threading.Thread(
            target=_stop_stream,
            name=f"jarvis-output-stop-a{attempt.attempt_id}",
            daemon=True,
        )
        try:
            attempt.stop_helper_alive = True
            stop_helper.start()
            attempt.stop_done.wait(timeout=self._STOP_STAGE_WAIT_S)
        except RuntimeError as exc:
            with self._lifecycle_lock:
                attempt.stop_helper_alive = False
                attempt.stop_error = exc
                attempt.stop_done.set()
        try:
            attempt.stream.close()
        except BaseException as exc:  # noqa: BLE001 - exact ownership retained
            attempt.close_error = exc
        finally:
            with self._lifecycle_lock:
                if self._close_attempt is attempt:
                    if attempt.close_error is None and not attempt.stop_helper_alive:
                        self._stream = None
                        self._lifecycle_state = "closed"
                    else:
                        self._lifecycle_state = "uncertain"
                attempt.done.set()

    def _terminalize_software_playback(self) -> None:
        """Invalidate buffered PCM independently of physical close proof."""
        if self._generation_ring is not None:
            if self._active_lease is not None:
                self._remember_tombstone(self._active_lease.playback_generation_id)
                self._active_lease = None
            with self._write_lock:
                self._generation_ring.request_discard()
        else:
            self._ring.reset()
        self._played_samples = 0
        self._drained.set()

    def close(self, *, timeout_s: float | None = None) -> PlayerStopResult:
        """Alias for :meth:`stop` — matches ADR-0005 §4.2 surface."""
        return self.stop(timeout_s=timeout_s)

    def restart(self, *, timeout_s: float | None = None) -> PlayerStartResult:
        """Close + reopen — used by watchdog when device change detected."""
        LOGGER.warning("AudioStreamPlayer restart (likely device change)")
        closed = self.stop(timeout_s=timeout_s)
        if not closed.definitively_closed:
            return PlayerStartResult(
                "uncertain",
                closed.attempt_id,
                f"restart_blocked:{closed.reason}",
            )
        return self.start()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def write(  # noqa: C901, PLR0913 — bounded wait plus atomic commit seam
        self,
        pcm: bytes,
        *,
        wait_if_full: bool = True,
        timeout_s: float = 10.0,
        cancel_event: threading.Event | None = None,
        commit_gate: _PCMCommitGate | None = None,
        generation: int = 0,
    ) -> int:
        """Feed mono float32 PCM ``bytes`` into the ring.

        Does NOT open the OutputStream — callers must :meth:`start` once
        (or construct with ``lazy_open=False``) before they expect audio
        to actually leave the speaker. The ring still accepts samples
        even when the stream is closed; this is what lets unit tests
        exercise the bytes / gain API without touching PortAudio.

        Returns the number of float32 samples accepted. Blocks until every
        byte is committed to the ring, unless
        ``wait_if_full=False`` or ``timeout_s`` elapses. Clears any stale
        abort signal at entry; re-checks it on each ring-full retry so a
        mid-write abort exits promptly.
        """
        samples = np.frombuffer(pcm, dtype=np.float32)
        # Initialization shares the publication lock with flush. A close that
        # sets ``cancel_event`` before taking this lock cannot have its abort
        # signal cleared by a writer that resumes after close.
        with self._write_lock:
            if cancel_event is not None and cancel_event.is_set():
                return 0
            self._drained.clear()
            self._abort.clear()
        deadline = time.monotonic() + timeout_s
        offset = 0
        while offset < len(samples):
            if self._abort.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return offset

            def _publish(start: int = offset) -> int:
                with self._write_lock:
                    if self._abort.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        return 0
                    return self._ring.write(samples[start:])

            published = (
                commit_gate.publish(generation, _publish) if commit_gate is not None else _publish()
            )
            if published is None:
                return offset
            written = published
            if written > 0 and not self._trace_first_ring_accept_fired:
                self._trace_first_ring_accept_fired = True
                record_realtime_trace(
                    "tts_first_pcm_accepted_to_ring",
                    measurement_semantics=("first_float32_samples_committed_to_software_ring"),
                    **self._trace_attributes,
                )
            offset += written
            if offset >= len(samples):
                break
            if not wait_if_full:
                LOGGER.warning("ring full, dropping %d samples", len(samples) - offset)
                return offset
            if time.monotonic() > deadline:
                LOGGER.warning("write timeout, dropping %d samples", len(samples) - offset)
                return offset
            time.sleep(0.01)
        return offset

    def activate_generation(
        self,
        *,
        session_id: str,
        response_id: str,
        response_group_id: str,
        turn_id: str,
    ) -> GenerationLease | ForegroundBusy:
        """Mint the next L5 playback lease when no foreground is active."""
        if not self._generation_safe or self._generation_ring is None:
            msg = "generation activation requires generation_safe=True"
            raise RuntimeError(msg)
        if self._active_lease is not None:
            return ForegroundBusy(active_lease=self._active_lease)
        self._next_generation += 1
        self._timeline_epoch += 1
        lease = GenerationLease(
            session_id=session_id,
            response_id=response_id,
            response_group_id=response_group_id,
            turn_id=turn_id,
            playback_generation_id=self._next_generation,
            timeline_epoch=self._timeline_epoch,
        )
        self._active_lease = lease
        self._ledgers[lease.playback_generation_id] = PlaybackLedger(
            lease,
            sample_rate=self._sample_rate_hz,
        )
        self._abort.clear()
        self._drained.clear()
        self._callback_first_generation = -1
        return lease

    def begin_generation_segment(
        self,
        *,
        expected_playback_generation_id: int,
        sequence: int,
        text: str,
        segment_hash: str,
    ) -> StalePlaybackGeneration | None:
        """Open one speech segment under the exact active generation."""
        lease = self._active_lease
        if lease is None or lease.playback_generation_id != expected_playback_generation_id:
            return self._stale(expected_playback_generation_id)
        self._ledgers[expected_playback_generation_id].begin_segment(
            sequence=sequence,
            text=text,
            segment_hash=segment_hash,
        )
        return None

    def write_generation(
        self,
        pcm: bytes,
        *,
        expected_playback_generation_id: int,
        segment_sequence: int,
    ) -> AcceptedSamples | StalePlaybackGeneration | None:
        """Publish one bounded PCM slice under generation CAS.

        ``None`` means the bounded ring is currently full and the async media
        owner must apply backpressure before retrying.  It is never a drop.
        """
        generation_ring = self._generation_ring
        lease = self._active_lease
        if (
            generation_ring is None
            or lease is None
            or lease.playback_generation_id != expected_playback_generation_id
        ):
            return self._stale(expected_playback_generation_id)
        samples = np.frombuffer(pcm, dtype=np.float32)
        if samples.size == 0:
            return None
        ledger = self._ledgers[expected_playback_generation_id]
        with self._write_lock:
            # The actor is the sole producer, but the explicit second check
            # preserves the CAS if shutdown installs a tombstone between the
            # caller's first check and ring publication.
            active = self._active_lease
            if active is None or active.playback_generation_id != expected_playback_generation_id:
                return self._stale(expected_playback_generation_id)
            written = generation_ring.write(
                samples,
                generation=expected_playback_generation_id,
                output_start_cursor=ledger.accepted_cursor,
            )
            if written <= 0:
                return None
            accepted = ledger.accept_samples(
                sequence=segment_sequence,
                sample_count=written,
            )
        if accepted.output_start_cursor == 0:
            record_realtime_trace(
                "tts_player_first_accept",
                response_id=lease.response_id,
                playback_generation_id=expected_playback_generation_id,
                segment_sequence=segment_sequence,
                accepted_samples=written,
                measurement_semantics=(
                    "first_generation_valid_float32_samples_accepted_by_software_player"
                ),
            )
        return accepted

    def finish_generation_segment(
        self,
        *,
        expected_playback_generation_id: int,
        sequence: int,
    ) -> StalePlaybackGeneration | None:
        """Finalize one independent segment resampler/timeline span."""
        lease = self._active_lease
        if lease is None or lease.playback_generation_id != expected_playback_generation_id:
            return self._stale(expected_playback_generation_id)
        self._ledgers[expected_playback_generation_id].finish_segment(sequence=sequence)
        return None

    def interrupt_generation(
        self,
        *,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration:
        """Atomically tombstone, then publish the unsubmitted discard boundary."""
        lease = self._active_lease
        if lease is None or lease.playback_generation_id != expected_playback_generation_id:
            return self._stale(expected_playback_generation_id)
        # Consume reports which linearized before this call, then publish the
        # tombstone. A callback already inside its final publication window is
        # exposed through `_callback_commit_generation`; the actor settles it
        # asynchronously before freezing/terminalizing this ledger.
        self.poll_presentation()
        self._remember_tombstone(expected_playback_generation_id)
        self._active_lease = None
        with self._write_lock:
            if self._generation_ring is not None:
                self._generation_ring.request_discard()
        ledger = self._ledgers[expected_playback_generation_id]
        ledger.mark_software_drained()
        self._drained.set()
        self.poll_presentation()
        return ledger.snapshot()

    def settle_interrupted_generation(
        self,
        *,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration | None:
        """Freeze a tombstoned ledger after every pre-CAS callback resolves.

        ``None`` is a bounded-poll signal: one callback which entered its
        publication window before the CAS still owns the decision whether its
        block reached the host. The async media actor yields and retries; the
        callback itself never waits.
        """
        if expected_playback_generation_id not in self._tombstoned_generations:
            return self._stale(expected_playback_generation_id)
        if self._callback_commit_generation == expected_playback_generation_id:
            return None
        self.poll_presentation()
        ledger = self._ledgers.get(expected_playback_generation_id)
        if ledger is None:
            return self._stale(expected_playback_generation_id)
        return ledger.freeze()

    def complete_generation(
        self,
        *,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration:
        """Close a naturally presented generation without resetting cursors."""
        lease = self._active_lease
        if lease is None or lease.playback_generation_id != expected_playback_generation_id:
            return self._stale(expected_playback_generation_id)
        snapshot = self.poll_generation(expected_playback_generation_id)
        if isinstance(snapshot, StalePlaybackGeneration):
            return snapshot
        if not snapshot.fully_presented:
            msg = "cannot complete playback before conservative presentation drain"
            raise RuntimeError(msg)
        self._active_lease = None
        self._remember_tombstone(expected_playback_generation_id)
        return self._ledgers[expected_playback_generation_id].freeze()

    def retire_generation(self, playback_generation_id: int) -> None:
        """Release completed ledger detail while retaining bounded stale identity."""
        lease = self._active_lease
        if lease is not None and lease.playback_generation_id == playback_generation_id:
            msg = "cannot retire the active playback generation"
            raise RuntimeError(msg)
        if self._callback_commit_generation == playback_generation_id:
            msg = "cannot retire a generation with an in-flight callback publication"
            raise RuntimeError(msg)
        self._ledgers.pop(playback_generation_id, None)
        self._pending_audible = [
            item for item in self._pending_audible if item[1] != playback_generation_id
        ]

    def _remember_tombstone(self, generation: int) -> None:
        """Keep a bounded recent terminal set for typed late-operation results."""
        if generation in self._tombstoned_generations:
            return
        self._tombstoned_generations.add(generation)
        self._tombstone_order.append(generation)
        if len(self._tombstone_order) > self._RECENT_TOMBSTONE_LIMIT:
            oldest = self._tombstone_order.popleft()
            self._tombstoned_generations.discard(oldest)

    def poll_presentation(self) -> None:  # noqa: C901, PLR0912 - two backend ledgers
        """Consume callback reports and advance conservative horizons."""
        if self._generation_ring is None:
            if self._legacy_callback_report_pending:
                self._legacy_callback_report_pending = False
                record_realtime_trace(
                    "audio_output_first_nonzero_callback",
                    measurement_semantics=("portaudio_callback_buffer_submission_not_dac_audible"),
                    **self._trace_attributes,
                )
            if self._legacy_first_chunk_pending:
                self._legacy_first_chunk_pending = False
                if self._on_first_chunk is not None:
                    with contextlib.suppress(Exception):
                        self._on_first_chunk()
            return
        for report in self._callback_reports.drain():
            ledger = self._ledgers.get(report.generation)
            if ledger is None:
                continue
            ledger.record_submitted(
                output_start_cursor=report.output_start_cursor,
                output_end_cursor=report.output_end_cursor,
                audibility_class=report.audibility_class,
            )
            due_ns = report.callback_monotonic_ns + report.presentation_delay_ns
            if len(self._pending_audible) >= self._PENDING_AUDIBLE_LIMIT:
                # A later callback horizon implies the earlier cursor has also
                # crossed its (earlier) estimate. Retaining only the later
                # report delays accounting and is therefore conservative.
                self._pending_audible.pop(0)
                self._presentation_horizon_coalesced += 1
            self._pending_audible.append(
                (due_ns, report.generation, report.output_end_cursor),
            )
            if report.first_for_generation:
                record_realtime_trace(
                    "audio_output_first_nonzero_callback",
                    response_id=ledger.lease.response_id,
                    playback_generation_id=report.generation,
                    frames_submitted=(report.output_end_cursor - report.output_start_cursor),
                    measurement_semantics=("portaudio_callback_buffer_submission_not_dac_audible"),
                )
                if self._on_first_chunk is not None:
                    with contextlib.suppress(Exception):
                        self._on_first_chunk()
        now_ns = time.monotonic_ns()
        remaining: list[tuple[int, int, int]] = []
        for due_ns, generation, cursor in self._pending_audible:
            ledger = self._ledgers.get(generation)
            if ledger is None:
                continue
            if due_ns <= now_ns:
                ledger.record_audible(
                    output_cursor=cursor,
                    cursor_quality="estimated",
                )
            else:
                remaining.append((due_ns, generation, cursor))
        self._pending_audible = remaining
        active = self._active_lease
        if active is not None and self.bytes_pending() == 0:
            self._ledgers[active.playback_generation_id].mark_software_drained()
        dropped = self._callback_reports.dropped
        if dropped > self._callback_report_drop_seen:
            record_realtime_trace(
                "audio_callback_report_overflow",
                dropped_reports=dropped - self._callback_report_drop_seen,
                capacity=2048,
            )
            self._callback_report_drop_seen = dropped
        coalesced = self._presentation_horizon_coalesced
        if coalesced > self._presentation_horizon_coalesced_seen:
            record_realtime_trace(
                "audio_presentation_horizon_coalesced",
                coalesced_reports=(coalesced - self._presentation_horizon_coalesced_seen),
                capacity=self._PENDING_AUDIBLE_LIMIT,
                measurement_semantics="later_estimated_horizon_retained_conservatively",
            )
            self._presentation_horizon_coalesced_seen = coalesced

    def poll_generation(
        self,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration:
        """Return the latest generation snapshot after consuming reports."""
        self.poll_presentation()
        ledger = self._ledgers.get(expected_playback_generation_id)
        if ledger is None:
            return self._stale(expected_playback_generation_id)
        return ledger.snapshot()

    @property
    def active_lease(self) -> GenerationLease | None:
        """Return the current L5 write lease."""
        return self._active_lease

    def _stale(self, expected: int) -> StalePlaybackGeneration:
        active = self._active_lease
        return StalePlaybackGeneration(
            expected_playback_generation_id=expected,
            active_playback_generation_id=(
                active.playback_generation_id if active is not None else None
            ),
            reason=(
                "already_terminal"
                if expected in self._tombstoned_generations
                else "stale_generation"
            ),
        )

    def bytes_pending(self) -> int:
        """Queued bytes not yet read by the PortAudio callback."""
        ring = self._generation_ring if self._generation_ring is not None else self._ring
        return ring.available_read() * self._BYTES_PER_SAMPLE

    def flush(self) -> None:
        """Drop every queued sample and signal in-flight writes to bail."""
        if self._generation_ring is not None:
            msg = (
                "generation-safe playback requires interrupt_generation() "
                "with the expected playback_generation_id"
            )
            raise RuntimeError(msg)
        self._abort.set()
        with self._write_lock:
            self._ring.reset()
            self._drained.set()

    def drain(self, timeout_s: float = 30.0) -> bool:
        """Block until the ring is empty (or timeout/abort). Returns True if drained."""
        deadline = time.monotonic() + timeout_s
        ring = self._generation_ring if self._generation_ring is not None else self._ring
        while ring.available_read() > 0:
            self.poll_presentation()
            if self._abort.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        self._drained.set()
        self.poll_presentation()
        return True

    # ------------------------------------------------------------------
    # Gain / ducking
    # ------------------------------------------------------------------

    def set_gain(self, target: float, ramp_ms: float = 30.0) -> None:
        """Publish a latest-wins ramp command for callback-thread consumption."""
        ramp_samples = int(self._sample_rate_hz * ramp_ms / 1000.0)
        self._gain_command = (float(target), max(0, ramp_samples))

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

    def _host_output_latency_ns(self, stream: Any) -> int:  # noqa: ANN401 - foreign device handle
        """Return the host's output-latency estimate, clamped to the ceiling.

        Never smaller than a positive number the host reported.  This value
        becomes `presentation_delay_ns`, the horizon gating `record_audible`,
        so under-stating it over-claims what Allen heard — the direction
        ADR-0006 §3 D6 forbids.  Only the three inputs that carry no
        measurement at all fall back to the configured estimate: no attribute,
        not a real number, and a `0.0`-or-negative report, where `0.0` is
        PortAudio's "not available" sentinel and believing it literally would
        claim samples audible the instant they cross the callback boundary.
        """
        reported = getattr(stream, "latency", None)
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            seconds = float(reported)
            if seconds > 0.0:
                capped = min(seconds, self._MAX_PLAUSIBLE_OUTPUT_LATENCY_S)
                return int(capped * 1_000_000_000)
        return self._default_output_latency_ns

    @property
    def estimated_output_latency_ns(self) -> int:
        """Output latency in ns: host-reported and clamped, configured otherwise."""
        return self._estimated_output_latency_ns

    @property
    def starvation_gaps(self) -> int:
        """Lifetime generation-ring dry windows that a later block resumed."""
        return self._starvation_gaps

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

    def begin_trace_turn(
        self,
        attributes: Mapping[str, TraceValue],
        *,
        enabled: bool = True,
    ) -> None:
        """Correlate the next ring write and non-silent PortAudio callback."""
        self._trace_attributes = dict(attributes)
        self._trace_first_ring_accept_fired = not enabled
        self._trace_first_callback_fired = not enabled
        self._legacy_callback_report_pending = False
        self._legacy_first_chunk_pending = False
        if enabled:
            self.reset_first_chunk()

    # ------------------------------------------------------------------
    # Callback — runs on PortAudio thread, keep it tight
    # ------------------------------------------------------------------

    def _callback(  # noqa: C901, PLR0912, PLR0915 - realtime path stays inline
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
        generation_ring = self._generation_ring
        if generation_ring is None:
            actual = self._ring.read_into(view, frames)
            self._played_samples += actual
            # Legacy callbacks retain their old diagnostic flags.  Trace/user
            # delivery happens when the non-callback drain owner polls.
            if actual > 0 and not self._trace_first_callback_fired:
                self._trace_first_callback_fired = True
                self._legacy_callback_report_pending = True
            if actual > 0 and not self._first_chunk_fired:
                self._first_chunk_fired = True
                self._legacy_first_chunk_pending = True
            gain_command = self._gain_command
            if gain_command is not self._gain_consumed_command:
                self._gain.set_target(*gain_command)
                self._gain_consumed_command = gain_command
            self._gain.apply(view)
            return

        if frames > self._callback_max_frames:
            # Fail silent on an unexpected host block rather than allocate or
            # overrun scratch buffers on the realtime thread.
            view[:frames] = 0.0
            self._underflow_count += 1
            return

        active_before = self._active_lease
        actual = generation_ring.read_into(
            view,
            self._callback_generations,
            self._callback_cursors,
            frames,
        )
        # ADR-0006:347 starvation clause.  A short read means the ring ran
        # dry; PortAudio cannot see it because `read_into` already zero-padded
        # a complete, on-time block.  It only broke a timeline once audio for
        # that generation had been committed (before that it is prefill) and
        # once the *same* generation resumes (otherwise it is the normal
        # end-of-generation tail, which every clean response produces).
        generation_before = -1 if active_before is None else active_before.playback_generation_id
        if generation_before >= 0:
            if actual > 0 and generation_before == self._starvation_dry_generation:
                # Any audio ends the dry window, including a short read: the
                # gap was audible whether or not the ring refilled a whole block.
                self._starvation_gaps += 1
                self._starvation_dry_generation = -1
            if actual < frames and generation_before == self._callback_first_generation:
                self._starvation_dry_generation = generation_before
        if actual <= 0:
            return
        active_after = self._active_lease
        if (
            active_before is None
            or active_after is None
            or active_before.playback_generation_id != active_after.playback_generation_id
        ):
            view[:actual] = 0.0
            return
        generation = active_after.playback_generation_id
        valid = self._callback_valid[:actual]
        np.equal(
            self._callback_generations[:actual],
            generation,
            out=valid,
        )
        if not bool(np.all(valid)):
            view[:actual] = 0.0
            return
        gain_command = self._gain_command
        if gain_command is not self._gain_consumed_command:
            self._gain.set_target(*gain_command)
            self._gain_consumed_command = gain_command
        audibility_class = self._gain.apply(view)

        # Revalidate immediately before returning the block to PortAudio.  A
        # CAS tombstone that landed during the numpy copies kills the entire
        # block; a block already returned before CAS is submitted-host tail.
        self._callback_commit_generation = generation
        try:
            active_final = self._active_lease
            if active_final is None or active_final.playback_generation_id != generation:
                view[:actual] = 0.0
                return
            start_cursor = int(self._callback_cursors[0])
            end_cursor = int(self._callback_cursors[actual - 1]) + 1
            first = self._callback_first_generation != generation
            if first:
                self._callback_first_generation = generation
            self._played_samples += actual
            self._callback_reports.write(
                generation=generation,
                output_start_cursor=start_cursor,
                output_end_cursor=end_cursor,
                audibility_class=audibility_class,
                callback_monotonic_ns=time.monotonic_ns(),
                presentation_delay_ns=self._estimated_output_latency_ns,
                first_for_generation=first,
            )
        finally:
            self._callback_commit_generation = -1


# --- MiniMax T2A WebSocket client (ported from legacy core/tts_minimax_ws.py) ---
#
# Protocol (https://platform.minimax.io/docs/guides/speech-t2a-websocket):
#
#     connect → connected_success → task_start → task_started
#         → task_continue → audio chunks (hex pcm) → is_final
#         → task_finish → close
#
# Audio frames carry hex-encoded int16 LE PCM at ``sample_rate_in`` (32 kHz by
# default). The client decodes hex → int16 → float32 mono and returns the
# concatenated bytes — the same float32 PCM bytes the
# :class:`AudioStreamPlayer` consumes.
#
# Two intentional deviations from the legacy port (ADR-0005 §14 Task 15):
#   1. ``websockets.connect`` is wrapped by a module-level ``_ws_connect`` seam
#      so tests can :func:`unittest.mock.patch.object` it without standing up a
#      real server.
#   2. Primary / fallback endpoint logic lives in :meth:`synthesize` itself —
#      one ``OSError`` on the primary triggers a fresh connect to the fallback.
#      If both fail, :class:`MiniMaxUnavailableError` bubbles to Task 16's
#      fallback chain (F7 macos-say).


class MiniMaxUnavailableError(RuntimeError):
    """Both primary and fallback MiniMax endpoints are unreachable."""


class _MiniMaxProtocolError(RuntimeError):
    """Server returned a non-zero status_code in ``base_resp``."""


class TTSConcurrentSendError(RuntimeError):
    """A second segment send raced the response-scoped command writer."""


class TTSSessionClosedError(RuntimeError):
    """A command targeted a closed response-scoped TTS session."""


@dataclass(frozen=True)
class TTSResponseSegment:
    """One semantic segment sent through a response-scoped session."""

    response_id: str
    playback_generation_id: int
    sequence: int
    text: str


@dataclass(frozen=True)
class TTSAudioChunk:
    """Provider PCM bytes before the per-segment canonical resampler."""

    sequence: int
    pcm: bytes
    sample_rate_hz: int
    channels: int = 1
    sample_format: Literal["int16_le"] = "int16_le"


@dataclass(frozen=True)
class TTSSegmentFinished:
    """Provider terminal for one segment; only now may resampling finalize."""

    sequence: int
    usage: Mapping[str, object] | None = None


TTSAudioEvent = TTSAudioChunk | TTSSegmentFinished


class TTSSession(Protocol):
    """Typed response-scoped single-writer/single-reader provider contract."""

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        """Open one logical response session."""

    async def send(self, segment: TTSResponseSegment) -> None:
        """Serialize one segment through the command writer."""

    def audio_events(self) -> AsyncIterator[TTSAudioEvent]:
        """Return the session's sole audio-event iterator."""

    async def finish(self) -> None:
        """Finish a clean response session."""

    async def abort(self, reason: str) -> None:
        """Abort network work after playback CAS."""

    async def close(self) -> None:
        """Close every owned task and the transport."""


async def _ws_connect(url: str, *, additional_headers: dict[str, str]) -> Any:  # noqa: ANN401
    """Open a websocket connection.

    Module-level so tests can :func:`unittest.mock.patch.object` it without
    standing up a real server. ``websockets`` is imported lazily so the module
    stays importable even when the dependency is absent in some CI runners.
    """
    import websockets  # noqa: PLC0415

    return await websockets.connect(
        url,
        additional_headers=additional_headers,
        max_size=1_048_576,
        max_queue=16,
    )


def _base_to_ws_url(base_url: str) -> str:
    """Rewrite ``https://host`` → ``wss://host/ws/v1/t2a_v2`` (legacy convention)."""
    cleaned = base_url.rstrip("/")
    cleaned = cleaned.replace("https://", "wss://").replace("http://", "ws://")
    return cleaned + "/ws/v1/t2a_v2"


class MiniMaxWSClient:
    """One-shot MiniMax TTS WebSocket client with primary/fallback endpoint.

    Public surface required by ADR-0005 §4.2:

    * :meth:`synthesize` — text → concatenated float32 mono PCM bytes
    * :meth:`synthesize_stream` — async iterator yielding PCM chunks as bytes

    Defaults match the legacy ``core/tts_minimax_ws.py`` constants. The
    ``sample_rate_in`` / ``sample_rate_out`` pair stays equal (32 kHz) by
    default so no ``soxr`` resampling is needed; pass
    ``sample_rate_out=48000`` to engage the resampler.
    """

    _CONNECT_TIMEOUT = 3.0
    _TASK_START_TIMEOUT = 3.0
    _FIRST_CHUNK_TIMEOUT = 8.0
    _BETWEEN_CHUNK_TIMEOUT = 5.0
    _TOTAL_TIMEOUT = 30.0
    _SESSION_CLOSE_TIMEOUT = 1.0

    def __init__(  # noqa: PLR0913 — keyword-only audio + endpoint config
        self,
        *,
        api_key: str,
        voice: str = "Chinese (Mandarin)_ExplorativeGirl",
        primary_endpoint: str = "https://api-uw.minimax.io",
        fallback_endpoint: str = "https://api.minimax.chat",
        model: str = "speech-2.8-turbo",
        volume: int = 3,
        sample_rate_in: int = 32000,
        sample_rate_out: int = 32000,
        connect_timeout_s: float = 3.0,
        total_timeout_s: float = _TOTAL_TIMEOUT,
    ) -> None:
        """Configure endpoints, voice and audio shape; does not connect yet."""
        self._api_key = api_key
        self._voice = voice
        self._primary_endpoint = primary_endpoint
        self._fallback_endpoint = fallback_endpoint
        self._model = model
        self._volume = int(volume)
        self._sr_in = int(sample_rate_in)
        self._sr_out = int(sample_rate_out)
        self._connect_timeout = float(connect_timeout_s)
        self._total_timeout = float(total_timeout_s)
        self._closed = threading.Event()
        self._sessions_lock = threading.Lock()
        self._active_sessions: set[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def streaming_candidate_count(self) -> int:
        """Return the ordered prefix-safe endpoint candidate count."""
        return 2

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> TTSSession:
        """Create one response-scoped session without opening the network."""
        endpoints = (self._primary_endpoint, self._fallback_endpoint)
        try:
            endpoint = endpoints[endpoint_index]
        except IndexError as exc:
            msg = f"invalid MiniMax endpoint index: {endpoint_index}"
            raise ValueError(msg) from exc
        return MiniMaxTTSSession(
            api_key=self._api_key,
            endpoint=endpoint,
            voice=self._voice,
            model=self._model,
            volume=self._volume,
            sample_rate_hz=self._sr_in,
            connect_timeout_s=self._connect_timeout,
            first_chunk_timeout_s=self._FIRST_CHUNK_TIMEOUT,
            between_chunk_timeout_s=self._BETWEEN_CHUNK_TIMEOUT,
            idle_close_s=idle_close_s,
            command_queue_capacity=command_queue_capacity,
            audio_queue_capacity=audio_queue_capacity,
        )

    async def synthesize(self, text: str) -> bytes:
        """Return float32 mono PCM bytes for ``text``.

        Tries the primary endpoint first; on ``OSError`` / ``TimeoutError`` /
        websockets error, falls back to the secondary endpoint. If both fail,
        raises :class:`MiniMaxUnavailableError`.
        """
        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(text):
            if not chunks:
                record_realtime_trace(
                    "tts_provider_first_pcm_received",
                    measurement_semantics="first_pcm_chunk_yielded_by_provider_adapter",
                    pcm_bytes=len(chunk),
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM under one total deadline and one cancellable session."""
        if self._closed.is_set():
            msg = "MiniMax client is closed"
            raise MiniMaxUnavailableError(msg)
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            msg = "MiniMax synthesize_stream requires an asyncio task"
            raise RuntimeError(msg)
        session = (loop, task)
        with self._sessions_lock:
            if self._closed.is_set():
                msg = "MiniMax client is closed"
                raise MiniMaxUnavailableError(msg)
            self._active_sessions.add(session)
        try:
            async with asyncio.timeout(self._total_timeout):
                async for chunk in self._synthesize_stream_with_fallback(text):
                    yield chunk
        except TimeoutError as exc:
            msg = f"MiniMax synthesis exceeded {self._total_timeout:.1f}s total deadline"
            raise MiniMaxUnavailableError(msg) from exc
        finally:
            with self._sessions_lock:
                self._active_sessions.discard(session)

    def request_close(self) -> None:
        """Reject new sessions and thread-safely cancel every active session."""
        self._closed.set()
        with self._sessions_lock:
            sessions = tuple(self._active_sessions)
        for loop, task in sessions:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    async def _synthesize_stream_with_fallback(
        self,
        text: str,
    ) -> AsyncIterator[bytes]:
        """Try both endpoints inside the caller's total-deadline scope."""
        last_exc: BaseException | None = None
        for endpoint in (self._primary_endpoint, self._fallback_endpoint):
            try:
                async for chunk in self._stream_one_endpoint(endpoint, text):
                    yield chunk
            except (OSError, TimeoutError, _MiniMaxProtocolError) as exc:
                LOGGER.warning("MiniMax endpoint %s failed: %s", endpoint, exc)
                last_exc = exc
                continue
            except Exception as exc:
                # websockets raises subclasses of Exception (not OSError); we
                # treat any WS-layer failure as a connect/protocol failure for
                # fallback, and re-raise anything outside that namespace.
                if not type(exc).__module__.startswith("websockets"):
                    raise
                LOGGER.warning(
                    "MiniMax endpoint %s failed (%s): %s",
                    endpoint,
                    type(exc).__name__,
                    exc,
                )
                last_exc = exc
                continue
            else:
                return

        msg = "both primary and fallback MiniMax endpoints failed"
        raise MiniMaxUnavailableError(msg) from last_exc

    # ------------------------------------------------------------------
    # Internals — one session against a single endpoint
    # ------------------------------------------------------------------

    async def _stream_one_endpoint(
        self,
        endpoint: str,
        text: str,
    ) -> AsyncIterator[bytes]:
        ws_url = _base_to_ws_url(endpoint)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        conn = await asyncio.wait_for(
            _ws_connect(ws_url, additional_headers=headers),
            timeout=self._connect_timeout,
        )
        try:
            await self._handshake(conn, text)
            resampler = self._make_resampler()
            async for chunk_bytes in self._stream_audio(conn, resampler):
                yield chunk_bytes
            # task_finish — best-effort, server may already be closing
            with contextlib.suppress(Exception):
                await conn.send(json.dumps({"event": "task_finish"}))
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    conn.close(),
                    timeout=self._SESSION_CLOSE_TIMEOUT,
                )

    async def _handshake(self, conn: Any, text: str) -> None:  # noqa: ANN401
        """Drive ``connected_success → task_start → task_started → task_continue``."""
        # 1. connected_success
        await asyncio.wait_for(conn.recv(), timeout=self._connect_timeout)

        # 2. task_start
        task_start = {
            "event": "task_start",
            "model": self._model,
            "voice_setting": {
                "voice_id": self._voice,
                "speed": 1.0,
                "vol": self._volume,
                "pitch": 0,
            },
            "audio_setting": {
                "format": "pcm",
                "sample_rate": self._sr_in,
                "bitrate": 128000,
                "channel": 1,
            },
        }
        await conn.send(json.dumps(task_start))
        ts = await asyncio.wait_for(conn.recv(), timeout=self._TASK_START_TIMEOUT)
        ts_obj = json.loads(ts)
        status = ts_obj.get("base_resp", {}).get("status_code", 0)
        if status != 0:
            msg = f"task_start rejected: {ts_obj.get('base_resp')}"
            raise _MiniMaxProtocolError(msg)

        # 3. task_continue
        await conn.send(json.dumps({"event": "task_continue", "text": text}))

    async def _stream_audio(
        self,
        conn: Any,  # noqa: ANN401
        resampler: Any | None,  # noqa: ANN401
    ) -> AsyncIterator[bytes]:
        """Loop ``conn.recv`` until ``is_final``; yield float32 PCM byte chunks."""
        carry: bytes = b""
        first = True
        while True:
            timeout = self._FIRST_CHUNK_TIMEOUT if first else self._BETWEEN_CHUNK_TIMEOUT
            msg_raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            obj = json.loads(msg_raw)

            audio_hex = obj.get("data", {}).get("audio", "") or ""
            if audio_hex:
                pcm_f32, carry = _decode_audio_hex(audio_hex, carry)
                if resampler is not None and pcm_f32.size:
                    pcm_f32 = resampler.resample_chunk(pcm_f32)
                if pcm_f32.size:
                    first = False
                    yield pcm_f32.astype(np.float32).tobytes()

            if obj.get("is_final"):
                if resampler is not None:
                    tail = resampler.resample_chunk(
                        np.zeros(0, dtype=np.float32),
                        last=True,
                    )
                    if tail.size:
                        yield tail.astype(np.float32).tobytes()
                return

    def _make_resampler(self) -> Any | None:  # noqa: ANN401
        """Return a ``soxr.ResampleStream`` when sr_in != sr_out, else ``None``.

        ``soxr`` is a hard runtime dependency (listed in ``pyproject.toml``
        ``[project].dependencies``) and the import is lazy purely to keep the
        surrounding module's import cost minimal. An ``ImportError`` here means
        the install environment is broken (``uv sync`` should have pulled
        soxr); the raised ``RuntimeError`` surfaces that misconfiguration.
        """
        if self._sr_in == self._sr_out:
            return None
        try:
            import soxr  # noqa: PLC0415
        except ImportError as exc:
            msg = (
                f"sample_rate_in={self._sr_in} != sample_rate_out={self._sr_out} "
                "requires 'soxr' (a hard runtime dep — env is broken if missing)"
            )
            raise RuntimeError(msg) from exc
        return soxr.ResampleStream(
            self._sr_in,
            self._sr_out,
            1,
            dtype="float32",
            quality="HQ",
        )


def _decode_audio_hex(audio_hex: str, carry: bytes) -> tuple[np.ndarray, bytes]:
    """Decode a hex-encoded int16-LE PCM frame to float32 mono PCM in [-1, 1].

    Carries a trailing odd byte forward into the next frame so int16 reshape
    never sees an unaligned buffer. Ported from
    ``core/tts_minimax_ws.MinimaxWSClient.feed``.
    """
    if len(audio_hex) % 2:
        audio_hex = audio_hex[:-1]
    raw = carry + bytes.fromhex(audio_hex)
    aligned_len = (len(raw) // 2) * 2
    new_carry = raw[aligned_len:]
    raw = raw[:aligned_len]
    if not raw:
        return np.zeros(0, dtype=np.float32), new_carry
    pcm_i16 = np.frombuffer(raw, dtype=np.int16).copy()
    pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
    return pcm_f32, new_carry


@dataclass(frozen=True)
class _SessionFailure:
    error: BaseException


@dataclass(frozen=True)
class _SessionWriterCommand:
    kind: Literal["segment", "finish"]
    acknowledged: asyncio.Future[None]
    segment: TTSResponseSegment | None = None


_SESSION_EVENTS_CLOSED = object()


class MiniMaxTTSSession:
    """One MiniMax response session with one writer and one reader task."""

    _CLOSE_TIMEOUT_S = 1.0

    def __init__(  # noqa: PLR0913 - explicit provider/session budgets
        self,
        *,
        api_key: str,
        endpoint: str,
        voice: str,
        model: str,
        volume: int,
        sample_rate_hz: int,
        connect_timeout_s: float,
        first_chunk_timeout_s: float,
        between_chunk_timeout_s: float,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._voice = voice
        self._model = model
        self._volume = volume
        self._sample_rate_hz = sample_rate_hz
        self._connect_timeout_s = connect_timeout_s
        self._first_chunk_timeout_s = first_chunk_timeout_s
        self._between_chunk_timeout_s = between_chunk_timeout_s
        self._idle_close_s = idle_close_s
        self._commands: asyncio.Queue[_SessionWriterCommand] = asyncio.Queue(
            maxsize=command_queue_capacity,
        )
        self._events: asyncio.Queue[TTSAudioEvent | _SessionFailure | object] = asyncio.Queue(
            maxsize=audio_queue_capacity
        )
        self._conn: Any | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._active_sequence: int | None = None
        self._active_ready = asyncio.Event()
        self._send_call_active = False
        self._reader_claimed = False
        self._closing = False
        self._close_complete = False
        self._close_lock = asyncio.Lock()
        self._opened = False
        self._response_id: str | None = None
        self._generation: int | None = None
        self._last_activity = 0.0

    async def open(
        self,
        response_id: str,
        playback_generation_id: int,
    ) -> None:
        """Connect and finish the handshake before starting owned tasks."""
        if self._opened or self._closing:
            msg = "TTS session cannot be opened twice"
            raise RuntimeError(msg)
        self._response_id = response_id
        self._generation = playback_generation_id
        loop = asyncio.get_running_loop()
        record_realtime_trace(
            "tts_session_open_requested",
            response_id=response_id,
            playback_generation_id=playback_generation_id,
            measurement_semantics="before_provider_transport_connect",
        )
        conn = await asyncio.wait_for(
            _ws_connect(
                _base_to_ws_url(self._endpoint),
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
            ),
            timeout=self._connect_timeout_s,
        )
        self._conn = conn
        try:
            hello_raw = await asyncio.wait_for(
                conn.recv(),
                timeout=self._connect_timeout_s,
            )
            self._touch_activity(loop)
            hello = json.loads(hello_raw)
            hello_status = hello.get("base_resp", {}).get("status_code", 0)
            if hello_status != 0:
                msg = f"MiniMax session hello rejected: {hello.get('base_resp')}"
                raise _MiniMaxProtocolError(msg)  # noqa: TRY301 - handshake cleanup below
            task_start = {
                "event": "task_start",
                "model": self._model,
                "voice_setting": {
                    "voice_id": self._voice,
                    "speed": 1.0,
                    "vol": self._volume,
                    "pitch": 0,
                },
                "audio_setting": {
                    "format": "pcm",
                    "sample_rate": self._sample_rate_hz,
                    "bitrate": 128000,
                    "channel": 1,
                },
            }
            await conn.send(json.dumps(task_start))
            self._touch_activity(loop)
            started_raw = await asyncio.wait_for(
                conn.recv(),
                timeout=self._connect_timeout_s,
            )
            self._touch_activity(loop)
            started = json.loads(started_raw)
            status = started.get("base_resp", {}).get("status_code", 0)
            if status != 0:
                msg = f"MiniMax task_start rejected: {started.get('base_resp')}"
                raise _MiniMaxProtocolError(msg)  # noqa: TRY301 - handshake cleanup below
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), timeout=self._CLOSE_TIMEOUT_S)
            self._conn = None
            raise
        self._opened = True
        self._touch_activity(loop)
        self._writer_task = asyncio.create_task(
            self._writer_main(),
            name=f"tts-command-writer-{response_id}",
        )
        self._reader_task = asyncio.create_task(
            self._reader_main(),
            name=f"tts-audio-reader-{response_id}",
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog_main(),
            name=f"tts-idle-watchdog-{response_id}",
        )
        record_realtime_trace(
            "tts_session_opened",
            response_id=response_id,
            playback_generation_id=playback_generation_id,
            measurement_semantics="provider_handshake_completed",
        )

    async def send(self, segment: TTSResponseSegment) -> None:
        """Send through the sole writer, rejecting overlapping segment feeds."""
        if not self._opened or self._closing:
            msg = "send targeted a closed TTS session"
            raise TTSSessionClosedError(msg)
        if segment.response_id != self._response_id or (
            segment.playback_generation_id != self._generation
        ):
            msg = "segment identity does not match its response-scoped session"
            raise ValueError(msg)
        if self._send_call_active or self._active_sequence is not None:
            msg = "a segment feed is already active"
            raise TTSConcurrentSendError(msg)
        self._send_call_active = True
        try:
            acknowledged = asyncio.get_running_loop().create_future()
            await self._commands.put(
                _SessionWriterCommand(
                    kind="segment",
                    segment=segment,
                    acknowledged=acknowledged,
                ),
            )
            await acknowledged
        finally:
            self._send_call_active = False

    async def _audio_events_iter(self) -> AsyncIterator[TTSAudioEvent]:
        if self._reader_claimed:
            msg = "TTSSession.audio_events() has exactly one reader"
            raise RuntimeError(msg)
        self._reader_claimed = True
        while True:
            item = await self._events.get()
            if item is _SESSION_EVENTS_CLOSED:
                return
            if isinstance(item, _SessionFailure):
                raise item.error
            if isinstance(item, TTSAudioChunk | TTSSegmentFinished):
                yield item

    def audio_events(self) -> AsyncIterator[TTSAudioEvent]:
        """Return the one normalized event iterator."""
        return self._audio_events_iter()

    async def finish(self) -> None:
        """Send task_finish only after the current segment terminal."""
        if self._closing:
            return
        if self._active_sequence is not None:
            msg = "cannot finish while a segment feed is active"
            raise TTSConcurrentSendError(msg)
        acknowledged = asyncio.get_running_loop().create_future()
        await self._commands.put(
            _SessionWriterCommand(kind="finish", acknowledged=acknowledged),
        )
        await acknowledged
        await self.close()

    async def abort(self, reason: str) -> None:
        """Close network work; playback CAS is owned by the caller."""
        record_realtime_trace(
            "tts_session_abort_requested",
            response_id=self._response_id,
            playback_generation_id=self._generation,
            reason=reason,
        )
        await self.close()

    async def close(self) -> None:
        """Bound cancellation and close of writer/reader/watchdog/transport."""
        async with self._close_lock:
            if self._close_complete:
                return
            self._closing = True
            current = asyncio.current_task()
            tasks = tuple(
                task
                for task in (self._writer_task, self._reader_task, self._watchdog_task)
                if task is not None and task is not current
            )
            for task in tasks:
                task.cancel()
            conn = self._conn
            self._conn = None
            if conn is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.close(), timeout=self._CLOSE_TIMEOUT_S)
            if tasks:
                _done, pending = await asyncio.wait(
                    tasks,
                    timeout=self._CLOSE_TIMEOUT_S,
                )
                if pending:
                    LOGGER.warning(
                        "TTS session tasks ignored bounded cancellation: %s",
                        ", ".join(task.get_name() for task in pending),
                    )
            with contextlib.suppress(asyncio.QueueFull):
                self._events.put_nowait(_SESSION_EVENTS_CLOSED)
            self._opened = False
            self._close_complete = True

    async def _writer_main(self) -> None:  # noqa: C901 - one writer FSM
        """Own every post-handshake ``send`` call."""
        try:
            while True:
                command = await self._commands.get()
                conn = self._conn
                if conn is None:
                    msg = "writer lost its connection"
                    raise TTSSessionClosedError(msg)  # noqa: TRY301 - writer failure lane
                try:
                    if command.kind == "segment":
                        segment = command.segment
                        if segment is None:
                            msg = "segment writer command missing payload"
                            raise RuntimeError(msg)  # noqa: TRY301 - writer failure lane
                        if self._active_sequence is not None:
                            msg = "writer observed overlapping active segments"
                            raise TTSConcurrentSendError(msg)  # noqa: TRY301
                        self._active_sequence = segment.sequence
                        self._active_ready.set()
                        await conn.send(
                            json.dumps(
                                {"event": "task_continue", "text": segment.text},
                            ),
                        )
                        self._touch_activity(asyncio.get_running_loop())
                    else:
                        await conn.send(json.dumps({"event": "task_finish"}))
                        self._touch_activity(asyncio.get_running_loop())
                    if not command.acknowledged.done():
                        command.acknowledged.set_result(None)
                except BaseException as exc:
                    if not command.acknowledged.done():
                        command.acknowledged.set_exception(exc)
                    raise
                finally:
                    self._commands.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider writer boundary
            await self._publish_failure(exc)

    async def _reader_main(self) -> None:  # noqa: C901 - one reader FSM
        """Own the only post-handshake ``recv`` loop."""
        first_for_segment = True
        try:
            while True:
                await self._active_ready.wait()
                sequence = self._active_sequence
                conn = self._conn
                if sequence is None or conn is None:
                    if self._closing:
                        return
                    await asyncio.sleep(0)
                    continue
                timeout = (
                    self._first_chunk_timeout_s
                    if first_for_segment
                    else self._between_chunk_timeout_s
                )
                raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
                self._touch_activity(asyncio.get_running_loop())
                obj = json.loads(raw)
                status = obj.get("base_resp", {}).get("status_code", 0)
                if status != 0:
                    msg = f"MiniMax audio event failed: {obj.get('base_resp')}"
                    raise _MiniMaxProtocolError(msg)  # noqa: TRY301 - reader failure lane
                audio_hex = obj.get("data", {}).get("audio", "") or ""
                if audio_hex:
                    if len(audio_hex) % 2:
                        audio_hex = audio_hex[:-1]
                    pcm = bytes.fromhex(audio_hex)
                    if pcm:
                        await self._events.put(
                            TTSAudioChunk(
                                sequence=sequence,
                                pcm=pcm,
                                sample_rate_hz=self._sample_rate_hz,
                            ),
                        )
                        first_for_segment = False
                if obj.get("is_final"):
                    usage_raw = obj.get("extra_info")
                    usage = usage_raw if isinstance(usage_raw, dict) else None
                    await self._events.put(
                        TTSSegmentFinished(sequence=sequence, usage=usage),
                    )
                    self._active_sequence = None
                    self._active_ready.clear()
                    first_for_segment = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider reader boundary
            await self._publish_failure(exc)

    async def _watchdog_main(self) -> None:
        """Close only an actually idle session; active feeds use reader deadlines."""
        while not self._closing:
            await asyncio.sleep(min(0.1, max(0.01, self._idle_close_s / 4)))
            if self._active_sequence is not None or not self._commands.empty():
                continue
            idle_for = asyncio.get_running_loop().time() - self._last_activity
            if idle_for <= self._idle_close_s:
                continue
            await self._publish_failure(
                TTSSessionClosedError(
                    f"TTS session idle for {idle_for:.3f}s",
                ),
            )
            conn = self._conn
            self._conn = None
            if conn is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        conn.close(),
                        timeout=self._CLOSE_TIMEOUT_S,
                    )
            self._closing = True
            return

    async def _publish_failure(self, error: BaseException) -> None:
        """Backpressure failure delivery through the bounded audio queue."""
        if self._closing:
            return
        await self._events.put(_SessionFailure(error=error))

    def _touch_activity(self, loop: asyncio.AbstractEventLoop) -> None:
        """Refresh the common send/receive activity clock."""
        self._last_activity = loop.time()


# --- TTS Pipeline (gate-mode routing + fallback chain) -----------------------
#
# ADR-0005 §5.3 / §10 F6-F7. The pipeline is event-driven: `_tts_watcher`
# (runtime layer) dispatches `surface.response_*` events into `begin_turn`,
# `handle_chunk`, `handle_emitted`. The pipeline owns gate-mode routing
# (spec §3.6.6: `sentence` plays each chunk immediately; `full_text` and
# `structured` buffer until the response is emitted) and the MiniMax →
# `macos_say` fallback chain (§3.6.11).
#
# This file's prior layers are pure ports (preprocessor, AudioStreamPlayer,
# MiniMaxWSClient). The pipeline is the orchestrator that L4/L5 events talk to.

GateMode = Literal["sentence", "full_text", "structured"]


class FallbackOwner(Protocol):
    """Side-effect owner created without starting the fallback process."""

    def run(self) -> bool:
        """Start the fallback unless cancelled; return whether it started."""

    def cancel(self, *, wait_timeout_s: float) -> bool:
        """Prevent start or terminate an already-started fallback within a bound."""


class MacOSSayProcessOwner:
    """Cancellable owner for one macOS ``say`` process.

    ``run`` and ``cancel`` share ``_lock`` as their process-start
    linearization point. Cancellation before ``Popen`` prevents the spawn;
    cancellation after it terminates, then kills, the exact captured process.
    """

    _RUN_TIMEOUT_S = 30.0

    def __init__(self, text: str, *, voice: str = "Tingting") -> None:
        """Capture arguments without spawning the process."""
        self._text = text
        self._voice = voice
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._process: subprocess.Popen[bytes] | None = None
        self._finished = threading.Event()

    def run(self) -> bool:
        """Spawn and wait for ``say`` unless close cancelled this owner first."""
        with self._lock:
            if self._cancel_requested:
                self._finished.set()
                return False
            try:
                process = subprocess.Popen(  # noqa: S603 — macOS API contract.
                    ["say", "-v", self._voice, self._text],  # noqa: S607
                )
            except OSError as exc:
                self._finished.set()
                LOGGER.warning(
                    "macos_say_fallback failed to start: %r — response remains silent",
                    exc,
                )
                return False
            self._process = process

        try:
            process.wait(timeout=self._RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOGGER.warning("macos_say_fallback exceeded %.1fs; terminating", self._RUN_TIMEOUT_S)
            self.cancel(wait_timeout_s=1.0)
        except (subprocess.SubprocessError, OSError) as exc:
            LOGGER.warning(
                "macos_say_fallback failed: %r — response remains silent",
                exc,
            )
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
                self._finished.set()
        return True

    def cancel(self, *, wait_timeout_s: float) -> bool:
        """Prevent spawn or terminate/kill the owned process within ``wait_timeout_s``."""
        with self._lock:
            self._cancel_requested = True
            process = self._process
            if process is None:
                self._finished.set()
                return True
            try:
                process_finished = process.poll() is not None
            except OSError:
                process_finished = False
            if process_finished:
                self._finished.set()
                return True
            with contextlib.suppress(OSError):
                process.terminate()

        terminate_wait = max(0.0, wait_timeout_s / 2.0)
        try:
            process.wait(timeout=terminate_wait)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            try:
                process.wait(timeout=max(0.0, wait_timeout_s - terminate_wait))
            except (subprocess.SubprocessError, OSError):
                return False
        except (subprocess.SubprocessError, OSError):
            return False
        finally:
            self._finished.set()
        return process.poll() is not None


@dataclass(frozen=True)
class _TTSWorkItem:
    kind: Literal["speak", "broadcast_spoken"]
    generation: int
    turn_id: str | None
    text: str = ""


class TTSPipeline:
    """Event-driven TTS playback per ADR-0005 §5.3.

    Lifecycle (one turn):

    * ``begin_turn(turn_id, gate_mode)`` — called on ``surface.response_open``;
      records the routing mode and clears any partial buffer from the prior
      turn.
    * ``handle_chunk(turn_id, text)`` — called on ``surface.response_chunk``.
      In ``sentence`` mode the chunk is synthesized and queued for playback
      immediately; in ``full_text`` / ``structured`` mode the chunk is
      appended to a per-turn buffer with no synthesis.
    * ``handle_emitted(turn_id)`` — called on ``surface.response_emitted``.
      In buffered modes the joined buffer is now synthesized in one shot,
      then ``end_turn`` runs.
    * ``end_turn(turn_id)`` — broadcasts the optional ``spoken`` voice phase
      and clears per-turn state. Safe to call on its own (sentence mode
      uses this directly from the watcher when the response is emitted).

    Sample rate alignment: callers should construct ``provider`` and
    ``player`` at the same rate (default 32 kHz). When they differ, the
    provider's ``soxr`` path resamples internally — see
    :class:`MiniMaxWSClient` for the contract.
    """

    _CLOSE_WAIT_S = 2.5
    _OWNER_CANCEL_WAIT_S = 0.5
    _PROVIDER_TOTAL_TIMEOUT_S = 35.0
    _MAX_PENDING_WORK_ITEMS = 32

    def __init__(
        self,
        *,
        provider: MiniMaxWSClient,
        player: AudioStreamPlayer,
        fallback: Callable[[str], FallbackOwner] | None,
        broadcaster: object | None = None,
        ducker: voice_ducking.SystemAudioDucker | None = None,
    ) -> None:
        """Wire the synthesis provider, audio player and macOS-say fallback.

        Args:
            provider: WebSocket-backed TTS source (MiniMax).
            player: PCM sink with a queue (drives ``is_speaking`` + ducking).
            fallback: Side-effect-free owner factory called when the provider
                is down; use :func:`macos_say_fallback` in production. The
                returned owner's ``run`` starts the external effect only after
                pipeline registration, while ``cancel`` owns shutdown.
            broadcaster: Optional object exposing
                ``broadcast_voice_sync(phase, *, turn_id)`` — the pipeline
                emits ``"spoken"`` at end-of-turn for UI feedback.
            ducker: Optional :class:`voice_ducking.SystemAudioDucker`.
                Sharing one instance with the wake listener makes output
                leases and capture ducking mutually exclusive. TTS never
                mutes itself; its lease prevents wake capture from muting
                provider I/O or queued playback.
        """
        self._provider = provider
        self._player = player
        self._fallback = fallback
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._turn_id: str | None = None
        self._gate_mode: GateMode = "sentence"
        self._buffer: list[str] = []
        self._state = threading.Condition(threading.Lock())
        self._commit_lock = threading.Lock()
        self._pcm_commit_gate = _PCMCommitGate()
        self._close_requested = threading.Event()
        self._closed = False
        self._generation = 0
        self._active_workers = 0
        self._output_active_leases = 0
        self._fallback_owner: FallbackOwner | None = None
        self._provider_loop: asyncio.AbstractEventLoop | None = None
        self._provider_task: asyncio.Task[bytes] | None = None
        # One reserved slot guarantees close can enqueue its sentinel. This
        # batch queue intentionally drops-and-traces overload; a future
        # streaming producer must define backpressure/coalescing rather than
        # reusing it as an unbounded chunk queue.
        self._work_queue: queue.Queue[_TTSWorkItem | None] = queue.Queue(
            maxsize=self._MAX_PENDING_WORK_ITEMS + 1,
        )
        self._worker_stop_sent = False
        self._worker_thread = threading.Thread(
            target=self._worker_main,
            name="jarvis-tts-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def begin_turn(self, turn_id: str, *, gate_mode: GateMode | None) -> None:
        """Called on ``surface.response_open``. Resets buffer + routing mode."""
        with self._state:
            if self._closed:
                LOGGER.info("TTSPipeline: ignoring begin_turn after close: %s", turn_id)
                return
            self._turn_id = turn_id
            self._gate_mode = gate_mode or "sentence"
            self._buffer.clear()

    def handle_chunk(self, turn_id: str, text: str) -> None:
        """Called on ``surface.response_chunk``.

        Sentence mode: every chunk is accumulated; a ``</voice>`` close
        tag in the chunk flushes the buffer through synth. Multiple
        ``<voice>...</voice>`` regions in one turn each flush at their
        own close. This batches the WS-connect overhead — one MiniMax
        round trip per voice region instead of one per chunk — and
        eliminates literal tag-text from being synthesised.
        Full_text / structured: accumulate as before, flush on
        ``handle_emitted``.
        """
        with self._state:
            if self._closed or turn_id != self._turn_id:
                LOGGER.warning(
                    "TTSPipeline: chunk for unknown/closed turn_id=%s (current=%s)",
                    turn_id,
                    self._turn_id,
                )
                return
            self._buffer.append(text)
            flush_now = self._gate_mode == "sentence" and "</voice>" in text
        if flush_now:
            self._flush_buffer()

    def handle_emitted(self, turn_id: str) -> None:
        """Called on ``surface.response_emitted``. Flushes any pending buffer."""
        with self._state:
            if self._closed or turn_id != self._turn_id:
                return
        self.end_turn(turn_id)

    def end_turn(self, turn_id: str) -> None:
        """Queue remaining speech then queue ``spoken`` behind its completion.

        Defensive flush covers truncated streams (``</voice>`` never
        arrived) and the tests that drive the pipeline directly without
        a full ``handle_emitted`` event. Both operations share the single
        TTS worker, so the UI phase cannot overtake queued synthesis.
        """
        self._flush_buffer()
        with self._state:
            if self._closed or turn_id != self._turn_id:
                return
            self._enqueue_work_locked(
                _TTSWorkItem(
                    kind="broadcast_spoken",
                    generation=self._generation,
                    turn_id=turn_id,
                ),
            )
            self._turn_id = None
            self._buffer.clear()

    def _flush_buffer(self) -> None:
        """Move buffered text to the owned daemon worker without blocking."""
        with self._state:
            if self._closed or not self._buffer:
                return
            joined = "".join(self._buffer)
            self._buffer.clear()
            if joined:
                self._enqueue_work_locked(
                    _TTSWorkItem(
                        kind="speak",
                        generation=self._generation,
                        turn_id=self._turn_id,
                        text=joined,
                    ),
                )

    def request_close(
        self,
        *,
        owner_cancel_timeout_s: float = _OWNER_CANCEL_WAIT_S,
    ) -> None:
        """Install the close gate and cancel captured side-effect owners.

        ``_commit_lock`` linearizes provider/fallback ownership. The separate
        ``_pcm_commit_gate`` linearizes generation invalidation with each
        non-blocking ring-index publication. Once this method returns, no new
        fallback can start and no stale PCM can publish. A registered ``say``
        owner has either been prevented from spawning or terminate/kill has
        been attempted within ``owner_cancel_timeout_s``.
        """
        fallback_owner: FallbackOwner | None
        provider_loop: asyncio.AbstractEventLoop | None
        provider_task: asyncio.Task[bytes] | None
        with self._commit_lock:
            if not self._close_requested.is_set():
                self._close_requested.set()
                invalidated_generation = self._pcm_commit_gate.invalidate()
                with self._state:
                    self._closed = True
                    self._generation = invalidated_generation
                    self._turn_id = None
                    self._buffer.clear()
                    if not self._worker_stop_sent:
                        self._worker_stop_sent = True
                        self._work_queue.put_nowait(None)
                    self._state.notify_all()
                    record_realtime_trace(
                        "tts_pipeline_close_requested",
                        generation=self._generation,
                    )
            fallback_owner = self._fallback_owner
            provider_loop = self._provider_loop
            provider_task = self._provider_task

        provider_close = getattr(self._provider, "request_close", None)
        if callable(provider_close):
            with contextlib.suppress(Exception):
                provider_close()
        if provider_loop is not None and provider_task is not None:
            with contextlib.suppress(RuntimeError):
                provider_loop.call_soon_threadsafe(provider_task.cancel)
        if fallback_owner is not None:
            try:
                cancelled = fallback_owner.cancel(
                    wait_timeout_s=max(0.0, owner_cancel_timeout_s),
                )
            except Exception:
                cancelled = False
                LOGGER.exception("TTS fallback owner cancellation failed")
            record_realtime_trace(
                "tts_fallback_cancelled_on_close",
                success=cancelled,
            )

        # Lock order is commit -> PCM gate, then (after both are released)
        # player write lock. The callback remains lock-free. Flush wakes
        # drain/full-ring waits after close has invalidated every publication.
        self._player.flush()

    def close(self, *, wait_timeout_s: float = _CLOSE_WAIT_S) -> bool:
        """Close the gate, bound worker drain, then stop PortAudio.

        Returns ``True`` when the cancellable provider/fallback owner, queued
        work, output leases, and daemon worker all stop before the bound. A
        pathological provider that ignores task cancellation can outlive the
        bound only on the daemon worker; its stale generation cannot write PCM
        or register fallback and cannot delay asyncio default-executor exit.
        """
        deadline = time.monotonic() + max(0.0, wait_timeout_s)
        self.request_close(
            owner_cancel_timeout_s=min(
                self._OWNER_CANCEL_WAIT_S,
                max(0.0, deadline - time.monotonic()),
            ),
        )
        with self._state:
            while self._active_workers > 0 or self._output_active_leases > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(timeout=remaining)
            idle = self._active_workers == 0 and self._output_active_leases == 0
            active_workers = self._active_workers
            active_leases = self._output_active_leases
        self._worker_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_stopped = not self._worker_thread.is_alive()
        idle = idle and worker_stopped
        if not idle:
            record_realtime_trace(
                "tts_pipeline_close_bounded",
                success=False,
                active_workers=active_workers,
                active_output_leases=active_leases,
                worker_stopped=worker_stopped,
                wait_timeout_s=wait_timeout_s,
            )
            LOGGER.warning(
                "TTSPipeline close bounded with workers=%d output_leases=%d",
                active_workers,
                active_leases,
            )
        self._player.flush()
        player_closed: object = self._player.stop(
            timeout_s=max(0.0, deadline - time.monotonic()),
        )
        if isinstance(player_closed, PlayerStopResult):
            device_closed = player_closed.definitively_closed
            device_close_reason = player_closed.reason
        else:
            # Test/port adapters predating the typed lifecycle return are
            # synchronous: a normal return remains their close proof.
            device_closed = True
            device_close_reason = "synchronous_adapter_return"
        record_realtime_trace(
            "tts_pipeline_closed",
            success=idle and device_closed,
            active_workers=active_workers,
            active_output_leases=active_leases,
            worker_stopped=worker_stopped,
            device_closed=device_closed,
            device_close_reason=device_close_reason,
        )
        return idle and device_closed

    def wait_until_idle(self, *, timeout_s: float) -> bool:
        """Wait for queued/running work and output leases without closing."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._state:
            while self._active_workers > 0 or self._output_active_leases > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state.wait(timeout=remaining)
            return True

    def is_output_active(self) -> bool:
        """Return whether synthesis, fallback, or queued playback owns output."""
        with self._state:
            if self._active_workers > 0 or self._output_active_leases > 0:
                return True
        return self._player.bytes_pending() > 0

    def is_speaking(self) -> bool:
        """Compatibility alias for the unified output-active lifecycle."""
        return self.is_output_active()

    def _generation_is_current(self, generation: int) -> bool:
        with self._state:
            return not self._closed and self._generation == generation

    def _enqueue_work_locked(self, item: _TTSWorkItem) -> bool:
        """Queue one item under ``_state`` or drop-and-trace bounded overload."""
        if self._work_queue.qsize() >= self._MAX_PENDING_WORK_ITEMS:
            record_realtime_trace(
                "tts_work_queue_overflow",
                turn_id=item.turn_id,
                work_kind=item.kind,
                capacity=self._MAX_PENDING_WORK_ITEMS,
                overflow_policy="drop_and_trace_not_stream_backpressure",
            )
            return False
        try:
            self._work_queue.put_nowait(item)
        except queue.Full:
            record_realtime_trace(
                "tts_work_queue_overflow",
                turn_id=item.turn_id,
                work_kind=item.kind,
                capacity=self._MAX_PENDING_WORK_ITEMS,
                overflow_policy="drop_and_trace_not_stream_backpressure",
            )
            return False
        self._active_workers += 1
        return True

    def _worker_main(self) -> None:
        """Own all provider/fallback work outside asyncio's default executor."""
        while True:
            item = self._work_queue.get()
            try:
                if item is None:
                    return
                if item.kind == "speak":
                    self._speak(item)
                else:
                    self._broadcast_spoken(item)
            except BaseException:
                LOGGER.exception("TTS worker item failed")
            finally:
                if item is not None:
                    self._finish_worker()
                self._work_queue.task_done()

    def _broadcast_spoken(self, item: _TTSWorkItem) -> None:
        if not self._generation_is_current(item.generation):
            return
        broadcast = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(broadcast):
            try:
                broadcast("spoken", turn_id=item.turn_id)
            except Exception as exc:  # noqa: BLE001 — broadcast must not crash TTS
                LOGGER.warning("broadcast_voice_sync(spoken) failed: %r", exc)

    def _finish_worker(self) -> None:
        with self._state:
            if self._active_workers <= 0:
                msg = "TTS worker released without a matching owner"
                raise RuntimeError(msg)
            self._active_workers -= 1
            self._state.notify_all()

    def _enter_output_active(self, generation: int) -> bool:
        """Acquire one synth-before-first-PCM output lease."""
        if self._ducker is not None and not self._ducker.enter_output():
            record_realtime_trace(
                "tts_output_lease_refused",
                turn_id=self._turn_id,
                reason="system_output_not_ready",
            )
            return False
        with self._state:
            if self._closed or self._generation != generation:
                stale = True
            else:
                stale = False
                self._output_active_leases += 1
        if stale:
            if self._ducker is not None:
                self._ducker.leave_output()
            return False
        return True

    def _leave_output_active(self) -> None:
        """Release one output lease without disturbing concurrent synthesis."""
        try:
            if self._ducker is not None:
                self._ducker.leave_output()
        finally:
            with self._state:
                if self._output_active_leases <= 0:
                    msg = "output-active lease released without a matching acquire"
                    raise RuntimeError(msg)
                self._output_active_leases -= 1
                self._state.notify_all()

    def _release_output_after_ring_empty(self, *, turn_id: str | None) -> None:
        """Hold the lease until the software ring empties, not until DAC output."""
        try:
            drained = self._player.drain()
            record_realtime_trace(
                "audio_ring_empty_observed",
                turn_id=turn_id,
                ring_drained=drained,
                measurement_semantics="software_ring_empty_not_dac_audible_horizon",
            )
        finally:
            self._leave_output_active()

    def _fallback_if_current(
        self,
        cleaned: str,
        *,
        generation: int,
        turn_id: str | None,
    ) -> None:
        """Register an owner, then start it outside the commit lock."""
        if self._fallback is None:
            record_realtime_trace(
                "tts_fallback_suppressed",
                turn_id=turn_id,
                reason="fallback_not_configured",
            )
            return
        try:
            owner = self._fallback(cleaned)
        except Exception:
            LOGGER.exception("TTS fallback owner construction failed")
            return

        with self._commit_lock:
            suppressed = self._close_requested.is_set() or not (
                self._generation_is_current(generation)
            )
            if suppressed:
                record_realtime_trace(
                    "tts_fallback_suppressed",
                    turn_id=turn_id,
                    reason="pipeline_closed_or_stale_generation",
                )
            elif self._fallback_owner is not None:
                msg = "TTS fallback owner overlap on single worker"
                raise RuntimeError(msg)
            else:
                self._fallback_owner = owner

        if suppressed:
            owner.cancel(wait_timeout_s=0.0)
            return

        try:
            record_realtime_trace("tts_fallback_owner_run_requested", turn_id=turn_id)
            started = owner.run()
            if started:
                record_realtime_trace(
                    "tts_fallback_owner_finished",
                    turn_id=turn_id,
                    measurement_semantics=(
                        "owned_fallback_run_returned_not_natural_audio_completion"
                    ),
                )
            else:
                record_realtime_trace(
                    "tts_fallback_suppressed",
                    turn_id=turn_id,
                    reason="owner_cancelled_before_process_start",
                )
        finally:
            with self._commit_lock:
                if self._fallback_owner is owner:
                    self._fallback_owner = None

    def _run_provider(self, cleaned: str, *, generation: int) -> bytes:
        """Run one bounded provider task on the owned daemon worker loop."""
        loop = asyncio.new_event_loop()

        async def _bounded_synthesize() -> bytes:
            try:
                return await asyncio.wait_for(
                    self._provider.synthesize(cleaned),
                    timeout=self._PROVIDER_TOTAL_TIMEOUT_S,
                )
            except TimeoutError as exc:
                msg = (
                    f"TTS provider exceeded {self._PROVIDER_TOTAL_TIMEOUT_S:.1f}s pipeline deadline"
                )
                raise MiniMaxUnavailableError(msg) from exc

        task = loop.create_task(_bounded_synthesize())
        with self._commit_lock:
            if self._close_requested.is_set() or not self._generation_is_current(
                generation,
            ):
                task.cancel()
            self._provider_loop = loop
            self._provider_task = task
        try:
            return loop.run_until_complete(task)
        finally:
            with self._commit_lock:
                if self._provider_task is task:
                    self._provider_task = None
                    self._provider_loop = None
            loop.close()

    def _commit_pcm(
        self,
        pcm: bytes,
        *,
        generation: int,
        turn_id: str | None,
    ) -> bool:
        """Commit current PCM and transfer its lease to a ring-drain owner."""
        if not pcm:
            return False
        with self._commit_lock:
            pcm_current = not self._close_requested.is_set() and (
                self._generation_is_current(generation)
            )
        if not pcm_current:
            record_realtime_trace(
                "tts_late_pcm_discarded",
                turn_id=turn_id,
                pcm_bytes=len(pcm),
            )
            return False
        accepted_samples = self._player.write(
            pcm,
            cancel_event=self._close_requested,
            commit_gate=self._pcm_commit_gate,
            generation=generation,
        )
        if accepted_samples == 0:
            reason = (
                "generation_invalidated_before_ring_publish"
                if not self._pcm_commit_gate.is_current(generation)
                else "player_rejected_pcm_before_ring_publish"
            )
            record_realtime_trace(
                "tts_late_pcm_discarded",
                turn_id=turn_id,
                pcm_bytes=len(pcm),
                reason=reason,
            )
            return False
        total_samples = len(pcm) // AudioStreamPlayer._BYTES_PER_SAMPLE  # noqa: SLF001
        if isinstance(accepted_samples, int) and accepted_samples < total_samples:
            record_realtime_trace(
                "tts_pcm_commit_interrupted",
                turn_id=turn_id,
                accepted_samples=accepted_samples,
                total_samples=total_samples,
            )
        if self._close_requested.is_set() or self._player.bytes_pending() <= 0:
            return False
        release_thread = threading.Thread(
            target=self._release_output_after_ring_empty,
            kwargs={"turn_id": turn_id},
            name="jarvis-tts-output-lease",
            daemon=True,
        )
        release_thread.start()
        return True

    def _begin_player_trace(self, *, turn_id: str | None) -> None:
        """Install correlation when this queued item actually reaches output."""
        prior_pcm_pending = self._player.bytes_pending() > 0
        if prior_pcm_pending:
            record_realtime_trace(
                "audio_trace_correlation_unavailable",
                turn_id=turn_id,
                reason="prior_turn_pcm_still_in_software_ring",
            )
        attributes: dict[str, TraceValue] = {}
        if turn_id is not None:
            attributes["turn_id"] = turn_id
        self._player.begin_trace_turn(
            attributes,
            enabled=not prior_pcm_pending,
        )

    def _speak(self, item: _TTSWorkItem) -> None:
        """Synthesize ``text`` and push the PCM bytes to the player.

        The output lease begins before provider I/O and remains held until the
        software ring empties. Ring-empty is not a claim that CoreAudio/DAC
        playback is physically complete; a later playback ledger owns that
        horizon. The OS-volume ducker is only used by capture.

        On :class:`MiniMaxUnavailableError` (both endpoints down) or any
        unexpected synth/playback exception, route the cleaned text to
        the macOS ``say`` fallback. F7 is the terminal leaf — failures
        beyond that are logged only (assistant response goes silent).
        """
        # Extract <voice>...</voice> regions BEFORE preprocessing so the
        # downstream MiniMax call never sees literal tag chars (which
        # synthesise as "less-than voice greater-than" gibberish) and so
        # any <document>...</document> region is silently dropped from
        # synthesis. See ``_extract_voice_content`` for fallback rules
        # when the text has no tags (legacy plain-text path).
        voice_only = _extract_voice_content(item.text)
        cleaned = _preprocess_for_speech(voice_only)
        if not cleaned:
            return
        generation = item.generation
        turn_id = item.turn_id
        if not self._generation_is_current(generation):
            return
        self._begin_player_trace(turn_id=turn_id)
        # NOTE: do NOT wrap synth+write in SystemAudioDucker. That ducker
        # zeroes the macOS master output volume — which silences the TTS
        # output stream itself for the duration of write() (write blocks
        # while the ring drains, up to its 10 s timeout). Legacy used a
        # PCM-level gain duck inside the player for barge-in; the
        # OS-level master-volume duck only belongs on the wake-capture
        # path (mute speakers while the mic is open).
        lease_acquired = False
        release_after_ring_empty = False
        try:
            lease_acquired = self._enter_output_active(generation)
            if not lease_acquired:
                return
            record_realtime_trace(
                "tts_batch_synthesis_started",
                turn_id=turn_id,
                provider_mode="batch",
                measurement_semantics="before_provider_adapter_call_upper_bound",
            )
            try:
                with realtime_trace_context(turn_id=turn_id, provider_mode="batch"):
                    pcm = self._run_provider(cleaned, generation=generation)
                if pcm:
                    record_realtime_trace(
                        "tts_batch_synthesis_completed",
                        turn_id=turn_id,
                        provider_mode="batch",
                        measurement_semantics="all_provider_pcm_aggregated_not_first_pcm",
                        pcm_bytes=len(pcm),
                    )
                release_after_ring_empty = self._commit_pcm(
                    pcm,
                    generation=generation,
                    turn_id=turn_id,
                )
            except asyncio.CancelledError:
                record_realtime_trace(
                    "tts_provider_cancelled_on_close",
                    turn_id=turn_id,
                )
            except MiniMaxUnavailableError:
                LOGGER.warning(
                    "MiniMax unavailable; falling back to macos_say for: %r",
                    cleaned,
                )
                self._fallback_if_current(
                    cleaned,
                    generation=generation,
                    turn_id=turn_id,
                )
            except Exception:
                # TTS path must never crash the daemon; F7 fallback.
                LOGGER.exception(
                    "TTS synth failed for turn_id=%s",
                    turn_id,
                )
                self._fallback_if_current(
                    cleaned,
                    generation=generation,
                    turn_id=turn_id,
                )
        finally:
            if lease_acquired and not release_after_ring_empty:
                self._leave_output_active()


def macos_say_fallback(
    text: str,
    *,
    voice: str = "Tingting",
) -> MacOSSayProcessOwner:
    """Create the ADR-0005 §10 F7 macOS ``say`` process owner.

    Construction has no external side effect. :class:`TTSPipeline` registers
    the owner before calling :meth:`MacOSSayProcessOwner.run`, giving close a
    linearizable cancellation point. ADR-0007 will eventually replace this
    leaf with a ``surface.failed`` event.
    """
    return MacOSSayProcessOwner(text, voice=voice)


__all__ = [
    "AudioStreamPlayer",
    "FallbackOwner",
    "MacOSSayProcessOwner",
    "MiniMaxUnavailableError",
    "MiniMaxWSClient",
    "PlayerStartResult",
    "PlayerStopResult",
    "TTSPipeline",
    "_preprocess_for_speech",
    "macos_say_fallback",
]
