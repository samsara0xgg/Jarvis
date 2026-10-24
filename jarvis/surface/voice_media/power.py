"""The media owner's power state machine: sleep, wake, admit, abort and revoke."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_media.contract import MediaPowerTransitionResult
from jarvis.surface.voice_tts import PlayerStartResult, PlayerStopResult

if TYPE_CHECKING:
    import concurrent.futures
    from collections.abc import Callable, Coroutine

    from jarvis.surface.voice_media.player_stop import PlayerStopper
    from jarvis.surface.voice_tts import AudioStreamPlayer


class ActorResume(Protocol):
    """The owner's coroutine that admits the actor again for one wake generation."""

    def __call__(
        self,
        *,
        attempt_id: int,
        admission_generation: int,
    ) -> Coroutine[Any, Any, bool]:
        """Make the coroutine for ``attempt_id`` and ``admission_generation``."""


class ActorPort:
    """The power controller's one way onto the actor's loop (ADR 0383).

    The owner gives it the loop it has, if any, and whether the actor has finished; the
    controller asks ``live`` where it used to read the loop and the closed flag together, and
    submits a coroutine as it used to with ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        *,
        loop: Callable[[], asyncio.AbstractEventLoop | None],
        closed: Callable[[], bool],
    ) -> None:
        """Submit to the loop ``loop`` answers; ``closed`` says the actor has finished."""
        self._loop = loop
        self._closed = closed

    def live(self) -> bool:
        """True while the actor has a loop and has not finished."""
        return self._loop() is not None and not self._closed()

    def submit(self, coroutine: Coroutine[Any, Any, bool]) -> concurrent.futures.Future[bool]:
        """Run ``coroutine`` on the actor's loop; a closed loop raises ``RuntimeError``."""
        loop = self._loop()
        if loop is None:
            msg = "the media owner's loop is not running"
            raise RuntimeError(msg)
        return asyncio.run_coroutine_threadsafe(coroutine, loop)


