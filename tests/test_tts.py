"""Tests for the TTS engine with mocked backends."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from core.tts import TTSEngine


def _make_config(engine="edge-tts"):
    return {
        "tts": {
            "engine": engine,
            "edge_voice": "zh-CN-YunxiNeural",
            "edge_rate": "+0%",
            "fallback_enabled": True,
        }
    }


def test_speak_short_uses_pyttsx3():
    fake_pyttsx3 = types.ModuleType("pyttsx3")
    mock_engine = MagicMock()
    fake_pyttsx3.init = MagicMock(return_value=mock_engine)
    sys.modules["pyttsx3"] = fake_pyttsx3

    try:
        tts = TTSEngine(_make_config())
        tts.speak_short("Hello")
        mock_engine.say.assert_called_once_with("Hello")
        mock_engine.runAndWait.assert_called_once()
    finally:
        sys.modules.pop("pyttsx3", None)


def test_pyttsx3_mode_uses_pyttsx3_directly():
    fake_pyttsx3 = types.ModuleType("pyttsx3")
    mock_engine = MagicMock()
    fake_pyttsx3.init = MagicMock(return_value=mock_engine)
    sys.modules["pyttsx3"] = fake_pyttsx3

    try:
        tts = TTSEngine(_make_config(engine="pyttsx3"))
        tts.speak("Test message")
        mock_engine.say.assert_called_once_with("Test message")
    finally:
        sys.modules.pop("pyttsx3", None)


def test_speak_empty_string_is_noop():
    tts = TTSEngine(_make_config())
    # Should not raise
    tts.speak("")
    tts.speak("   ")


def test_edge_tts_fallback_to_pyttsx3_on_failure():
    fake_pyttsx3 = types.ModuleType("pyttsx3")
    mock_engine = MagicMock()
    fake_pyttsx3.init = MagicMock(return_value=mock_engine)
    sys.modules["pyttsx3"] = fake_pyttsx3

    try:
        tts = TTSEngine(_make_config())
        # Force edge TTS to fail by making import succeed but method fail
        with patch.object(tts, "_speak_edge", side_effect=RuntimeError("network down")):
            tts.speak("Fallback test")
        mock_engine.say.assert_called_once_with("Fallback test")
    finally:
        sys.modules.pop("pyttsx3", None)
