"""Silero VAD direct ONNX runtime — frame-level probability + dB.

Replaces sherpa-onnx's `VoiceActivityDetector` wrapper. The wrapper hides
per-frame probability behind segment-level events; we need raw frame
output (every 32ms) so WP7 can drive volume ducking + soft-stop on the
earliest sign of user speech.

ONNX signature (the bundled ``data/silero_vad.onnx`` is the *pre-v4*
release shipped by sherpa-onnx — older than the typical "state[2,1,128]
+ sr" interface)::

    Inputs:  x  float32[1, 512]   # 32ms @ 16 kHz, fixed
             h  float32[2, 1, 64] # LSTM hidden
             c  float32[2, 1, 64] # LSTM cell
    Outputs: prob   float32[1,1]
             new_h  float32[2, 1, 64]
             new_c  float32[2, 1, 64]

Compatibility shim with the previous wrapper:
  - ``accept_waveform(audio)``  feed any-length audio (auto-chunked)
  - ``is_speech_detected()``    True while in ACTIVE state
  - ``empty()``                  False when a complete speech segment
                                 has been observed since the last reset
                                 (mirrors sherpa-onnx queue semantics)
  - ``reset()``                  clears LSTM state + state machine
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

_CHUNK_SAMPLES = 512  # silero is fixed-size per inference (32ms @ 16kHz)
_LSTM_SHAPE = (2, 1, 64)


class SileroVADDirect:
    """Direct onnxruntime VAD with frame-level probability output."""

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 16000,
        prob_threshold: float = 0.4,
        db_threshold: float = -45.0,  # dBFS; silence is typically < -50, speech ~ -30
        smoothing_window: int = 5,
        required_hits: int = 3,
        required_misses: int = 24,
    ) -> None:
        if sample_rate != 16000:
            raise ValueError(
                f"silero_vad.onnx only supports 16kHz, got {sample_rate}",
            )
        import onnxruntime as ort

        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"],
        )
        self._sample_rate = sample_rate
        self._prob_threshold = float(prob_threshold)
        self._db_threshold = float(db_threshold)
        self._smoothing_window = int(smoothing_window)
        self._required_hits = int(required_hits)
        self._required_misses = int(required_misses)

        self._h: np.ndarray
        self._c: np.ndarray
        self._buffer: np.ndarray
        self._prob_window: deque[float]
        self._db_window: deque[float]
        self._state: str
        self._hits: int
        self._misses: int
        self._segment_completed: bool
        self.reset()

        # Warm up — first inference takes ~200ms; do it now so the first
        # real chunk doesn't pay the cold-start tax during a live mic stream.
        try:
            self._infer_chunk(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        except Exception as exc:
            LOGGER.warning("Silero VAD warm-up failed: %s", exc)
        self.reset()

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear LSTM state and state-machine bookkeeping. Call before each session."""
        self._h = np.zeros(_LSTM_SHAPE, dtype=np.float32)
        self._c = np.zeros(_LSTM_SHAPE, dtype=np.float32)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._prob_window = deque(maxlen=self._smoothing_window)
        self._db_window = deque(maxlen=self._smoothing_window)
        self._state = "IDLE"
        self._hits = 0
        self._misses = 0
        self._segment_completed = False
        # perf_counter timestamp of the most recent IDLE→ACTIVE transition.
        # Used by bench harnesses to measure "speech-onset → callback-fire"
        # latency end-to-end. None until the first START event.
        self._last_start_perf: float | None = None

    # ------------------------------------------------------------------
    # Public API (compatible with previous sherpa-onnx wrapper)
    # ------------------------------------------------------------------

    def accept_waveform(self, audio: np.ndarray, sample_rate: int | None = None) -> None:
        """Feed any-length audio; auto-chunks to 512-sample windows.

        Trailing samples (< 512) are kept in an internal buffer for the next call.
        """
        del sample_rate  # ignored — fixed at construction time
        if audio.size == 0:
            return
        if audio.ndim > 1:
            audio = audio.reshape(-1)
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        self._buffer = np.concatenate([self._buffer, audio])
        while self._buffer.size >= _CHUNK_SAMPLES:
            chunk = self._buffer[:_CHUNK_SAMPLES]
            self._buffer = self._buffer[_CHUNK_SAMPLES:]
            prob = self._infer_chunk(chunk)
            db = _chunk_db(chunk)
            self._advance_state(prob, db)

    def is_speech_detected(self) -> bool:
        """True while currently in ACTIVE state (mirrors sherpa-onnx wrapper)."""
        return self._state == "ACTIVE"

    @property
    def last_start_perf(self) -> float | None:
        """``time.perf_counter()`` at the last IDLE→ACTIVE transition (None if never)."""
        return self._last_start_perf

    def empty(self) -> bool:
        """False when at least one complete speech segment has been observed.

        The previous sherpa-onnx wrapper exposed segment availability via
        ``empty()``; AudioRecorder polls it after each ``accept_waveform``
        and stops recording when False. We track the same flag here.
        """
        return not self._segment_completed

    # ------------------------------------------------------------------
    # Frame-level state transition
    # ------------------------------------------------------------------

    def _infer_chunk(self, chunk: np.ndarray) -> float:
        x = chunk.reshape(1, -1).astype(np.float32, copy=False)
        outputs = self._session.run(
            None, {"x": x, "h": self._h, "c": self._c},
        )
        prob = float(outputs[0].squeeze())
        self._h = outputs[1]
        self._c = outputs[2]
        return prob

    def _advance_state(self, prob: float, db: float) -> None:
        self._prob_window.append(prob)
        self._db_window.append(db)
        smooth_prob = float(np.mean(self._prob_window))
        smooth_db = float(np.mean(self._db_window))

        # A frame counts as speech when BOTH probability and energy clear
        # their thresholds. dB gate suppresses AEC residual + low-level
        # background noise that the model occasionally scores high on.
        is_speech = (
            smooth_prob >= self._prob_threshold
            and smooth_db >= self._db_threshold
        )

        if self._state == "IDLE":
            if is_speech:
                self._hits += 1
                if self._hits >= self._required_hits:
                    self._state = "ACTIVE"
                    self._misses = 0
                    self._last_start_perf = time.perf_counter()
            else:
                self._hits = 0
        else:  # ACTIVE
            if not is_speech:
                self._misses += 1
                if self._misses >= self._required_misses:
                    self._state = "IDLE"
                    self._segment_completed = True
                    self._hits = 0
            else:
                self._misses = 0


