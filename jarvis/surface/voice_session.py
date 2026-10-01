"""Single-ingress wake → pre-roll → endpoint session for ADR-0006 Wave 3.

This module reverses the legacy control flow: capture is already running when
the wake word is detected, and wake framing never stops while utterance VAD,
ASR, or output playback is active.  Wake, capture, and diagnostics consume
separate bounded SPSC subscribers on the same canonical cursor.

The session itself still holds no cancel authority.  A wake hit during
assistant output is suppressed as typed telemetry.  The one stop path is
conversation mode's injected ``stop_speaking`` (ADR 0041), never a call from
this module: neither echo, nor ordinary speech, nor any VAD verdict can reach
Wave-2 playback flush/CAS, ResponseRun cancellation, or action cancellation.
ADR 0053 adds two more injected runtime callables of the same kind:
``hold_output`` keeps answers from starting while Allen is talking, and
``supersede_unspoken`` lets an accepted utterance drop the unspoken answer to
his previous one.
"""

from __future__ import annotations

import contextlib
import enum
import functools
import logging
import os
import queue
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from jarvis.shared.realtime_trace import realtime_trace_context, record_realtime_trace
from jarvis.surface import voice_asr, voice_audio, voice_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared import Event
    from jarvis.surface import voice_backend

LOGGER = logging.getLogger("jarvis.surface.voice_session")

_WAKE_WINDOW_SAMPLES = 1280
# ADR-0006 D7 budget: this many consecutive coalesced (dropped) snapshots
# means partial decode cannot keep pace; the utterance falls back to
# acoustic endpointing instead of building a backlog.
_PARTIAL_DROP_DEGRADE_THRESHOLD = 3


@dataclass(frozen=True)
class PartialAsrConfig:
    """ADR-0006 D7 rolling-partial and semantic-hold bounds; off by default."""

    enabled: bool = False
    interval_ms: int = 240
    candidate_ms: int = 320
    max_hold_ms: int = 900
    post_roll_ms: int = 200


# ADR 0102: how long an accepted turn keeps conversation mode open while its
# answer has not started; a slower answer arrives after the mode has ended.
_ANSWER_WAIT_S = 30.0

# A wake score at or above this is logged, one line per attempt with its peak,
# so a wake that did not fire shows how close it came. Allen's missed takes on
# 2026-09-30 peaked at 0.06-0.21; 20 minutes of room talk never reached 0.1.
_WAKE_LOG_FLOOR = 0.1


@dataclass(frozen=True)
class RealtimeInputSessionConfig:
    """Hard bounds for wake framing, utterance assembly, and worker queues."""

    pre_roll_ms: int = 500
    max_utterance_s: float = 30.0
    armed_no_speech_timeout_s: float = 3.0
    min_voiced_s: float = 1.0
    short_sound_grace_ms: int = 400
    wake_subscriber_capacity: int = 64
    capture_subscriber_capacity: int = 128
    diagnostic_subscriber_capacity: int = 16
    detection_queue_capacity: int = 2
    commit_queue_capacity: int = 2
    wake_failure_threshold: int = 3
    worker_poll_s: float = 0.005
    shutdown_timeout_s: float = 3.0
    output_active_vad_mode: str = "record"
    # Conversation mode's soft barge-in (ADR 0100): speech over Jarvis first
    # lowers her to this gain, and holds her where she is once it has this much
    # voiced time (0 stops her at onset, as ADR 0041 did); shorter sounds end
    # after this much silence, and final ASR decides.
    barge_in_confirm_voiced_s: float = 0.8
    barge_in_yield_gain: float = 0.2
    barge_in_pause_ms: int = 350
    # ADR 0102: conversation mode ends this long after Jarvis last spoke or
    # Allen's last turn was accepted (never mid-utterance); 「等我一下」 holds
    # it open for conversation_wait_s instead.
    conversation_idle_exit_s: float = 10.0
    conversation_wait_s: float = 60.0
    partial_asr: PartialAsrConfig = PartialAsrConfig()


class EndpointPhase(enum.Enum):
    """ADR-0006 D3 Input FSM slice owned by the assembler for one utterance."""

    SPEECH_ACTIVE = "speech_active"
    ENDPOINT_PENDING = "endpoint_pending"
    FINALIZING_ASR = "finalizing_asr"
    COMMITTED = "committed"


@dataclass(frozen=True)
class PartialSnapshot:
    """Bounded utterance-so-far audio handed to the partial decode lane."""

    utterance_id: str
    revision: int
    audio_bytes: bytes


@dataclass(frozen=True)
class PartialRevision:
    """One ephemeral partial hypothesis; never persisted, never sent to L3."""

    utterance_id: str
    revision: int
    text: str
    decode_ms: float
    failed: bool = False


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
class WakeArmExpired:
    """False wake expired before speech; future speech requires a fresh wake."""

    session_id: str
    utterance_id: str
    turn_id: str
    stream_epoch: int
    input_sample_cursor: int
    reason: str = "armed_no_speech_timeout"


@dataclass(frozen=True)
class VoiceSessionStartResult:
    """Typed session startup result."""

    started: bool
    ingress: voice_audio.IngressStartResult


class VoiceSessionPreDeviceError(RuntimeError):
    """Model/session preparation failed before any input-owner attempt."""


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
    armed_no_speech_timeouts: int
    partial_decodes: int = 0
    partial_snapshot_drops: int = 0
    partial_late_revisions_discarded: int = 0


class _PartialDecoderPort(Protocol):
    """Bounded-snapshot decode entry on the sole authoritative recognizer."""

    def partial_text(self, audio_bytes: bytes) -> str:
        """Return an ephemeral partial hypothesis for one snapshot."""
        ...


class _WakeEnginePort(Protocol):
    """Wake engine subset used by the realtime session (ADR-0042: any engine)."""

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
        before_emit: Callable[[str], None] | None = ...,
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


