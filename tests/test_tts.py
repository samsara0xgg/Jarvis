"""Tests for the TTS engine with mocked backends."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from core.tts import TTSEngine, _EMOTION_TO_STYLE


def _make_config(engine="edge-tts", **tts_overrides):
    base = {
        "tts": {
            "engine": engine,
            "edge_voice": "zh-CN-YunxiNeural",
            "edge_rate": "+0%",
            "fallback_enabled": True,
            "azure_key": "",
            "azure_region": "canadacentral",
            "azure_voice": "zh-CN-XiaoxiaoNeural",
        }
    }
    base["tts"].update(tts_overrides)
    return base


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


# --- Emotion mapping tests ---


class TestEmotionMapping:
    def test_all_sensevoice_emotions_have_mapping(self):
        """Every SenseVoice emotion should map to an Azure style."""
        sensevoice_emotions = ["HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED", "EMO_UNKNOWN"]
        for emo in sensevoice_emotions:
            assert emo in _EMOTION_TO_STYLE, f"Missing mapping for {emo}"

    def test_happy_maps_to_cheerful(self):
        assert _EMOTION_TO_STYLE["HAPPY"] == "cheerful"

    def test_unknown_emotion_defaults_to_chat(self):
        assert _EMOTION_TO_STYLE.get("NONEXISTENT", "chat") == "chat"


# --- Azure TTS tests ---


class TestAzureTTS:
    def test_azure_config_reads_correctly(self):
        config = _make_config(engine="azure", azure_key="test-key", azure_region="eastus")
        tts = TTSEngine(config)
        assert tts.engine_name == "azure"
        assert tts.azure_key == "test-key"
        assert tts.azure_region == "eastus"
        assert tts.azure_voice == "zh-CN-XiaoxiaoNeural"

    def test_azure_reads_env_key(self):
        with patch.dict("os.environ", {"AZURE_SPEECH_KEY": "env-key"}):
            config = _make_config(engine="azure")
            tts = TTSEngine(config)
            assert tts.azure_key == "env-key"

    def test_azure_no_key_raises_on_speak(self):
        """Azure TTS without key should raise, then fallback to edge-tts."""
        config = _make_config(engine="azure", azure_key="")
        tts = TTSEngine(config)

        # _speak_azure should raise, but speak() catches and falls through
        with patch.object(tts, "_speak_edge") as mock_edge:
            tts.speak("test")
            mock_edge.assert_called_once_with("test")

    def test_azure_speak_builds_ssml_with_emotion(self):
        """Azure TTS should build SSML with the correct style from emotion."""
        import azure.cognitiveservices.speech as speechsdk

        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        mock_result = MagicMock()
        mock_result.reason = speechsdk.ResultReason.SynthesizingAudioCompleted

        mock_synthesizer = MagicMock()
        mock_synthesizer.speak_ssml_async.return_value.get.return_value = mock_result

        with patch("azure.cognitiveservices.speech.SpeechConfig"), \
             patch("azure.cognitiveservices.speech.audio.AudioOutputConfig"), \
             patch("azure.cognitiveservices.speech.SpeechSynthesizer", return_value=mock_synthesizer), \
             patch.object(tts, "_play_audio_file"):
            tts._speak_azure("你好", emotion="HAPPY")

        ssml_arg = mock_synthesizer.speak_ssml_async.call_args[0][0]
        assert 'style="cheerful"' in ssml_arg
        assert "你好" in ssml_arg
        assert "XiaoxiaoNeural" in ssml_arg

    def test_azure_fallback_to_edge_on_failure(self):
        """If Azure TTS fails, should fall back to edge-tts."""
        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        with patch.object(tts, "_speak_azure", side_effect=RuntimeError("Azure down")):
            with patch.object(tts, "_speak_edge") as mock_edge:
                tts.speak("fallback test", emotion="HAPPY")
                mock_edge.assert_called_once_with("fallback test")

    def test_speak_passes_emotion_to_azure(self):
        """speak(text, emotion=...) should forward emotion to _speak_azure."""
        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        with patch.object(tts, "_speak_azure") as mock_azure:
            tts.speak("test", emotion="SAD")
            mock_azure.assert_called_once_with("test", "SAD")

    def test_speak_without_emotion_defaults_empty(self):
        """speak(text) without emotion should still work."""
        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        with patch.object(tts, "_speak_azure") as mock_azure:
            tts.speak("test")
            mock_azure.assert_called_once_with("test", "")


# --- Emotion pipeline integration ---


class TestEmotionPipeline:
    def test_transcription_result_carries_emotion(self):
        from core.speech_recognizer import TranscriptionResult
        r = TranscriptionResult(text="你好", language="zh", confidence=0.9, emotion="HAPPY", event="Speech")
        assert r.emotion == "HAPPY"
        assert r.event == "Speech"

    def test_transcription_result_emotion_defaults_empty(self):
        from core.speech_recognizer import TranscriptionResult
        r = TranscriptionResult(text="hello", language="en", confidence=0.8)
        assert r.emotion == ""
        assert r.event == ""