def _chunk_db(chunk: np.ndarray) -> float:
    """Approximate per-frame dBFS (clamp adds a floor to avoid -inf)."""
    rms = float(np.sqrt(np.mean(chunk * chunk)))
    return 20.0 * float(np.log10(rms + 1e-10))


# ---------------------------------------------------------------------------
# Provider factory — used by audio_recorder + interrupt_monitor
# ---------------------------------------------------------------------------

def build_vad(cfg: dict, *, mode: str = "record") -> Any:
    """Construct the configured VAD provider.

    Args:
        cfg: section dict (``audio:`` for record mode, ``interrupt:`` for tts mode)
        mode: ``"record"`` (default thresholds) or ``"tts"`` (during-TTS thresholds,
              picks Mac/RPi dB defaults via :func:`platform.system`).

    Returns:
        Either a :class:`SileroVADDirect` or a sherpa-onnx VoiceActivityDetector.
    """
    provider = str(cfg.get("vad_provider", "silero_direct")).lower()

    if provider == "silero_direct":
        if mode == "tts":
            import platform
            # During-TTS dBFS defaults tuned per-platform: Mac CoreAudio
            # returns higher energy (near-field mic + louder speaker),
            # RPi ReSpeaker post-AEC is quieter. Both are dBFS (negative).
            if platform.system() == "Darwin":
                db_default = float(cfg.get("vad_db_threshold_during_tts_mac", -22.0))
            else:
                db_default = float(cfg.get("vad_db_threshold_during_tts_rpi", -32.0))
            return SileroVADDirect(
                model_path=str(cfg["vad_model_path"]),
                prob_threshold=float(cfg.get("vad_prob_threshold_during_tts", 0.5)),
                db_threshold=db_default,
                smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
                required_hits=int(cfg.get("vad_required_hits", 3)),
                required_misses=int(cfg.get("vad_required_misses", 24)),
            )
        # Record-mode dBFS default: -45 clears typical silence (~-60)
        # but is well below normal speech (~-30).
        return SileroVADDirect(
            model_path=str(cfg["vad_model_path"]),
            prob_threshold=float(cfg.get("vad_prob_threshold", 0.4)),
            db_threshold=float(cfg.get("vad_db_threshold", -45.0)),
            smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
            required_hits=int(cfg.get("vad_required_hits", 3)),
            required_misses=int(cfg.get("vad_required_misses", 24)),
        )

    # Fallback: original sherpa-onnx wrapper
    import sherpa_onnx
    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = str(cfg["vad_model_path"])
    if mode == "tts":
        vad_cfg.silero_vad.threshold = float(cfg.get("vad_threshold_during_tts", 0.8))
        vad_cfg.silero_vad.min_speech_duration = float(
            cfg.get("vad_min_speech_duration", 0.15))
        vad_cfg.silero_vad.min_silence_duration = float(
            cfg.get("vad_min_silence_duration", 0.2))
        vad_cfg.silero_vad.max_speech_duration = float(
            cfg.get("vad_max_speech_duration", 10.0))
        buf_seconds = 10
    else:
        vad_cfg.silero_vad.threshold = float(cfg.get("vad_threshold", 0.5))
        vad_cfg.silero_vad.min_silence_duration = float(
            cfg.get("vad_silence_duration", 0.5))
        vad_cfg.silero_vad.min_speech_duration = float(
            cfg.get("vad_min_speech_duration", 0.25))
        vad_cfg.silero_vad.max_speech_duration = float(
            cfg.get("vad_max_speech_duration", 20.0))
        buf_seconds = 30
    vad_cfg.sample_rate = 16000
    return sherpa_onnx.VoiceActivityDetector(
        vad_cfg, buffer_size_in_seconds=buf_seconds,
    )
