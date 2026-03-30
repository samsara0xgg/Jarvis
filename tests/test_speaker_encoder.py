"""Tests for the SpeechBrain-based speaker encoder wrapper."""

from __future__ import annotations

from pathlib import Path
import sys
import types

import numpy as np
import yaml
from scipy.io import wavfile

from core.speaker_encoder import SpeakerEncoder


def _load_config() -> dict:
    """Load the project config for speaker encoder tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


class _FakeTensor:
    """Minimal tensor shim used to fake Torch tensors in tests."""

    def __init__(self, array: np.ndarray) -> None:
        """Store the wrapped NumPy array."""

        self.array = np.asarray(array, dtype=np.float32)

    def unsqueeze(self, axis: int) -> "_FakeTensor":
        """Expand the array along the given axis."""

        return _FakeTensor(np.expand_dims(self.array, axis=axis))

    def detach(self) -> "_FakeTensor":
        """Mimic Torch's detach behavior."""

        return self

    def cpu(self) -> "_FakeTensor":
        """Mimic Torch's cpu behavior."""

        return self

    def numpy(self) -> np.ndarray:
        """Return the underlying NumPy array."""

        return self.array


def _install_fake_speechbrain(monkeypatch) -> type:
    """Install fake SpeechBrain and Torch modules into `sys.modules`."""

    class FakeEncoderClassifier:
        """Fake classifier that emits deterministic embeddings."""

        load_calls = 0
        encode_calls = 0
        last_waveform_shape: tuple[int, ...] | None = None

        @classmethod
        def from_hparams(cls, source: str, savedir: str | None = None, run_opts: dict | None = None):
            """Capture model loading parameters and return a fake instance."""

            del source, savedir, run_opts
            cls.load_calls += 1
            return cls()

        def encode_batch(self, wavs: _FakeTensor, wav_lens: _FakeTensor | None = None) -> _FakeTensor:
            """Return a stable fake embedding while recording input shape."""

            del wav_lens
            type(self).encode_calls += 1
            type(self).last_waveform_shape = wavs.array.shape
            embedding = np.arange(192, dtype=np.float32).reshape(1, 1, 192)
            return _FakeTensor(embedding)

    torch_module = types.ModuleType("torch")
    torch_module.float32 = np.float32
    torch_module.from_numpy = lambda array: _FakeTensor(np.asarray(array, dtype=np.float32))
    torch_module.tensor = lambda data, dtype=None: _FakeTensor(np.asarray(data, dtype=np.float32))

    speechbrain_module = types.ModuleType("speechbrain")
    inference_module = types.ModuleType("speechbrain.inference")
    speaker_module = types.ModuleType("speechbrain.inference.speaker")
    speaker_module.EncoderClassifier = FakeEncoderClassifier

    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "speechbrain", speechbrain_module)
    monkeypatch.setitem(sys.modules, "speechbrain.inference", inference_module)
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", speaker_module)

    return FakeEncoderClassifier


def test_encode_returns_embedding_and_loads_model_lazily(monkeypatch) -> None:
    """The encoder should load the SpeechBrain model once and reuse it."""

    fake_classifier = _install_fake_speechbrain(monkeypatch)
    encoder = SpeakerEncoder(_load_config())
    audio = np.linspace(-0.5, 0.5, 16000, dtype=np.float32)

    embedding_one = encoder.encode(audio)
    embedding_two = encoder.encode(audio)

    assert embedding_one.shape == (192,)
    assert embedding_two.shape == (192,)
    np.testing.assert_allclose(embedding_one, np.arange(192, dtype=np.float32))
    assert fake_classifier.load_calls == 1
    assert fake_classifier.encode_calls == 2


def test_encode_file_reads_wav_and_resamples(monkeypatch, tmp_path: Path) -> None:
    """The file-based encoder should normalize and resample WAV input."""

    fake_classifier = _install_fake_speechbrain(monkeypatch)
    encoder = SpeakerEncoder(_load_config())
    duration_seconds = 1.0
    source_sample_rate = 8000
    sample_count = int(source_sample_rate * duration_seconds)
    time_axis = np.arange(sample_count, dtype=np.float32) / source_sample_rate
    waveform = 0.2 * np.sin(2.0 * np.pi * 220.0 * time_axis)
    output_path = tmp_path / "speaker.wav"
    wavfile.write(output_path, source_sample_rate, (waveform * np.iinfo(np.int16).max).astype(np.int16))

    embedding = encoder.encode_file(str(output_path))

    assert embedding.shape == (192,)
    assert fake_classifier.load_calls == 1
    assert fake_classifier.last_waveform_shape == (1, 16000)


def test_encoder_patches_missing_torchaudio_backend_helpers(monkeypatch) -> None:
    """The encoder should patch removed torchaudio helpers before SpeechBrain import."""

    fake_classifier = _install_fake_speechbrain(monkeypatch)
    torchaudio_module = types.ModuleType("torchaudio")
    torchaudio_module.__version__ = "2.11.0"
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio_module)

    encoder = SpeakerEncoder(_load_config())
    embedding = encoder.encode(np.ones(16000, dtype=np.float32))

    assert embedding.shape == (192,)
    assert hasattr(torchaudio_module, "list_audio_backends")
    assert hasattr(torchaudio_module, "set_audio_backend")
    assert torchaudio_module.list_audio_backends() == []
    assert fake_classifier.load_calls == 1
