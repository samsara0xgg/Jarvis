"""ADR-0005 §4.2 + spec §3.6.1 — voice_audio: Silero VAD + PortAudio recorder.

Direct onnxruntime Silero VAD with frame-level probability + dBFS gating.
Replaces sherpa-onnx's segment-level wrapper so wake / PTT paths can react
to the earliest sign of user speech at 32 ms granularity (512 samples @ 16
kHz).

Public surface:
  - :data:`SILERO_CHUNK_SAMPLES`           module constant (= 512)
  - :class:`VadEvent`                     per-frame classification enum
  - :class:`SileroVad`                    state machine + ONNX runner
  - :func:`capture_utterance`             VAD-gated PortAudio recorder
  - :func:`_load_silero_session`          lazy session factory (test-patchable)
  - :func:`_open_input_stream`            lazy PortAudio factory (test-patchable)

ONNX I/O (pre-v4 silero_vad.onnx shipped with sherpa-onnx)::

    Inputs:  x  float32[1, 512]
             h  float32[2, 1, 64]
             c  float32[2, 1, 64]
    Outputs: prob   float32[1, 1]
             new_h  float32[2, 1, 64]
             new_c  float32[2, 1, 64]
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface import voice_backend

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

SILERO_CHUNK_SAMPLES = 512  # silero fixed-size per inference (32 ms @ 16 kHz)
_LSTM_SHAPE = (2, 1, 64)
_SAMPLE_RATE = 16000
_SILERO_FRAME_MS = SILERO_CHUNK_SAMPLES / _SAMPLE_RATE * 1000.0
_PREWARM_FRAMES = 5
_DEFAULT_DEVICE_MISS_LIMIT = 3
_REQUIRED_SESSION_SUBSCRIBERS = 3


class VadEvent(enum.Enum):
    """Per-frame VAD classification result returned by :meth:`SileroVad.feed`."""

    SPEECH_ACTIVE = "speech_active"
    SILENCE = "silence"


@dataclass(frozen=True)
class VadThresholds:
    """Mode-specific Silero thresholds (legacy ``vad_silero.build_vad`` defaults)."""

    prob_threshold: float
    db_threshold: float
    smoothing_window: int = 5
    required_hits: int = 3
    required_misses: int = 24


# Mode → thresholds. Record mode is more sensitive (catches user mid-thought).
# TTS mode is stricter so playback bleed doesn't false-trigger a barge-in.
_MODE_THRESHOLDS: dict[str, VadThresholds] = {
    "record": VadThresholds(prob_threshold=0.4, db_threshold=-45.0),
    "tts": VadThresholds(prob_threshold=0.5, db_threshold=-22.0),
}


def _load_silero_session(
    model_path: Path | None = None,
) -> Any:  # noqa: ANN401 — onnxruntime typing is dynamic
    """Lazy-load a Silero ONNX :class:`onnxruntime.InferenceSession`.

    Extracted as a module-level function so tests can patch it without
    needing the actual ``silero_vad.onnx`` artifact. The class's
    ``__init__`` is intentionally cheap; the ONNX file is only opened on
    the first call to :meth:`SileroVad.feed`.

    Args:
        model_path: optional override; ``None`` uses the project default
            location (resolved by the recorder wiring in Task 8).
    """
    import onnxruntime as ort  # noqa: PLC0415

    if model_path is None:
        msg = "Silero model_path must be supplied (wired in surface.voice_audio recorder)"
        raise ValueError(msg)
    return ort.InferenceSession(
        str(model_path),
        providers=["CPUExecutionProvider"],
    )


class SileroVad:
    """Frame-level Silero VAD with IDLE/ACTIVE state machine.

    Threshold defaults follow legacy ``core.vad_silero.build_vad``:
      - ``record`` mode: prob >= 0.4, dB >= -45
      - ``tts`` mode:    prob >= 0.5, dB >= -22 (macOS speakers default)

    The state machine debounces noisy per-frame output: ``required_hits``
    consecutive (smoothed) speech frames trigger IDLE -> ACTIVE; then
    ``required_misses`` consecutive silence frames trigger ACTIVE -> IDLE.
    Smoothing window of 5 frames (~160 ms) averages out the model's
    single-frame jitter.

    :meth:`feed` returns the per-frame classification (instantaneous),
    while :meth:`empty` flips to ``True`` once the configured number of
    consecutive post-speech silence frames has been observed — the recorder
    uses that as the stop signal.
    """

    def __init__(
        self,
        *,
        mode: str,
        model_path: Path | None = None,
    ) -> None:
        """Construct a Silero VAD bound to ``mode`` ('record' | 'tts')."""
        if mode not in _MODE_THRESHOLDS:
            msg = f"unknown VAD mode {mode!r}; expected one of {list(_MODE_THRESHOLDS)}"
            raise ValueError(msg)
        self._mode = mode
        self._model_path = model_path
        self._t = _MODE_THRESHOLDS[mode]

        # Lazy: session opened on first feed() call so tests can patch
        # _load_silero_session without an actual ONNX file present.
        self._session: Any | None = None

        # LSTM + state machine bookkeeping — populated by reset().
        self._h: np.ndarray
        self._c: np.ndarray
        self._prob_window: deque[float]
        self._db_window: deque[float]
        self._state: str
        self._hits: int
        self._misses: int
        self._post_speech_silence_seen: bool
        self._last_start_perf: float | None
        self.reset()

    # ------------------------------------------------------------------
    # Classmethod surface — exposed for tests / config introspection
    # ------------------------------------------------------------------

    @classmethod
    def thresholds(cls, mode: str) -> VadThresholds:
        """Return the legacy :class:`VadThresholds` defaults for ``mode``."""
        if mode not in _MODE_THRESHOLDS:
            msg = f"unknown VAD mode {mode!r}; expected one of {list(_MODE_THRESHOLDS)}"
            raise ValueError(msg)
        return _MODE_THRESHOLDS[mode]

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear LSTM state and state-machine bookkeeping. Call before each session."""
        self._h = np.zeros(_LSTM_SHAPE, dtype=np.float32)
        self._c = np.zeros(_LSTM_SHAPE, dtype=np.float32)
        self._prob_window = deque(maxlen=self._t.smoothing_window)
        self._db_window = deque(maxlen=self._t.smoothing_window)
        self._state = "IDLE"
        self._hits = 0
        self._misses = 0
        self._post_speech_silence_seen = False
        self._last_start_perf = None

    def prepare_utterance(self) -> None:
        """Reset utterance-local state and converge the LSTM on silence.

        The five prewarm inferences update only the provider's recurrent
        state; smoothing and endpoint counters stay empty until real capture
        frames arrive. Calling this before the input stream opens also keeps
        model/session cold-start work from consuming the start of speech.
        """
        self.reset()
        silence = np.zeros(SILERO_CHUNK_SAMPLES, dtype=np.float32)
        for _ in range(_PREWARM_FRAMES):
            self._infer_chunk(silence)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, frame: bytes) -> VadEvent:
        """Feed a single 512-sample int16 PCM frame; return per-frame classification.

        Frame contract: ``len(frame) == SILERO_CHUNK_SAMPLES * 2`` (int16
        little-endian). Anything else raises :class:`ValueError`.

        Returns :data:`VadEvent.SPEECH_ACTIVE` when the smoothed prob+dB
        gate classifies this frame as speech; :data:`VadEvent.SILENCE`
        otherwise. The IDLE/ACTIVE state machine runs underneath and
        feeds :meth:`empty`.
        """
        expected_bytes = SILERO_CHUNK_SAMPLES * 2
        if len(frame) != expected_bytes:
            msg = (
                f"frame must be {expected_bytes} bytes (512 int16 samples), "
                f"got {len(frame)}"
            )
            raise ValueError(msg)

        # int16 PCM → normalized float32 in [-1, 1].
        chunk = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0

        prob = self._infer_chunk(chunk)
        db = _chunk_db(chunk)
        is_speech = self._advance_state(prob, db)
        return VadEvent.SPEECH_ACTIVE if is_speech else VadEvent.SILENCE

    def empty(self) -> bool:
        """``True`` once consecutive post-speech silence reaches its threshold.

        The recorder polls this after each frame and stops capture when it
        flips. Any intervening speech resets the consecutive-miss counter.
        """
        return self._post_speech_silence_seen

    def is_speech_detected(self) -> bool:
        """``True`` while currently in ACTIVE state (legacy wrapper compat)."""
        return self._state == "ACTIVE"

    @property
    def last_start_perf(self) -> float | None:
        """``time.perf_counter()`` at last IDLE→ACTIVE transition (``None`` if never)."""
        return self._last_start_perf

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_session(self) -> Any:  # noqa: ANN401 — onnxruntime typing is dynamic
        if self._session is None:
            self._session = _load_silero_session(self._model_path)
        return self._session

    def _infer_chunk(self, chunk: np.ndarray) -> float:
        session = self._ensure_session()
        x = chunk.reshape(1, -1).astype(np.float32, copy=False)
        outputs = session.run(
            None,
            {"x": x, "h": self._h, "c": self._c},
        )
        prob = float(np.asarray(outputs[0]).squeeze())
        self._h = outputs[1]
        self._c = outputs[2]
        return prob

    def _advance_state(self, prob: float, db: float) -> bool:
        """Advance state machine; return per-frame is_speech (instantaneous)."""
        self._prob_window.append(prob)
        self._db_window.append(db)
        smooth_prob = float(np.mean(self._prob_window))
        smooth_db = float(np.mean(self._db_window))

        # A frame counts as speech only when BOTH probability and energy
        # clear their thresholds. The dB gate suppresses AEC residual +
        # low-level background noise the model occasionally scores high on.
        is_speech = (
            smooth_prob >= self._t.prob_threshold
            and smooth_db >= self._t.db_threshold
        )

        if self._state == "IDLE":
            if is_speech:
                self._hits += 1
                if self._hits >= self._t.required_hits:
                    self._state = "ACTIVE"
                    self._misses = 0
                    self._last_start_perf = time.perf_counter()
                    record_realtime_trace(
                        "vad_speech_started",
                        vad_mode=self._mode,
                        required_hits=self._t.required_hits,
                    )
            else:
                self._hits = 0
        elif not is_speech:
            self._misses += 1
            if self._misses >= self._t.required_misses:
                self._state = "IDLE"
                self._hits = 0
                self._post_speech_silence_seen = True
                record_realtime_trace(
                    "endpoint_candidate",
                    vad_mode=self._mode,
                    consecutive_silence_frames=self._misses,
                    consecutive_silence_audio_ms=round(
                        self._misses * _SILERO_FRAME_MS,
                        3,
                    ),
                )
        else:
            # ACTIVE + still speech.
            self._misses = 0

        return is_speech