class PowerController:
    """The only writer of the power fields; callable from any thread (ADR 0383).

    Suspend, resume, admit, abort and revoke are driven by the host's sleep notifications, by
    ``close`` and by the actor, each on its own thread, so every field moves under one lock
    and the lock is never held across a call out of this class except where the old code held
    it (ADR 0014 D20). It reaches the actor's loop only through ``actor``; what the owner
    writes for it, to stop and start accepting, stays the owner's, behind a callable.
    """

    def __init__(  # noqa: PLR0913 - explicit ownership dependencies
        self,
        *,
        player: AudioStreamPlayer,
        stopper: PlayerStopper,
        shutdown_timeout_s: float,
        actor: ActorPort,
        closed: Callable[[], bool],
        stop_accepting: Callable[[], None],
        start_accepting: Callable[[], None],
        lane_isolated: Callable[[], bool],
        suspend_actor: Callable[[], Coroutine[Any, Any, bool]],
        resume_actor: ActorResume,
    ) -> None:
        """Open and stop ``player`` through ``stopper``, each helper bounded by the deadline asked.

        ``closed`` says the actor has finished and ``lane_isolated`` that the lane failed
        closed; ``stop_accepting`` and ``start_accepting`` write the owner's admission flag.
        ``suspend_actor`` and ``resume_actor`` make the owner's coroutines that run on the actor.
        """
        self._player = player
        self._stopper = stopper
        self._shutdown_timeout_s = shutdown_timeout_s
        self._actor = actor
        self._closed = closed
        self._stop_accepting = stop_accepting
        self._start_accepting = start_accepting
        self._lane_isolated = lane_isolated
        self._suspend_actor = suspend_actor
        self._resume_actor = resume_actor
        self._lock = threading.Lock()
        self._suspended = False
        self._state = "running"
        self._attempt_id = 0
        self._helper: threading.Thread | None = None
        self._helper_done = threading.Event()
        self._helper_error: BaseException | None = None
        self._helper_reason = ""
        self._last_timed_out_attempt_id: int | None = None
        self._shutdown = False
        self._admission_generation = 0
        self._actor_resume_attempt_id: int | None = None
        self._actor_resume_generation: int | None = None
        self._actor_resume_future: concurrent.futures.Future[bool] | None = None
        self._unadmitted_wake_attempt_id: int | None = None
        self._abort_attempt_id: int | None = None
        # Set inside ``_lock`` with ``_shutdown`` and read by the actor's loop without it.
        self._shutdown_requested = threading.Event()

    @property
    def state(self) -> str:
        """The power state: running, stopping, suspended, starting, open_unadmitted or uncertain."""
        return self._state

    @property
    def suspended(self) -> bool:
        """True from the actor's terminalization for sleep until a wake is admitted."""
        return self._suspended

    @property
    def attempt_id(self) -> int:
        """The id of the latest transition attempt."""
        return self._attempt_id

    @property
    def helper(self) -> threading.Thread | None:
        """The latest transition helper thread, if one was started."""
        return self._helper

    @property
    def actor_resume_future(self) -> concurrent.futures.Future[bool] | None:
        """The actor's admission for the latest wake, if one was asked."""
        return self._actor_resume_future

    def is_shutdown_requested(self) -> bool:
        """True once a shutdown deadline was published; the actor's loop ends on it."""
        return self._shutdown_requested.is_set()

    def snapshot(self) -> tuple[str, threading.Event]:
        """The power state and the event its helper sets, read together under the power lock."""
        with self._lock:
            return self._state, self._helper_done

    def mark_shutdown(self) -> None:
        """Mark power shut down and the actor's loop to end, both inside the power lock."""
        with self._lock:
            self._shutdown = True
            self._shutdown_requested.set()

    def mark_suspended(self) -> None:
        """Mark output suspended; the actor's terminalization for sleep starts here, unlocked."""
        self._suspended = True

    def admit_actor_resume(self, *, attempt_id: int, admission_generation: int) -> bool:
        """Apply actor admission only for the exact current wake generation."""
        with self._lock:
            if (
                self._admission_generation != admission_generation
                or self._unadmitted_wake_attempt_id != attempt_id
                or self._state != "open_unadmitted"
                or self._abort_attempt_id == attempt_id
                or self._shutdown_requested.is_set()
                or self._shutdown
                or self._lane_isolated()
            ):
                return False
            self._suspended = False
            self._start_accepting()
            return True

    def suspend_for_sleep(  # noqa: C901, PLR0912, PLR0915 - exact late-continuation FSM
        self,
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Terminalize actor output, then prove the player stream stopped."""
        transition_deadline = (
            deadline
            if deadline is not None
            else time.monotonic()
            + (
                self._shutdown_timeout_s
                if timeout_s is None
                else max(0.0, timeout_s)
            )
        )
        with self._lock:
            # Linearize sleep before any actor-side admission from an older
            # wake Future. The coroutine validates this generation while it
            # owns the same lock around every side effect.
            self._admission_generation += 1
        self._stop_accepting()
        if not self._actor.live():
            return MediaPowerTransitionResult(
                "closed",
                self._attempt_id,
                "actor_closed",
            )
        with self._lock:
            if self._state == "suspended":
                return MediaPowerTransitionResult(
                    "suspended",
                    self._attempt_id,
                    "already_suspended",
                )
            if self._state == "starting":
                # A sleep generation must revoke a wake open already inside
                # foreign ``player.start``. The exact start helper owns the
                # compensating close when it returns; this caller only joins.
                attempt_id = self._attempt_id
                self._abort_attempt_id = attempt_id
                done = self._helper_done
                helper = self._helper
                launch = False
            elif self._state == "stopping":
                attempt_id = self._attempt_id
                done = self._helper_done
                helper = self._helper
                launch = False
            elif self._state not in {"running", "open_unadmitted", "uncertain"}:
                return MediaPowerTransitionResult(
                    "uncertain",
                    self._attempt_id,
                    f"power_state_{self._state}",
                )
            else:
                self._attempt_id += 1
                attempt_id = self._attempt_id
                done = threading.Event()
                self._helper_done = done
                self._helper_error = None
                self._helper_reason = ""
                self._state = "stopping"
                helper = None
                launch = True
        if launch:
            try:
                future = self._actor.submit(self._suspend_actor())
            except BaseException as exc:  # noqa: BLE001 - exact attempt failure
                with self._lock:
                    if self._attempt_id == attempt_id:
                        self._helper_error = exc
                        self._helper_reason = (
                            f"actor_terminalization:{type(exc).__name__}"
                        )
                        self._state = "uncertain"
                done.set()
            else:

                def _finish_suspend() -> None:
                    error: BaseException | None = None
                    reason = ""
                    try:
                        terminalized = future.result()
                        if not terminalized:
                            reason = "actor_terminalization_debt"
                            error = RuntimeError(reason)
                        else:
                            result = self._stopper.stop_player_with_bound(
                                self._shutdown_timeout_s,
                            )
                            if (
                                isinstance(result, PlayerStopResult)
                                and not result.definitively_closed
                            ):
                                reason = result.reason
                                error = RuntimeError(reason)
                    except BaseException as exc:  # noqa: BLE001 - lifecycle debt
                        error = exc
                        reason = f"actor_terminalization:{type(exc).__name__}"
                    with self._lock:
                        late = self._last_timed_out_attempt_id == attempt_id
                        if self._attempt_id == attempt_id:
                            self._helper_error = error
                            self._helper_reason = reason
                            self._state = (
                                "suspended" if error is None else "uncertain"
                            )
                    record_realtime_trace(
                        "media_power_suspend_continuation",
                        attempt_id=attempt_id,
                        late_after_caller_timeout=late,
                        definitively_suspended=error is None,
                        reason=reason or "actor_terminalized_and_player_closed",
                    )
                    done.set()

                helper = threading.Thread(
                    target=_finish_suspend,
                    name=f"jarvis-media-power-stop-a{attempt_id}",
                    daemon=True,
                )
                with self._lock:
                    if self._attempt_id == attempt_id:
                        self._helper = helper
                try:
                    helper.start()
                except RuntimeError as exc:
                    with self._lock:
                        if self._attempt_id == attempt_id:
                            self._helper_error = exc
                            self._helper_reason = "continuation_thread_start_failed"
                            self._state = "uncertain"
                    done.set()
        if not done.wait(timeout=max(0.0, transition_deadline - time.monotonic())):
            with self._lock:
                self._last_timed_out_attempt_id = attempt_id
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                "actor_terminalization_or_player_stop_timeout",
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                deadline_exhausted=time.monotonic() >= transition_deadline,
            )
        with self._lock:
            state = self._state
            error = self._helper_error
            reason = self._helper_reason
        if state != "suspended":
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                reason
                or f"power_stop:{type(error).__name__ if error is not None else state}",
            )
        return MediaPowerTransitionResult(
            "suspended",
            attempt_id,
            "actor_terminalized_and_player_closed",
        )

    def resume_after_wake(  # noqa: PLR0911 - exact player/actor/debt outcomes
        self,
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Open a fresh player stream before restoring output admission."""
        transition_deadline = (
            deadline
            if deadline is not None
            else time.monotonic()
            + (
                self._shutdown_timeout_s
                if timeout_s is None
                else max(0.0, timeout_s)
            )
        )
        with self._lock:
            prior_state = self._state
            prior_done = self._helper_done
        if prior_state == "stopping" and not prior_done.wait(
            timeout=max(0.0, transition_deadline - time.monotonic()),
        ):
            return MediaPowerTransitionResult(
                "uncertain",
                self._attempt_id,
                "prior_player_stop_debt",
                helper_thread_alive=True,
                deadline_exhausted=time.monotonic() >= transition_deadline,
            )
        started = self._transition_player_for_power(
            action="start",
            deadline=transition_deadline,
        )
        if started.status != "resumed":
            return started
        if not self._actor.live():
            return MediaPowerTransitionResult(
                "closed",
                started.attempt_id,
                "actor_closed_after_start",
            )
        with self._lock:
            future = self._actor_resume_future
            admission_generation = self._admission_generation
            if (
                future is None
                or self._actor_resume_attempt_id != started.attempt_id
                or self._actor_resume_generation != admission_generation
            ):
                future = self._actor.submit(
                    self._resume_actor(
                        attempt_id=started.attempt_id,
                        admission_generation=admission_generation,
                    ),
                )
                self._actor_resume_attempt_id = started.attempt_id
                self._actor_resume_generation = admission_generation
                self._actor_resume_future = future
        try:
            actor_admitted = future.result(
                timeout=max(0.0, transition_deadline - time.monotonic()),
            )
        except TimeoutError:
            return MediaPowerTransitionResult(
                "uncertain",
                started.attempt_id,
                "actor_resume_timeout",
                helper_thread_alive=True,
                deadline_exhausted=time.monotonic() >= transition_deadline,
            )
        except Exception as exc:  # noqa: BLE001 - bounded cross-loop boundary
            return MediaPowerTransitionResult(
                "uncertain",
                started.attempt_id,
                f"actor_resume:{type(exc).__name__}",
                deadline_exhausted=time.monotonic() >= transition_deadline,
            )
        if not actor_admitted:
            return MediaPowerTransitionResult(
                "uncertain",
                started.attempt_id,
                "actor_resume_generation_revoked",
            )
        with self._lock:
            if (
                self._shutdown
                or self._closed()
                or self._admission_generation != admission_generation
                or self._unadmitted_wake_attempt_id != started.attempt_id
                or self._state != "open_unadmitted"
            ):
                return MediaPowerTransitionResult(
                    "closed" if self._shutdown or self._closed() else "uncertain",
                    started.attempt_id,
                    (
                        "shutdown_revoked_actor_resume"
                        if self._shutdown or self._closed()
                        else "actor_resume_generation_revoked"
                    ),
                )
            self._state = "running"
        record_realtime_trace(
            "media_power_resumed",
            attempt_id=started.attempt_id,
            ownership="fresh_player_stream",
        )
        return started

    def revoke_wake_starts_for_shutdown(self) -> None:
        """Revoke wake-created output without stopping an existing running owner."""
        with self._lock:
            self._shutdown = True
            compensate_open_unadmitted = self._state == "open_unadmitted" or (
                self._state == "running"
                and self._unadmitted_wake_attempt_id is not None
            )
            state = self._state
        record_realtime_trace(
            "media_power_wake_start_revoked",
            power_state=state,
            compensate_open_unadmitted=compensate_open_unadmitted,
        )
        if compensate_open_unadmitted:
            # Launch the exact stop helper but do not make coordinator shutdown
            # wait for it. Full media close remains ordered after input close.
            self._transition_player_for_power(
                action="stop",
                deadline=time.monotonic(),
            )

    def admit_wake_start(self, *, attempt_id: int) -> bool:
        """Commit one fresh output only after coordinator restored input."""
        with self._lock:
            if (
                self._shutdown
                or self._state != "running"
                or self._unadmitted_wake_attempt_id != attempt_id
            ):
                return False
            self._unadmitted_wake_attempt_id = None
            return True

    def abort_wake_start(
        self,
        *,
        attempt_id: int,
        reason: str,
        deadline: float | None = None,
    ) -> MediaPowerTransitionResult:
        """Abort one unadmitted wake output without disabling future wakes."""
        with self._lock:
            if self._unadmitted_wake_attempt_id != attempt_id:
                return MediaPowerTransitionResult(
                    "closed",
                    attempt_id,
                    "wake_attempt_already_retired",
                )
            self._abort_attempt_id = attempt_id
            self._admission_generation += 1
            state = self._state
        self._stop_accepting()
        record_realtime_trace(
            "media_power_wake_start_aborted",
            attempt_id=attempt_id,
            power_state=state,
            reason=reason,
        )
        if state in {"open_unadmitted", "running"}:
            return self.suspend_for_sleep(
                deadline=(
                    deadline
                    if deadline is not None
                    else time.monotonic() + self._shutdown_timeout_s
                ),
            )
        return MediaPowerTransitionResult(
            "uncertain",
            attempt_id,
            "wake_start_abort_joining_start_helper",
            helper_thread_alive=state == "starting",
        )

    def _transition_player_for_power(  # noqa: C901, PLR0911, PLR0915 - exact bounded power FSM
        self,
        *,
        action: Literal["start", "stop"],
        deadline: float,
    ) -> MediaPowerTransitionResult:
        """Run or join one exact bounded player lifecycle helper."""
        desired = "suspended" if action == "stop" else "open_unadmitted"
        transitional = "stopping" if action == "stop" else "starting"
        with self._lock:
            if action == "start" and (self._shutdown or self._closed()):
                return MediaPowerTransitionResult(
                    "closed",
                    self._attempt_id,
                    "shutdown_revoked_player_start",
                )
            if self._state == desired:
                status: Literal["suspended", "resumed"] = (
                    "suspended" if action == "stop" else "resumed"
                )
                return MediaPowerTransitionResult(
                    status,
                    self._attempt_id,
                    f"already_{desired}",
                )
            allowed = (
                {"running", "open_unadmitted", "uncertain"}
                if action == "stop"
                else {"suspended"}
            )
            if self._state == transitional:
                done = self._helper_done
                helper = self._helper
                attempt_id = self._attempt_id
            elif self._state not in allowed:
                return MediaPowerTransitionResult(
                    "uncertain",
                    self._attempt_id,
                    f"power_state_{self._state}",
                )
            else:
                self._attempt_id += 1
                attempt_id = self._attempt_id
                done = threading.Event()
                self._helper_done = done
                self._helper_error = None
                self._helper_reason = ""
                self._state = transitional
                if action == "start":
                    self._admission_generation += 1
                    self._unadmitted_wake_attempt_id = attempt_id
                    self._abort_attempt_id = None

                def _operate() -> None:  # noqa: C901, PLR0912, PLR0915 - exact start/revoke compensation
                    error: BaseException | None = None
                    reason = ""
                    shutdown_cleanup: bool | None = None
                    try:
                        if action == "stop":
                            remaining = max(0.0, deadline - time.monotonic())
                            self._stopper.stop_player_until_definitive(remaining)
                        else:
                            with self._lock:
                                revoked_before_start = (
                                    self._attempt_id != attempt_id
                                    or self._shutdown
                                    or self._abort_attempt_id == attempt_id
                                    or self._closed()
                                )
                            if revoked_before_start:
                                reason = "shutdown_revoked_before_player_start"
                                shutdown_cleanup = True
                            else:
                                result = self._player.start()
                                typed_started = not isinstance(result, PlayerStartResult) or (
                                    result.started
                                )
                                typed_failed_closed = (
                                    isinstance(result, PlayerStartResult)
                                    and result.status == "failed_closed"
                                )
                                if not typed_started:
                                    reason = result.reason
                                    error = RuntimeError(result.reason)
                                with self._lock:
                                    revoked_after_start = (
                                        self._attempt_id != attempt_id
                                        or self._shutdown
                                        or self._abort_attempt_id == attempt_id
                                        or self._closed()
                                    )
                                if revoked_after_start:
                                    if typed_failed_closed:
                                        shutdown_cleanup = True
                                    else:
                                        try:
                                            stopped = self._stopper.stop_player_with_bound(
                                                self._shutdown_timeout_s,
                                            )
                                            if isinstance(stopped, PlayerStopResult):
                                                shutdown_cleanup = stopped.definitively_closed
                                                if not shutdown_cleanup:
                                                    reason = stopped.reason
                                                    if not self._shutdown:
                                                        self._stopper.stop_player_until_definitive(
                                                            self._shutdown_timeout_s,
                                                        )
                                                        shutdown_cleanup = True
                                            else:
                                                shutdown_cleanup = True
                                        except BaseException as stop_exc:  # noqa: BLE001
                                            shutdown_cleanup = False
                                            reason = (
                                                "shutdown_compensating_stop:"
                                                f"{type(stop_exc).__name__}"
                                            )
                                    if not shutdown_cleanup:
                                        error = RuntimeError(reason)
                    except BaseException as exc:  # noqa: BLE001 - lifecycle debt
                        error = exc
                        reason = f"{type(exc).__name__}:{exc}"
                        if action == "start":
                            with self._lock:
                                revoked_after_error = (
                                    self._attempt_id != attempt_id
                                    or self._shutdown
                                    or self._abort_attempt_id == attempt_id
                                    or self._closed()
                                )
                            if revoked_after_error:
                                try:
                                    stopped = self._stopper.stop_player_with_bound(
                                        self._shutdown_timeout_s,
                                    )
                                    shutdown_cleanup = (
                                        stopped.definitively_closed
                                        if isinstance(stopped, PlayerStopResult)
                                        else True
                                    )
                                except BaseException:  # noqa: BLE001 - retained debt
                                    shutdown_cleanup = False
                    finally:
                        with self._lock:
                            if self._attempt_id == attempt_id:
                                self._helper_error = error
                                self._helper_reason = reason
                                if action == "start" and shutdown_cleanup is not None:
                                    self._state = (
                                        "suspended" if shutdown_cleanup else "uncertain"
                                    )
                                    if shutdown_cleanup:
                                        self._unadmitted_wake_attempt_id = None
                                        self._abort_attempt_id = None
                                else:
                                    self._state = (
                                        desired if error is None else "uncertain"
                                    )
                                    if action == "stop" and error is None:
                                        self._unadmitted_wake_attempt_id = None
                                        self._abort_attempt_id = None
                        if action == "start" and shutdown_cleanup is not None:
                            if self._shutdown:
                                self._stopper.record_shutdown_player_cleanup(
                                    definitive=shutdown_cleanup,
                                )
                            record_realtime_trace(
                                "media_power_start_revoked",
                                attempt_id=attempt_id,
                                compensating_stop_definitive=shutdown_cleanup,
                                reason=reason or "shutdown_revoked_late_start",
                            )
                        elif action == "stop" and self._shutdown:
                            self._stopper.record_shutdown_player_cleanup(
                                definitive=error is None,
                            )
                        done.set()

                helper = threading.Thread(
                    target=_operate,
                    name=f"jarvis-media-power-{action}-a{attempt_id}",
                    daemon=True,
                )
                self._helper = helper
                try:
                    helper.start()
                except RuntimeError as exc:
                    self._helper_error = exc
                    self._state = "uncertain"
                    done.set()
        if not done.wait(timeout=max(0.0, deadline - time.monotonic())):
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                f"player_{action}_timeout",
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                deadline_exhausted=time.monotonic() >= deadline,
            )
        with self._lock:
            state = self._state
            error = self._helper_error
            operation_reason = self._helper_reason
            shutdown = self._shutdown or self._closed()
        if action == "start" and shutdown:
            return MediaPowerTransitionResult(
                "closed",
                attempt_id,
                operation_reason or "shutdown_revoked_player_start",
            )
        if state != desired:
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                operation_reason
                or f"player_{action}:{type(error).__name__ if error is not None else state}",
            )
        status = "suspended" if action == "stop" else "resumed"
        record_realtime_trace(
            f"media_power_{status}",
            attempt_id=attempt_id,
            measurement_boundary="software_player_lifecycle_return",
        )
        return MediaPowerTransitionResult(status, attempt_id, f"player_{action}_completed")
