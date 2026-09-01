"""Threaded integration regressions for duck/restore and TTS shutdown ownership."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_ducking, voice_tts

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _mock_player() -> MagicMock:
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    player.write.return_value = 240
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
        fallback=None,
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
        assert pipeline.wait_until_idle(timeout_s=1.0)

    assert not restore_thread.is_alive()
    assert not tts_thread.is_alive()
    assert ducker.restore_state == "ready"
    player.write.assert_called_once()
    assert pipeline.close(wait_timeout_s=1.0)


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
        fallback=None,
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
        assert pipeline.wait_until_idle(timeout_s=3.0)

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
    assert pipeline.close(wait_timeout_s=1.0)


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
    fallback_factory = MagicMock()
    ducker = voice_ducking.SystemAudioDucker(enabled=False)
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=fallback_factory,
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
    fallback_factory.assert_not_called()
    assert not pipeline.is_output_active()
    assert ducker.enter_output(timeout_s=0.0)
    ducker.leave_output()


def test_close_invalidates_pcm_at_atomic_ring_publish() -> None:
    """A writer paused before the atomic gate cannot publish after close."""
    reset_realtime_trace()
    publish_attempted = threading.Event()
    release_publish = threading.Event()

    async def _synthesize(_text: str) -> bytes:
        await asyncio.sleep(0)
        return np.ones(24, dtype=np.float32).tobytes()

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _synthesize
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=100,
        ring_seconds=1.0,
        lazy_open=True,
    )
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=None,
        ducker=voice_ducking.SystemAudioDucker(enabled=False),
    )
    gate = pipeline._pcm_commit_gate  # noqa: SLF001 — integration seam.
    real_publish = gate.publish

    def _pause_immediately_before_atomic_publish(
        generation: int,
        publish: Callable[[], int],
    ) -> int | None:
        publish_attempted.set()
        assert release_publish.wait(timeout=2.0)
        return real_publish(generation, publish)

    with patch.object(
        gate,
        "publish",
        side_effect=_pause_immediately_before_atomic_publish,
    ):
        pipeline.begin_turn("T-pcm-publish-barrier", gate_mode="sentence")
        pipeline.handle_chunk(
            "T-pcm-publish-barrier",
            "<voice>关闭后不得发布</voice>",
        )
        assert publish_attempted.wait(timeout=1.0)

        started_at = time.monotonic()
        pipeline.request_close()
        assert time.monotonic() - started_at < 0.25
        assert player.bytes_pending() == 0
        release_publish.set()

    assert pipeline.close(wait_timeout_s=1.0)
    callback_buffer = np.zeros((24, 1), dtype=np.float32)
    player._callback(  # noqa: SLF001 — deterministic PortAudio consumer seam.
        callback_buffer,
        24,
        None,
        None,
    )
    assert player.bytes_pending() == 0
    assert player.played_samples == 0
    assert not pipeline._worker_thread.is_alive()  # noqa: SLF001 — leak proof.
    assert not pipeline.is_output_active()
    assert any(
        point.name == "tts_late_pcm_discarded"
        and point.attributes.get("reason")
        == "generation_invalidated_before_ring_publish"
        for point in realtime_trace_snapshot()
    )


def test_tts_batch_work_queue_is_bounded_and_traces_overflow() -> None:
    """Batch overload drops with telemetry; it never becomes an unbounded queue."""
    reset_realtime_trace()
    provider_entered = threading.Event()
    release_provider = threading.Event()

    async def _blocked_synthesize(_text: str) -> bytes:
        provider_entered.set()
        assert release_provider.wait(timeout=2.0)
        await asyncio.sleep(0)
        return b""

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _blocked_synthesize
    player = _mock_player()
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=None,
        ducker=voice_ducking.SystemAudioDucker(enabled=False),
    )
    pipeline.begin_turn("T-bounded-batch", gate_mode="sentence")
    pipeline.handle_chunk("T-bounded-batch", "<voice>占住 worker</voice>")
    assert provider_entered.wait(timeout=1.0)

    for sequence in range(pipeline._MAX_PENDING_WORK_ITEMS + 8):  # noqa: SLF001
        pipeline.handle_chunk(
            "T-bounded-batch",
            f"<voice>queued-{sequence}</voice>",
        )

    overflows = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "tts_work_queue_overflow"
    ]
    assert overflows
    assert all(
        point.attributes["overflow_policy"]
        == "drop_and_trace_not_stream_backpressure"
        for point in overflows
    )
    assert pipeline._work_queue.qsize() <= pipeline._MAX_PENDING_WORK_ITEMS  # noqa: SLF001

    pipeline.request_close()
    assert (
        pipeline._work_queue.qsize()  # noqa: SLF001 — includes reserved sentinel.
        <= pipeline._MAX_PENDING_WORK_ITEMS + 1  # noqa: SLF001
    )
    release_provider.set()
    assert pipeline.close(wait_timeout_s=1.0)
    assert not pipeline._worker_thread.is_alive()  # noqa: SLF001 — leak proof.
    assert not pipeline.is_output_active()


def test_close_wins_after_fallback_registration_before_process_start() -> None:
    """The close/fallback linearization point prevents a post-close spawn."""
    run_entered = threading.Event()
    release_run = threading.Event()
    finished = threading.Event()
    process_started = threading.Event()
    owner_lock = threading.Lock()
    cancelled = False

    class _BarrierOwner:
        def run(self) -> bool:
            run_entered.set()
            assert release_run.wait(timeout=2.0)
            with owner_lock:
                if cancelled:
                    finished.set()
                    return False
                process_started.set()
                finished.set()
                return True

        def cancel(self, *, wait_timeout_s: float) -> bool:
            nonlocal cancelled
            with owner_lock:
                cancelled = True
            release_run.set()
            return finished.wait(timeout=wait_timeout_s)

    unavailable_message = "provider unavailable"

    async def _unavailable(_text: str) -> bytes:
        raise voice_tts.MiniMaxUnavailableError(unavailable_message)

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _unavailable
    player = _mock_player()
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: _BarrierOwner(),
    )
    pipeline.begin_turn("T-fallback-register", gate_mode="sentence")
    pipeline.handle_chunk("T-fallback-register", "<voice>不要在关闭后启动</voice>")
    assert run_entered.wait(timeout=1.0)

    assert pipeline.close(wait_timeout_s=1.0)
    assert not process_started.is_set()
    player.write.assert_not_called()
    assert not pipeline.is_output_active()


def test_close_terminates_then_kills_started_macos_say() -> None:
    """An already-started ``say`` process is killed and joined within the bound."""
    process_started = threading.Event()
    process_finished = threading.Event()
    terminate_called = threading.Event()
    kill_called = threading.Event()

    class _StubbornProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            terminate_called.set()

        def kill(self) -> None:
            kill_called.set()
            self.returncode = -9
            process_finished.set()

        def wait(self, timeout: float | None = None) -> int:
            command = "say"
            timeout_s = timeout or 0.0
            if threading.current_thread().name == "jarvis-tts-worker":
                if not process_finished.wait(timeout=timeout):
                    raise subprocess.TimeoutExpired(command, timeout_s)
                return self.returncode or 0
            if self.returncode is None:
                raise subprocess.TimeoutExpired(command, timeout_s)
            return self.returncode

    stubborn = _StubbornProcess()

    def _popen(*_args: object, **_kwargs: object) -> _StubbornProcess:
        process_started.set()
        return stubborn

    unavailable_message = "provider unavailable"

    async def _unavailable(_text: str) -> bytes:
        raise voice_tts.MiniMaxUnavailableError(unavailable_message)

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize.side_effect = _unavailable
    player = _mock_player()
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=voice_tts.MacOSSayProcessOwner,
    )

    with patch.object(subprocess, "Popen", side_effect=_popen):
        pipeline.begin_turn("T-say-kill", gate_mode="sentence")
        pipeline.handle_chunk("T-say-kill", "<voice>终止进程</voice>")
        assert process_started.wait(timeout=1.0)
        started_at = time.monotonic()
        assert pipeline.close(wait_timeout_s=1.0)
        elapsed_s = time.monotonic() - started_at

    assert elapsed_s < 1.0
    assert terminate_called.is_set()
    assert kill_called.is_set()
    assert process_finished.is_set()
    player.write.assert_not_called()
    assert not pipeline.is_output_active()


def test_real_watcher_provider_cancel_teardown_is_bounded(tmp_path: Path) -> None:
    """TTS teardown never leaves provider work in asyncio's default executor."""
    reset_realtime_trace()
    provider_entered = threading.Event()
    cancellation_seen = threading.Event()
    fallback_factory = MagicMock()
    elapsed_s = 0.0

    async def _provider_late_return(_text: str) -> bytes:
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            return b"\x00" * 960
        return b""

    async def _body() -> tuple[voice_tts.TTSPipeline, MagicMock]:
        nonlocal elapsed_s
        conn = open_event_log(tmp_path / "events.db")
        provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
        provider.synthesize.side_effect = _provider_late_return
        player = _mock_player()
        pipeline = voice_tts.TTSPipeline(
            provider=provider,
            player=player,
            fallback=fallback_factory,
            ducker=voice_ducking.SystemAudioDucker(enabled=False),
        )
        watcher = asyncio.create_task(
            inherent_loop._tts_watcher(  # noqa: SLF001 — integration seam.
                conn=conn,
                pipeline=pipeline,
                poll_interval_s=0.01,
            ),
        )
        await asyncio.sleep(0.03)
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T-owned-provider",
                "query": "shutdown",
                "kind": "text",
                "required_gate_mode": "sentence",
            },
            correlation={"turn_id": "T-owned-provider"},
        )
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={"turn_id": "T-owned-provider", "text": "<voice>关闭</voice>"},
            correlation={"turn_id": "T-owned-provider"},
        )
        assert await asyncio.to_thread(provider_entered.wait, 1.0)

        started_at = time.monotonic()
        inherent_loop._request_tts_close(pipeline)  # noqa: SLF001 — serve order.
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        inherent_loop._shutdown_tts(pipeline)  # noqa: SLF001 — serve order.
        elapsed_s = time.monotonic() - started_at
        conn.close()
        return pipeline, player

    run_started = time.monotonic()
    pipeline, player = asyncio.run(_body())
    asyncio_run_elapsed_s = time.monotonic() - run_started

    assert elapsed_s < 1.0
    assert asyncio_run_elapsed_s < 2.0
    assert cancellation_seen.is_set()
    assert pipeline.wait_until_idle(timeout_s=0.0)
    assert not pipeline._worker_thread.is_alive()  # noqa: SLF001 — ownership proof.
    assert not pipeline.is_output_active()
    player.write.assert_not_called()
    fallback_factory.assert_not_called()
    assert any(
        point.name == "tts_late_pcm_discarded"
        for point in realtime_trace_snapshot()
    )


