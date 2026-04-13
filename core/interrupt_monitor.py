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
    ) -> None:
        icfg = config.get("interrupt", {})
        self.enabled = bool(icfg.get("enabled", False))
        self._on_interrupt = on_interrupt
        self._on_resume = on_resume
        self._fired = False
        self._lock = threading.Lock()

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

    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts."""
        if not self.enabled:
            return
        self._fired = False
        self._audio_chunks = []
        self._recording = True
        self._load_recognizer()
        if self._recognizer:
            self._stream = self._recognizer.create_stream()

    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None."""
        self._recording = False
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

        Args:
            audio: Float32 mono audio samples.
            sample_rate: Sample rate (must be 16000).
        """
        if not self.enabled or not self._recording:
            return

        # Accumulate for post-interrupt re-transcription (stop after fired)
        if not self._fired:
            self._audio_chunks.append(audio.copy())

        if self._stream and self._recognizer:
            try:
                self._stream.accept_waveform(sample_rate, audio)
                while self._recognizer.is_ready(self._stream):
                    self._recognizer.decode_stream(self._stream)
                result = self._recognizer.get_result(self._stream)
                if result.text.strip():
                    self._check_partial(result.text.strip())
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
                    break
            if callback is None:
                for kw in self._resume_kw:
                    if kw in text:
                        self._fired = True
                        callback = self._on_resume
                        break
        if callback:
            callback()

    def start_mic_listener(self, sample_rate: int = 16000, block_size: int = 1600) -> None:
        """Open a microphone stream and feed audio to the monitor.

        The stream runs in a background thread until ``stop_mic_listener``
        is called.  Designed for use during TTS playback when the main
        recording pipeline is idle.
        """
        if not self.enabled:
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

        def _reader() -> None:
            while not self._mic_stop.is_set():
                try:
                    data, _ = self._mic_stream.read(block_size)
                    self.feed_audio(data[:, 0], sample_rate)
                except Exception:
                    break

        self._mic_thread = threading.Thread(target=_reader, daemon=True, name="interrupt-mic")
        self._mic_thread.start()
        LOGGER.debug("Interrupt mic listener started")

    def stop_mic_listener(self) -> None:
        """Stop the background microphone stream."""
        if hasattr(self, "_mic_stop"):
            self._mic_stop.set()
        if hasattr(self, "_mic_stream") and self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        if hasattr(self, "_mic_thread") and self._mic_thread:
            self._mic_thread.join(timeout=2)
            self._mic_thread = None
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
            encoder = str(p / "encoder-epoch-99-avg-1.onnx")
            decoder = str(p / "decoder-epoch-99-avg-1.onnx")
            joiner = str(p / "joiner-epoch-99-avg-1.onnx")
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