def _chunk_db(chunk: np.ndarray) -> float:
    """Approximate per-frame dBFS (epsilon floor avoids ``-inf``)."""
    rms = float(np.sqrt(np.mean(chunk * chunk)))
    return 20.0 * float(np.log10(rms + 1e-10))


# --- Recorder (PortAudio + VAD-gated end-of-speech) ----------------------


def _open_input_stream(*, sample_rate_hz: int, blocksize: int) -> Any:  # noqa: ANN401
    """Open a blocking PortAudio input stream.

    Module-level so tests can :func:`unittest.mock.patch.object` it without
    needing PortAudio installed. ``sounddevice`` is imported lazily so the
    surrounding module stays importable in environments where it isn't
    available (CI, headless test runners).
    """
    import sounddevice as sd  # noqa: PLC0415

    return sd.RawInputStream(
        samplerate=sample_rate_hz,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
    )


def capture_utterance(
    *,
    vad: SileroVad,
    max_duration_s: float,
    min_voiced_s: float,
    sample_rate_hz: int = 16000,
) -> bytes:
    """VAD-gated capture from the default input device.

    Reads PCM16 mono frames of :data:`SILERO_CHUNK_SAMPLES` each (32 ms at
    16 kHz). Feeds each frame into the VAD; once at least ``min_voiced_s``
    worth of voiced frames have been seen AND the VAD's :meth:`empty` flag
    becomes ``True`` (post-speech silence), the recording ends.

    Hard cap: ``max_duration_s`` enforced via frame-count.

    Returns the concatenated raw PCM16 little-endian bytes.

    Args:
        vad: a constructed :class:`SileroVad` in ``"record"`` mode.
        max_duration_s: hard upper bound (ADR-0005 §5.1 default 5.0).
        min_voiced_s: minimum voiced duration before VAD-cut allowed
            (ADR-0005 §5.1 default 1.0).
        sample_rate_hz: capture rate; MUST match VAD's expected rate
            (Silero ships 16 kHz).
    """
    blocksize = SILERO_CHUNK_SAMPLES
    seconds_per_frame = blocksize / sample_rate_hz
    max_frames = int(max_duration_s / seconds_per_frame) + 1
    min_voiced_frames = int(min_voiced_s / seconds_per_frame)

    audio_bytes = bytearray()
    voiced_frames = 0

    vad.prepare_utterance()
    with _open_input_stream(sample_rate_hz=sample_rate_hz, blocksize=blocksize) as stream:
        for _frame_idx in range(max_frames):
            frame, _overflowed = stream.read(blocksize)
            audio_bytes.extend(frame)
            event = vad.feed(bytes(frame))
            if event == VadEvent.SPEECH_ACTIVE:
                voiced_frames += 1
            if voiced_frames >= min_voiced_frames and vad.empty():
                break

    return bytes(audio_bytes)


# --- ADR-0006 Step 5: one callback ingress + bounded canonical fan-out ---


class SubscriberPurpose(enum.Enum):
    """Overflow semantics for one canonical ingress subscriber."""

    WAKE = "wake"
    CAPTURE = "capture"
    DIAGNOSTIC = "diagnostic"


class InputCapabilityState(enum.Enum):
    """Precise local-input capability advertised by the sole owner."""

    STOPPED = "stopped"
    AVAILABLE = "available"
    SUSPENDED = "suspended"
    WAKE_UNAVAILABLE = "wake_unavailable"
    LOCAL_CAPTURE_UNAVAILABLE = "local_capture_unavailable"
    CLOSE_UNCERTAIN = "close_uncertain"