class PartialAsrLane:
    """Latest-only partial decode lane: one pending snapshot, one decode in flight."""

    def __init__(self, decoder: _PartialDecoderPort) -> None:
        """Bind the lane to the sole recognizer's partial entry."""
        self._decoder = decoder
        self._condition = threading.Condition()
        self._pending: PartialSnapshot | None = None
        self._latest: PartialRevision | None = None
        self._cancelled_utterance_id = ""
        self._consecutive_drops = 0
        self.decodes = 0
        self.drops = 0
        self.late_revisions_discarded = 0

    def submit(self, snapshot: PartialSnapshot) -> int:
        """Replace any undecoded snapshot; return the consecutive drop count."""
        with self._condition:
            if self._pending is not None:
                self._consecutive_drops += 1
                self.drops += 1
            else:
                self._consecutive_drops = 0
            self._pending = snapshot
            self._condition.notify()
            return self._consecutive_drops

    def cancel(self, utterance_id: str) -> None:
        """Endpoint commit: drop queued work and invalidate late revisions."""
        with self._condition:
            if self._pending is not None and self._pending.utterance_id == utterance_id:
                self._pending = None
            self._consecutive_drops = 0
            if self._latest is not None and self._latest.utterance_id == utterance_id:
                self._latest = None
            self._cancelled_utterance_id = utterance_id

    def run_once(self, *, timeout_s: float) -> bool:
        """Decode the latest pending snapshot if any; return whether one ran."""
        with self._condition:
            if self._pending is None:
                self._condition.wait(timeout=timeout_s)
            snapshot = self._pending
            self._pending = None
        if snapshot is None:
            return False
        started = time.perf_counter()
        text = ""
        failed = False
        try:
            text = self._decoder.partial_text(snapshot.audio_bytes)
        except Exception:  # noqa: BLE001 - a failed partial degrades, never kills the lane
            failed = True
            LOGGER.warning(
                "partial decode failed utterance_id=%s revision=%s",
                snapshot.utterance_id,
                snapshot.revision,
                exc_info=True,
            )
        decode_ms = (time.perf_counter() - started) * 1_000.0
        with self._condition:
            self.decodes += 1
            if snapshot.utterance_id == self._cancelled_utterance_id:
                self.late_revisions_discarded += 1
                return True
            self._latest = PartialRevision(
                utterance_id=snapshot.utterance_id,
                revision=snapshot.revision,
                text=text,
                decode_ms=decode_ms,
                failed=failed,
            )
        return True

    def take_revision(self) -> PartialRevision | None:
        """Pop the newest unread revision."""
        with self._condition:
            revision, self._latest = self._latest, None
            return revision


