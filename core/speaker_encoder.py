"""Speaker embedding utilities built around SpeechBrain's ECAPA-TDNN model."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal
from scipy.io import wavfile

LOGGER = logging.getLogger(__name__)


class SpeakerEncoder:
    """Extract 192-dimensional speaker embeddings from mono audio waveforms.

    Args:
        config: Application configuration dictionary. The encoder reads the
            `speaker` section when present and falls back to related audio
            settings where appropriate.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the speaker encoder without loading the model yet.

        Args:
            config: Parsed application configuration.
        """

        speaker_config = config.get("speaker", config)
        audio_config = config.get("audio", {})

        self.model_source = str(
            speaker_config.get("model_source", "speechbrain/spkrec-ecapa-voxceleb")
        )
        self.sample_rate = int(speaker_config.get("sample_rate", audio_config.get("sample_rate", 16000)))
        self.embedding_dim = int(speaker_config.get("embedding_dim", 192))
        self.device = str(speaker_config.get("device", "cpu"))

        configured_cache_dir = speaker_config.get("cache_dir")
        self.cache_dir = Path(configured_cache_dir) if configured_cache_dir else None
        self.logger = LOGGER
        self._model: Any | None = None
        self._torch: Any | None = None

    def encode(self, audio: np.ndarray) -> np.ndarray:
        """Encode a waveform into a single speaker embedding vector.

        Args:
            audio: A one-dimensional mono waveform or an array that can be
                collapsed into mono.

        Returns:
            A normalized 192-dimensional embedding vector.

        Raises:
            RuntimeError: If SpeechBrain or Torch is unavailable.
            ValueError: If the provided audio is empty.
        """

        waveform = self._normalize_audio(audio)
        if waveform.size == 0:
            raise ValueError("Cannot encode an empty audio array.")

        model = self._load_model()
        torch_module = self._load_torch()
        waveform_tensor = torch_module.from_numpy(waveform.astype(np.float32, copy=False)).unsqueeze(0)
        wav_lens = torch_module.tensor([1.0], dtype=getattr(torch_module, "float32", None))

        try:
            embeddings = model.encode_batch(waveform_tensor, wav_lens=wav_lens)
        except TypeError:
            embeddings = model.encode_batch(waveform_tensor)

        embedding_array = self._to_numpy(embeddings).reshape(-1).astype(np.float32, copy=False)
        if embedding_array.size != self.embedding_dim:
            raise RuntimeError(
                f"Expected {self.embedding_dim} embedding dimensions, got {embedding_array.size}."
            )

        self.logger.info(
            "Generated speaker embedding with %d dimensions from in-memory audio.",
            embedding_array.size,
        )
        return embedding_array

    def encode_file(self, filepath: str) -> np.ndarray:
        """Load a WAV file and convert it into a speaker embedding.

        Args:
            filepath: Path to a WAV audio file on disk.

        Returns:
            A normalized 192-dimensional embedding vector.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file has an invalid sample rate or empty payload.
        """

        input_path = Path(filepath)
        if not input_path.exists():
            raise FileNotFoundError(f"Audio file not found: {input_path}")

        sample_rate, audio = wavfile.read(input_path)
        prepared_audio = self._prepare_audio(audio, sample_rate)
        self.logger.info("Loaded WAV file for speaker encoding: %s", input_path)
        return self.encode(prepared_audio)

    def _load_model(self) -> Any:
        """Load and cache the SpeechBrain encoder on first use."""

        if self._model is not None:
            return self._model

        encoder_classifier = self._load_encoder_classifier()
        self._load_torch()

        kwargs: dict[str, Any] = {
            "source": self.model_source,
            "run_opts": {"device": self.device},
        }
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            kwargs["savedir"] = str(self.cache_dir)

        self.logger.info("Loading SpeechBrain speaker encoder: %s", self.model_source)
        self._model = encoder_classifier.from_hparams(**kwargs)
        return self._model

    def _load_encoder_classifier(self) -> Any:
        """Import the SpeechBrain encoder classifier with compatibility fallbacks."""

        self._patch_torchaudio_compatibility()

        try:
            from speechbrain.inference.speaker import EncoderClassifier

            return EncoderClassifier
        except ImportError:
            pass

        try:
            from speechbrain.inference.classifiers import EncoderClassifier

            return EncoderClassifier
        except ImportError:
            pass

        try:
            from speechbrain.pretrained import EncoderClassifier

            return EncoderClassifier
        except ImportError as exc:
            raise RuntimeError(
                "SpeechBrain is required for speaker encoding but is not installed."
            ) from exc

    def _patch_torchaudio_compatibility(self) -> None:
        """Patch removed torchaudio backend helpers for SpeechBrain compatibility.

        Newer torchaudio releases removed global backend helpers such as
        `list_audio_backends` and `set_audio_backend`. Some SpeechBrain
        versions still import and call them during module initialization.
        Defining no-op shims keeps speaker model import working on those
        versions without affecting the main project flow.
        """

        try:
            import torchaudio
        except ImportError:
            return

        if not hasattr(torchaudio, "list_audio_backends"):
            self.logger.warning(
                "torchaudio.list_audio_backends is unavailable; applying "
                "SpeechBrain compatibility shim."
            )

            def _list_audio_backends() -> list[str]:
                return []

            setattr(torchaudio, "list_audio_backends", _list_audio_backends)

        if not hasattr(torchaudio, "set_audio_backend"):
            def _set_audio_backend(_backend: str | None) -> None:
                return None

            setattr(torchaudio, "set_audio_backend", _set_audio_backend)

    def _load_torch(self) -> Any:
        """Import and cache Torch only when the encoder is first needed."""

        if self._torch is not None:
            return self._torch

        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("Torch is required for speaker encoding but is not installed.") from exc

        self._torch = torch
        return self._torch

    def _prepare_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Normalize and resample audio data to the model sample rate."""

        if sample_rate <= 0:
            raise ValueError("Audio sample rate must be greater than zero.")

        normalized_audio = self._normalize_audio(audio)
        if normalized_audio.size == 0:
            raise ValueError("Audio file contains no samples.")

        if sample_rate == self.sample_rate:
            return normalized_audio

        greatest_common_divisor = math.gcd(sample_rate, self.sample_rate)
        upsample = self.sample_rate // greatest_common_divisor
        downsample = sample_rate // greatest_common_divisor
        resampled_audio = signal.resample_poly(normalized_audio, upsample, downsample)
        self.logger.info(
            "Resampled audio from %d Hz to %d Hz for speaker encoding.",
            sample_rate,
            self.sample_rate,
        )
        return np.asarray(resampled_audio, dtype=np.float32)

    def _normalize_audio(self, audio: np.ndarray) -> np.ndarray:
        """Convert audio arrays into a clipped mono float32 waveform."""

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

    def _to_numpy(self, tensor_like: Any) -> np.ndarray:
        """Convert Torch-like tensors or arrays into NumPy arrays."""

        value = tensor_like
        if hasattr(value, "detach"):
            value = value.detach()
        if hasattr(value, "cpu"):
            value = value.cpu()
        if hasattr(value, "numpy"):
            value = value.numpy()
        return np.asarray(value, dtype=np.float32)
