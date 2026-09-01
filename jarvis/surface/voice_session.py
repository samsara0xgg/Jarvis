"""Single-ingress wake → pre-roll → endpoint session for ADR-0006 Wave 3.

This module reverses the legacy control flow: capture is already running when
the wake word is detected, and wake framing never stops while utterance VAD,
ASR, or output playback is active.  Wake, capture, and diagnostics consume
separate bounded SPSC subscribers on the same canonical cursor.

Wave 3 deliberately has no barge-in cancellation path.  During assistant
output the wake subscriber is still drained, but detections are suppressed as
typed telemetry.  Neither echo nor ordinary speech can call Wave-2 playback
flush/CAS, ResponseRun cancellation, or action cancellation.
"""

from __future__ import annotations

import enum
import logging
import queue
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from jarvis.shared.realtime_trace import realtime_trace_context, record_realtime_trace
from jarvis.surface import voice_audio, voice_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.shared import Event

LOGGER = logging.getLogger("jarvis.surface.voice_session")

_WAKE_WINDOW_SAMPLES = 1280


@dataclass(frozen=True)
class RealtimeInputSessionConfig:
    """Hard bounds for wake framing, utterance assembly, and worker queues."""

    pre_roll_ms: int = 500
    max_utterance_s: float = 30.0
    min_voiced_s: float = 1.0
    wake_subscriber_capacity: int = 64
    capture_subscriber_capacity: int = 128
    diagnostic_subscriber_capacity: int = 16
    detection_queue_capacity: int = 2
    commit_queue_capacity: int = 2
    wake_failure_threshold: int = 3
    worker_poll_s: float = 0.005
    shutdown_timeout_s: float = 3.0


@dataclass(frozen=True)
class WakeDetection:
    """Wake decision bound to the canonical ingress cursor, not wall time."""

    stream_epoch: int
    input_sample_cursor: int
    observed_monotonic_ns: int
    probability: float


@dataclass(frozen=True)
class CapturedUtterance:
    """One gap-free complete PCM utterance ready for authoritative ASR."""

    session_id: str
    utterance_id: str
    turn_id: str
    stream_epoch: int
    start_sample_cursor: int
    end_sample_cursor: int
    endpoint_reason: str
    audio_bytes: bytes


@dataclass(frozen=True)
class UtteranceCaptureFailure:
    """Explicit active-capture failure; no gapped audio may reach ASR."""

    session_id: str
    utterance_id: str
    turn_id: str
    stream_epoch: int
    reason: str
    input_sample_cursor: int


@dataclass(frozen=True)
class VoiceSessionStartResult:
    """Typed session startup result."""

    started: bool
    ingress: voice_audio.IngressStartResult


@dataclass(frozen=True)
class VoiceSessionCloseResult:
    """Typed proof that every session owner stopped (or remained visible)."""

    ingress: voice_audio.IngressCloseResult
    alive_threads: tuple[str, ...]
    pending_detections: int
    pending_commits: int

    @property
    def definitively_closed(self) -> bool:
        """Return whether hardware and all software owners are gone."""
        return self.ingress.definitively_closed and not self.alive_threads


@dataclass(frozen=True)
class VoiceSessionMetrics:
    """Raw session counters with software-boundary provenance."""

    wake_windows: int
    wake_detections: int
    wake_suppressed_during_output: int
    capture_starts: int
    endpoint_commits: int
    capture_discontinuities: int
    commit_queue_full: int
    diagnostic_frames: int
    diagnostic_last_cursor: int | None
    wake_prediction_failures: int


class _WakeEnginePort(Protocol):
    """OpenWakeWord subset used by the realtime session."""

    @property
    def model_name(self) -> str:
        """Prediction mapping key."""
        ...

    def predict(self, frame_bytes: bytes) -> dict[str, float]:
        """Score one continuous 80 ms frame."""
        ...

    def reset(self) -> None:
        """Reset accumulated wake features after a decision/discontinuity."""
        ...

    def close(self) -> None:
        """Release model handles."""
        ...


class _PipelinePort(Protocol):
    """Authoritative whole-WAV ASR/commit adapter retained from ADR-0005."""

    def run_turn(  # noqa: PLR0913 - mirrors the compatibility adapter signature
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_acquire_timeout_s: float = ...,
        lock_already_held: bool = ...,
        broadcast: bool = ...,
        session_id: str | None = ...,
        utterance_id: str | None = ...,
        endpoint_reason: str | None = ...,
    ) -> Event:
        """Run final ASR and commit ``utterance.received``."""
        ...


