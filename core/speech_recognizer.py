"""Speech recognition — SenseVoice (sherpa-onnx) primary, Whisper fallback."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

import re

import numpy as np

LOGGER = logging.getLogger(__name__)

_SAMPLE_RATE = 16000

# SenseVoice 有时在语气词前插入句号，如 "学会查汇率。了" → "学会查汇率了"
_MISPLACED_PERIOD = re.compile(r"[。，]([了吧啊呢嘛呀哦哈的吗啦噢])")


@dataclass
class TranscriptionResult:
    """Structured transcription output returned by the speech recognizer.

    Attributes:
        text: The recognized text content.
        language: The detected or configured language code.
        confidence: A best-effort confidence score in the range `[0.0, 1.0]`.
        emotion: Detected emotion (SenseVoice only), e.g. "NEUTRAL", "HAPPY".
        event: Detected audio event (SenseVoice only), e.g. "Speech", "Laughter".
    """

    text: str
    language: str
    confidence: float
    emotion: str = ""
    event: str = ""


class SpeechRecognizer:
    """Transcribe audio — SenseVoice (sherpa-onnx) primary, Whisper fallback.

    Args:
        config: Application configuration. The recognizer reads the ``asr``
            section when present and falls back to top-level keys.
    """

    def __init__(self, config: dict) -> None:
        asr_config = config.get("asr", config)
        self.model_size = str(asr_config.get("model_size", "base"))
        configured_language = asr_config.get("language")
        self.language = str(configured_language) if configured_language else None
        self.logger = LOGGER

        # Provider selection: sensevoice (fast, local) or local (Whisper)
        self.provider = str(asr_config.get("provider", "local")).strip().lower()
        self.fallback_to_local = bool(asr_config.get("fallback_to_local", True))

        # SenseVoice config
        self._sensevoice_model_dir = Path(asr_config.get(
            "sensevoice_model_dir", "data/sensevoice-small-int8",
        ))
        self._sensevoice_num_threads = int(asr_config.get("sensevoice_num_threads", 4))
        self._sherpa_recognizer: Any | None = None

        # Whisper (lazy loaded)
        self._model: Any | None = None
        self._whisper_module: Any | None = None

        # Auto-degrade if SenseVoice model missing
        if self.provider == "sensevoice" and not (self._sensevoice_model_dir / "model.int8.onnx").exists():
            self.logger.warning(
                "SenseVoice model not found at %s, falling back to local Whisper",
                self._sensevoice_model_dir,
            )
            self.provider = "local"

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe a mono audio array into text.

        Args:
            audio: Recorded audio samples as a NumPy array. Integer PCM data is
                normalized automatically before transcription.

        Returns:
            A structured transcription result containing text, language, and a
            best-effort confidence score.

        Raises:
            RuntimeError: If no ASR backend is available.
            ValueError: If the supplied audio array is empty.
        """
        normalized_audio = self._normalize_audio(audio)
        if normalized_audio.size == 0:
            raise ValueError("Cannot transcribe an empty audio array.")

        if self.provider == "sensevoice":
            try:
                return self._transcribe_sensevoice(normalized_audio)
            except Exception as exc:
                self.logger.warning("SenseVoice failed: %s", exc)
                if not self.fallback_to_local:
                    raise

        return self._transcribe_whisper(normalized_audio)

    # ------------------------------------------------------------------
    # SenseVoice backend (sherpa-onnx)
    # ------------------------------------------------------------------

    def _transcribe_sensevoice(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe using SenseVoice INT8 via sherpa-onnx."""
        recognizer = self._load_sensevoice()
        stream = recognizer.create_stream()
        stream.accept_waveform(_SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)

        result = stream.result
        text = _MISPLACED_PERIOD.sub(r"\1", result.text.strip())

        # Extract detected language (e.g. "<|zh|>" → "zh")
        raw_lang = getattr(result, "lang", "") or ""
        language = raw_lang.strip("<|>") if raw_lang else (self.language or "zh")

        # Extract emotion/event for downstream use
        emotion = getattr(result, "emotion", "") or ""
        event = getattr(result, "event", "") or ""

        # Confidence heuristic: low for silence/hallucination
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.01 or len(text) <= 1:
            confidence = 0.1
            text = ""
        else:
            confidence = 0.9

        # Parse emotion tag (e.g. "<|HAPPY|>" → "HAPPY")
        emotion_clean = emotion.strip("<|>") if emotion else ""
        event_clean = event.strip("<|>") if event else ""

        self.logger.info(
            "SenseVoice: lang=%s emotion=%s event=%s conf=%.1f text=%r",
            language, emotion_clean, event_clean, confidence, text,
        )
        return TranscriptionResult(
            text=text, language=language, confidence=confidence,
            emotion=emotion_clean, event=event_clean,
        )

    def _load_sensevoice(self) -> Any:
        """Lazy-load the sherpa-onnx SenseVoice recognizer."""
        if self._sherpa_recognizer is not None:
            return self._sherpa_recognizer

        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "sherpa-onnx is required for SenseVoice ASR. "
                "Install with: pip install sherpa-onnx"
            ) from exc

        model_path = self._sensevoice_model_dir
        self.logger.info("Loading SenseVoice model from %s", model_path)
        # Force the configured language so short utterances don't get
        # misidentified as Japanese/Korean by SenseVoice's multilingual
        # detector. Empty string = auto-detect (SenseVoice default).
        self._sherpa_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path / "model.int8.onnx"),
            tokens=str(model_path / "tokens.txt"),
            num_threads=self._sensevoice_num_threads,
            language=self.language or "",
            use_itn=True,
        )
        return self._sherpa_recognizer

    # ------------------------------------------------------------------
    # Whisper backend (local, fallback)
    # ------------------------------------------------------------------

    def _transcribe_whisper(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe using local Whisper model."""
        model = self._load_whisper()
        transcription = model.transcribe(
            audio,
            language=self.language,
            fp16=False,
            verbose=False,
        )
        text = str(transcription.get("text", "")).strip()
        language = str(transcription.get("language") or self.language or "unknown")
        confidence = self._estimate_confidence(transcription)
        self.logger.info(
            "Whisper transcription: language=%s confidence=%.3f text=%r",
            language, confidence, text,
        )
        return TranscriptionResult(text=text, language=language, confidence=confidence)

    def _load_whisper(self) -> Any:
        """Load the Whisper model on first use and reuse it afterwards."""
        if self._model is not None:
            return self._model

        try:
            import whisper
        except ImportError as exc:
            raise RuntimeError(
                "openai-whisper is required for speech recognition but is not installed."
            ) from exc

        self.logger.info("Loading Whisper model: %s", self.model_size)
        self._whisper_module = whisper
        self._model = whisper.load_model(self.model_size)
        return self._model

    def _estimate_confidence(self, transcription: dict[str, Any]) -> float:
        """Estimate a confidence score from Whisper segment log probabilities."""

        segments = transcription.get("segments") or []
        if not segments:
            return 0.0 if not str(transcription.get("text", "")).strip() else 0.5

        scores: list[float] = []
        for segment in segments:
            avg_logprob = float(segment.get("avg_logprob", -1.0))
            no_speech_prob = float(segment.get("no_speech_prob", 0.0))
            probability = float(np.exp(min(avg_logprob, 0.0)))
            scores.append(max(0.0, min(1.0, probability * (1.0 - no_speech_prob))))

        return float(np.mean(scores, dtype=np.float64))

    def _normalize_audio(self, audio: np.ndarray) -> np.ndarray:
        """Convert audio arrays to a one-dimensional float32 waveform."""

        array = np.asarray(audio)
        if array.ndim == 0:
            array = array.reshape(1)
        if array.ndim > 1:
            if array.shape[-1] == 1:
                array = array.reshape(-1)
            else:
                array = np.mean(array, axis=-1)

        if np.issubdtype(array.dtype, np.integer):
            info = np.iinfo(array.dtype)
            scale = float(max(abs(info.min), info.max))
            normalized = array.astype(np.float32) / scale
        else:
            normalized = array.astype(np.float32, copy=False)

        return np.clip(normalized, -1.0, 1.0)
