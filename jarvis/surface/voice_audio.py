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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface import voice_backend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

SILERO_CHUNK_SAMPLES = 512  # silero fixed-size per inference (32 ms @ 16 kHz)
_LSTM_SHAPE = (2, 1, 64)
_SAMPLE_RATE = 16000
_SILERO_FRAME_MS = SILERO_CHUNK_SAMPLES / _SAMPLE_RATE * 1000.0
_PREWARM_FRAMES = 5
_DEFAULT_DEVICE_MISS_LIMIT = 3
_REQUIRED_SESSION_SUBSCRIBERS = 3
_CAPABILITY_DISPATCH_CAPACITY = 32


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
    def endpoint_silence_frames(self) -> int:
        """Consecutive silence frames that end an ACTIVE utterance (``required_misses``)."""
        return self._t.required_misses

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
                    "vad_endpoint_candidate",
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
    OUTPUT_UNAVAILABLE = "output_unavailable"
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
    route_observer_enabled: bool = False
    barge_detection_mode: str = "ptt"
    accepted_natural_profiles: tuple[voice_backend.DeviceProfileKey, ...] = ()


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
    version: int
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


class _CapabilityDispatcher:
    """One bounded observer lane that never runs user code under owner locks."""

    def __init__(self, sink: _CapabilitySink) -> None:
        self._sink = sink
        self._condition = threading.Condition()
        self._pending: deque[InputCapabilitySnapshot] = deque()
        self._closing = False
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-audio-capability-dispatch",
            daemon=True,
        )
        self._started = False
        try:
            self._thread.start()
            self._started = True
        except RuntimeError:
            LOGGER.exception("audio capability dispatcher failed to start")

    def submit(self, snapshot: InputCapabilitySnapshot) -> None:
        """Enqueue without waiting for the observer; retain newest ordered state."""
        with self._condition:
            if not self._started or self._closing:
                self._dropped += 1
                return
            if len(self._pending) >= _CAPABILITY_DISPATCH_CAPACITY:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(snapshot)
            self._condition.notify()

    def request_close(self) -> None:
        """Drain queued snapshots after any in-flight observer returns."""
        with self._condition:
            self._closing = True
            self._condition.notify()

    @property
    def thread_alive(self) -> bool:
        """Expose bounded-lane liveness for integration shutdown assertions."""
        return self._started and self._thread.is_alive()

    @property
    def dropped(self) -> int:
        """Return snapshots replaced before observer delivery."""
        with self._condition:
            return self._dropped

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if not self._pending:
                    return
                snapshot = self._pending.popleft()
            try:
                self._sink(snapshot)
            except Exception:  # noqa: BLE001 - observer cannot break input owner
                LOGGER.warning("audio ingress capability sink failed", exc_info=True)


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


@dataclass
class _CallbackTimeline:
    """Epoch-local callback cursor isolated until exact open commit."""

    stream_epoch: int
    attempt_id: str
    native_ring: _PreallocatedPcmRing
    sequence: int = 0
    sample_cursor: int = 0
    callback_calls: int = 0
    callback_frames: int = 0
    first_callback_monotonic_ns: int | None = None
    first_callback_adc_time_s: float | None = None
    retired_metrics_recorded: bool = False


