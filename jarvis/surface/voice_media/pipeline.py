"""The persistent L5 media owner: the one class the daemon builds, on its bounded bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Literal

from jarvis.shared import Event, lang
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_ledger import (
    ForegroundBusy,
    OutputTimelineSnapshot,
    StalePlaybackGeneration,
)
from jarvis.surface.voice_media.config import StreamingMediaConfig
from jarvis.surface.voice_media.contract import (
    MediaPowerTransitionResult,
    MediaSubmitOutcome,
    StreamingMediaStartupError,
    StreamingTTSProvider,
)
from jarvis.surface.voice_media.gain import OutputGain
from jarvis.surface.voice_media.journal import PlaybackJournal
from jarvis.surface.voice_media.network_lost import NetworkLostCue
from jarvis.surface.voice_media.pcm import SegmentResampler, cut_off_cue, head_dropped
from jarvis.surface.voice_media.player_stop import PlayerStopper
from jarvis.surface.voice_media.power import ActorPort, PowerController
from jarvis.surface.voice_media.registry import ActivePlaybackRegistry
from jarvis.surface.voice_media.response import (
    CHANNEL_TAG_RE,
    ActiveResponse,
    MediaCommand,
    ResponseBuffer,
    ResponseChunk,
)
from jarvis.surface.voice_media.ring_sink import RingSink
from jarvis.surface.voice_media.say_fallback import SayFallback
from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
from jarvis.surface.voice_media.spare import SpareSessions
from jarvis.surface.voice_tts import (
    AudioStreamPlayer,
    PlayerStartResult,
    TTSAudioChunk,
    TTSResponseSegment,
    TTSSegmentFinished,
    TTSSession,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator, Callable

    from jarvis.surface.voice_ducking import SystemAudioDucker

LOGGER = logging.getLogger(__name__)

_RESPONSE_TERMINAL_TYPES = frozenset({"response.cancelled", "response.failed"})
_RESPONSE_EVENT_TYPES = frozenset(
    {"surface.response_open", "surface.response_chunk", "surface.response_emitted"}
    | _RESPONSE_TERMINAL_TYPES,
)
_ANSWERED_GROUPS_LIMIT = 256
"""How many recent groups whose answer opened the media owner remembers."""

_TTS_SILENT_CHANNELS = frozenset({"queue_review", "silent_log", "badge_card"})


def _response_id(event: Event) -> str | None:
    value = event.payload.get("response_id")
    return value if isinstance(value, str) else None


class StreamingTTSPipeline:
    """One persistent async L5 media actor behind a bounded sync/async bridge."""

    def __init__(  # noqa: PLR0913, PLR0915 - explicit ownership dependencies
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
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._foreground_decision = foreground_decision_callable
        self._start_player = start_player
        self._silent_intent_channels = silent_intent_channels
        self._speaks_turn = speaks_turn
        self._registry = ActivePlaybackRegistry(
            boot_high_water_id=boot_high_water_id,
        )
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
            lane_isolated=lambda: self._lane_isolated,
            suspend_actor=lambda: self._suspend_for_sleep_owned(),  # noqa: PLW0108 - tests patch the method
            resume_actor=lambda *, attempt_id, admission_generation: self._resume_after_wake_owned(
                attempt_id=attempt_id,
                admission_generation=admission_generation,
            ),
        )
        self._lane_isolated = False
        self._terminal_debt: ActiveResponse | None = None
        self._event_cursor = boot_high_water_id
        self._responses: dict[str, ResponseBuffer] = {}
        self._after_drain: deque[ResponseBuffer] = deque()
        self._active: ActiveResponse | None = None
        # ADR 0053: while Allen's words are coming in no answer starts; it
        # parks here. A turn that ever started playing is never dropped.
        self._held = False
        # ADR 0054: the same parking while the audio devices are re-read.
        self._device_held = False
        self._parked: deque[ResponseBuffer] = deque()
        # ponytail: only grows, one short id per spoken turn; trim it if a
        # daemon ever runs for months.
        self._started_turns: set[str] = set()
        # ADR 0121: groups whose answer (any phase but commentary) has opened. A
        # wait line of such a group that has not started is dropped, never played
        # behind the answer. Insertion-ordered so the oldest can be trimmed.
        self._answered_groups: dict[str, None] = {}
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
            current=lambda: self._active,
        )
        self._say_fallback = SayFallback(
            player=player,
            config=self._config,
            clock=self._clock,
            ring=self._ring,
            current=lambda: self._active,
            isolate=self._isolate_fallback_cleanup,
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
        return self._registry.boot_high_water_id

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
                self._stop_foreground_output_owned(response_id, reason),
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
            asyncio.run_coroutine_threadsafe(self._hold_output_owned(held=held), loop)

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
                self._hold_for_devices_owned(held=held), loop,
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
            future = asyncio.run_coroutine_threadsafe(self._drop_unspoken_owned(turn_ids), loop)
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
            asyncio.run_coroutine_threadsafe(self._pause_owned(paused=paused), loop)

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
        if self._active is not None and not await self._interrupt_active(
            reason="system_sleep",
        ):
            return False
        while self._after_drain:
            response = self._after_drain.popleft()
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
        self._responses.clear()
        self._reject_queued_commands()
        self._output_active.clear()
        return not self._lane_isolated and self._active is None

    async def _stop_foreground_output_owned(self, response_id: str | None, reason: str) -> str:
        """Actor-owned stop of one named (or, for ``None``, the current) response's output."""
        active = self._active
        waiting = None if response_id is None else self._responses.get(response_id)
        if waiting is not None and (active is None or active.response is not waiting):
            # Named but not audible yet: queued behind another answer, parked
            # while Allen talks (ADR 0053) or still buffering. The surface
            # already shows it, so a stop there means it is never said.
            self._unschedule(waiting)
            self._responses.pop(waiting.response_id, None)
            self._registry.terminalize(waiting.response_id)
            self._record_dropped(waiting, reason=reason)
            self._broadcast_spoken(waiting, event_type="dropped")
            record_realtime_trace(
                "media_stop_foreground_waiting", response_id=waiting.response_id, reason=reason,
            )
            return "applied"
        if (
            active is None
            or (response_id is not None and active.response.response_id != response_id)
            or active.terminal_commit_pending
        ):
            if response_id is None:
                # The caller faded her out before asking; nothing is cut.
                self._gain.restore()
            record_realtime_trace(
                "media_stop_foreground_stale",
                response_id=response_id,
                active_response_id=None if active is None else active.response.response_id,
                terminal_commit_pending=active is not None and active.terminal_commit_pending,
            )
            return "stale"
        try:
            await self._gain.fade_out()
            if not await self._interrupt_active(reason=reason):
                return "uncertain"
        finally:
            self._gain.restore()
        # ADR-0006 D4: a user stop covers the queued speech of that group
        # too, and `_release_active(start_successor=False)` never advances
        # the lane, so an unpurged successor would sit unstartable.
        self._purge_after_drain()
        return "applied"

    async def _pause_owned(self, *, paused: bool) -> None:
        if paused:
            await self._gain.fade_out()
            self._player.pause_generation(paused=True)
            return
        self._player.pause_generation(paused=False)
        self._gain.restore()

    async def _hold_output_owned(self, *, held: bool) -> None:
        self._held = held
        if held:
            await self._spare.prewarm()
        if held or self._device_held:
            return
        parked, self._parked = self._parked, deque()
        for response in parked:
            await self._schedule_response(response)

    def _admitting(self) -> bool:
        return not self._power.suspended and self._accepting.is_set()

    async def _hold_for_devices_owned(self, *, held: bool) -> bool:
        self._device_held = held
        if not held and not self._held:
            parked, self._parked = self._parked, deque()
            for response in parked:
                await self._schedule_response(response)
        return self._active is None

    async def _drop_unspoken_owned(self, turn_ids: frozenset[str]) -> frozenset[str]:
        dropped = turn_ids - self._started_turns
        for response in list(self._responses.values()):
            if response.turn_id in dropped:
                self._unschedule(response)
                self._responses.pop(response.response_id, None)
                self._registry.terminalize(response.response_id)
        record_realtime_trace("media_unspoken_dropped", turns=",".join(sorted(dropped)))
        return dropped

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
            boot_id=self._registry.boot_id,
            boot_high_water_id=self._registry.boot_high_water_id,
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
        if command.row_id <= self._registry.boot_high_water_id:
            return MediaSubmitOutcome(
                "historical",
                command.event.event_uid,
                response_id=_response_id(command.event),
            )
        if command.row_id <= self._event_cursor:
            return MediaSubmitOutcome(
                "duplicate" if self._registry.has_seen(command.event.event_uid) else "invalid",
                command.event.event_uid,
                response_id=_response_id(command.event),
                detail="committed row already crossed by ordered media drain",
            )
        target_outcome: MediaSubmitOutcome | None = None
        while self._event_cursor < command.row_id:
            drained = False
            for row_id, event in self._journal.event_rows_through(
                self._event_cursor,
                command.row_id,
                self._config.event_drain_batch,
            ):
                drained = True
                self._event_cursor = row_id
                if event.type not in _RESPONSE_EVENT_TYPES:
                    continue
                outcome = await self._handle_event(row_id=row_id, event=event)
                if row_id == command.row_id:
                    if event.event_uid != command.event.event_uid:
                        return MediaSubmitOutcome(
                            "invalid",
                            command.event.event_uid,
                            detail="row id/event uid mismatch at media boundary",
                        )
                    target_outcome = outcome
            if not drained:
                break
        if target_outcome is not None:
            return target_outcome
        return MediaSubmitOutcome(
            "invalid",
            command.event.event_uid,
            response_id=_response_id(command.event),
            detail="hint row was absent or not a response event",
        )

    async def _handle_event(  # noqa: C901, PLR0911 - closed three-event dispatch
        self,
        *,
        row_id: int,
        event: Event,
    ) -> MediaSubmitOutcome:
        outcome = self._registry.classify(row_id=row_id, event=event)
        if outcome.status != "accepted":
            return outcome
        payload = event.payload
        response_id = str(payload["response_id"])
        if self._power.suspended:
            self._registry.terminalize(response_id)
            self._responses.pop(response_id, None)
            return MediaSubmitOutcome(
                "closed",
                event.event_uid,
                response_id,
                "system power transition suspended output",
            )
        if event.type == "surface.response_open":
            # The drain replays every row up to the submitted one, including those of a
            # turn the watcher refused to submit, so the verdict is read here too: by
            # where the turn was submitted from, which the open header never carries.
            if payload.get("attention_channel") in _TTS_SILENT_CHANNELS or (
                self._silent_intent_channels
                and self._journal.intent_channel(str(payload.get("turn_id", "")))
                in self._silent_intent_channels
            ) or (
                self._speaks_turn is not None
                and not self._journal.speaks(str(payload.get("turn_id", "")), self._speaks_turn)
            ):
                self._registry.terminalize(response_id)
                return outcome
            response_group_raw = payload.get("response_group_id")
            if not isinstance(response_group_raw, str) or not response_group_raw:
                self._registry.terminalize(response_id)
                return MediaSubmitOutcome(
                    "invalid",
                    event.event_uid,
                    response_id,
                    "streaming response open requires stable response_group_id",
                )
            opened = ResponseBuffer(
                row_id=row_id,
                source_event_id=event.event_uid,
                response_id=response_id,
                response_group_id=response_group_raw,
                turn_id=str(payload.get("turn_id", "")),
                phase=str(payload.get("phase", "final")),
                channel=str(payload.get("channel", "both")),
                gate_mode=str(payload.get("required_gate_mode", "sentence")),
                stream=payload.get("kind") == "stream",
            )
            if not self._admit_open(opened):
                return outcome
            self._responses[response_id] = opened
            # Only a stream open's chunks are single gate-permitted segments.
            self._responses[response_id].incremental = (
                self._config.speak_from_segments and self._responses[response_id].stream
            )
            return outcome
        response = self._responses.get(response_id)
        if response is None:
            return MediaSubmitOutcome("unregistered", event.event_uid, response_id)
        if event.type == "surface.response_chunk":
            sequence = int(payload["sequence"])
            text = str(payload.get("text", ""))
            prospective = sum(
                len(item.raw_text.encode()) for item in response.chunks.values()
            )
            prospective += len(text.encode())
            if prospective > self._config.response_text_bytes:
                self._registry.terminalize(response_id)
                self._responses.pop(response_id, None)
                return MediaSubmitOutcome(
                    "overloaded",
                    event.event_uid,
                    response_id,
                    "bounded response text budget exceeded",
                )
            segment_hash_raw = payload.get("segment_hash")
            segment_hash = (
                segment_hash_raw
                if isinstance(segment_hash_raw, str) and segment_hash_raw
                else hashlib.sha256(text.encode()).hexdigest()
            )
            response.chunks[sequence] = ResponseChunk(
                sequence=sequence,
                raw_text=text,
                segment_hash=segment_hash,
                source_event_uid=event.event_uid,
            )
            await self._chunk_buffered(response, text)
            return outcome
        if event.type == "surface.response_emitted":
            await self._response_emitted(response, event)
        else:
            await self._response_terminal(response, event)
        return outcome

    async def _response_terminal(self, response: ResponseBuffer, event: Event) -> None:
        """L3 ended the run: stop an active playback, or drop a buffered one."""
        reason = event.type.replace(".", "_")
        active = self._active
        if active is not None and active.response is response:
            if active.terminal_commit_pending:
                # A media-side terminal is already in flight and owns this
                # response's whole exit: it will release the lease, advance the
                # queue and clear `_output_active` itself. Re-entering
                # `_interrupt_active` would cancel `_play_response` inside its
                # own `finally`, skipping `leave_output()` and leaving the
                # system output lease held, then emit a contradictory `spoken`
                # frame once the CAS answers `AlreadyTerminal`. Clearing
                # `_output_active` here is just as wrong: audio is still
                # playing until that terminal lands, and `is_speaking()` feeds
                # the wake listener.
                return
            await self._interrupt_active(reason=reason)
            self._purge_after_drain()
            self._output_active.clear()
            return
        self._unschedule(response)
        self._responses.pop(response.response_id, None)
        self._registry.terminalize(response.response_id)
        self._record_dropped(response, reason=reason)
        self._broadcast_spoken(response, event_type="dropped")

    async def _response_emitted(self, response: ResponseBuffer, event: Event) -> None:
        """Complete the buffer; a scheduled response finishes its own segment loop."""
        response.emitted = True
        response.source_event_id = event.event_uid
        response.append_voice_suffix(
            event.payload.get("voice_text"),
            source_event_uid=event.event_uid,
        )
        response.updated.set()
        if not response.scheduled:
            await self._schedule_response(response)

    async def _chunk_buffered(self, response: ResponseBuffer, text: str) -> None:
        """Wake the segment loop; a tag ends incremental speaking for this response."""
        response.updated.set()
        if response.stream and CHANNEL_TAG_RE.search(text) is not None:
            active = self._active
            if active is not None and active.response is response:
                response.tagged = True
            else:
                response.incremental = False
                self._unschedule(response)
        if response.incremental and not response.scheduled and response.speech_text():
            response.scheduled = True
            await self._schedule_response(response)

    def _purge_after_drain(self) -> None:
        """Drop every queued successor a foreground grant has displaced."""
        while self._after_drain:
            old = self._after_drain.popleft()
            self._registry.terminalize(old.response_id)
            self._responses.pop(old.response_id, None)
            # Its text may already be on screen: the surface ends its turn on `spoken`.
            self._record_dropped(old, reason="displaced")
            self._broadcast_spoken(old, event_type="dropped")

    def _admit_open(self, opened: ResponseBuffer) -> bool:
        """Apply ADR 0121 to a response's open row; ``False`` when it is dropped.

        A wait line whose group's answer is already out would queue behind the
        answer and be heard after all of it, so it never starts. An answer's open
        drops the group's wait lines that have not started.
        """
        if opened.phase != "commentary":
            self._answer_opened(opened.response_group_id)
            return True
        if opened.response_group_id not in self._answered_groups:
            return True
        self._registry.terminalize(opened.response_id)
        self._record_dropped(opened, reason="answer_started")
        return False

    def _answer_opened(self, group_id: str) -> None:
        """Note that this group's answer opened and drop its unplayed wait lines.

        A line already playing is left to finish: interrupting it would also
        purge the answer queued behind it.
        """
        self._answered_groups[group_id] = None
        while len(self._answered_groups) > _ANSWERED_GROUPS_LIMIT:
            del self._answered_groups[next(iter(self._answered_groups))]
        for line in list(self._responses.values()):
            if (
                line.phase == "commentary"
                and line.response_group_id == group_id
                and not (self._active is not None and self._active.response is line)
            ):
                self._unschedule(line)
                self._responses.pop(line.response_id, None)
                self._registry.terminalize(line.response_id)
                self._record_dropped(line, reason="answer_started")

    def _unschedule(self, response: ResponseBuffer) -> None:
        response.scheduled = False
        if response in self._after_drain:
            self._after_drain.remove(response)
        if response in self._parked:
            self._parked.remove(response)

    async def _schedule_response(  # noqa: C901, PLR0911 - explicit lane disposition table
        self,
        response: ResponseBuffer,
    ) -> None:
        speech = response.speech_text()
        if not speech:
            self._responses.pop(response.response_id, None)
            self._registry.terminalize(response.response_id)
            self._broadcast_spoken(response)
            return
        active = self._active
        if active is None:
            self._start_response(response)
            return
        if (
            self._foreground_decision is not None
            and active.response.response_group_id != response.response_group_id
        ):
            # ADR-0008 D8: a cross-group candidate takes the lane only when
            # L3 policy selects it; arriving is not permission.
            verdict = self._foreground_decision(
                active.response.response_group_id,
                active.response.row_id,
                response.response_group_id,
                response.row_id,
            )
            if verdict == "enqueue_after_drain":
                # ADR 0107: another turn's answer
                # waits until she has finished, and is not lost for arriving second.
                self._enqueue_after_drain(response)
                return
            if verdict != "supersede":
                self._registry.terminalize(response.response_id)
                self._responses.pop(response.response_id, None)
                record_realtime_trace(
                    "media_foreground_declined",
                    response_id=response.response_id,
                    response_group_id=response.response_group_id,
                    incumbent_response_id=active.response.response_id,
                    verdict=verdict,
                    terminal_commit_pending=active.terminal_commit_pending,
                )
                return
            if active.terminal_commit_pending:
                # A tombstoned incumbent is draining, not occupying: the
                # player released its lease at complete/interrupt_generation,
                # so the winner may take the lane and only has to wait for the
                # terminal debt below. D8 displaces a GROUP, so the grant
                # discards that group's queued continuation exactly as the
                # live-incumbent branch does -- otherwise which meaning of
                # "supersede" applies would turn on sub-millisecond timing.
                purged = len(self._after_drain)
                self._purge_after_drain()
                record_realtime_trace(
                    "media_foreground_draining_lane_granted",
                    response_id=response.response_id,
                    response_group_id=response.response_group_id,
                    incumbent_response_id=active.response.response_id,
                    purged_lane_depth=purged,
                )
        if active.terminal_commit_pending:
            if len(self._after_drain) >= self._config.response_lane_capacity:
                self._registry.terminalize(response.response_id)
                self._responses.pop(response.response_id, None)
                return
            # A generation whose PCM eligibility is already tombstoned still
            # owns the lane until its terminal is durable. Queue every
            # successor behind that debt; never reinterpret a late foreground
            # event as permission to mint a generation concurrently.
            self._after_drain.append(response)
            record_realtime_trace(
                "media_terminal_debt_wait_enqueued",
                response_id=response.response_id,
                response_group_id=response.response_group_id,
                lane_depth=len(self._after_drain),
            )
            return
        if active.response.response_group_id == response.response_group_id:
            self._enqueue_after_drain(response, behind=active.response.response_group_id)
            return
        if not await self._interrupt_active(reason="foreground_superseded"):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        self._purge_after_drain()
        self._start_response(response)

    def _enqueue_after_drain(self, response: ResponseBuffer, *, behind: str | None = None) -> None:
        """Queue ``response`` for the lane; ``behind`` keeps that group's answer together.

        The playing answer's own continuation goes ahead of another turn's
        answer already waiting, so a turn is said whole before the next.
        """
        if len(self._after_drain) >= self._config.response_lane_capacity:
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            record_realtime_trace(
                "media_after_drain_overflow",
                response_id=response.response_id,
                response_group_id=response.response_group_id,
                capacity=self._config.response_lane_capacity,
            )
            return
        index = next(
            (
                position
                for position, queued in enumerate(self._after_drain)
                if behind is not None and queued.response_group_id != behind
            ),
            len(self._after_drain),
        )
        self._after_drain.insert(index, response)
        record_realtime_trace(
            "media_after_drain_enqueued",
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            phase=response.phase,
            lane_depth=len(self._after_drain),
        )

    def _start_response(self, response: ResponseBuffer) -> None:
        if (
            self._power.suspended
            or self._lane_isolated
            or self._power.is_shutdown_requested()
        ):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        if self._held or self._device_held:
            # ADR 0053: Allen is talking; the answer starts when he stops.
            # ADR 0054: or the player is closed while the devices are re-read.
            self._parked.append(response)
            self._output_active.clear()
            return
        self._started_turns.add(response.turn_id)
        result = self._player.activate_generation(
            session_id=self._registry.boot_id,
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            turn_id=response.turn_id,
        )
        if isinstance(result, ForegroundBusy):
            msg = "media owner/player foreground state diverged"
            raise RuntimeError(msg)  # noqa: TRY004 - runtime state, not caller type
        active = ActiveResponse(
            response=response,
            lease=result,
            starvation_gaps_at_start=self._player.starvation_gaps,
            host_underflows_at_start=self._player.underflow_count,
        )
        self._active = active
        self._output_active.set()
        active.task = asyncio.create_task(
            self._play_response(active),
            name=f"media-response-{response.response_id}",
        )
        active.presentation_task = asyncio.create_task(
            self._presentation_owner(active),
            name=f"media-presentation-{response.response_id}",
        )
        active.task.add_done_callback(self._response_task_done)
        record_realtime_trace(
            "tts_request_started",
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            playback_generation_id=result.playback_generation_id,
            phase=response.phase,
            measurement_semantics="media_owner_started_provider_request",
        )

    async def _play_response(  # noqa: C901 - bounded lifecycle cleanup FSM
        self,
        active: ActiveResponse,
    ) -> None:
        response = active.response
        live = response.incremental and not response.emitted
        segments = response.speech_segments()
        committed = segments[0][1] if live and segments else response.speech_text()
        speech_hash = hashlib.sha256(committed.encode()).hexdigest()
        if self._ducker is not None:
            self._ducker.enter_output()
            active.output_lease = True
        try:
            activation = self._journal.playback_started(
                payload={
                    "session_id": active.lease.session_id,
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "playback_generation_id": active.lease.playback_generation_id,
                    "phase": response.phase,
                    "channel": response.channel,
                    "speech_text_hash": speech_hash,
                    "estimated_output_latency_ns": (
                        self._player.estimated_output_latency_ns
                    ),
                    **({"incremental": True} if live else {}),
                },
                source_event_id=response.source_event_id,
                correlation={"turn_id": response.turn_id},
            )
            active.activation_event_uid = activation.event_uid
            async with asyncio.timeout(self._config.response_timeout_s) as budget:
                completed = await self._stream_with_prefix_fallback(
                    active,
                    segments,
                    budget=budget,
                    live=live,
                )
                if active.advance_after_cleanup or self._active is not active:
                    return
                if not completed:
                    await self._fail_active(
                        active,
                        reason="tts_fallback_unavailable",
                        retryable=True,
                    )
                    return
                if live:
                    speech_hash = hashlib.sha256(active.prepared_text.encode()).hexdigest()
                await self._drain_and_complete(active, segment_hash=speech_hash)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            LOGGER.warning(
                "streaming TTS response %s exceeded response_timeout_s=%.3f for one segment",
                response.response_id,
                self._config.response_timeout_s,
            )
            await self._fail_active(active, reason="tts_response_timeout", retryable=True)
        except Exception as exc:  # noqa: BLE001 - provider adapters fail heterogeneously
            LOGGER.warning("streaming TTS response failed: %r", exc)
            await self._fail_active(
                active,
                reason=f"tts_provider_error:{type(exc).__name__}",
                retryable=True,
            )
        finally:
            session = active.session
            active.session = None
            if session is not None:
                close_task = asyncio.create_task(session.close())
                await self._clock.wait_task_bounded(close_task, timeout_s=0.5)
            # Every exit path retains and drains fallback process ownership;
            # an exceptional ``wait()`` must not let a still-live ``say``
            # overlap the next generation.
            await self._say_fallback.cancel(active)
            if active.output_lease and self._ducker is not None:
                active.output_lease = False
                with contextlib.suppress(Exception):
                    self._ducker.leave_output()
            if active.advance_after_cleanup:
                active.advance_after_cleanup = False
                if self._active is active:
                    self._active = None
                self._advance_after_drain()

    async def _presentation_owner(self, active: ActiveResponse) -> None:
        """Persist whole-segment heard checkpoints while provider I/O continues."""
        try:
            while self._active is active and not self._lane_isolated:
                snapshot = self._player.poll_generation(
                    active.lease.playback_generation_id,
                )
                if isinstance(snapshot, StalePlaybackGeneration):
                    return
                await self._checkpoint_if_advanced(active, snapshot)
                self._broadcast_position(active, snapshot)
                await asyncio.sleep(self._config.presentation_poll_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "presentation checkpoint owner failed: response_id=%s generation=%d",
                active.response.response_id,
                active.lease.playback_generation_id,
            )

    async def _stream_with_prefix_fallback(  # noqa: C901, PLR0911, PLR0912, PLR0915 - bounded endpoint FSM
        self,
        active: ActiveResponse,
        segments: list[tuple[int, str, str]],
        *,
        budget: asyncio.Timeout,
        live: bool,
    ) -> bool:
        """Walk ``segments``; while ``live`` the list grows until the buffer is emitted."""
        lease = active.lease
        accepted_total = 0
        trimmer = self._ring.new_silence_trimmer()
        last_error: BaseException | None = None
        endpoint_index = 0
        session: TTSSession | None = None
        iterator: AsyncIterator[TTSAudioChunk | TTSSegmentFinished] | None = None
        provider_first = False
        segment_index = 0
        while True:
            if segment_index >= len(segments) and not (
                live and await self._ring.await_segments(active, segments)
            ):
                break
            if live and active.response.tagged:
                await self._fail_active(active, reason="stream_chunk_tagged", retryable=False)
                return False
            sequence, text, segment_hash = segments[segment_index]
            # Opened after this segment's wait and closed at the next segment's
            # reschedule, so the bound covers one segment's provider I/O plus its
            # backpressured playback - plus the drain, for the final segment -
            # never the whole generation, whose length is deliberately unbounded.
            budget.reschedule(
                asyncio.get_running_loop().time() + self._config.response_timeout_s,
            )
            self._ring.prepare_segment(active, sequence, text, segment_hash)
            opened = self._player.begin_generation_segment(
                expected_playback_generation_id=lease.playback_generation_id,
                sequence=sequence,
                text=text,
                segment_hash=segment_hash,
            )
            if isinstance(opened, StalePlaybackGeneration):
                return False
            segment_start = accepted_total
            accepted_segment = 0
            while endpoint_index < self._provider.streaming_candidate_count:
                if self._active is not active:
                    return False
                try:
                    if session is None:
                        language = self._last_answer_language = lang.text_language(text)
                        session = (
                            await self._spare.take(language) if endpoint_index == 0 else None
                        ) or self._provider.create_tts_session(
                            endpoint_index=endpoint_index,
                            language=language,
                            idle_close_s=self._config.session_idle_close_s,
                            command_queue_capacity=self._config.session_command_capacity,
                            audio_queue_capacity=self._config.session_audio_capacity,
                        )
                        active.session = session
                        await session.open(
                            active.response.response_id,
                            lease.playback_generation_id,
                        )
                        iterator = session.audio_events().__aiter__()
                    await session.send(
                        TTSResponseSegment(
                            response_id=active.response.response_id,
                            playback_generation_id=lease.playback_generation_id,
                            sequence=sequence,
                            text=text,
                        ),
                    )
                    resampler: SegmentResampler | None = None
                    pending_alignment: list[TTSAudioChunk] = []
                    if trimmer is not None:
                        trimmer.begin_segment(text)
                    while True:
                        if iterator is None:  # pragma: no cover - session invariant
                            msg = "opened TTS session has no audio iterator"
                            raise RuntimeError(msg)  # noqa: TRY301 - provider failure lane
                        event = await iterator.__anext__()
                        if event.sequence != sequence:
                            msg = (
                                f"provider segment sequence {event.sequence} does not "
                                f"match active sequence {sequence}"
                            )
                            raise RuntimeError(msg)  # noqa: TRY301 - provider failure lane
                        if isinstance(event, TTSAudioChunk):
                            if event.word_boundaries and event.timing_text == text:
                                pending_alignment.append(event)
                            if not event.pcm:
                                continue
                            if resampler is None:
                                resampler = SegmentResampler(
                                    input_rate_hz=event.sample_rate_hz,
                                    output_rate_hz=self._config.canonical_sample_rate_hz,
                                )
                            if not provider_first:
                                provider_first = True
                                record_realtime_trace(
                                    "tts_provider_first_pcm_received",
                                    response_id=active.response.response_id,
                                    playback_generation_id=lease.playback_generation_id,
                                    endpoint_index=endpoint_index,
                                    pcm_bytes=len(event.pcm),
                                    measurement_semantics=(
                                        "first_nonempty_provider_pcm_event_received_by_media_owner"
                                    ),
                                )
                            canonical = resampler.feed(event.pcm)
                            if trimmer is not None:
                                canonical = trimmer.feed(canonical)
                            written = await self._ring.write_all(
                                active,
                                canonical,
                                sequence=sequence,
                            )
                            accepted_segment += written
                            accepted_total += written
                            continue
                        tail = resampler.finish() if resampler is not None else b""
                        if trimmer is not None:
                            tail = trimmer.feed(tail) + trimmer.end_segment()
                        if tail:
                            written = await self._ring.write_all(active, tail, sequence=sequence)
                            accepted_segment += written
                            accepted_total += written
                        if accepted_segment and pending_alignment:
                            self._ring.record_alignment(
                                active, pending_alignment, segment_start=segment_start,
                                segment_samples=accepted_segment, trimmed=head_dropped(trimmer),
                            )
                        finished = self._player.finish_generation_segment(
                            expected_playback_generation_id=lease.playback_generation_id,
                            sequence=sequence,
                        )
                        if isinstance(finished, StalePlaybackGeneration):
                            return False
                        record_realtime_trace(
                            "tts_provider_segment_final",
                            response_id=active.response.response_id,
                            playback_generation_id=lease.playback_generation_id,
                            segment_sequence=sequence,
                            accepted_segment_samples=accepted_segment,
                            usage_present=event.usage is not None,
                            measurement_semantics=(
                                "provider_segment_final_received_not_playback_complete"
                            ),
                        )
                        self._journal.tts_usage(
                            event.usage,
                            response_id=active.response.response_id,
                            sequence=sequence,
                        )
                        self._network_lost.prefetch()
                        break
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider transport boundary
                    last_error = exc
                    if session is not None:
                        close_task = asyncio.create_task(session.close())
                        await self._clock.wait_task_bounded(close_task, timeout_s=0.5)
                    session = None
                    iterator = None
                    active.session = None
                    if accepted_segment > 0:
                        # ADR-0006 D5: a segment heard in part is never said
                        # again. What she said plays out, and a cue tells Allen
                        # her voice broke rather than leaving him in silence.
                        await self._play_cut_off_cue(active, sequence=sequence)
                        await self._fail_active(
                            active,
                            reason="partial_tts_provider_failure",
                            retryable=False,
                        )
                        return False
                    record_realtime_trace(
                        "tts_prefix_safe_endpoint_fallback",
                        response_id=active.response.response_id,
                        playback_generation_id=lease.playback_generation_id,
                        segment_sequence=sequence,
                        failed_endpoint_index=endpoint_index,
                        accepted_samples=0,
                        error_type=type(exc).__name__,
                    )
                    endpoint_index += 1
            else:
                if accepted_total > 0:
                    # The same break between two sentences: no endpoint can say
                    # the next one, and waiting for the whole text to hand it to
                    # `say` waits on a model that is offline too (Wi-Fi off,
                    # 2026-10-01: silence until Allen spoke).
                    await self._play_cut_off_cue(active, sequence=sequence)
                    await self._fail_active(
                        active,
                        reason="partial_tts_provider_failure",
                        retryable=False,
                    )
                    return False
                if self._config.enable_macos_say_fallback:
                    while live and await self._ring.await_segments(active, segments):
                        if active.response.tagged:
                            break
                        budget.reschedule(
                            asyncio.get_running_loop().time()
                            + self._config.response_timeout_s,
                        )
                    if live and active.response.tagged:
                        await self._fail_active(
                            active,
                            reason="stream_chunk_tagged",
                            retryable=False,
                        )
                        return False
                    remaining = segments[segment_index:]
                    return await self._say_fallback.run(active, remaining, budget=budget)
                if last_error is not None:
                    raise last_error
                return False
            segment_index += 1
        if not segments:
            return False
        record_realtime_trace(
            "tts_provider_final",
            response_id=active.response.response_id,
            playback_generation_id=lease.playback_generation_id,
            accepted_samples=accepted_total,
            measurement_semantics="final_provider_segment_received_not_playback_complete",
        )
        if session is not None:
            with contextlib.suppress(Exception):
                await session.finish()
            active.session = None
        return True

    async def _play_cut_off_cue(self, active: ActiveResponse, *, sequence: int) -> None:
        """Play what was accepted to its end, then her spoken line or tones, before the terminal."""
        line = self._network_lost.line(lang.language())
        cue = line or cut_off_cue(self._config.canonical_sample_rate_hz)
        await self._ring.write_all(active, cue, sequence=sequence)
        record_realtime_trace(
            "tts_cut_off_cue",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            segment_sequence=sequence,
            cue="spoken_line" if line else "tones",
        )
        while self._active is active:
            snapshot = self._player.poll_generation(active.lease.playback_generation_id)
            if isinstance(snapshot, StalePlaybackGeneration):
                return
            await self._checkpoint_if_advanced(active, snapshot)
            if snapshot.estimated_audible_samples >= snapshot.accepted_samples:
                return
            await asyncio.sleep(self._config.presentation_poll_s)

    def _isolate_fallback_cleanup(self, active: ActiveResponse) -> None:
        """Fail closed when exact subprocess quiescence cannot be established."""
        self._lane_isolated = True
        self._accepting.clear()
        active.advance_after_cleanup = False
        record_realtime_trace(
            "media_fallback_cleanup_isolated",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
        )
        # No terminal was being attempted and no exception reached here: the
        # janitor simply could not prove the `say` subprocess was quiescent.
        self._emit_lane_isolated(
            active,
            terminal_type=None,
            error_type=None,
            isolation_reason="fallback_cleanup_unproven",
        )
        self._request_shutdown_deadline(self._config.shutdown_timeout_s)

    async def _drain_and_complete(
        self,
        active: ActiveResponse,
        *,
        segment_hash: str,
    ) -> None:
        lease = active.lease
        software_drain_recorded = False
        while self._active is active:
            snapshot = self._player.poll_generation(lease.playback_generation_id)
            if isinstance(snapshot, StalePlaybackGeneration):
                await self._fail_active(
                    active,
                    reason="playback_ledger_lost_before_completion",
                    retryable=True,
                )
                return
            await self._checkpoint_if_advanced(active, snapshot)
            if snapshot.software_drained and not software_drain_recorded:
                software_drain_recorded = True
                record_realtime_trace(
                    "tts_software_drain",
                    response_id=active.response.response_id,
                    playback_generation_id=lease.playback_generation_id,
                    submitted_samples=snapshot.submitted_samples,
                    measurement_semantics=("generation_ring_empty_not_physical_dac_completion"),
                )
            if snapshot.fully_presented:
                record_realtime_trace(
                    "tts_estimated_audible",
                    response_id=active.response.response_id,
                    playback_generation_id=lease.playback_generation_id,
                    estimated_audible_samples=snapshot.estimated_audible_samples,
                    cursor_quality=snapshot.cursor_quality,
                    measurement_semantics=(
                        "full_conservative_callback_plus_output_latency_horizon_not_loopback"
                    ),
                )
                active.terminal_commit_pending = True
                completed = self._player.complete_generation(
                    expected_playback_generation_id=lease.playback_generation_id,
                )
                if isinstance(completed, StalePlaybackGeneration):
                    await self._fail_active(
                        active,
                        reason="playback_generation_lost_before_completion",
                        retryable=True,
                    )
                    return
                durable = await self._commit_terminal_durable(
                    active,
                    event_type="surface.playback_completed",
                    snapshot=completed,
                    reason=None,
                    speech_text_hash=segment_hash,
                    retryable=None,
                )
                if durable:
                    await self._release_active(
                        active,
                        event_type="surface.playback_completed",
                        start_successor=True,
                    )
                return
            await asyncio.sleep(self._config.presentation_poll_s)

    async def _fail_active(
        self,
        active: ActiveResponse,
        *,
        reason: str,
        retryable: bool,
    ) -> None:
        # An interrupt already owns this response's terminal: a response task that
        # sees the tombstone while the interrupt settles must not race it to a failure.
        if self._active is not active or active.terminal_commit_pending:
            return
        active.terminal_commit_pending = True
        snapshot = await self._interrupt_snapshot(active)
        if snapshot is None:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_failed",
                error=RuntimeError("callback publication did not settle"),
                isolation_reason="callback_publication_unsettled",
            )
            return
        durable = await self._commit_terminal_durable(
            active,
            event_type="surface.playback_failed",
            snapshot=snapshot,
            reason=reason,
            speech_text_hash=None,
            retryable=retryable,
        )
        if durable:
            await self._release_active(
                active,
                event_type="surface.playback_failed",
                start_successor=True,
            )

    async def _interrupt_active(self, *, reason: str) -> bool:
        active = self._active
        if active is None:
            return not self._lane_isolated
        active.terminal_commit_pending = True
        # Generation tombstone is the first irreversible publication.  Only
        # after it lands do we cancel network/fallback work.
        snapshot = await self._interrupt_snapshot(active)
        if snapshot is not None:
            # The caption stops now, not after the terminal commit.
            self._broadcast_position(active, snapshot, stopped=True)
        await self._cancel_active_io(active, reason=reason)
        if snapshot is None:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_interrupted",
                error=RuntimeError("callback publication did not settle"),
                isolation_reason="callback_publication_unsettled",
            )
            return False
        durable = await self._commit_terminal_durable(
            active,
            event_type="surface.playback_interrupted",
            snapshot=snapshot,
            reason=reason,
            speech_text_hash=None,
            retryable=None,
        )
        if not durable:
            return False
        await self._release_active(
            active,
            event_type="surface.playback_interrupted",
            start_successor=False,
        )
        return True

    async def _cancel_active_io(self, active: ActiveResponse, *, reason: str) -> None:
        task = active.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        session = active.session
        if session is not None:
            abort_task = asyncio.create_task(session.abort(reason))
            await self._clock.wait_task_bounded(abort_task, timeout_s=0.5)
        await self._say_fallback.cancel(active)
        if task is not None and task is not asyncio.current_task():
            await self._clock.wait_task_bounded(task, timeout_s=0.5)

    async def _checkpoint_if_advanced(
        self,
        active: ActiveResponse,
        snapshot: OutputTimelineSnapshot,
    ) -> None:
        sequence = snapshot.heard_through_sequence
        if not snapshot.heard_text or snapshot.heard_text == active.last_checkpoint_text:
            return
        for attempt in range(1, self._config.checkpoint_retry_attempts + 1):
            if self._journal.checkpoint_recorded(
                response_id=active.response.response_id,
                playback_generation_id=active.lease.playback_generation_id,
                session_id=active.lease.session_id,
                heard_text_hash=snapshot.heard_text_hash,
            ):
                active.last_checkpoint_text = snapshot.heard_text
                return
            try:
                self._journal.playback_checkpoint(
                    payload={
                        "session_id": active.lease.session_id,
                        "response_id": active.response.response_id,
                        "turn_id": active.response.turn_id,
                        "playback_generation_id": active.lease.playback_generation_id,
                        "heard_through_sequence": sequence,
                        "submitted_samples": snapshot.submitted_samples,
                        "estimated_audible_samples": snapshot.estimated_audible_samples,
                        "heard_partial_sequence": snapshot.heard_partial_sequence,
                        "heard_partial_text_end": snapshot.heard_partial_text_end,
                        "heard_text_hash": snapshot.heard_text_hash,
                        "heard_text": snapshot.heard_text,
                        "cursor_quality": snapshot.cursor_quality,
                    },
                    source_event_id=active.activation_event_uid or active.response.source_event_id,
                    correlation={"turn_id": active.response.turn_id},
                )
            except Exception:
                LOGGER.exception(
                    "playback checkpoint append failed: response_id=%s generation=%d attempt=%d",
                    active.response.response_id,
                    active.lease.playback_generation_id,
                    attempt,
                )
                if attempt < self._config.checkpoint_retry_attempts:
                    await asyncio.sleep(self._config.durability_retry_s)
                continue
            active.last_checkpoint_text = snapshot.heard_text
            return

    async def _commit_terminal_durable(  # noqa: PLR0913 - mirrors terminal payload owner
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        snapshot: OutputTimelineSnapshot,
        reason: str | None,
        speech_text_hash: str | None,
        retryable: bool | None,
    ) -> bool:
        """Block successor minting on bounded retry of the Wave 1 terminal CAS."""
        last_error: BaseException | None = None
        for attempt in range(1, self._config.terminal_retry_attempts + 1):
            try:
                self._journal.commit_terminal(
                    active,
                    event_type=event_type,
                    snapshot=snapshot,
                    reason=reason,
                    speech_text_hash=speech_text_hash,
                    retryable=retryable,
                    starvation_gaps=(
                        self._player.starvation_gaps - active.starvation_gaps_at_start
                    ),
                    host_underflows=(
                        self._player.underflow_count - active.host_underflows_at_start
                    ),
                    tail_ramp_samples=self._player.tail_ramp_samples,
                )
                self._registry.terminalize(active.response.response_id)
            except Exception as exc:
                last_error = exc
                LOGGER.exception(
                    "playback terminal append failed: type=%s response_id=%s "
                    "generation=%d attempt=%d",
                    event_type,
                    active.response.response_id,
                    active.lease.playback_generation_id,
                    attempt,
                )
                if attempt < self._config.terminal_retry_attempts:
                    delay = self._clock.remaining_s(self._config.durability_retry_s)
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                continue
            return True
        self._isolate_terminal_debt(
            active,
            event_type=event_type,
            error=last_error or RuntimeError("terminal append exhausted retries"),
            isolation_reason="terminal_append_exhausted",
        )
        return False

    async def _interrupt_snapshot(
        self,
        active: ActiveResponse,
    ) -> OutputTimelineSnapshot | None:
        """Tombstone first, then settle the callback publication linearization."""
        self._player.interrupt_generation(
            expected_playback_generation_id=active.lease.playback_generation_id,
        )
        deadline = asyncio.get_running_loop().time() + self._clock.remaining_s(0.5)
        while asyncio.get_running_loop().time() < deadline:
            settled = self._player.settle_interrupted_generation(
                expected_playback_generation_id=active.lease.playback_generation_id,
            )
            if settled is None:
                await asyncio.sleep(0)
                continue
            return None if isinstance(settled, StalePlaybackGeneration) else settled
        return None

    def _response_task_done(self, task: asyncio.Task[None]) -> None:
        """Fail closed if an unexpected task exception escaped lifecycle cleanup."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        LOGGER.error(
            "media response task escaped: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        active = self._active
        if active is None or active.task is not task:
            return
        if active.terminal_commit_pending:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_failed",
                error=error,
                isolation_reason="task_escaped",
            )
            return
        recovery = asyncio.create_task(
            self._fail_active(
                active,
                reason=f"media_owner_task_error:{type(error).__name__}",
                retryable=False,
            ),
            name=f"media-task-recovery-{active.response.response_id}",
        )
        recovery.add_done_callback(self._log_recovery_error)

    @staticmethod
    def _log_recovery_error(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error("media task recovery escaped: %r", error)

    async def _release_active(
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        start_successor: bool,
    ) -> None:
        """Retire/broadcast only after durable terminal commit."""
        if self._active is not active:
            return
        presentation = active.presentation_task
        if presentation is not None and presentation is not asyncio.current_task():
            presentation.cancel()
            await self._clock.wait_task_bounded(presentation, timeout_s=0.25)
        self._responses.pop(active.response.response_id, None)
        self._player.retire_generation(active.lease.playback_generation_id)
        self._broadcast_spoken(active.response, event_type=event_type)
        if start_successor and asyncio.current_task() is active.task:
            # Keep the durable-but-cleaning response as the lane owner. New
            # deliveries queue behind its terminal_commit_pending marker until
            # provider/process cleanup completes in `_play_response.finally`.
            active.advance_after_cleanup = True
            return
        self._active = None
        if not start_successor:
            self._output_active.clear()
            return
        self._advance_after_drain()

    def _advance_after_drain(self) -> None:
        if self._power.suspended or self._lane_isolated or self._active is not None:
            return
        if self._after_drain:
            self._start_response(self._after_drain.popleft())
        else:
            self._output_active.clear()

    def _record_dropped(self, response: ResponseBuffer, *, reason: str) -> None:
        """Leave the durable fact that none of ``response`` was spoken.

        Best effort: letting the answer go must not fail on its record.
        """
        try:
            self._journal.speech_dropped(
                payload={
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "reason": reason,
                },
                source_event_id=response.source_event_id,
                correlation={"turn_id": response.turn_id},
            )
        except Exception:  # noqa: BLE001 - see docstring
            LOGGER.warning(
                "could not record dropped speech %s", response.response_id, exc_info=True,
            )

    def _broadcast_position(
        self,
        active: ActiveResponse,
        snapshot: OutputTimelineSnapshot,
        *,
        stopped: bool = False,
    ) -> None:
        """Tell the screen where her voice is, when that changed (ADR 0112).

        Held (a soft barge-in being judged) or ``stopped``, the caption stops
        with her; nothing is said after a stop, which is the last report.
        """
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if not callable(callback) or active.position_stopped:
            return
        active.position_stopped = stopped
        held = stopped or self._player.generation_held(active.lease.playback_generation_id) is True
        position = (snapshot.played_letters, snapshot.playing_letters, held)
        if position == active.last_position or not isinstance(position[0], int):
            return
        active.last_position = position
        with contextlib.suppress(Exception):
            callback(
                "playing",
                turn_id=active.response.turn_id,
                played=position[0],
                ahead=position[1],
                held=held,
                **self._wait_line_mark(active.response),
            )

    @staticmethod
    def _wait_line_mark(response: ResponseBuffer) -> dict[str, str]:
        """``response_phase`` for a wait line's voice ops, nothing for an answer (ADR 0121)."""
        return {"response_phase": "commentary"} if response.phase == "commentary" else {}

    def _broadcast_spoken(
        self, response: ResponseBuffer, *, event_type: str = "no_speech",
    ) -> None:
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback(
                    "spoken",
                    turn_id=response.turn_id,
                    output_outcome=event_type.removeprefix("surface.playback_"),
                    **self._wait_line_mark(response),
                )

    def _emit_lane_isolated(
        self,
        active: ActiveResponse,
        *,
        terminal_type: str | None,
        error_type: str | None,
        isolation_reason: str,
    ) -> None:
        """Append the durable record that this lane will speak nothing again.

        Best effort by construction: ``_lane_isolated`` is never reset and no
        supervisor recreates the owner, so the process is deaf until restart.
        Isolation must never be prevented by its own record-keeping failing.
        """
        try:
            self._journal.lane_isolated(
                payload={
                    "session_id": active.lease.session_id,
                    "response_id": active.response.response_id,
                    "turn_id": active.response.turn_id,
                    "playback_generation_id": active.lease.playback_generation_id,
                    "terminal_type": terminal_type,
                    "error_type": error_type,
                    "isolation_reason": isolation_reason,
                },
                source_event_id=(
                    active.activation_event_uid or active.response.source_event_id
                ),
                correlation={"turn_id": active.response.turn_id},
            )
        except Exception:
            LOGGER.exception(
                "media lane isolation row could not be appended: "
                "response_id=%s generation=%d reason=%s",
                active.response.response_id,
                active.lease.playback_generation_id,
                isolation_reason,
            )

    def _isolate_terminal_debt(
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        error: BaseException,
        isolation_reason: str,
    ) -> None:
        """Fail closed: retain the ledger and forbid every successor generation."""
        self._lane_isolated = True
        self._terminal_debt = active
        self._accepting.clear()
        self._registry.terminalize(active.response.response_id)
        if self._active is active:
            self._active = None
        if active.presentation_task is not None:
            active.presentation_task.cancel()
        while self._after_drain:
            queued = self._after_drain.popleft()
            self._registry.terminalize(queued.response_id)
        self._responses.clear()
        self._output_active.clear()
        record_realtime_trace(
            "media_terminal_debt_isolated",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            terminal_type=event_type,
            error_type=type(error).__name__,
        )
        self._emit_lane_isolated(
            active,
            terminal_type=event_type,
            error_type=type(error).__name__,
            isolation_reason=isolation_reason,
        )
        self._request_shutdown_deadline(self._config.shutdown_timeout_s)

    async def _shutdown_owned(self) -> None:
        self._accepting.clear()
        await self._spare.close()
        await self._network_lost.close()
        if self._active is not None:
            await self._interrupt_active(reason="media_owner_shutdown")
        await self._say_fallback.drain()
        while self._after_drain:
            response = self._after_drain.popleft()
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
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
            boot_id=self._registry.boot_id,
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
