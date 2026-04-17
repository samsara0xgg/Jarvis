"""Full-duplex interrupt monitoring during TTS playback.

Detects voice interrupts via streaming ASR keyword matching and
provides utilities for interrupt content processing.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

INTERRUPT_KEYWORDS = frozenset({
    "等一下", "停", "打住", "暂停", "等等", "你听我说",
    "不对", "你理解错了", "不是这样", "说错了",
})

RESUME_KEYWORDS = frozenset({
    "继续说", "接着说", "你继续", "继续",
})

# Pattern: keyword optionally followed by punctuation/space, then content
_STRIP_RE = re.compile(
    r"^(" + "|".join(re.escape(kw) for kw in sorted(INTERRUPT_KEYWORDS, key=len, reverse=True))
    + r")[，,。.！!？?\s]*",
)


def strip_interrupt_prefix(text: str) -> str:
    """Remove leading interrupt keyword + trailing punctuation from text.

    >>> strip_interrupt_prefix("停，改成多伦多的天气")
    '改成多伦多的天气'
    >>> strip_interrupt_prefix("明天天气怎么样")
    '明天天气怎么样'
    """
    return _STRIP_RE.sub("", text)


class InterruptMonitor:
    """Monitor audio during TTS playback for interrupt keywords.

    Feeds audio chunks to a streaming ASR recognizer, checks partial
    results against keyword sets, and fires callbacks on detection.

    Args:
        config: Application config dict (reads ``interrupt`` section).
        on_interrupt: Called when an interrupt keyword is detected.
        on_resume: Called when a resume keyword is detected.
    """

    def __init__(
        self,
        config: dict,
        on_interrupt: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        on_soft_pause: Callable[[], None] | None = None,
        on_soft_resume: Callable[[], None] | None = None,
    ) -> None:
        icfg = config.get("interrupt", {})
        self.enabled = bool(icfg.get("enabled", False))
        self._on_interrupt = on_interrupt
        self._on_resume = on_resume
        self._on_soft_pause = on_soft_pause
        self._on_soft_resume = on_soft_resume
        self._fired = False
        self._lock = threading.Lock()

        # WP7 soft-stop: pause TTS playback as soon as VAD detects user
        # speech, and resume on either VAD-end OR a no-keyword timeout.
        # Off by default — flip on after Allen verifies SIGSTOP+afplay
        # behavior on the target machine.
        self._soft_stop_enabled = bool(icfg.get("soft_stop_enabled", False))
        self._soft_stop_timeout_s = float(icfg.get("soft_stop_timeout_ms", 3000)) / 1000.0
        self._soft_state = "NORMAL"  # NORMAL | DUCKED | CANCELLED
        self._was_speech_detected = False
        self._soft_resume_timer: threading.Timer | None = None

        # Custom keyword sets from config (fallback to defaults)
        kw_list = icfg.get("keywords")
        self._interrupt_kw = frozenset(kw_list) if kw_list else INTERRUPT_KEYWORDS
        resume_list = icfg.get("resume_keywords")
        self._resume_kw = frozenset(resume_list) if resume_list else RESUME_KEYWORDS

        # Streaming ASR recognizer (lazy-loaded)
        self._recognizer: Any = None
        self._stream: Any = None
        self._asr_config = icfg.get("streaming_asr", {})

        # Audio accumulator for post-interrupt re-transcription
        self._audio_chunks: list[np.ndarray] = []
        self._recording = False

        # Streaming ASR buffer — accumulate small chunks to avoid
        # sherpa-onnx feature-extraction crash on too-short frames.
        self._asr_buffer = np.array([], dtype=np.float32)
        self._min_chunk_samples = int(icfg.get("streaming_asr_chunk_samples", 3200))

        # Silero VAD gate (loaded lazily in start() for symmetry with recognizer)
        self._vad: Any = None
        self._vad_config = icfg

        # Mic listener state (initialized here for type-safety)
        self._mic_stop: threading.Event | None = None
        self._mic_stream: Any = None
        self._mic_thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts."""
        if not self.enabled:
            return
        self._fired = False
        self._audio_chunks = []
        self._recording = True
        self._load_recognizer()
        self._load_vad()
        self._asr_buffer = np.array([], dtype=np.float32)
        if self._recognizer:
            self._stream = self._recognizer.create_stream()
        if self._vad is not None:
            self._vad.reset()
        # Reset soft-stop state for the new session
        self._soft_state = "NORMAL"
        self._was_speech_detected = False
        self._cancel_soft_timer()

    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None."""
        self._recording = False
        self._cancel_soft_timer()
        # If we exited mid-DUCKED without a keyword, ensure the caller's
        # playback state isn't left frozen.
        with self._lock:
            should_resume = (
                self._soft_stop_enabled
                and self._soft_state == "DUCKED"
                and self._on_soft_resume is not None
            )
            self._soft_state = "NORMAL"
        if should_resume:
            try:
                self._on_soft_resume()
            except Exception as exc:
                LOGGER.warning("on_soft_resume on stop() failed: %s", exc)
        if self._stream and self._recognizer:
            try:
                self._recognizer.decode_stream(self._stream)
            except Exception:
                pass
            self._stream = None
        if self._audio_chunks:
            result = np.concatenate(self._audio_chunks)
            self._audio_chunks = []
            return result
        return None

    def reset(self) -> None:
        """Reset fired state so new detections can trigger."""
        with self._lock:
            self._fired = False
        if self._recognizer and self._stream is None:
            self._stream = self._recognizer.create_stream()

    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Silero VAD gates the stream: non-speech chunks are accumulated for
        post-interrupt re-transcription but NOT forwarded to streaming ASR.
        This avoids wasting CPU on AEC residual noise and reduces false
        keyword triggers.
        """
        if not self.enabled or not self._recording:
            return

        # Accumulate for post-interrupt re-transcription (stop after fired)
        if not self._fired:
            self._audio_chunks.append(audio.copy())

        # VAD gate: skip ASR when no speech detected
        if self._vad is not None:
            try:
                self._vad.accept_waveform(audio)
                is_speech = self._vad.is_speech_detected()
                if self._soft_stop_enabled:
                    self._update_soft_state(is_speech)
                if not is_speech:
                    return
            except Exception as exc:
                LOGGER.debug("VAD gate error: %s", exc)
                return

        if self._stream and self._recognizer:
            try:
                with self._lock:
                    self._asr_buffer = np.concatenate([self._asr_buffer, audio])
                    if len(self._asr_buffer) < self._min_chunk_samples:
                        return
                    chunk = self._asr_buffer
                    self._asr_buffer = np.array([], dtype=np.float32)
                self._stream.accept_waveform(sample_rate, chunk)
                while self._recognizer.is_ready(self._stream):
                    self._recognizer.decode_stream(self._stream)
                result = self._recognizer.get_result(self._stream)
                text = result.text.strip() if hasattr(result, 'text') else str(result).strip()
                if text:
                    self._check_partial(text)
            except Exception as exc:
                LOGGER.debug("Streaming ASR error: %s", exc)

    def _check_partial(self, text: str) -> None:
        """Check a partial ASR result against keyword sets."""
        if not self.enabled:
            return
        callback = None
        with self._lock:
            if self._fired:
                return
            for kw in self._interrupt_kw:
                if kw in text:
                    self._fired = True
                    callback = self._on_interrupt
                    # Hard interrupt path: cancel any pending soft resume
                    # so we don't accidentally SIGCONT after stop() killed it.
                    self._soft_state = "CANCELLED"
                    self._cancel_soft_timer_locked()
                    break
            if callback is None:
                for kw in self._resume_kw:
                    if kw in text:
                        self._fired = True
                        callback = self._on_resume
                        break
        if callback:
            callback()

    # ------------------------------------------------------------------
    # WP7 soft-stop helpers
    # ------------------------------------------------------------------

    def _update_soft_state(self, is_speech: bool) -> None:
        """Drive the NORMAL → DUCKED → NORMAL transitions on VAD edges."""
        callback: Callable[[], None] | None = None
        with self._lock:
            prev = self._was_speech_detected
            self._was_speech_detected = is_speech

            if (
                not prev and is_speech
                and self._soft_state == "NORMAL"
                and self._on_soft_pause is not None
            ):
                self._soft_state = "DUCKED"
                self._start_soft_timer_locked()
                callback = self._on_soft_pause
            elif (
                prev and not is_speech
                and self._soft_state == "DUCKED"
            ):
                # VAD ended without a keyword landing — resume playback now
                # rather than waiting for the timer (faster recovery).
                self._soft_state = "NORMAL"
                self._cancel_soft_timer_locked()
                callback = self._on_soft_resume
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                LOGGER.warning("soft-stop callback failed: %s", exc)

    def _start_soft_timer_locked(self) -> None:
        """Schedule the no-keyword timeout. Caller must hold ``self._lock``."""
        if self._soft_resume_timer is not None:
            self._soft_resume_timer.cancel()
        timer = threading.Timer(self._soft_stop_timeout_s, self._on_soft_timeout)
        timer.daemon = True
        self._soft_resume_timer = timer
        timer.start()

    def _on_soft_timeout(self) -> None:
        """Fired by the timer: if still DUCKED, resume playback."""
        callback: Callable[[], None] | None = None
        with self._lock:
            if self._soft_state == "DUCKED":
                self._soft_state = "NORMAL"
                callback = self._on_soft_resume
            self._soft_resume_timer = None
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                LOGGER.warning("soft-stop timeout callback failed: %s", exc)

    def _cancel_soft_timer(self) -> None:
        with self._lock:
            self._cancel_soft_timer_locked()

    def _cancel_soft_timer_locked(self) -> None:
        if self._soft_resume_timer is not None:
            self._soft_resume_timer.cancel()
            self._soft_resume_timer = None

    def start_mic_listener(self, sample_rate: int = 16000, block_size: int = 1600) -> None:
        """Open a microphone stream and feed audio to the monitor.

        The stream runs in a background thread until ``stop_mic_listener``
        is called.  Designed for use during TTS playback when the main
        recording pipeline is idle.
        """
        if not self.enabled:
            return
        if self._mic_stream is not None:
            LOGGER.warning("start_mic_listener called while already running; ignoring")
            return
        import sounddevice as sd
        self._mic_stop = threading.Event()
        self._mic_stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block_size,
        )
        self._mic_stream.start()

        # Capture stream in closure so reader never dereferences self._mic_stream
        _stream = self._mic_stream
        _stop = self._mic_stop

        def _reader() -> None:
            while not _stop.is_set():
                try:
                    data, _ = _stream.read(block_size)
                    self.feed_audio(data[:, 0], sample_rate)
                except Exception as exc:
                    if not _stop.is_set():
                        LOGGER.warning("Mic listener reader exited: %s", exc)
                    break

        self._mic_thread = threading.Thread(target=_reader, daemon=True, name="interrupt-mic")
        self._mic_thread.start()
        LOGGER.debug("Interrupt mic listener started")

    def stop_mic_listener(self) -> None:
        """Stop the background microphone stream."""
        if self._mic_stop is not None:
            self._mic_stop.set()
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=2)
            if self._mic_thread.is_alive():
                LOGGER.warning("interrupt-mic thread did not exit within 2s")
            self._mic_thread = None
        self._mic_stop = None
        LOGGER.debug("Interrupt mic listener stopped")

    def _load_recognizer(self) -> None:
        """Lazy-load the sherpa-onnx streaming recognizer."""
        if self._recognizer is not None:
            return
        model_dir = self._asr_config.get("model_dir", "")
        if not model_dir:
            LOGGER.info(
                "No streaming ASR model configured; keyword detection disabled"
            )
            return
        try:
            import sherpa_onnx
            from pathlib import Path

            p = Path(model_dir)
            encoder = str(p / self._asr_config.get(
                "encoder", "encoder-epoch-99-avg-1.int8.onnx"))
            decoder = str(p / self._asr_config.get(
                "decoder", "decoder-epoch-99-avg-1.int8.onnx"))
            joiner = str(p / self._asr_config.get(
                "joiner", "joiner-epoch-99-avg-1.int8.onnx"))
            tokens = str(p / "tokens.txt")
            if not Path(encoder).exists():
                LOGGER.warning(
                    "Streaming ASR model not found at %s", model_dir
                )
                return
            num_threads = int(self._asr_config.get("num_threads", 2))
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                tokens=tokens,
                num_threads=num_threads,
                sample_rate=16000,
                feature_dim=80,
            )
            LOGGER.info("Streaming ASR loaded from %s", model_dir)
        except ImportError:
            LOGGER.warning(
                "sherpa-onnx not installed; streaming ASR unavailable"
            )
        except Exception as exc:
            LOGGER.warning("Failed to load streaming ASR: %s", exc)

    def _load_vad(self) -> None:
        """Lazy-load the VAD gate (provider chosen via config).

        WP6: dispatches to ``vad_silero.build_vad`` in ``mode='tts'`` so
        we get the during-TTS thresholds (Mac vs RPi dB defaults baked in).
        Fail fast on load errors — no fallback path.
        """
        if self._vad is not None:
            return
        model_path = self._vad_config.get("vad_model_path", "")
        if not model_path:
            LOGGER.info("No VAD model configured; interrupt gate disabled")
            return
        from core.vad_silero import build_vad
        self._vad = build_vad(self._vad_config, mode="tts")
        provider = self._vad_config.get("vad_provider", "silero_direct")
        LOGGER.info(
            "Interrupt VAD loaded (provider=%s, model=%s)", provider, model_path,
        )
