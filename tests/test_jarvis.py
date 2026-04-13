"""Tests for the JarvisApp orchestration with all fakes."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.speaker_verifier import VerificationResult
from core.speech_recognizer import TranscriptionResult


def _make_config(tmp_path):
    return {
        "audio": {"sample_rate": 16000, "channels": 1, "default_duration": 1.0,
                   "min_duration": 0.1, "low_volume_threshold": 0.001},
        "asr": {"model_size": "base", "language": "zh"},
        "speaker": {"model_source": "test", "embedding_dim": 192, "device": "cpu"},
        "verification": {"threshold": 0.70},
        "enrollment": {"num_samples": 3, "default_role": "resident"},
        "auth": {"user_store_path": str(tmp_path / "users.json")},
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
        "tts": {"engine": "pyttsx3", "fallback_enabled": False},
        "wake_word": {"enabled": False},
        "session": {"silence_timeout": 30, "utterance_duration": 3},
        "memory": {
            "max_conversation_turns": 10,
            "conversation_dir": str(tmp_path / "convos"),
            "preferences_dir": str(tmp_path / "prefs"),
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
    """Test the full pipeline: audio → verify + ASR → Claude → response."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        # Mock the Claude response
        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("卧室灯已打开，先生。")])
        )

        # Mock speaker verification
        fake_verification = VerificationResult(
            verified=True, user="test_user", confidence=0.85, all_scores={"test_user": 0.85},
        )
        app.speaker_verifier.verify = MagicMock(return_value=fake_verification)

        # Mock ASR
        fake_transcription = TranscriptionResult(text="打开卧室灯", language="zh", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription)

        # Add a user to the store
        app.user_store.add_user({
            "user_id": "test_user",
            "name": "Tony",
            "embedding": [0.1] * 192,
            "role": "owner",
            "permissions": [],
            "enrolled_at": "2025-01-01T00:00:00Z",
        })

        # Mock the intent router to return a controlled smart_home result
        from core.intent_router import RouteResult
        mock_route = RouteResult(
            tier="local", intent="smart_home", confidence=0.95,
            duration_ms=10, provider="mock",
            actions=[{"device_id": "bedroom_light", "action": "turn_on", "value": None}],
            response="好的，卧室灯已打开。",
        )
        app.intent_router.route = MagicMock(return_value=mock_route)
        app.intent_router.route_and_respond = MagicMock(return_value=mock_route)

        # Run the pipeline
        audio = np.random.randn(16000).astype(np.float32)
        response = app.handle_utterance(audio)

        assert "开了" in response
        app.speaker_verifier.verify.assert_called_once()
        app.speech_recognizer.transcribe.assert_called_once()
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)


def test_jarvis_unidentified_speaker_gets_guest_access(tmp_path):
    """Unidentified speakers should still get a response but with guest role."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("I don't recognize you, but I can help.")])
        )

        fake_verification = VerificationResult(
            verified=False, user=None, confidence=0.3, all_scores={},
        )
        app.speaker_verifier.verify = MagicMock(return_value=fake_verification)

        fake_transcription = TranscriptionResult(text="what time is it", language="en", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription)

        audio = np.random.randn(16000).astype(np.float32)
        response = app.handle_utterance(audio)

        assert response  # Should still get a response
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)


def test_jarvis_skill_registry_has_all_skills(tmp_path):
    """Verify all expected skills are registered."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        expected_builtins = {"smart_home", "weather", "time", "reminders", "todos", "system_control", "memory", "automation", "health", "scheduler", "skill_mgmt", "model_switch"}
        actual_skills = set(app.skill_registry.skill_names)
        assert expected_builtins.issubset(actual_skills), f"Missing: {expected_builtins - actual_skills}"
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)


def test_jarvis_conversation_persists_across_calls(tmp_path):
    """Conversation history should accumulate across handle_utterance calls."""
    _install_fake_anthropic()
    _install_fake_pyttsx3()

    try:
        from jarvis import JarvisApp

        config = _make_config(tmp_path)
        app = JarvisApp(config, config_path=tmp_path / "config.yaml")

        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Response 1")])
        )

        fake_verification = VerificationResult(
            verified=True, user="user1", confidence=0.9, all_scores={"user1": 0.9},
        )
        app.speaker_verifier.verify = MagicMock(return_value=fake_verification)

        app.user_store.add_user({
            "user_id": "user1", "name": "Test", "embedding": [0.1] * 192,
            "role": "owner", "permissions": [], "enrolled_at": "2025-01-01",
        })

        fake_transcription = TranscriptionResult(text="hello", language="en", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription)

        audio = np.random.randn(16000).astype(np.float32)
        app.handle_utterance(audio)

        # Second call
        app.llm._get_anthropic_client().messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Response 2")])
        )
        fake_transcription2 = TranscriptionResult(text="follow up", language="en", confidence=0.9)
        app.speech_recognizer.transcribe = MagicMock(return_value=fake_transcription2)
        app.handle_utterance(audio)

        history = app.conversation_store.get_history("user1")
        assert len(history) >= 4  # 2 user + 2 assistant messages
    finally:
        sys.modules.pop("anthropic", None)
        sys.modules.pop("pyttsx3", None)
