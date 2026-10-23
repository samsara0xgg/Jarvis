"""The persistent L5 media owner: the one class the daemon builds, on its bounded bridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Literal

from jarvis.shared import Event, lang
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_media.config import StreamingMediaConfig
from jarvis.surface.voice_media.contract import (
    MediaPowerTransitionResult,
    MediaSubmitOutcome,
    StreamingMediaStartupError,
    StreamingTTSProvider,
)
from jarvis.surface.voice_media.gain import OutputGain
from jarvis.surface.voice_media.journal import PlaybackJournal
from jarvis.surface.voice_media.lane import PlaybackLane
from jarvis.surface.voice_media.network_lost import NetworkLostCue
from jarvis.surface.voice_media.player_stop import PlayerStopper
from jarvis.surface.voice_media.power import ActorPort, PowerController
from jarvis.surface.voice_media.response import MediaCommand
from jarvis.surface.voice_media.ring_sink import RingSink
from jarvis.surface.voice_media.say_fallback import SayFallback
from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
from jarvis.surface.voice_media.spare import SpareSessions
from jarvis.surface.voice_media.streamer import ResponseStreamer
from jarvis.surface.voice_tts import AudioStreamPlayer, PlayerStartResult

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.surface.voice_ducking import SystemAudioDucker

LOGGER = logging.getLogger(__name__)


class StreamingTTSPipeline:
    """One persistent async L5 media actor behind a bounded sync/async bridge."""

    def __init__(  # noqa: PLR0913 - explicit ownership dependencies
        self,
        *,
        provider: StreamingTTSProvider,
        player: AudioStreamPlayer,
        conn_factory: Callable[[], sqlite3.Connection],
        boot_high_water_id: int,
        config: StreamingMediaConfig | None = None,
        broadcaster: object | None = None,
        ducker: SystemAudioDucker | None = None,
        foreground_decision_callable: Callable[[str, int, str, int], str] | None = None,
        start_player: bool = True,
        silent_intent_channels: frozenset[str] = frozenset(),
        speaks_turn: Callable[[sqlite3.Connection, str], bool] | None = None,
    ) -> None:
        """Start one persistent actor without letting stuck startup pin exit.

        ``speaks_turn`` (ADR 0209), given the actor's own connection and a turn id, says
        whether this actor speaks that turn at all; a phone's actor speaks only the turns
        its phone opened by voice. ``None`` speaks every turn not in ``silent_intent_channels``.
        """
        self._provider = provider
        self._player = player
        self._journal = PlaybackJournal(conn_factory)
        self._config = config or StreamingMediaConfig()
        self._start_player = start_player
        self._accepting = threading.Event()
        self._accepting.set()
        self._ready = threading.Event()
        self._startup_finished = threading.Event()
        self._startup_abandoned = threading.Event()
        self._startup_lock = threading.Lock()
        self._startup_phase = "opening_connection"
        self._closed = threading.Event()
        self._output_active = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[MediaCommand | object] | None = None
        self._startup_error: BaseException | None = None
        self._startup_error_phase: str | None = None
        self._startup_device_disposition: Literal[
            "not_attempted", "failed_closed", "uncertain"
        ] = "not_attempted"
        self._clock = ShutdownClock()
        self._stopper = PlayerStopper(
            player=player,
            provider=provider,
            shutdown_timeout_s=self._config.shutdown_timeout_s,
            clock=self._clock,
            power=lambda: self._power.snapshot(),  # noqa: PLW0108 - the controller is built after the stopper
        )
        self._power = PowerController(
            player=player,
            stopper=self._stopper,
            shutdown_timeout_s=self._config.shutdown_timeout_s,
            actor=ActorPort(loop=lambda: self._loop, closed=self._closed.is_set),
            closed=self._closed.is_set,
            stop_accepting=self._accepting.clear,
            start_accepting=self._accepting.set,
            lane_isolated=lambda: self._lane.isolated,
            suspend_actor=lambda: self._suspend_for_sleep_owned(),  # noqa: PLW0108 - tests patch the method
            resume_actor=lambda *, attempt_id, admission_generation: self._resume_after_wake_owned(
                attempt_id=attempt_id,
                admission_generation=admission_generation,
            ),
        )
        self._gain = OutputGain(player)
        # Language of the last answer's first segment: what he speaks, so what the next spare reads.
        self._last_answer_language: lang.Language | None = None
        self._spare = SpareSessions(
            provider=provider,
            config=self._config,
            clock=self._clock,
            admitting=self._admitting,
            language=lambda: self._last_answer_language or lang.language(),
        )
        self._network_lost = NetworkLostCue(
            provider=provider,
            config=self._config,
            clock=self._clock,
            journal=self._journal,
            admitting=self._admitting,
        )
        self._ring = RingSink(
            player=player,
            config=self._config,
            journal=self._journal,
            current=lambda: self._lane.active,
        )
        self._say_fallback = SayFallback(
            player=player,
            config=self._config,
            clock=self._clock,
            ring=self._ring,
            current=lambda: self._lane.active,
            isolate=lambda active: self._lane.isolate_fallback_cleanup(active),  # noqa: PLW0108 - the lane is built after the fallback
        )
        self._streamer = ResponseStreamer(
            provider=provider,
            player=player,
            config=self._config,
            clock=self._clock,
            journal=self._journal,
            ring=self._ring,
            say_fallback=self._say_fallback,
            spare=self._spare,
            network_lost=self._network_lost,
            current=lambda: self._lane.active,
            set_answer_language=self._remember_answer_language,
            checkpoint=lambda active, snapshot: self._lane.checkpoint_if_advanced(active, snapshot),  # noqa: PLW0108 - the lane is built after the streamer
        )
        self._lane = PlaybackLane(
            player=player,
            config=self._config,
            clock=self._clock,
            journal=self._journal,
            gain=self._gain,
            spare=self._spare,
            say_fallback=self._say_fallback,
            streamer=self._streamer,
            power=self._power,
            broadcaster=broadcaster,
            ducker=ducker,
            foreground_decision=foreground_decision_callable,
            silent_intent_channels=silent_intent_channels,
            speaks_turn=speaks_turn,
            boot_high_water_id=boot_high_water_id,
            stop_accepting=self._accepting.clear,
            set_output_active=self._output_active.set,
            clear_output_active=self._output_active.clear,
            request_shutdown_deadline=self._request_shutdown_deadline,
        )
        self._thread = threading.Thread(
            target=self._thread_main,
            name="jarvis-media-owner",
            # A permanently stuck third-party connection/device constructor
            # cannot be interrupted by Python. The actor is therefore daemon
            # backed, publishes explicit degraded cleanup state, and rejects
            # successors until late startup has been isolated and closed.
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + self._config.shutdown_timeout_s
        finished = self._startup_finished.wait(
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if not finished:
            phase = self._get_startup_phase()
            self._startup_abandoned.set()
            self._accepting.clear()
            self._publish_shutdown_deadline(deadline)
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            message = "persistent media owner did not start within its bound"
            raise StreamingMediaStartupError(
                message,
                phase=phase,
                device_disposition=(
                    "uncertain" if phase == "starting_player" else "not_attempted"
                ),
            )
        if not self._ready.is_set():
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            self._raise_startup_error()

    def _raise_startup_error(self) -> None:
        """Surface an actor-thread failure after its readiness publication."""
        error = self._startup_error
        if error is not None:
            msg = "persistent media owner failed to start"
            raise StreamingMediaStartupError(
                msg,
                phase=self._startup_error_phase or self._get_startup_phase(),
                device_disposition=self._startup_device_disposition,
            ) from error

    def _set_startup_phase(self, phase: str) -> None:
        with self._startup_lock:
            self._startup_phase = phase

    def _get_startup_phase(self) -> str:
        with self._startup_lock:
            return self._startup_phase

    @property
    def boot_high_water_event_log_id(self) -> int:
        """Expose the exact replay cutoff captured before actor construction."""
        return self._lane.boot_high_water_id

    @property
    def media_thread_name(self) -> str:
        """Expose the single owner identity for integration leak assertions."""
        return self._thread.name

    async def submit_event(
        self,
        *,
        row_id: int,
        event: Event,
        origin: str = "watcher",
    ) -> MediaSubmitOutcome:
        """Apply bounded cross-loop backpressure and await command disposition."""
        if not self._accepting.is_set():
            return MediaSubmitOutcome("closed", event.event_uid)
        loop = self._loop
        if loop is None or self._closed.is_set():
            return MediaSubmitOutcome("closed", event.event_uid)
        future = asyncio.run_coroutine_threadsafe(
            self._submit_owned(row_id=row_id, event=event, origin=origin),
            loop,
        )
        return await asyncio.wrap_future(future)

    def stop_foreground_output(self, response_id: str | None, *, reason: str = "user_stop") -> str:
        """Stop the speech audible right now, leaving its ResponseRun alone.

        ``response_id=None`` stops whatever is speaking: a spoken interruption
        in conversation mode (ADR 0041, ``reason="barge_in"``) knows no id.
        A named response that is not audible yet (queued, parked while Allen
        talks, buffering) is dropped unsaid.

        ADR-0014 D20 step 4, callable from any non-actor thread. The target
        is resolved on the actor against ``self._active``'s own playback
        lease rather than against a caller-supplied generation id, which
        cannot be stale by the time it is compared. Answers ``applied``,
        ``stale`` (nothing matching was speaking) or ``uncertain``.

        ``uncertain`` covers two different unknowns. The actor reached the
        stop and its durable terminal was owed, or this caller stopped
        waiting first -- in which case even the tombstone may not have been
        published yet. Neither tells the caller the audio is definitely
        stopped, which is exactly why they share one word. The wait is
        bounded by ``streaming_output.shutdown_timeout_s``, the actor's own
        budget; ``realtime.response.cancel_timeout_ms`` bounds the SQLite
        CAS of the ``generation`` scope and has nothing to bound here.

        The terminal says ``reason``: ``user_stop`` for the surface's stop
        button, ``barge_in`` when Allen talked over Jarvis.
        """
        loop = self._loop
        if loop is None or self._closed.is_set():
            return "stale"
        if response_id is None:
            # Whatever is speaking fades now, not once the actor gets here: an
            # actor busy on SQLite could let her play on for seconds.
            self._gain.begin_fade()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._lane.stop_foreground_output(response_id, reason),
                loop,
            )
        except RuntimeError:
            if response_id is None:
                self._gain.restore()
            return "stale"
        try:
            return future.result(timeout=self._config.shutdown_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            # The actor took the request; whether the tombstone landed is
            # exactly what this caller cannot know.
            return "uncertain"

    def hold_output(self, *, held: bool) -> None:
        """Hold or release answer starts while Allen is talking (ADR 0053).

        Fire and forget from the capture threads; the actor applies the holds
        in call order. An answer already speaking is barge-in's, not the hold's.
        """
        loop = self._loop
        if loop is None or self._closed.is_set():
            return
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(self._lane.hold_output(held=held), loop)

    def hold_for_devices(self, *, held: bool) -> bool:
        """Hold or release answer starts around an audio device refresh (ADR 0054).

        Answers whether nothing is playing once the hold is in place, so the
        caller may close the player; a closed or unresponsive actor answers
        ``False``. The caller always releases, which starts the parked answers
        unless Allen's own hold (ADR 0053) still stands.
        """
        loop = self._loop
        if loop is None or self._closed.is_set():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._lane.hold_for_devices(held=held), loop,
            )
        except RuntimeError:
            return False
        try:
            return future.result(timeout=self._config.shutdown_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            return False

    @property
    def player(self) -> AudioStreamPlayer:
        """The persistent player, which a device refresh closes and reopens (ADR 0054)."""
        return self._player

    def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
        """Drop the answers of ``turn_ids`` that never reached the speaker (ADR 0053).

        Returns the turns dropped: those of ``turn_ids`` none of whose answers
        ever started playing. Their runs are the caller's to cancel. A closed
        or unresponsive actor drops nothing.
        """
        loop = self._loop
        if loop is None or self._closed.is_set() or not turn_ids:
            return frozenset()
        try:
            future = asyncio.run_coroutine_threadsafe(self._lane.drop_unspoken(turn_ids), loop)
        except RuntimeError:
            return frozenset()
        try:
            return future.result(timeout=self._config.shutdown_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            return frozenset()

    def request_close(self) -> None:
        """Stop admission and publish shutdown outside the normal command lane."""
        self._accepting.clear()
        self._request_shutdown_deadline(self._config.shutdown_timeout_s)

    def close(self, *, wait_timeout_s: float | None = None) -> bool:
        """Return true only after actor, provider, and device cleanup complete."""
        timeout = (
            self._config.shutdown_timeout_s
            if wait_timeout_s is None
            else max(0.0, wait_timeout_s)
        )
        deadline = time.monotonic() + timeout
        self._accepting.clear()
        self._publish_shutdown_deadline(deadline)
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if (
            not self._thread.is_alive()
            and self._closed.is_set()
            and self._stopper.provider_stopped
            and self._stopper.player_stopped
            and not self._stopper.player_definitive
        ):
            self._stopper.stop_player_bounded()
        complete = self.cleanup_complete
        if not complete:
            record_realtime_trace(
                "media_owner_cleanup_degraded",
                actor_stopped=not self._thread.is_alive(),
                provider_stopped=self._stopper.provider_stopped,
                player_stopped=self._stopper.player_stopped,
                startup_phase=self._get_startup_phase(),
            )
        return complete

    @property
    def cleanup_complete(self) -> bool:
        """Report confirmed teardown, including late daemon-janitor completion."""
        clean = self._stopper.clean
        return (
            not self._thread.is_alive()
            and self._closed.is_set()
            and self._stopper.provider_stopped
            and self._stopper.player_stopped
            and self._stopper.player_definitive
            and clean
        )

    def _request_shutdown_deadline(self, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        self._publish_shutdown_deadline(deadline)

    def _publish_shutdown_deadline(self, deadline: float) -> None:
        """Publish an absolute deadline that can only move earlier, and mark power shut down."""
        self._clock.lower_to(deadline)
        self._power.mark_shutdown()

    def wait_until_idle(self, *, timeout_s: float) -> bool:
        """Wait without borrowing actor-owned mutable state."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self._output_active.is_set():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def is_output_active(self) -> bool:
        """Return the actor-published output ownership bit."""
        return self._output_active.is_set()

    def is_speaking(self) -> bool:
        """Compatibility alias used by the wake listener."""
        return self.is_output_active()

    def set_output_gain(self, gain: float, ramp_ms: float = 10.0) -> None:
        """Ramp the player's output gain: 0.0 is the ADR-0015 D2 speech mute, 1.0 restores."""
        self._gain.set_mute(gain, ramp_ms)

    def set_yield_gain(self, gain: float) -> None:
        """Lower the speech while Allen may be taking the turn; 1.0 gives it back.

        Scales, never replaces, the mute: a yield ending during a mute stays silent.
        """
        self._gain.set_yield(gain)

    def pause_speaking(self, paused: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        """Hold her answer where it is while Allen's words are judged, or let it go on.

        Fire and forget from the capture thread; the actor fades her out
        before the hold and back in after it, in call order.
        """
        loop = self._loop
        if loop is None or self._closed.is_set():
            return
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(self._lane.pause(paused=paused), loop)

    def suspend_for_sleep(
        self,
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Terminalize actor output, then prove the player stream stopped."""
        return self._power.suspend_for_sleep(timeout_s=timeout_s, deadline=deadline)

    def resume_after_wake(
        self,
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Open a fresh player stream before restoring output admission."""
        return self._power.resume_after_wake(timeout_s=timeout_s, deadline=deadline)

    def revoke_wake_starts_for_shutdown(self) -> None:
        """Revoke wake-created output without stopping an existing running owner."""
        self._power.revoke_wake_starts_for_shutdown()

    def admit_wake_start(self, *, attempt_id: int) -> bool:
        """Commit one fresh output only after coordinator restored input."""
        return self._power.admit_wake_start(attempt_id=attempt_id)

    def abort_wake_start(
        self,
        *,
        attempt_id: int,
        reason: str,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Abort one unadmitted wake output without disabling future wakes."""
        return self._power.abort_wake_start(
            attempt_id=attempt_id,
            reason=reason,
            deadline=deadline,
        )

    async def _suspend_for_sleep_owned(self) -> bool:
        """Actor-owned power transition; distinct from speech interruption."""
        self._power.mark_suspended()
        await self._spare.close()
        if not await self._lane.suspend_for_sleep():
            return False
        self._reject_queued_commands()
        self._output_active.clear()
        return not self._lane.isolated and self._lane.active is None

    def _admitting(self) -> bool:
        return not self._power.suspended and self._accepting.is_set()

    async def _resume_after_wake_owned(
        self,
        *,
        attempt_id: int,
        admission_generation: int,
    ) -> bool:
        """Apply actor admission only for the exact current wake generation."""
        return self._power.admit_actor_resume(
            attempt_id=attempt_id,
            admission_generation=admission_generation,
        )

    async def _submit_owned(
        self,
        *,
        row_id: int,
        event: Event,
        origin: str,
    ) -> MediaSubmitOutcome:
        queue_ = self._queue
        if queue_ is None or not self._accepting.is_set():
            return MediaSubmitOutcome("closed", event.event_uid)
        acknowledged: asyncio.Future[MediaSubmitOutcome] = (
            asyncio.get_running_loop().create_future()
        )
        try:
            queue_.put_nowait(
                MediaCommand(
                    row_id=row_id,
                    event=event,
                    origin=origin,
                    acknowledged=acknowledged,
                ),
            )
        except asyncio.QueueFull:
            return MediaSubmitOutcome(
                "overloaded",
                event.event_uid,
                detail="bounded media command queue is full",
            )
        return await acknowledged

    def _thread_main(self) -> None:  # noqa: C901 - explicit startup/cleanup phases
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._journal.open()
            if self._startup_abandoned.is_set():
                return
            self._queue = asyncio.Queue(
                maxsize=self._config.command_queue_capacity,
            )
            if self._start_player:
                self._set_startup_phase("starting_player")
                try:
                    started = self._player.start()
                except BaseException:
                    # An untyped exception from a device start cannot prove
                    # whether the foreign API retained a physical stream.
                    self._startup_device_disposition = "uncertain"
                    raise
                if isinstance(started, PlayerStartResult) and not started.started:
                    self._startup_device_disposition = (
                        "failed_closed"
                        if started.status == "failed_closed"
                        else "uncertain"
                    )
                    self._raise_player_start_debt(started)
                if self._startup_abandoned.is_set():
                    return
            self._set_startup_phase("running")
            self._ready.set()
            self._startup_finished.set()
            loop.run_until_complete(self._run_owned())
        except BaseException as exc:
            self._startup_error = exc
            self._startup_error_phase = self._get_startup_phase()
            self._request_shutdown_deadline(self._config.shutdown_timeout_s)
            self._startup_finished.set()
            LOGGER.exception("persistent media owner crashed")
        finally:
            self._request_shutdown_deadline(self._config.shutdown_timeout_s)
            self._startup_finished.set()
            pending = tuple(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                done, still_pending = loop.run_until_complete(
                    asyncio.wait(
                        pending,
                        timeout=self._clock.remaining_s(self._config.shutdown_timeout_s),
                    ),
                )
                if done:
                    loop.run_until_complete(asyncio.gather(*done, return_exceptions=True))
                if still_pending:
                    LOGGER.error(
                        "media owner tasks ignored bounded cancellation: %s",
                        ", ".join(task.get_name() for task in still_pending),
                    )
            self._stopper.stop_provider_bounded()
            self._stopper.stop_player_bounded()
            self._journal.close()
            loop.close()
            self._set_startup_phase("closed")
            self._closed.set()

    @staticmethod
    def _raise_player_start_debt(started: PlayerStartResult) -> None:
        """Convert a typed player debt into the actor startup exception path."""
        msg = f"player startup ownership not proven: {started.reason}"
        raise RuntimeError(msg)

    async def _run_owned(self) -> None:
        record_realtime_trace(
            "media_owner_started",
            boot_id=self._lane.boot_id,
            boot_high_water_id=self._lane.boot_high_water_id,
            command_queue_capacity=self._config.command_queue_capacity,
        )
        queue_ = self._queue
        if queue_ is None:  # pragma: no cover - constructor invariant
            return
        while not self._power.is_shutdown_requested():
            try:
                command = await asyncio.wait_for(queue_.get(), timeout=0.02)
            except TimeoutError:
                continue
            try:
                if not isinstance(command, MediaCommand):
                    continue
                try:
                    outcome = await self._handle_command(command)
                except Exception as exc:  # command isolation keeps owner resident
                    LOGGER.exception(
                        "media command failed: type=%s origin=%s",
                        command.event.type,
                        command.origin,
                    )
                    outcome = MediaSubmitOutcome(
                        "invalid",
                        command.event.event_uid,
                        detail=repr(exc),
                    )
                if not command.acknowledged.done():
                    command.acknowledged.set_result(outcome)
            finally:
                queue_.task_done()
            # A fail-closed terminal debt may request shutdown from inside
            # this command. Give the submit coroutine one loop turn to
            # observe its acknowledgement before the owner closes the loop.
            if self._power.is_shutdown_requested():
                await asyncio.sleep(0)
        await self._shutdown_owned()

    async def _handle_command(
        self,
        command: MediaCommand,
    ) -> MediaSubmitOutcome:
        return await self._lane.handle_command(command)

    def _remember_answer_language(self, language: lang.Language) -> None:
        """Remember the language of the answer's first segment: what the next spare reads."""
        self._last_answer_language = language

    async def _shutdown_owned(self) -> None:
        self._accepting.clear()
        await self._spare.close()
        await self._network_lost.close()
        if self._lane.active is not None:
            await self._lane.interrupt_active(reason="media_owner_shutdown")
        await self._say_fallback.drain()
        self._lane.discard_queued()
        self._stopper.stop_provider_bounded()
        self._stopper.stop_player_bounded()
        self._reject_queued_commands()
        # Rejected submitters are bridge coroutines on this loop. Let their
        # acknowledgement futures resume before loop teardown so a full normal
        # queue cannot strand callers while shutdown uses its control event.
        await asyncio.sleep(0)
        self._output_active.clear()
        record_realtime_trace(
            "media_owner_stopped",
            boot_id=self._lane.boot_id,
            owned_tasks_remaining=0,
        )

    def _reject_queued_commands(self) -> None:
        queue_ = self._queue
        if queue_ is None:
            return
        while True:
            try:
                command = queue_.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if isinstance(command, MediaCommand) and not command.acknowledged.done():
                    command.acknowledged.set_result(
                        MediaSubmitOutcome("closed", command.event.event_uid),
                    )
            finally:
                queue_.task_done()