class UtteranceAssembler:
    """Wake-armed VAD/pre-roll assembly with explicit gap failure."""

    def __init__(  # noqa: PLR0913 - keyword-only composition boundary
        self,
        *,
        vad: voice_audio.SileroVad,
        config: RealtimeInputSessionConfig,
        sample_rate_hz: int,
        frame_samples: int,
        session_id: str,
        lane: PartialAsrLane | None = None,
        output_active: Callable[[], bool] | None = None,
    ) -> None:
        """Create bounded idle/pre-roll/utterance storage around one VAD."""
        self._vad = vad
        self._config = config
        self._output_active = output_active
        self._output_active_vad_mode = config.output_active_vad_mode
        self._sample_rate_hz = sample_rate_hz
        self._frame_samples = frame_samples
        self._session_id = session_id
        self._lane = lane
        self._partial = config.partial_asr
        self._frame_ms = frame_samples * 1_000.0 / sample_rate_hz

        def _frames(ms: int) -> int:
            return max(1, int(ms * sample_rate_hz / 1_000 / frame_samples + 0.999))

        self._interval_frames = _frames(self._partial.interval_ms)
        self._candidate_frames = _frames(self._partial.candidate_ms)
        self._max_hold_frames = _frames(self._partial.max_hold_ms)
        self._post_roll_frames = _frames(self._partial.post_roll_ms)
        self._acoustic_silence_frames = (
            vad.endpoint_silence_frames if self._partial.enabled else 0
        )
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
        self._short_sound_grace_frames = _frames(config.short_sound_grace_ms)
        self._barge_pause_frames = _frames(config.barge_in_pause_ms)
        # Set while this utterance started over Jarvis and has not yet stopped her.
        self._yielding, self._yield_silence = False, 0
        self._armed_timeout_samples = max(
            frame_samples,
            int(config.armed_no_speech_timeout_s * sample_rate_hz),
        )
        self._state = _AssemblerState.IDLE
        self._stream_epoch: int | None = None
        self._expected_cursor: int | None = None
        self._utterance_id = ""
        self._turn_id = ""
        self._wake_cursor = 0
        self._armed_deadline_cursor = 0
        self._armed_deadline_monotonic_ns = 0
        self._audio_frames: list[bytes] = []
        self._start_cursor = 0
        self._voiced_frames = 0
        self._short_sound_silence = 0
        self._consecutive_silence = 0
        self._last_speech_index = -1
        self._hold_frames = 0
        self._frames_since_snapshot = 0
        self._snapshot_count = 0
        self._accepted_revision = 0
        self._previous_partial: str | None = None
        self._stable_prefix = ""
        self._degraded = False
        self._phase: EndpointPhase | None = None
        self._phase_utterance_id = ""
        # The commit thread marks COMMITTED while the capture thread may start
        # the next utterance; the lock keeps that check-then-set atomic.
        self._phase_lock = threading.Lock()

    @property
    def endpoint_phase(self) -> EndpointPhase | None:
        """Return the D3 phase of the current or most recently finalized utterance."""
        return self._phase

    @property
    def stable_prefix(self) -> str:
        """Return the normalized prefix that survived two consecutive revisions."""
        return self._stable_prefix

    def _set_phase(self, phase: EndpointPhase | None, utterance_id: str) -> None:
        with self._phase_lock:
            self._phase = phase
            self._phase_utterance_id = utterance_id

    def mark_committed(self, utterance_id: str) -> None:
        """Record the durable ``utterance.received`` commit for one utterance."""
        with self._phase_lock:
            if (
                self._phase_utterance_id == utterance_id
                and self._phase is EndpointPhase.FINALIZING_ASR
            ):
                self._phase = EndpointPhase.COMMITTED

    @property
    def active(self) -> bool:
        """Return whether an utterance has crossed speech onset."""
        return self._state is _AssemblerState.ACTIVE

    @property
    def armed(self) -> bool:
        """Return whether a wake decision is awaiting/recording speech."""
        return self._state in {_AssemblerState.ARMED, _AssemblerState.ACTIVE}

    @property
    def turn_id(self) -> str:
        """Return the turn id minted by the most recent :meth:`arm`."""
        return self._turn_id

    @property
    def voiced_frames(self) -> int:
        """Return the speech frames of the active utterance so far."""
        return self._voiced_frames

    def yield_endpoint(self, *, on: bool) -> None:
        """While on, the active utterance also ends after ``barge_in_pause_ms`` of silence.

        For a sound spoken over Jarvis that has not stopped her: she is quieter
        until final ASR judges it, so it should not wait out the pause and the
        short-sound grace of an ordinary sentence.
        """
        self._yielding = on
        self._yield_silence = 0

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
        *,
        expires: bool = True,
    ) -> tuple[CapturedUtterance | UtteranceCaptureFailure | WakeArmExpired, ...]:
        """Arm from the exact wake cursor and replay only already-consumed suffix.

        ``expires=False`` is conversation mode's arm: it waits for speech with
        no ``armed_no_speech_timeout_s`` deadline, until the owner resets it.
        """
        self._state = _AssemblerState.ARMED
        self._stream_epoch = detection.stream_epoch
        self._expected_cursor = detection.input_sample_cursor
        self._utterance_id = "U" + secrets.token_hex(8)
        self._turn_id = "T" + secrets.token_hex(4)
        self._wake_cursor = detection.input_sample_cursor
        self._armed_deadline_cursor = (
            detection.input_sample_cursor + self._armed_timeout_samples
            if expires
            else sys.maxsize
        )
        self._armed_deadline_monotonic_ns = (
            detection.observed_monotonic_ns
            + int(self._config.armed_no_speech_timeout_s * 1_000_000_000)
            if expires
            else 0
        )
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
        outcomes: list[
            CapturedUtterance | UtteranceCaptureFailure | WakeArmExpired
        ] = []
        for frame in replay:
            outcome = self.feed(frame)
            if outcome is not None:
                outcomes.append(outcome)
        return tuple(outcomes)

    def _apply_output_vad_mode(self) -> None:
        """Select the VAD profile for the next frame from the output state.

        Deliberately unlike the wake loop's fail-to-suppressed behaviour: an
        ``output_active`` that raises keeps whatever profile is in effect,
        because failing to the stricter profile on an unknown output state
        could truncate an utterance already in progress.
        """
        if self._output_active is None:
            return
        try:
            speaking = self._output_active()
        except Exception:  # noqa: BLE001 - unknown output state keeps the current profile
            # Not logged: this runs per classified frame, and the wake loop
            # already reports a failing output_active at its own rate.
            return
        self._vad.set_mode(self._output_active_vad_mode if speaking else "record")

    def feed(
        self,
        frame: voice_audio.CanonicalAudioFrame,
    ) -> CapturedUtterance | UtteranceCaptureFailure | WakeArmExpired | None:
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
        if self._state is _AssemblerState.ARMED and (
            self._expected_cursor >= self._armed_deadline_cursor
            or (
                self._armed_deadline_monotonic_ns > 0
                and frame.captured_monotonic_ns >= self._armed_deadline_monotonic_ns
            )
        ):
            expired = WakeArmExpired(
                session_id=self._session_id,
                utterance_id=self._utterance_id,
                turn_id=self._turn_id,
                stream_epoch=frame.stream_epoch,
                input_sample_cursor=frame.sample_cursor + frame.frame_count,
            )
            self.reset_to_idle()
            self._idle_history.clear()
            self._vad.prepare_utterance()
            self.observe_idle(frame)
            return expired
        self._apply_output_vad_mode()
        event = self._vad.feed(frame.pcm16_mono)
        if self._state is _AssemblerState.ARMED:
            self._speech_pre_roll.append(frame)
            if not self._vad.is_speech_detected():
                return None
            self._state = _AssemblerState.ACTIVE
            self._audio_frames = [item.pcm16_mono for item in self._speech_pre_roll]
            self._start_cursor = self._speech_pre_roll[0].sample_cursor
            self._speech_pre_roll.clear()
            self._set_phase(EndpointPhase.SPEECH_ACTIVE, self._utterance_id)
        else:
            self._audio_frames.append(frame.pcm16_mono)
        speech = event is voice_audio.VadEvent.SPEECH_ACTIVE
        if speech:
            self._voiced_frames += 1
        endpoint_reason = self._endpoint(speech=speech)
        if endpoint_reason is None:
            return None
        return self._commit(frame, endpoint_reason)

    def _endpoint(self, *, speech: bool) -> str | None:
        """Return why the utterance ends on this frame, or ``None`` while it goes on."""
        if self._yielding:
            self._yield_silence = 0 if speech else self._yield_silence + 1
            if self._yield_silence >= self._barge_pause_frames:
                return "barge_pause"
        if self._partial.enabled:
            return self._semantic_endpoint(speech=speech)
        reason: str | None = None
        if not speech and self._vad.empty():
            reason = self._pause_endpoint()
        else:
            self._short_sound_silence = 0
        if reason is None and len(self._audio_frames) >= self._max_frames:
            reason = "max_duration"
        return reason

    def _pause_endpoint(self) -> str | None:
        """Acoustic endpoint on one silent frame after the detector's pause.

        Enough voiced audio before the pause ends the utterance there. A
        shorter sound (a cough, a click, 「嗯」, a one-word 「好」) waits
        ``short_sound_grace_ms`` more for words to follow; if none do it ends
        as ``short_pause`` and final ASR decides whether it held any words.
        Without that bound it stayed in flight until ``max_utterance_s``,
        holding every answer (ADR 0074).
        """
        if self._voiced_frames >= self._min_voiced_frames:
            return "acoustic_pause"
        self._short_sound_silence += 1
        if self._short_sound_silence >= self._short_sound_grace_frames:
            return "short_pause"
        return None

    def _commit(
        self,
        frame: voice_audio.CanonicalAudioFrame,
        endpoint_reason: str,
    ) -> CapturedUtterance:
        record_realtime_trace(
            "endpoint_candidate",
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            stream_epoch=frame.stream_epoch,
            input_sample_cursor=frame.sample_cursor + frame.frame_count,
            endpoint_reason=endpoint_reason,
            measurement_boundary="software_correlated_vad_assembler",
        )
        frames = self._audio_frames
        end_sample_cursor = frame.sample_cursor + frame.frame_count
        if (
            self._partial.enabled
            and endpoint_reason != "max_duration"
            and self._last_speech_index >= 0
        ):
            # Post-roll: keep a bounded silence tail after the last speech
            # frame instead of the whole hold window.
            frames = frames[: self._last_speech_index + 1 + self._post_roll_frames]
            end_sample_cursor = self._start_cursor + len(frames) * self._frame_samples
        utterance = CapturedUtterance(
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            stream_epoch=frame.stream_epoch,
            start_sample_cursor=self._start_cursor,
            end_sample_cursor=end_sample_cursor,
            endpoint_reason=endpoint_reason,
            audio_bytes=b"".join(frames),
        )
        self.reset_to_idle()
        self._set_phase(EndpointPhase.FINALIZING_ASR, utterance.utterance_id)
        return utterance

    def _semantic_endpoint(self, *, speech: bool) -> str | None:
        """ADR-0006 D7: acoustic candidate opens a hold; a stable prefix or bound closes it."""
        if speech:
            self._consecutive_silence = 0
            self._last_speech_index = len(self._audio_frames) - 1
        else:
            self._consecutive_silence += 1
        if not self._degraded:
            self._pull_revisions()
        if len(self._audio_frames) >= self._max_frames:
            return "max_duration"
        self._advance_hold(speech=speech)
        if self._phase is not EndpointPhase.ENDPOINT_PENDING:
            return None
        return self._hold_verdict()

    def _advance_hold(self, *, speech: bool) -> None:
        if self._phase is EndpointPhase.ENDPOINT_PENDING:
            if speech:
                self._decide("resume", "speech_resumed")
                self._set_phase(EndpointPhase.SPEECH_ACTIVE, self._utterance_id)
            else:
                self._hold_frames += 1
        # D7 step 1 opens the hold on the pause alone; false onsets are rejected
        # downstream by the final-ASR empty filter. Gating the hold on
        # min_voiced merged a short first sentence into the next one.
        hold_opens = (
            self._phase is EndpointPhase.SPEECH_ACTIVE
            and self._consecutive_silence >= self._candidate_frames
        )
        if hold_opens:
            self._set_phase(EndpointPhase.ENDPOINT_PENDING, self._utterance_id)
            self._hold_frames = 0
            self._decide("hold", "acoustic_pause")
        if not self._degraded:
            self._frames_since_snapshot += 1
            if hold_opens or self._frames_since_snapshot >= self._interval_frames:
                self._submit_snapshot()

    def _hold_verdict(self) -> str | None:
        if self._degraded:
            if self._consecutive_silence >= self._acoustic_silence_frames:
                self._decide("commit", "acoustic_pause")
                return "acoustic_pause"
            return None
        # The stable prefix lags the latest hypothesis by one revision, so a
        # dangling connective can hide in the unstable suffix; judge
        # completeness only once the hypothesis has converged onto the prefix.
        if self._previous_partial == self._stable_prefix and voice_asr.looks_complete(
            self._stable_prefix,
        ):
            self._decide("commit", "semantic_complete")
            return "semantic_complete"
        if self._hold_frames >= self._max_hold_frames:
            self._decide("commit", "max_hold")
            return "max_hold"
        return None

    def _decide(self, verdict: str, reason: str) -> None:
        record_realtime_trace(
            "endpoint_decision",
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            verdict=verdict,
            reason=reason,
            held_ms=round(self._hold_frames * self._frame_ms, 3),
            stable_prefix_len=len(self._stable_prefix),
        )

    def _submit_snapshot(self) -> None:
        if self._lane is None:
            return
        self._frames_since_snapshot = 0
        self._snapshot_count += 1
        # ponytail: whole-utterance snapshot, bounded by max_utterance_s and the
        # over-budget degrade (measured ~15 ms decode per audio second, so
        # utterances beyond ~15 s fall back to acoustic endpointing); upgrade
        # path is a suffix-anchored rolling window.
        drops = self._lane.submit(
            PartialSnapshot(
                utterance_id=self._utterance_id,
                revision=self._snapshot_count,
                audio_bytes=b"".join(self._audio_frames),
            ),
        )
        if drops >= _PARTIAL_DROP_DEGRADE_THRESHOLD:
            self._degrade("coalescing_queue_drops", consecutive_drops=drops)

    def _pull_revisions(self) -> None:
        if self._lane is None:
            return
        revision = self._lane.take_revision()
        if (
            revision is None
            or revision.utterance_id != self._utterance_id
            or revision.revision <= self._accepted_revision
        ):
            return
        self._accepted_revision = revision.revision
        if revision.failed:
            self._degrade("partial_decode_failed", decode_ms=round(revision.decode_ms, 3))
            return
        normalized = voice_asr.normalize_partial_text(revision.text)
        if self._previous_partial is not None:
            common = os.path.commonprefix([self._previous_partial, normalized])
            if len(common) >= len(self._stable_prefix):
                self._stable_prefix = common
        self._previous_partial = normalized
        record_realtime_trace(
            "asr_partial",
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            revision=revision.revision,
            stable_prefix_len=len(self._stable_prefix),
            decode_ms=round(revision.decode_ms, 3),
        )
        if revision.decode_ms > self._partial.interval_ms:
            self._degrade("partial_decode_over_budget", decode_ms=round(revision.decode_ms, 3))

    def _degrade(self, reason: str, **attributes: float) -> None:
        self._degraded = True
        if self._lane is not None:
            self._lane.cancel(self._utterance_id)
        record_realtime_trace(
            "partial_asr_degraded",
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            turn_id=self._turn_id,
            reason=reason,
            **attributes,
        )

    def reset_to_idle(self) -> None:
        """Clear every utterance-local mutable field."""
        self._state = _AssemblerState.IDLE
        self._stream_epoch = None
        self._expected_cursor = None
        self._utterance_id = ""
        self._turn_id = ""
        self._wake_cursor = 0
        self._armed_deadline_cursor = 0
        self._armed_deadline_monotonic_ns = 0
        self._audio_frames.clear()
        self._speech_pre_roll.clear()
        self._voiced_frames = 0
        self._short_sound_silence = 0
        self._yielding = False
        self._yield_silence = 0
        self._consecutive_silence = 0
        self._last_speech_index = -1
        self._hold_frames = 0
        self._frames_since_snapshot = 0
        self._snapshot_count = 0
        self._accepted_revision = 0
        self._previous_partial = None
        self._stable_prefix = ""
        self._degraded = False
        self._set_phase(None, "")


