"""Persistent L5 media owner for generation-safe streaming speech output.

The owner runs one asyncio loop for the daemon lifetime.  Surface watchers
submit committed response events into its bounded command queue; only this
actor may open TTS sessions, mutate playback generations, or own fallback
processes.  The PortAudio callback remains a lock-free consumer and reports
bounded software milestones back to the actor.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np

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
    TTSAudioChunk,
    TTSResponseSegment,
    TTSSegmentFinished,
    TTSSession,
    _extract_voice_content,
    _preprocess_for_speech,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.shared import Event
    from jarvis.surface.voice_ducking import SystemAudioDucker

LOGGER = logging.getLogger(__name__)

_MAX_MEDIA_COMMANDS = 1024
_MAX_AFTER_DRAIN_RESPONSES = 64
_MAX_RESPONSE_TEXT_BYTES = 1_048_576
_MAX_SESSION_COMMANDS = 32
_MAX_SESSION_AUDIO_EVENTS = 256
_MAX_RESPONSE_TIMEOUT_S = 300.0
_MAX_SHUTDOWN_TIMEOUT_S = 10.0

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
    enable_macos_say_fallback: bool = True

    def __post_init__(self) -> None:
        """Reject unbounded or non-positive actor budgets."""
        positive_ints = (
            self.canonical_sample_rate_hz,
            self.command_queue_capacity,
            self.response_lane_capacity,
            self.response_text_bytes,
            self.session_command_capacity,
            self.session_audio_capacity,
        )
        positive_floats = (
            self.session_idle_close_s,
            self.response_timeout_s,
            self.ring_retry_s,
            self.presentation_poll_s,
            self.shutdown_timeout_s,
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
        ):
            msg = "streaming media bounds exceed validated production maxima"
            raise ValueError(msg)


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
    chunks: dict[int, str] = field(default_factory=dict)
    emitted: bool = False

    def speech_segments(self) -> list[tuple[int, str, str]]:
        """Return ordered semantic segments with stable text hashes."""
        ordered = [(key, self.chunks[key]) for key in sorted(self.chunks)]
        raw = "".join(text for _sequence, text in ordered)
        if "<voice>" in raw or "<document>" in raw:
            text = _preprocess_for_speech(_extract_voice_content(raw))
            return [(0, text, hashlib.sha256(text.encode()).hexdigest())] if text else []
        segments: list[tuple[int, str, str]] = []
        for sequence, raw_segment in ordered:
            text = _preprocess_for_speech(raw_segment)
            if text:
                segments.append(
                    (sequence, text, hashlib.sha256(text.encode()).hexdigest()),
                )
        return segments

    def speech_text(self) -> str:
        return "".join(text for _sequence, text, _hash in self.speech_segments())


@dataclass
class _ActiveResponse:
    response: _ResponseBuffer
    lease: GenerationLease
    task: asyncio.Task[None] | None = None
    session: TTSSession | None = None
    fallback_process: asyncio.subprocess.Process | None = None
    output_lease: bool = False
    last_checkpoint_sequence: int | None = None


@dataclass(frozen=True)
class _MediaCommand:
    row_id: int
    event: Event
    origin: str
    acknowledged: asyncio.Future[MediaSubmitOutcome]


_SHUTDOWN = object()


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
        self._delivery_high_water_id = boot_high_water_id
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
        if row_id <= self._delivery_high_water_id:
            return MediaSubmitOutcome(
                status="historical",
                event_uid=event.event_uid,
                response_id=response_id,
                detail="out-of-order or replayed current-boot Event Log row",
            )
        self._delivery_high_water_id = row_id
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
        start_player: bool = True,
    ) -> None:
        """Start one non-daemon thread and its daemon-lifetime asyncio loop."""
        self._provider = provider
        self._player = player
        self._conn_factory = conn_factory
        self._config = config or StreamingMediaConfig()
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._start_player = start_player
        self._registry = ActivePlaybackRegistry(
            boot_high_water_id=boot_high_water_id,
        )
        self._accepting = threading.Event()
        self._accepting.set()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._output_active = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_MediaCommand | object] | None = None
        self._conn: sqlite3.Connection | None = None
        self._startup_error: BaseException | None = None
        self._shutdown_future: concurrent.futures.Future[None] | None = None
        self._responses: dict[str, _ResponseBuffer] = {}
        self._after_drain: deque[_ResponseBuffer] = deque()
        self._active: _ActiveResponse | None = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="jarvis-media-owner",
            daemon=False,
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._config.shutdown_timeout_s):
            msg = "persistent media owner did not start within its bound"
            raise RuntimeError(msg)
        self._raise_startup_error()

    def _raise_startup_error(self) -> None:
        """Surface an actor-thread failure after its readiness publication."""
        error = self._startup_error
        if error is not None:
            msg = "persistent media owner failed to start"
            raise RuntimeError(msg) from error

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

    def request_close(self) -> None:
        """Stop admission immediately, then enqueue one bounded owner shutdown."""
        self._accepting.clear()
        if self._shutdown_future is not None:
            return
        loop = self._loop
        if loop is None or self._closed.is_set():
            return
        shutdown_coro = self._enqueue_shutdown()
        try:
            self._shutdown_future = asyncio.run_coroutine_threadsafe(
                shutdown_coro,
                loop,
            )
        except RuntimeError:
            shutdown_coro.close()
            return

    def close(self, *, wait_timeout_s: float | None = None) -> bool:
        """Bound shutdown and join the one non-daemon owner thread."""
        self.request_close()
        timeout = wait_timeout_s or self._config.shutdown_timeout_s
        shutdown = self._shutdown_future
        if shutdown is not None:
            with contextlib.suppress(concurrent.futures.TimeoutError, Exception):
                shutdown.result(timeout=timeout)
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive() and self._closed.is_set()

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
        await queue_.put(
            _MediaCommand(
                row_id=row_id,
                event=event,
                origin=origin,
                acknowledged=acknowledged,
            ),
        )
        return await acknowledged

    async def _enqueue_shutdown(self) -> None:
        queue_ = self._queue
        if queue_ is not None:
            await queue_.put(_SHUTDOWN)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._conn = self._conn_factory()
            self._queue = asyncio.Queue(
                maxsize=self._config.command_queue_capacity + 1,
            )
            self._ready.set()
            loop.run_until_complete(self._run_owned())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            LOGGER.exception("persistent media owner crashed")
        finally:
            pending = tuple(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                done, still_pending = loop.run_until_complete(
                    asyncio.wait(
                        pending,
                        timeout=self._config.shutdown_timeout_s,
                    ),
                )
                if done:
                    loop.run_until_complete(asyncio.gather(*done, return_exceptions=True))
                if still_pending:
                    LOGGER.error(
                        "media owner tasks ignored bounded cancellation: %s",
                        ", ".join(task.get_name() for task in still_pending),
                    )
            with contextlib.suppress(Exception):
                self._player.stop()
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None
            loop.close()
            self._closed.set()

    async def _run_owned(self) -> None:
        if self._start_player:
            self._player.start()
        record_realtime_trace(
            "media_owner_started",
            boot_id=self._registry.boot_id,
            boot_high_water_id=self._registry.boot_high_water_id,
            command_queue_capacity=self._config.command_queue_capacity,
        )
        queue_ = self._queue
        if queue_ is None:  # pragma: no cover - constructor invariant
            return
        while True:
            command = await queue_.get()
            try:
                if command is _SHUTDOWN:
                    await self._shutdown_owned()
                    return
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

    async def _handle_command(  # noqa: PLR0911 - closed three-event dispatch
        self,
        command: _MediaCommand,
    ) -> MediaSubmitOutcome:
        outcome = self._registry.classify(row_id=command.row_id, event=command.event)
        if outcome.status != "accepted":
            return outcome
        event = command.event
        payload = event.payload
        response_id = str(payload["response_id"])
        if event.type == "surface.response_open":
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
                row_id=command.row_id,
                source_event_id=event.event_uid,
                response_id=response_id,
                response_group_id=response_group_raw,
                turn_id=str(payload.get("turn_id", "")),
                phase=str(payload.get("phase", "final")),
                channel=str(payload.get("channel", "both")),
                gate_mode=str(payload.get("required_gate_mode", "sentence")),
            )
            return outcome
        response = self._responses.get(response_id)
        if response is None:
            return MediaSubmitOutcome("unregistered", event.event_uid, response_id)
        if event.type == "surface.response_chunk":
            sequence = int(payload["sequence"])
            text = str(payload.get("text", ""))
            prospective = sum(len(item.encode()) for item in response.chunks.values())
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
            response.chunks[sequence] = text
            return outcome
        if event.type == "surface.response_emitted":
            response.emitted = True
            response.source_event_id = event.event_uid
            await self._schedule_response(response)
        return outcome

    async def _schedule_response(self, response: _ResponseBuffer) -> None:
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
        await self._interrupt_active(reason="foreground_superseded")
        while self._after_drain:
            old = self._after_drain.popleft()
            self._registry.terminalize(old.response_id)
            self._responses.pop(old.response_id, None)
        self._start_response(response)

    def _start_response(self, response: _ResponseBuffer) -> None:
        result = self._player.activate_generation(
            session_id=self._registry.boot_id,
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            turn_id=response.turn_id,
        )
        if isinstance(result, ForegroundBusy):
            msg = "media owner/player foreground state diverged"
            raise RuntimeError(msg)  # noqa: TRY004 - runtime state, not caller type
        active = _ActiveResponse(response=response, lease=result)
        self._active = active
        self._output_active.set()
        active.task = asyncio.create_task(
            self._play_response(active),
            name=f"media-response-{response.response_id}",
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

    async def _play_response(
        self,
        active: _ActiveResponse,
    ) -> None:
        response = active.response
        segments = response.speech_segments()
        speech_hash = hashlib.sha256(response.speech_text().encode()).hexdigest()
        if self._ducker is not None:
            active.output_lease = self._ducker.enter_output(timeout_s=0.0)
            if not active.output_lease:
                await self._fail_active(
                    active,
                    reason="system_output_lease_refused",
                    retryable=True,
                )
                return
        try:
            async with asyncio.timeout(self._config.response_timeout_s):
                completed = await self._stream_with_prefix_fallback(active, segments)
                if self._active is not active:
                    return
                if not completed:
                    await self._fail_active(
                        active,
                        reason="tts_fallback_unavailable",
                        retryable=True,
                    )
                    return
                await self._drain_and_complete(active, segment_hash=speech_hash)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
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
                with contextlib.suppress(Exception):
                    await session.close()
            if active.output_lease and self._ducker is not None:
                active.output_lease = False
                with contextlib.suppress(Exception):
                    self._ducker.leave_output()

    async def _stream_with_prefix_fallback(  # noqa: C901, PLR0911, PLR0912, PLR0915 - bounded endpoint FSM
        self,
        active: _ActiveResponse,
        segments: list[tuple[int, str, str]],
    ) -> bool:
        lease = active.lease
        accepted_total = 0
        last_error: BaseException | None = None
        endpoint_index = 0
        session: TTSSession | None = None
        iterator: AsyncIterator[TTSAudioChunk | TTSSegmentFinished] | None = None
        provider_first = False
        for segment_index, (sequence, text, segment_hash) in enumerate(segments):
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
                        with contextlib.suppress(Exception):
                            await session.close()
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
                    remaining = segments[segment_index:]
                    return await self._run_macos_say(active, remaining)
                if last_error is not None:
                    raise last_error
                return False
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
        """Compatibility fallback owned by the media loop, never an executor."""
        speech = "".join(text for _sequence, text, _hash in segments)
        try:
            process = await asyncio.create_subprocess_exec(
                "say",
                "-v",
                "Tingting",
                speech,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        active.fallback_process = process
        record_realtime_trace(
            "tts_macos_say_started",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            cursor_quality="unknown",
        )
        try:
            return_code = await process.wait()
        finally:
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
            self._checkpoint_if_advanced(active, snapshot)
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
                self._try_commit_terminal(
                    active,
                    event_type="surface.playback_completed",
                    snapshot=completed,
                    reason=None,
                    speech_text_hash=segment_hash,
                    retryable=None,
                )
                self._finish_active(active)
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
        snapshot = self._interrupt_snapshot(active)
        if snapshot is not None:
            self._try_commit_terminal(
                active,
                event_type="surface.playback_failed",
                snapshot=snapshot,
                reason=reason,
                speech_text_hash=None,
                retryable=retryable,
            )
        else:
            self._registry.terminalize(active.response.response_id)
        self._finish_active(active)

    async def _interrupt_active(self, *, reason: str) -> None:
        active = self._active
        if active is None:
            return
        # Generation tombstone is the first irreversible publication.  Only
        # after it lands do we cancel network/fallback work.
        snapshot = self._interrupt_snapshot(active)
        self._active = None
        if snapshot is not None:
            self._try_commit_terminal(
                active,
                event_type="surface.playback_interrupted",
                snapshot=snapshot,
                reason=reason,
                speech_text_hash=None,
                retryable=None,
            )
        task = active.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        session = active.session
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.abort(reason), timeout=1.0)
        process = active.fallback_process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.5)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1.0)
        self._responses.pop(active.response.response_id, None)
        self._player.retire_generation(active.lease.playback_generation_id)
        if not self._after_drain:
            self._output_active.clear()

    def _checkpoint_if_advanced(
        self,
        active: _ActiveResponse,
        snapshot: OutputTimelineSnapshot,
    ) -> None:
        sequence = snapshot.heard_through_sequence
        if sequence is None or sequence == active.last_checkpoint_sequence:
            return
        conn = self._require_conn()
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
                    "cursor_quality": snapshot.cursor_quality,
                },
                source_event_id=active.response.source_event_id,
                correlation={"turn_id": active.response.turn_id},
            )
        except Exception:
            LOGGER.exception(
                "playback checkpoint append failed: response_id=%s generation=%d",
                active.response.response_id,
                active.lease.playback_generation_id,
            )
        active.last_checkpoint_sequence = sequence

    def _try_commit_terminal(  # noqa: PLR0913 - mirrors terminal payload owner
        self,
        active: _ActiveResponse,
        *,
        event_type: str,
        snapshot: OutputTimelineSnapshot,
        reason: str | None,
        speech_text_hash: str | None,
        retryable: bool | None,
    ) -> bool:
        """Commit via the Wave 1 CAS without leaking media ownership on I/O failure."""
        try:
            self._commit_terminal(
                active,
                event_type=event_type,
                snapshot=snapshot,
                reason=reason,
                speech_text_hash=speech_text_hash,
                retryable=retryable,
            )
        except Exception:
            LOGGER.exception(
                "playback terminal append failed: type=%s response_id=%s generation=%d",
                event_type,
                active.response.response_id,
                active.lease.playback_generation_id,
            )
            self._registry.terminalize(active.response.response_id)
            return False
        return True

    def _interrupt_snapshot(
        self,
        active: _ActiveResponse,
    ) -> OutputTimelineSnapshot | None:
        """Tombstone first, then recover conservative device-close state if needed."""
        result = self._player.interrupt_generation(
            expected_playback_generation_id=active.lease.playback_generation_id,
        )
        if not isinstance(result, StalePlaybackGeneration):
            return result
        recovered = self._player.poll_generation(active.lease.playback_generation_id)
        return None if isinstance(recovered, StalePlaybackGeneration) else recovered

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
        snapshot = self._interrupt_snapshot(active)
        if snapshot is not None:
            self._try_commit_terminal(
                active,
                event_type="surface.playback_failed",
                snapshot=snapshot,
                reason=f"media_owner_task_error:{type(error).__name__}",
                speech_text_hash=None,
                retryable=False,
            )
        self._registry.terminalize(active.response.response_id)
        self._finish_active(active)

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
            "heard_through_sequence": (
                snapshot.heard_through_sequence
                if snapshot.heard_through_sequence is not None
                else -1
            ),
            "submitted_samples": snapshot.submitted_samples,
            "total_samples": snapshot.accepted_samples,
            "provider": "minimax_ws_streaming",
            "cursor_quality": snapshot.cursor_quality,
        }
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = speech_text_hash or hashlib.sha256(b"").hexdigest()
        else:
            payload["heard_text_hash"] = snapshot.heard_text_hash
            payload["reason"] = reason or "unknown"
            if snapshot.heard_text:
                payload["heard_text"] = snapshot.heard_text
            if event_type == "surface.playback_failed" and retryable is not None:
                payload["retryable"] = retryable
        terminalize_playback(
            self._require_conn(),
            event_type=event_type,
            payload=payload,
            source_event_id=active.response.source_event_id,
            correlation={"turn_id": active.response.turn_id},
        )
        self._registry.terminalize(active.response.response_id)

    def _finish_active(self, active: _ActiveResponse) -> None:
        if self._active is not active:
            return
        self._active = None
        self._responses.pop(active.response.response_id, None)
        self._player.retire_generation(active.lease.playback_generation_id)
        self._broadcast_spoken(active.response.turn_id)
        if self._after_drain:
            next_response = self._after_drain.popleft()
            self._start_response(next_response)
        else:
            self._output_active.clear()

    def _broadcast_spoken(self, turn_id: str) -> None:
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback("spoken", turn_id=turn_id)

    async def _shutdown_owned(self) -> None:
        self._accepting.clear()
        if self._active is not None:
            await self._interrupt_active(reason="media_owner_shutdown")
        while self._after_drain:
            response = self._after_drain.popleft()
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
        request_close = getattr(self._provider, "request_close", None)
        if callable(request_close):
            with contextlib.suppress(Exception):
                request_close()
        self._player.stop()
        self._output_active.clear()
        record_realtime_trace(
            "media_owner_stopped",
            boot_id=self._registry.boot_id,
            owned_tasks_remaining=0,
        )

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

    macos_fallback = values.get("enable_macos_say_fallback")
    if macos_fallback is None:
        macos_fallback = defaults.enable_macos_say_fallback
    elif not isinstance(macos_fallback, bool):
        msg = "realtime.streaming_output.enable_macos_say_fallback must be boolean"
        raise ValueError(msg)

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
        enable_macos_say_fallback=macos_fallback,
    )


__all__ = [
    "ActivePlaybackRegistry",
    "MediaSubmitOutcome",
    "StreamingMediaConfig",
    "StreamingTTSPipeline",
    "StreamingTTSProvider",
    "streaming_media_config_from_mapping",
]
