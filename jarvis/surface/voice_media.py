"""Persistent L5 media owner for generation-safe streaming speech output.

The owner runs one asyncio loop for the daemon lifetime.  Surface watchers
submit committed response events into its bounded command queue; only this
actor may open TTS sessions, mutate playback generations, or own fallback
processes.  The PortAudio callback remains a lock-free consumer and reports
bounded software milestones back to the actor.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np

from jarvis.shared import Event
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import emit_event
from jarvis.state.lifecycle_terminal import terminalize_playback
from jarvis.surface.voice_ledger import (
    AcceptedSamples,
    ForegroundBusy,
    GenerationLease,
    OutputTimelineSnapshot,
    StalePlaybackGeneration,
)
from jarvis.surface.voice_tts import (
    AudioStreamPlayer,
    PlayerStartResult,
    PlayerStopResult,
    TTSAudioChunk,
    TTSResponseSegment,
    TTSSegmentFinished,
    TTSSession,
    _preprocess_for_speech,
)

if TYPE_CHECKING:
    import concurrent.futures
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.surface.voice_ducking import SystemAudioDucker

LOGGER = logging.getLogger(__name__)

_MAX_MEDIA_COMMANDS = 1024
_MAX_AFTER_DRAIN_RESPONSES = 64
_MAX_RESPONSE_TEXT_BYTES = 1_048_576
_MAX_SESSION_COMMANDS = 32
_MAX_SESSION_AUDIO_EVENTS = 256
_MAX_RESPONSE_TIMEOUT_S = 300.0
_MAX_SHUTDOWN_TIMEOUT_S = 10.0
_MAX_EVENT_DRAIN_BATCH = 1024
_MAX_DURABILITY_RETRY_ATTEMPTS = 10
_CHANNEL_TAG_RE = re.compile(r"</?(?:voice|document)>")
_RESPONSE_TERMINAL_TYPES = frozenset({"response.cancelled", "response.failed"})
_RESPONSE_EVENT_TYPES = frozenset(
    {"surface.response_open", "surface.response_chunk", "surface.response_emitted"}
    | _RESPONSE_TERMINAL_TYPES,
)
_TTS_SILENT_CHANNELS = frozenset({"queue_review", "silent_log"})
_SELECT_EVENT_ROWS_THROUGH = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, payload_json, "
    "source_event_id, correlation_json FROM events "
    "WHERE id > ? AND id <= ? ORDER BY id ASC LIMIT ?"
)


def _hydrate_event_row(row: tuple[object, ...]) -> tuple[int, Event]:
    """Hydrate one actor-owned Event Log row while retaining its row id."""
    (
        row_id,
        event_uid,
        event_type,
        schema_version,
        ts_epoch_ms,
        payload_json,
        source_event_id,
        correlation_json,
    ) = row
    if not isinstance(payload_json, str):
        msg = "Event Log payload_json must be text"
        raise TypeError(msg)
    payload = json.loads(payload_json)
    correlation = None
    if isinstance(correlation_json, str):
        correlation = json.loads(correlation_json)
    return int(str(row_id)), Event(
        event_uid=str(event_uid),
        type=str(event_type),
        schema_version=int(str(schema_version)),
        ts_epoch_ms=int(str(ts_epoch_ms)),
        payload=payload,
        source_event_id=(None if source_event_id is None else str(source_event_id)),
        correlation=correlation,
    )


def _response_id(event: Event) -> str | None:
    value = event.payload.get("response_id")
    return value if isinstance(value, str) else None


_ChannelMode = Literal["plain", "voice", "document"]


def _structured_voice_spans(raw_text: str) -> list[tuple[int, int]]:
    """Return voice-only spans after lexing the complete bounded response text.

    Parsing the joined response is the lexical carry: a tag may be split at
    any byte boundary between renderer chunks, while the returned spans are
    later projected back onto those original chunk identities.
    """
    spans: list[tuple[int, int]] = []
    mode: _ChannelMode = "plain"
    cursor = 0
    for match in _CHANNEL_TAG_RE.finditer(raw_text):
        if mode == "voice" and cursor < match.start():
            spans.append((cursor, match.start()))
        tag = match.group(0)
        if tag == "<voice>":
            mode = "voice"
        elif tag == "<document>":
            mode = "document"
        elif (tag == "</voice>" and mode == "voice") or (
            tag == "</document>" and mode == "document"
        ):
            mode = "plain"
        cursor = match.end()
    if mode == "voice" and cursor < len(raw_text):
        spans.append((cursor, len(raw_text)))
    return spans

MediaSubmitStatus = Literal[
    "accepted",
    "duplicate",
    "historical",
    "unregistered",
    "terminal",
    "closed",
    "overloaded",
    "invalid",
]


@dataclass(frozen=True)
class MediaSubmitOutcome:
    """Bounded watcher/direct-delivery disposition."""

    status: MediaSubmitStatus
    event_uid: str
    response_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class MediaPowerTransitionResult:
    """Bounded power-lifecycle result for the persistent output owner."""

    status: Literal["suspended", "resumed", "uncertain", "closed"]
    attempt_id: int
    reason: str
    helper_thread_alive: bool = False
    deadline_exhausted: bool = False

    @property
    def succeeded(self) -> bool:
        """Return whether the requested output state is proven."""
        return self.status in {"suspended", "resumed", "closed"}


class StreamingMediaStartupError(RuntimeError):
    """Bounded startup failure with an explicit audio-device isolation debt."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        device_disposition: Literal["not_attempted", "failed_closed", "uncertain"],
    ) -> None:
        """Record the phase and exact typed output-ownership disposition."""
        super().__init__(message)
        self.phase = phase
        self.device_disposition = device_disposition
        self.device_state_uncertain = device_disposition == "uncertain"

    @property
    def legacy_fallback_safe(self) -> bool:
        """Return whether no prior physical output ownership can overlap."""
        return self.device_disposition in {"not_attempted", "failed_closed"}


class StreamingTTSProvider(Protocol):
    """Provider factory consumed only by the persistent media loop."""

    @property
    def streaming_candidate_count(self) -> int:
        """Return ordered prefix-safe endpoint candidates."""

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> TTSSession:
        """Create a closed response-scoped session."""


@dataclass(frozen=True)
class StreamingMediaConfig:
    """All queues and network/presentation waits are explicitly bounded."""

    canonical_sample_rate_hz: int = 48_000
    command_queue_capacity: int = 64
    response_lane_capacity: int = 8
    response_text_bytes: int = 256 * 1024
    session_command_capacity: int = 2
    session_audio_capacity: int = 16
    session_idle_close_s: float = 10.0
    response_timeout_s: float = 45.0
    ring_retry_s: float = 0.002
    presentation_poll_s: float = 0.005
    shutdown_timeout_s: float = 3.0
    event_drain_batch: int = 128
    terminal_retry_attempts: int = 3
    checkpoint_retry_attempts: int = 3
    durability_retry_s: float = 0.02
    enable_macos_say_fallback: bool = True
    speak_from_segments: bool = False

    def __post_init__(self) -> None:
        """Reject unbounded or non-positive actor budgets."""
        positive_ints = (
            self.canonical_sample_rate_hz,
            self.command_queue_capacity,
            self.response_lane_capacity,
            self.response_text_bytes,
            self.session_command_capacity,
            self.session_audio_capacity,
            self.event_drain_batch,
            self.terminal_retry_attempts,
            self.checkpoint_retry_attempts,
        )
        positive_floats = (
            self.session_idle_close_s,
            self.response_timeout_s,
            self.ring_retry_s,
            self.presentation_poll_s,
            self.shutdown_timeout_s,
            self.durability_retry_s,
        )
        if any(value <= 0 for value in positive_ints + positive_floats):
            msg = "streaming media bounds must all be positive"
            raise ValueError(msg)
        if (
            self.command_queue_capacity > _MAX_MEDIA_COMMANDS
            or self.response_lane_capacity > _MAX_AFTER_DRAIN_RESPONSES
            or self.response_text_bytes > _MAX_RESPONSE_TEXT_BYTES
            or self.session_command_capacity > _MAX_SESSION_COMMANDS
            or self.session_audio_capacity > _MAX_SESSION_AUDIO_EVENTS
            or self.response_timeout_s > _MAX_RESPONSE_TIMEOUT_S
            or self.shutdown_timeout_s > _MAX_SHUTDOWN_TIMEOUT_S
            or self.event_drain_batch > _MAX_EVENT_DRAIN_BATCH
            or self.terminal_retry_attempts > _MAX_DURABILITY_RETRY_ATTEMPTS
            or self.checkpoint_retry_attempts > _MAX_DURABILITY_RETRY_ATTEMPTS
        ):
            msg = "streaming media bounds exceed validated production maxima"
            raise ValueError(msg)


