"""Tests for speech recognition (SenseVoice + Whisper backends)."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.speech_recognizer import SpeechRecognizer


class _FakeWhisperModel:
    """Fake Whisper model returning predefined transcription payloads."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        """Store queued payloads and a call log."""

        self._payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        audio: np.ndarray,
        language: str | None = None,
        fp16: bool = False,
        verbose: bool = False,
    ) -> dict[str, object]:
        """Record the call and return the next fake transcription payload."""

        self.calls.append(
            {
                "audio": np.array(audio, copy=True),
                "language": language,
                "fp16": fp16,
                "verbose": verbose,
            }
        )
        return self._payloads.pop(0)


def test_transcribe_loads_whisper_lazily_and_reuses_model() -> None:
    """The recognizer should lazy-load Whisper once and reuse the loaded model."""

    fake_model = _FakeWhisperModel(
        [
            {
                "text": "打开客厅灯",
                "language": "zh",
                "segments": [
                    {"avg_logprob": -0.1, "no_speech_prob": 0.1},
                ],
            },
            {
                "text": "退出",
                "language": "zh",
                "segments": [],
            },
        ]
    )
    load_calls: list[str] = []
    fake_whisper = SimpleNamespace(
        load_model=lambda model_size: load_calls.append(model_size) or fake_model
    )
    recognizer = SpeechRecognizer({"asr": {"model_size": "tiny", "language": "zh"}})

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "whisper", fake_whisper)

        stereo_pcm = np.array(
            [[0, 32767], [32767, 0], [12000, -12000]],
            dtype=np.int16,
        )
        first_result = recognizer.transcribe(stereo_pcm)
        second_result = recognizer.transcribe(np.ones(4, dtype=np.float32))

    assert load_calls == ["tiny"]
    assert first_result.text == "打开客厅灯"
    assert first_result.language == "zh"
    assert first_result.confidence == pytest.approx(np.exp(-0.1) * 0.9, rel=1e-4)
    assert second_result.text == "退出"
    assert second_result.confidence == 0.5
    assert len(fake_model.calls) == 2
    assert fake_model.calls[0]["audio"].dtype == np.float32
    assert fake_model.calls[0]["audio"].ndim == 1
    assert fake_model.calls[0]["language"] == "zh"


def test_transcribe_rejects_empty_audio() -> None:
    """Empty audio arrays should be rejected before model inference."""

    recognizer = SpeechRecognizer({"asr": {"model_size": "base"}})

    with pytest.raises(ValueError, match="empty audio array"):
        recognizer.transcribe(np.array([], dtype=np.float32))


def test_transcribe_raises_runtime_error_when_whisper_is_missing() -> None:
    """A missing Whisper dependency should surface as a descriptive runtime error."""

    recognizer = SpeechRecognizer({"asr": {"model_size": "base"}})
    original_import = builtins.__import__

    def _fake_import(
        name: str,
        globals_dict=None,
        locals_dict=None,
        fromlist=(),
        level: int = 0,
    ):
        if name == "whisper":
            raise ImportError("whisper not installed")
        return original_import(name, globals_dict, locals_dict, fromlist, level)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(builtins, "__import__", _fake_import)
        monkeypatch.delitem(sys.modules, "whisper", raising=False)

        with pytest.raises(RuntimeError, match="openai-whisper"):
            recognizer.transcribe(np.ones(8, dtype=np.float32))


# --- SenseVoice backend tests ---


class _FakeSherpaResult:
    """Fake sherpa-onnx result with lang/emotion/event attrs."""

    def __init__(self, text: str, lang: str = "<|zh|>", emotion: str = "<|NEUTRAL|>", event: str = "<|Speech|>") -> None:
        self.text = text
        self.lang = lang
        self.emotion = emotion
        self.event = event
        self.tokens = list(text)


class _FakeSherpaStream:
    """Fake sherpa-onnx OfflineStream."""

    def __init__(self, text: str, lang: str = "<|zh|>") -> None:
        self.result = _FakeSherpaResult(text, lang=lang)
        self._accepted = False

    def accept_waveform(self, sample_rate: int, audio: np.ndarray) -> None:
        self._accepted = True


class _FakeSherpaRecognizer:
    """Fake sherpa-onnx OfflineRecognizer."""

    def __init__(self, text: str = "打开卧室灯", lang: str = "<|zh|>") -> None:
        self._text = text
        self._lang = lang
        self.decode_count = 0

    def create_stream(self) -> _FakeSherpaStream:
        return _FakeSherpaStream(self._text, self._lang)

    def decode_stream(self, stream: _FakeSherpaStream) -> None:
        self.decode_count += 1


def test_sensevoice_primary_path() -> None:
    """SenseVoice should be used when provider=sensevoice."""
    config = {"asr": {"provider": "sensevoice", "language": "zh"}}
    recognizer = SpeechRecognizer(config)
    recognizer.provider = "sensevoice"

    fake_recognizer = _FakeSherpaRecognizer("开灯", "<|zh|>")
    recognizer._sherpa_recognizer = fake_recognizer

    # Use audio with sufficient RMS to pass silence filter
    audio = np.ones(16000, dtype=np.float32) * 0.5
    result = recognizer.transcribe(audio)
    assert result.text == "开灯"
    assert result.language == "zh"
    assert result.confidence == 0.9
    assert fake_recognizer.decode_count == 1


def test_sensevoice_detects_language() -> None:
    """SenseVoice should extract detected language from result.lang."""
    config = {"asr": {"provider": "sensevoice", "language": "zh"}}
    recognizer = SpeechRecognizer(config)
    recognizer.provider = "sensevoice"

    fake_recognizer = _FakeSherpaRecognizer("hello world", "<|en|>")
    recognizer._sherpa_recognizer = fake_recognizer

    audio = np.ones(16000, dtype=np.float32) * 0.5
    result = recognizer.transcribe(audio)
    assert result.language == "en"


def test_sensevoice_silence_returns_empty() -> None:
    """Silence audio should return empty text with low confidence."""
    config = {"asr": {"provider": "sensevoice", "language": "zh"}}
    recognizer = SpeechRecognizer(config)
    recognizer.provider = "sensevoice"

    fake_recognizer = _FakeSherpaRecognizer("嗯")
    recognizer._sherpa_recognizer = fake_recognizer

    silence = np.zeros(16000, dtype=np.float32)
    result = recognizer.transcribe(silence)
    assert result.text == ""
    assert result.confidence < 0.5


def test_sensevoice_fallback_to_whisper() -> None:
    """If SenseVoice fails, should fall back to Whisper when fallback_to_local=True."""
    config = {"asr": {"provider": "sensevoice", "fallback_to_local": True, "language": "zh"}}
    recognizer = SpeechRecognizer(config)
    recognizer.provider = "sensevoice"

    # Make SenseVoice fail
    recognizer._sherpa_recognizer = None
    def _fail(audio):
        raise RuntimeError("SenseVoice crashed")
    recognizer._transcribe_sensevoice = _fail

    # Set up fake Whisper
    fake_model = _FakeWhisperModel([{"text": "回退成功", "language": "zh", "segments": []}])
    fake_whisper = SimpleNamespace(load_model=lambda s: fake_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "whisper", fake_whisper)
        result = recognizer.transcribe(np.ones(16000, dtype=np.float32))

    assert result.text == "回退成功"


def test_sensevoice_no_fallback_raises() -> None:
    """If fallback_to_local=False, SenseVoice failure should propagate."""
    config = {"asr": {"provider": "sensevoice", "fallback_to_local": False}}
    recognizer = SpeechRecognizer(config)
    recognizer.provider = "sensevoice"

    def _fail(audio):
        raise RuntimeError("SenseVoice crashed")
    recognizer._transcribe_sensevoice = _fail

    with pytest.raises(RuntimeError, match="SenseVoice crashed"):
        recognizer.transcribe(np.ones(16000, dtype=np.float32))


def test_provider_local_skips_sensevoice() -> None:
    """Provider=local should use Whisper directly, never touch SenseVoice."""
    config = {"asr": {"provider": "local", "model_size": "tiny", "language": "zh"}}
    recognizer = SpeechRecognizer(config)
    assert recognizer.provider == "local"

    fake_model = _FakeWhisperModel([{"text": "直接Whisper", "language": "zh", "segments": []}])
    fake_whisper = SimpleNamespace(load_model=lambda s: fake_model)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "whisper", fake_whisper)
        result = recognizer.transcribe(np.ones(16000, dtype=np.float32))

    assert result.text == "直接Whisper"


def test_auto_degrade_when_model_missing(tmp_path) -> None:
    """If SenseVoice model dir doesn't exist, should auto-degrade to local."""
    config = {"asr": {"provider": "sensevoice", "sensevoice_model_dir": str(tmp_path / "nonexistent")}}
    recognizer = SpeechRecognizer(config)
    assert recognizer.provider == "local"
