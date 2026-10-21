"""Fakes for the streaming media lane: TTS provider and session, websocket, callback pump."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self

import numpy as np
import pytest

from jarvis.state.event_log import emit_event
from jarvis.surface import voice_media, voice_tts

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator, Callable

    from jarvis.shared import Event


@dataclass(frozen=True)
class Behavior:
    outcome: Literal["success", "fail_before", "fail_after"] = "success"
    final_delay_s: float = 0.01
    samples: int = 160
    sample_rate_hz: int = 8_000
    late_after_abort: bool = False
    amplitude: int = 2_000
    fail_at_sequence: int | None = None  # fails before that segment's audio


class FakeSession:
    def __init__(
        self,
        provider: FakeProvider,
        *,
        endpoint_index: int,
    ) -> None:
        self._provider = provider
        self._endpoint_index = endpoint_index
        self._behavior = Behavior()
        self._response_id = ""
        self._generation = -1
        self._segments: asyncio.Queue[voice_tts.TTSResponseSegment] = asyncio.Queue()
        self._reader_claimed = False
        self._closed = False
        self._send_active = False

    async def connect(self) -> None:
        self._provider.actions.append("connect")
        if self._provider.connect_error is not None:
            raise self._provider.connect_error

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        self._response_id = response_id
        self._generation = playback_generation_id
        self._behavior = self._provider.behaviors.get(
            (response_id, self._endpoint_index),
            Behavior(),
        )
        self._provider.opened.append((response_id, self._endpoint_index))
        self._provider.actions.append(f"open:{response_id}")

    async def send(self, segment: voice_tts.TTSResponseSegment) -> None:
        if self._send_active:
            raise voice_tts.TTSConcurrentSendError
        self._send_active = True
        try:
            await asyncio.sleep(0)
            await self._segments.put(segment)
            self._provider.sent.append((self._response_id, segment.sequence))
        finally:
            self._send_active = False

    def audio_events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:
        if self._reader_claimed:
            msg = "fake session has one audio reader"
            raise RuntimeError(msg)
        self._reader_claimed = True
        self._provider.reader_claims[self._response_id] = (
            self._provider.reader_claims.get(self._response_id, 0) + 1
        )
        return self._events()

    async def _events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:  # noqa: C901 - one branch per fake failure mode
        while not self._closed:
            if self._behavior.outcome == "fail_before":
                msg = "fake connect/read failure before prefix"
                raise OSError(msg)
            segment = await self._segments.get()
            if segment.sequence == self._behavior.fail_at_sequence:
                msg = "fake failure before this segment's audio"
                raise OSError(msg)
            gate = self._provider.segment_gates.get((self._response_id, segment.sequence))
            while gate is not None and not gate.is_set() and not self._closed:  # noqa: ASYNC110
                await asyncio.sleep(0.001)
            pcm = np.full(
                self._behavior.samples,
                self._behavior.amplitude,
                dtype="<i2",
            ).tobytes()
            yield voice_tts.TTSAudioChunk(
                sequence=segment.sequence,
                pcm=pcm,
                sample_rate_hz=self._behavior.sample_rate_hz,
            )
            if self._behavior.outcome == "fail_after":
                await asyncio.sleep(self._behavior.final_delay_s)
                msg = "fake failure after accepted prefix"
                raise OSError(msg)
            try:
                final_gate = self._provider.final_gates.get(
                    (self._response_id, segment.sequence),
                )
                while (  # noqa: ASYNC110
                    final_gate is not None and not final_gate.is_set() and not self._closed
                ):
                    await asyncio.sleep(0.001)
                await asyncio.sleep(self._behavior.final_delay_s)
            except asyncio.CancelledError:
                if not self._behavior.late_after_abort:
                    raise
                while not self._provider.late_pcm_release.is_set():
                    try:
                        await asyncio.sleep(0.001)
                    except asyncio.CancelledError:
                        continue
                self._provider.late_yields.append((self._response_id, segment.sequence))
                yield voice_tts.TTSAudioChunk(
                    sequence=segment.sequence,
                    pcm=np.full(self._behavior.samples, -2000, dtype="<i2").tobytes(),
                    sample_rate_hz=self._behavior.sample_rate_hz,
                )
                yield voice_tts.TTSSegmentFinished(sequence=segment.sequence)
                return
            self._provider.provider_finals.append((self._response_id, segment.sequence))
            yield voice_tts.TTSSegmentFinished(sequence=segment.sequence)

    async def finish(self) -> None:
        self._closed = True

    async def abort(self, reason: str) -> None:
        self._provider.aborted.append((self._response_id, reason))
        self._closed = True

    async def close(self) -> None:
        self._provider.closed.append(self._response_id)
        self._closed = True


class FakeProvider:
    def __init__(
        self,
        behaviors: dict[tuple[str, int], Behavior] | None = None,
        *,
        candidate_count: int = 2,
    ) -> None:
        self.behaviors = behaviors or {}
        self._candidate_count = candidate_count
        self.opened: list[tuple[str, int]] = []
        self.sent: list[tuple[str, int]] = []
        self.reader_claims: dict[str, int] = {}
        self.provider_finals: list[tuple[str, int]] = []
        self.aborted: list[tuple[str, str]] = []
        self.closed: list[str] = []  # response ids, in the order their sessions were closed
        self.actions: list[str] = []
        self.segment_gates: dict[tuple[str, int], threading.Event] = {}
        self.final_gates: dict[tuple[str, int], threading.Event] = {}
        self.late_pcm_release = threading.Event()
        self.late_yields: list[tuple[str, int]] = []
        self.created = 0
        self.languages: list[str] = []
        self.connect_error: Exception | None = None

    @property
    def streaming_candidate_count(self) -> int:
        return self._candidate_count

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        language: str,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> voice_tts.TTSSession:
        del idle_close_s, command_queue_capacity, audio_queue_capacity
        self.created += 1
        self.languages.append(language)
        return FakeSession(
            self,
            endpoint_index=endpoint_index,
        )

    def request_close(self) -> None:
        return


class CallbackPump:
    def __init__(
        self,
        player: voice_tts.AudioStreamPlayer,
        *,
        record: bool = False,
    ) -> None:
        self._player = player
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="test-portaudio-pump")
        self._record = record
        # The blocks the production callback wrote are the signal PortAudio
        # hands the device; retaining them is the only observable of the
        # output amplitude edge.
        self.blocks: list[np.ndarray] = []

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        assert not self._thread.is_alive()

    def step(self, frames: int = 32) -> None:
        """Drive one real callback, exactly as the pump thread would."""
        out = np.zeros((frames, 1), dtype=np.float32)
        self._player._callback(out, frames, None, None)  # noqa: SLF001
        if self._record:
            self.blocks.append(out[:, 0].copy())

    @property
    def signal(self) -> np.ndarray:
        """Return every retained block concatenated, in emission order."""
        return np.concatenate(self.blocks)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            time.sleep(0.0005)


class RecordingBroadcaster:
    def __init__(self, actions: list[str]) -> None:
        self._actions = actions

    def broadcast_voice_sync(
        self,
        phase: str,
        *,
        turn_id: str,
        **payload: object,
    ) -> None:
        self._actions.append(
            f"ui:{phase}:{turn_id}:{payload.get('output_outcome', '')}",
        )


def make_player(*, ring_seconds: float = 0.25) -> voice_tts.AudioStreamPlayer:
    return voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=ring_seconds,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=0.0,
    )


def media_config() -> voice_media.StreamingMediaConfig:
    return voice_media.StreamingMediaConfig(
        canonical_sample_rate_hz=8_000,
        command_queue_capacity=8,
        response_lane_capacity=4,
        response_text_bytes=4096,
        session_command_capacity=1,
        session_audio_capacity=2,
        session_idle_close_s=1.0,
        response_timeout_s=2.0,
        ring_retry_s=0.0005,
        presentation_poll_s=0.0005,
        shutdown_timeout_s=2.0,
        enable_macos_say_fallback=False,
        prefetch_network_lost_line=False,
    )


def emit_response(  # noqa: PLR0913 - exact three-event fixture identity
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str | list[str],
    phase: str = "final",
) -> list[tuple[int, Event]]:
    texts = [text] if isinstance(text, str) else text
    events = [
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": turn_id,
                "query": "q",
                "kind": "text",
                "response_id": response_id,
                "response_group_id": group_id,
                "phase": phase,
                "channel": "speech",
            },
        ),
        *[
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": turn_id,
                    "text": chunk,
                    "response_id": response_id,
                    "response_group_id": group_id,
                    "sequence": sequence,
                    "phase": phase,
                    "channel": "speech",
                    "segment_hash": hashlib.sha256(chunk.encode()).hexdigest(),
                },
            )
            for sequence, chunk in enumerate(texts)
        ],
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": turn_id,
                "text": "".join(texts),
                "response_id": response_id,
                "response_group_id": group_id,
                "phase": phase,
                "channel": "speech",
            },
        ),
    ]
    rows: list[tuple[int, Event]] = []
    for event in events:
        row = conn.execute(
            "SELECT id FROM events WHERE event_uid = ?",
            (event.event_uid,),
        ).fetchone()
        assert row is not None
        rows.append((int(row[0]), event))
    return rows


async def submit_response(
    pipeline: voice_media.StreamingTTSPipeline,
    rows: list[tuple[int, Event]],
) -> list[voice_media.MediaSubmitOutcome]:
    return [
        await pipeline.submit_event(row_id=row_id, event=event, origin="direct")
        for row_id, event in rows
    ]


def terminal_rows(conn: sqlite3.Connection) -> list[tuple[str, dict[str, object]]]:
    rows = conn.execute(
        "SELECT type, payload_json FROM events WHERE type IN (?, ?, ?) ORDER BY id",
        (
            "surface.playback_completed",
            "surface.playback_interrupted",
            "surface.playback_failed",
        ),
    ).fetchall()
    return [(str(row[0]), json.loads(str(row[1]))) for row in rows]


def terminal_for(
    conn: sqlite3.Connection,
    *,
    response_id: str,
) -> tuple[str, dict[str, object]]:
    return next(
        (kind, payload)
        for kind, payload in terminal_rows(conn)
        if payload["response_id"] == response_id
    )


def wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> None:
    """Block until a pump/actor-driven condition holds, without asserting on it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.0005)
    msg = "timed out waiting for the media fixture to reach its gate condition"
    raise AssertionError(msg)


