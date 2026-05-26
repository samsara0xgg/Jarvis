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
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

SILERO_CHUNK_SAMPLES = 512  # silero fixed-size per inference (32 ms @ 16 kHz)
_LSTM_SHAPE = (2, 1, 64)
_SAMPLE_RATE = 16000


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
    import onnxruntime as ort  # type: ignore[import-not-found]  # noqa: PLC0415

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
    while :meth:`empty` flips to ``True`` once post-speech silence has
    been observed — the recorder uses that as the stop signal.
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
        """``True`` once post-speech silence has been observed.

        Flips to ``True`` the first time the state machine, having entered
        ACTIVE, sees a non-speech frame. The recorder polls this after
        each frame and stops capture when it flips.
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
            else:
                self._hits = 0
        elif not is_speech:
            # ACTIVE branch: first non-speech frame flips the
            # "post-speech silence seen" flag — that's the recorder's
            # stop signal (looser than the full required_misses
            # transition back to IDLE).
            self._post_speech_silence_seen = True
            self._misses += 1
            if self._misses >= self._t.required_misses:
                self._state = "IDLE"
                self._hits = 0
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
    import sounddevice as sd  # type: ignore[import-not-found]  # noqa: PLC0415

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
