"""Audio recording utilities for capturing, validating, and saving mono WAV data."""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Any

import numpy as np
from scipy.io import wavfile

LOGGER = logging.getLogger(__name__)
_PROGRESS_LOGGER_NAME = f"{__name__}.progress"


class _InlineProgressHandler(logging.StreamHandler):
    """Render progress updates inline without forcing newline-separated output."""

    terminator = ""


class AudioRecorder:
    """Record, validate, and persist 16 kHz mono audio clips.

    Args:
        config: Application configuration dictionary. The recorder reads the
            `audio` section when present and falls back to top-level keys.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the audio recorder with validated configuration.

        Args:
            config: Parsed application configuration.
        """

        audio_config = config.get("audio", config)
        configured_sample_rate = int(audio_config.get("sample_rate", 16000))
        configured_channels = int(audio_config.get("channels", 1))

        if configured_sample_rate != 16000:
            LOGGER.warning(
                "Configured sample rate %s is unsupported; forcing 16000 Hz.",
                configured_sample_rate,
            )
        if configured_channels != 1:
            LOGGER.warning(
                "Configured channel count %s is unsupported; forcing mono.",
                configured_channels,
            )

        self.sample_rate = 16000
        self.channels = 1
        self.default_duration = float(audio_config.get("default_duration", 3.0))
        self.min_duration = float(audio_config.get("min_duration", 1.0))
        self.low_volume_threshold = float(audio_config.get("low_volume_threshold", 0.02))
        self.block_duration = float(audio_config.get("block_duration", 0.1))
        self.volume_bar_width = int(audio_config.get("volume_bar_width", 24))
        self.logger = LOGGER
        self._progress_logger = logging.getLogger(_PROGRESS_LOGGER_NAME)
        self._progress_logger.setLevel(logging.INFO)
        self._progress_logger.propagate = False
        self._ensure_progress_handler()

    def record(self, duration: float | None = None) -> np.ndarray:
        """Record audio for the requested duration and return normalized samples.

        Args:
            duration: Recording duration in seconds. When omitted, the default
                duration from the config is used.

        Returns:
            A one-dimensional `float32` NumPy array containing normalized audio
            samples in the range `[-1.0, 1.0]`.

        Raises:
            RuntimeError: If `sounddevice` is unavailable.
            TimeoutError: If recording does not complete in the expected time.
            ValueError: If the requested duration is not positive.
        """

        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "sounddevice is required for recording audio but is not installed."
            ) from exc

        target_duration = self.default_duration if duration is None else float(duration)
        if target_duration <= 0:
            raise ValueError("Recording duration must be greater than zero.")

        target_frames = int(math.ceil(target_duration * self.sample_rate))
        blocksize = max(1, int(self.block_duration * self.sample_rate))
        captured_frames = 0
        audio_chunks: list[np.ndarray] = []
        finished = threading.Event()
        progress_started = False

        def callback(
            indata: np.ndarray,
            frames: int,
            time_info: Any,
            status: Any,
        ) -> None:
            """Collect input frames and update the terminal volume meter."""

            del time_info
            nonlocal captured_frames, progress_started

            if status:
                self.logger.warning("Audio input status: %s", status)

            remaining = target_frames - captured_frames
            if remaining <= 0:
                finished.set()
                raise sd.CallbackStop()

            chunk = np.asarray(indata[: min(frames, remaining), 0], dtype=np.float32).copy()
            audio_chunks.append(chunk)
            captured_frames += chunk.shape[0]
            progress_started = True
            self._render_volume_bar(
                level=self.get_volume_level(chunk),
                captured_frames=captured_frames,
                target_frames=target_frames,
            )

            if captured_frames >= target_frames:
                finished.set()
                raise sd.CallbackStop()

        self.logger.info(
            "Starting recording for %.2f seconds at %d Hz mono.",
            target_duration,
            self.sample_rate,
        )

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=blocksize,
                callback=callback,
            ):
                if not finished.wait(timeout=target_duration + 5.0):
                    raise TimeoutError("Audio recording timed out before completion.")
        finally:
            if progress_started:
                self._progress_logger.info("\n")

        audio = (
            np.concatenate(audio_chunks, axis=0).astype(np.float32, copy=False)
            if audio_chunks
            else np.empty(0, dtype=np.float32)
        )
        if audio.shape[0] > target_frames:
            audio = audio[:target_frames]

        quality_ok, message = self.is_quality_ok(audio)
        log_method = self.logger.info if quality_ok else self.logger.warning
        log_method(message)
        self.logger.info("Recording finished with %.2f seconds of audio.", audio.size / self.sample_rate)
        return audio

    def save_wav(self, audio: np.ndarray, filepath: str) -> None:
        """Save normalized audio data as a 16-bit PCM WAV file.

        Args:
            audio: Audio samples to persist.
            filepath: Destination path for the WAV file.

        Raises:
            ValueError: If the audio array is empty.
        """

        normalized_audio = self._normalize_audio(audio)
        if normalized_audio.size == 0:
            raise ValueError("Cannot save an empty audio array.")

        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pcm_audio = np.clip(
            np.round(normalized_audio * np.iinfo(np.int16).max),
            np.iinfo(np.int16).min,
            np.iinfo(np.int16).max,
        ).astype(np.int16)
        wavfile.write(output_path, self.sample_rate, pcm_audio)
        self.logger.info("Saved WAV file to %s", output_path)

    def get_volume_level(self, audio: np.ndarray) -> float:
        """Compute the RMS volume level for an audio array.

        Args:
            audio: Audio samples in integer PCM or normalized float form.

        Returns:
            The RMS volume level in normalized floating-point scale.
        """

        normalized_audio = self._normalize_audio(audio)
        if normalized_audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(normalized_audio), dtype=np.float64)))

    def is_quality_ok(self, audio: np.ndarray) -> tuple[bool, str]:
        """Validate the recorded audio against duration and loudness thresholds.

        Args:
            audio: Audio samples to validate.

        Returns:
            A `(is_ok, message)` tuple describing the validation result.
        """

        normalized_audio = self._normalize_audio(audio)
        duration_seconds = normalized_audio.size / self.sample_rate
        volume_level = self.get_volume_level(normalized_audio)
        issues: list[str] = []

        if normalized_audio.size == 0:
            issues.append("audio is empty")
        if duration_seconds < self.min_duration:
            issues.append(
                f"duration {duration_seconds:.2f}s is below minimum {self.min_duration:.2f}s"
            )
        if volume_level < self.low_volume_threshold:
            issues.append(
                "volume "
                f"{volume_level:.4f} is below threshold {self.low_volume_threshold:.4f}"
            )

        if issues:
            return False, f"Audio quality warning: {'; '.join(issues)}."
        return True, "Audio quality check passed."

    def _ensure_progress_handler(self) -> None:
        """Attach an inline handler once so the volume meter stays on one line."""

        if any(isinstance(handler, _InlineProgressHandler) for handler in self._progress_logger.handlers):
            return

        handler = _InlineProgressHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._progress_logger.addHandler(handler)

    def _render_volume_bar(
        self,
        level: float,
        captured_frames: int,
        target_frames: int,
    ) -> None:
        """Render an inline ASCII bar representing live input volume."""

        safe_threshold = max(self.low_volume_threshold * 4.0, 1e-6)
        scaled_level = min(level / safe_threshold, 1.0)
        filled = min(self.volume_bar_width, int(round(scaled_level * self.volume_bar_width)))
        bar = "#" * filled + "-" * (self.volume_bar_width - filled)
        progress = min(captured_frames / max(target_frames, 1), 1.0)
        self._progress_logger.info(
            "\rRecording %6.1f%% [%s] level=%.4f",
            progress * 100.0,
            bar,
            level,
        )

    def _normalize_audio(self, audio: np.ndarray) -> np.ndarray:
        """Convert audio arrays to one-dimensional normalized float32 samples."""

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
