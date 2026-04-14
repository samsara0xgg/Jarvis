"""Tests for interrupt resume and keyword stripping."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest


class TestInterruptResume:
    """Test the _interrupted_response mechanism."""

    def _make_app(self, tmp_path):
        """Create a minimal JarvisApp with mocks."""
        from tests.test_jarvis import _make_config
        config = _make_config(tmp_path)
        with patch("core.speaker_encoder.SpeakerEncoder"), \
             patch("core.speaker_verifier.SpeakerVerifier"), \
             patch("core.speech_recognizer.SpeechRecognizer"), \
             patch("core.audio_recorder.AudioRecorder"), \
             patch("core.llm.LLMClient"), \
             patch("devices.device_manager.DeviceManager"):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=tmp_path / "config.yaml")
            return app

    def test_interrupted_response_initially_none(self, tmp_path):
        app = self._make_app(tmp_path)
        assert app._interrupted_response is None


class TestKeywordStripping:
    """Test interrupt keyword prefix removal."""

    def test_strip_interrupt_keyword_prefix(self):
        from core.interrupt_monitor import strip_interrupt_prefix
        assert strip_interrupt_prefix("停，改成多伦多的天气") == "改成多伦多的天气"
        assert strip_interrupt_prefix("等一下帮我查下明天") == "帮我查下明天"
        assert strip_interrupt_prefix("停") == ""
        assert strip_interrupt_prefix("明天天气怎么样") == "明天天气怎么样"

    def test_strip_handles_whitespace_and_punctuation(self):
        from core.interrupt_monitor import strip_interrupt_prefix
        assert strip_interrupt_prefix("停 改成多伦多") == "改成多伦多"
        assert strip_interrupt_prefix("等一下，查下天气") == "查下天气"
        assert strip_interrupt_prefix("打住。我要说的是") == "我要说的是"


class TestInterruptDuringTTS:
    def _make_app(self, tmp_path):
        from tests.test_jarvis import _make_config
        config = _make_config(tmp_path)
        config["interrupt"] = {"enabled": True}
        with patch("core.speaker_encoder.SpeakerEncoder"), \
             patch("core.speaker_verifier.SpeakerVerifier"), \
             patch("core.speech_recognizer.SpeechRecognizer"), \
             patch("core.audio_recorder.AudioRecorder"), \
             patch("core.llm.LLMClient"), \
             patch("devices.device_manager.DeviceManager"):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=tmp_path / "config.yaml")
            return app

    def test_app_has_interrupt_monitor(self, tmp_path):
        app = self._make_app(tmp_path)
        assert hasattr(app, "interrupt_monitor")
        from core.interrupt_monitor import InterruptMonitor
        assert isinstance(app.interrupt_monitor, InterruptMonitor)
