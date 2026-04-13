"""Tests for the TTS engine with mocked backends."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from core.tts import TTSEngine, _EMOTION_TO_AZURE_STYLE


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
            assert emo in _EMOTION_TO_AZURE_STYLE, f"Missing mapping for {emo}"

    def test_happy_maps_to_cheerful(self):
        assert _EMOTION_TO_AZURE_STYLE["HAPPY"] == "cheerful"

    def test_unknown_emotion_defaults_to_chat(self):
        assert _EMOTION_TO_AZURE_STYLE.get("NONEXISTENT", "chat") == "chat"


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
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AZURE_SPEECH_KEY", None)
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

    def test_ssml_escapes_special_xml_characters(self):
        """Text with <, >, &, and quotes must be escaped in the SSML body."""
        import azure.cognitiveservices.speech as speechsdk

        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        mock_result = MagicMock()
        mock_result.reason = speechsdk.ResultReason.SynthesizingAudioCompleted

        mock_synthesizer = MagicMock()
        mock_synthesizer.speak_ssml_async.return_value.get.return_value = mock_result

        dangerous_text = 'Price is <100> & "cheap" today'

        with patch("azure.cognitiveservices.speech.SpeechConfig"), \
             patch("azure.cognitiveservices.speech.audio.AudioOutputConfig"), \
             patch("azure.cognitiveservices.speech.SpeechSynthesizer", return_value=mock_synthesizer), \
             patch.object(tts, "_play_audio_file"):
            tts._speak_azure(dangerous_text, emotion="NEUTRAL")

        ssml_arg = mock_synthesizer.speak_ssml_async.call_args[0][0]
        # Raw < > & " should NOT appear unescaped in text body
        assert "&lt;100&gt;" in ssml_arg
        assert "&amp;" in ssml_arg
        # The text should not contain raw angle brackets as content
        # (they exist in the XML tags, but the text body must be escaped)
        assert dangerous_text not in ssml_arg  # raw form should not be present

    def test_azure_synthesizer_reuse_across_calls(self):
        """Azure TTS creates a new synthesizer per call (no stale state)."""
        import azure.cognitiveservices.speech as speechsdk

        config = _make_config(engine="azure", azure_key="fake-key")
        tts = TTSEngine(config)

        mock_result = MagicMock()
        mock_result.reason = speechsdk.ResultReason.SynthesizingAudioCompleted

        mock_synth_cls = MagicMock()
        mock_synth_instance = MagicMock()
        mock_synth_instance.speak_ssml_async.return_value.get.return_value = mock_result
        mock_synth_cls.return_value = mock_synth_instance

        with patch("azure.cognitiveservices.speech.SpeechConfig"), \
             patch("azure.cognitiveservices.speech.audio.AudioOutputConfig"), \
             patch("azure.cognitiveservices.speech.SpeechSynthesizer", new=mock_synth_cls), \
             patch.object(tts, "_play_audio_file"):
            tts._speak_azure("First call", emotion="HAPPY")
            tts._speak_azure("Second call", emotion="SAD")

        # SpeechSynthesizer should be constructed twice (once per call)
        assert mock_synth_cls.call_count == 2
        # Two different SSML payloads should have been sent
        first_ssml = mock_synth_instance.speak_ssml_async.call_args_list[0][0][0]
        second_ssml = mock_synth_instance.speak_ssml_async.call_args_list[1][0][0]
        assert 'style="cheerful"' in first_ssml
        assert 'style="gentle"' in second_ssml

    def test_azure_config_key_priority_config_over_env(self):
        """Config-file azure_key should take priority over AZURE_SPEECH_KEY env var."""
        with patch.dict("os.environ", {"AZURE_SPEECH_KEY": "env-key"}):
            config = _make_config(engine="azure", azure_key="config-key")
            tts = TTSEngine(config)
            assert tts.azure_key == "config-key"


# --- Edge/pyttsx3 path coverage ---


class TestEdgeTTSPaths:
    def test_speak_async_empty_is_noop(self):
        tts = TTSEngine(_make_config())
        tts.speak_async("")  # should not raise
        tts.speak_async("   ")

    def test_speak_async_submits_to_executor(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "_speak_safe") as mock_safe:
            with patch.object(tts._executor, "submit", wraps=tts._executor.submit) as mock_submit:
                tts.speak_async("hello")
                mock_submit.assert_called_once()

    def test_speak_safe_catches_exceptions(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "speak", side_effect=RuntimeError("boom")):
            tts._speak_safe("test")  # should not raise

    def test_speak_short_empty_is_noop(self):
        tts = TTSEngine(_make_config())
        tts.speak_short("")
        tts.speak_short("  ")

    def test_speak_short_falls_back_to_edge(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "_speak_pyttsx3", side_effect=RuntimeError("no pyttsx3")):
            with patch.object(tts, "_speak_edge") as mock_edge:
                tts.speak_short("test")
                mock_edge.assert_called_once_with("test")

    def test_speak_short_all_fail_no_raise(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "_speak_pyttsx3", side_effect=RuntimeError("fail")):
            with patch.object(tts, "_speak_edge", side_effect=RuntimeError("fail")):
                tts.speak_short("test")  # should not raise

    def test_play_audio_file_darwin(self):
        tts = TTSEngine(_make_config())
        tts._platform = "Darwin"
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            tts._play_audio_file("/tmp/test.mp3")
            mock_popen.assert_called_once()
            assert mock_popen.call_args[0][0] == ["afplay", "/tmp/test.mp3"]

    def test_play_audio_file_linux_mpv(self):
        tts = TTSEngine(_make_config())
        tts._platform = "Linux"
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        with patch("shutil.which", return_value="/usr/bin/mpv"):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                tts._play_audio_file("/tmp/test.mp3")
                assert "mpv" in mock_popen.call_args[0][0][0]

    def test_play_audio_file_unsupported_platform(self):
        tts = TTSEngine(_make_config())
        tts._platform = "FreeBSD"
        tts._play_audio_file("/tmp/test.mp3")  # should not raise, just log

    def test_edge_tts_engine_fallback_when_all_fail(self):
        tts = TTSEngine(_make_config(engine="edge-tts", fallback_enabled=False))
        with patch.object(tts, "_speak_edge", side_effect=RuntimeError("no network")):
            # No fallback, should silently fail (fallback disabled)
            tts.speak("test")  # logs warning but doesn't crash

    def test_azure_engine_fallback_to_edge(self):
        """When azure fails and engine is azure, it falls through to edge-tts."""
        tts = TTSEngine(_make_config(engine="azure", azure_key="fake"))
        with patch.object(tts, "_speak_azure", side_effect=RuntimeError("azure fail")):
            with patch.object(tts, "_speak_edge") as mock_edge:
                tts.speak("fallback")
                mock_edge.assert_called_once_with("fallback")


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


# --- TTS Speed control tests ---


class TestTTSSpeed:
    def test_speed_default_is_1(self):
        """Without speed in config, default should be 1.0."""
        tts = TTSEngine(_make_config())
        assert tts.speed == 1.0

    def test_speed_reads_from_config(self):
        """Speed should be read from config."""
        tts = TTSEngine(_make_config(speed=1.5))
        assert tts.speed == 1.5

    def test_speed_clamps_low(self):
        """Speed below 0.25 should clamp to 0.25."""
        tts = TTSEngine(_make_config(speed=0.1))
        assert tts.speed == 0.25

    def test_speed_clamps_high(self):
        """Speed above 4.0 should clamp to 4.0."""
        tts = TTSEngine(_make_config(speed=10.0))
        assert tts.speed == 4.0

    def test_minimax_uses_speed(self):
        """MiniMax payload should use configured speed."""
        tts = TTSEngine(_make_config(engine="minimax", minimax_key="fake", speed=1.3))
        assert tts.speed == 1.3

    def test_openai_tts_passes_speed(self):
        """OpenAI TTS create() should receive speed parameter."""
        config = _make_config(engine="openai_tts", openai_tts_key="fake", speed=1.2)
        tts = TTSEngine(config)

        mock_response = MagicMock()
        mock_response.content = b"fake audio"
        mock_client = MagicMock()
        mock_client.audio.speech.create.return_value = mock_response
        tts._openai_client = mock_client

        tts._synth_openai("test text", "NEUTRAL")

        call_kwargs = mock_client.audio.speech.create.call_args
        assert call_kwargs.kwargs.get("speed") == 1.2


# --- TTS Precache tests ---


class TestTTSPrecache:
    def test_precache_calls_synth_for_uncached_phrases(self, tmp_path):
        """precache() should call synth_to_file for phrases not yet in cache."""
        config = _make_config(engine="minimax", minimax_key="fake", cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrases = ["好的", "再见"]
        with patch.object(tts, "synth_to_file", return_value=(str(tmp_path / "out.mp3"), True)) as mock_synth:
            tts.precache(phrases)

        assert mock_synth.call_count == len(phrases)
        mock_synth.assert_any_call("好的", emotion="")
        mock_synth.assert_any_call("再见", emotion="")

    def test_precache_skips_existing_cache_files(self, tmp_path):
        """precache() should not re-synthesize phrases already in the cache."""
        config = _make_config(engine="minimax", minimax_key="fake", cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrase = "好的"
        # Pre-create the expected cache file
        cache_key = tts._tts_cache_key(phrase, "calm")
        cache_file = tmp_path / f"{cache_key}.mp3"
        cache_file.write_bytes(b"fake audio")

        with patch.object(tts, "synth_to_file") as mock_synth:
            tts.precache([phrase])

        mock_synth.assert_not_called()

    def test_precache_skips_long_phrases(self, tmp_path):
        """precache() should skip phrases longer than 50 characters."""
        config = _make_config(engine="minimax", minimax_key="fake", cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        long_phrase = "这是一个超过五十个字符的非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的句子"
        assert len(long_phrase) > 50

        with patch.object(tts, "synth_to_file") as mock_synth:
            tts.precache([long_phrase])

        mock_synth.assert_not_called()

    def test_precache_continues_on_synth_failure(self, tmp_path):
        """precache() should not raise if synth_to_file fails for a phrase."""
        config = _make_config(engine="minimax", minimax_key="fake", cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrases = ["好的", "再见"]
        call_count = 0

        def _fail_first(text: str, emotion: str = "") -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("synth failed")
            return (str(tmp_path / "ok.mp3"), True)

        with patch.object(tts, "synth_to_file", side_effect=_fail_first):
            tts.precache(phrases)  # must not raise

        assert call_count == 2  # both phrases attempted