def test_minimax_total_deadline_bounds_a_stuck_connect() -> None:
    """The concrete provider owns a total deadline above per-message waits."""
    async def _never_connect(
        _url: str,
        *,
        additional_headers: dict[str, str],
    ) -> object:
        del additional_headers
        await asyncio.Event().wait()
        return object()

    async def _body() -> float:
        client = voice_tts.MiniMaxWSClient(
            api_key="integration-key",
            connect_timeout_s=1.0,
            total_timeout_s=0.03,
        )
        started_at = time.monotonic()
        with (
            patch.object(voice_tts, "_ws_connect", side_effect=_never_connect),
            pytest.raises(voice_tts.MiniMaxUnavailableError),
        ):
            await client.synthesize("deadline")
        client.request_close()
        return time.monotonic() - started_at

    assert asyncio.run(_body()) < 0.5


def test_minimax_request_close_cancels_and_closes_active_session() -> None:
    """Provider close cancels its task and runs the WebSocket close finally."""
    handshake_waiting = asyncio.Event()
    connection_closed = asyncio.Event()

    class _HangingConnection:
        def __init__(self) -> None:
            self.recv_count = 0

        async def recv(self) -> str:
            self.recv_count += 1
            if self.recv_count == 1:
                return '{"event":"connected_success"}'
            handshake_waiting.set()
            await asyncio.Event().wait()
            return ""

        async def send(self, _message: str) -> None:
            await asyncio.sleep(0)

        async def close(self) -> None:
            connection_closed.set()

    async def _body() -> None:
        connection = _HangingConnection()
        client = voice_tts.MiniMaxWSClient(
            api_key="integration-key",
            total_timeout_s=5.0,
        )
        with patch.object(
            voice_tts,
            "_ws_connect",
            return_value=connection,
        ):
            synth_task = asyncio.create_task(client.synthesize("cancel"))
            await asyncio.wait_for(handshake_waiting.wait(), timeout=1.0)
            client.request_close()
            with pytest.raises(asyncio.CancelledError):
                await synth_task
        assert connection_closed.is_set()

    asyncio.run(_body())
