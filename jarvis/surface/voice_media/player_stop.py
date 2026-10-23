"""The media owner's bounded stops of the player and the provider, and the cleanup record."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_tts import PlayerStopResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface.voice_media.contract import StreamingTTSProvider
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_tts import AudioStreamPlayer

LOGGER = logging.getLogger(__name__)


class PlayerStopper:
    """The only writer of the player and provider stop ledger (ADR 0383).

    A stop runs once on an isolated daemon helper, so a device that never answers cannot pin
    the caller past the shutdown deadline; the errors of those helpers stay in the cleanup
    record the owner reads through ``clean``.

    The owner's thread, ``close`` and the host's sleep-notification threads (through the
    power helpers) may call it: the player's fields move under its own lock, the cleanup
    record under another, and the provider's stop is started by the owner's thread alone.
    """

    def __init__(
        self,
        *,
        player: AudioStreamPlayer,
        provider: StreamingTTSProvider,
        shutdown_timeout_s: float,
        clock: ShutdownClock,
        power: Callable[[], tuple[str, threading.Event]],
    ) -> None:
        """Stop ``player`` and ``provider`` within ``shutdown_timeout_s``, capped by ``clock``.

        ``power`` answers the owner's power state and the event its power helper sets when
        a transition is done, both read under the owner's power lock.
        """
        self._player = player
        self._provider = provider
        self._shutdown_timeout_s = shutdown_timeout_s
        self._clock = clock
        self._power = power
        self._cleanup_lock = threading.Lock()
        self._cleanup_errors: list[str] = []
        self._player_stop_started = False
        self._player_stop_done = threading.Event()
        self._player_stop_definitive = False
        self._player_stop_lock = threading.Lock()
        self._player_stop_thread: threading.Thread | None = None
        self._provider_stop_started = False
        self._provider_stop_done = threading.Event()
        self._provider_stop_thread: threading.Thread | None = None

    @property
    def player_stopped(self) -> bool:
        """True once a player stop has finished, definitively or not."""
        return self._player_stop_done.is_set()

    @property
    def player_definitive(self) -> bool:
        """True once the player is proven closed."""
        return self._player_stop_definitive

    @property
    def provider_stopped(self) -> bool:
        """True once the provider stop has finished."""
        return self._provider_stop_done.is_set()

    @property
    def clean(self) -> bool:
        """True while no stop helper has recorded an error."""
        with self._cleanup_lock:
            return not self._cleanup_errors

    def record_shutdown_player_cleanup(self, *, definitive: bool) -> None:
        """Publish late start compensation into the pipeline cleanup ledger."""
        with self._player_stop_lock:
            self._player_stop_started = True
            self._player_stop_definitive = definitive
            if definitive:
                self._player_stop_done.set()
                return
            self._player_stop_done.clear()
            existing = self._player_stop_thread
            if existing is not None and existing.is_alive():
                return

            def _join_exact_close_debt() -> None:
                retries = 0
                while True:
                    try:
                        result = self.stop_player_with_bound(
                            self._shutdown_timeout_s,
                        )
                        closed = (
                            result.definitively_closed
                            if isinstance(result, PlayerStopResult)
                            else True
                        )
                    except BaseException as exc:  # noqa: BLE001 - retained typed debt
                        retries += 1
                        record_realtime_trace(
                            "media_shutdown_player_cleanup_retry",
                            retry=retries,
                            reason=f"{type(exc).__name__}:{exc}",
                        )
                        time.sleep(min(0.1, 0.005 * (2 ** min(retries, 4))))
                        continue
                    if not closed:
                        retries += 1
                        record_realtime_trace(
                            "media_shutdown_player_cleanup_retry",
                            retry=retries,
                            reason=(
                                result.reason
                                if isinstance(result, PlayerStopResult)
                                else "untyped_close_debt"
                            ),
                        )
                        time.sleep(min(0.1, 0.005 * (2 ** min(retries, 4))))
                        continue
                    with self._player_stop_lock:
                        self._player_stop_definitive = True
                        self._player_stop_done.set()
                    record_realtime_trace(
                        "media_shutdown_player_cleanup_complete",
                        retries=retries,
                    )
                    return

            thread = threading.Thread(
                target=_join_exact_close_debt,
                name="jarvis-media-shutdown-player-debt",
                daemon=True,
            )
            self._player_stop_thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._player_stop_lock:
                if self._player_stop_thread is thread:
                    self._player_stop_thread = None
                    self._player_stop_started = False
                    self._player_stop_done.set()
            LOGGER.exception("late player cleanup janitor failed to start")

    def stop_player_bounded(self) -> bool:
        """Move potentially stuck device teardown to one controlled daemon helper."""
        power_state, power_done = self._power()
        if power_state in {"stopping", "starting"}:
            if not power_done.wait(
                timeout=self._clock.remaining_s(self._shutdown_timeout_s),
            ):
                LOGGER.error(
                    "power player transition still owns device at media shutdown",
                )
                return False
            power_state, _ = self._power()
        if power_state == "suspended":
            self._player_stop_started = True
            self._player_stop_definitive = True
            self._player_stop_done.set()
            return True
        with self._player_stop_lock:
            if (
                self._player_stop_started
                and self._player_stop_done.is_set()
                and not self._player_stop_definitive
            ):
                self._player_stop_started = False
                self._player_stop_done = threading.Event()
            launch_stop = not self._player_stop_started
            if launch_stop:
                self._player_stop_started = True

        if launch_stop:

            def _stop() -> None:
                try:
                    self.stop_player_until_definitive(
                        self._clock.remaining_s(self._shutdown_timeout_s),
                    )
                    self._player_stop_definitive = True
                except Exception as exc:
                    with self._cleanup_lock:
                        self._cleanup_errors.append(f"player:{type(exc).__name__}")
                    LOGGER.exception("streaming media player stop failed")
                finally:
                    self._player_stop_done.set()

            self._player_stop_thread = threading.Thread(
                target=_stop,
                name="jarvis-media-device-stop",
                daemon=True,
            )
            self._player_stop_thread.start()
        stopped = self._player_stop_done.wait(
            timeout=self._clock.remaining_s(self._shutdown_timeout_s),
        )
        if not stopped:
            LOGGER.error(
                "player.stop exceeded media shutdown deadline; isolated daemon helper remains",
            )
        return stopped and self._player_stop_definitive

    def stop_player_with_bound(self, timeout_s: float) -> object:
        """Call the typed player API while retaining legacy test-port support."""
        try:
            return self._player.stop(timeout_s=max(0.0, timeout_s))
        except TypeError as exc:
            if "timeout_s" not in str(exc):
                raise
            return self._player.stop()

    def stop_player_until_definitive(self, first_timeout_s: float) -> object:
        """Join/retry one typed player debt on the current helper thread."""
        timeout_s = max(0.0, first_timeout_s)
        retries = 0
        while True:
            try:
                result = self.stop_player_with_bound(timeout_s)
            except BaseException as exc:  # noqa: BLE001 - retained exact debt
                retries += 1
                record_realtime_trace(
                    "media_player_stop_debt_retry",
                    retry=retries,
                    reason=f"{type(exc).__name__}:{exc}",
                )
            else:
                if not isinstance(result, PlayerStopResult) or result.definitively_closed:
                    return result
                retries += 1
                record_realtime_trace(
                    "media_player_stop_debt_retry",
                    retry=retries,
                    reason=result.reason,
                    helper_thread_alive=result.helper_thread_alive,
                )
            time.sleep(min(0.1, 0.005 * (2 ** min(retries, 4))))
            timeout_s = self._shutdown_timeout_s

    def stop_provider_bounded(self) -> bool:
        """Close the actor's provider exactly once on an isolated daemon helper."""
        request_close = getattr(self._provider, "request_close", None)
        if not callable(request_close):
            self._provider_stop_done.set()
            return True
        if not self._provider_stop_started:
            self._provider_stop_started = True

            def _stop() -> None:
                try:
                    request_close()
                except Exception as exc:
                    with self._cleanup_lock:
                        self._cleanup_errors.append(f"provider:{type(exc).__name__}")
                    LOGGER.exception("streaming media provider stop failed")
                finally:
                    self._provider_stop_done.set()

            self._provider_stop_thread = threading.Thread(
                target=_stop,
                name="jarvis-media-provider-stop",
                daemon=True,
            )
            self._provider_stop_thread.start()
        stopped = self._provider_stop_done.wait(
            timeout=self._clock.remaining_s(self._shutdown_timeout_s),
        )
        if not stopped:
            LOGGER.error(
                "provider stop exceeded media shutdown deadline; isolated daemon helper remains",
            )
        return stopped