class _PreallocatedPcmRing:
    """Bounded SPSC PCM ring with policy-aware overflow markers.

    The producer never waits or grows storage.  Consumers receive a fresh
    bounded ``bytes`` copy, so a later producer wrap cannot mutate a retained
    frame.  CPython's GIL serializes the slot copy and index publication; the
    commit index additionally rejects a slot observed during a wrap.
    """

    def __init__(
        self,
        *,
        capacity: int,
        max_frame_bytes: int,
        serialized_publication: bool = False,
    ) -> None:
        if capacity <= 0 or max_frame_bytes <= 0:
            msg = "PCM ring capacity and max_frame_bytes must be positive"
            raise ValueError(msg)
        self._capacity = capacity
        self._max_frame_bytes = max_frame_bytes
        self._slots = [_PcmSlot(storage=bytearray(max_frame_bytes)) for _ in range(capacity)]
        self._write_index = 0
        self._read_index = 0
        self._drop_before_index = 0
        self._producer_pending_discontinuity = False
        self._consumer_pending_discontinuity = False
        self._publication_token: object | None = None
        self._publication_lock = threading.Lock() if serialized_publication else None
        self._consumer_before_commit_hook: Callable[[int], None] | None = None
        self._closed = False
        self.overflow_count = 0
        self.active_discontinuity_count = 0

    def write(  # noqa: PLR0911, PLR0913 - fixed nonblocking callback state machine
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
        publication_token: object | None = None,
        _publication_lock_held: bool = False,
    ) -> bool:
        """Copy one frame without blocking; return whether it was published."""
        publication_lock = self._publication_lock
        if publication_lock is not None and not _publication_lock_held:
            with publication_lock:
                return self.write(
                    pcm=pcm,
                    byte_count=byte_count,
                    stream_epoch=stream_epoch,
                    sequence=sequence,
                    sample_cursor=sample_cursor,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                    frame_count=frame_count,
                    adc_time_s=adc_time_s,
                    captured_monotonic_ns=captured_monotonic_ns,
                    discontinuity_before=discontinuity_before,
                    purpose=purpose,
                    active=active,
                    publication_token=publication_token,
                    _publication_lock_held=True,
                )
        if (
            publication_token is not None
            and self._publication_token is not publication_token
        ):
            return False
        if self._closed or byte_count <= 0 or byte_count > self._max_frame_bytes:
            self._producer_pending_discontinuity = True
            return False
        effective_read_index = max(self._read_index, self._drop_before_index)
        if self._write_index - effective_read_index >= self._capacity:
            self.overflow_count += 1
            if purpose is SubscriberPurpose.DIAGNOSTIC:
                self._producer_pending_discontinuity = True
                return False
            if purpose is SubscriberPurpose.CAPTURE and active:
                self.active_discontinuity_count += 1
                self._drop_before_index = max(
                    self._drop_before_index,
                    self._write_index,
                )
            else:
                self._drop_before_index = max(
                    self._drop_before_index,
                    self._write_index - self._capacity + 1,
                )
        slot_index = self._write_index % self._capacity
        slot = self._slots[slot_index]
        try:
            source = memoryview(pcm).cast("B")
            if len(source) < byte_count:
                self._producer_pending_discontinuity = True
                return False
            slot.storage[:byte_count] = source[:byte_count]
        except (BufferError, TypeError, ValueError):
            self._producer_pending_discontinuity = True
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
        slot.discontinuity_before = (
            discontinuity_before or self._producer_pending_discontinuity
        )
        if (
            publication_token is not None
            and self._publication_token is not publication_token
        ):
            return False
        self._commit_slot(slot, self._write_index)
        return True

    def _commit_slot(self, slot: _PcmSlot, write_index: int) -> None:
        """Publish one prepared slot; native generations own disjoint rings."""
        slot.committed_index = write_index
        self._producer_pending_discontinuity = False
        self._write_index = write_index + 1

    def read(self) -> _OwnedPcmFrame | None:
        """Return one owned frame or ``None`` when empty/closed."""
        if self._read_index < self._drop_before_index:
            self._read_index = self._drop_before_index
            self._consumer_pending_discontinuity = True
        if self._read_index >= self._write_index:
            return None
        expected_index = self._read_index
        slot = self._slots[expected_index % self._capacity]
        if slot.committed_index != expected_index:
            # Producer dropped/overwrote this position. Re-anchor to the oldest
            # position still representable and make the next frame explicit.
            self._read_index = max(
                self._read_index + 1,
                self._drop_before_index,
                self._write_index - self._capacity,
            )
            self._consumer_pending_discontinuity = True
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
            discontinuity_before=(
                slot.discontinuity_before or self._consumer_pending_discontinuity
            ),
            pcm=payload,
        )
        hook = self._consumer_before_commit_hook
        if hook is not None:
            hook(expected_index)
        if slot.committed_index != expected_index:
            self._consumer_pending_discontinuity = True
            return None
        if expected_index < self._drop_before_index:
            self._read_index = self._drop_before_index
            self._consumer_pending_discontinuity = True
            return None
        self._consumer_pending_discontinuity = False
        self._read_index = expected_index + 1
        return frame

    def set_publication_token(self, token: object | None) -> None:
        """Publish/revoke the exact attempt allowed to commit callback slots."""
        publication_lock = self._publication_lock
        if publication_lock is None:
            self._publication_token = token
            return
        with publication_lock:
            self._publication_token = token

    def close(self) -> None:
        """Reject new writes and discard unread storage."""
        publication_lock = self._publication_lock
        if publication_lock is None:
            self._close_unlocked()
            return
        with publication_lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        self._closed = True
        self._publication_token = None
        self._drop_before_index = self._write_index

    def discard_all(self, *, discontinuity: bool) -> None:
        """Drop unread frames while retaining the fixed storage allocation."""
        publication_lock = self._publication_lock
        if publication_lock is None:
            self._discard_all_unlocked(discontinuity=discontinuity)
            return
        with publication_lock:
            self._discard_all_unlocked(discontinuity=discontinuity)

    def _discard_all_unlocked(self, *, discontinuity: bool) -> None:
        self._drop_before_index = self._write_index
        self._producer_pending_discontinuity = discontinuity


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

    def _close_from_ingress(self) -> None:
        """Close after ingress atomically removed this membership."""
        if self._closed:
            return
        self._closed = True
        self._ring.close()

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
        native_format = backend.input_format()
        self._native_format = native_format
        self._native_ring = _PreallocatedPcmRing(
            capacity=config.native_ring_capacity,
            max_frame_bytes=native_format.bytes_per_callback,
        )
        self._retired_native_overflows = 0
        self._canonicalizer = AudioIngressCanonicalizer(
            sample_rate_hz=config.canonical_sample_rate_hz,
            frame_samples=config.canonical_frame_samples,
        )
        self._subscriber_lock = threading.Lock()
        self._subscribers: dict[str, AudioSubscription] = {}
        self._subscriber_snapshot: tuple[AudioSubscription, ...] = ()
        self._subscribe_before_insert_hook: Callable[[], None] | None = None
        self._wake_report_before_commit_hook: Callable[[], None] | None = None
        self._lifecycle_lock = threading.Lock()
        self._control_lock = threading.RLock()
        self._control_generation = 0
        self._control_intent: Literal["running", "suspended", "closing"] = "running"
        self._control_open_attempt_id: str | None = None
        self._control_open_generation: int | None = None
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._opening_timeline: _CallbackTimeline | None = None
        self._active_timeline: _CallbackTimeline | None = None
        self._active_epoch: int | None = None
        self._last_epoch = 0
        self._attempt_sequence = 0
        self._active_profile: voice_backend.InputDeviceProfile | None = None
        self._closing = False
        self._suspended = False
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
        self._pending_ingress_fault_epoch: int | None = None
        self._pending_ingress_fault_code: str | None = None
        self._deferred_fault: voice_backend.BackendFault | None = None
        self._device_uid_misses = 0
        self._capability_publish_lock = threading.Lock()
        self._capability_lock = threading.RLock()
        self._capability_version = 0
        self._capability = InputCapabilitySnapshot(
            state=InputCapabilityState.STOPPED,
            version=self._capability_version,
            stream_epoch=None,
            reason="not_started",
            wake_available=False,
            local_capture_available=False,
        )
        # Start the observer lane only after every constructor operation that
        # can fail; otherwise a failed ingress construction would leak a
        # waiting dispatcher with no owner able to close it.
        self._capability_dispatcher = (
            _CapabilityDispatcher(capability_sink) if capability_sink is not None else None
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
            serialized_publication=True,
        )
        subscription = AudioSubscription(
            ingress=self,
            name=name,
            purpose=purpose,
            ring=ring,
        )
        with self._subscriber_lock:
            if self._closing:
                ring.close()
                msg = "audio ingress is closing"
                raise RuntimeError(msg)
            if name in self._subscribers:
                ring.close()
                msg = f"audio subscriber already exists: {name}"
                raise ValueError(msg)
            if len(self._subscribers) >= self._config.max_subscribers:
                ring.close()
                msg = "audio subscriber bound reached"
                raise RuntimeError(msg)
            ring.set_publication_token(self._active_timeline)
            hook = self._subscribe_before_insert_hook
            if hook is not None:
                hook()
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

    def _control_allows_running(self, expected_generation: int) -> bool:
        """Check one exact recovery/open generation while control lock is held."""
        return (
            self._control_generation == expected_generation
            and self._control_intent == "running"
            and not self._closing
            and not self._suspended
            and not self._worker_stop.is_set()
        )

    def _control_allows_suspended(self, expected_generation: int) -> bool:
        """Check one exact sleep generation while control lock is held."""
        return (
            self._control_generation == expected_generation
            and self._control_intent == "suspended"
            and not self._closing
            and self._suspended
        )

    def start(self) -> IngressStartResult:
        """Start the canonicalizer worker and exactly one backend epoch."""
        with self._lifecycle_lock:
            if self._worker is not None:
                msg = "audio ingress already started"
                raise RuntimeError(msg)
            self._worker_stop.clear()
            with self._control_lock:
                self._control_generation += 1
                control_generation = self._control_generation
                self._control_intent = "running"
                self._suspended = False
            with self._subscriber_lock:
                self._closing = False
            self._worker = threading.Thread(
                target=self._run_worker,
                name="jarvis-audio-ingress",
                daemon=False,
            )
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                raise
            backend_result = self._open_new_epoch(
                reason="startup",
                expected_control_generation=control_generation,
            )
            if backend_result.started:
                snapshot = self._publish_capability_if_current(
                    control_generation,
                    InputCapabilityState.AVAILABLE,
                    reason="input_stream_started",
                )
                if snapshot is None:
                    snapshot = self.capability
                return IngressStartResult(
                    started=True,
                    capability=snapshot,
                    backend_result=backend_result,
                )
            self._worker_stop.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=self._config.shutdown_timeout_s)
        state = (
            InputCapabilityState.CLOSE_UNCERTAIN
            if backend_result.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
            else InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
        )
        snapshot = self._publish_capability_if_current(
            control_generation,
            state,
            reason=backend_result.reason or backend_result.status.value,
        )
        if snapshot is None:
            snapshot = self.capability
        return IngressStartResult(
            started=False,
            capability=snapshot,
            backend_result=backend_result,
        )

    def _open_new_epoch(  # noqa: C901, PLR0911, PLR0912, PLR0915 - exact generation/open/commit outcomes
        self,
        *,
        reason: str,
        deadline: float | None = None,
        expected_control_generation: int | None = None,
    ) -> voice_backend.BackendStartResult:
        with self._control_lock:
            control_generation = (
                self._control_generation
                if expected_control_generation is None
                else expected_control_generation
            )
            if not self._control_allows_running(control_generation):
                return voice_backend.BackendStartResult(
                    status=voice_backend.BackendStartStatus.FAILED_CLOSED,
                    stream_epoch=self._last_epoch,
                    profile=None,
                    reason="control_generation_revoked_before_open",
                )
        ownership = self._backend.ownership_snapshot()
        if ownership.state is not voice_backend.BackendLifecycleState.CLOSED:
            return voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=ownership.stream_epoch or self._last_epoch,
                profile=None,
                reason=ownership.reason or f"ownership_debt_{ownership.state.value}",
                attempt_id=ownership.attempt_id,
            )
        self._last_epoch += 1
        epoch = self._last_epoch
        self._attempt_sequence += 1
        attempt_id = f"ingress-{id(self):x}-a{self._attempt_sequence}"
        native_ring = _PreallocatedPcmRing(
            capacity=self._config.native_ring_capacity,
            max_frame_bytes=self._native_format.bytes_per_callback,
        )
        timeline = _CallbackTimeline(
            stream_epoch=epoch,
            attempt_id=attempt_id,
            native_ring=native_ring,
        )
        self._opening_timeline = timeline
        self._active_timeline = None
        self._native_ring.set_publication_token(None)
        self._active_epoch = None
        self._active_profile = None
        self._first_callback_monotonic_ns = None
        self._first_callback_adc_time_s = None
        self._reported_first_callback_epoch = None
        self._pending_ingress_fault_epoch = None
        self._pending_ingress_fault_code = None
        self._deferred_fault = None
        self._device_uid_misses = 0
        # Opening callbacks remain uncommitted and are dropped. Only the exact
        # successful attempt is published after backend.start() returns.
        self._canonicalizer.reset(stream_epoch=epoch, discontinuity=False)
        with self._subscriber_lock:
            for subscriber in self._subscriber_snapshot:
                subscriber._ring.discard_all(discontinuity=False)  # noqa: SLF001
        timeout_s = None if deadline is None else max(0.0, deadline - time.monotonic())
        with self._control_lock:
            if not self._control_allows_running(control_generation):
                self._opening_timeline = None
                return voice_backend.BackendStartResult(
                    status=voice_backend.BackendStartStatus.FAILED_CLOSED,
                    stream_epoch=epoch,
                    profile=None,
                    reason="control_generation_revoked_at_physical_open",
                    attempt_id=attempt_id,
                )
            self._control_open_attempt_id = attempt_id
            self._control_open_generation = control_generation
        try:
            try:
                result = self._backend.start(
                    stream_epoch=epoch,
                    attempt_id=attempt_id,
                    frame_sink=self._on_backend_frame,
                    render_source=None,
                    timeout_s=timeout_s,
                )
            except TypeError as exc:
                if "timeout_s" not in str(exc):
                    raise
                result = self._backend.start(
                    stream_epoch=epoch,
                    attempt_id=attempt_id,
                    frame_sink=self._on_backend_frame,
                    render_source=None,
                )
        finally:
            with self._control_lock:
                exact_registered_open = (
                    self._control_open_attempt_id == attempt_id
                    and self._control_open_generation == control_generation
                )
                control_current = exact_registered_open and self._control_allows_running(
                    control_generation,
                )
                if exact_registered_open:
                    self._control_open_attempt_id = None
                    self._control_open_generation = None
        if not result.started:
            self._opening_timeline = None
            return result
        if not control_current or self._worker_stop.is_set():
            self._opening_timeline = None
            stop_result = self._stop_backend_debt(deadline=deadline)
            if stop_result is None:
                stop_result = voice_backend.BackendStopResult(
                    status=voice_backend.BackendStopStatus.CLOSED,
                    stream_epoch=epoch,
                    reason="revoked_open_already_closed",
                    attempt_id=attempt_id,
                )
            return voice_backend.BackendStartResult(
                status=(
                    voice_backend.BackendStartStatus.FAILED_CLOSED
                    if stop_result.definitively_closed
                    else voice_backend.BackendStartStatus.OPEN_UNCERTAIN
                ),
                stream_epoch=epoch,
                profile=None,
                reason="owner_revoked_during_open",
                attempt_id=attempt_id,
            )
        if result.attempt_id != attempt_id or self._opening_timeline is not timeline:
            self._opening_timeline = None
            self._stop_backend_debt(deadline=deadline)
            return voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=epoch,
                profile=None,
                reason="open_attempt_identity_mismatch",
                attempt_id=attempt_id,
            )
        with self._control_lock:
            commit_allowed = self._control_allows_running(control_generation)
            if commit_allowed:
                self._opening_timeline = None
                self._native_ring = native_ring
                self._active_timeline = timeline
                self._active_epoch = epoch
                native_ring.set_publication_token(timeline)
                with self._subscriber_lock:
                    for subscriber in self._subscriber_snapshot:
                        subscriber._ring.set_publication_token(timeline)  # noqa: SLF001
                self._active_profile = result.profile
                record_realtime_trace(
                    "audio_input_epoch_opened",
                    stream_epoch=epoch,
                    reason=reason,
                    device_uid=(
                        result.profile.device_uid if result.profile is not None else None
                    ),
                    input_sample_cursor=0,
                    measurement_boundary="software_epoch_not_first_callback",
                )
        if not commit_allowed:
            self._opening_timeline = None
            # Never hold the local control lock across a foreign close. Sleep
            # and shutdown must be able to advance terminal intent even if the
            # backend violates its own close bound.
            stop_result = self._stop_backend_debt(deadline=deadline)
            definitively_closed = stop_result is None or stop_result.definitively_closed
            return voice_backend.BackendStartResult(
                status=(
                    voice_backend.BackendStartStatus.FAILED_CLOSED
                    if definitively_closed
                    else voice_backend.BackendStartStatus.OPEN_UNCERTAIN
                ),
                stream_epoch=epoch,
                profile=None,
                reason="control_generation_revoked_before_epoch_commit",
                attempt_id=attempt_id,
            )
        return result

    def _on_backend_frame(  # noqa: PLR0913 - fixed backend sink contract
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        callback_buffer: Any,  # noqa: ANN401
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
    ) -> None:
        """ADC callback sink: validate epoch and copy into one fixed ring."""
        timeline = self._active_timeline
        if (
            self._closing
            or self._suspended
            or timeline is None
            or timeline.stream_epoch != stream_epoch
            or timeline.attempt_id != attempt_id
        ):
            self._late_epoch_callbacks_rejected += 1
            return
        native_format = self._native_format
        if frame_count != native_format.callback_frame_samples:
            self._callback_shape_faults += 1
            self._pending_ingress_fault_epoch = stream_epoch
            self._pending_ingress_fault_code = "unexpected_callback_frame_count"
            return
        sequence = timeline.sequence
        sample_cursor = timeline.sample_cursor
        timeline.sequence += 1
        timeline.sample_cursor += frame_count
        timeline.callback_calls += 1
        timeline.callback_frames += frame_count
        self._callback_calls += 1
        self._callback_frames += frame_count
        if timeline.first_callback_monotonic_ns is None:
            timeline.first_callback_adc_time_s = adc_time_s
            timeline.first_callback_monotonic_ns = captured_monotonic_ns
        native_ring = timeline.native_ring
        overflow_count = native_ring.overflow_count
        published = native_ring.write(
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
            publication_token=timeline,
        )
        if self._active_timeline is not timeline:
            self._late_epoch_callbacks_rejected += 1
            return
        if not published and native_ring.overflow_count == overflow_count:
            self._pending_ingress_fault_epoch = stream_epoch
            self._pending_ingress_fault_code = "callback_buffer_copy_failed"

    def _run_worker(self) -> None:  # noqa: C901, PLR0912, PLR0915 - one owner loop serializes frame/fault/route ordering
        """Canonicalize/fan out frames and own bounded fault recovery."""
        last_fault_poll = 0.0
        last_route_poll = 0.0
        while not self._worker_stop.is_set():
            did_work = False
            worker_timeline = self._active_timeline
            native_ring = (
                worker_timeline.native_ring if worker_timeline is not None else None
            )
            native = native_ring.read() if native_ring is not None else None
            while native is not None:
                if native_ring is None:
                    break
                did_work = True
                active_timeline = self._active_timeline
                if (
                    active_timeline is not None
                    and native.stream_epoch == active_timeline.stream_epoch
                ):
                    for frame in self._canonicalizer.feed(native):
                        self._fan_out(frame)
                native = native_ring.read()
            timeline = self._active_timeline
            epoch = timeline.stream_epoch if timeline is not None else None
            now = time.monotonic()
            if (
                timeline is not None
                and self._reported_first_callback_epoch != epoch
                and timeline.first_callback_monotonic_ns is not None
            ):
                self._reported_first_callback_epoch = epoch
                record_realtime_trace(
                    "audio_input_first_callback",
                    stream_epoch=epoch,
                    attempt_id=timeline.attempt_id,
                    callback_monotonic_ns=timeline.first_callback_monotonic_ns,
                    adc_time_s=timeline.first_callback_adc_time_s,
                    input_sample_cursor=0,
                    input_clock_domain="portaudio_adc_time_and_process_monotonic",
                    measurement_boundary="adc_callback_arrival_not_acoustic_truth",
                )
            if timeline is not None and now - last_fault_poll >= self._config.fault_poll_s:
                last_fault_poll = now
                active_epoch = timeline.stream_epoch
                fault: voice_backend.BackendFault | None
                if (
                    self._deferred_fault is not None
                    and self._deferred_fault.stream_epoch == active_epoch
                ):
                    fault = self._deferred_fault
                    self._deferred_fault = None
                elif self._pending_ingress_fault_epoch == active_epoch:
                    code = self._pending_ingress_fault_code or "ingress_callback_error"
                    self._pending_ingress_fault_epoch = None
                    self._pending_ingress_fault_code = None
                    fault = voice_backend.BackendFault(
                        stream_epoch=active_epoch,
                        code=code,
                        detail=code,
                        recoverable=True,
                        attempt_id=timeline.attempt_id,
                    )
                else:
                    fault = self._backend.poll_fault(stream_epoch=active_epoch)
                if fault is not None:
                    self._handle_fault(fault)
            if timeline is not None and now - last_route_poll >= self._config.route_poll_s:
                last_route_poll = now
                active_epoch = timeline.stream_epoch
                profile = self._active_profile
                current_uid = self._backend.current_device_uid()
                if current_uid is None:
                    self._device_uid_misses += 1
                    if self._device_uid_misses >= _DEFAULT_DEVICE_MISS_LIMIT:
                        self._device_uid_misses = 0
                        self._handle_fault(
                            voice_backend.BackendFault(
                                stream_epoch=active_epoch,
                                code="default_device_query_failed",
                                detail="three consecutive default-input queries failed",
                                recoverable=True,
                                attempt_id=timeline.attempt_id,
                            ),
                        )
                elif profile is not None and current_uid != profile.device_uid:
                    self._device_uid_misses = 0
                    self._handle_fault(
                        voice_backend.BackendFault(
                            stream_epoch=active_epoch,
                            code="default_device_changed",
                            detail=f"{profile.device_uid}->{current_uid}",
                            recoverable=True,
                            attempt_id=timeline.attempt_id,
                        ),
                    )
                else:
                    self._device_uid_misses = 0
            if not did_work:
                self._worker_stop.wait(timeout=self._config.worker_poll_s)

    def _fan_out(self, frame: CanonicalAudioFrame) -> None:
        timeline = self._active_timeline
        if timeline is None or timeline.stream_epoch != frame.stream_epoch:
            return
        self._canonical_frames += 1
        for subscriber in self._subscriber_snapshot:
            before_overflow = subscriber.overflow_count
            published = subscriber._ring.write(  # noqa: SLF001 - ingress owns subscriber rings
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
                publication_token=timeline,
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
            elif not published:
                record_realtime_trace(
                    "audio_input_subscriber_write_rejected",
                    stream_epoch=frame.stream_epoch,
                    subscriber=subscriber.name,
                    purpose=subscriber.purpose.value,
                    measurement_boundary="software_subscriber_ring",
                )

    def _revoke_publication(self) -> _CallbackTimeline | None:
        """Atomically revoke the callback generation before lifecycle work."""
        timeline = self._active_timeline
        self._active_timeline = None
        self._opening_timeline = None
        self._active_epoch = None
        if timeline is not None:
            timeline.native_ring.set_publication_token(None)
            if not timeline.retired_metrics_recorded:
                self._retired_native_overflows += timeline.native_ring.overflow_count
                timeline.retired_metrics_recorded = True
        with self._subscriber_lock:
            for subscriber in self._subscriber_snapshot:
                subscriber._ring.set_publication_token(None)  # noqa: SLF001
        return timeline

    def _stop_backend_debt(
        self,
        *,
        deadline: float | None = None,
    ) -> voice_backend.BackendStopResult | None:
        """Close the exact physical ledger entry, if ownership remains possible."""
        for _ in range(2):
            ownership = self._backend.ownership_snapshot()
            if ownership.state is voice_backend.BackendLifecycleState.CLOSED:
                return None
            if ownership.stream_epoch is None or ownership.attempt_id is None:
                return voice_backend.BackendStopResult(
                    status=voice_backend.BackendStopStatus.CLOSE_UNCERTAIN,
                    stream_epoch=ownership.stream_epoch or self._last_epoch,
                    reason="backend_ownership_identity_missing",
                    helper_thread_alive=ownership.helper_thread_alive,
                    attempt_id=ownership.attempt_id,
                )
            timeout_s = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            try:
                result = self._backend.stop(
                    stream_epoch=ownership.stream_epoch,
                    attempt_id=ownership.attempt_id,
                    timeout_s=timeout_s,
                )
            except TypeError as exc:
                if "timeout_s" not in str(exc):
                    raise
                result = self._backend.stop(
                    stream_epoch=ownership.stream_epoch,
                    attempt_id=ownership.attempt_id,
                )
            if result.status is not voice_backend.BackendStopStatus.STALE_ATTEMPT:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                return result
        return result

    def _handle_fault(  # noqa: C901, PLR0911, PLR0912 - bounded recovery exits
        self,
        fault: voice_backend.BackendFault,
    ) -> None:
        timeline = self._active_timeline
        if (
            self._closing
            or self._suspended
            or timeline is None
            or timeline.stream_epoch != fault.stream_epoch
            or (fault.attempt_id is not None and fault.attempt_id != timeline.attempt_id)
        ):
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            self._deferred_fault = fault
            return
        try:
            with self._control_lock:
                control_generation = self._control_generation
                if not self._control_allows_running(control_generation):
                    return
            timeline = self._active_timeline
            if timeline is None or timeline.stream_epoch != fault.stream_epoch:
                return
            self._faults += 1
            self._revoke_publication()
            record_realtime_trace(
                "audio_input_fault",
                stream_epoch=fault.stream_epoch,
                attempt_id=timeline.attempt_id,
                fault_code=fault.code,
                recoverable=fault.recoverable,
                measurement_boundary="software_backend_health",
            )
            self._stop_backend_debt()
            with self._control_lock:
                if not self._control_allows_running(control_generation):
                    return
            ownership = self._backend.ownership_snapshot()
            definitively_closed = (
                ownership.state is voice_backend.BackendLifecycleState.CLOSED
            )
            if not definitively_closed or not fault.recoverable:
                state = (
                    InputCapabilityState.CLOSE_UNCERTAIN
                    if not definitively_closed
                    else InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
                )
                self._publish_capability_if_current(
                    control_generation,
                    state,
                    reason=fault.code,
                )
                return
            backoff = self._config.reopen_initial_backoff_s
            for attempt_number in range(1, self._config.reopen_attempts + 1):
                if self._worker_stop.wait(timeout=backoff):
                    return
                with self._control_lock:
                    if not self._control_allows_running(control_generation):
                        return
                self._reopen_attempts += 1
                record_realtime_trace(
                    "audio_input_reopen_started",
                    prior_stream_epoch=fault.stream_epoch,
                    attempt=attempt_number,
                    reason=fault.code,
                )
                result = self._open_new_epoch(
                    reason=f"recovery:{fault.code}",
                    expected_control_generation=control_generation,
                )
                with self._control_lock:
                    control_current = self._control_allows_running(control_generation)
                if not control_current:
                    self._revoke_publication()
                    self._stop_backend_debt()
                    return
                if result.started:
                    self._reopen_successes += 1
                    self._publish_capability_if_current(
                        control_generation,
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
                    self._publish_capability_if_current(
                        control_generation,
                        InputCapabilityState.CLOSE_UNCERTAIN,
                        reason="reopen_state_uncertain",
                    )
                    return
                backoff = min(backoff * 2.0, self._config.reopen_max_backoff_s)
            with self._control_lock:
                if not self._control_allows_running(control_generation):
                    return
            self._publish_capability_if_current(
                control_generation,
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

    def stop_for_sleep(
        self,
        *,
        deadline: float | None = None,
    ) -> voice_backend.BackendStopResult | None:
        """Close the active epoch before sleep and suppress recovery."""
        transition_deadline = (
            time.monotonic() + self._config.shutdown_timeout_s
            if deadline is None
            else deadline
        )
        # This lock protects only local scalars; foreign backend.start never
        # holds it.  Sleep therefore always publishes a newer terminal
        # generation before it reports SUSPENDED.
        with self._control_lock:
            if self._control_intent == "closing":
                return None
            self._control_generation += 1
            control_generation = self._control_generation
            self._control_intent = "suspended"
            self._suspended = True
        self._publish_capability_for_intent(
            control_generation,
            "suspended",
            InputCapabilityState.SUSPENDED,
            reason="system_sleep_requested",
        )
        self._revoke_publication()
        if not self._lifecycle_lock.acquire(
            timeout=max(0.0, transition_deadline - time.monotonic()),
        ):
            # The backend ledger is independently serialized. Even when an
            # ingress recovery holds this lock, revoke publication and join or
            # cancel the exact physical debt instead of merely reporting it.
            result = self._stop_backend_debt(deadline=transition_deadline)
            if result is None:
                result = voice_backend.BackendStopResult(
                    status=voice_backend.BackendStopStatus.CLOSED,
                    stream_epoch=self._last_epoch,
                    reason="sleep_lifecycle_lock_timeout_but_backend_closed",
                )
            return result
        try:
            result = self._stop_backend_debt(deadline=transition_deadline)
            if result is None:
                return None
            return result
        finally:
            self._lifecycle_lock.release()

    def resume_after_wake(  # noqa: PLR0911 - exact bounded wake outcomes
        self,
        *,
        deadline: float | None = None,
    ) -> voice_backend.BackendStartResult | None:
        """Start a fresh epoch after wake; old cursor/profile state is discarded."""
        transition_deadline = (
            time.monotonic() + self._config.shutdown_timeout_s
            if deadline is None
            else deadline
        )
        with self._control_lock:
            if (
                self._control_intent != "suspended"
                or self._closing
                or not self._suspended
            ):
                return None
            suspended_generation = self._control_generation
        if not self._lifecycle_lock.acquire(
            timeout=max(0.0, transition_deadline - time.monotonic()),
        ):
            result = voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=self._last_epoch + 1,
                profile=None,
                reason="wake_lifecycle_lock_timeout",
            )
            self._publish_capability_for_intent(
                suspended_generation,
                "suspended",
                InputCapabilityState.CLOSE_UNCERTAIN,
                reason="wake_lifecycle_lock_timeout",
            )
            return result
        try:
            with self._control_lock:
                if not self._control_allows_suspended(suspended_generation):
                    return None
            close_result = self._stop_backend_debt(deadline=transition_deadline)
            ownership = self._backend.ownership_snapshot()
            if ownership.state is not voice_backend.BackendLifecycleState.CLOSED:
                result = voice_backend.BackendStartResult(
                    status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
                    stream_epoch=ownership.stream_epoch or self._last_epoch,
                    profile=None,
                    reason=(
                        close_result.reason
                        if close_result is not None
                        else ownership.reason or "wake_prior_close_debt"
                    ),
                    attempt_id=ownership.attempt_id,
                )
                self._publish_capability_for_intent(
                    suspended_generation,
                    "suspended",
                    InputCapabilityState.CLOSE_UNCERTAIN,
                    reason="wake_blocked_by_prior_close_debt",
                )
                return result
            with self._control_lock:
                if not self._control_allows_suspended(suspended_generation):
                    return None
                self._control_generation += 1
                control_generation = self._control_generation
                self._control_intent = "running"
                self._suspended = False
            result = self._open_new_epoch(
                reason="system_wake",
                deadline=transition_deadline,
                expected_control_generation=control_generation,
            )
            if result.started:
                state = InputCapabilityState.AVAILABLE
            elif (
                result.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
                or self._backend.ownership_snapshot().state
                is not voice_backend.BackendLifecycleState.CLOSED
            ):
                state = InputCapabilityState.CLOSE_UNCERTAIN
            else:
                state = InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
            snapshot = self._publish_capability_if_current(
                control_generation,
                state,
                reason=("wake_reopen_succeeded" if result.started else "wake_reopen_failed"),
            )
            if snapshot is None and self._suspended:
                return result
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

    def _commit_capability_locked(
        self,
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot:
        """Commit one version while the caller owns ``_capability_lock``."""
        wake_available = state is InputCapabilityState.AVAILABLE
        local_capture_available = state in {
            InputCapabilityState.AVAILABLE,
            InputCapabilityState.WAKE_UNAVAILABLE,
        }
        ownership = self._backend.ownership_snapshot()
        capability_epoch = self._active_epoch or ownership.stream_epoch
        self._capability_version += 1
        snapshot = InputCapabilitySnapshot(
            state=state,
            version=self._capability_version,
            stream_epoch=capability_epoch,
            reason=reason,
            wake_available=wake_available,
            local_capture_available=local_capture_available,
        )
        self._capability = snapshot
        return snapshot

    def _trace_capability(self, snapshot: InputCapabilitySnapshot) -> None:
        """Trace committed state after all owner locks are released."""
        record_realtime_trace(
            "audio_input_capability_changed",
            version=snapshot.version,
            state=snapshot.state.value,
            stream_epoch=snapshot.stream_epoch,
            reason=snapshot.reason,
            wake_available=snapshot.wake_available,
            local_capture_available=snapshot.local_capture_available,
            ptt_upload_available=snapshot.ptt_upload_available,
            text_available=snapshot.text_available,
        )

    def _publish_capability(
        self,
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot:
        """Commit in version order and enqueue without invoking observer code."""
        with self._capability_publish_lock:
            with self._capability_lock:
                snapshot = self._commit_capability_locked(state, reason=reason)
            dispatcher = self._capability_dispatcher
            if dispatcher is not None:
                dispatcher.submit(snapshot)
        self._trace_capability(snapshot)
        return snapshot

    def _publish_capability_if_current(
        self,
        expected_control_generation: int,
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot | None:
        """Publish only if the exact running generation still owns control."""
        with self._capability_publish_lock:
            with self._control_lock, self._capability_lock:
                if not self._control_allows_running(expected_control_generation):
                    return None
                snapshot = self._commit_capability_locked(state, reason=reason)
            dispatcher = self._capability_dispatcher
            if dispatcher is not None:
                dispatcher.submit(snapshot)
        self._trace_capability(snapshot)
        return snapshot

    def _publish_capability_for_intent(
        self,
        expected_control_generation: int,
        expected_intent: Literal["suspended", "closing"],
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot | None:
        """Publish a terminal snapshot only for its exact control generation."""
        with self._capability_publish_lock:
            with self._control_lock, self._capability_lock:
                if (
                    self._control_generation != expected_control_generation
                    or self._control_intent != expected_intent
                ):
                    return None
                snapshot = self._commit_capability_locked(state, reason=reason)
            dispatcher = self._capability_dispatcher
            if dispatcher is not None:
                dispatcher.submit(snapshot)
        self._trace_capability(snapshot)
        return snapshot

    def _publish_wake_capability_if_current(
        self,
        expected_control_generation: int,
        expected_state: InputCapabilityState,
        state: InputCapabilityState,
        *,
        reason: str,
    ) -> InputCapabilitySnapshot | None:
        """CAS a wake-only transition against exact running state."""
        with self._capability_publish_lock:
            with self._control_lock, self._capability_lock:
                if (
                    not self._control_allows_running(expected_control_generation)
                    or self._active_epoch is None
                    or self._capability.state is not expected_state
                ):
                    return None
                snapshot = self._commit_capability_locked(state, reason=reason)
            dispatcher = self._capability_dispatcher
            if dispatcher is not None:
                dispatcher.submit(snapshot)
        self._trace_capability(snapshot)
        return snapshot

    def report_wake_unavailable(self, *, reason: str) -> InputCapabilitySnapshot:
        """Downgrade only wake decisions while the shared input remains healthy."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return self.capability
        try:
            with self._control_lock, self._capability_lock:
                if (
                    self._active_epoch is None
                    or not self._control_allows_running(self._control_generation)
                    or self._capability.state is not InputCapabilityState.AVAILABLE
                ):
                    return self._capability
                control_generation = self._control_generation
            hook = self._wake_report_before_commit_hook
            if hook is not None:
                hook()
            snapshot = self._publish_wake_capability_if_current(
                control_generation,
                InputCapabilityState.AVAILABLE,
                InputCapabilityState.WAKE_UNAVAILABLE,
                reason=reason,
            )
            return self.capability if snapshot is None else snapshot
        finally:
            self._lifecycle_lock.release()

    def report_wake_available(self, *, reason: str) -> InputCapabilitySnapshot:
        """Restore wake capability after a successful model decision."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return self.capability
        try:
            with self._control_lock, self._capability_lock:
                if (
                    self._active_epoch is None
                    or not self._control_allows_running(self._control_generation)
                    or self._capability.state
                    is not InputCapabilityState.WAKE_UNAVAILABLE
                ):
                    return self._capability
                control_generation = self._control_generation
            hook = self._wake_report_before_commit_hook
            if hook is not None:
                hook()
            snapshot = self._publish_wake_capability_if_current(
                control_generation,
                InputCapabilityState.WAKE_UNAVAILABLE,
                InputCapabilityState.AVAILABLE,
                reason=reason,
            )
            return self.capability if snapshot is None else snapshot
        finally:
            self._lifecycle_lock.release()

    def report_output_unavailable(self, *, reason: str) -> InputCapabilitySnapshot:
        """Keep input suspended when fresh output ownership is not proven."""
        with self._control_lock:
            if self._control_intent == "closing":
                return self.capability
            self._suspended = True
            self._control_generation += 1
            control_generation = self._control_generation
            self._control_intent = "suspended"
        snapshot = self._publish_capability_for_intent(
            control_generation,
            "suspended",
            InputCapabilityState.OUTPUT_UNAVAILABLE,
            reason=reason,
        )
        return self.capability if snapshot is None else snapshot

    def metrics(self) -> IngressMetrics:
        """Return one raw software/ADC-boundary counter snapshot."""
        subscribers = self._subscriber_snapshot
        timeline = self._active_timeline
        active_native_overflows = (
            timeline.native_ring.overflow_count if timeline is not None else 0
        )
        return IngressMetrics(
            stream_epoch=timeline.stream_epoch if timeline is not None else None,
            callback_calls=self._callback_calls,
            callback_frames=self._callback_frames,
            first_callback_monotonic_ns=(
                timeline.first_callback_monotonic_ns if timeline is not None else None
            ),
            input_sample_cursor=timeline.sample_cursor if timeline is not None else 0,
            canonical_frames=self._canonical_frames,
            late_epoch_callbacks_rejected=self._late_epoch_callbacks_rejected,
            callback_shape_faults=self._callback_shape_faults,
            native_overflows=self._retired_native_overflows + active_native_overflows,
            subscriber_overflows=sum(sub.overflow_count for sub in subscribers),
            active_discontinuities=sum(sub.active_discontinuity_count for sub in subscribers),
            faults=self._faults,
            reopen_attempts=self._reopen_attempts,
            reopen_successes=self._reopen_successes,
        )

    @property
    def capability(self) -> InputCapabilitySnapshot:
        """Return the latest precise capability snapshot."""
        with self._capability_lock:
            return self._capability

    @property
    def stream_epoch(self) -> int | None:
        """Return the active epoch, if any."""
        return self._active_epoch

    def clock_mapping(self) -> voice_backend.InputClockMapping | None:
        """Anchor cursor zero for the active epoch to ADC and monotonic clocks."""
        timeline = self._active_timeline
        if timeline is None or timeline.first_callback_monotonic_ns is None:
            return None
        mapping = voice_backend.InputClockMapping(
            stream_epoch=timeline.stream_epoch,
            input_sample_cursor=0,
            adc_time_s=timeline.first_callback_adc_time_s,
            monotonic_ns=timeline.first_callback_monotonic_ns,
            input_clock_domain="portaudio_adc_time_and_process_monotonic",
        )
        return mapping if self._active_timeline is timeline else None

    def close(self) -> IngressCloseResult:
        """Reject callbacks, close the backend, and join every owned worker."""
        deadline = time.monotonic() + self._config.shutdown_timeout_s
        with self._control_lock:
            self._closing = True
            self._control_generation += 1
            control_generation = self._control_generation
            self._control_intent = "closing"
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers.values())
            self._subscribers.clear()
            self._subscriber_snapshot = ()
            self._capture_active = False
        revoked = self._revoke_publication()
        for subscriber in subscribers:
            subscriber._close_from_ingress()  # noqa: SLF001 - paired owner method
        self._worker_stop.set()
        acquired = self._lifecycle_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if acquired:
            try:
                backend_result = self._stop_backend_debt(deadline=deadline)
            finally:
                self._lifecycle_lock.release()
        else:
            backend_result = self._stop_backend_debt(deadline=deadline)
            if backend_result is None:
                backend_result = voice_backend.BackendStopResult(
                    status=voice_backend.BackendStopStatus.CLOSED,
                    stream_epoch=self._last_epoch,
                    reason="shutdown_lifecycle_lock_timeout_but_backend_closed",
                )
        epoch = revoked.stream_epoch if revoked is not None else self._last_epoch or None
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        worker_alive = worker is not None and worker.is_alive()
        ownership = self._backend.ownership_snapshot()
        with self._control_lock:
            pending_open_attempt = self._control_open_attempt_id is not None
        definitively_closed = (
            not worker_alive
            and not pending_open_attempt
            and ownership.state is voice_backend.BackendLifecycleState.CLOSED
        )
        self._publish_capability_for_intent(
            control_generation,
            "closing",
            (
                InputCapabilityState.STOPPED
                if definitively_closed
                else InputCapabilityState.CLOSE_UNCERTAIN
            ),
            reason=("shutdown_complete" if definitively_closed else "shutdown_uncertain"),
        )
        dispatcher = self._capability_dispatcher
        if dispatcher is not None and definitively_closed:
            # Observer execution is deliberately outside the shutdown proof.
            # A stuck sink may delay its own terminal notification, never the
            # input owner's absolute close deadline.
            dispatcher.request_close()
        result = IngressCloseResult(
            definitively_closed=definitively_closed,
            stream_epoch=epoch,
            backend_result=backend_result,
            worker_alive=worker_alive,
            open_subscribers=0,
        )
        record_realtime_trace(
            "audio_input_owner_closed",
            stream_epoch=epoch,
            definitively_closed=result.definitively_closed,
            worker_alive=result.worker_alive,
            open_subscribers=result.open_subscribers,
            measurement_boundary="software_resource_ownership",
            pending_open_attempt=pending_open_attempt,
            capability_observer_drops=(dispatcher.dropped if dispatcher is not None else 0),
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
    return replace(
        config,
        route_observer_enabled=_route_observer_enabled(values.get("route_observer")),
        **_barge_in_config(values.get("barge_in")),
    )


def _route_observer_enabled(raw: object) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, Mapping):
        msg = "realtime.single_audio_ingress.route_observer must be a mapping"
        raise ValueError(msg)  # noqa: TRY004 - config validation contract
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        msg = "realtime.single_audio_ingress.route_observer.enabled must be a boolean"
        raise ValueError(msg)  # noqa: TRY004 - config validation contract
    return enabled


_PROFILE_KEY_FIELDS = (
    "input_uid",
    "output_uid",
    "backend",
    "route_kind",
    "aec_mode",
    "input_sample_rate",
    "output_sample_rate",
)


def _accepted_profile(entry: object) -> voice_backend.DeviceProfileKey | None:
    """Parse one exact D9 key; anything malformed or non-headphones is skipped."""
    if not isinstance(entry, Mapping) or set(entry) != set(_PROFILE_KEY_FIELDS):
        return None
    strings = {name: entry[name] for name in ("input_uid", "output_uid", "backend", "aec_mode")}
    rates = {name: entry[name] for name in ("input_sample_rate", "output_sample_rate")}
    if (
        not isinstance(strings["input_uid"], str)
        or not isinstance(strings["output_uid"], str)
        or not isinstance(strings["backend"], str)
        or strings["aec_mode"] != "none"
        or entry["route_kind"] != voice_backend.RouteKind.HEADPHONES.value
        or any(isinstance(rate, bool) or not isinstance(rate, int) for rate in rates.values())
    ):
        return None
    return voice_backend.DeviceProfileKey(
        input_uid=strings["input_uid"],
        output_uid=strings["output_uid"],
        backend=strings["backend"],
        route_kind=voice_backend.RouteKind.HEADPHONES,
        input_sample_rate=int(rates["input_sample_rate"]),
        output_sample_rate=int(rates["output_sample_rate"]),
    )


def _barge_in_config(raw: object) -> dict[str, Any]:
    """Fail closed to ``ptt`` and an empty accepted list on any malformed value."""
    values = raw if isinstance(raw, Mapping) else {}
    if raw is not None and not isinstance(raw, Mapping):
        LOGGER.warning("realtime.single_audio_ingress.barge_in is not a mapping; using ptt")
    mode = values.get("detection_mode", "ptt")
    if mode not in {"ptt", "keyword_two_stage"}:
        LOGGER.warning(
            "realtime.single_audio_ingress.barge_in.detection_mode=%r unsupported; using ptt",
            mode,
        )
        mode = "ptt"
    accepted_raw = values.get("accepted_natural_profiles", [])
    if not isinstance(accepted_raw, list):
        LOGGER.warning("barge_in.accepted_natural_profiles is not a list; accepting none")
        accepted_raw = []
    accepted: list[voice_backend.DeviceProfileKey] = []
    for entry in accepted_raw:
        key = _accepted_profile(entry)
        if key is None:
            LOGGER.warning("barge_in.accepted_natural_profiles entry skipped: %r", entry)
            continue
        accepted.append(key)
    return {"barge_detection_mode": mode, "accepted_natural_profiles": tuple(accepted)}


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