@dataclass(frozen=True)
class AudioIngressConfig:
    """Hard bounds and canonical format for one audio ingress."""

    canonical_sample_rate_hz: int = 16_000
    canonical_frame_samples: int = SILERO_CHUNK_SAMPLES
    native_ring_capacity: int = 128
    default_subscriber_capacity: int = 64
    max_subscribers: int = 8
    worker_poll_s: float = 0.002
    fault_poll_s: float = 0.02
    route_poll_s: float = 1.0
    reopen_attempts: int = 3
    reopen_initial_backoff_s: float = 0.05
    reopen_max_backoff_s: float = 0.5
    backend_open_timeout_s: float = 2.0
    backend_close_timeout_s: float = 2.0
    shutdown_timeout_s: float = 2.0


@dataclass(frozen=True)
class CanonicalAudioFrame:
    """Owned canonical PCM frame safe beyond every callback/ring lifetime."""

    stream_epoch: int
    sequence: int
    sample_cursor: int
    sample_rate_hz: int
    frame_count: int
    adc_time_s: float | None
    captured_monotonic_ns: int
    discontinuity_before: bool
    pcm16_mono: bytes
    measurement_boundary: str = "software_canonical_frame"


@dataclass(frozen=True)
class InputCapabilitySnapshot:
    """Capability downgrade published by the unique input owner."""

    state: InputCapabilityState
    stream_epoch: int | None
    reason: str
    wake_available: bool
    local_capture_available: bool
    ptt_upload_available: bool = True
    text_available: bool = True


@dataclass(frozen=True)
class IngressStartResult:
    """Typed startup result used by runtime fallback arbitration."""

    started: bool
    capability: InputCapabilitySnapshot
    backend_result: voice_backend.BackendStartResult


@dataclass(frozen=True)
class IngressCloseResult:
    """Typed shutdown proof for the worker, backend, and subscribers."""

    definitively_closed: bool
    stream_epoch: int | None
    backend_result: voice_backend.BackendStopResult | None
    worker_alive: bool
    open_subscribers: int


@dataclass(frozen=True)
class IngressMetrics:
    """Bounded raw counters; none claim physical acoustic truth."""

    stream_epoch: int | None
    callback_calls: int
    callback_frames: int
    first_callback_monotonic_ns: int | None
    input_sample_cursor: int
    canonical_frames: int
    late_epoch_callbacks_rejected: int
    callback_shape_faults: int
    native_overflows: int
    subscriber_overflows: int
    active_discontinuities: int
    faults: int
    reopen_attempts: int
    reopen_successes: int


class _CapabilitySink(Protocol):
    """Runtime-facing notification invoked only off the ADC callback."""

    def __call__(self, snapshot: InputCapabilitySnapshot) -> None:
        """Observe one precise capability transition."""
        ...


@dataclass
class _PcmSlot:
    """One preallocated ring slot; metadata is published after byte copy."""

    storage: bytearray
    committed_index: int = -1
    length: int = 0
    stream_epoch: int = 0
    sequence: int = 0
    sample_cursor: int = 0
    sample_rate_hz: int = 0
    channels: int = 0
    frame_count: int = 0
    adc_time_s: float | None = None
    captured_monotonic_ns: int = 0
    discontinuity_before: bool = False


@dataclass(frozen=True)
class _OwnedPcmFrame:
    """Consumer-owned copy from one preallocated SPSC slot."""

    stream_epoch: int
    sequence: int
    sample_cursor: int
    sample_rate_hz: int
    channels: int
    frame_count: int
    adc_time_s: float | None
    captured_monotonic_ns: int
    discontinuity_before: bool
    pcm: bytes


class _PreallocatedPcmRing:
    """Bounded SPSC PCM ring with policy-aware overflow markers.

    The producer never waits or grows storage.  Consumers receive a fresh
    bounded ``bytes`` copy, so a later producer wrap cannot mutate a retained
    frame.  CPython's GIL serializes the slot copy and index publication; the
    commit index additionally rejects a slot observed during a wrap.
    """

    def __init__(self, *, capacity: int, max_frame_bytes: int) -> None:
        if capacity <= 0 or max_frame_bytes <= 0:
            msg = "PCM ring capacity and max_frame_bytes must be positive"
            raise ValueError(msg)
        self._capacity = capacity
        self._max_frame_bytes = max_frame_bytes
        self._slots = [_PcmSlot(storage=bytearray(max_frame_bytes)) for _ in range(capacity)]
        self._write_index = 0
        self._read_index = 0
        self._pending_discontinuity = False
        self._closed = False
        self.overflow_count = 0
        self.active_discontinuity_count = 0

    def write(  # noqa: PLR0913 - callback metadata is the fixed frame contract
        self,
        *,
        pcm: Any,  # noqa: ANN401
        byte_count: int,
        stream_epoch: int,
        sequence: int,
        sample_cursor: int,
        sample_rate_hz: int,
        channels: int,
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
        purpose: SubscriberPurpose,
        active: bool,
    ) -> bool:
        """Copy one frame without blocking; return whether it was published."""
        if self._closed or byte_count <= 0 or byte_count > self._max_frame_bytes:
            self._pending_discontinuity = True
            return False
        if self._write_index - self._read_index >= self._capacity:
            self.overflow_count += 1
            self._pending_discontinuity = True
            if purpose is SubscriberPurpose.DIAGNOSTIC:
                return False
            if purpose is SubscriberPurpose.CAPTURE and active:
                self.active_discontinuity_count += 1
                self._read_index = self._write_index
            else:
                self._read_index += 1
        slot_index = self._write_index % self._capacity
        slot = self._slots[slot_index]
        try:
            source = memoryview(pcm).cast("B")
            if len(source) < byte_count:
                self._pending_discontinuity = True
                return False
            slot.storage[:byte_count] = source[:byte_count]
        except (BufferError, TypeError, ValueError):
            self._pending_discontinuity = True
            return False
        slot.length = byte_count
        slot.stream_epoch = stream_epoch
        slot.sequence = sequence
        slot.sample_cursor = sample_cursor
        slot.sample_rate_hz = sample_rate_hz
        slot.channels = channels
        slot.frame_count = frame_count
        slot.adc_time_s = adc_time_s
        slot.captured_monotonic_ns = captured_monotonic_ns
        slot.discontinuity_before = discontinuity_before or self._pending_discontinuity
        slot.committed_index = self._write_index
        self._pending_discontinuity = False
        self._write_index += 1
        return True

    def read(self) -> _OwnedPcmFrame | None:
        """Return one owned frame or ``None`` when empty/closed."""
        if self._read_index >= self._write_index:
            return None
        expected_index = self._read_index
        slot = self._slots[expected_index % self._capacity]
        if slot.committed_index != expected_index:
            # Producer dropped/overwrote this position. Re-anchor to the oldest
            # position still representable and make the next frame explicit.
            self._read_index = max(self._read_index + 1, self._write_index - self._capacity)
            self._pending_discontinuity = True
            return None
        payload = bytes(memoryview(slot.storage)[: slot.length])
        frame = _OwnedPcmFrame(
            stream_epoch=slot.stream_epoch,
            sequence=slot.sequence,
            sample_cursor=slot.sample_cursor,
            sample_rate_hz=slot.sample_rate_hz,
            channels=slot.channels,
            frame_count=slot.frame_count,
            adc_time_s=slot.adc_time_s,
            captured_monotonic_ns=slot.captured_monotonic_ns,
            discontinuity_before=(slot.discontinuity_before or self._pending_discontinuity),
            pcm=payload,
        )
        if slot.committed_index != expected_index:
            self._pending_discontinuity = True
            return None
        self._pending_discontinuity = False
        self._read_index += 1
        return frame

    def close(self) -> None:
        """Reject new writes and discard unread storage."""
        self._closed = True
        self._read_index = self._write_index

    def discard_all(self, *, discontinuity: bool) -> None:
        """Drop unread frames while retaining the fixed storage allocation."""
        self._read_index = self._write_index
        self._pending_discontinuity = discontinuity