class _BroadcasterPort(Protocol):
    """Worker-safe Inherent voice phase bridge."""

    def broadcast_voice_sync(
        self,
        phase: str,
        *,
        turn_id: str,
        **payload: object,
    ) -> None:
        """Publish one ephemeral UI phase."""
        ...


class _AssemblerState(enum.Enum):
    IDLE = "idle"
    ARMED = "armed"
    ACTIVE = "active"


class WakeWindowFramer:
    """Accumulate canonical 32 ms frames into gap-free 80 ms wake windows."""

    def __init__(self) -> None:
        """Initialize an empty epoch-local wake buffer."""
        self._stream_epoch: int | None = None
        self._expected_cursor: int | None = None
        self._buffer_start_cursor = 0
        self._buffer = bytearray()

    def reset(self) -> None:
        """Drop partial wake state after any epoch/gap."""
        self._stream_epoch = None
        self._expected_cursor = None
        self._buffer_start_cursor = 0
        self._buffer.clear()

    def feed(
        self,
        frame: voice_audio.CanonicalAudioFrame,
    ) -> tuple[tuple[bytes, int], ...]:
        """Return ``(pcm, end_cursor)`` windows without missing/duplicate samples."""
        if (
            frame.discontinuity_before
            or self._stream_epoch != frame.stream_epoch
            or (self._expected_cursor is not None and frame.sample_cursor != self._expected_cursor)
        ):
            self.reset()
        if self._stream_epoch is None:
            self._stream_epoch = frame.stream_epoch
            self._buffer_start_cursor = frame.sample_cursor
        self._buffer.extend(frame.pcm16_mono)
        self._expected_cursor = frame.sample_cursor + frame.frame_count
        windows: list[tuple[bytes, int]] = []
        window_bytes = _WAKE_WINDOW_SAMPLES * 2
        while len(self._buffer) >= window_bytes:
            payload = bytes(self._buffer[:window_bytes])
            del self._buffer[:window_bytes]
            end_cursor = self._buffer_start_cursor + _WAKE_WINDOW_SAMPLES
            windows.append((payload, end_cursor))
            self._buffer_start_cursor = end_cursor
        return tuple(windows)