class DuplexVoiceSession:
    """Own wake/capture/commit workers around one continuously sampled ingress."""

    def __init__(  # noqa: PLR0913, PLR0915 - keyword-only composition boundary
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
        mic_muted: Callable[[], bool] | None = None,
        conversation: Callable[[], bool] | None = None,
        set_conversation: Callable[[bool, str], None] | None = None,
        answer_words: Callable[[str, str, str], None] | None = None,
        stop_speaking: Callable[[], object] | None = None,
        hold_output: Callable[[bool], None] | None = None,
        supersede_unspoken: Callable[[str], None] | None = None,
        yield_speaking: Callable[[float], None] | None = None,
        pause_speaking: Callable[[bool], None] | None = None,
    ) -> None:
        """Register all bounded subscribers before any hardware starts.

        ``conversation`` reads the surface's conversation switch (ADR 0041):
        while it is on and the mic is live, capture stays armed without a
        wake hit, and speech that starts while Jarvis is speaking calls
        ``stop_speaking``. ``set_conversation(on, reason)`` flips it (ADR
        0102): a wake hit turns it on, and a dismissal (退下) or
        ``conversation_idle_exit_s`` of quiet turns it off;
        ``answer_words(turn_id, reason, text)`` has her say one line back to
        a dismissal or 「等我一下」 (``reason`` ``dismissed`` or ``wait``). With
        ``yield_speaking`` and ``pause_speaking`` it first only lowers her
        (``barge_in_yield_gain``) and holds her where she is once the speech
        has ``barge_in_confirm_voiced_s`` of voice; final ASR then decides: a
        listening sound, one syllable that says nothing, or nothing lets her
        go on from there with the gain back, a stop request stops her, and
        none becomes a turn.

        ADR 0053: ``hold_output(True)`` from an utterance's speech onset
        until it is accepted or comes to nothing, so no answer starts while
        Allen is talking; ``supersede_unspoken(turn_id)`` once it is
        accepted, before ``utterance.received`` is written.
        """
        self._ingress = ingress
        self._wake_engine = wake_engine
        self._pipeline = pipeline
        self._broadcaster = broadcaster
        self._output_active = output_active
        self._mic_muted = mic_muted
        self._conversation = conversation
        self._set_conversation = set_conversation
        self._answer_words = answer_words
        # ADR 0102: when conversation mode last had an accepted turn or Jarvis's
        # speech; an accepted turn waits for her answer, 「等我一下」 holds it.
        self._conversation_busy_at = time.monotonic()
        self._awaiting_answer = False
        self._conversation_hold_until = 0.0
        self._stop_speaking = stop_speaking
        self._hold_output = hold_output
        self._supersede_unspoken = supersede_unspoken
        self._yield_speaking = yield_speaking
        self._pause_speaking = pause_speaking
        # Soft barge-in: the turns spoken over Jarvis until final ASR has
        # judged their words. "yielding": she is only quieter; "paused": the
        # words held enough voice, so she is also held where she is.
        self._barges: dict[str, str] = {}
        self._barge_lock = threading.Lock()
        self._yielded = False
        self._confirm_voiced_frames = int(
            config.barge_in_confirm_voiced_s * 16_000 / voice_audio.SILERO_CHUNK_SAMPLES + 0.999,
        )
        # Turns whose words are still coming in: spoken, or waiting on ASR.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        # True while the assembler's arm is conversation mode's, not a wake's.
        self._conversation_armed = False
        self._wake_threshold = wake_threshold
        self._config = config
        self._session_id = "S" + secrets.token_hex(8)
        self._partial_lane: PartialAsrLane | None = None
        if config.partial_asr.enabled:
            if not callable(getattr(pipeline, "partial_text", None)):
                msg = "partial_asr.enabled requires a pipeline exposing partial_text()"
                raise TypeError(msg)
            self._partial_lane = PartialAsrLane(pipeline)  # type: ignore[arg-type]
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
            lane=self._partial_lane,
            output_active=output_active,
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
        self._started_threads: list[threading.Thread] = []
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
        self._armed_no_speech_timeouts = 0

    def start(self) -> VoiceSessionStartResult:
        """Prewarm models, start ingress, then start bounded software owners."""
        if self._started:
            msg = "duplex voice session already started"
            raise RuntimeError(msg)
        try:
            self._assembler.prepare()
        except Exception as exc:
            msg = "duplex voice session prepare failed before input device open"
            raise VoiceSessionPreDeviceError(msg) from exc
        ingress_result = self._ingress.start()
        if not ingress_result.started:
            self._wake_subscription.close()
            self._capture_subscription.close()
            self._diagnostic_subscription.close()
            try:
                self._wake_engine.close()
            except Exception:  # noqa: BLE001 - failed ingress must still return typed
                LOGGER.debug("wake engine close failed after ingress start failure", exc_info=True)
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
        if self._partial_lane is not None:
            self._threads = (
                *self._threads,
                threading.Thread(
                    target=self._partial_loop,
                    name="jarvis-partial-asr",
                    daemon=False,
                ),
            )
        self._started_threads.clear()
        try:
            for thread in self._threads:
                thread.start()
                self._started_threads.append(thread)
        except BaseException:
            self._stop.set()
            self._ingress.close()
            deadline = time.monotonic() + self._config.shutdown_timeout_s
            for started_thread in self._started_threads:
                started_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            try:
                self._wake_engine.close()
            except Exception:  # noqa: BLE001 - preserve original start failure
                LOGGER.debug("wake engine close failed after partial start", exc_info=True)
            raise
        self._started = True
        record_realtime_trace(
            "duplex_voice_session_started",
            session_id=self._session_id,
            stream_epoch=self._ingress.stream_epoch,
            hard_cancel_enabled=False,
            natural_barge_in_enabled=False,
            allowed_barge_mode=(
                self.device_profile.allowed_barge_mode
                if self.device_profile is not None
                else "ptt"
            ),
        )
        return VoiceSessionStartResult(started=True, ingress=ingress_result)

    def _wake_loop(self) -> None:  # noqa: C901, PLR0912, PLR0915 - linear drain/decision/suppress/fault loop
        voice_audio.prefer_this_thread()
        framer = WakeWindowFramer()
        consecutive_prediction_failures = 0
        near_peak = 0.0
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
                    if probability >= _WAKE_LOG_FLOOR:
                        near_peak = max(near_peak, probability)
                    elif near_peak:
                        LOGGER.info("wake score %.3f, below %.2f", near_peak, self._wake_threshold)
                        near_peak = 0.0
                    continue
                LOGGER.info("wake score %.3f", probability)
                near_peak = 0.0
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
                    self._enqueue_wake_detection(
                        frame,
                        end_cursor=end_cursor,
                        probability=probability,
                    )
                try:
                    self._wake_engine.reset()
                except Exception:  # noqa: BLE001 - reset best effort, stream drain wins
                    LOGGER.debug("wake engine reset failed after decision", exc_info=True)

    def _enqueue_wake_detection(
        self,
        frame: voice_audio.CanonicalAudioFrame,
        *,
        end_cursor: int,
        probability: float,
    ) -> None:
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

    def _conversation_open(self) -> bool:
        """Conversation mode is on and nothing blocks the mic (mute, GPT-Live)."""
        try:
            return (
                self._conversation is not None
                and self._conversation()
                and not (self._mic_muted is not None and self._mic_muted())
            )
        except Exception:  # noqa: BLE001 - an unreadable switch is off, never always-listening
            return False

    def _keep_conversation_armed(self, frame: voice_audio.CanonicalAudioFrame) -> None:
        """ADR 0041: arm on every idle frame while conversation mode is open.

        Arming at this frame's own cursor replays nothing, and the arm never
        expires; turning the mode off (or muting) drops an arm that has not
        heard speech yet, so no later speech commits without a wake hit.
        """
        if self._conversation_open():
            if not self._assembler.armed:
                self._assembler.arm(
                    WakeDetection(
                        stream_epoch=frame.stream_epoch,
                        input_sample_cursor=frame.sample_cursor,
                        observed_monotonic_ns=frame.captured_monotonic_ns,
                        probability=1.0,
                    ),
                    expires=False,
                )
                self._conversation_armed = True
        elif self._conversation_armed and not self._assembler.active:
            self._assembler.reset_to_idle()
            self._conversation_armed = False

    def _change_conversation(self, *, on: bool, reason: str) -> None:
        if self._set_conversation is None:
            return
        try:
            self._set_conversation(on, reason)
        except Exception:  # noqa: BLE001 - the switch cannot break capture
            LOGGER.warning("set_conversation failed on=%s reason=%s", on, reason, exc_info=True)

    def _leave_idle_conversation(self) -> None:
        """ADR 0102: conversation mode ends ``conversation_idle_exit_s`` after it was last used.

        Used is Jarvis speaking or one of Allen's turns accepted; other voice
        (a hum, a word that says nothing, room talk heard as nothing) does not
        count, but the mode never ends while an utterance is still coming in.
        An accepted turn holds it until her answer starts, for at most
        ``_ANSWER_WAIT_S``; 「等我一下」 holds it ``conversation_wait_s``. The
        clock starts over while the mode is off, so it counts from its start.
        """
        now = time.monotonic()
        if self._set_conversation is None or not self._conversation_open() or self._speaking():
            self._conversation_busy_at = now
            self._awaiting_answer = False
            return
        if self._assembler.active or self._in_flight or now < self._conversation_hold_until:
            return
        if self._awaiting_answer and now - self._conversation_busy_at < _ANSWER_WAIT_S:
            return
        if now - self._conversation_busy_at >= self._config.conversation_idle_exit_s:
            self._conversation_busy_at = now
            self._awaiting_answer = False
            self._change_conversation(on=False, reason="idle")

    def _speaking(self) -> bool:
        try:
            return self._output_active is not None and self._output_active()
        except Exception:  # noqa: BLE001 - unknown output state stops nothing
            return False

    def _barge_in_on_speech(self) -> None:
        """Speech started while Jarvis speaks in conversation mode: yield, or stop her."""
        if not self._speaking() or self._stop_speaking is None:
            return
        record_realtime_trace("conversation_barge_in", session_id=self._session_id)
        if (
            self._confirm_voiced_frames == 0
            or self._yield_speaking is None
            or self._pause_speaking is None
        ):
            threading.Thread(
                target=self._stop_speaking, name="conversation-barge-in", daemon=True,
            ).start()
            return
        with self._barge_lock:
            self._barges[self._assembler.turn_id] = "yielding"
            self._set_yield_locked()
        self._assembler.yield_endpoint(on=True)

    def _confirm_barge_in(self) -> None:
        """Enough voice over her: she holds where she is until it is judged (capture thread).

        A long 「嗯——」 or a cough stopped her for good before (live test
        2026-09-29); held, she goes on from there if it says nothing.
        """
        turn_id = self._assembler.turn_id
        if self._assembler.voiced_frames < self._confirm_voiced_frames:
            return
        with self._barge_lock:
            if self._barges.get(turn_id) != "yielding":
                return
            self._barges[turn_id] = "paused"
        self._assembler.yield_endpoint(on=False)
        record_realtime_trace("conversation_barge_in_confirmed", session_id=self._session_id)
        self._pause(paused=True)

    def _pause(self, *, paused: bool) -> None:
        if self._pause_speaking is None:
            return
        try:
            self._pause_speaking(paused)
        except Exception:  # noqa: BLE001 - output state cannot break capture
            LOGGER.debug("realtime pause_speaking failed", exc_info=True)

    def _stop_now(self) -> None:
        """Stop what Jarvis is still saying; the gain comes back only after."""
        if self._stop_speaking is not None and self._speaking():
            self._stop_speaking()

    def _set_yield_locked(self) -> None:
        """Lower her while any barge-in is undecided, restore once none is."""
        yielding = bool(self._barges)
        if yielding == self._yielded or self._yield_speaking is None:
            return
        self._yielded = yielding
        try:
            self._yield_speaking(self._config.barge_in_yield_gain if yielding else 1.0)
        except Exception:  # noqa: BLE001 - output state cannot break capture
            LOGGER.debug("realtime yield_speaking failed", exc_info=True)

    def _settle_barge_in(self, turn_id: str, *, go_on: bool) -> None:
        """She goes on from where she was, or stops; only then does her gain come back."""
        with self._barge_lock:
            state = self._barges.get(turn_id)
        if state is None:
            return
        if not go_on:
            self._stop_now()
        elif state == "paused":
            self._pause(paused=False)
        with self._barge_lock:
            self._barges.pop(turn_id, None)
            self._set_yield_locked()

    def _end_barge_in(self, turn_id: str) -> None:
        """Its words are lost before a verdict: held, she stops; only lowered, she goes on."""
        with self._barge_lock:
            paused = self._barges.get(turn_id) == "paused"
        self._settle_barge_in(turn_id, go_on=not paused)

    def _judge_words(self, turn_id: str, text: str) -> None:
        """Final ASR's verdict on words spoken over Jarvis, then ADR 0053's drop.

        A listening sound or one syllable that says nothing lets her go on (from
        where she was held, if they were long) and a stop request stops her;
        none of them is a turn. Anything else stops her and is a turn. In
        conversation mode (ADR 0102) a dismissal (退下) stops her and ends the
        mode, 「等我一下」 stops her and holds the mode, and with her silent a
        listening sound or one syllable that says nothing is dropped; none of
        them is a turn. A turn restarts the mode's clock.
        """
        if self._conversation_open():
            self._judge_conversation_words(turn_id, text)
        with self._barge_lock:
            state = self._barges.get(turn_id)
        if state is not None:
            backchannel = voice_asr.is_backchannel(text)
            stop = voice_asr.is_stop_request(text)
            unclear = not stop and voice_asr.is_unclear_sound(text)
            self._settle_barge_in(turn_id, go_on=backchannel or unclear)
            verdict = (
                "backchannel" if backchannel
                else "stop" if stop
                else "unclear" if unclear
                else "turn"
            )
            record_realtime_trace(
                "conversation_barge_in_judged",
                session_id=self._session_id,
                verdict=verdict,
            )
            if verdict != "turn":
                raise voice_pipeline.VoicePipelineAbsorbedError(
                    "stop_request" if stop else verdict,
                )
        self._conversation_busy_at = time.monotonic()
        self._awaiting_answer = True
        self._conversation_hold_until = 0.0
        self._supersede(turn_id)

    def _judge_conversation_words(self, turn_id: str, text: str) -> None:
        """ADR 0102's no-turn words in conversation mode; returns when they are none."""
        with self._barge_lock:
            over_her = turn_id in self._barges
        reason = None
        if voice_asr.is_dismissal(text):
            reason = "dismissed"
            self._settle_barge_in(turn_id, go_on=False)
            self._change_conversation(on=False, reason=reason)
        elif voice_asr.is_wait_request(text):
            reason = "wait"
            self._settle_barge_in(turn_id, go_on=False)
            self._conversation_hold_until = time.monotonic() + self._config.conversation_wait_s
        elif not over_her and (
            voice_asr.is_backchannel(text) or voice_asr.is_unclear_sound(text)
        ):
            reason = "backchannel" if voice_asr.is_backchannel(text) else "unclear"
        if reason in {"dismissed", "wait"} and self._answer_words is not None:
            try:
                self._answer_words(turn_id, reason, text)
            except Exception:  # noqa: BLE001 - her answer cannot break capture
                LOGGER.warning("answer_words failed reason=%s", reason, exc_info=True)
        if reason is not None:
            raise voice_pipeline.VoicePipelineAbsorbedError(reason)

    def _judge_no_words(self, turn_id: str, heard: str, *, addressed: bool = False) -> None:
        """No turn in it: her name or a lone stop word over her stops her, anything else not."""
        self._settle_barge_in(
            turn_id, go_on=not (addressed or voice_asr.is_stop_request(heard)),
        )

    def _mark_in_flight(self, turn_id: str, *, active: bool) -> None:
        """Hold answers while any of Allen's utterances is in flight (ADR 0053)."""
        with self._in_flight_lock:
            before = bool(self._in_flight)
            if active:
                self._in_flight.add(turn_id)
            else:
                self._in_flight.discard(turn_id)
            after = bool(self._in_flight)
            # Inside the lock, so the holds reach the output in the order decided.
            if before != after and self._hold_output is not None:
                try:
                    self._hold_output(after)
                except Exception:  # noqa: BLE001 - output state cannot break capture
                    LOGGER.debug("realtime hold_output failed", exc_info=True)

    def _supersede(self, turn_id: str) -> None:
        if self._supersede_unspoken is None:
            return
        try:
            self._supersede_unspoken(turn_id)
        except Exception:  # an unspoken answer left alone never loses Allen's words
            LOGGER.exception("realtime supersede_unspoken failed turn_id=%s", turn_id)

    def _capture_loop(self) -> None:
        voice_audio.prefer_this_thread()
        while not self._stop.is_set():
            self._drain_detection_commands()
            frame = self._capture_subscription.read(timeout_s=self._config.worker_poll_s)
            if frame is None:
                continue
            self._keep_conversation_armed(frame)
            self._leave_idle_conversation()
            was_active = self._assembler.active
            outcome = self._assembler.feed(frame)
            is_active = self._assembler.active
            if is_active != was_active:
                self._capture_subscription.set_active_utterance(active=is_active)
                if is_active:
                    self._mark_in_flight(self._assembler.turn_id, active=True)
                    self._capture_starts += 1
                    record_realtime_trace(
                        "audio_input_capture_started",
                        session_id=self._session_id,
                        stream_epoch=frame.stream_epoch,
                        input_sample_cursor=frame.sample_cursor,
                        measurement_boundary="software_vad_speech_onset",
                    )
                    if self._conversation_armed:
                        # A wake arm says "listening" at the wake; a
                        # conversation arm has no wake, so it says it here.
                        self._broadcast("listening", turn_id=self._assembler.turn_id)
                        self._barge_in_on_speech()
            if is_active:
                self._confirm_barge_in()
            if outcome is not None:
                self._conversation_armed = False
                self._handle_capture_outcome(outcome)

    def _drain_detection_commands(self) -> None:
        while True:
            try:
                detection = self._detections.get_nowait()
            except queue.Empty:
                return
            try:
                # ADR-0015: a muted microphone drops the wake instead of arming.
                # Barge-in never reaches this queue (see _open_barge_in_candidate),
                # so Allen can still stop Jarvis mid-sentence while muted.
                if self._mic_muted is not None and self._mic_muted():
                    LOGGER.info("wake detection dropped: microphone muted")
                    continue
                if not self._assembler.armed:
                    outcomes = self._assembler.arm(detection)
                    # ADR-0006 §5: listening begins at arm, before speech onset.
                    # Emitted before the replayed frames' outcomes so the card
                    # surfaces while the owner is still speaking, not after.
                    #
                    # arm() replays buffered frames through feed(), and every
                    # feed() that produces an outcome resets the assembler,
                    # clearing _turn_id. Each outcome is built with the freshly
                    # minted turn id just before that reset, so it is the
                    # reliable reader whenever replay produced one.
                    turn_id = outcomes[0].turn_id if outcomes else self._assembler.turn_id
                    if self._assembler.active:
                        self._mark_in_flight(self._assembler.turn_id, active=True)
                    self._broadcast("listening", turn_id=turn_id)
                    # ADR 0102: the wake word opens conversation mode, as a tap does.
                    if not self._conversation_open():
                        self._change_conversation(on=True, reason="wake")
                    for outcome in outcomes:
                        self._handle_capture_outcome(outcome)
            finally:
                self._detections.task_done()

    def _handle_capture_outcome(
        self,
        outcome: CapturedUtterance | UtteranceCaptureFailure | WakeArmExpired,
    ) -> None:
        if self._partial_lane is not None and not isinstance(outcome, WakeArmExpired):
            # D7: the endpoint commit (or capture failure) cancels queued
            # partial work before final ASR can be enqueued.
            self._partial_lane.cancel(outcome.utterance_id)
        self._capture_subscription.set_active_utterance(active=False)
        # A committed utterance stays in flight until its ASR answers.
        self._mark_in_flight(outcome.turn_id, active=isinstance(outcome, CapturedUtterance))
        if isinstance(outcome, WakeArmExpired):
            self._armed_no_speech_timeouts += 1
            record_realtime_trace(
                "audio_input_wake_arm_expired",
                session_id=outcome.session_id,
                utterance_id=outcome.utterance_id,
                turn_id=outcome.turn_id,
                stream_epoch=outcome.stream_epoch,
                input_sample_cursor=outcome.input_sample_cursor,
                reason=outcome.reason,
                measurement_boundary="software_armed_timeout",
            )
            # ADR-0014 D27: `listening` never auto-fades, so the false wake
            # must be terminalized or it strands the card on screen.
            self._broadcast("empty", turn_id=outcome.turn_id, reason=outcome.reason)
            return
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
            self._end_barge_in(outcome.turn_id)
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
            self._end_barge_in(outcome.turn_id)
            self._mark_in_flight(outcome.turn_id, active=False)
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
        voice_audio.prefer_this_thread()
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
                        before_emit=functools.partial(self._judge_words, utterance.turn_id),
                    )
                self._assembler.mark_committed(utterance.utterance_id)
            except voice_pipeline.VoicePipelineAbsorbedError as absorbed:
                LOGGER.info(
                    "realtime wake: words over Jarvis were no turn (%s) turn_id=%s",
                    absorbed.reason, utterance.turn_id,
                )
            except voice_pipeline.VoicePipelineWakeOnlyError:
                # Allen paused after "Hey Jarvis": listen for the question
                # from where the wake phrase ended, as if the wake hit had
                # landed there, instead of answering an empty turn.
                LOGGER.info("realtime wake: wake phrase only turn_id=%s", utterance.turn_id)
                self._judge_no_words(utterance.turn_id, "", addressed=True)
                with contextlib.suppress(queue.Full):  # a newer wake is already queued
                    self._detections.put_nowait(
                        WakeDetection(
                            stream_epoch=utterance.stream_epoch,
                            input_sample_cursor=utterance.end_sample_cursor,
                            observed_monotonic_ns=time.monotonic_ns(),
                            probability=1.0,
                        ),
                    )
            except voice_pipeline.VoicePipelineEmptyError as empty:
                LOGGER.info("realtime wake: empty utterance turn_id=%s", utterance.turn_id)
                self._judge_no_words(utterance.turn_id, empty.heard)
            except voice_pipeline.VoiceInputBusyError:
                LOGGER.warning("realtime wake: final ASR lane busy turn_id=%s", utterance.turn_id)
                self._broadcast("error", turn_id=utterance.turn_id, reason="asr_busy")
            except Exception:
                LOGGER.exception("realtime wake: ASR commit failed turn_id=%s", utterance.turn_id)
                self._broadcast("error", turn_id=utterance.turn_id, reason="asr_error")
            finally:
                self._end_barge_in(utterance.turn_id)
                self._mark_in_flight(utterance.turn_id, active=False)
                self._commits.task_done()

    def _partial_loop(self) -> None:
        lane = self._partial_lane
        if lane is None:
            return
        while not self._stop.is_set():
            try:
                lane.run_once(timeout_s=self._config.worker_poll_s)
            except Exception:
                LOGGER.exception("realtime wake: partial decode failed")

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
        lane = self._partial_lane
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
            armed_no_speech_timeouts=self._armed_no_speech_timeouts,
            partial_decodes=lane.decodes if lane is not None else 0,
            partial_snapshot_drops=lane.drops if lane is not None else 0,
            partial_late_revisions_discarded=(
                lane.late_revisions_discarded if lane is not None else 0
            ),
        )

    @property
    def device_profile(self) -> voice_backend.DeviceProfileSnapshot | None:
        """Expose the ingress-owned D9 snapshot for later barge-in decisions."""
        return self._ingress.device_profile

    @property
    def ingress(self) -> voice_audio.AudioIngress:
        """Expose lifecycle hooks to runtime (sleep/wake/route change)."""
        return self._ingress

    def close(self) -> VoiceSessionCloseResult:
        """Close ingress first, then join every software owner within one bound."""
        ingress_result = self._ingress.close()
        self._stop.set()
        deadline = time.monotonic() + self._config.shutdown_timeout_s
        for thread in self._started_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            self._wake_engine.close()
        except Exception:  # noqa: BLE001 - report thread/backend ownership separately
            LOGGER.debug("wake engine close failed", exc_info=True)
        with self._barge_lock:  # no word left to judge: never leave her quieter, or held
            held = "paused" in self._barges.values()
            self._barges.clear()
            self._set_yield_locked()
        if held:
            self._pause(paused=False)
        alive = tuple(thread.name for thread in self._started_threads if thread.is_alive())
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

    def _vad_mode(key: str, fallback: str) -> str:
        value = values.get(key)
        mode = fallback if value is None else str(value)
        # Unknown names raise ValueError here, which the pre-device path
        # already downgrades to invalid_input_config before anything opens.
        voice_audio.SileroVad.thresholds(mode)
        return mode

    confirm_voiced_s, yield_gain = _barge_in_yield(values, defaults)
    return RealtimeInputSessionConfig(
        pre_roll_ms=_positive_int("pre_roll_ms", defaults.pre_roll_ms),
        max_utterance_s=_positive_float(
            "max_utterance_s",
            defaults.max_utterance_s,
        ),
        armed_no_speech_timeout_s=_positive_float(
            "armed_no_speech_timeout_s",
            defaults.armed_no_speech_timeout_s,
        ),
        min_voiced_s=_positive_float("min_voiced_s", defaults.min_voiced_s),
        short_sound_grace_ms=_positive_int(
            "short_sound_grace_ms",
            defaults.short_sound_grace_ms,
        ),
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
        output_active_vad_mode=_vad_mode(
            "output_active_vad_mode",
            defaults.output_active_vad_mode,
        ),
        barge_in_confirm_voiced_s=confirm_voiced_s,
        barge_in_yield_gain=yield_gain,
        barge_in_pause_ms=_positive_int("barge_in_pause_ms", defaults.barge_in_pause_ms),
        conversation_idle_exit_s=_positive_float(
            "conversation_idle_exit_s", defaults.conversation_idle_exit_s,
        ),
        conversation_wait_s=_positive_float("conversation_wait_s", defaults.conversation_wait_s),
        partial_asr=_partial_asr_config_from_mapping(values.get("partial_asr")),
    )


