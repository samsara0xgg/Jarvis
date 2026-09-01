"""Threaded integration regressions for duck/restore and TTS shutdown ownership."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.surface import voice_ducking, voice_tts


def _mock_player() -> MagicMock:
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    return player


def test_tts_waits_until_restore_subprocess_finishes() -> None:
    """A TTS provider cannot start while the OS restore call is blocked."""
    reset_realtime_trace()
    restore_entered = threading.Event()
    release_restore = threading.Event()
    provider_started = threading.Event()

    def _osascript(script: str) -> str:
        if 'return "output volume:"' in script:
            return "output volume:37, output muted:false"
        restore_entered.set()
        assert release_restore.wait(timeout=2.0)
        return ""

    async def _synthesize(_text: str) -> bytes:
        provider_started.set()
        await asyncio.sleep(0)
        return b"\x00" * 960

    ducker = voice_ducking.SystemAudioDucker()
    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _synthesize
    player = _mock_player()
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: None,
        ducker=ducker,
    )

    with (
        patch.object(voice_ducking, "_available", return_value=True),
        patch.object(voice_ducking, "_run_osascript", side_effect=_osascript),
    ):
        assert ducker.duck()
        restore_thread = threading.Thread(target=ducker.restore)
        restore_thread.start()
        assert restore_entered.wait(timeout=1.0)

        pipeline.begin_turn("T-restore-barrier", gate_mode="sentence")
        tts_thread = threading.Thread(
            target=pipeline.handle_chunk,
            args=("T-restore-barrier", "<voice>恢复后再说</voice>"),
        )
        tts_thread.start()
        assert not provider_started.wait(timeout=0.1)

        release_restore.set()
        restore_thread.join(timeout=1.0)
        assert provider_started.wait(timeout=1.0)
        tts_thread.join(timeout=1.0)

    assert not restore_thread.is_alive()
    assert not tts_thread.is_alive()
    assert ducker.restore_state == "ready"
    player.write.assert_called_once()


def test_restore_failure_keeps_tts_fail_closed() -> None:
    """A failed OS restore cannot be published as output-ready."""
    reset_realtime_trace()
    failure_message = "restore failed"

    def _osascript(script: str) -> str:
        if 'return "output volume:"' in script:
            return "output volume:52, output muted:false"
        raise RuntimeError(failure_message)

    ducker = voice_ducking.SystemAudioDucker()
    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    player = _mock_player()
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: None,
        ducker=ducker,
    )

    with (
        patch.object(voice_ducking, "_available", return_value=True),
        patch.object(voice_ducking, "_run_osascript", side_effect=_osascript),
    ):
        assert ducker.duck()
        ducker.restore()
        assert ducker.restore_state == "failed"
        pipeline.begin_turn("T-restore-failed", gate_mode="sentence")
        pipeline.handle_chunk("T-restore-failed", "<voice>不能静音回答</voice>")

    provider.synthesize.assert_not_called()
    player.write.assert_not_called()
    assert not pipeline.is_output_active()
    failure = next(
        point
        for point in realtime_trace_snapshot()
        if point.name == "system_output_restore_completed"
    )
    assert failure.attributes["success"] is False
    assert failure.attributes["output_ready"] is False


@pytest.mark.parametrize("late_outcome", ["pcm", "provider_error"])
def test_close_rejects_late_provider_pcm_and_fallback(late_outcome: str) -> None:
    """A real provider thread returning after close owns no output rights."""
    reset_realtime_trace()
    provider_entered = threading.Event()
    release_provider = threading.Event()
    provider_failure_message = "late provider failure"

    async def _synthesize(_text: str) -> bytes:
        provider_entered.set()
        assert release_provider.wait(timeout=2.0)
        await asyncio.sleep(0)
        if late_outcome == "provider_error":
            raise voice_tts.MiniMaxUnavailableError(provider_failure_message)
        return b"\x00" * 960

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _synthesize
    player = _mock_player()
    fallback_calls: list[str] = []
    ducker = voice_ducking.SystemAudioDucker(enabled=False)
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=fallback_calls.append,
        ducker=ducker,
    )
    pipeline.begin_turn("T-late-provider", gate_mode="sentence")
    provider_thread = threading.Thread(
        target=pipeline.handle_chunk,
        args=("T-late-provider", "<voice>关闭后不能播放</voice>"),
    )
    provider_thread.start()
    assert provider_entered.wait(timeout=1.0)

    assert not pipeline.close(wait_timeout_s=0.05)
    player.stop.assert_called_once()
    release_provider.set()
    provider_thread.join(timeout=1.0)

    assert not provider_thread.is_alive()
    assert pipeline.close(wait_timeout_s=1.0)
    player.write.assert_not_called()
    assert fallback_calls == []
    assert not pipeline.is_output_active()
    assert ducker.enter_output(timeout_s=0.0)
    ducker.leave_output()