class UtteranceAssembler:
    """Wake-armed VAD/pre-roll assembly with explicit gap failure."""

    def __init__(
        self,
        *,
        vad: voice_audio.SileroVad,
        config: RealtimeInputSessionConfig,
        sample_rate_hz: int,
        frame_samples: int,
        session_id: str,
    ) -> None:
        """Create bounded idle/pre-roll/utterance storage around one VAD."""
        self._vad = vad
        self._config = config
        self._sample_rate_hz = sample_rate_hz
        self._frame_samples = frame_samples
        self._session_id = session_id
        pre_roll_frames = max(
            1,
            int(config.pre_roll_ms * sample_rate_hz / 1_000 / frame_samples + 0.999),
        )
        # Idle history also covers wake-thread/capture-thread scheduling skew.
        self._idle_history: deque[voice_audio.CanonicalAudioFrame] = deque(
            maxlen=pre_roll_frames + 16,
        )
        self._speech_pre_roll: deque[voice_audio.CanonicalAudioFrame] = deque(
            maxlen=pre_roll_frames,
        )
        self._max_frames = max(
            1,
            int(config.max_utterance_s * sample_rate_hz / frame_samples + 0.999),
        )
        self._min_voiced_frames = max(
            1,
            int(config.min_voiced_s * sample_rate_hz / frame_samples),
        )
        self._state = _AssemblerState.IDLE
        self._stream_epoch: int | None = None
        self._expected_cursor: int | None = None
        self._utterance_id = ""
        self._turn_id = ""
        self._wake_cursor = 0
        self._audio_frames: list[bytes] = []
        self._start_cursor = 0
        self._voiced_frames = 0

    @property
    def active(self) -> bool:
        """Return whether an utterance has crossed speech onset."""
        return self._state is _AssemblerState.ACTIVE

    @property
    def armed(self) -> bool:
        """Return whether a wake decision is awaiting/recording speech."""
        return self._state in {_AssemblerState.ARMED, _AssemblerState.ACTIVE}

    def prepare(self) -> None:
        """Reset and prewarm Silero outside the first-speech hot path."""
        self._vad.prepare_utterance()

    def observe_idle(self, frame: voice_audio.CanonicalAudioFrame) -> None:
        """Maintain bounded scheduling history while no wake is armed."""
        if (
            frame.discontinuity_before
            or self._stream_epoch != frame.stream_epoch
            or (self._expected_cursor is not None and frame.sample_cursor != self._expected_cursor)
        ):
            self._idle_history.clear()
        self._stream_epoch = frame.stream_epoch
        self._expected_cursor = frame.sample_cursor + frame.frame_count
        self._idle_history.append(frame)

    def arm(
        self,
        detection: WakeDetection,
    ) -> tuple[CapturedUtterance | UtteranceCaptureFailure, ...]:
        """Arm from the exact wake cursor and replay only already-consumed suffix."""
        self._state = _AssemblerState.ARMED
        self._stream_epoch = detection.stream_epoch
        self._expected_cursor = detection.input_sample_cursor
        self._utterance_id = "U" + secrets.token_hex(8)
        self._turn_id = "T" + secrets.token_hex(4)
        self._wake_cursor = detection.input_sample_cursor
        self._audio_frames.clear()
        self._speech_pre_roll.clear()
        self._voiced_frames = 0
        replay = tuple(
            frame
            for frame in self._idle_history
            if frame.stream_epoch == detection.stream_epoch
            and frame.sample_cursor + frame.frame_count > detection.input_sample_cursor
        )
        self._idle_history.clear()
        if replay:
            self._expected_cursor = replay[0].sample_cursor
        outcomes: list[CapturedUtterance | UtteranceCaptureFailure] = []
        for frame in replay:
            outcome = self.feed(frame)
            if outcome is not None:
                outcomes.append(outcome)
        return tuple(outcomes)

    def feed(
        self,
        frame: voice_audio.CanonicalAudioFrame,
    ) -> CapturedUtterance | UtteranceCaptureFailure | None:
        """Consume one canonical frame and return only terminal assembly outcomes."""
        if self._state is _AssemblerState.IDLE:
            self.observe_idle(frame)
            return None
        discontinuity = (
            frame.discontinuity_before
            or self._stream_epoch != frame.stream_epoch
            or (self._expected_cursor is not None and frame.sample_cursor != self._expected_cursor)
        )
        if discontinuity:
            if self._state is _AssemblerState.ACTIVE:
                failure = UtteranceCaptureFailure(
                    session_id=self._session_id,
                    utterance_id=self._utterance_id,
                    turn_id=self._turn_id,
                    stream_epoch=frame.stream_epoch,
                    reason="active_audio_discontinuity",
                    input_sample_cursor=frame.sample_cursor,
                )
                self.reset_to_idle()
                self.observe_idle(frame)
                return failure
            # Armed but no speech: rebuild pre-roll and VAD rather than fail a
            # nonexistent utterance. The next real onset remains gap-free.
            self._speech_pre_roll.clear()
            self._vad.prepare_utterance()
            self._stream_epoch = frame.stream_epoch
        self._expected_cursor = frame.sample_cursor + frame.frame_count
        event = self._vad.feed(frame.pcm16_mono)
        if self._state is _AssemblerState.ARMED:
            self._speech_pre_roll.append(frame)
            if not self._vad.is_speech_detected():
                return None
            self._state = _AssemblerState.ACTIVE
            self._audio_frames = [item.pcm16_mono for item in self._speech_pre_roll]
            self._start_cursor = self._speech_pre_roll[0].sample_cursor
            self._speech_pre_roll.clear()
        else:
            self._audio_frames.append(frame.pcm16_mono)
        if event is voice_audio.VadEvent.SPEECH_ACTIVE:
            self._voiced_frames += 1
        endpoint_reason: str | None = None
        if self._voiced_frames >= self._min_voiced_frames and self._vad.empty():
            endpoint_reason = "acoustic_pause"
        elif len(self._audio_frames) >= self._max_frames:
            endpoint_reason = "max_duration"
        if endpoint_reason is None:
            return None
        audio_bytes = b"".join(self._audio_frames)
        utterance = CapturedUtterance(
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            stream_epoch=frame.stream_epoch,
            start_sample_cursor=self._start_cursor,
            end_sample_cursor=frame.sample_cursor + frame.frame_count,
            endpoint_reason=endpoint_reason,
            audio_bytes=audio_bytes,
        )
        self.reset_to_idle()
        return utterance

    def reset_to_idle(self) -> None:
        """Clear every utterance-local mutable field."""
        self._state = _AssemblerState.IDLE
        self._stream_epoch = None
        self._expected_cursor = None
        self._utterance_id = ""
        self._turn_id = ""
        self._wake_cursor = 0
        self._audio_frames.clear()
        self._speech_pre_roll.clear()
        self._voiced_frames = 0


