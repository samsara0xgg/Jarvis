"""Tests for the JarvisApp orchestration with all fakes."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np

from core.speech_recognizer import TranscriptionResult


def _make_config(tmp_path):
    return {
        "audio": {"sample_rate": 16000, "channels": 1, "default_duration": 1.0,
                   "min_duration": 0.1, "low_volume_threshold": 0.001},
        "asr": {"model_size": "base", "language": "zh"},
        "devices": {
            "mode": "sim",
            "sim_devices": [
                {
                    "device_id": "bedroom_light",
                    "name": "卧室灯",
                    "device_type": "light",
                    "required_role": "guest",
                    "is_available": True,
                    "initial_state": {"is_on": False, "brightness": 100,
                                      "color_temp": "neutral", "color": "white"},
                },
            ],
        },
        "hue": {
            "light_aliases": {"bedroom_light": ["卧室灯"]},
            "group_aliases": {},
            "scene_aliases": {},
            "voice_shortcuts": {},
        },
        "llm": {"model": "test-model", "max_tokens": 256, "api_key": "test-key"},
        "tts": {"engine": "pyttsx3", "fallback_enabled": False, "minimax_ws": False},
        "wake_word": {"enabled": False},
        "startup": {"prewarm": False},
        "session": {"silence_timeout": 30, "utterance_duration": 3},
        "memory": {
            "max_conversation_turns": 10,
            "conversation_dir": str(tmp_path / "convos"),
            "preferences_dir": str(tmp_path / "prefs"),
            "db_path": str(tmp_path / "memory.db"),
            "observer": {"enabled": False},
            "outcome_detector": {"nli": {"enabled": False}},
        },
        "skills": {
            "weather": {"default_city": "Vancouver"},
            "reminders": {"path": str(tmp_path / "reminders.json")},
            "todos": {"dir": str(tmp_path / "todos")},
        },
        "logging": {"level": "WARNING"},
    }


class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, content: list):
        self.content = content


def _install_fake_anthropic():
    fake_module = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = MagicMock()

    fake_module.Anthropic = FakeAnthropic
    sys.modules["anthropic"] = fake_module
    return FakeAnthropic


def _install_fake_pyttsx3():
    fake_module = types.ModuleType("pyttsx3")
    mock_engine = MagicMock()
    fake_module.init = MagicMock(return_value=mock_engine)
    sys.modules["pyttsx3"] = fake_module
    return mock_engine


def test_jarvis_handle_utterance_end_to_end(tmp_path):
    """Test the full pipeline: audio → ASR → Claude → response."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    app = None
    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("卧室灯已打开，先生。")])
        )

        fake_transcription = TranscriptionResult(text="打开卧室灯", language="zh", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription)

        audio = np.random.randn(16000).astype(np.float32)
        response = app.handle_utterance(audio)

        assert "卧室灯" in response and "打开" in response
        app.speech_recognizer.transcribe.assert_called_once()
    finally:
        if app is not None:
            app.shutdown()
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)


def test_jarvis_tool_registry_has_tools(tmp_path):
    """Verify ToolRegistry has tools registered."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    app = None
    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        assert app.tool_registry.count() > 0, "ToolRegistry should have at least one tool"
        defs = app.tool_registry.get_tool_definitions(user_role="owner")
        tool_names = {d["name"] for d in defs}
        assert "smart_home_control" in tool_names, f"Missing smart_home_control in {tool_names}"
    finally:
        if app is not None:
            app.shutdown()
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)


def test_jarvis_conversation_persists_across_calls(tmp_path):
    """Conversation history should accumulate across handle_utterance calls."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    app = None
    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Response 1")])
        )

        fake_transcription = TranscriptionResult(text="hello", language="en", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription)

        audio = np.random.randn(16000).astype(np.float32)
        app.handle_utterance(audio)

        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Response 2")])
        )
        fake_transcription2 = TranscriptionResult(text="follow up", language="en", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription2)
        app.handle_utterance(audio)

        history = app.conversation_store.get_history("default_user")
        assert len(history) >= 4  # 2 user + 2 assistant messages
    finally:
        if app is not None:
            app.shutdown()
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)