@dataclass(frozen=True)
class _ResponseChunk:
    sequence: int
    raw_text: str
    segment_hash: str
    source_event_uid: str = ""


@dataclass
class _ResponseBuffer:
    row_id: int
    source_event_id: str
    response_id: str
    response_group_id: str
    turn_id: str
    phase: str
    channel: str
    gate_mode: str
    chunks: dict[int, _ResponseChunk] = field(default_factory=dict)
    emitted: bool = False
    stream: bool = False
    incremental: bool = False
    scheduled: bool = False
    tagged: bool = False
    updated: asyncio.Event = field(default_factory=asyncio.Event)

    def append_voice_suffix(self, voice_text: object, *, source_event_uid: str) -> None:
        """Speak the emitted voice text no chunk carried as one final segment."""
        sequences = sorted(self.chunks)
        if not isinstance(voice_text, str) or sequences != list(range(len(sequences))):
            return
        committed = "".join(self.chunks[index].raw_text for index in sequences)
        if len(voice_text) <= len(committed) or not voice_text.startswith(committed):
            return
        suffix = voice_text[len(committed) :]
        self.chunks[len(sequences)] = _ResponseChunk(
            sequence=len(sequences),
            raw_text=suffix,
            segment_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            source_event_uid=source_event_uid,
        )

    def speech_segments(self) -> list[tuple[int, str, str]]:
        """Map joined structured spans back to exact renderer identities."""
        ordered = [self.chunks[key] for key in sorted(self.chunks)]
        raw = "".join(chunk.raw_text for chunk in ordered)
        structured = _CHANNEL_TAG_RE.search(raw) is not None
        voice_spans = _structured_voice_spans(raw) if structured else []
        segments: list[tuple[int, str, str]] = []
        chunk_start = 0
        for chunk in ordered:
            if structured:
                chunk_end = chunk_start + len(chunk.raw_text)
                raw_speech = "".join(
                    raw[max(chunk_start, start) : min(chunk_end, end)]
                    for start, end in voice_spans
                    if start < chunk_end and end > chunk_start
                )
            else:
                raw_speech = chunk.raw_text
            text = _preprocess_for_speech(raw_speech)
            if text:
                segments.append((chunk.sequence, text, chunk.segment_hash))
            chunk_start += len(chunk.raw_text)
        return segments

    def speech_text(self) -> str:
        return "".join(text for _sequence, text, _hash in self.speech_segments())


@dataclass
class _ActiveResponse:
    response: _ResponseBuffer
    lease: GenerationLease
    task: asyncio.Task[None] | None = None
    presentation_task: asyncio.Task[None] | None = None
    session: TTSSession | None = None
    fallback_process: asyncio.subprocess.Process | None = None
    fallback_spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
    fallback_janitor_task: asyncio.Task[bool] | None = None
    output_lease: bool = False
    last_checkpoint_sequence: int | None = None
    provider_label: str = "minimax_ws_streaming"
    advance_after_cleanup: bool = False
    terminal_commit_pending: bool = False
    activation_event_uid: str | None = None
    prepared_text: str = ""
    starvation_gaps_at_start: int = 0
    host_underflows_at_start: int = 0


@dataclass(frozen=True)
class _MediaCommand:
    row_id: int
    event: Event
    origin: str
    acknowledged: asyncio.Future[MediaSubmitOutcome]


