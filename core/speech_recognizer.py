"""Speech recognition utilities backed by a lazily loaded Whisper model."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Structured transcription output returned by the speech recognizer.

    Attributes:
        text: The recognized text content.
        language: The detected or configured language code.
        confidence: A best-effort confidence score in the range `[0.0, 1.0]`.
    """

    text: str
    language: str
    confidence: float


class SpeechRecognizer:
    """Transcribe recorded audio with an OpenAI Whisper model.

    Args:
        config: Application configuration. The recognizer reads the `asr`
            section when present and falls back to top-level keys.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the recognizer without loading the model yet.

        Args:
            config: Parsed application configuration.
        """

        asr_config = config.get("asr", config)
        self.model_size = str(asr_config.get("model_size", "base"))
        configured_language = asr_config.get("language")
        self.language = str(configured_language) if configured_language else None
        self.logger = LOGGER
        self._model: Any | None = None
        self._whisper_module: Any | None = None

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe a mono audio array into text.

        Args:
            audio: Recorded audio samples as a NumPy array. Integer PCM data is
                normalized automatically before transcription.

        Returns:
            A structured transcription result containing text, language, and a
            best-effort confidence score.

        Raises:
            RuntimeError: If the `openai-whisper` package is unavailable.
            ValueError: If the supplied audio array is empty.
        """

        normalized_audio = self._normalize_audio(audio)
        if normalized_audio.size == 0:
            raise ValueError("Cannot transcribe an empty audio array.")

        model = self._load_model()
        transcription = model.transcribe(
            normalized_audio,
            language=self.language,
            fp16=False,
            verbose=False,
        )
        text = str(transcription.get("text", "")).strip()
        language = str(transcription.get("language") or self.language or "unknown")
        confidence = self._estimate_confidence(transcription)
        self.logger.info(
            "Transcription completed with language=%s confidence=%.3f text=%r",
            language,
            confidence,
            text,
        )
        return TranscriptionResult(text=text, language=language, confidence=confidence)

    def _load_model(self) -> Any:
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