class DuplexVoiceSession:
    """Own wake/capture/commit workers around one continuously sampled ingress."""

    def __init__(  # noqa: PLR0913 - keyword-only composition boundary
        self,
        *,
        ingress: voice_audio.AudioIngress,
        wake_engine: _WakeEnginePort,
        vad: voice_audio.SileroVad,
        pipeline: _PipelinePort,
        broadcaster: _BroadcasterPort | None,
        output_active: Callable[[], bool] | None,
        wake_threshold: float,
        config: RealtimeInputSessionConfig,
    ) -> None:
        """Register all bounded subscribers before any hardware starts."""
        self._ingress = ingress
        self._wake_engine = wake_engine
        self._pipeline = pipeline
        self._broadcaster = broadcaster
        self._output_active = output_active
        self._wake_threshold = wake_threshold
        self._config = config
        self._session_id = "S" + secrets.token_hex(8)
        self._wake_subscription = ingress.subscribe(
            name="wake",
            purpose=voice_audio.SubscriberPurpose.WAKE,
            capacity=config.wake_subscriber_capacity,
        )
        self._capture_subscription = ingress.subscribe(
            name="utterance_capture",
            purpose=voice_audio.SubscriberPurpose.CAPTURE,
            capacity=config.capture_subscriber_capacity,
        )
        self._diagnostic_subscription = ingress.subscribe(
            name="timing_diagnostics",
            purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
            capacity=config.diagnostic_subscriber_capacity,
        )
        self._assembler = UtteranceAssembler(
            vad=vad,
            config=config,
            sample_rate_hz=16_000,
            frame_samples=voice_audio.SILERO_CHUNK_SAMPLES,
            session_id=self._session_id,
        )
        self._detections: queue.Queue[WakeDetection] = queue.Queue(
            maxsize=config.detection_queue_capacity,
        )
        self._commits: queue.Queue[CapturedUtterance] = queue.Queue(
            maxsize=config.commit_queue_capacity,
        )
        self._stop = threading.Event()
        self._threads: tuple[threading.Thread, ...] = ()
        self._started = False
        self._wake_windows = 0
        self._wake_detections = 0
        self._wake_suppressed_during_output = 0
        self._capture_starts = 0
        self._endpoint_commits = 0
        self._capture_discontinuities = 0
        self._commit_queue_full = 0
        self._diagnostic_frames = 0
        self._diagnostic_last_cursor: int | None = None
        self._wake_prediction_failures = 0

    def start(self) -> VoiceSessionStartResult:
        """Prewarm models, start ingress, then start bounded software owners."""
        if self._started:
            msg = "duplex voice session already started"
            raise RuntimeError(msg)
        self._assembler.prepare()
        ingress_result = self._ingress.start()
        if not ingress_result.started:
            self._wake_subscription.close()
            self._capture_subscription.close()
            self._diagnostic_subscription.close()
            return VoiceSessionStartResult(started=False, ingress=ingress_result)
        self._stop.clear()
        self._threads = (
            threading.Thread(target=self._wake_loop, name="jarvis-wake-ingress", daemon=False),
            threading.Thread(
                target=self._capture_loop,
                name="jarvis-utterance-ingress",
                daemon=False,
            ),
            threading.Thread(
                target=self._commit_loop,
                name="jarvis-utterance-commit",
                daemon=False,
            ),
            threading.Thread(
                target=self._diagnostic_loop,
                name="jarvis-audio-input-diagnostics",
                daemon=False,
            ),
        )
        for thread in self._threads:
            thread.start()
        self._started = True
        record_realtime_trace(
            "duplex_voice_session_started",
            session_id=self._session_id,
            stream_epoch=self._ingress.stream_epoch,
            hard_cancel_enabled=False,
            natural_barge_in_enabled=False,
        )
        return VoiceSessionStartResult(started=True, ingress=ingress_result)

    def _wake_loop(self) -> None:  # noqa: C901, PLR0912, PLR0915 - linear drain/decision/suppress/fault loop
        framer = WakeWindowFramer()
        consecutive_prediction_failures = 0
        while not self._stop.is_set():
            frame = self._wake_subscription.read(timeout_s=self._config.worker_poll_s)
            if frame is None:
                continue
            if frame.discontinuity_before:
                framer.reset()
                try:
                    self._wake_engine.reset()
                except Exception:  # noqa: BLE001 - model reset cannot stop drain
                    LOGGER.debug("wake engine reset failed after discontinuity", exc_info=True)
            for window, end_cursor in framer.feed(frame):
                self._wake_windows += 1
                try:
                    predictions = self._wake_engine.predict(window)
                    probability = float(
                        predictions.get(self._wake_engine.model_name, 0.0),
                    )
                except Exception:  # noqa: BLE001 - keep draining after model fault
                    self._wake_prediction_failures += 1
                    consecutive_prediction_failures += 1
                    if consecutive_prediction_failures >= self._config.wake_failure_threshold:
                        self._ingress.report_wake_unavailable(
                            reason="wake_prediction_failure_threshold",
                        )
                    if consecutive_prediction_failures == self._config.wake_failure_threshold:
                        LOGGER.warning(
                            "wake prediction failure threshold reached; wake unavailable",
                            exc_info=True,
                        )
                    continue
                if consecutive_prediction_failures >= self._config.wake_failure_threshold:
                    self._ingress.report_wake_available(
                        reason="wake_prediction_recovered",
                    )
                consecutive_prediction_failures = 0
                if probability < self._wake_threshold:
                    continue
                self._wake_detections += 1
                try:
                    output_active = bool(
                        self._output_active is not None and self._output_active(),
                    )
                    suppress_reason = "wave3_no_interrupt_policy_during_output"
                except Exception:  # noqa: BLE001 - unknown output state fails safe
                    output_active = True
                    suppress_reason = "wave3_output_activity_unknown"
                    LOGGER.warning("output activity query failed; wake suppressed", exc_info=True)
                if output_active:
                    self._wake_suppressed_during_output += 1
                    record_realtime_trace(
                        "audio_input_wake_suppressed",
                        session_id=self._session_id,
                        stream_epoch=frame.stream_epoch,
                        input_sample_cursor=end_cursor,
                        reason=suppress_reason,
                        hard_cancel_performed=False,
                    )
                else:
                    detection = WakeDetection(
                        stream_epoch=frame.stream_epoch,
                        input_sample_cursor=end_cursor,
                        observed_monotonic_ns=frame.captured_monotonic_ns,
                        probability=probability,
                    )
                    try:
                        self._detections.put_nowait(detection)
                    except queue.Full:
                        record_realtime_trace(
                            "audio_input_wake_detection_dropped",
                            session_id=self._session_id,
                            stream_epoch=frame.stream_epoch,
                            reason="bounded_detection_queue_full",
                        )
                    else:
                        record_realtime_trace(
                            "audio_input_wake_detected",
                            session_id=self._session_id,
                            stream_epoch=frame.stream_epoch,
                            input_sample_cursor=end_cursor,
                            probability=probability,
                            measurement_boundary="software_openwakeword_decision",
                        )
                try:
                    self._wake_engine.reset()
                except Exception:  # noqa: BLE001 - reset best effort, stream drain wins
                    LOGGER.debug("wake engine reset failed after decision", exc_info=True)

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            self._drain_detection_commands()
            frame = self._capture_subscription.read(timeout_s=self._config.worker_poll_s)
            if frame is None:
                continue
            was_active = self._assembler.active
            outcome = self._assembler.feed(frame)
            is_active = self._assembler.active
            if is_active != was_active:
                self._capture_subscription.set_active_utterance(active=is_active)
                if is_active:
                    self._capture_starts += 1
                    record_realtime_trace(
                        "audio_input_capture_started",
                        session_id=self._session_id,
                        stream_epoch=frame.stream_epoch,
                        input_sample_cursor=frame.sample_cursor,
                        measurement_boundary="software_vad_speech_onset",
                    )
            if outcome is not None:
                self._handle_capture_outcome(outcome)

    def _drain_detection_commands(self) -> None:
        while True:
            try:
                detection = self._detections.get_nowait()
            except queue.Empty:
                return
            try:
                if not self._assembler.armed:
                    for outcome in self._assembler.arm(detection):
                        self._handle_capture_outcome(outcome)
            finally:
                self._detections.task_done()

    def _handle_capture_outcome(
        self,
        outcome: CapturedUtterance | UtteranceCaptureFailure,
    ) -> None:
        self._capture_subscription.set_active_utterance(active=False)
        if isinstance(outcome, UtteranceCaptureFailure):
            self._capture_discontinuities += 1
            record_realtime_trace(
                "audio_input_discontinuity",
                session_id=outcome.session_id,
                utterance_id=outcome.utterance_id,
                turn_id=outcome.turn_id,
                stream_epoch=outcome.stream_epoch,
                input_sample_cursor=outcome.input_sample_cursor,
                active_utterance=True,
                outcome="failed_restart_required",
                measurement_boundary="software_subscriber_timeline",
            )
            self._broadcast(
                "error",
                turn_id=outcome.turn_id,
                reason="audio_discontinuity_retry_wake",
            )
            self._assembler.prepare()
            return
        self._endpoint_commits += 1
        record_realtime_trace(
            "audio_input_endpoint_committed",
            session_id=outcome.session_id,
            utterance_id=outcome.utterance_id,
            turn_id=outcome.turn_id,
            stream_epoch=outcome.stream_epoch,
            start_sample_cursor=outcome.start_sample_cursor,
            end_sample_cursor=outcome.end_sample_cursor,
            audio_bytes=len(outcome.audio_bytes),
            endpoint_reason=outcome.endpoint_reason,
            measurement_boundary="software_complete_gap_free_audio",
        )
        self._broadcast("transcribing", turn_id=outcome.turn_id)
        try:
            self._commits.put_nowait(outcome)
        except queue.Full:
            self._commit_queue_full += 1
            record_realtime_trace(
                "audio_input_commit_queue_full",
                session_id=outcome.session_id,
                utterance_id=outcome.utterance_id,
                turn_id=outcome.turn_id,
                policy="reject_newest_no_durable_cursor_advance",
            )
            self._broadcast(
                "error",
                turn_id=outcome.turn_id,
                reason="asr_commit_queue_full",
            )
        self._assembler.prepare()

    def _commit_loop(self) -> None:
        while not self._stop.is_set():
            try:
                utterance = self._commits.get(timeout=self._config.worker_poll_s)
            except queue.Empty:
                continue
            try:
                with realtime_trace_context(
                    session_id=utterance.session_id,
                    utterance_id=utterance.utterance_id,
                    turn_id=utterance.turn_id,
                    stream_epoch=utterance.stream_epoch,
                    channel="inherent_wake",
                ):
                    self._pipeline.run_turn(
                        audio_bytes=utterance.audio_bytes,
                        turn_id=utterance.turn_id,
                        channel="inherent_wake",
                        language="zh-CN",
                        lock_already_held=False,
                        session_id=utterance.session_id,
                        utterance_id=utterance.utterance_id,
                        endpoint_reason=utterance.endpoint_reason,
                    )
            except voice_pipeline.VoicePipelineEmptyError:
                LOGGER.info("realtime wake: empty utterance turn_id=%s", utterance.turn_id)
            except voice_pipeline.VoiceInputBusyError:
                LOGGER.warning("realtime wake: final ASR lane busy turn_id=%s", utterance.turn_id)
                self._broadcast("error", turn_id=utterance.turn_id, reason="asr_busy")
            except Exception:
                LOGGER.exception("realtime wake: ASR commit failed turn_id=%s", utterance.turn_id)
                self._broadcast("error", turn_id=utterance.turn_id, reason="asr_error")
            finally:
                self._commits.task_done()

    def _diagnostic_loop(self) -> None:
        while not self._stop.is_set():
            frame = self._diagnostic_subscription.read(
                timeout_s=self._config.worker_poll_s,
            )
            if frame is None:
                continue
            self._diagnostic_frames += 1
            self._diagnostic_last_cursor = frame.sample_cursor + frame.frame_count

    def _broadcast(self, phase: str, *, turn_id: str, **payload: object) -> None:
        if self._broadcaster is None:
            return
        try:
            self._broadcaster.broadcast_voice_sync(phase, turn_id=turn_id, **payload)
        except Exception:  # noqa: BLE001 - UI cannot break media ownership
            LOGGER.debug("realtime voice broadcast failed phase=%s", phase, exc_info=True)

    def metrics(self) -> VoiceSessionMetrics:
        """Return raw counters for deterministic/live acceptance reports."""
        return VoiceSessionMetrics(
            wake_windows=self._wake_windows,
            wake_detections=self._wake_detections,
            wake_suppressed_during_output=self._wake_suppressed_during_output,
            capture_starts=self._capture_starts,
            endpoint_commits=self._endpoint_commits,
            capture_discontinuities=self._capture_discontinuities,
            commit_queue_full=self._commit_queue_full,
            diagnostic_frames=self._diagnostic_frames,
            diagnostic_last_cursor=self._diagnostic_last_cursor,
            wake_prediction_failures=self._wake_prediction_failures,
        )

    @property
    def ingress(self) -> voice_audio.AudioIngress:
        """Expose lifecycle hooks to runtime (sleep/wake/route change)."""
        return self._ingress

    def close(self) -> VoiceSessionCloseResult:
        """Close ingress first, then join every software owner within one bound."""
        ingress_result = self._ingress.close()
        self._stop.set()
        deadline = time.monotonic() + self._config.shutdown_timeout_s
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            self._wake_engine.close()
        except Exception:  # noqa: BLE001 - report thread/backend ownership separately
            LOGGER.debug("wake engine close failed", exc_info=True)
        alive = tuple(thread.name for thread in self._threads if thread.is_alive())
        result = VoiceSessionCloseResult(
            ingress=ingress_result,
            alive_threads=alive,
            pending_detections=self._detections.qsize(),
            pending_commits=self._commits.qsize(),
        )
        record_realtime_trace(
            "duplex_voice_session_closed",
            session_id=self._session_id,
            definitively_closed=result.definitively_closed,
            alive_threads=",".join(result.alive_threads),
            pending_detections=result.pending_detections,
            pending_commits=result.pending_commits,
        )
        return result