class ActivePlaybackRegistry:
    """Current-boot high-water, registration, dedup, and tombstone boundary."""

    _RECENT_EVENT_LIMIT = 8192
    _RECENT_TERMINAL_LIMIT = 8192
    _ACTIVE_RESPONSE_LIMIT = 1024
    _SEQUENCES_PER_RESPONSE_LIMIT = 4096

    def __init__(self, *, boot_high_water_id: int) -> None:
        """Capture the only replay boundary accepted for this boot."""
        self.boot_id = "BOOT" + uuid.uuid4().hex
        self.boot_high_water_id = boot_high_water_id
        self._seen_events: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._registered: set[str] = set()
        self._terminal: set[str] = set()
        self._terminal_order: deque[str] = deque()
        self._sequences: set[tuple[str, int]] = set()
        self._sequence_counts: dict[str, int] = {}
        self._emitted: set[str] = set()

    def classify(  # noqa: C901, PLR0911, PLR0912 - lifecycle disposition table
        self,
        *,
        row_id: int,
        event: Event,
    ) -> MediaSubmitOutcome:
        """Reject pre-boot, duplicate, unregistered, and terminal deliveries."""
        response_raw = event.payload.get("response_id")
        response_id = response_raw if isinstance(response_raw, str) else None
        if row_id <= self.boot_high_water_id:
            return MediaSubmitOutcome(
                status="historical",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.event_uid in self._seen_events:
            return MediaSubmitOutcome(
                status="duplicate",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        self._remember_event(event.event_uid)
        if response_id is None or not response_id:
            return MediaSubmitOutcome(
                status="invalid",
                event_uid=event.event_uid,
                detail="streaming response event requires stable response_id",
            )
        if response_id in self._terminal:
            return MediaSubmitOutcome(
                status="terminal",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.type == "surface.response_open":
            if response_id in self._registered:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            if len(self._registered) >= self._ACTIVE_RESPONSE_LIMIT:
                return MediaSubmitOutcome(
                    status="overloaded",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="current-boot active response registry is full",
                )
            self._registered.add(response_id)
            return MediaSubmitOutcome("accepted", event.event_uid, response_id)
        if response_id not in self._registered:
            return MediaSubmitOutcome(
                status="unregistered",
                event_uid=event.event_uid,
                response_id=response_id,
            )
        if event.type == "surface.response_chunk":
            if response_id in self._emitted:
                return MediaSubmitOutcome(
                    status="terminal",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="response chunk arrived after response_emitted",
                )
            sequence_raw = event.payload.get("sequence")
            if not isinstance(sequence_raw, int) or isinstance(sequence_raw, bool):
                return MediaSubmitOutcome(
                    status="invalid",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="streaming response chunk requires integer sequence",
                )
            key = (response_id, sequence_raw)
            if key in self._sequences:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            if (
                self._sequence_counts.get(response_id, 0)
                >= self._SEQUENCES_PER_RESPONSE_LIMIT
            ):
                self.terminalize(response_id)
                return MediaSubmitOutcome(
                    status="overloaded",
                    event_uid=event.event_uid,
                    response_id=response_id,
                    detail="bounded response segment registry is full",
                )
            self._sequences.add(key)
            self._sequence_counts[response_id] = (
                self._sequence_counts.get(response_id, 0) + 1
            )
        elif event.type == "surface.response_emitted":
            if response_id in self._emitted:
                return MediaSubmitOutcome(
                    status="duplicate",
                    event_uid=event.event_uid,
                    response_id=response_id,
                )
            self._emitted.add(response_id)
        return MediaSubmitOutcome("accepted", event.event_uid, response_id)

    def has_seen(self, event_uid: str) -> bool:
        """Return whether the actor already drained this committed event."""
        return event_uid in self._seen_events

    def terminalize(self, response_id: str) -> None:
        """Prevent every late same-boot delivery after playback terminal."""
        if response_id not in self._terminal:
            self._terminal.add(response_id)
            self._terminal_order.append(response_id)
            if len(self._terminal_order) > self._RECENT_TERMINAL_LIMIT:
                self._terminal.discard(self._terminal_order.popleft())
        self._registered.discard(response_id)
        self._emitted.discard(response_id)
        self._sequences = {key for key in self._sequences if key[0] != response_id}
        self._sequence_counts.pop(response_id, None)

    def _remember_event(self, event_uid: str) -> None:
        self._seen_events.add(event_uid)
        self._seen_order.append(event_uid)
        if len(self._seen_order) > self._RECENT_EVENT_LIMIT:
            self._seen_events.discard(self._seen_order.popleft())


class _SegmentResampler:
    """Independent int16-LE provider segment to canonical float32-LE PCM."""

    def __init__(self, *, input_rate_hz: int, output_rate_hz: int) -> None:
        self._input_rate_hz = input_rate_hz
        self._output_rate_hz = output_rate_hz
        self._carry = b""
        self._closed = False
        self._resampler: Any | None = None
        if input_rate_hz != output_rate_hz:
            import soxr  # noqa: PLC0415

            self._resampler = soxr.ResampleStream(
                input_rate_hz,
                output_rate_hz,
                1,
                dtype="float32",
                quality="HQ",
            )

    def feed(self, pcm: bytes) -> bytes:
        """Decode and incrementally resample one provider chunk."""
        if self._closed:
            msg = "cannot feed a finalized segment resampler"
            raise RuntimeError(msg)
        raw = self._carry + pcm
        aligned = len(raw) - (len(raw) % 2)
        self._carry = raw[aligned:]
        if aligned == 0:
            return b""
        samples = np.frombuffer(raw[:aligned], dtype="<i2").astype(np.float32)
        samples /= 32768.0
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        return samples.astype(np.float32, copy=False).tobytes()

    def finish(self) -> bytes:
        """Finalize only after TTSSegmentFinished; discard one impossible odd byte."""
        if self._closed:
            return b""
        self._closed = True
        self._carry = b""
        if self._resampler is None:
            return b""
        tail = self._resampler.resample_chunk(
            np.zeros(0, dtype=np.float32),
            last=True,
        )
        return np.asarray(tail, dtype=np.float32).tobytes()


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
    ) -> None:
        """Start one persistent actor without letting stuck startup pin exit."""
        self._provider = provider
        self._player = player
        self._conn_factory = conn_factory
        self._config = config or StreamingMediaConfig()
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._foreground_decision = foreground_decision_callable
        self._start_player = start_player
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
        self._power_lock = threading.Lock()
        self._power_suspended = False
        self._power_state = "running"
        self._power_attempt_id = 0
        self._power_helper: threading.Thread | None = None
        self._power_helper_done = threading.Event()
        self._power_helper_error: BaseException | None = None
        self._power_helper_reason = ""
        self._power_last_timed_out_attempt_id: int | None = None
        self._power_shutdown = False
        self._power_admission_generation = 0
        self._power_actor_resume_attempt_id: int | None = None
        self._power_actor_resume_generation: int | None = None
        self._power_actor_resume_future: concurrent.futures.Future[bool] | None = None
        self._unadmitted_wake_attempt_id: int | None = None
        self._power_abort_attempt_id: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_MediaCommand | object] | None = None
        self._conn: sqlite3.Connection | None = None
        self._startup_error: BaseException | None = None
        self._startup_error_phase: str | None = None
        self._startup_device_disposition: Literal[
            "not_attempted", "failed_closed", "uncertain"
        ] = "not_attempted"
        self._shutdown_requested = threading.Event()
        self._deadline_lock = threading.Lock()
        self._shutdown_deadline = float("inf")
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
        self._lane_isolated = False
        self._terminal_debt: _ActiveResponse | None = None
        self._fallback_janitors: set[asyncio.Task[bool]] = set()
        self._event_cursor = boot_high_water_id
        self._responses: dict[str, _ResponseBuffer] = {}
        self._after_drain: deque[_ResponseBuffer] = deque()
        self._active: _ActiveResponse | None = None
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

    def stop_foreground_output(self, response_id: str) -> str:
        """Stop the speech audible right now, leaving its ResponseRun alone.

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

        The caller's ``reason`` is not carried: a stop through this method
        is a user stop, and its terminal always says ``user_stop``.
        """
        loop = self._loop
        if loop is None or self._closed.is_set():
            return "stale"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._stop_foreground_output_owned(response_id),
                loop,
            )
        except RuntimeError:
            return "stale"
        try:
            return future.result(timeout=self._config.shutdown_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            # The actor took the request; whether the tombstone landed is
            # exactly what this caller cannot know.
            return "uncertain"

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
            and self._provider_stop_done.is_set()
            and self._player_stop_done.is_set()
            and not self._player_stop_definitive
        ):
            self._stop_player_bounded()
        complete = self.cleanup_complete
        if not complete:
            record_realtime_trace(
                "media_owner_cleanup_degraded",
                actor_stopped=not self._thread.is_alive(),
                provider_stopped=self._provider_stop_done.is_set(),
                player_stopped=self._player_stop_done.is_set(),
                startup_phase=self._get_startup_phase(),
            )
        return complete

    @property
    def cleanup_complete(self) -> bool:
        """Report confirmed teardown, including late daemon-janitor completion."""
        with self._cleanup_lock:
            clean = not self._cleanup_errors
        return (
            not self._thread.is_alive()
            and self._closed.is_set()
            and self._provider_stop_done.is_set()
            and self._player_stop_done.is_set()
            and self._player_stop_definitive
            and clean
        )

    def _request_shutdown_deadline(self, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        self._publish_shutdown_deadline(deadline)

    def _publish_shutdown_deadline(self, deadline: float) -> None:
        """Atomically publish an absolute deadline that can only move earlier."""
        with self._deadline_lock:
            self._shutdown_deadline = min(self._shutdown_deadline, deadline)
        with self._power_lock:
            self._power_shutdown = True
            self._shutdown_requested.set()

    def _remaining_s(self, cap_s: float) -> float:
        with self._deadline_lock:
            deadline = self._shutdown_deadline
        if deadline == float("inf"):
            return cap_s
        return min(cap_s, max(0.0, deadline - time.monotonic()))

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
                self._config.shutdown_timeout_s
                if timeout_s is None
                else max(0.0, timeout_s)
            )
        )
        with self._power_lock:
            # Linearize sleep before any actor-side admission from an older
            # wake Future. The coroutine validates this generation while it
            # owns the same lock around every side effect.
            self._power_admission_generation += 1
        self._accepting.clear()
        loop = self._loop
        if loop is None or self._closed.is_set():
            return MediaPowerTransitionResult(
                "closed",
                self._power_attempt_id,
                "actor_closed",
            )
        with self._power_lock:
            if self._power_state == "suspended":
                return MediaPowerTransitionResult(
                    "suspended",
                    self._power_attempt_id,
                    "already_suspended",
                )
            if self._power_state == "starting":
                # A sleep generation must revoke a wake open already inside
                # foreign ``player.start``. The exact start helper owns the
                # compensating close when it returns; this caller only joins.
                attempt_id = self._power_attempt_id
                self._power_abort_attempt_id = attempt_id
                done = self._power_helper_done
                helper = self._power_helper
                launch = False
            elif self._power_state == "stopping":
                attempt_id = self._power_attempt_id
                done = self._power_helper_done
                helper = self._power_helper
                launch = False
            elif self._power_state not in {"running", "open_unadmitted", "uncertain"}:
                return MediaPowerTransitionResult(
                    "uncertain",
                    self._power_attempt_id,
                    f"power_state_{self._power_state}",
                )
            else:
                self._power_attempt_id += 1
                attempt_id = self._power_attempt_id
                done = threading.Event()
                self._power_helper_done = done
                self._power_helper_error = None
                self._power_helper_reason = ""
                self._power_state = "stopping"
                helper = None
                launch = True
        if launch:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._suspend_for_sleep_owned(),
                    loop,
                )
            except BaseException as exc:  # noqa: BLE001 - exact attempt failure
                with self._power_lock:
                    if self._power_attempt_id == attempt_id:
                        self._power_helper_error = exc
                        self._power_helper_reason = (
                            f"actor_terminalization:{type(exc).__name__}"
                        )
                        self._power_state = "uncertain"
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
                            result = self._stop_player_with_bound(
                                self._config.shutdown_timeout_s,
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
                    with self._power_lock:
                        late = self._power_last_timed_out_attempt_id == attempt_id
                        if self._power_attempt_id == attempt_id:
                            self._power_helper_error = error
                            self._power_helper_reason = reason
                            self._power_state = (
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
                with self._power_lock:
                    if self._power_attempt_id == attempt_id:
                        self._power_helper = helper
                try:
                    helper.start()
                except RuntimeError as exc:
                    with self._power_lock:
                        if self._power_attempt_id == attempt_id:
                            self._power_helper_error = exc
                            self._power_helper_reason = "continuation_thread_start_failed"
                            self._power_state = "uncertain"
                    done.set()
        if not done.wait(timeout=max(0.0, transition_deadline - time.monotonic())):
            with self._power_lock:
                self._power_last_timed_out_attempt_id = attempt_id
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                "actor_terminalization_or_player_stop_timeout",
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                deadline_exhausted=time.monotonic() >= transition_deadline,
            )
        with self._power_lock:
            state = self._power_state
            error = self._power_helper_error
            reason = self._power_helper_reason
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
                self._config.shutdown_timeout_s
                if timeout_s is None
                else max(0.0, timeout_s)
            )
        )
        with self._power_lock:
            prior_state = self._power_state
            prior_done = self._power_helper_done
        if prior_state == "stopping" and not prior_done.wait(
            timeout=max(0.0, transition_deadline - time.monotonic()),
        ):
            return MediaPowerTransitionResult(
                "uncertain",
                self._power_attempt_id,
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
        loop = self._loop
        if loop is None or self._closed.is_set():
            return MediaPowerTransitionResult(
                "closed",
                started.attempt_id,
                "actor_closed_after_start",
            )
        with self._power_lock:
            future = self._power_actor_resume_future
            admission_generation = self._power_admission_generation
            if (
                future is None
                or self._power_actor_resume_attempt_id != started.attempt_id
                or self._power_actor_resume_generation != admission_generation
            ):
                future = asyncio.run_coroutine_threadsafe(
                    self._resume_after_wake_owned(
                        attempt_id=started.attempt_id,
                        admission_generation=admission_generation,
                    ),
                    loop,
                )
                self._power_actor_resume_attempt_id = started.attempt_id
                self._power_actor_resume_generation = admission_generation
                self._power_actor_resume_future = future
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
        with self._power_lock:
            if (
                self._power_shutdown
                or self._closed.is_set()
                or self._power_admission_generation != admission_generation
                or self._unadmitted_wake_attempt_id != started.attempt_id
                or self._power_state != "open_unadmitted"
            ):
                return MediaPowerTransitionResult(
                    "closed" if self._power_shutdown or self._closed.is_set() else "uncertain",
                    started.attempt_id,
                    (
                        "shutdown_revoked_actor_resume"
                        if self._power_shutdown or self._closed.is_set()
                        else "actor_resume_generation_revoked"
                    ),
                )
            self._power_state = "running"
        record_realtime_trace(
            "media_power_resumed",
            attempt_id=started.attempt_id,
            ownership="fresh_player_stream",
        )
        return started

    def revoke_wake_starts_for_shutdown(self) -> None:
        """Revoke wake-created output without stopping an existing running owner."""
        with self._power_lock:
            self._power_shutdown = True
            compensate_open_unadmitted = self._power_state == "open_unadmitted" or (
                self._power_state == "running"
                and self._unadmitted_wake_attempt_id is not None
            )
            state = self._power_state
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
        with self._power_lock:
            if (
                self._power_shutdown
                or self._power_state != "running"
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
        with self._power_lock:
            if self._unadmitted_wake_attempt_id != attempt_id:
                return MediaPowerTransitionResult(
                    "closed",
                    attempt_id,
                    "wake_attempt_already_retired",
                )
            self._power_abort_attempt_id = attempt_id
            self._power_admission_generation += 1
            state = self._power_state
        self._accepting.clear()
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
                    else time.monotonic() + self._config.shutdown_timeout_s
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
        with self._power_lock:
            if action == "start" and (self._power_shutdown or self._closed.is_set()):
                return MediaPowerTransitionResult(
                    "closed",
                    self._power_attempt_id,
                    "shutdown_revoked_player_start",
                )
            if self._power_state == desired:
                status: Literal["suspended", "resumed"] = (
                    "suspended" if action == "stop" else "resumed"
                )
                return MediaPowerTransitionResult(
                    status,
                    self._power_attempt_id,
                    f"already_{desired}",
                )
            allowed = (
                {"running", "open_unadmitted", "uncertain"}
                if action == "stop"
                else {"suspended"}
            )
            if self._power_state == transitional:
                done = self._power_helper_done
                helper = self._power_helper
                attempt_id = self._power_attempt_id
            elif self._power_state not in allowed:
                return MediaPowerTransitionResult(
                    "uncertain",
                    self._power_attempt_id,
                    f"power_state_{self._power_state}",
                )
            else:
                self._power_attempt_id += 1
                attempt_id = self._power_attempt_id
                done = threading.Event()
                self._power_helper_done = done
                self._power_helper_error = None
                self._power_helper_reason = ""
                self._power_state = transitional
                if action == "start":
                    self._power_admission_generation += 1
                    self._unadmitted_wake_attempt_id = attempt_id
                    self._power_abort_attempt_id = None

                def _operate() -> None:  # noqa: C901, PLR0912, PLR0915 - exact start/revoke compensation
                    error: BaseException | None = None
                    reason = ""
                    shutdown_cleanup: bool | None = None
                    try:
                        if action == "stop":
                            remaining = max(0.0, deadline - time.monotonic())
                            self._stop_player_until_definitive(remaining)
                        else:
                            with self._power_lock:
                                revoked_before_start = (
                                    self._power_attempt_id != attempt_id
                                    or self._power_shutdown
                                    or self._power_abort_attempt_id == attempt_id
                                    or self._closed.is_set()
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
                                with self._power_lock:
                                    revoked_after_start = (
                                        self._power_attempt_id != attempt_id
                                        or self._power_shutdown
                                        or self._power_abort_attempt_id == attempt_id
                                        or self._closed.is_set()
                                    )
                                if revoked_after_start:
                                    if typed_failed_closed:
                                        shutdown_cleanup = True
                                    else:
                                        try:
                                            stopped = self._stop_player_with_bound(
                                                self._config.shutdown_timeout_s,
                                            )
                                            if isinstance(stopped, PlayerStopResult):
                                                shutdown_cleanup = stopped.definitively_closed
                                                if not shutdown_cleanup:
                                                    reason = stopped.reason
                                                    if not self._power_shutdown:
                                                        self._stop_player_until_definitive(
                                                            self._config.shutdown_timeout_s,
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
                            with self._power_lock:
                                revoked_after_error = (
                                    self._power_attempt_id != attempt_id
                                    or self._power_shutdown
                                    or self._power_abort_attempt_id == attempt_id
                                    or self._closed.is_set()
                                )
                            if revoked_after_error:
                                try:
                                    stopped = self._stop_player_with_bound(
                                        self._config.shutdown_timeout_s,
                                    )
                                    shutdown_cleanup = (
                                        stopped.definitively_closed
                                        if isinstance(stopped, PlayerStopResult)
                                        else True
                                    )
                                except BaseException:  # noqa: BLE001 - retained debt
                                    shutdown_cleanup = False
                    finally:
                        with self._power_lock:
                            if self._power_attempt_id == attempt_id:
                                self._power_helper_error = error
                                self._power_helper_reason = reason
                                if action == "start" and shutdown_cleanup is not None:
                                    self._power_state = (
                                        "suspended" if shutdown_cleanup else "uncertain"
                                    )
                                    if shutdown_cleanup:
                                        self._unadmitted_wake_attempt_id = None
                                        self._power_abort_attempt_id = None
                                else:
                                    self._power_state = (
                                        desired if error is None else "uncertain"
                                    )
                                    if action == "stop" and error is None:
                                        self._unadmitted_wake_attempt_id = None
                                        self._power_abort_attempt_id = None
                        if action == "start" and shutdown_cleanup is not None:
                            if self._power_shutdown:
                                self._record_shutdown_player_cleanup(
                                    definitive=shutdown_cleanup,
                                )
                            record_realtime_trace(
                                "media_power_start_revoked",
                                attempt_id=attempt_id,
                                compensating_stop_definitive=shutdown_cleanup,
                                reason=reason or "shutdown_revoked_late_start",
                            )
                        elif action == "stop" and self._power_shutdown:
                            self._record_shutdown_player_cleanup(
                                definitive=error is None,
                            )
                        done.set()

                helper = threading.Thread(
                    target=_operate,
                    name=f"jarvis-media-power-{action}-a{attempt_id}",
                    daemon=True,
                )
                self._power_helper = helper
                try:
                    helper.start()
                except RuntimeError as exc:
                    self._power_helper_error = exc
                    self._power_state = "uncertain"
                    done.set()
        if not done.wait(timeout=max(0.0, deadline - time.monotonic())):
            return MediaPowerTransitionResult(
                "uncertain",
                attempt_id,
                f"player_{action}_timeout",
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                deadline_exhausted=time.monotonic() >= deadline,
            )
        with self._power_lock:
            state = self._power_state
            error = self._power_helper_error
            operation_reason = self._power_helper_reason
            shutdown = self._power_shutdown or self._closed.is_set()
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

    async def _suspend_for_sleep_owned(self) -> bool:
        """Actor-owned power transition; distinct from speech interruption."""
        self._power_suspended = True
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

    async def _stop_foreground_output_owned(self, response_id: str) -> str:
        """Actor-owned stop of one named response's audible output."""
        active = self._active
        if (
            active is None
            or active.response.response_id != response_id
            or active.terminal_commit_pending
        ):
            record_realtime_trace(
                "media_stop_foreground_stale",
                response_id=response_id,
                active_response_id=None if active is None else active.response.response_id,
                terminal_commit_pending=active is not None and active.terminal_commit_pending,
            )
            return "stale"
        if not await self._interrupt_active(reason="user_stop"):
            return "uncertain"
        # ADR-0006 D4: a user stop covers the queued speech of that group
        # too, and `_release_active(start_successor=False)` never advances
        # the lane, so an unpurged successor would sit unstartable.
        self._purge_after_drain()
        return "applied"

    async def _resume_after_wake_owned(
        self,
        *,
        attempt_id: int,
        admission_generation: int,
    ) -> bool:
        """Apply actor admission only for the exact current wake generation."""
        with self._power_lock:
            if (
                self._power_admission_generation != admission_generation
                or self._unadmitted_wake_attempt_id != attempt_id
                or self._power_state != "open_unadmitted"
                or self._power_abort_attempt_id == attempt_id
                or self._shutdown_requested.is_set()
                or self._power_shutdown
                or self._lane_isolated
            ):
                return False
            self._power_suspended = False
            self._accepting.set()
            return True

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
                _MediaCommand(
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
            self._conn = self._conn_factory()
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
                        timeout=self._remaining_s(self._config.shutdown_timeout_s),
                    ),
                )
                if done:
                    loop.run_until_complete(asyncio.gather(*done, return_exceptions=True))
                if still_pending:
                    LOGGER.error(
                        "media owner tasks ignored bounded cancellation: %s",
                        ", ".join(task.get_name() for task in still_pending),
                    )
            self._stop_provider_bounded()
            self._stop_player_bounded()
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
            loop.close()
            self._set_startup_phase("closed")
            self._closed.set()

    @staticmethod
    def _raise_player_start_debt(started: PlayerStartResult) -> None:
        """Convert a typed player debt into the actor startup exception path."""
        msg = f"player startup ownership not proven: {started.reason}"
        raise RuntimeError(msg)

    def _record_shutdown_player_cleanup(self, *, definitive: bool) -> None:
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
                        result = self._stop_player_with_bound(
                            self._config.shutdown_timeout_s,
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

    def _stop_player_bounded(self) -> bool:
        """Move potentially stuck device teardown to one controlled daemon helper."""
        with self._power_lock:
            power_state = self._power_state
            power_done = self._power_helper_done
        if power_state in {"stopping", "starting"}:
            if not power_done.wait(
                timeout=self._remaining_s(self._config.shutdown_timeout_s),
            ):
                LOGGER.error(
                    "power player transition still owns device at media shutdown",
                )
                return False
            with self._power_lock:
                power_state = self._power_state
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
                    self._stop_player_until_definitive(
                        self._remaining_s(self._config.shutdown_timeout_s),
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
            timeout=self._remaining_s(self._config.shutdown_timeout_s),
        )
        if not stopped:
            LOGGER.error(
                "player.stop exceeded media shutdown deadline; isolated daemon helper remains",
            )
        return stopped and self._player_stop_definitive

    def _stop_player_with_bound(self, timeout_s: float) -> object:
        """Call the typed player API while retaining legacy test-port support."""
        try:
            return self._player.stop(timeout_s=max(0.0, timeout_s))
        except TypeError as exc:
            if "timeout_s" not in str(exc):
                raise
            return self._player.stop()

    def _stop_player_until_definitive(self, first_timeout_s: float) -> object:
        """Join/retry one typed player debt on the current helper thread."""
        timeout_s = max(0.0, first_timeout_s)
        retries = 0
        while True:
            try:
                result = self._stop_player_with_bound(timeout_s)
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
            timeout_s = self._config.shutdown_timeout_s

    def _stop_provider_bounded(self) -> bool:
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
            timeout=self._remaining_s(self._config.shutdown_timeout_s),
        )
        if not stopped:
            LOGGER.error(
                "provider stop exceeded media shutdown deadline; isolated daemon helper remains",
            )
        return stopped

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
        while not self._shutdown_requested.is_set():
            try:
                command = await asyncio.wait_for(queue_.get(), timeout=0.02)
            except TimeoutError:
                continue
            try:
                if not isinstance(command, _MediaCommand):
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
            if self._shutdown_requested.is_set():
                await asyncio.sleep(0)
        await self._shutdown_owned()

    async def _handle_command(
        self,
        command: _MediaCommand,
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
        conn = self._require_conn()
        while self._event_cursor < command.row_id:
            rows = conn.execute(
                _SELECT_EVENT_ROWS_THROUGH,
                (
                    self._event_cursor,
                    command.row_id,
                    self._config.event_drain_batch,
                ),
            ).fetchall()
            if not rows:
                break
            for raw_row in rows:
                row_id, event = _hydrate_event_row(tuple(raw_row))
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
        if target_outcome is not None:
            return target_outcome
        return MediaSubmitOutcome(
            "invalid",
            command.event.event_uid,
            response_id=_response_id(command.event),
            detail="hint row was absent or not a response event",
        )

    async def _handle_event(  # noqa: PLR0911 - closed three-event dispatch
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
        if self._power_suspended:
            self._registry.terminalize(response_id)
            self._responses.pop(response_id, None)
            return MediaSubmitOutcome(
                "closed",
                event.event_uid,
                response_id,
                "system power transition suspended output",
            )
        if event.type == "surface.response_open":
            if payload.get("attention_channel") in _TTS_SILENT_CHANNELS:
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
            self._responses[response_id] = _ResponseBuffer(
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
            response.chunks[sequence] = _ResponseChunk(
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

    async def _response_terminal(self, response: _ResponseBuffer, event: Event) -> None:
        """L3 ended the run: stop an active playback, or drop a buffered one."""
        reason = event.type.replace(".", "_")
        active = self._active
        if active is not None and active.response is response:
            await self._interrupt_active(reason=reason)
            while self._after_drain:
                queued = self._after_drain.popleft()
                self._registry.terminalize(queued.response_id)
                self._responses.pop(queued.response_id, None)
            self._output_active.clear()
            return
        self._unschedule(response)
        self._responses.pop(response.response_id, None)
        self._registry.terminalize(response.response_id)

    async def _response_emitted(self, response: _ResponseBuffer, event: Event) -> None:
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

    async def _chunk_buffered(self, response: _ResponseBuffer, text: str) -> None:
        """Wake the segment loop; a tag ends incremental speaking for this response."""
        response.updated.set()
        if response.stream and _CHANNEL_TAG_RE.search(text) is not None:
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

    def _unschedule(self, response: _ResponseBuffer) -> None:
        response.scheduled = False
        if response in self._after_drain:
            self._after_drain.remove(response)

    async def _schedule_response(  # noqa: C901, PLR0911 - explicit lane disposition table
        self,
        response: _ResponseBuffer,
    ) -> None:
        speech = response.speech_text()
        if not speech:
            self._responses.pop(response.response_id, None)
            self._registry.terminalize(response.response_id)
            self._broadcast_spoken(response.turn_id)
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
            self._after_drain.append(response)
            record_realtime_trace(
                "media_after_drain_enqueued",
                response_id=response.response_id,
                response_group_id=response.response_group_id,
                phase=response.phase,
                lane_depth=len(self._after_drain),
            )
            return
        if not await self._interrupt_active(reason="foreground_superseded"):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        self._purge_after_drain()
        self._start_response(response)

    def _start_response(self, response: _ResponseBuffer) -> None:
        if (
            self._power_suspended
            or self._lane_isolated
            or self._shutdown_requested.is_set()
        ):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        result = self._player.activate_generation(
            session_id=self._registry.boot_id,
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            turn_id=response.turn_id,
        )
        if isinstance(result, ForegroundBusy):
            msg = "media owner/player foreground state diverged"
            raise RuntimeError(msg)  # noqa: TRY004 - runtime state, not caller type
        active = _ActiveResponse(
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

    async def _play_response(  # noqa: C901, PLR0912, PLR0915 - bounded lifecycle cleanup FSM
        self,
        active: _ActiveResponse,
    ) -> None:
        response = active.response
        live = response.incremental and not response.emitted
        segments = response.speech_segments()
        committed = segments[0][1] if live and segments else response.speech_text()
        speech_hash = hashlib.sha256(committed.encode()).hexdigest()
        if self._ducker is not None:
            active.output_lease = self._ducker.enter_output(timeout_s=0.0)
            if not active.output_lease:
                await self._fail_active(
                    active,
                    reason="system_output_lease_refused",
                    retryable=True,
                )
                if active.advance_after_cleanup:
                    active.advance_after_cleanup = False
                    if self._active is active:
                        self._active = None
                    self._advance_after_drain()
                return
        try:
            activation = emit_event(
                self._require_conn(),
                type="surface.playback_started",
                payload={
                    "session_id": active.lease.session_id,
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "playback_generation_id": active.lease.playback_generation_id,
                    "phase": response.phase,
                    "channel": response.channel,
                    "speech_text_hash": speech_hash,
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
                await self._wait_task_bounded(close_task, timeout_s=0.5)
            # Every exit path retains and drains fallback process ownership;
            # an exceptional ``wait()`` must not let a still-live ``say``
            # overlap the next generation.
            await self._cancel_fallback(active)
            if active.output_lease and self._ducker is not None:
                active.output_lease = False
                with contextlib.suppress(Exception):
                    self._ducker.leave_output()
            if active.advance_after_cleanup:
                active.advance_after_cleanup = False
                if self._active is active:
                    self._active = None
                self._advance_after_drain()

    async def _presentation_owner(self, active: _ActiveResponse) -> None:
        """Persist whole-segment heard checkpoints while provider I/O continues."""
        try:
            while self._active is active and not self._lane_isolated:
                snapshot = self._player.poll_generation(
                    active.lease.playback_generation_id,
                )
                if isinstance(snapshot, StalePlaybackGeneration):
                    return
                await self._checkpoint_if_advanced(active, snapshot)
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
        active: _ActiveResponse,
        segments: list[tuple[int, str, str]],
        *,
        budget: asyncio.Timeout,
        live: bool,
    ) -> bool:
        """Walk ``segments``; while ``live`` the list grows until the buffer is emitted."""
        lease = active.lease
        accepted_total = 0
        last_error: BaseException | None = None
        endpoint_index = 0
        session: TTSSession | None = None
        iterator: AsyncIterator[TTSAudioChunk | TTSSegmentFinished] | None = None
        provider_first = False
        segment_index = 0
        while True:
            if segment_index >= len(segments) and not (
                live and await self._await_segments(active, segments)
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
            emit_event(
                self._require_conn(),
                type="surface.playback_segment_prepared",
                payload={
                    "session_id": lease.session_id,
                    "response_id": active.response.response_id,
                    "turn_id": active.response.turn_id,
                    "playback_generation_id": lease.playback_generation_id,
                    "sequence": sequence,
                    "speech_text": text,
                    "speech_text_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "segment_hash": segment_hash,
                    "source_chunk_event_uid": active.response.chunks[sequence].source_event_uid,
                },
                source_event_id=active.activation_event_uid,
                correlation={"turn_id": active.response.turn_id},
            )
            active.prepared_text += text
            opened = self._player.begin_generation_segment(
                expected_playback_generation_id=lease.playback_generation_id,
                sequence=sequence,
                text=text,
                segment_hash=segment_hash,
            )
            if isinstance(opened, StalePlaybackGeneration):
                return False
            accepted_segment = 0
            while endpoint_index < self._provider.streaming_candidate_count:
                if self._active is not active:
                    return False
                try:
                    if session is None:
                        session = self._provider.create_tts_session(
                            endpoint_index=endpoint_index,
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
                    resampler: _SegmentResampler | None = None
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
                            if resampler is None:
                                resampler = _SegmentResampler(
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
                            written = await self._write_all(
                                active,
                                canonical,
                                sequence=sequence,
                            )
                            accepted_segment += written
                            accepted_total += written
                            continue
                        if resampler is not None:
                            written = await self._write_all(
                                active,
                                resampler.finish(),
                                sequence=sequence,
                            )
                            accepted_segment += written
                            accepted_total += written
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
                        break
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider transport boundary
                    last_error = exc
                    if session is not None:
                        close_task = asyncio.create_task(session.close())
                        await self._wait_task_bounded(close_task, timeout_s=0.5)
                    session = None
                    iterator = None
                    active.session = None
                    if accepted_segment > 0:
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
                if self._config.enable_macos_say_fallback:
                    while live and await self._await_segments(active, segments):
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
                    return await self._run_macos_say(active, remaining)
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

    async def _await_segments(
        self,
        active: _ActiveResponse,
        segments: list[tuple[int, str, str]],
    ) -> bool:
        """Extend ``segments`` with newly durable ones; False once the buffer is complete."""
        response = active.response
        last = segments[-1][0] if segments else -1
        while self._active is active:
            if response.tagged:
                return True
            response.updated.clear()
            fresh = [item for item in response.speech_segments() if item[0] > last]
            if fresh:
                segments.extend(fresh)
                return True
            if response.emitted:
                return False
            await response.updated.wait()
        return False

    async def _write_all(
        self,
        active: _ActiveResponse,
        pcm: bytes,
        *,
        sequence: int,
    ) -> int:
        offset_samples = 0
        samples = memoryview(pcm)
        total_samples = len(samples) // 4
        while offset_samples < total_samples:
            if self._active is not active:
                return offset_samples
            result = self._player.write_generation(
                samples[offset_samples * 4 :].tobytes(),
                expected_playback_generation_id=(active.lease.playback_generation_id),
                segment_sequence=sequence,
            )
            if isinstance(result, StalePlaybackGeneration):
                return offset_samples
            if isinstance(result, AcceptedSamples):
                offset_samples += result.sample_count
                continue
            self._player.poll_presentation()
            await asyncio.sleep(self._config.ring_retry_s)
        return offset_samples

    async def _run_macos_say(
        self,
        active: _ActiveResponse,
        segments: list[tuple[int, str, str]],
    ) -> bool:
        """Compatibility fallback with exact spawn/process cancellation ownership."""
        speech = "".join(text for _sequence, text, _hash in segments)
        active.provider_label = "macos_say"
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "say",
                "-v",
                "Tingting",
                speech,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            name=f"media-say-spawn-{active.response.response_id}",
        )
        active.fallback_spawn_task = spawn_task
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            raise
        except OSError:
            active.fallback_spawn_task = None
            return False
        active.fallback_spawn_task = None
        active.fallback_process = process
        record_realtime_trace(
            "tts_macos_say_started",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            cursor_quality="unknown",
        )
        return_code = await process.wait()
        if active.fallback_process is process:
            active.fallback_process = None
        if return_code != 0 or self._active is not active:
            return False
        for index, (sequence, text, segment_hash) in enumerate(segments):
            if index > 0:
                opened = self._player.begin_generation_segment(
                    expected_playback_generation_id=active.lease.playback_generation_id,
                    sequence=sequence,
                    text=text,
                    segment_hash=segment_hash,
                )
                if isinstance(opened, StalePlaybackGeneration):
                    return False
            finished = self._player.finish_generation_segment(
                expected_playback_generation_id=active.lease.playback_generation_id,
                sequence=sequence,
            )
            if isinstance(finished, StalePlaybackGeneration):
                return False
        return True

    async def _cancel_fallback(
        self,
        active: _ActiveResponse,
    ) -> None:
        existing_janitor = active.fallback_janitor_task
        if existing_janitor is not None:
            quiescent = await asyncio.shield(existing_janitor)
            if active.fallback_janitor_task is existing_janitor:
                active.fallback_janitor_task = None
            if not quiescent:
                self._isolate_fallback_cleanup(active)
            return
        spawn_task = active.fallback_spawn_task
        owned_process = active.fallback_process
        if spawn_task is None and owned_process is None:
            return
        # Claim every handle and publish the janitor before the first await.
        # The response task and foreground interrupt can enter cleanup
        # concurrently, but the second caller will now join the same debt.
        active.fallback_spawn_task = None
        active.fallback_process = None
        janitor = asyncio.create_task(
            self._cleanup_fallback_handles(spawn_task, owned_process),
            name=f"media-say-janitor-{active.response.response_id}",
        )
        active.fallback_janitor_task = janitor
        self._fallback_janitors.add(janitor)
        janitor.add_done_callback(self._fallback_janitors.discard)
        quiescent = await asyncio.shield(janitor)
        if active.fallback_janitor_task is janitor:
            active.fallback_janitor_task = None
        if not quiescent:
            self._isolate_fallback_cleanup(active)

    async def _cleanup_fallback_handles(
        self,
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None,
        owned_process: asyncio.subprocess.Process | None,
    ) -> bool:
        """Own claimed spawn/process handles through terminate/kill/reap."""
        spawned_process: asyncio.subprocess.Process | None = None
        if spawn_task is not None:
            spawn_task.cancel()
            _done, pending = await asyncio.wait(
                {spawn_task},
                timeout=self._remaining_s(0.25),
            )
            if pending:
                try:
                    spawned_process = await spawn_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOGGER.exception("late macOS say spawn failed")
            elif not spawn_task.cancelled() and spawn_task.exception() is None:
                spawned_process = spawn_task.result()
        processes = [
            process
            for process in (spawned_process, owned_process)
            if process is not None
        ]
        for index, process in enumerate(processes):
            if index > 0 and process is processes[0]:
                continue
            if not await self._terminate_process(process):
                return False
        return True

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> bool:
        """Terminate, kill if needed, and never report quiescent before reap."""
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        wait_task = asyncio.create_task(process.wait())
        done, _pending = await asyncio.wait(
            {wait_task},
            timeout=self._remaining_s(0.25),
        )
        if done:
            try:
                wait_task.result()
            except asyncio.CancelledError:
                LOGGER.warning("macOS say wait was cancelled after terminate; forcing kill")
            except Exception as exc:  # noqa: BLE001 - cleanup must still force kill
                LOGGER.warning("macOS say wait failed after terminate; forcing kill: %r", exc)
            else:
                return True
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        reap_task = wait_task if not wait_task.done() else asyncio.create_task(process.wait())
        try:
            # Reap completion is presentation ownership debt. A pathological
            # child may make close() return degraded, but cannot overlap a
            # successor or pin process exit because the actor is daemon-backed.
            await asyncio.shield(reap_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("macOS say could not be reaped after kill")
            return False
        return True

    def _isolate_fallback_cleanup(self, active: _ActiveResponse) -> None:
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
        active: _ActiveResponse,
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
        active: _ActiveResponse,
        *,
        reason: str,
        retryable: bool,
    ) -> None:
        if self._active is not active:
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

    async def _cancel_active_io(self, active: _ActiveResponse, *, reason: str) -> None:
        task = active.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        session = active.session
        if session is not None:
            abort_task = asyncio.create_task(session.abort(reason))
            await self._wait_task_bounded(abort_task, timeout_s=0.5)
        await self._cancel_fallback(active)
        if task is not None and task is not asyncio.current_task():
            await self._wait_task_bounded(task, timeout_s=0.5)

    async def _wait_task_bounded(self, task: asyncio.Task[Any], *, timeout_s: float) -> bool:
        done, pending = await asyncio.wait(
            {task},
            timeout=self._remaining_s(timeout_s),
        )
        if pending:
            task.cancel()
            return False
        if done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        return True

    async def _checkpoint_if_advanced(
        self,
        active: _ActiveResponse,
        snapshot: OutputTimelineSnapshot,
    ) -> None:
        sequence = snapshot.heard_through_sequence
        if sequence is None or sequence == active.last_checkpoint_sequence:
            return
        for attempt in range(1, self._config.checkpoint_retry_attempts + 1):
            conn = self._require_conn()
            existing = conn.execute(
                "SELECT 1 FROM events WHERE type = 'surface.playback_checkpoint' "
                "AND json_extract(payload_json, '$.response_id') = ? "
                "AND json_extract(payload_json, '$.playback_generation_id') = ? "
                "AND json_extract(payload_json, '$.session_id') = ? "
                "AND json_extract(payload_json, '$.heard_through_sequence') = ? LIMIT 1",
                (
                    active.response.response_id,
                    active.lease.playback_generation_id,
                    active.lease.session_id,
                    sequence,
                ),
            ).fetchone()
            if existing is not None:
                active.last_checkpoint_sequence = sequence
                return
            try:
                emit_event(
                    conn,
                    type="surface.playback_checkpoint",
                    payload={
                        "session_id": active.lease.session_id,
                        "response_id": active.response.response_id,
                        "turn_id": active.response.turn_id,
                        "playback_generation_id": active.lease.playback_generation_id,
                        "heard_through_sequence": sequence,
                        "submitted_samples": snapshot.submitted_samples,
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
            active.last_checkpoint_sequence = sequence
            return

    async def _commit_terminal_durable(  # noqa: PLR0913 - mirrors terminal payload owner
        self,
        active: _ActiveResponse,
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
                self._commit_terminal(
                    active,
                    event_type=event_type,
                    snapshot=snapshot,
                    reason=reason,
                    speech_text_hash=speech_text_hash,
                    retryable=retryable,
                )
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
                    delay = self._remaining_s(self._config.durability_retry_s)
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
        active: _ActiveResponse,
    ) -> OutputTimelineSnapshot | None:
        """Tombstone first, then settle the callback publication linearization."""
        self._player.interrupt_generation(
            expected_playback_generation_id=active.lease.playback_generation_id,
        )
        deadline = asyncio.get_running_loop().time() + self._remaining_s(0.5)
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

    def _commit_terminal(  # noqa: PLR0913 - exact terminal shape is explicit
        self,
        active: _ActiveResponse,
        *,
        event_type: str,
        snapshot: OutputTimelineSnapshot,
        reason: str | None,
        speech_text_hash: str | None,
        retryable: bool | None,
    ) -> None:
        payload: dict[str, object] = {
            "session_id": active.lease.session_id,
            "response_id": active.response.response_id,
            "turn_id": active.response.turn_id,
            "playback_generation_id": active.lease.playback_generation_id,
            "heard_through_sequence": snapshot.heard_through_sequence,
            "submitted_samples": snapshot.submitted_samples,
            "total_samples": snapshot.accepted_samples,
            "provider": active.provider_label,
            "cursor_quality": snapshot.cursor_quality,
            "heard_text_hash": snapshot.heard_text_hash,
            "heard_text": snapshot.heard_text,
            "starvation_gaps": (self._player.starvation_gaps - active.starvation_gaps_at_start),
            "host_underflows": (self._player.underflow_count - active.host_underflows_at_start),
        }
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = speech_text_hash or hashlib.sha256(b"").hexdigest()
        else:
            payload["reason"] = reason or "unknown"
            if event_type == "surface.playback_failed" and retryable is not None:
                payload["retryable"] = retryable
        terminalize_playback(
            self._require_conn(),
            event_type=event_type,
            payload=payload,
            source_event_id=active.activation_event_uid or active.response.source_event_id,
            correlation={"turn_id": active.response.turn_id},
        )
        self._registry.terminalize(active.response.response_id)

    async def _release_active(
        self,
        active: _ActiveResponse,
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
            await self._wait_task_bounded(presentation, timeout_s=0.25)
        self._responses.pop(active.response.response_id, None)
        self._player.retire_generation(active.lease.playback_generation_id)
        self._broadcast_spoken(active.response.turn_id, event_type=event_type)
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
        if self._power_suspended or self._lane_isolated or self._active is not None:
            return
        if self._after_drain:
            self._start_response(self._after_drain.popleft())
        else:
            self._output_active.clear()

    def _broadcast_spoken(self, turn_id: str, *, event_type: str = "no_speech") -> None:
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback(
                    "spoken",
                    turn_id=turn_id,
                    output_outcome=event_type.removeprefix("surface.playback_"),
                )

    def _emit_lane_isolated(
        self,
        active: _ActiveResponse,
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
            emit_event(
                self._require_conn(),
                type="surface.playback_lane_isolated",
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
        active: _ActiveResponse,
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
        if self._active is not None:
            await self._interrupt_active(reason="media_owner_shutdown")
        while self._fallback_janitors:
            # Shutdown owns late-spawn debt too. Waiting here may make the
            # caller observe degraded bounded close, but the daemon actor
            # remains the sole janitor and will finish once the spawn returns.
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._fallback_janitors)),
                return_exceptions=True,
            )
        while self._after_drain:
            response = self._after_drain.popleft()
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
        self._stop_provider_bounded()
        self._stop_player_bounded()
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
                if isinstance(command, _MediaCommand) and not command.acknowledged.done():
                    command.acknowledged.set_result(
                        MediaSubmitOutcome("closed", command.event.event_uid),
                    )
            finally:
                queue_.task_done()

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            msg = "media owner Event Log connection is unavailable"
            raise RuntimeError(msg)
        return conn


def streaming_media_config_from_mapping(
    raw: Mapping[str, object] | None,
) -> StreamingMediaConfig:
    """Parse bounded rollout values; reject malformed explicit entries."""
    values = {} if raw is None else raw
    defaults = StreamingMediaConfig()

    def _positive_int(key: str, fallback: int) -> int:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.streaming_output.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    def _positive_float(key: str, fallback: float) -> float:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            msg = f"realtime.streaming_output.{key} must be a positive number"
            raise ValueError(msg)
        return float(value)

    def _boolean(key: str, fallback: bool) -> bool:  # noqa: FBT001 - config default
        value = values.get(key)
        if value is None:
            return fallback
        if not isinstance(value, bool):
            msg = f"realtime.streaming_output.{key} must be boolean"
            raise ValueError(msg)  # noqa: TRY004 - malformed config value, not a caller type
        return value

    return StreamingMediaConfig(
        canonical_sample_rate_hz=_positive_int(
            "canonical_sample_rate_hz",
            defaults.canonical_sample_rate_hz,
        ),
        command_queue_capacity=_positive_int(
            "command_queue_capacity",
            defaults.command_queue_capacity,
        ),
        response_lane_capacity=_positive_int(
            "response_lane_capacity",
            defaults.response_lane_capacity,
        ),
        response_text_bytes=_positive_int(
            "response_text_bytes",
            defaults.response_text_bytes,
        ),
        session_command_capacity=_positive_int(
            "session_command_capacity",
            defaults.session_command_capacity,
        ),
        session_audio_capacity=_positive_int(
            "session_audio_capacity",
            defaults.session_audio_capacity,
        ),
        session_idle_close_s=_positive_float(
            "session_idle_close_s",
            defaults.session_idle_close_s,
        ),
        response_timeout_s=_positive_float(
            "response_timeout_s",
            defaults.response_timeout_s,
        ),
        ring_retry_s=_positive_float("ring_retry_s", defaults.ring_retry_s),
        presentation_poll_s=_positive_float(
            "presentation_poll_s",
            defaults.presentation_poll_s,
        ),
        shutdown_timeout_s=_positive_float(
            "shutdown_timeout_s",
            defaults.shutdown_timeout_s,
        ),
        event_drain_batch=_positive_int(
            "event_drain_batch",
            defaults.event_drain_batch,
        ),
        terminal_retry_attempts=_positive_int(
            "terminal_retry_attempts",
            defaults.terminal_retry_attempts,
        ),
        checkpoint_retry_attempts=_positive_int(
            "checkpoint_retry_attempts",
            defaults.checkpoint_retry_attempts,
        ),
        durability_retry_s=_positive_float(
            "durability_retry_s",
            defaults.durability_retry_s,
        ),
        enable_macos_say_fallback=_boolean(
            "enable_macos_say_fallback",
            defaults.enable_macos_say_fallback,
        ),
        speak_from_segments=_boolean("speak_from_segments", defaults.speak_from_segments),
    )


__all__ = [
    "ActivePlaybackRegistry",
    "MediaPowerTransitionResult",
    "MediaSubmitOutcome",
    "StreamingMediaConfig",
    "StreamingMediaStartupError",
    "StreamingTTSPipeline",
    "StreamingTTSProvider",
    "streaming_media_config_from_mapping",
]