class AudioIngressCanonicalizer:
    """Single stateful native→16 kHz mono PCM16 conversion owner."""

    def __init__(self, *, sample_rate_hz: int, frame_samples: int) -> None:
        """Create one canonical timeline; ``reset`` starts each epoch."""
        self._sample_rate_hz = sample_rate_hz
        self._frame_samples = frame_samples
        self._pending = bytearray()
        self._stream_epoch: int | None = None
        self._canonical_cursor = 0
        self._canonical_sequence = 0
        self._source_cursor = 0
        self._next_output_source_position = 0.0
        self._previous_source_sample: float | None = None
        self._pending_discontinuity = False
        self._last_adc_time_s: float | None = None
        self._last_monotonic_ns = 0

    def reset(self, *, stream_epoch: int, discontinuity: bool) -> None:
        """Reset resampler/framer state on every epoch or discontinuity."""
        self._pending.clear()
        self._stream_epoch = stream_epoch
        self._canonical_cursor = 0
        self._canonical_sequence = 0
        self._source_cursor = 0
        self._next_output_source_position = 0.0
        self._previous_source_sample = None
        self._pending_discontinuity = discontinuity
        self._last_adc_time_s = None
        self._last_monotonic_ns = 0

    def feed(self, native: _OwnedPcmFrame) -> tuple[CanonicalAudioFrame, ...]:
        """Canonicalize one owned native frame into zero or more fixed frames."""
        if self._stream_epoch != native.stream_epoch or native.discontinuity_before:
            self.reset(
                stream_epoch=native.stream_epoch,
                discontinuity=native.discontinuity_before,
            )
        raw = np.frombuffer(native.pcm, dtype="<i2")
        expected = native.frame_count * native.channels
        if raw.size != expected:
            self._pending_discontinuity = True
            return ()
        if native.channels > 1:
            mixed = np.mean(
                raw.reshape(native.frame_count, native.channels).astype(np.float32),
                axis=1,
            )
        else:
            mixed = raw.astype(np.float32)
        if native.sample_rate_hz == self._sample_rate_hz:
            canonical = np.clip(np.rint(mixed), -32768, 32767).astype("<i2")
        else:
            canonical = self._resample_linear(mixed, native.sample_rate_hz)
        self._pending.extend(canonical.tobytes())
        self._last_adc_time_s = native.adc_time_s
        self._last_monotonic_ns = native.captured_monotonic_ns
        frames: list[CanonicalAudioFrame] = []
        frame_bytes = self._frame_samples * 2
        while len(self._pending) >= frame_bytes:
            payload = bytes(self._pending[:frame_bytes])
            del self._pending[:frame_bytes]
            frames.append(
                CanonicalAudioFrame(
                    stream_epoch=native.stream_epoch,
                    sequence=self._canonical_sequence,
                    sample_cursor=self._canonical_cursor,
                    sample_rate_hz=self._sample_rate_hz,
                    frame_count=self._frame_samples,
                    adc_time_s=self._last_adc_time_s,
                    captured_monotonic_ns=self._last_monotonic_ns,
                    discontinuity_before=self._pending_discontinuity,
                    pcm16_mono=payload,
                ),
            )
            self._pending_discontinuity = False
            self._canonical_sequence += 1
            self._canonical_cursor += self._frame_samples
        return tuple(frames)

    def _resample_linear(self, samples: np.ndarray, source_rate_hz: int) -> np.ndarray:
        """Stateful bounded linear resampling for non-native fake/future backends."""
        if source_rate_hz <= 0 or samples.size == 0:
            return np.empty(0, dtype="<i2")
        source_start = self._source_cursor
        source_end = source_start + int(samples.size)
        if self._previous_source_sample is None:
            coordinates = np.arange(source_start, source_end, dtype=np.float64)
            values = samples
        else:
            coordinates = np.arange(source_start - 1, source_end, dtype=np.float64)
            values = np.concatenate(
                (np.asarray([self._previous_source_sample], dtype=np.float32), samples),
            )
        step = source_rate_hz / self._sample_rate_hz
        last_interpolable = source_end - 1
        if self._next_output_source_position > last_interpolable:
            output_positions = np.empty(0, dtype=np.float64)
        else:
            count = (
                int(
                    (last_interpolable - self._next_output_source_position) / step,
                )
                + 1
            )
            output_positions = self._next_output_source_position + step * np.arange(
                count,
                dtype=np.float64,
            )
            self._next_output_source_position += count * step
        self._source_cursor = source_end
        self._previous_source_sample = float(samples[-1])
        if output_positions.size == 0:
            return np.empty(0, dtype="<i2")
        output = np.interp(output_positions, coordinates, values)
        resampled: np.ndarray = np.clip(np.rint(output), -32768, 32767).astype(
            "<i2",
        )
        return resampled