def realtime_input_session_config_from_mapping(
    raw: Mapping[str, object] | None,
) -> RealtimeInputSessionConfig:
    """Parse strict bounded session values from ``single_audio_ingress``."""
    values = {} if raw is None else raw
    defaults = RealtimeInputSessionConfig()

    def _positive_int(key: str, fallback: int) -> int:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.single_audio_ingress.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    def _positive_float(key: str, fallback: float) -> float:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            msg = f"realtime.single_audio_ingress.{key} must be a positive number"
            raise ValueError(msg)
        return float(value)

    return RealtimeInputSessionConfig(
        pre_roll_ms=_positive_int("pre_roll_ms", defaults.pre_roll_ms),
        max_utterance_s=_positive_float(
            "max_utterance_s",
            defaults.max_utterance_s,
        ),
        min_voiced_s=_positive_float("min_voiced_s", defaults.min_voiced_s),
        wake_subscriber_capacity=_positive_int(
            "wake_subscriber_capacity",
            defaults.wake_subscriber_capacity,
        ),
        capture_subscriber_capacity=_positive_int(
            "capture_subscriber_capacity",
            defaults.capture_subscriber_capacity,
        ),
        diagnostic_subscriber_capacity=_positive_int(
            "diagnostic_subscriber_capacity",
            defaults.diagnostic_subscriber_capacity,
        ),
        detection_queue_capacity=_positive_int(
            "detection_queue_capacity",
            defaults.detection_queue_capacity,
        ),
        commit_queue_capacity=_positive_int(
            "commit_queue_capacity",
            defaults.commit_queue_capacity,
        ),
        wake_failure_threshold=_positive_int(
            "wake_failure_threshold",
            defaults.wake_failure_threshold,
        ),
        worker_poll_s=_positive_float("session_worker_poll_s", defaults.worker_poll_s),
        shutdown_timeout_s=_positive_float(
            "session_shutdown_timeout_s",
            defaults.shutdown_timeout_s,
        ),
    )


__all__ = [
    "CapturedUtterance",
    "DuplexVoiceSession",
    "RealtimeInputSessionConfig",
    "UtteranceAssembler",
    "UtteranceCaptureFailure",
    "VoiceSessionCloseResult",
    "VoiceSessionMetrics",
    "VoiceSessionStartResult",
    "WakeDetection",
    "WakeWindowFramer",
    "realtime_input_session_config_from_mapping",
]