def count_rows(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def playback_rows_for(conn: sqlite3.Connection, response_id: str, kind: str) -> int:
    return count_rows(
        conn,
        "SELECT count(*) FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.response_id') = ?",
        kind,
        response_id,
    )


def await_playback_started(conn: sqlite3.Connection, response_id: str) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if playback_rows_for(conn, response_id, "surface.playback_started") == 1:
            return
        time.sleep(0.005)
    pytest.fail(f"{response_id} never reached surface.playback_started")


class FakeWebSocket:
    def __init__(self) -> None:
        self.audio_gate = asyncio.Event()
        self.sent: list[dict[str, object]] = []
        self.recv_calls = 0
        self.recv_in_flight = 0
        self.max_recv_in_flight = 0
        self.send_in_flight = 0
        self.max_send_in_flight = 0
        self.closed = False

    async def recv(self) -> str:
        self.recv_in_flight += 1
        self.max_recv_in_flight = max(
            self.max_recv_in_flight,
            self.recv_in_flight,
        )
        try:
            self.recv_calls += 1
            if self.recv_calls == 1:
                return json.dumps({"base_resp": {"status_code": 0}})
            if self.recv_calls == 2:
                return json.dumps({"base_resp": {"status_code": 0}})
            await self.audio_gate.wait()
            return json.dumps(
                {
                    "data": {"audio": np.ones(32, dtype="<i2").tobytes().hex()},
                    "is_final": True,
                    "base_resp": {"status_code": 0},
                },
            )
        finally:
            self.recv_in_flight -= 1

    async def send(self, raw: str) -> None:
        self.send_in_flight += 1
        self.max_send_in_flight = max(
            self.max_send_in_flight,
            self.send_in_flight,
        )
        try:
            await asyncio.sleep(0)
            self.sent.append(json.loads(raw))
        finally:
            self.send_in_flight -= 1

    async def close(self) -> None:
        self.closed = True


WORDS = tuple(f"{number}、" for number in range(1, 51))


TEXT = "".join(WORDS)


SAMPLES_PER_WORD = 80


RATE = 8_000


class AlignedSession(FakeSession):
    scale = 1.0  # the share of the audio the provider's word times span

    async def _events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:
        segment = await self._segments.get()
        assert segment.text == TEXT
        # MiniMax's word "end" is where the word starts (voiced onset for the first).
        ends = tuple(
            (len("".join(WORDS[:index])), (index - 1) * 10.0 * self.scale)
            for index in range(1, len(WORDS) + 1)
        )
        yield voice_tts.TTSAudioChunk(
            sequence=segment.sequence,
            pcm=b"",
            sample_rate_hz=RATE,
            timing_text=segment.text,
            word_boundaries=ends,
        )
        yield voice_tts.TTSAudioChunk(
            sequence=segment.sequence,
            pcm=np.full(len(WORDS) * SAMPLES_PER_WORD, 2_000, dtype="<i2").tobytes(),
            sample_rate_hz=RATE,
        )
        yield voice_tts.TTSSegmentFinished(sequence=segment.sequence)


class AlignedProvider(FakeProvider):
    session = AlignedSession

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        language: str,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> voice_tts.TTSSession:
        del language, idle_close_s, command_queue_capacity, audio_queue_capacity
        return self.session(self, endpoint_index=endpoint_index)