class AudioSubscription:
    """One bounded canonical SPSC subscriber owned by ``AudioIngress``."""

    def __init__(
        self,
        *,
        ingress: AudioIngress,
        name: str,
        purpose: SubscriberPurpose,
        ring: _PreallocatedPcmRing,
    ) -> None:
        """Bind one subscriber to its ingress-owned fixed ring."""
        self._ingress = ingress
        self.name = name
        self.purpose = purpose
        self._ring = ring
        self._active = False
        self._closed = False

    def set_active_utterance(self, *, active: bool) -> None:
        """Select capture overflow policy without touching the ADC callback."""
        if self.purpose is not SubscriberPurpose.CAPTURE and active:
            msg = "only capture subscribers may enter active-utterance mode"
            raise ValueError(msg)
        self._active = active
        self._ingress._refresh_capture_active()  # noqa: SLF001 - paired owner

    @property
    def active_utterance(self) -> bool:
        """Return the current capture overflow mode."""
        return self._active

    def read(self, *, timeout_s: float = 0.0) -> CanonicalAudioFrame | None:
        """Read one owned frame with a bounded polling wait."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while not self._closed:
            native = self._ring.read()
            if native is not None:
                return CanonicalAudioFrame(
                    stream_epoch=native.stream_epoch,
                    sequence=native.sequence,
                    sample_cursor=native.sample_cursor,
                    sample_rate_hz=native.sample_rate_hz,
                    frame_count=native.frame_count,
                    adc_time_s=native.adc_time_s,
                    captured_monotonic_ns=native.captured_monotonic_ns,
                    discontinuity_before=native.discontinuity_before,
                    pcm16_mono=native.pcm,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self._ingress.worker_poll_s, remaining))
        return None

    def close(self) -> None:
        """Unsubscribe idempotently and release unread bounded storage."""
        if self._closed:
            return
        self._closed = True
        self._ring.close()
        self._ingress._unsubscribe(self)  # noqa: SLF001 - paired owner method

    @property
    def overflow_count(self) -> int:
        """Return lifetime software-subscriber overflow count."""
        return self._ring.overflow_count

    @property
    def active_discontinuity_count(self) -> int:
        """Return active-utterance overflows that require fail/restart."""
        return self._ring.active_discontinuity_count


class AudioIngress:
    """The daemon's single logical owner of local input and its timeline."""

    def __init__(
        self,
        *,
        backend: voice_backend.AudioDuplexBackend,
        config: AudioIngressConfig,
        capability_sink: _CapabilitySink | None = None,
    ) -> None:
        """Build a stopped ingress; subscribers may register before ``start``."""
        self._backend = backend
        self._config = config
        self._capability_sink = capability_sink
        native_format = backend.input_format()
        self._native_format = native_format
        self._native_ring = _PreallocatedPcmRing(
            capacity=config.native_ring_capacity,
            max_frame_bytes=native_format.bytes_per_callback,
        )
        self._canonicalizer = AudioIngressCanonicalizer(
            sample_rate_hz=config.canonical_sample_rate_hz,
            frame_samples=config.canonical_frame_samples,
        )
        self._subscriber_lock = threading.Lock()
        self._subscribers: dict[str, AudioSubscription] = {}
        self._subscriber_snapshot: tuple[AudioSubscription, ...] = ()
        self._lifecycle_lock = threading.Lock()
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._active_epoch: int | None = None
        self._last_epoch = 0
        self._active_profile: voice_backend.InputDeviceProfile | None = None
        self._closing = False
        self._suspended = False
        self._callback_sequence = 0
        self._input_sample_cursor = 0
        self._callback_calls = 0
        self._callback_frames = 0
        self._first_callback_monotonic_ns: int | None = None
        self._first_callback_adc_time_s: float | None = None
        self._reported_first_callback_epoch: int | None = None
        self._late_epoch_callbacks_rejected = 0
        self._callback_shape_faults = 0
        self._canonical_frames = 0
        self._faults = 0
        self._reopen_attempts = 0
        self._reopen_successes = 0
        self._capture_active = False
        self._uncertain_epoch: int | None = None
        self._pending_ingress_fault_epoch: int | None = None
        self._pending_ingress_fault_code: str | None = None
        self._device_uid_misses = 0
        self._capability = InputCapabilitySnapshot(
            state=InputCapabilityState.STOPPED,
            stream_epoch=None,
            reason="not_started",
            wake_available=False,
            local_capture_available=False,
        )

    @property
    def worker_poll_s(self) -> float:
        """Expose the bounded subscriber polling cadence."""
        return self._config.worker_poll_s

    def subscribe(
        self,
        *,
        name: str,
        purpose: SubscriberPurpose,
        capacity: int | None = None,
    ) -> AudioSubscription:
        """Register one bounded canonical SPSC lane."""
        if not name:
            msg = "audio subscriber name must be non-empty"
            raise ValueError(msg)
        resolved_capacity = capacity or self._config.default_subscriber_capacity
        ring = _PreallocatedPcmRing(
            capacity=resolved_capacity,
            max_frame_bytes=self._config.canonical_frame_samples * 2,
        )
        subscription = AudioSubscription(
            ingress=self,
            name=name,
            purpose=purpose,
            ring=ring,
        )
        with self._subscriber_lock:
            if self._closing:
                msg = "audio ingress is closing"
                raise RuntimeError(msg)
            if name in self._subscribers:
                msg = f"audio subscriber already exists: {name}"
                raise ValueError(msg)
            if len(self._subscribers) >= self._config.max_subscribers:
                msg = "audio subscriber bound reached"
                raise RuntimeError(msg)
            self._subscribers[name] = subscription
            self._subscriber_snapshot = tuple(self._subscribers.values())
        return subscription

    def _unsubscribe(self, subscription: AudioSubscription) -> None:
        with self._subscriber_lock:
            if self._subscribers.get(subscription.name) is subscription:
                del self._subscribers[subscription.name]
                self._subscriber_snapshot = tuple(self._subscribers.values())
                self._refresh_capture_active()

    def _refresh_capture_active(self) -> None:
        """Publish one callback-readable scalar outside the ADC callback."""
        self._capture_active = any(
            sub.purpose is SubscriberPurpose.CAPTURE and sub.active_utterance
            for sub in self._subscriber_snapshot
        )

    def start(self) -> IngressStartResult:
        """Start the canonicalizer worker and exactly one backend epoch."""
        with self._lifecycle_lock:
            if self._worker is not None:
                msg = "audio ingress already started"
                raise RuntimeError(msg)
            self._worker_stop.clear()
            self._closing = False
            self._worker = threading.Thread(
                target=self._run_worker,
                name="jarvis-audio-ingress",
                daemon=False,
            )
            self._worker.start()
            backend_result = self._open_new_epoch(reason="startup")
            if backend_result.started:
                self._uncertain_epoch = None
                snapshot = self._publish_capability(
                    InputCapabilityState.AVAILABLE,
                    reason="input_stream_started",
                )
                return IngressStartResult(
                    started=True,
                    capability=snapshot,
                    backend_result=backend_result,
                )
            self._worker_stop.set()
            if backend_result.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN:
                self._uncertain_epoch = backend_result.stream_epoch
            worker = self._worker
        if worker is not None:
            worker.join(timeout=self._config.shutdown_timeout_s)
        state = (
            InputCapabilityState.CLOSE_UNCERTAIN
            if backend_result.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
            else InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
        )
        snapshot = self._publish_capability(
            state,
            reason=backend_result.reason or backend_result.status.value,
        )
        return IngressStartResult(
            started=False,
            capability=snapshot,
            backend_result=backend_result,
        )

    def _open_new_epoch(self, *, reason: str) -> voice_backend.BackendStartResult:
        self._last_epoch += 1
        epoch = self._last_epoch
        self._active_profile = None
        self._callback_sequence = 0
        self._input_sample_cursor = 0
        self._first_callback_monotonic_ns = None
        self._first_callback_adc_time_s = None
        self._reported_first_callback_epoch = None
        self._pending_ingress_fault_epoch = None
        self._pending_ingress_fault_code = None
        self._device_uid_misses = 0
        # Publish the epoch only after every epoch-local clock/cursor scalar is
        # reset. The backend may invoke its first callback as soon as start()
        # begins, so this assignment must still precede that foreign call.
        self._active_epoch = epoch
        # The epoch itself is the boundary; the first frame is not falsely
        # labelled as an in-epoch loss. Old queued frames are discarded before
        # the backend may publish the new cursor-zero callback.
        self._canonicalizer.reset(stream_epoch=epoch, discontinuity=False)
        self._native_ring.discard_all(discontinuity=False)
        for subscriber in self._subscriber_snapshot:
            subscriber._ring.discard_all(discontinuity=False)  # noqa: SLF001
        result = self._backend.start(
            stream_epoch=epoch,
            frame_sink=self._on_backend_frame,
            render_source=None,
        )
        if not result.started:
            self._active_epoch = None
            return result
        if self._closing or self._suspended or self._worker_stop.is_set():
            self._active_epoch = None
            stop_result = self._backend.stop(stream_epoch=epoch)
            return voice_backend.BackendStartResult(
                status=(
                    voice_backend.BackendStartStatus.FAILED_CLOSED
                    if stop_result.definitively_closed
                    else voice_backend.BackendStartStatus.OPEN_UNCERTAIN
                ),
                stream_epoch=epoch,
                profile=None,
                reason="owner_revoked_during_open",
            )
        self._active_profile = result.profile
        record_realtime_trace(
            "audio_input_epoch_opened",
            stream_epoch=epoch,
            reason=reason,
            device_uid=result.profile.device_uid if result.profile is not None else None,
            input_sample_cursor=0,
            measurement_boundary="software_epoch_not_first_callback",
        )
        return result

    def _on_backend_frame(  # noqa: PLR0913 - fixed backend sink contract
        self,
        *,
        stream_epoch: int,
        callback_buffer: Any,  # noqa: ANN401
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
    ) -> None:
        """ADC callback sink: validate epoch and copy into one fixed ring."""
        active_epoch = self._active_epoch
        if self._closing or self._suspended or active_epoch != stream_epoch:
            self._late_epoch_callbacks_rejected += 1
            return
        native_format = self._native_format
        if frame_count != native_format.callback_frame_samples:
            self._callback_shape_faults += 1
            self._pending_ingress_fault_epoch = stream_epoch
            self._pending_ingress_fault_code = "unexpected_callback_frame_count"
            return
        sequence = self._callback_sequence
        sample_cursor = self._input_sample_cursor
        self._callback_sequence += 1
        self._input_sample_cursor += frame_count
        self._callback_calls += 1
        self._callback_frames += frame_count
        if self._first_callback_monotonic_ns is None:
            self._first_callback_adc_time_s = adc_time_s
            # Monotonic is the publication scalar: off-callback readers that
            # observe it also see the paired ADC anchor under CPython's GIL.
            self._first_callback_monotonic_ns = captured_monotonic_ns
        overflow_count = self._native_ring.overflow_count
        published = self._native_ring.write(
            pcm=callback_buffer,
            byte_count=frame_count * native_format.channels * 2,
            stream_epoch=stream_epoch,
            sequence=sequence,
            sample_cursor=sample_cursor,
            sample_rate_hz=native_format.sample_rate_hz,
            channels=native_format.channels,
            frame_count=frame_count,
            adc_time_s=adc_time_s,
            captured_monotonic_ns=captured_monotonic_ns,
            discontinuity_before=discontinuity_before,
            purpose=SubscriberPurpose.CAPTURE,
            active=self._capture_active,
        )
        if not published and self._native_ring.overflow_count == overflow_count:
            self._pending_ingress_fault_epoch = stream_epoch
            self._pending_ingress_fault_code = "callback_buffer_copy_failed"

    def _run_worker(self) -> None:  # noqa: C901, PLR0912 - one owner loop serializes frame/fault/route ordering
        """Canonicalize/fan out frames and own bounded fault recovery."""
        last_fault_poll = 0.0
        last_route_poll = 0.0
        while not self._worker_stop.is_set():
            did_work = False
            native = self._native_ring.read()
            while native is not None:
                did_work = True
                for frame in self._canonicalizer.feed(native):
                    self._fan_out(frame)
                native = self._native_ring.read()
            epoch = self._active_epoch
            now = time.monotonic()
            if (
                epoch is not None
                and self._reported_first_callback_epoch != epoch
                and self._first_callback_monotonic_ns is not None
            ):
                self._reported_first_callback_epoch = epoch
                record_realtime_trace(
                    "audio_input_first_callback",
                    stream_epoch=epoch,
                    callback_monotonic_ns=self._first_callback_monotonic_ns,
                    adc_time_s=self._first_callback_adc_time_s,
                    input_sample_cursor=0,
                    input_clock_domain="portaudio_adc_time_and_process_monotonic",
                    measurement_boundary="adc_callback_arrival_not_acoustic_truth",
                )
            if epoch is not None and now - last_fault_poll >= self._config.fault_poll_s:
                last_fault_poll = now
                fault: voice_backend.BackendFault | None
                if self._pending_ingress_fault_epoch == epoch:
                    code = self._pending_ingress_fault_code or "ingress_callback_error"
                    self._pending_ingress_fault_epoch = None
                    self._pending_ingress_fault_code = None
                    fault = voice_backend.BackendFault(
                        stream_epoch=epoch,
                        code=code,
                        detail=code,
                        recoverable=True,
                    )
                else:
                    fault = self._backend.poll_fault(stream_epoch=epoch)
                if fault is not None:
                    self._handle_fault(fault)
            if epoch is not None and now - last_route_poll >= self._config.route_poll_s:
                last_route_poll = now
                profile = self._active_profile
                current_uid = self._backend.current_device_uid()
                if current_uid is None:
                    self._device_uid_misses += 1
                    if self._device_uid_misses >= _DEFAULT_DEVICE_MISS_LIMIT:
                        self._device_uid_misses = 0
                        self._handle_fault(
                            voice_backend.BackendFault(
                                stream_epoch=epoch,
                                code="default_device_query_failed",
                                detail="three consecutive default-input queries failed",
                                recoverable=True,
                            ),
                        )
                elif profile is not None and current_uid != profile.device_uid:
                    self._device_uid_misses = 0
                    self._handle_fault(
                        voice_backend.BackendFault(
                            stream_epoch=epoch,
                            code="default_device_changed",
                            detail=f"{profile.device_uid}->{current_uid}",
                            recoverable=True,
                        ),
                    )
                else:
                    self._device_uid_misses = 0
            if not did_work:
                self._worker_stop.wait(timeout=self._config.worker_poll_s)

    def _fan_out(self, frame: CanonicalAudioFrame) -> None:
        self._canonical_frames += 1
        for subscriber in self._subscriber_snapshot:
            before_overflow = subscriber.overflow_count
            subscriber._ring.write(  # noqa: SLF001 - ingress owns subscriber rings
                pcm=frame.pcm16_mono,
                byte_count=len(frame.pcm16_mono),
                stream_epoch=frame.stream_epoch,
                sequence=frame.sequence,
                sample_cursor=frame.sample_cursor,
                sample_rate_hz=frame.sample_rate_hz,
                channels=1,
                frame_count=frame.frame_count,
                adc_time_s=frame.adc_time_s,
                captured_monotonic_ns=frame.captured_monotonic_ns,
                discontinuity_before=frame.discontinuity_before,
                purpose=subscriber.purpose,
                active=subscriber.active_utterance,
            )
            if subscriber.overflow_count > before_overflow:
                record_realtime_trace(
                    "audio_input_subscriber_overflow",
                    stream_epoch=frame.stream_epoch,
                    subscriber=subscriber.name,
                    purpose=subscriber.purpose.value,
                    active_utterance=subscriber.active_utterance,
                    policy=(
                        "fail_restart" if subscriber.active_utterance else "drop_oldest_rebuild"
                    ),
                    measurement_boundary="software_subscriber_ring",
                )

    def _handle_fault(  # noqa: PLR0911 - bounded recovery exits on each terminal state
        self,
        fault: voice_backend.BackendFault,
    ) -> None:
        if self._closing or self._suspended or self._active_epoch != fault.stream_epoch:
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            return
        try:
            if self._active_epoch != fault.stream_epoch:
                return
            self._faults += 1
            self._active_epoch = None
            record_realtime_trace(
                "audio_input_fault",
                stream_epoch=fault.stream_epoch,
                fault_code=fault.code,
                recoverable=fault.recoverable,
                measurement_boundary="software_backend_health",
            )
            close_result = self._backend.stop(stream_epoch=fault.stream_epoch)
            if not close_result.definitively_closed or not fault.recoverable:
                state = (
                    InputCapabilityState.CLOSE_UNCERTAIN
                    if not close_result.definitively_closed
                    else InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
                )
                self._publish_capability(state, reason=fault.code)
                return
            backoff = self._config.reopen_initial_backoff_s
            for attempt_number in range(1, self._config.reopen_attempts + 1):
                if self._worker_stop.wait(timeout=backoff):
                    return
                self._reopen_attempts += 1
                record_realtime_trace(
                    "audio_input_reopen_started",
                    prior_stream_epoch=fault.stream_epoch,
                    attempt=attempt_number,
                    reason=fault.code,
                )
                result = self._open_new_epoch(reason=f"recovery:{fault.code}")
                if result.started:
                    self._reopen_successes += 1
                    self._publish_capability(
                        InputCapabilityState.AVAILABLE,
                        reason="bounded_reopen_succeeded",
                    )
                    record_realtime_trace(
                        "audio_input_reopen_succeeded",
                        prior_stream_epoch=fault.stream_epoch,
                        stream_epoch=result.stream_epoch,
                        attempt=attempt_number,
                    )
                    return
                if result.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN:
                    self._publish_capability(
                        InputCapabilityState.CLOSE_UNCERTAIN,
                        reason="reopen_state_uncertain",
                    )
                    return
                backoff = min(backoff * 2.0, self._config.reopen_max_backoff_s)
            self._publish_capability(
                InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE,
                reason="reopen_budget_exhausted",
            )
            record_realtime_trace(
                "audio_input_reopen_failed",
                prior_stream_epoch=fault.stream_epoch,
                attempts=self._config.reopen_attempts,
                outcome="budget_exhausted",
            )
        finally:
            self._lifecycle_lock.release()

    def stop_for_sleep(self) -> voice_backend.BackendStopResult | None:
        """Close the active epoch before sleep and suppress recovery."""
        self._suspended = True
        if not self._lifecycle_lock.acquire(timeout=self._config.shutdown_timeout_s):
            result = voice_backend.BackendStopResult(
                status=voice_backend.BackendStopStatus.CLOSE_UNCERTAIN,
                stream_epoch=self._active_epoch or self._last_epoch,
                reason="sleep_lifecycle_lock_timeout",
            )
            self._publish_capability(
                InputCapabilityState.CLOSE_UNCERTAIN,
                reason="sleep_lifecycle_lock_timeout",
            )
            return result
        try:
            epoch = self._active_epoch
            self._active_epoch = None
            if epoch is None:
                self._publish_capability(
                    InputCapabilityState.SUSPENDED,
                    reason="sleep_without_active_epoch",
                )
                return None
            result = self._backend.stop(stream_epoch=epoch)
            state = (
                InputCapabilityState.SUSPENDED
                if result.definitively_closed
                else InputCapabilityState.CLOSE_UNCERTAIN
            )
            self._publish_capability(state, reason="system_sleep")
            return result
        finally:
            self._lifecycle_lock.release()

    def resume_after_wake(self) -> voice_backend.BackendStartResult | None:
        """Start a fresh epoch after wake; old cursor/profile state is discarded."""
        if not self._lifecycle_lock.acquire(timeout=self._config.shutdown_timeout_s):
            result = voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=self._last_epoch + 1,
                profile=None,
                reason="wake_lifecycle_lock_timeout",
            )
            self._publish_capability(
                InputCapabilityState.CLOSE_UNCERTAIN,
                reason="wake_lifecycle_lock_timeout",
            )
            return result
        try:
            if self._closing or not self._suspended:
                return None
            self._suspended = False
            result = self._open_new_epoch(reason="system_wake")
            self._publish_capability(
                (
                    InputCapabilityState.AVAILABLE
                    if result.started
                    else InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
                ),
                reason=("wake_reopen_succeeded" if result.started else "wake_reopen_failed"),
            )
            return result
        finally:
            self._lifecycle_lock.release()

    def notify_route_change(self, *, reason: str = "route_change") -> None:
        """Force profile revocation and bounded recovery under a new epoch."""
        epoch = self._active_epoch
        if epoch is None:
            return
        self._handle_fault(
            voice_backend.BackendFault(
                stream_epoch=epoch,
                code=reason,
                detail=reason,
                recoverable=True,
            ),
        )

    def _publish_capability(
        self,
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot:
        wake_available = state is InputCapabilityState.AVAILABLE
        local_capture_available = state in {
            InputCapabilityState.AVAILABLE,
            InputCapabilityState.WAKE_UNAVAILABLE,
        }
        snapshot = InputCapabilitySnapshot(
            state=state,
            stream_epoch=self._active_epoch,
            reason=reason,
            wake_available=wake_available,
            local_capture_available=local_capture_available,
        )
        self._capability = snapshot
        record_realtime_trace(
            "audio_input_capability_changed",
            state=state.value,
            stream_epoch=self._active_epoch,
            reason=reason,
            wake_available=snapshot.wake_available,
            local_capture_available=snapshot.local_capture_available,
            ptt_upload_available=snapshot.ptt_upload_available,
            text_available=snapshot.text_available,
        )
        if self._capability_sink is not None:
            try:
                self._capability_sink(snapshot)
            except Exception:  # noqa: BLE001 - observer cannot break media owner
                LOGGER.warning("audio ingress capability sink failed", exc_info=True)
        return snapshot

    def report_wake_unavailable(self, *, reason: str) -> InputCapabilitySnapshot:
        """Downgrade only wake decisions while the shared input remains healthy."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return self._capability
        try:
            if (
                self._active_epoch is None
                or self._capability.state is not InputCapabilityState.AVAILABLE
            ):
                return self._capability
            return self._publish_capability(
                InputCapabilityState.WAKE_UNAVAILABLE,
                reason=reason,
            )
        finally:
            self._lifecycle_lock.release()

    def report_wake_available(self, *, reason: str) -> InputCapabilitySnapshot:
        """Restore wake capability after a successful model decision."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return self._capability
        try:
            if (
                self._active_epoch is None
                or self._capability.state is not InputCapabilityState.WAKE_UNAVAILABLE
            ):
                return self._capability
            return self._publish_capability(InputCapabilityState.AVAILABLE, reason=reason)
        finally:
            self._lifecycle_lock.release()

    def metrics(self) -> IngressMetrics:
        """Return one raw software/ADC-boundary counter snapshot."""
        subscribers = self._subscriber_snapshot
        return IngressMetrics(
            stream_epoch=self._active_epoch,
            callback_calls=self._callback_calls,
            callback_frames=self._callback_frames,
            first_callback_monotonic_ns=self._first_callback_monotonic_ns,
            input_sample_cursor=self._input_sample_cursor,
            canonical_frames=self._canonical_frames,
            late_epoch_callbacks_rejected=self._late_epoch_callbacks_rejected,
            callback_shape_faults=self._callback_shape_faults,
            native_overflows=self._native_ring.overflow_count,
            subscriber_overflows=sum(sub.overflow_count for sub in subscribers),
            active_discontinuities=sum(sub.active_discontinuity_count for sub in subscribers),
            faults=self._faults,
            reopen_attempts=self._reopen_attempts,
            reopen_successes=self._reopen_successes,
        )

    @property
    def capability(self) -> InputCapabilitySnapshot:
        """Return the latest precise capability snapshot."""
        return self._capability

    @property
    def stream_epoch(self) -> int | None:
        """Return the active epoch, if any."""
        return self._active_epoch

    def clock_mapping(self) -> voice_backend.InputClockMapping | None:
        """Anchor cursor zero for the active epoch to ADC and monotonic clocks."""
        epoch = self._active_epoch
        monotonic_ns = self._first_callback_monotonic_ns
        if epoch is None or monotonic_ns is None:
            return None
        mapping = voice_backend.InputClockMapping(
            stream_epoch=epoch,
            input_sample_cursor=0,
            adc_time_s=self._first_callback_adc_time_s,
            monotonic_ns=monotonic_ns,
            input_clock_domain="portaudio_adc_time_and_process_monotonic",
        )
        return mapping if self._active_epoch == epoch else None

    def close(self) -> IngressCloseResult:
        """Reject callbacks, close the backend, and join every owned worker."""
        if self._closing:
            worker = self._worker
            return IngressCloseResult(
                definitively_closed=worker is None or not worker.is_alive(),
                stream_epoch=self._active_epoch,
                backend_result=None,
                worker_alive=worker is not None and worker.is_alive(),
                open_subscribers=len(self._subscriber_snapshot),
            )
        self._closing = True
        self._worker_stop.set()
        deadline = time.monotonic() + self._config.shutdown_timeout_s
        acquired = self._lifecycle_lock.acquire(timeout=self._config.shutdown_timeout_s)
        if acquired:
            try:
                epoch = self._active_epoch
                self._active_epoch = None
                backend_result = (
                    self._backend.stop(stream_epoch=epoch)
                    if epoch is not None
                    else (
                        self._backend.stop(stream_epoch=self._uncertain_epoch)
                        if self._uncertain_epoch is not None
                        else None
                    )
                )
            finally:
                self._lifecycle_lock.release()
        else:
            epoch = self._active_epoch
            backend_result = voice_backend.BackendStopResult(
                status=voice_backend.BackendStopStatus.CLOSE_UNCERTAIN,
                stream_epoch=epoch or self._last_epoch,
                reason="shutdown_lifecycle_lock_timeout",
            )
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        for subscriber in tuple(self._subscriber_snapshot):
            subscriber.close()
        worker_alive = worker is not None and worker.is_alive()
        definitively_closed = not worker_alive and (
            backend_result is None or backend_result.definitively_closed
        )
        self._publish_capability(
            (
                InputCapabilityState.STOPPED
                if definitively_closed
                else InputCapabilityState.CLOSE_UNCERTAIN
            ),
            reason=("shutdown_complete" if definitively_closed else "shutdown_uncertain"),
        )
        result = IngressCloseResult(
            definitively_closed=definitively_closed,
            stream_epoch=epoch,
            backend_result=backend_result,
            worker_alive=worker_alive,
            open_subscribers=len(self._subscriber_snapshot),
        )
        record_realtime_trace(
            "audio_input_owner_closed",
            stream_epoch=epoch,
            definitively_closed=result.definitively_closed,
            worker_alive=result.worker_alive,
            open_subscribers=result.open_subscribers,
            measurement_boundary="software_resource_ownership",
        )
        return result


def audio_ingress_config_from_mapping(  # noqa: C901 - strict parsing plus cross-field safety checks
    raw: Mapping[str, object] | None,
) -> AudioIngressConfig:
    """Parse strict positive bounds; absent values retain feature-off defaults."""
    values = {} if raw is None else raw
    defaults = AudioIngressConfig()

    def _positive_int(key: str, fallback: int) -> int:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.single_audio_ingress.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    def _positive_float(key: str, fallback: float) -> float:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            msg = f"realtime.single_audio_ingress.{key} must be a positive number"
            raise ValueError(msg)
        return float(value)

    config = AudioIngressConfig(
        canonical_sample_rate_hz=_positive_int(
            "canonical_sample_rate_hz",
            defaults.canonical_sample_rate_hz,
        ),
        canonical_frame_samples=_positive_int(
            "canonical_frame_samples",
            defaults.canonical_frame_samples,
        ),
        native_ring_capacity=_positive_int(
            "native_ring_capacity",
            defaults.native_ring_capacity,
        ),
        default_subscriber_capacity=_positive_int(
            "default_subscriber_capacity",
            defaults.default_subscriber_capacity,
        ),
        max_subscribers=_positive_int("max_subscribers", defaults.max_subscribers),
        worker_poll_s=_positive_float("worker_poll_s", defaults.worker_poll_s),
        fault_poll_s=_positive_float("fault_poll_s", defaults.fault_poll_s),
        route_poll_s=_positive_float("route_poll_s", defaults.route_poll_s),
        reopen_attempts=_positive_int("reopen_attempts", defaults.reopen_attempts),
        reopen_initial_backoff_s=_positive_float(
            "reopen_initial_backoff_s",
            defaults.reopen_initial_backoff_s,
        ),
        reopen_max_backoff_s=_positive_float(
            "reopen_max_backoff_s",
            defaults.reopen_max_backoff_s,
        ),
        backend_open_timeout_s=_positive_float(
            "backend_open_timeout_s",
            defaults.backend_open_timeout_s,
        ),
        backend_close_timeout_s=_positive_float(
            "backend_close_timeout_s",
            defaults.backend_close_timeout_s,
        ),
        shutdown_timeout_s=_positive_float(
            "shutdown_timeout_s",
            defaults.shutdown_timeout_s,
        ),
    )
    if config.canonical_sample_rate_hz != _SAMPLE_RATE:
        msg = "Wave 3 requires canonical_sample_rate_hz=16000 for Silero/SenseVoice"
        raise ValueError(msg)
    if config.canonical_frame_samples != SILERO_CHUNK_SAMPLES:
        msg = "Wave 3 requires canonical_frame_samples=512 for Silero"
        raise ValueError(msg)
    if config.max_subscribers < _REQUIRED_SESSION_SUBSCRIBERS:
        msg = "Wave 3 requires max_subscribers>=3 for wake/capture/diagnostics"
        raise ValueError(msg)
    if config.reopen_initial_backoff_s > config.reopen_max_backoff_s:
        msg = "reopen_initial_backoff_s must not exceed reopen_max_backoff_s"
        raise ValueError(msg)
    return config


__all__ = [
    "SILERO_CHUNK_SAMPLES",
    "AudioIngress",
    "AudioIngressCanonicalizer",
    "AudioIngressConfig",
    "AudioSubscription",
    "CanonicalAudioFrame",
    "IngressCloseResult",
    "IngressMetrics",
    "IngressStartResult",
    "InputCapabilitySnapshot",
    "InputCapabilityState",
    "SileroVad",
    "SubscriberPurpose",
    "VadEvent",
    "VadThresholds",
    "audio_ingress_config_from_mapping",
    "capture_utterance",
]
