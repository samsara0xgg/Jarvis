"""Tests for TTSEngine soft-stop API (WP7): suspend_playback / resume_playback."""

from __future__ import annotations

import platform
from unittest.mock import MagicMock, patch

import pytest

from core.tts import TTSEngine


@pytest.fixture
def engine_with_proc():
    """A bare-bones TTSEngine with a fake live playback process."""
    with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
        e = TTSEngine.__new__(TTSEngine)
        e._play_proc = MagicMock()
        e._play_proc.poll.return_value = None  # process is alive
        e._play_proc.pid = 12345
        e._play_lock = __import__("threading").Lock()
        e._paused = False
        e._platform = "Darwin"
        e._stream_player = None
        e.logger = MagicMock()
    return e


class TestSuspendResume:
    def test_suspend_sends_sigstop(self, engine_with_proc):
        with patch("core.tts.os.kill") as mock_kill:
            assert engine_with_proc.suspend_playback() is True
            mock_kill.assert_called_once()
            args, _ = mock_kill.call_args
            assert args[0] == 12345
        assert engine_with_proc.is_paused() is True

    def test_double_suspend_is_idempotent(self, engine_with_proc):
        with patch("core.tts.os.kill"):
            engine_with_proc.suspend_playback()
            assert engine_with_proc.suspend_playback() is False  # already paused

    def test_resume_sends_sigcont(self, engine_with_proc):
        engine_with_proc._paused = True
        with patch("core.tts.os.kill") as mock_kill:
            assert engine_with_proc.resume_playback() is True
            mock_kill.assert_called_once()
        assert engine_with_proc.is_paused() is False

    def test_resume_when_not_paused_noops(self, engine_with_proc):
        with patch("core.tts.os.kill") as mock_kill:
            assert engine_with_proc.resume_playback() is False
            mock_kill.assert_not_called()

    def test_suspend_no_proc_returns_false(self, engine_with_proc):
        engine_with_proc._play_proc = None
        assert engine_with_proc.suspend_playback() is False

    def test_suspend_dead_proc_returns_false(self, engine_with_proc):
        engine_with_proc._play_proc.poll.return_value = 0  # exited
        assert engine_with_proc.suspend_playback() is False

    def test_windows_skips_kill(self, engine_with_proc):
        engine_with_proc._platform = "Windows"
        with patch("core.tts.os.kill") as mock_kill:
            assert engine_with_proc.suspend_playback() is False
            mock_kill.assert_not_called()

    def test_stop_wakes_paused_process_before_terminating(self, engine_with_proc):
        engine_with_proc._paused = True
        engine_with_proc._play_proc.wait.return_value = 0
        with patch("core.tts.os.kill") as mock_kill:
            engine_with_proc.stop()
            # SIGCONT must precede terminate so the kill signal can be delivered.
            mock_kill.assert_called_once()
            engine_with_proc._play_proc.terminate.assert_called_once()
        assert engine_with_proc._paused is False
