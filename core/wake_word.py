"""Wake word detector using openwakeword (free, no API key needed)."""

from __future__ import annotations

import logging

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
        self.logger = LOGGER
        self._model = None

    def start(self) -> None:
        """Initialize the openwakeword engine."""
        import openwakeword
        from openwakeword.model import Model

        model_map = {
            "jarvis": "hey_jarvis_v0.1",
            "hey jarvis": "hey_jarvis_v0.1",
            "hey_jarvis": "hey_jarvis_v0.1",
        }
        model_name = model_map.get(self.keyword, "hey_jarvis_v0.1")

        # Find full path from installed models
        model_path = None
        for path in openwakeword.get_pretrained_model_paths():
            if model_name in path:
                model_path = path
                break

        if model_path is None:
            raise RuntimeError(f"openwakeword model '{model_name}' not found")

        self._model = Model(
            wakeword_model_paths=[model_path],
        )
        self.logger.info(
            "openwakeword detector started (model=%s, threshold=%.2f)",
            model_name, self.threshold,
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