def _barge_in_yield(
    values: Mapping[str, object], defaults: RealtimeInputSessionConfig,
) -> tuple[float, float]:
    """``barge_in_confirm_voiced_s`` (0 stops at onset) and ``barge_in_yield_gain`` (0-1]."""
    confirm = values.get("barge_in_confirm_voiced_s", defaults.barge_in_confirm_voiced_s)
    if isinstance(confirm, bool) or not isinstance(confirm, int | float) or confirm < 0:
        msg = "realtime.single_audio_ingress.barge_in_confirm_voiced_s must be a number >= 0"
        raise ValueError(msg)
    gain = values.get("barge_in_yield_gain", defaults.barge_in_yield_gain)
    if isinstance(gain, bool) or not isinstance(gain, int | float) or not 0 < gain <= 1:
        msg = "realtime.single_audio_ingress.barge_in_yield_gain must be above 0 and at most 1"
        raise ValueError(msg)
    return float(confirm), float(gain)


def _partial_asr_config_from_mapping(raw: object) -> PartialAsrConfig:
    """Parse the ADR-0006 D7 ``partial_asr`` block; absent means off."""
    if raw is None:
        return PartialAsrConfig()
    if not isinstance(raw, Mapping):
        msg = "realtime.single_audio_ingress.partial_asr must be a mapping"
        raise ValueError(msg)  # noqa: TRY004 - runtime downgrades on ValueError
    defaults = PartialAsrConfig()
    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        msg = "realtime.single_audio_ingress.partial_asr.enabled must be a boolean"
        raise ValueError(msg)  # noqa: TRY004 - runtime downgrades on ValueError

    def _positive_int(key: str, fallback: int) -> int:
        value = raw.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.single_audio_ingress.partial_asr.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    return PartialAsrConfig(
        enabled=enabled,
        interval_ms=_positive_int("interval_ms", defaults.interval_ms),
        candidate_ms=_positive_int("candidate_ms", defaults.candidate_ms),
        max_hold_ms=_positive_int("max_hold_ms", defaults.max_hold_ms),
        post_roll_ms=_positive_int("post_roll_ms", defaults.post_roll_ms),
    )


__all__ = [
    "CapturedUtterance",
    "DuplexVoiceSession",
    "EndpointPhase",
    "PartialAsrConfig",
    "PartialAsrLane",
    "PartialRevision",
    "PartialSnapshot",
    "RealtimeInputSessionConfig",
    "UtteranceAssembler",
    "UtteranceCaptureFailure",
    "VoiceSessionCloseResult",
    "VoiceSessionMetrics",
    "VoiceSessionStartResult",
    "WakeArmExpired",
    "WakeDetection",
    "WakeWindowFramer",
    "realtime_input_session_config_from_mapping",
]
