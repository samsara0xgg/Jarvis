"""Tests for the MiniMax-only TTS engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.tts import TTSEngine


def _make_config(**tts_overrides):
    base = {
        "tts": {
            "minimax_key": "fake-primary-key",
            "minimax_fallback_key": "",
            "minimax_voice": "Chinese (Mandarin)_ExplorativeGirl",
            "minimax_model": "speech-2.8-turbo",
        }
    }
    base["tts"].update(tts_overrides)
    return base


def test_speak_empty_string_is_noop():
    tts = TTSEngine(_make_config())
    tts.speak("")
    tts.speak("   ")


# --- TTS Speed control tests ---


class TestTTSSpeed:
    def test_speed_default_is_1(self):
        tts = TTSEngine(_make_config())
        assert tts.speed == 1.0

    def test_speed_reads_from_config(self):
        tts = TTSEngine(_make_config(speed=1.5))
        assert tts.speed == 1.5

    def test_speed_clamps_low(self):
        tts = TTSEngine(_make_config(speed=0.1))
        assert tts.speed == 0.25

    def test_speed_clamps_high(self):
        tts = TTSEngine(_make_config(speed=10.0))
        assert tts.speed == 4.0


# --- TTS Precache tests ---


class TestTTSPrecache:
    def test_precache_calls_synth_for_uncached_phrases(self, tmp_path):
        config = _make_config(cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrases = ["好的", "再见"]
        with patch.object(tts, "synth_to_file", return_value=(str(tmp_path / "out.pcm"), True)) as mock_synth:
            tts.precache(phrases)

        assert mock_synth.call_count == len(phrases)
        mock_synth.assert_any_call("好的", emotion="")
        mock_synth.assert_any_call("再见", emotion="")

    def test_precache_skips_existing_cache_files(self, tmp_path):
        config = _make_config(cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrase = "好的"
        cache_key = tts._tts_cache_key(phrase, "calm")
        cache_file = tmp_path / f"{cache_key}.pcm"
        cache_file.write_bytes(b"fake audio")

        with patch.object(tts, "synth_to_file") as mock_synth:
            tts.precache([phrase])

        mock_synth.assert_not_called()

    def test_precache_skips_long_phrases(self, tmp_path):
        config = _make_config(cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        long_phrase = "这是一个超过五十个字符的非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的句子"
        assert len(long_phrase) > 50

        with patch.object(tts, "synth_to_file") as mock_synth:
            tts.precache([long_phrase])

        mock_synth.assert_not_called()

    def test_precache_continues_on_synth_failure(self, tmp_path):
        config = _make_config(cache_dir=str(tmp_path))
        tts = TTSEngine(config)

        phrases = ["好的", "再见"]
        call_count = 0

        def _fail_first(text: str, emotion: str = "") -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("synth failed")
            return (str(tmp_path / "ok.pcm"), True)

        with patch.object(tts, "synth_to_file", side_effect=_fail_first):
            tts.precache(phrases)

        assert call_count == 2


# --- Async speak path ---


class TestSpeakAsync:
    def test_speak_async_empty_is_noop(self):
        tts = TTSEngine(_make_config())
        tts.speak_async("")
        tts.speak_async("   ")

    def test_speak_async_submits_to_executor(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "_speak_safe"):
            with patch.object(tts._executor, "submit", wraps=tts._executor.submit) as mock_submit:
                tts.speak_async("hello")
                mock_submit.assert_called_once()

    def test_speak_safe_catches_exceptions(self):
        tts = TTSEngine(_make_config())
        with patch.object(tts, "speak", side_effect=RuntimeError("boom")):
            tts._speak_safe("test")  # should not raise


# --- MiniMax fallback path ---


class TestMiniMaxFallback:
    def test_fallback_invoked_when_primary_ws_fails(self, tmp_path):
        config = _make_config(
            cache_dir=str(tmp_path),
            minimax_fallback_key="fake-fallback-key",
        )
        tts = TTSEngine(config)

        # Force WS path to raise; verify HTTP fallback called with .chat URL/key.
        with patch("core.tts.asyncio.run", side_effect=RuntimeError("WS .io down")), \
             patch.object(tts, "_synth_minimax_http", return_value=b"\x00" * 32000) as mock_http:
            path, deletable = tts._synth_minimax("你好", "")
            mock_http.assert_called_once()
            args, _ = mock_http.call_args
            assert args[0] == "https://api.minimax.chat/v1/t2a_v2"
            assert args[1] == "fake-fallback-key"
            assert path.endswith(".pcm")

    def test_no_fallback_key_propagates_primary_error(self, tmp_path):
        config = _make_config(cache_dir=str(tmp_path), minimax_fallback_key="")
        tts = TTSEngine(config)

        with patch("core.tts.asyncio.run", side_effect=RuntimeError("WS down")):
            try:
                tts._synth_minimax("你好", "")
            except RuntimeError as exc:
                assert "WS down" in str(exc)
            else:
                raise AssertionError("expected RuntimeError to propagate")


# --- Emotion pipeline integration (TranscriptionResult dataclass) ---


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


# --- _play_audio_file platform paths ---


class TestPlayAudioFile:
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
