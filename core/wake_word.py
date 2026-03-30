"""Porcupine-based wake word detector for 'Hey Jarvis' activation."""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class WakeWordDetector:
    """Detect the 'Hey Jarvis' wake word using Picovoice Porcupine.

    Runs on raw PCM audio frames from a continuous microphone stream.
    When the wake word is detected, the main loop transitions from
    passive listening to active conversation mode.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        wake_config = config.get("wake_word", {})
        self.access_key = str(wake_config.get("picovoice_access_key", "")).strip()
        self.keyword = str(wake_config.get("keyword", "jarvis")).strip().lower()
        self.sensitivity = float(wake_config.get("sensitivity", 0.5))
        self.logger = LOGGER
        self._porcupine: Any = None

    def start(self) -> None:
        """Initialize the Porcupine engine.

        Raises:
            RuntimeError: If pvporcupine is not installed or the access key is invalid.
        """
        try:
            import pvporcupine
        except ImportError as exc:
            raise RuntimeError(
                "pvporcupine is required for wake word detection. "
                "Install with: pip install pvporcupine"
            ) from exc

        if not self.access_key:
            raise RuntimeError(
                "Picovoice access key is required. "
                "Get one at https://console.picovoice.ai/ and set "
                "wake_word.picovoice_access_key in config.yaml."
            )

        self._porcupine = pvporcupine.create(
            access_key=self.access_key,
            keywords=[self.keyword],
            sensitivities=[self.sensitivity],
        )
        self.logger.info(
            "Porcupine wake word detector started (keyword=%s, sensitivity=%.2f)",
            self.keyword,
            self.sensitivity,
        )

    def process_frame(self, pcm_frame: list[int]) -> bool:
        """Process one audio frame and check for the wake word.

        Args:
            pcm_frame: A list of 16-bit PCM samples.  The length must
                match ``frame_length``.

        Returns:
            True if the wake word was detected in this frame.

        Raises:
            RuntimeError: If the detector has not been started.
        """
        if self._porcupine is None:
            raise RuntimeError("WakeWordDetector has not been started.")
        result = self._porcupine.process(pcm_frame)
        return result >= 0

    @property
    def frame_length(self) -> int:
        """Number of samples per frame expected by Porcupine."""
        if self._porcupine is not None:
            return self._porcupine.frame_length
        return 512

    @property
    def sample_rate(self) -> int:
        """Sample rate expected by Porcupine (always 16000 Hz)."""
        if self._porcupine is not None:
            return self._porcupine.sample_rate
        return 16000

    def stop(self) -> None:
        """Release Porcupine resources."""
        if self._porcupine is not None:
            self._porcupine.delete()
            self._porcupine = None
            self.logger.info("Porcupine wake word detector stopped.")
