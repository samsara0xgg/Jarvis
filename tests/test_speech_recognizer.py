"""Tests for Whisper-backed speech recognition helpers."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

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
