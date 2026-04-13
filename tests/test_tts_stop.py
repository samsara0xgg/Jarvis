# tests/test_tts_stop.py
"""Tests for TTSEngine.stop() and interruptible playback."""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.tts import TTSEngine, TTSPipeline, SentenceType


def _make_config(**overrides):
    base = {
        "tts": {
            "engine": "pyttsx3",
            "fallback_enabled": False,
        }
    }
    base["tts"].update(overrides)
    return base


class TestTTSEngineStop:
    def test_stop_has_method(self):
        tts = TTSEngine(_make_config())
        assert hasattr(tts, "stop")
        assert callable(tts.stop)

    def test_stop_when_nothing_playing(self):
        tts = TTSEngine(_make_config())
        # Should not raise
        tts.stop()

    def test_play_audio_file_saves_proc_handle(self):
        tts = TTSEngine(_make_config())
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            tts._platform = "Darwin"
            tts._play_audio_file("/tmp/test.mp3")
            mock_popen.assert_called_once()
            # After playback, handle should be cleared
            assert tts._play_proc is None

    def test_stop_terminates_playing_process(self):
        tts = TTSEngine(_make_config())
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = 0
        tts._play_proc = mock_proc
        tts.stop()
        mock_proc.terminate.assert_called_once()

    def test_stop_is_thread_safe(self):
        tts = TTSEngine(_make_config())
        # Concurrent stop calls should not raise
        threads = [threading.Thread(target=tts.stop) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)


class TestTTSPipelineAbort:
    def test_abort_calls_engine_stop(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        pipeline = TTSPipeline(engine)
        pipeline.start()
        pipeline.abort()
        engine.stop.assert_called_once()
        pipeline.stop()

    def test_abort_returns_remaining_sentences(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        # Make synthesis block so items stay queued
        engine.synth_to_file = MagicMock(
            side_effect=lambda *a, **kw: time.sleep(10) or None,
        )
        pipeline = TTSPipeline(engine)
        pipeline.start()
        pipeline.submit("句子一", SentenceType.FIRST)
        pipeline.submit("句子二", SentenceType.MIDDLE)
        pipeline.submit("句子三", SentenceType.MIDDLE)
        # Abort immediately — at most one item dequeued by worker
        remaining = pipeline.abort()
        pipeline.stop()
        assert len(remaining) >= 2

    def test_abort_returns_empty_when_nothing_queued(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        pipeline = TTSPipeline(engine)
        pipeline.start()
        remaining = pipeline.abort()
        pipeline.stop()
        assert remaining == []
