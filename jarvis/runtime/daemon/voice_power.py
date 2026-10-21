"""The voice power coordinator: sleep, wake and device changes against the audio input."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Literal

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface import voice_audio, voice_backend, voice_media, voice_session

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class SingleIngressActivation:
    """Validated Wave-3 activation decision before any input device open."""

    requested: bool
    capable: bool
    reason: str
    ingress_config: voice_audio.AudioIngressConfig | None = None
    session_config: voice_session.RealtimeInputSessionConfig | None = None


@dataclasses.dataclass(frozen=True)
class VoiceInputOwners:
    """Composition-root input branch selected for this boot."""

    duplex_session: voice_session.DuplexVoiceSession | None
    single_ingress_attempted: bool
    # Why this branch was taken. The failing paths already record it as an
    # ``audio_input_activation_downgraded`` trace; carrying it out is what lets
    # the startup record name the successful choice too.
    reason: str = "unknown"


@dataclasses.dataclass(frozen=True)
class SessionAttempt:
    """What starting the capture session came to: the session, or why there is none.

    ``attempted`` is whether a device open was tried; a timed-out foreign open may still own
    the microphone. ``definitively_closed`` is whether the failed attempt's close confirmed it
    free: only then may a second input owner follow (ADR 0006). ``why`` names the cause for the
    log: the capability's state and reason once the device was reached, else what failed.
    """

    session: voice_session.DuplexVoiceSession | None
    attempted: bool = False
    definitively_closed: bool = True
    why: str = ""

    @property
    def retryable(self) -> bool:
        """Whether another attempt is safe: a device open failed and its close was confirmed."""
        return self.session is None and self.attempted and self.definitively_closed


@dataclasses.dataclass(frozen=True)
class VoicePowerTransition:
    """Auditable ordered input/output transition at the power boundary."""

    input_result: object | None
    output_result: voice_media.MediaPowerTransitionResult | None
    deadline_monotonic: float
    elapsed_s: float
    input_skipped_reason: str | None = None


SUSPENDED = voice_audio.InputCapabilityState.SUSPENDED
_CLOSE_UNCERTAIN = voice_audio.InputCapabilityState.CLOSE_UNCERTAIN


class VoicePowerCoordinator:
    """Serialize power lifecycle without exposing a speech-cancel seam."""

    _TOTAL_TRANSITION_BOUND_S = 2.75

    def __init__(
        self,
        *,
        session: voice_session.DuplexVoiceSession,
        media: voice_media.StreamingTTSPipeline,
    ) -> None:
        """Bind the input session and the speech pipeline the power transitions drive."""
        self._session = session
        self._media = media
        self._lock = threading.Lock()
        self._intent_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._pending_wake_generation = 0
        self._pending_wake_thread: threading.Thread | None = None
        self._pending_wake_done = threading.Event()
        self._pending_wake_done.set()
        self._pending_wake_state: Literal["idle", "in_flight", "recovered"] = "idle"
        # ADR 0054: from the system's sleep notice until a wake has run to its end.
        self._asleep = False

    @staticmethod
    def _input_resume_succeeded(result: object | None) -> bool:
        """Interpret the typed production port and explicit legacy test adapter."""
        if isinstance(result, voice_backend.BackendStartResult):
            return result.started
        return result is not None

    @staticmethod
    def _input_resume_reason(result: object | None) -> str:
        if isinstance(result, voice_backend.BackendStartResult):
            return result.status.value
        return "none" if result is None else "legacy_adapter_success"

    def before_sleep(self) -> VoicePowerTransition:
        """Close input first, then terminalize and stop output."""
        started = time.monotonic()
        deadline = started + self._TOTAL_TRANSITION_BOUND_S
        # Revoke older wake work before waiting for the operation lock. A
        # continuation may be inside a foreign media wait, but it can no
        # longer restore input/output after this linearization point.
        with self._intent_lock:
            self._pending_wake_generation += 1
            self._asleep = True
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            output_result = voice_media.MediaPowerTransitionResult(
                "uncertain",
                0,
                "coordinator_lock_timeout",
                deadline_exhausted=True,
            )
            return VoicePowerTransition(
                None,
                output_result,
                deadline,
                time.monotonic() - started,
                "coordinator_lock_timeout",
            )
        try:
            self._pending_wake_state = "idle"
            input_result = self._session.ingress.stop_for_sleep(deadline=deadline)
            output_result = self._media.suspend_for_sleep(deadline=deadline)
            elapsed_s = time.monotonic() - started
            record_realtime_trace(
                "voice_power_before_sleep_completed",
                elapsed_s=elapsed_s,
                total_bound_s=self._TOTAL_TRANSITION_BOUND_S,
                deadline_exhausted=time.monotonic() >= deadline,
                output_status=output_result.status,
                output_reason=output_result.reason,
            )
            return VoicePowerTransition(
                input_result,
                output_result,
                deadline,
                elapsed_s,
            )
        finally:
            self._lock.release()

    def on_wake(self) -> VoicePowerTransition:
        """Wake both devices; awake again once it has run, unless a newer sleep came meanwhile."""
        with self._intent_lock:
            generation = self._pending_wake_generation
        try:
            return self._wake()
        finally:
            with self._intent_lock:
                if generation == self._pending_wake_generation:
                    self._asleep = False

    def _wake(  # noqa: C901, PLR0912, PLR0915 - ordered cross-device CAS
        self,
    ) -> VoicePowerTransition:
        """Create fresh output ownership before re-enabling input decisions."""
        started = time.monotonic()
        deadline = started + self._TOTAL_TRANSITION_BOUND_S
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            reason = "coordinator_lock_timeout"
            output_result = voice_media.MediaPowerTransitionResult(
                "uncertain",
                0,
                reason,
                deadline_exhausted=True,
            )
            return VoicePowerTransition(
                None,
                output_result,
                deadline,
                time.monotonic() - started,
                reason,
            )
        lock_owned = True
        try:
            pending = self._pending_wake_thread
            if self._pending_wake_state == "in_flight" and not (
                pending is not None and pending.is_alive()
            ):
                self._pending_wake_state = "idle"
            if self._pending_wake_state in {"in_flight", "recovered"}:
                pending_alive = pending is not None and pending.is_alive()
                output_result = voice_media.MediaPowerTransitionResult(
                    (
                        "resumed"
                        if self._pending_wake_state == "recovered"
                        else "uncertain"
                    ),
                    self._pending_wake_generation,
                    (
                        "pending_wake_already_resumed"
                        if self._pending_wake_state == "recovered"
                        else "pending_wake_continuation_in_flight"
                    ),
                    helper_thread_alive=pending_alive,
                )
                return VoicePowerTransition(
                    None,
                    output_result,
                    deadline,
                    time.monotonic() - started,
                    output_result.reason,
                )
            with self._intent_lock:
                wake_generation = self._pending_wake_generation
            self._lock.release()
            lock_owned = False
            output_result = self._media.resume_after_wake(deadline=deadline)
            if not self._lock.acquire(
                timeout=max(0.0, deadline - time.monotonic()),
            ):
                self._media.abort_wake_start(
                    attempt_id=output_result.attempt_id,
                    reason="wake_operation_lock_timeout",
                    deadline=deadline,
                )
                return VoicePowerTransition(
                    None,
                    output_result,
                    deadline,
                    time.monotonic() - started,
                    "wake_operation_lock_timeout",
                )
            lock_owned = True
            skipped_reason: str | None = None
            with self._intent_lock:
                wake_current = (
                    wake_generation == self._pending_wake_generation
                    and not self._shutdown.is_set()
                )
            if output_result.status == "resumed" and output_result.succeeded and wake_current:
                input_result = self._session.ingress.resume_after_wake(deadline=deadline)
                with self._intent_lock:
                    wake_current = (
                        wake_generation == self._pending_wake_generation
                        and not self._shutdown.is_set()
                    )
                    admitted = (
                        wake_current
                        and self._input_resume_succeeded(input_result)
                        and self._media.admit_wake_start(
                            attempt_id=output_result.attempt_id,
                        )
                    )
            else:
                input_result = None
                admitted = False
            if output_result.status == "resumed" and output_result.succeeded:
                if not admitted:
                    if not wake_current:
                        if input_result is not None:
                            self._session.ingress.stop_for_sleep(deadline=deadline)
                        self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason="wake_generation_revoked",
                            deadline=deadline,
                        )
                        skipped_reason = "wake_generation_revoked"
                    else:
                        input_reason = self._input_resume_reason(input_result)
                        if input_result is not None:
                            self._session.ingress.stop_for_sleep(deadline=deadline)
                        aborted = self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason=f"input_resume_{input_reason}",
                            deadline=deadline,
                        )
                        skipped_reason = (
                            f"input_resume_{input_reason};output_{aborted.status}:"
                            f"{aborted.reason}"
                        )
            else:
                input_result = None
                if wake_current:
                    skipped_reason = f"output_{output_result.status}:{output_result.reason}"
                    self._session.ingress.report_output_unavailable(reason=skipped_reason)
                    if output_result.helper_thread_alive and not self._shutdown.is_set():
                        self._schedule_pending_wake_locked(wake_generation)
                else:
                    self._media.abort_wake_start(
                        attempt_id=output_result.attempt_id,
                        reason="wake_generation_revoked",
                        deadline=deadline,
                    )
                    skipped_reason = "wake_generation_revoked"
            elapsed_s = time.monotonic() - started
            record_realtime_trace(
                "voice_power_wake_completed",
                elapsed_s=elapsed_s,
                total_bound_s=self._TOTAL_TRANSITION_BOUND_S,
                deadline_exhausted=time.monotonic() >= deadline,
                output_status=output_result.status,
                output_reason=output_result.reason,
                input_skipped_reason=skipped_reason,
            )
            return VoicePowerTransition(
                input_result,
                output_result,
                deadline,
                elapsed_s,
                skipped_reason,
            )
        finally:
            if lock_owned:
                self._lock.release()

    def _schedule_pending_wake_locked(  # noqa: C901, PLR0915 - one exact retry owner
        self,
        generation: int,
    ) -> None:
        """Keep one deduplicated wake continuation after an exact stop debt."""
        pending = self._pending_wake_thread
        if pending is not None and pending.is_alive():
            return
        self._pending_wake_state = "in_flight"
        done = threading.Event()
        self._pending_wake_done = done

        def _continue_wake() -> None:  # noqa: C901, PLR0912, PLR0915 - exact debt loop
            output_result: voice_media.MediaPowerTransitionResult | None = None
            input_result: object | None = None
            reason = ""
            deadline = time.monotonic()
            try:
                while not self._shutdown.is_set():
                    deadline = time.monotonic() + self._TOTAL_TRANSITION_BOUND_S
                    with self._intent_lock:
                        generation_current = (
                            generation == self._pending_wake_generation
                            and not self._shutdown.is_set()
                        )
                    if not generation_current:
                        reason = "pending_wake_revoked"
                        return
                    # Never hold the coordinator operation lock across the
                    # foreign media wait. A newer sleep can acquire it and
                    # complete while this exact debt remains joinable here.
                    output_result = self._media.resume_after_wake(deadline=deadline)
                    acquired = self._lock.acquire(
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                    if not acquired:
                        reason = "pending_wake_coordinator_lock_timeout"
                        self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason=reason,
                            deadline=deadline,
                        )
                        continue
                    retry_exact_debt = False
                    try:
                        with self._intent_lock:
                            generation_current = (
                                generation == self._pending_wake_generation
                                and not self._shutdown.is_set()
                            )
                        if (
                            output_result.status == "resumed"
                            and output_result.succeeded
                            and generation_current
                        ):
                            input_result = self._session.ingress.resume_after_wake(
                                deadline=deadline,
                            )
                            with self._intent_lock:
                                generation_current = (
                                    generation == self._pending_wake_generation
                                    and not self._shutdown.is_set()
                                )
                                admitted = (
                                    generation_current
                                    and self._input_resume_succeeded(input_result)
                                    and self._media.admit_wake_start(
                                        attempt_id=output_result.attempt_id,
                                    )
                                )
                        else:
                            input_result = None
                            admitted = False
                        if output_result.status == "resumed" and output_result.succeeded:
                            if admitted:
                                self._pending_wake_state = "recovered"
                                reason = "pending_wake_resumed"
                                return
                            if not generation_current:
                                if input_result is not None:
                                    self._session.ingress.stop_for_sleep(deadline=deadline)
                                self._media.abort_wake_start(
                                    attempt_id=output_result.attempt_id,
                                    reason="pending_wake_generation_revoked",
                                    deadline=deadline,
                                )
                                reason = "pending_wake_generation_revoked"
                            else:
                                input_reason = self._input_resume_reason(input_result)
                                if input_result is not None:
                                    self._session.ingress.stop_for_sleep(deadline=deadline)
                                aborted = self._media.abort_wake_start(
                                    attempt_id=output_result.attempt_id,
                                    reason=f"input_resume_{input_reason}",
                                    deadline=deadline,
                                )
                                reason = (
                                    f"pending_input_resume_{input_reason};"
                                    f"output_{aborted.status}:{aborted.reason}"
                                )
                            return
                        if not generation_current:
                            self._media.abort_wake_start(
                                attempt_id=output_result.attempt_id,
                                reason="pending_wake_generation_revoked",
                                deadline=deadline,
                            )
                            reason = "pending_wake_generation_revoked"
                            return
                        reason = f"output_{output_result.status}:{output_result.reason}"
                        if not self._shutdown.is_set():
                            self._session.ingress.report_output_unavailable(reason=reason)
                        retry_exact_debt = output_result.helper_thread_alive
                    finally:
                        self._lock.release()
                    record_realtime_trace(
                        "voice_power_pending_wake_retry",
                        generation=generation,
                        output_status=output_result.status,
                        reason=reason,
                        retry_exact_debt=retry_exact_debt,
                        deadline_exhausted=time.monotonic() >= deadline,
                    )
                    if not retry_exact_debt:
                        return
                reason = "pending_wake_revoked"
            finally:
                with self._lock:
                    if (
                        generation == self._pending_wake_generation
                        and self._pending_wake_state != "recovered"
                    ):
                        self._pending_wake_state = "idle"
                record_realtime_trace(
                    "voice_power_pending_wake_completed",
                    generation=generation,
                    output_status=(
                        output_result.status if output_result is not None else "not_attempted"
                    ),
                    input_resumed=input_result is not None,
                    reason=reason,
                    deadline_exhausted=time.monotonic() >= deadline,
                )
                done.set()

        thread = threading.Thread(
            target=_continue_wake,
            name=f"jarvis-power-pending-wake-g{generation}",
            daemon=True,
        )
        self._pending_wake_thread = thread
        try:
            thread.start()
        except RuntimeError:
            done.set()
            LOGGER.exception("failed to start pending wake continuation")

    @property
    def asleep(self) -> bool:
        """The system said it sleeps and no wake has run to its end since."""
        return self._asleep

    def quiet(self) -> bool:
        """Awake, nothing plays, Allen is not mid-sentence, and no close is in doubt."""
        ingress = self._session.ingress
        return (
            not self._asleep
            and not self._media.is_output_active()
            and not ingress.capture_active
            and ingress.capability.state is not _CLOSE_UNCERTAIN
        )

    def refresh_devices(  # noqa: PLR0911 - each refusal is its own answer
        self, *, input_device: str | None, output_device: str | None,
    ) -> str:
        """Close every stream, re-read PortAudio's devices, reopen on these (ADR 0054).

        Starts only while :meth:`quiet`; an answer arriving meanwhile parks and
        starts afterwards. The caller has already closed any GPT-Live speaker.
        Answers ``refreshed ...`` only when PortAudio initialised again. A wake
        whose microphone failed left both devices parked: re-read, then wake again.
        """
        ingress = self._session.ingress
        if not self._lock.acquire(timeout=self._TOTAL_TRANSITION_BOUND_S):
            return "busy:power_transition"
        try:
            if self._shutdown.is_set():
                return "closed"
            state = ingress.capability.state
            if self._asleep or state is _CLOSE_UNCERTAIN:
                return f"skipped:{'asleep' if self._asleep else state.value}"
            if state is SUSPENDED:
                if self._pending_wake_state != "idle":
                    return "busy:wake_in_flight"
                player = self._media.player
                if not player.stop().definitively_closed:
                    return "not_refreshed:close_uncertain"
                voice_backend.reinitialize_portaudio()
                player.set_device(output_device)
                ingress.set_input_device(input_device)
                ingress.set_output_device(output_device)
            else:
                return self._reopen_locked(input_device, output_device)
        finally:
            self._lock.release()
        woke = self.on_wake()
        speaker = woke.output_result
        return (
            f"refreshed_and_woke speaker={speaker and f'{speaker.status}:{speaker.reason}'} "
            f"microphone={self._input_resume_reason(woke.input_result)}"
        )

    def _reopen_locked(self, input_device: str | None, output_device: str | None) -> str:
        """Close both devices while nothing plays, re-read PortAudio, reopen on these."""
        ingress = self._session.ingress
        nothing_playing = self._media.hold_for_devices(held=True)
        try:
            if not nothing_playing:
                return "busy:speaking"
            if ingress.capture_active:
                return "busy:listening"
            stopped = ingress.stop_for_sleep(
                deadline=time.monotonic() + self._TOTAL_TRANSITION_BOUND_S,
            )
            player = self._media.player
            closed = player.stop()
            if (stopped is None or stopped.definitively_closed) and closed.definitively_closed:
                voice_backend.reinitialize_portaudio()
                outcome = "refreshed"
            else:
                outcome = "not_refreshed:close_uncertain"
            player.set_device(output_device)
            started = player.start()
            ingress.set_input_device(input_device)
            ingress.set_output_device(output_device)
            resumed = ingress.resume_after_wake(
                deadline=time.monotonic() + self._TOTAL_TRANSITION_BOUND_S,
            )
            if not started.started:
                ingress.report_output_unavailable(reason=f"device_refresh:{started.reason}")
            return (
                f"{outcome} speaker={started.status}:{started.reason} "
                f"microphone={self._input_resume_reason(resumed)}"
            )
        finally:
            self._media.hold_for_devices(held=False)

    def close(self, *, timeout_s: float | None = None) -> bool:
        """Revoke any pending wake before input/output owner shutdown."""
        self._shutdown.set()
        with self._intent_lock:
            self._pending_wake_generation += 1
        self._media.revoke_wake_starts_for_shutdown()
        timeout = self._TOTAL_TRANSITION_BOUND_S if timeout_s is None else max(0.0, timeout_s)
        deadline = time.monotonic() + timeout
        if self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                self._pending_wake_state = "idle"
            finally:
                self._lock.release()
        pending = self._pending_wake_thread
        if pending is not None:
            pending.join(timeout=max(0.0, deadline - time.monotonic()))
        return pending is None or not pending.is_alive()
