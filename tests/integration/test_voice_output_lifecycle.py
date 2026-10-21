"""Threaded integration regressions for output-player close ownership and MiniMax deadlines."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from jarvis.surface import voice_tts


class _LifecycleOutputStream:
    """Foreign OutputStream fixture with independently controlled lifecycle."""

    def __init__(
        self,
        *,
        stop_error: BaseException | None = None,
        close_failures: int = 0,
        close_entered: threading.Event | None = None,
        close_release: threading.Event | None = None,
        stop_release: threading.Event | None = None,
    ) -> None:
        self.stop_error = stop_error
        self.close_failures = close_failures
        self.close_entered = close_entered
        self.close_release = close_release
        self.stop_release = stop_release
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_release is not None:
            self.stop_release.wait()
        if self.stop_error is not None:
            raise self.stop_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            self.close_release.wait()
        if self.close_failures > 0:
            self.close_failures -= 1
            message = "injected output close failure"
            raise OSError(message)


def test_output_player_close_debt_blocks_successor_until_exact_retry_closes() -> None:
    """Stop failure cannot skip close; close debt serializes retry and restart."""
    first = _LifecycleOutputStream(
        stop_error=OSError("injected stop failure"),
        close_failures=1,
    )
    second = _LifecycleOutputStream()
    streams = iter([first, second])
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=lambda **_kwargs: next(streams),
    ):
        player = voice_tts.AudioStreamPlayer(lazy_open=True)
        assert player.start().started
        failed = player.stop(timeout_s=0.5)
        assert not failed.definitively_closed
        assert first.stop_calls == 1
        assert first.close_calls == 1
        blocked = player.start()
        assert not blocked.started
        assert second.start_calls == 0
        retried = player.stop(timeout_s=0.5)
        assert retried.definitively_closed
        assert first.close_calls == 2
        assert player.start().started
        assert second.start_calls == 1
        assert player.stop(timeout_s=0.5).definitively_closed


def test_output_player_concurrent_close_joins_one_hung_exact_helper() -> None:
    """Repeated stop joins one close helper and never clears timeout debt."""
    close_entered = threading.Event()
    close_release = threading.Event()
    first = _LifecycleOutputStream(
        close_entered=close_entered,
        close_release=close_release,
    )
    second = _LifecycleOutputStream()
    streams = iter([first, second])
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=lambda **_kwargs: next(streams),
    ):
        player = voice_tts.AudioStreamPlayer(lazy_open=True)
        assert player.start().started
        results: list[voice_tts.PlayerStopResult] = []
        first_stop = threading.Thread(
            target=lambda: results.append(player.stop(timeout_s=0.02)),
        )
        second_stop = threading.Thread(
            target=lambda: results.append(player.stop(timeout_s=0.02)),
        )
        first_stop.start()
        assert close_entered.wait(timeout=1.0)
        second_stop.start()
        first_stop.join(timeout=1.0)
        second_stop.join(timeout=1.0)
        assert len(results) == 2
        assert all(not result.definitively_closed for result in results)
        assert first.stop_calls == 1
        assert first.close_calls == 1
        assert not player.start().started
        assert second.start_calls == 0
        close_release.set()
        deadline = time.monotonic() + 1.0
        while not player.stop(timeout_s=0.02).definitively_closed:
            assert time.monotonic() < deadline
        assert player.start().started
        assert second.start_calls == 1
        assert player.stop(timeout_s=0.5).definitively_closed


def test_output_player_attempts_close_when_stop_hangs() -> None:
    """A hung stop stage keeps debt after close and cannot block close attempt."""
    stop_release = threading.Event()
    first = _LifecycleOutputStream(stop_release=stop_release)
    second = _LifecycleOutputStream()
    streams = iter([first, second])
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=lambda **_kwargs: next(streams),
    ):
        player = voice_tts.AudioStreamPlayer(lazy_open=True)
        assert player.start().started
        result = player.stop(timeout_s=0.5)
        assert not result.definitively_closed
        assert result.helper_thread_alive
        assert first.stop_calls == 1
        assert first.close_calls == 1
        assert not player.start().started
        assert second.start_calls == 0
        repeated = player.stop(timeout_s=0.02)
        assert not repeated.definitively_closed
        assert repeated.attempt_id == result.attempt_id
        assert first.stop_calls == 1
        assert first.close_calls == 1
        stop_release.set()
        deadline = time.monotonic() + 1.0
        while not player.stop(timeout_s=0.02).definitively_closed:
            assert time.monotonic() < deadline
        assert player.start().started
        assert second.start_calls == 1
        assert player.stop(timeout_s=0.5).definitively_closed


def test_output_stop_final_cas_is_atomic_against_repeated_stop() -> None:
    """No retry can enter between stop-helper liveness and final close CAS."""
    stop_release = threading.Event()
    final_cas_entered = threading.Event()
    final_cas_release = threading.Event()
    stream = _LifecycleOutputStream(stop_release=stop_release)
    with patch.object(voice_tts, "_open_output_stream", return_value=stream):
        player = voice_tts.AudioStreamPlayer(lazy_open=True)
        assert player.start().started
        first = player.stop(timeout_s=0.08)
        assert not first.definitively_closed
        assert stream.stop_calls == 1
        assert stream.close_calls == 1

        def _hold_final_cas() -> None:
            final_cas_entered.set()
            assert final_cas_release.wait(timeout=1.0)

        with patch.object(
            player,
            "_stop_before_final_cas_hook",
            _hold_final_cas,
        ):
            stop_release.set()
            assert final_cas_entered.wait(timeout=1.0)
            results: list[voice_tts.PlayerStopResult] = []
            repeated = threading.Thread(
                target=lambda: results.append(player.stop(timeout_s=0.5)),
            )
            repeated.start()
            time.sleep(0.02)
            assert repeated.is_alive()
            assert stream.stop_calls == 1
            assert stream.close_calls == 1
            final_cas_release.set()
            repeated.join(timeout=1.0)
            assert not repeated.is_alive()
            assert len(results) == 1
            assert results[0].definitively_closed
            assert stream.stop_calls == 1
            assert stream.close_calls == 1


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


def test_a_configured_device_that_will_not_resolve_fails_closed_by_name() -> None:
    """An unresolvable device name fails the open closed and says which name.

    `sounddevice` raises `ValueError` for a name that matches no device. There
    is no fallback to the system default: routing a run out of the owner's real
    speakers is the failure this configuration exists to remove.
    """
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=ValueError("no output device matching 'No Such Device'"),
    ) as opener:
        named = voice_tts.AudioStreamPlayer(lazy_open=True, device="No Such Device").start()
        # Exactly one open attempt, carrying the configured device: a retry with
        # device=None would be the fallback this asserts does not happen.
        assert opener.call_count == 1
        assert opener.call_args.kwargs["device"] == "No Such Device"
        default = voice_tts.AudioStreamPlayer(lazy_open=True).start()

    assert named.status == "failed_closed"
    assert not named.started
    assert "No Such Device" in named.reason
    assert default.status == "failed_closed"
    assert default.reason == "open:ValueError"
