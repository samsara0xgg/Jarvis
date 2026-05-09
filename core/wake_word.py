"""Wake word detector using openwakeword (free, no API key needed)."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

LOGGER = logging.getLogger(__name__)


class WakeWordDetector:
    """Detect wake words using openwakeword.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        wake_config = config.get("wake_word", {})
        self.keyword = str(wake_config.get("keyword", "jarvis")).strip().lower()
        self.threshold = float(wake_config.get("sensitivity", 0.5))
        self.inference_framework = str(
            wake_config.get("inference_framework", "onnx")
        ).strip().lower()
        # 0.0 disables openwakeword's built-in Silero VAD pre-gate. Raise (e.g. 0.3-0.5)
        # only if false wakes from TV/music are observed; on XVF3800 the hardware NS
        # already filters most non-speech, so leave at 0.0 unless measured otherwise.
        self.vad_threshold = float(wake_config.get("vad_threshold", 0.0))
        self.logger = LOGGER
        self._model = None

    def start(self) -> None:
        """Initialize the openwakeword engine."""
        import openwakeword
        from openwakeword.utils import download_models
        from openwakeword.model import Model

        model_map = {
            "jarvis": "hey_jarvis_v0.1",
            "hey jarvis": "hey_jarvis_v0.1",
            "hey_jarvis": "hey_jarvis_v0.1",
        }
        model_name = model_map.get(self.keyword, "hey_jarvis_v0.1")

        def _find_model_path() -> str | None:
            for path in openwakeword.get_pretrained_model_paths(self.inference_framework):
                if model_name in path and Path(path).exists():
                    return path
            return None

        # Find full path from installed models. Some openwakeword wheels ship
        # metadata without the actual model assets, so download the requested
        # official model on first use if the path is missing.
        model_path = _find_model_path()
        if model_path is None:
            self.logger.info("Downloading openwakeword model assets for %s", model_name)
            download_models([model_name])
            model_path = _find_model_path()
        if model_path is None:
            available = [
                path for path in openwakeword.get_pretrained_model_paths(self.inference_framework)
                if model_name in path
            ]
            raise RuntimeError(
                f"openwakeword model '{model_name}' not found for "
                f"{self.inference_framework}; candidates={available}"
            )

        kwargs: dict = {
            "wakeword_models": [model_path],
            "inference_framework": self.inference_framework,
        }
        if self.vad_threshold > 0:
            kwargs["vad_threshold"] = self.vad_threshold
        self._model = Model(**kwargs)
        self.logger.info(
            "openwakeword detector started (model=%s, framework=%s, threshold=%.2f, vad_threshold=%.2f)",
            model_name, self.inference_framework, self.threshold, self.vad_threshold,
        )

    def process_frame(self, pcm_frame: list[int]) -> bool:
        """Process one audio frame and check for the wake word.

        Args:
            pcm_frame: A list of 16-bit PCM samples.

        Returns:
            True if the wake word was detected.
        """
        if self._model is None:
            raise RuntimeError("WakeWordDetector has not been started.")
        audio = np.array(pcm_frame, dtype=np.int16)
        predictions = self._model.predict(audio)
        for score in predictions.values():
            if score > self.threshold:
                self._model.reset()
                return True
        return False

    def reset(self) -> None:
        """Reset internal model state (clears accumulated audio features)."""
        if self._model is not None:
            self._model.reset()

    @property
    def frame_length(self) -> int:
        """Number of samples per frame (80ms at 16kHz)."""
        return 1280

    @property
    def sample_rate(self) -> int:
        """Sample rate (always 16000 Hz)."""
        return 16000

    def stop(self) -> None:
        """Release resources."""
        self._model = None
        self.logger.info("Wake word detector stopped.")
