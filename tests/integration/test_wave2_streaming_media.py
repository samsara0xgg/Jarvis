"""Wave 2 streaming output integration, churn, replay, and rollout gates."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Self, cast
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import yaml

from jarvis.decision.conversation import conversation_history_note
from jarvis.decision.packet import assemble_packet
from jarvis.runtime import inherent_loop
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback
from jarvis.surface import voice_media, voice_tts
from jarvis.surface.playback_recovery import reconcile_open_playback
from jarvis.surface.voice_ledger import (
    ForegroundBusy,
    GenerationLease,
    PlaybackLedger,
    StalePlaybackGeneration,
)
from scripts import bench_voice_streaming_output as voice_bench

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from typing import Any

    from jarvis.shared import Event


@dataclass(frozen=True)
class _Behavior:
    outcome: Literal["success", "fail_before", "fail_after"] = "success"
    final_delay_s: float = 0.01
    samples: int = 160
    sample_rate_hz: int = 8_000
    late_after_abort: bool = False
    amplitude: int = 2_000


class _FakeSession:
    def __init__(
        self,
        provider: _FakeProvider,
        *,
        endpoint_index: int,
    ) -> None:
        self._provider = provider
        self._endpoint_index = endpoint_index
        self._behavior = _Behavior()
        self._response_id = ""
        self._generation = -1
        self._segments: asyncio.Queue[voice_tts.TTSResponseSegment] = asyncio.Queue()
        self._reader_claimed = False
        self._closed = False
        self._send_active = False

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        self._response_id = response_id
        self._generation = playback_generation_id
        self._behavior = self._provider.behaviors.get(
            (response_id, self._endpoint_index),
            _Behavior(),
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

    async def _events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:
        while not self._closed:
            if self._behavior.outcome == "fail_before":
                msg = "fake connect/read failure before prefix"
                raise OSError(msg)
            segment = await self._segments.get()
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
        self._closed = True


class _FakeProvider:
    def __init__(
        self,
        behaviors: dict[tuple[str, int], _Behavior] | None = None,
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
        self.actions: list[str] = []
        self.segment_gates: dict[tuple[str, int], threading.Event] = {}
        self.final_gates: dict[tuple[str, int], threading.Event] = {}
        self.late_pcm_release = threading.Event()
        self.late_yields: list[tuple[str, int]] = []

    @property
    def streaming_candidate_count(self) -> int:
        return self._candidate_count

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> voice_tts.TTSSession:
        del idle_close_s, command_queue_capacity, audio_queue_capacity
        return _FakeSession(
            self,
            endpoint_index=endpoint_index,
        )

    def request_close(self) -> None:
        return


class _CallbackPump:
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


class _RecordingBroadcaster:
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


def _player(*, ring_seconds: float = 0.25) -> voice_tts.AudioStreamPlayer:
    return voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=ring_seconds,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=0.0,
    )


def test_media_power_transition_stops_then_reopens_fresh_player_stream(
    tmp_path: Path,
) -> None:
    """System sleep/wake uses typed bounded output lifecycle, not speech cancel."""
    db_path = tmp_path / "power-media.db"
    conn = open_event_log(db_path)
    conn.close()
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(),
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), shutdown_timeout_s=1.0),
        start_player=True,
    )
    suspended = pipeline.suspend_for_sleep(timeout_s=1.0)
    assert suspended.status == "suspended"
    assert suspended.succeeded
    assert player.stop.call_count == 1
    resumed = pipeline.resume_after_wake(timeout_s=1.0)
    assert resumed.status == "resumed"
    assert resumed.succeeded
    assert resumed.attempt_id > suspended.attempt_id
    assert player.start.call_count == 2
    assert pipeline.close(wait_timeout_s=1.0)


def _config() -> voice_media.StreamingMediaConfig:
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
    )


def _emit_response(  # noqa: PLR0913 - exact three-event fixture identity
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


async def _submit_response(
    pipeline: voice_media.StreamingTTSPipeline,
    rows: list[tuple[int, Event]],
) -> list[voice_media.MediaSubmitOutcome]:
    return [
        await pipeline.submit_event(row_id=row_id, event=event, origin="direct")
        for row_id, event in rows
    ]


def _terminal_rows(conn: sqlite3.Connection) -> list[tuple[str, dict[str, object]]]:
    rows = conn.execute(
        "SELECT type, payload_json FROM events WHERE type IN (?, ?, ?) ORDER BY id",
        (
            "surface.playback_completed",
            "surface.playback_interrupted",
            "surface.playback_failed",
        ),
    ).fetchall()
    return [(str(row[0]), json.loads(str(row[1]))) for row in rows]


def _terminal_for(
    conn: sqlite3.Connection,
    *,
    response_id: str,
) -> tuple[str, dict[str, object]]:
    return next(
        (kind, payload)
        for kind, payload in _terminal_rows(conn)
        if payload["response_id"] == response_id
    )


def _payload_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    assert isinstance(value, int)
    return value


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> None:
    """Block until a pump/actor-driven condition holds, without asserting on it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.0005)
    msg = "timed out waiting for the media fixture to reach its gate condition"
    raise AssertionError(msg)


def _pump_until(
    pump: _CallbackPump,
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 3.0,
) -> None:
    """Drive real callbacks from the test thread until a condition holds."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        pump.step()
        time.sleep(0.0005)
    msg = "timed out driving the callback to the fixture's gate condition"
    raise AssertionError(msg)


def _generation_ring(player: voice_tts.AudioStreamPlayer) -> voice_tts._GenerationRingBuffer:
    ring = player._generation_ring  # noqa: SLF001
    assert ring is not None
    return ring


def test_mid_generation_ring_starvation_lands_on_the_playback_terminal(
    tmp_path: Path,
) -> None:
    """A dry-then-resumed ring is durable as starvation_gaps on the terminal row."""
    db_path = tmp_path / "starvation.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider()
    second_segment = threading.Event()
    provider.segment_gates[("RS", 1)] = second_segment
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            rows = _emit_response(
                conn,
                response_id="RS",
                group_id="GS",
                turn_id="TS",
                text=["first segment. ", "second segment."],
            )
            asyncio.run(_submit_response(pipeline, rows))
            ring = _generation_ring(player)
            # The host has consumed every sample of segment 0 while the
            # provider is still gated: the generation is mid-stream and dry.
            _wait_until(lambda: ring._write_idx > 0 and ring.available_read() <= 0)  # noqa: SLF001
            second_segment.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    kind, payload = _terminal_for(open_event_log(db_path), response_id="RS")
    assert kind == "surface.playback_completed"
    assert payload["provider"] != "macos_say"
    assert payload["playback_generation_id"] == 1
    assert _payload_int(payload, "starvation_gaps") >= 1
    assert _payload_int(payload, "host_underflows") == 0


def test_a_clean_response_reports_no_starvation_for_its_end_of_generation_tail(
    tmp_path: Path,
) -> None:
    """The false-pass guard: the tail every clean response produces counts zero."""
    db_path = tmp_path / "clean-tail.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider()
    segment_final = threading.Event()
    provider.final_gates[("RC", 0)] = segment_final
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        rows = _emit_response(
            conn,
            response_id="RC",
            group_id="GC",
            turn_id="TC",
            text="clean tail",
        )
        asyncio.run(_submit_response(pipeline, rows))
        ring = _generation_ring(player)
        # Every sample of the single segment is in the ring before the host
        # ever runs, so the only dry window is the tail after the last block.
        _wait_until(lambda: ring._write_idx >= _Behavior().samples)  # noqa: SLF001
        with _CallbackPump(player):
            segment_final.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    kind, payload = _terminal_for(open_event_log(db_path), response_id="RC")
    assert kind == "surface.playback_completed"
    assert payload["provider"] != "macos_say"
    assert _payload_int(payload, "starvation_gaps") == 0
    assert _payload_int(payload, "host_underflows") == 0


def test_generation_cas_races_and_thousand_cycle_churn() -> None:  # noqa: C901, PLR0915
    """Late PCM/cancel/drain never crosses generations or leaks ring state."""
    drain_race = voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=0.02,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=0.05,
    )
    drain_lease = drain_race.activate_generation(
        session_id="S",
        response_id="RD",
        response_group_id="GD",
        turn_id="TD",
    )
    assert not isinstance(drain_lease, ForegroundBusy)
    drain_race.begin_generation_segment(
        expected_playback_generation_id=drain_lease.playback_generation_id,
        sequence=0,
        text="drain",
        segment_hash="drain",
    )
    drain_race.write_generation(
        np.ones(16, dtype=np.float32).tobytes(),
        expected_playback_generation_id=drain_lease.playback_generation_id,
        segment_sequence=0,
    )
    drain_race.finish_generation_segment(
        expected_playback_generation_id=drain_lease.playback_generation_id,
        sequence=0,
    )
    drain_race._callback(np.zeros((16, 1), dtype=np.float32), 16, None, None)  # noqa: SLF001
    interrupted_at_drain = drain_race.interrupt_generation(
        expected_playback_generation_id=drain_lease.playback_generation_id,
    )
    assert not isinstance(interrupted_at_drain, StalePlaybackGeneration)
    assert interrupted_at_drain.estimated_audible_samples == 0

    player = _player(ring_seconds=0.02)
    first = player.activate_generation(
        session_id="S",
        response_id="R0",
        response_group_id="G0",
        turn_id="T0",
    )
    assert not isinstance(first, ForegroundBusy)
    player.begin_generation_segment(
        expected_playback_generation_id=first.playback_generation_id,
        sequence=0,
        text="old",
        segment_hash="old",
    )
    assert (
        player.write_generation(
            np.full(16, -0.5, dtype=np.float32).tobytes(),
            expected_playback_generation_id=first.playback_generation_id,
            segment_sequence=0,
        )
        is not None
    )
    player.interrupt_generation(
        expected_playback_generation_id=first.playback_generation_id,
    )
    second = player.activate_generation(
        session_id="S",
        response_id="R1",
        response_group_id="G1",
        turn_id="T1",
    )
    assert not isinstance(second, ForegroundBusy)
    player.begin_generation_segment(
        expected_playback_generation_id=second.playback_generation_id,
        sequence=0,
        text="new",
        segment_hash="new",
    )
    late_pcm = player.write_generation(
        np.ones(8, dtype=np.float32).tobytes(),
        expected_playback_generation_id=first.playback_generation_id,
        segment_sequence=0,
    )
    late_cancel = player.interrupt_generation(
        expected_playback_generation_id=first.playback_generation_id,
    )
    assert isinstance(late_pcm, StalePlaybackGeneration)
    assert late_pcm.reason == "already_terminal"
    assert isinstance(late_cancel, StalePlaybackGeneration)
    assert (
        player.write_generation(
            np.full(16, 0.75, dtype=np.float32).tobytes(),
            expected_playback_generation_id=second.playback_generation_id,
            segment_sequence=0,
        )
        is not None
    )
    output = np.zeros((32, 1), dtype=np.float32)
    player._callback(output, 32, None, None)  # noqa: SLF001
    # A short read (`actual = 16 < frames = 32`) with a matching generation:
    # ADR-0006 D12 ramps its own last real samples to zero inside the block, so
    # the 16 samples are the second generation's 0.75 scaled by the descending
    # tail ramp -- never the stale 1.0 or the first generation's -0.5.
    assert np.allclose(output[:16, 0], np.float32(0.75) * np.linspace(1.0, 0.0, 16))

    player.interrupt_generation(
        expected_playback_generation_id=second.playback_generation_id,
    )
    assert player.settle_interrupted_generation(
        expected_playback_generation_id=first.playback_generation_id,
    ) is not None
    assert player.settle_interrupted_generation(
        expected_playback_generation_id=second.playback_generation_id,
    ) is not None
    player.retire_generation(first.playback_generation_id)
    player.retire_generation(second.playback_generation_id)
    cycle_start = threading.Barrier(3)
    cycle_done = threading.Barrier(3)
    writer_precommit = threading.Event()
    writer_release = threading.Event()
    callback_read = threading.Event()
    callback_release = threading.Event()
    current: dict[str, object] = {}
    worker_errors: list[BaseException] = []
    writer_results: list[object] = []
    callback_outputs: list[np.ndarray] = []
    original_frombuffer = np.frombuffer
    generation_ring = player._generation_ring  # noqa: SLF001
    assert generation_ring is not None
    original_read_into = generation_ring.read_into

    def _barrier_frombuffer(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = original_frombuffer(*args, **kwargs)
        if threading.current_thread().name == "late-provider-racer":
            writer_precommit.set()
            assert writer_release.wait(timeout=1.0)
        return result

    def _barrier_read_into(*args: Any, **kwargs: Any) -> int:  # noqa: ANN401
        result = original_read_into(*args, **kwargs)
        if threading.current_thread().name == "callback-racer":
            callback_read.set()
            assert callback_release.wait(timeout=1.0)
        return result

    def _late_provider_writer() -> None:
        try:
            for cycle in range(1_000):
                cycle_start.wait()
                lease = cast("Any", current["lease"])
                writer_results.append(
                    player.write_generation(
                        np.full(8, -(cycle + 1), dtype=np.float32).tobytes(),
                        expected_playback_generation_id=lease.playback_generation_id,
                        segment_sequence=0,
                    ),
                )
                cycle_done.wait()
        except BaseException as exc:  # noqa: BLE001 - barrier surfaces thread failures
            worker_errors.append(exc)

    def _concurrent_callback() -> None:
        try:
            for _cycle in range(1_000):
                cycle_start.wait()
                output = np.zeros((8, 1), dtype=np.float32)
                player._callback(output, 8, None, None)  # noqa: SLF001
                callback_outputs.append(output.copy())
                cycle_done.wait()
        except BaseException as exc:  # noqa: BLE001 - barrier surfaces thread failures
            worker_errors.append(exc)

    writer = threading.Thread(target=_late_provider_writer, name="late-provider-racer")
    callback = threading.Thread(target=_concurrent_callback, name="callback-racer")
    with (
        patch.object(np, "frombuffer", side_effect=_barrier_frombuffer),
        patch.object(generation_ring, "read_into", side_effect=_barrier_read_into),
    ):
        writer.start()
        callback.start()
        previous_value = np.float32(0.0)
        for cycle in range(1_000):
            old_lease = player.activate_generation(
                session_id="S",
                response_id=f"RN{cycle}",
                response_group_id=f"GN{cycle}",
                turn_id=f"TN{cycle}",
            )
            assert not isinstance(old_lease, ForegroundBusy)
            player.begin_generation_segment(
                expected_playback_generation_id=old_lease.playback_generation_id,
                sequence=0,
                text="old",
                segment_hash="old",
            )
            player.write_generation(
                np.full(8, -0.5, dtype=np.float32).tobytes(),
                expected_playback_generation_id=old_lease.playback_generation_id,
                segment_sequence=0,
            )
            current["lease"] = old_lease
            writer_precommit.clear()
            writer_release.clear()
            callback_read.clear()
            callback_release.clear()
            cycle_start.wait()
            assert writer_precommit.wait(timeout=1.0)
            assert callback_read.wait(timeout=1.0)
            player.interrupt_generation(
                expected_playback_generation_id=old_lease.playback_generation_id,
            )
            new_value = np.float32((cycle + 1) / 1_001)
            new_lease = player.activate_generation(
                session_id="S",
                response_id=f"RNEXT{cycle}",
                response_group_id=f"GNEXT{cycle}",
                turn_id=f"TNEXT{cycle}",
            )
            assert not isinstance(new_lease, ForegroundBusy)
            player.begin_generation_segment(
                expected_playback_generation_id=new_lease.playback_generation_id,
                sequence=0,
                text="new",
                segment_hash="new",
            )
            player.write_generation(
                np.full(8, new_value, dtype=np.float32).tobytes(),
                expected_playback_generation_id=new_lease.playback_generation_id,
                segment_sequence=0,
            )
            writer_release.set()
            callback_release.set()
            cycle_done.wait()
            # The block that straddles the CAS carries neither generation's
            # PCM.  What it does carry is the synthesized declick tail
            # (ADR-0006 D11) decaying off the previous cycle's amplitude,
            # monotonically toward zero.
            straddling = callback_outputs[-1][:, 0]
            assert not np.any(straddling == np.float32(-0.5))
            assert not np.any(straddling == new_value)
            assert np.all(np.abs(straddling) <= previous_value)
            assert np.all(np.diff(np.abs(straddling)) <= 0.0)
            new_output = np.zeros((8, 1), dtype=np.float32)
            player._callback(new_output, 8, None, None)  # noqa: SLF001
            # The next generation plays at full amplitude with no un-mute.
            assert np.all(new_output[:, 0] == new_value)
            previous_value = new_value
            settled = player.settle_interrupted_generation(
                expected_playback_generation_id=old_lease.playback_generation_id,
            )
            assert settled is not None
            player.retire_generation(old_lease.playback_generation_id)
            player.interrupt_generation(
                expected_playback_generation_id=new_lease.playback_generation_id,
            )
            settled_new = player.settle_interrupted_generation(
                expected_playback_generation_id=new_lease.playback_generation_id,
            )
            assert settled_new is not None
            player.retire_generation(new_lease.playback_generation_id)
    writer.join(timeout=2.0)
    callback.join(timeout=2.0)
    assert not writer.is_alive()
    assert not callback.is_alive()
    assert not worker_errors
    assert len(writer_results) == 1_000
    assert all(isinstance(result, StalePlaybackGeneration) for result in writer_results)
    stale_writer_results = cast("list[StalePlaybackGeneration]", writer_results)
    assert all(result.reason == "already_terminal" for result in stale_writer_results)
    # No straddling block ever leaked the tombstoned generation's PCM; the
    # per-cycle checks above cover the declick tail each one carries instead.
    assert all(not np.any(output == np.float32(-0.5)) for output in callback_outputs)
    assert player.active_lease is None
    assert player.bytes_pending() == 0
    assert not player._ledgers  # noqa: SLF001

    closing = player.activate_generation(
        session_id="S",
        response_id="RCLOSE",
        response_group_id="GCLOSE",
        turn_id="TCLOSE",
    )
    assert not isinstance(closing, ForegroundBusy)
    player.begin_generation_segment(
        expected_playback_generation_id=closing.playback_generation_id,
        sequence=0,
        text="close race",
        segment_hash="close-race",
    )
    player.stop()
    late_after_close = player.write_generation(
        np.ones(8, dtype=np.float32).tobytes(),
        expected_playback_generation_id=closing.playback_generation_id,
        segment_sequence=0,
    )
    cancel_after_close = player.interrupt_generation(
        expected_playback_generation_id=closing.playback_generation_id,
    )
    output_after_close = np.ones((8, 1), dtype=np.float32)
    player._callback(output_after_close, 8, None, None)  # noqa: SLF001
    assert isinstance(late_after_close, StalePlaybackGeneration)
    assert late_after_close.reason == "already_terminal"
    assert isinstance(cancel_after_close, StalePlaybackGeneration)
    assert np.count_nonzero(output_after_close) == 0


def test_callback_interrupt_linearization_and_gain_mailbox_barriers() -> None:  # noqa: PLR0915
    """Pre-CAS reports settle; post-CAS PCM and post-gain muted text do not."""
    player = _player(ring_seconds=0.02)
    lease = player.activate_generation(
        session_id="S",
        response_id="RBARRIER",
        response_group_id="GBARRIER",
        turn_id="TBARRIER",
    )
    assert not isinstance(lease, ForegroundBusy)
    player.begin_generation_segment(
        expected_playback_generation_id=lease.playback_generation_id,
        sequence=0,
        text="pre cas",
        segment_hash="pre-cas",
    )
    player.write_generation(
        np.ones(8, dtype=np.float32).tobytes(),
        expected_playback_generation_id=lease.playback_generation_id,
        segment_sequence=0,
    )
    player.finish_generation_segment(
        expected_playback_generation_id=lease.playback_generation_id,
        sequence=0,
    )
    report_entered = threading.Event()
    report_release = threading.Event()
    original_report = player._callback_reports.write  # noqa: SLF001

    def _blocked_report(**kwargs: object) -> None:
        report_entered.set()
        assert report_release.wait(timeout=1.0)
        original_report(**cast("Any", kwargs))

    output = np.zeros((8, 1), dtype=np.float32)
    with patch.object(player._callback_reports, "write", side_effect=_blocked_report):  # noqa: SLF001
        callback = threading.Thread(
            target=player._callback,  # noqa: SLF001
            args=(output, 8, None, None),
            name="callback-publication-barrier",
        )
        callback.start()
        assert report_entered.wait(timeout=1.0)
        player.interrupt_generation(
            expected_playback_generation_id=lease.playback_generation_id,
        )
        assert (
            player.settle_interrupted_generation(
                expected_playback_generation_id=lease.playback_generation_id,
            )
            is None
        )
        report_release.set()
        callback.join(timeout=1.0)
    assert not callback.is_alive()
    settled = player.settle_interrupted_generation(
        expected_playback_generation_id=lease.playback_generation_id,
    )
    assert settled is not None
    assert not isinstance(settled, StalePlaybackGeneration)
    assert settled.submitted_samples == 8
    assert np.all(output[:, 0] == 1.0)
    player.retire_generation(lease.playback_generation_id)

    post_cas = player.activate_generation(
        session_id="S",
        response_id="RPOST",
        response_group_id="GPOST",
        turn_id="TPOST",
    )
    assert not isinstance(post_cas, ForegroundBusy)
    player.begin_generation_segment(
        expected_playback_generation_id=post_cas.playback_generation_id,
        sequence=0,
        text="post cas",
        segment_hash="post-cas",
    )
    player.write_generation(
        np.ones(8, dtype=np.float32).tobytes(),
        expected_playback_generation_id=post_cas.playback_generation_id,
        segment_sequence=0,
    )
    player.interrupt_generation(
        expected_playback_generation_id=post_cas.playback_generation_id,
    )
    post_output = np.ones((8, 1), dtype=np.float32)
    player._callback(post_output, 8, None, None)  # noqa: SLF001
    post_settled = player.settle_interrupted_generation(
        expected_playback_generation_id=post_cas.playback_generation_id,
    )
    assert post_settled is not None
    assert not isinstance(post_settled, StalePlaybackGeneration)
    assert post_settled.submitted_samples == 0
    # No post-CAS generation sample reaches the host.  What the host does get
    # is the synthesized declick tail (ADR-0006 D11) decaying off the previous
    # block's amplitude, never the flat 1.0 the tombstoned generation wrote.
    assert np.all(post_output[:, 0] == voice_tts._DECLICK_RAMP[:8])  # noqa: SLF001
    player.retire_generation(post_cas.playback_generation_id)

    gain_player = _player(ring_seconds=0.02)
    gain_lease = gain_player.activate_generation(
        session_id="S",
        response_id="RGAIN",
        response_group_id="GGAIN",
        turn_id="TGAIN",
    )
    assert not isinstance(gain_lease, ForegroundBusy)
    gain_player.begin_generation_segment(
        expected_playback_generation_id=gain_lease.playback_generation_id,
        sequence=7,
        text="must not be heard",
        segment_hash="gain",
    )
    gain_player.write_generation(
        np.ones(16, dtype=np.float32).tobytes(),
        expected_playback_generation_id=gain_lease.playback_generation_id,
        segment_sequence=7,
    )
    gain_player.finish_generation_segment(
        expected_playback_generation_id=gain_lease.playback_generation_id,
        sequence=7,
    )
    apply_entered = threading.Event()
    apply_release = threading.Event()
    original_apply = gain_player._gain.apply  # noqa: SLF001
    apply_calls = 0

    def _barrier_apply(block: np.ndarray) -> str:
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 1:
            apply_entered.set()
            assert apply_release.wait(timeout=1.0)
        return original_apply(block)

    first_gain_output = np.zeros((8, 1), dtype=np.float32)
    with patch.object(gain_player._gain, "apply", side_effect=_barrier_apply):  # noqa: SLF001
        gain_callback = threading.Thread(
            target=gain_player._callback,  # noqa: SLF001
            args=(first_gain_output, 8, None, None),
            name="gain-command-barrier",
        )
        gain_callback.start()
        assert apply_entered.wait(timeout=1.0)
        gain_player.set_gain(0.0, ramp_ms=0.0)
        apply_release.set()
        gain_callback.join(timeout=1.0)
        second_gain_output = np.ones((8, 1), dtype=np.float32)
        gain_player._callback(second_gain_output, 8, None, None)  # noqa: SLF001
    assert not gain_callback.is_alive()
    snapshot = gain_player.poll_generation(gain_lease.playback_generation_id)
    assert not isinstance(snapshot, StalePlaybackGeneration)
    assert np.all(first_gain_output[:, 0] == 1.0)
    assert np.count_nonzero(second_gain_output) == 0
    assert snapshot.estimated_audible_samples == 16
    assert snapshot.heard_text == ""
    assert snapshot.heard_through_sequence is None


def test_streaming_owner_first_accept_replay_dedup_and_prefix_safety(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Real actor wiring streams before final and enforces boot/prefix boundaries."""
    reset_realtime_trace()
    db_path = tmp_path / "wave2.db"
    conn = open_event_log(db_path)
    historical = _emit_response(
        conn,
        response_id="RH",
        group_id="GH",
        turn_id="TH",
        text="historical",
    )
    high_water = max(row_id for row_id, _event in historical)
    provider = _FakeProvider(
        {
            ("RF", 0): _Behavior("fail_before"),
            ("RF", 1): _Behavior(
                "success",
                final_delay_s=0.03,
                sample_rate_hz=4_000,
            ),
            ("RP", 0): _Behavior("fail_after"),
        },
    )
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=high_water,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            historical_outcome = asyncio.run(
                pipeline.submit_event(
                    row_id=historical[0][0],
                    event=historical[0][1],
                    origin="replay",
                ),
            )
            assert historical_outcome.status == "historical"

            rows = _emit_response(
                conn,
                response_id="RF",
                group_id="GF",
                turn_id="TF",
                text="fallback before prefix",
            )
            outcomes = asyncio.run(_submit_response(pipeline, rows))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 3
            duplicate = asyncio.run(
                pipeline.submit_event(
                    row_id=rows[1][0],
                    event=rows[1][1],
                    origin="watcher",
                ),
            )
            assert duplicate.status == "duplicate"
            assert pipeline.wait_until_idle(timeout_s=2.0)
            assert provider.opened[:2] == [("RF", 0), ("RF", 1)]
            points = [
                point
                for point in realtime_trace_snapshot()
                if point.attributes.get("response_id") == "RF"
            ]
            accept_ns = next(
                point.monotonic_ns for point in points if point.name == "tts_player_first_accept"
            )
            final_ns = next(
                point.monotonic_ns for point in points if point.name == "tts_provider_final"
            )
            assert accept_ns < final_ns
            assert provider.reader_claims == {"RF": 2}

            late = emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": "TF",
                    "text": "late",
                    "response_id": "RF",
                    "response_group_id": "GF",
                    "sequence": 99,
                },
            )
            late_id = int(
                conn.execute(
                    "SELECT id FROM events WHERE event_uid = ?",
                    (late.event_uid,),
                ).fetchone()[0],
            )
            late_outcome = asyncio.run(
                pipeline.submit_event(row_id=late_id, event=late, origin="watcher"),
            )
            assert late_outcome.status == "terminal"

            multi = _emit_response(
                conn,
                response_id="RM",
                group_id="GM",
                turn_id="TM",
                text=["first segment. ", "second segment."],
            )
            asyncio.run(_submit_response(pipeline, multi))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            assert [sequence for response, sequence in provider.sent if response == "RM"] == [
                0,
                1,
            ]
            assert provider.reader_claims["RM"] == 1
            assert [endpoint for response, endpoint in provider.opened if response == "RM"] == [0]

            partial = _emit_response(
                conn,
                response_id="RP",
                group_id="GP",
                turn_id="TP",
                text="never replay my prefix",
            )
            asyncio.run(_submit_response(pipeline, partial))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            assert ("RP", 1) not in provider.opened
    finally:
        assert pipeline.close()
        conn.close()
    terminals = _terminal_rows(open_event_log(db_path))
    by_response = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert by_response["RF"] == "surface.playback_completed"
    assert by_response["RM"] == "surface.playback_completed"
    assert by_response["RP"] == "surface.playback_failed"
    rf_payload = next(payload for _kind, payload in terminals if payload["response_id"] == "RF")
    total_samples = rf_payload["total_samples"]
    assert isinstance(total_samples, int)
    assert total_samples > 160
    assert sum(payload["response_id"] == "RP" for _kind, payload in terminals) == 1
    assert not any(thread.name == pipeline.media_thread_name for thread in threading.enumerate())


def test_aborted_provider_late_pcm_cannot_pollute_active_successor(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """A cancellation-resistant N provider may yield late, but only N+1 reaches host."""
    db_path = tmp_path / "late-provider.db"
    conn = open_event_log(db_path)
    old_final = threading.Event()
    provider = _FakeProvider(
        {
            ("RLATE-N", 0): _Behavior(
                "success",
                final_delay_s=1.0,
                late_after_abort=True,
            ),
            ("RLATE-NEXT", 0): _Behavior("success", final_delay_s=0.01),
        },
        candidate_count=1,
    )
    provider.final_gates[("RLATE-N", 0)] = old_final
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), response_timeout_s=3.0),
        start_player=False,
    )
    try:
        old = _emit_response(
            conn,
            response_id="RLATE-N",
            group_id="GLATE-N",
            turn_id="TLATE-N",
            text="old provider",
        )
        asyncio.run(_submit_response(pipeline, old))
        deadline = time.monotonic() + 1.0
        while player.bytes_pending() == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert player.bytes_pending() > 0
        successor = _emit_response(
            conn,
            response_id="RLATE-NEXT",
            group_id="GLATE-NEXT",
            turn_id="TLATE-NEXT",
            text="new provider",
        )
        asyncio.run(_submit_response(pipeline, successor))
        deadline = time.monotonic() + 1.0
        while (
            (("RLATE-NEXT", 0) not in provider.sent or player.bytes_pending() == 0)
            and time.monotonic() < deadline
        ):
            time.sleep(0.002)
        assert ("RLATE-NEXT", 0) in provider.sent
        assert player.bytes_pending() > 0
        provider.late_pcm_release.set()
        deadline = time.monotonic() + 1.0
        while not provider.late_yields and time.monotonic() < deadline:
            time.sleep(0.002)
        assert provider.late_yields == [("RLATE-N", 0)]
        host_block = np.zeros((320, 1), dtype=np.float32)
        player._callback(host_block, 320, None, None)  # noqa: SLF001
        nonzero = host_block[host_block != 0]
        assert nonzero.size > 0
        assert np.all(nonzero > 0), "late N uses negative PCM and must be generation-dropped"
        deadline = time.monotonic() + 2.0
        while not pipeline.wait_until_idle(timeout_s=0.0) and time.monotonic() < deadline:
            player._callback(np.zeros((320, 1), dtype=np.float32), 320, None, None)  # noqa: SLF001
            time.sleep(0.002)
        assert pipeline.wait_until_idle(timeout_s=0.05)
    finally:
        old_final.set()
        provider.late_pcm_release.set()
        assert pipeline.close()
        conn.close()
    terminal_conn = open_event_log(db_path)
    try:
        terminals = [
            (kind, payload)
            for kind, payload in _terminal_rows(terminal_conn)
            if payload["response_id"] in {"RLATE-N", "RLATE-NEXT"}
        ]
    finally:
        terminal_conn.close()
    assert len(terminals) == 2
    assert {kind for kind, _payload in terminals} == {
        "surface.playback_interrupted",
        "surface.playback_completed",
    }


@pytest.mark.parametrize("terminal_mode", ["completed", "failed", "supersede"])
@pytest.mark.parametrize("persistent", [False, True])
def test_terminal_debt_blocks_every_successor_until_durable(
    tmp_path: Path,
    terminal_mode: str,
    persistent: bool,  # noqa: FBT001 - data-driven fault mode
) -> None:
    """Completed/failed/supersede all fail closed behind the terminal CAS."""
    db_path = tmp_path / f"terminal-{terminal_mode}-{persistent}.db"
    conn = open_event_log(db_path)
    first_behavior = (
        _Behavior("fail_after", final_delay_s=0.04)
        if terminal_mode == "failed"
        else _Behavior("success", final_delay_s=0.08)
    )
    provider = _FakeProvider({("RDEBT", 0): first_behavior}, candidate_count=1)
    actions = provider.actions
    broadcaster = _RecordingBroadcaster(actions)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        broadcaster=broadcaster,
        start_player=False,
    )
    original_terminalize = terminalize_playback
    attempts = 0

    def _faulted_terminalize(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        payload = cast("dict[str, object]", kwargs["payload"])
        if payload["response_id"] == "RDEBT":
            attempts += 1
            actions.append(f"terminal-attempt:{attempts}")
            if persistent or attempts < 3:
                msg = "injected playback terminal append fault"
                raise RuntimeError(msg)
        outcome = original_terminalize(*cast("Any", args), **cast("Any", kwargs))
        if payload["response_id"] == "RDEBT":
            actions.append("terminal-durable:RDEBT")
        return outcome

    try:
        with (
            patch.object(voice_media, "terminalize_playback", side_effect=_faulted_terminalize),
            _CallbackPump(player),
        ):
            first = _emit_response(
                conn,
                response_id="RDEBT",
                group_id="GDEBT",
                turn_id="TDEBT",
                text="old response",
            )
            asyncio.run(_submit_response(pipeline, first))
            successor = _emit_response(
                conn,
                response_id="RNEXT",
                group_id=("GNEXT" if terminal_mode == "supersede" else "GDEBT"),
                turn_id="TNEXT",
                text="successor response",
            )
            asyncio.run(_submit_response(pipeline, successor))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()

    terminals = _terminal_rows(open_event_log(db_path))
    first_terminals = [
        kind for kind, payload in terminals if payload["response_id"] == "RDEBT"
    ]
    assert attempts == 3
    if persistent:
        assert first_terminals == []
        assert ("RNEXT", 0) not in provider.opened
        assert not any(action.startswith("ui:spoken:TDEBT") for action in actions)
        assert player._next_generation == 1  # noqa: SLF001
    else:
        expected = {
            "completed": "surface.playback_completed",
            "failed": "surface.playback_failed",
            "supersede": "surface.playback_interrupted",
        }[terminal_mode]
        assert first_terminals == [expected]
        assert ("RNEXT", 0) in provider.opened
        terminal_index = actions.index("terminal-durable:RDEBT")
        successor_index = actions.index("open:RNEXT")
        ui_index = next(
            index
            for index, action in enumerate(actions)
            if action.startswith("ui:spoken:TDEBT")
        )
        assert terminal_index < ui_index < successor_index


def test_checkpoint_persists_during_later_provider_feed_and_retries(  # noqa: PLR0915 - real actor/cursor/prompt round trip
    tmp_path: Path,
) -> None:
    """A whole heard segment checkpoints before the blocked next segment final."""
    db_path = tmp_path / "checkpoint-parallel.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    second_gate = threading.Event()
    provider.segment_gates[("RCP", 1)] = second_gate
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    original_emit = emit_event
    checkpoint_attempts = 0
    emit_event(
        conn,
        type="utterance.received",
        payload={
            "turn_id": "TCP",
            "transcript": "synthetic checkpoint question",
            "channel": "voice",
        },
    )

    def _transient_checkpoint(*args: object, **kwargs: object) -> object:
        nonlocal checkpoint_attempts
        if kwargs.get("type") == "surface.playback_checkpoint":
            checkpoint_attempts += 1
            if checkpoint_attempts == 1:
                msg = "transient checkpoint append fault"
                raise RuntimeError(msg)
        return original_emit(*cast("Any", args), **cast("Any", kwargs))

    try:
        rows = _emit_response(
            conn,
            response_id="RCP",
            group_id="GCP",
            turn_id="TCP",
            text=["first checkpoint. ", "blocked second."],
        )
        with (
            patch.object(voice_media, "emit_event", side_effect=_transient_checkpoint),
            _CallbackPump(player),
        ):
            asyncio.run(_submit_response(pipeline, rows))
            deadline = time.monotonic() + 1.0
            checkpoint_row: tuple[object, ...] | None = None
            while time.monotonic() < deadline:
                checkpoint_row = conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE type = 'surface.playback_checkpoint' ORDER BY id LIMIT 1",
                ).fetchone()
                if checkpoint_row is not None:
                    break
                time.sleep(0.005)
            assert checkpoint_row is not None
            payload = json.loads(str(checkpoint_row[0]))
            assert payload["heard_through_sequence"] == 0
            assert payload["heard_text"] == "first checkpoint."
            followup = emit_event(
                conn,
                type="utterance.received",
                payload={
                    "turn_id": "TCP-NEXT",
                    "transcript": "continue from what I heard",
                    "channel": "voice",
                },
            )
            note = conversation_history_note(assemble_packet(followup, conn))
            assert note is not None
            view = json.loads(note.rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
            assert view["spoken_heard"]["text"] == "first checkpoint."
            assert view["spoken_heard"]["cursor_quality"] in {"estimated", "measured_dac"}
            assert view["panel_available"] == "first checkpoint. blocked second."
            assert "audit_generated" not in view
            assert ("RCP", 1) in provider.sent
            assert ("RCP", 0) in provider.provider_finals
            assert ("RCP", 1) not in provider.provider_finals
            assert checkpoint_attempts >= 2
            second_gate.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
            completed_note = conversation_history_note(assemble_packet(followup, conn))
            assert completed_note is not None
            completed = json.loads(completed_note.rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
            assert completed["spoken_heard"]["text"] == "first checkpoint.blocked second."
    finally:
        second_gate.set()
        assert pipeline.close()
        conn.close()
    checkpoint_conn = open_event_log(db_path)
    try:
        first_count = checkpoint_conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'surface.playback_checkpoint' "
            "AND json_extract(payload_json, '$.heard_through_sequence') = 0",
        ).fetchone()
        assert first_count is not None
        assert first_count[0] == 1
    finally:
        checkpoint_conn.close()


def test_ordered_event_log_drain_handles_cross_source_reordering(tmp_path: Path) -> None:
    """A later direct wake drains open/chunk/emitted once across unrelated rows."""
    db_path = tmp_path / "ordered-drain.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    open_event = emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": "TORDER",
            "query": "q",
            "kind": "text",
            "response_id": "RORDER",
            "response_group_id": "GORDER",
        },
    )
    unrelated_one = emit_event(
        conn,
        type="surface.user_intent",
        payload={"turn_id": "T-UNRELATED-1", "transcript": "unrelated"},
    )
    chunk_event = emit_event(
        conn,
        type="surface.response_chunk",
        payload={
            "turn_id": "TORDER",
            "text": "ordered once",
            "response_id": "RORDER",
            "response_group_id": "GORDER",
            "sequence": 0,
        },
    )
    unrelated_two = emit_event(
        conn,
        type="surface.user_intent",
        payload={"turn_id": "T-UNRELATED-2", "transcript": "unrelated"},
    )
    emitted_event = emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            "turn_id": "TORDER",
            "text": "ordered once",
            "response_id": "RORDER",
            "response_group_id": "GORDER",
        },
    )
    event_ids = {
        event.event_uid: int(
            conn.execute(
                "SELECT id FROM events WHERE event_uid = ?",
                (event.event_uid,),
            ).fetchone()[0],
        )
        for event in (
            open_event,
            unrelated_one,
            chunk_event,
            unrelated_two,
            emitted_event,
        )
    }
    try:
        with _CallbackPump(player):
            direct = asyncio.run(
                pipeline.submit_event(
                    row_id=event_ids[emitted_event.event_uid],
                    event=emitted_event,
                    origin="direct",
                ),
            )
            assert direct.status == "accepted"
            duplicate_open = asyncio.run(
                pipeline.submit_event(
                    row_id=event_ids[open_event.event_uid],
                    event=open_event,
                    origin="watcher",
                ),
            )
            duplicate_chunk = asyncio.run(
                pipeline.submit_event(
                    row_id=event_ids[chunk_event.event_uid],
                    event=chunk_event,
                    origin="watcher",
                ),
            )
            duplicate_emitted = asyncio.run(
                pipeline.submit_event(
                    row_id=event_ids[emitted_event.event_uid],
                    event=emitted_event,
                    origin="watcher",
                ),
            )
            assert duplicate_open.status == "duplicate"
            assert duplicate_chunk.status == "duplicate"
            assert duplicate_emitted.status == "duplicate"
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert provider.opened.count(("RORDER", 0)) == 1
    terminal_conn = open_event_log(db_path)
    try:
        assert len(_terminal_rows(terminal_conn)) == 1
    finally:
        terminal_conn.close()


def test_watcher_retries_overloaded_final_row_without_a_later_wakeup(  # noqa: C901, PLR0915
    tmp_path: Path,
) -> None:
    """A full media queue cannot strand the last durable emitted row."""
    db_path = tmp_path / "watcher-overload.db"
    conn = open_event_log(db_path)
    rows = _emit_response(
        conn,
        response_id="RWATCH-LAST",
        group_id="GWATCH-LAST",
        turn_id="TWATCH-LAST",
        text="last committed row",
    )
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), command_queue_capacity=1),
        start_player=False,
    )
    command_entered = threading.Event()
    command_release = threading.Event()
    original_handle = pipeline._handle_command  # noqa: SLF001
    original_submit = pipeline.submit_event
    watcher_statuses: list[str] = []
    blocked_once = False

    async def _blocked_handle(
        command: voice_media._MediaCommand,
    ) -> voice_media.MediaSubmitOutcome:
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            command_entered.set()
            while not command_release.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.001)
        return await original_handle(command)

    async def _recording_submit(
        *,
        row_id: int,
        event: Event,
        origin: str = "watcher",
    ) -> voice_media.MediaSubmitOutcome:
        outcome = await original_submit(row_id=row_id, event=event, origin=origin)
        if origin == "watcher":
            watcher_statuses.append(outcome.status)
        return outcome

    submitters: list[threading.Thread] = []
    watcher_conn = open_event_log(db_path)

    async def _watch_until_terminal() -> None:
        watcher = asyncio.create_task(
            inherent_loop._tts_watcher(  # noqa: SLF001 - production watcher integration
                conn=watcher_conn,
                pipeline=pipeline,
                poll_interval_s=0.005,
            ),
        )
        try:
            deadline = time.monotonic() + 1.0
            while "overloaded" not in watcher_statuses:
                if time.monotonic() >= deadline:
                    pytest.fail("watcher never observed bounded queue overload")
                await asyncio.sleep(0.002)
            command_release.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if conn.execute(
                    "SELECT 1 FROM events WHERE type = 'surface.playback_completed' "
                    "AND json_extract(payload_json, '$.response_id') = 'RWATCH-LAST'",
                ).fetchone():
                    return
                await asyncio.sleep(0.005)
            pytest.fail("final emitted row remained stranded after capacity freed")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    try:
        with (
            _CallbackPump(player),
            patch.object(pipeline, "_handle_command", side_effect=_blocked_handle),
            patch.object(pipeline, "submit_event", side_effect=_recording_submit),
        ):
            for row_id, event in rows[:2]:
                submitter = threading.Thread(
                    target=asyncio.run,
                    args=(original_submit(row_id=row_id, event=event, origin="direct"),),
                )
                submitter.start()
                submitters.append(submitter)
                if len(submitters) == 1:
                    assert command_entered.wait(timeout=1.0)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                queue = pipeline._queue  # noqa: SLF001
                if queue is not None and queue.full():
                    break
                time.sleep(0.001)
            else:
                pytest.fail("direct delivery did not fill the media queue")
            asyncio.run(_watch_until_terminal())
        assert pipeline.wait_until_idle(timeout_s=1.0)
    finally:
        command_release.set()
        assert pipeline.close()
        watcher_conn.close()
        conn.close()
    for submitter in submitters:
        submitter.join(timeout=1.0)
        assert not submitter.is_alive()
    assert watcher_statuses.count("overloaded") >= 1
    terminal_conn = open_event_log(db_path)
    try:
        terminals = [
            payload
            for kind, payload in _terminal_rows(terminal_conn)
            if kind == "surface.playback_completed"
            and payload["response_id"] == "RWATCH-LAST"
        ]
    finally:
        terminal_conn.close()
    assert len(terminals) == 1
    assert provider.opened.count(("RWATCH-LAST", 0)) == 1


def test_structured_chunks_preserve_heard_prefix_on_mid_second_interrupt(
    tmp_path: Path,
) -> None:
    """Voice/document parsing retains renderer sequence/hash across chunk tags."""
    db_path = tmp_path / "structured-prefix.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    second_final = threading.Event()
    provider.final_gates[("RSTRUCT", 1)] = second_final
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    emit_event(
        conn,
        type="utterance.received",
        payload={
            "turn_id": "TSTRUCT",
            "transcript": "synthetic structured question",
            "channel": "voice",
        },
    )
    try:
        with _CallbackPump(player):
            structured = _emit_response(
                conn,
                response_id="RSTRUCT",
                group_id="GSTRUCT",
                turn_id="TSTRUCT",
                text=[
                    "<voice>第一句。",
                    "第二句。</voice><document>不可朗读。</document>",
                ],
            )
            asyncio.run(_submit_response(pipeline, structured))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                checkpoint = conn.execute(
                    "SELECT 1 FROM events WHERE type = 'surface.playback_checkpoint' "
                    "AND json_extract(payload_json, '$.response_id') = 'RSTRUCT' "
                    "AND json_extract(payload_json, '$.heard_through_sequence') = 0",
                ).fetchone()
                if checkpoint is not None and ("RSTRUCT", 1) in provider.sent:
                    break
                time.sleep(0.005)
            else:
                pytest.fail("first structured segment never checkpointed")
            superseding = _emit_response(
                conn,
                response_id="RSTRUCT-NEXT",
                group_id="GSTRUCT-NEXT",
                turn_id="TSTRUCT-NEXT",
                text="new foreground",
            )
            asyncio.run(_submit_response(pipeline, superseding))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        second_final.set()
        assert pipeline.close()
        conn.close()
    terminal_conn = open_event_log(db_path)
    try:
        old_payload = next(
            payload
            for kind, payload in _terminal_rows(terminal_conn)
            if kind == "surface.playback_interrupted" and payload["response_id"] == "RSTRUCT"
        )
        followup = emit_event(
            terminal_conn,
            type="utterance.received",
            payload={
                "turn_id": "TSTRUCT-FOLLOWUP",
                "transcript": "继续解释",
                "channel": "voice",
            },
        )
        note = conversation_history_note(assemble_packet(followup, terminal_conn))
        assert note is not None
        view = json.loads(note.rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
        assert view["spoken_heard"]["text"] == "第一句。"
        assert "第二句" in view["panel_available"]
        assert "不可朗读" in view["panel_available"]
    finally:
        terminal_conn.close()
    assert old_payload["heard_through_sequence"] == 0
    assert old_payload["heard_text"] == "第一句。"
    assert "第二句" not in str(old_payload["heard_text"])
    assert "不可朗读" not in str(old_payload["heard_text"])


def test_structured_lexer_carries_every_split_tag_without_losing_chunk_identity(
    tmp_path: Path,
) -> None:
    """Every lexical split keeps tags/documents off the wire and ids exact."""
    raw = "<voice>spoken</voice><document>hidden</document>"
    spoken_start = raw.index("spoken")
    spoken_end = spoken_start + len("spoken")
    # Every byte offset inside each tag, plus both of its boundaries: the
    # boundary cuts are the adjacent `</voice><document>` transition.
    cuts = [
        raw.index(tag) + offset
        for tag in ("<voice>", "</voice>", "<document>", "</document>")
        for offset in range(len(tag) + 1)
    ]
    db_path = tmp_path / "lexer-splits.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            for index, cut in enumerate(cuts):
                response_id = f"RLEX{index}"
                parts = (raw[:cut], raw[cut:])
                rows = _emit_response(
                    conn,
                    response_id=response_id,
                    group_id=f"GLEX{index}",
                    turn_id=f"TLEX{index}",
                    text=list(parts),
                )
                asyncio.run(_submit_response(pipeline, rows))
                assert pipeline.wait_until_idle(timeout_s=2.0), f"cut={cut}"
                prepared = [
                    json.loads(str(row[0]))
                    for row in conn.execute(
                        "SELECT payload_json FROM events "
                        "WHERE type = 'surface.playback_segment_prepared' "
                        "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
                        (response_id,),
                    ).fetchall()
                ]
                expected = [
                    sequence
                    for sequence, (start, end) in enumerate(((0, cut), (cut, len(raw))))
                    if start < spoken_end and end > spoken_start
                ]
                assert [row["sequence"] for row in prepared] == expected, f"cut={cut}"
                assert "".join(str(row["speech_text"]) for row in prepared) == "spoken", (
                    f"cut={cut}"
                )
                assert [row["segment_hash"] for row in prepared] == [
                    hashlib.sha256(parts[sequence].encode()).hexdigest()
                    for sequence in expected
                ], f"cut={cut}"
    finally:
        assert pipeline.close()
        conn.close()


def test_after_drain_same_group_and_foreground_supersede(tmp_path: Path) -> None:
    """Commentary/final queue in-group; a different foreground group interrupts."""
    db_path = tmp_path / "lane.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {
            ("RC", 0): _Behavior("success", final_delay_s=0.06),
            ("RN", 0): _Behavior("success", final_delay_s=0.2),
        },
        candidate_count=1,
    )
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            commentary = _emit_response(
                conn,
                response_id="RC",
                group_id="G1",
                turn_id="TC",
                text="commentary",
                phase="commentary",
            )
            asyncio.run(_submit_response(pipeline, commentary))
            final = _emit_response(
                conn,
                response_id="RFN",
                group_id="G1",
                turn_id="TFN",
                text="final",
            )
            asyncio.run(_submit_response(pipeline, final))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            assert [response for response, _endpoint in provider.opened[:2]] == ["RC", "RFN"]

            old = _emit_response(
                conn,
                response_id="RN",
                group_id="G2",
                turn_id="TN",
                text="old foreground",
            )
            asyncio.run(_submit_response(pipeline, old))
            superseding = _emit_response(
                conn,
                response_id="RS",
                group_id="G3",
                turn_id="TS",
                text="new foreground",
            )
            asyncio.run(_submit_response(pipeline, superseding))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    terminals = _terminal_rows(open_event_log(db_path))
    terminal_kind = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert terminal_kind["RC"] == "surface.playback_completed"
    assert terminal_kind["RFN"] == "surface.playback_completed"
    assert terminal_kind["RN"] == "surface.playback_interrupted"
    assert terminal_kind["RS"] == "surface.playback_completed"


def _isolation_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'surface.playback_lane_isolated' ORDER BY id",
    ).fetchall()
    return [cast("dict[str, object]", json.loads(str(row[0]))) for row in rows]


def test_a_normal_response_reports_no_starvation_for_its_prefill(
    tmp_path: Path,
) -> None:
    """The other false-pass guard: the host runs before the first sample exists.

    The clean-tail case starts its pump only once the ring is full, so it never
    exercises the prefill half of the rule. Here the host is already calling
    into a dry ring before the provider has produced anything, which is every
    real response's opening moment.
    """
    db_path = tmp_path / "prefill.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            # The pump is already starved before the response exists.
            _wait_until(lambda: player.callback_calls > 0)
            rows = _emit_response(
                conn,
                response_id="RP0",
                group_id="GP0",
                turn_id="TP0",
                text="prefill is not starvation",
            )
            asyncio.run(_submit_response(pipeline, rows))
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    kind, payload = _terminal_for(open_event_log(db_path), response_id="RP0")
    assert kind == "surface.playback_completed"
    assert payload["provider"] != "macos_say"
    assert _payload_int(payload, "starvation_gaps") == 0
    assert _payload_int(payload, "host_underflows") == 0


def test_an_unsettled_callback_publication_writes_a_durable_lane_isolated_row(
    tmp_path: Path,
) -> None:
    """Total silence until restart stops being unprovable after the fact."""
    db_path = tmp_path / "lane-isolated.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RISO", 0)] = hold
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with (
            _CallbackPump(player),
            patch.object(player, "settle_interrupted_generation", return_value=None),
        ):
            rows = _emit_response(
                conn,
                response_id="RISO",
                group_id="GISO",
                turn_id="TISO",
                text="this lane is about to go deaf",
            )
            asyncio.run(_submit_response(pipeline, rows))
            _await_playback_started(conn, "RISO")
            assert pipeline.stop_foreground_output("RISO") != "applied"
    finally:
        hold.set()
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()

    verdict = open_event_log(db_path)
    try:
        isolated = _isolation_rows(verdict)
        assert len(isolated) == 1
        payload = isolated[0]
        assert payload["isolation_reason"] == "callback_publication_unsettled"
        assert payload["terminal_type"] == "surface.playback_interrupted"
        assert payload["playback_generation_id"] == 1
        assert payload["response_id"] == "RISO"
        assert payload["turn_id"] == "TISO"
        assert payload["error_type"] == "RuntimeError"
        assert payload["session_id"]
        # No terminal could be written honestly: the snapshot never settled.
        assert _playback_rows_for(verdict, "RISO", "surface.playback_interrupted") == 0

        # The next boot joins the isolation row on the same CAS identity and
        # blames the isolation, not the restart it forced.
        assert len(reconcile_open_playback(verdict)) == 1
        assert _reason_of(verdict, "RISO") == "media_lane_isolated"
    finally:
        verdict.close()


def test_isolation_completes_even_when_its_own_durable_row_cannot_be_appended(
    tmp_path: Path,
) -> None:
    """Record-keeping failure must never leave the lane un-isolated."""
    db_path = tmp_path / "lane-isolated-append-fault.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RISOF", 0)] = hold
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    original_emit = emit_event

    def _faulted_emit(*args: object, **kwargs: object) -> object:
        if kwargs.get("type") == "surface.playback_lane_isolated":
            msg = "injected lane isolation append fault"
            raise RuntimeError(msg)
        return original_emit(*cast("Any", args), **cast("Any", kwargs))

    try:
        with (
            _CallbackPump(player),
            patch.object(player, "settle_interrupted_generation", return_value=None),
            patch.object(voice_media, "emit_event", side_effect=_faulted_emit),
        ):
            rows = _emit_response(
                conn,
                response_id="RISOF",
                group_id="GISOF",
                turn_id="TISOF",
                text="the ledger fails too",
            )
            asyncio.run(_submit_response(pipeline, rows))
            _await_playback_started(conn, "RISOF")
            assert pipeline.stop_foreground_output("RISOF") != "applied"
            # The lane is isolated: nothing further is admitted, and the
            # injected append failure did not escape the actor.
            successor = _emit_response(
                conn,
                response_id="RISOF-NEXT",
                group_id="GISOF-NEXT",
                turn_id="TISOF-NEXT",
                text="never spoken",
            )
            outcomes = asyncio.run(_submit_response(pipeline, successor))
            assert [outcome.status for outcome in outcomes] == ["closed"] * 3
    finally:
        hold.set()
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()

    verdict = open_event_log(db_path)
    try:
        assert _isolation_rows(verdict) == []
        assert _playback_rows_for(verdict, "RISOF-NEXT", "surface.playback_started") == 0

        # The lane really was isolated, but the log holds no evidence of it.
        # The reconciler reads what is written, so this orphan is a restart.
        assert len(reconcile_open_playback(verdict)) == 1
        assert _reason_of(verdict, "RISOF") == "daemon_restart"
    finally:
        verdict.close()


def _count_rows(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _reason_of(conn: sqlite3.Connection, response_id: str) -> str:
    """The ``reason`` on the single ``surface.playback_interrupted`` row."""
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'surface.playback_interrupted' "
        "AND json_extract(payload_json, '$.response_id') = ?",
        (response_id,),
    ).fetchall()
    assert len(rows) == 1
    return str(cast("dict[str, object]", json.loads(str(rows[0][0])))["reason"])


def _playback_rows_for(conn: sqlite3.Connection, response_id: str, kind: str) -> int:
    return _count_rows(
        conn,
        "SELECT count(*) FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.response_id') = ?",
        kind,
        response_id,
    )


def _await_playback_started(conn: sqlite3.Connection, response_id: str) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _playback_rows_for(conn, response_id, "surface.playback_started") == 1:
            return
        time.sleep(0.005)
    pytest.fail(f"{response_id} never reached surface.playback_started")


def test_stop_foreground_output_interrupts_only_the_named_response(
    tmp_path: Path,
) -> None:
    """H2/H3: the stop commits one user_stop terminal for exactly its target."""
    db_path = tmp_path / "stop-speaking.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RSTOP", 0)] = hold
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            speaking = _emit_response(
                conn,
                response_id="RSTOP",
                group_id="GSTOP",
                turn_id="TSTOP",
                text="这一句正在朗读。",
            )
            asyncio.run(_submit_response(pipeline, speaking))
            _await_playback_started(conn, "RSTOP")

            # H3: an id that is not the active response changes nothing.
            # The count is quiescent at 2 (segment_prepared + started): the
            # lone segment is held at its final gate, so no further
            # segment_prepared and no checkpoint can land mid-window.
            playback_before = _count_rows(
                conn,
                "SELECT count(*) FROM events WHERE type LIKE 'surface.playback_%'",
            )
            assert pipeline.stop_foreground_output("RSTOP-NOT-ACTIVE") == "stale"
            playback_after = _count_rows(
                conn,
                "SELECT count(*) FROM events WHERE type LIKE 'surface.playback_%'",
            )
            assert (playback_before, playback_after) == (2, 2)

            # H2: the named target stops.
            assert pipeline.stop_foreground_output("RSTOP") == "applied"

            # A later chunk of the same response cannot restart it.
            late = emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": "TSTOP",
                    "text": "被丢弃的续写。",
                    "response_id": "RSTOP",
                    "response_group_id": "GSTOP",
                    "sequence": 1,
                    "phase": "final",
                    "channel": "speech",
                    "segment_hash": hashlib.sha256(b"late").hexdigest(),
                },
            )
            late_row = conn.execute(
                "SELECT id FROM events WHERE event_uid = ?",
                (late.event_uid,),
            ).fetchone()
            assert late_row is not None
            asyncio.run(
                pipeline.submit_event(row_id=int(late_row[0]), event=late, origin="direct"),
            )
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        hold.set()
        assert pipeline.close()
        conn.close()

    verdict = open_event_log(db_path)
    try:
        interrupted = verdict.execute(
            "SELECT count(*) FROM events WHERE type = 'surface.playback_interrupted' "
            "AND json_extract(payload_json, '$.response_id') = 'RSTOP' "
            "AND json_extract(payload_json, '$.reason') = 'user_stop'",
        ).fetchone()
        assert interrupted is not None
        assert int(interrupted[0]) == 1
        assert _playback_rows_for(verdict, "RSTOP", "surface.playback_completed") == 0
        assert _playback_rows_for(verdict, "RSTOP", "surface.playback_started") == 1
    finally:
        verdict.close()


def test_stop_foreground_output_drops_the_queued_sibling_without_a_row(
    tmp_path: Path,
) -> None:
    """H5: stopping A silences queued B, and nothing durable explains B."""
    db_path = tmp_path / "stop-speaking-queued.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RA", 0)] = hold
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            first = _emit_response(
                conn,
                response_id="RA",
                group_id="GQ",
                turn_id="TQ",
                text="第一个回答。",
                phase="commentary",
            )
            asyncio.run(_submit_response(pipeline, first))
            _await_playback_started(conn, "RA")
            queued = _emit_response(
                conn,
                response_id="RB",
                group_id="GQ",
                turn_id="TQ",
                text="排队中的回答。",
            )
            asyncio.run(_submit_response(pipeline, queued))
            assert _playback_rows_for(conn, "RB", "surface.playback_started") == 0

            assert pipeline.stop_foreground_output("RA") == "applied"
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        hold.set()
        assert pipeline.close()
        conn.close()

    verdict = open_event_log(db_path)
    try:
        assert _playback_rows_for(verdict, "RA", "surface.playback_interrupted") == 1
        # B never speaks, and -- the disclosed limitation of this card -- no
        # durable row anywhere accounts for its silence. A future card that
        # gives the purge a terminal must change this second count
        # deliberately.
        started_b = _playback_rows_for(verdict, "RB", "surface.playback_started")
        any_playback_b = _count_rows(
            verdict,
            "SELECT count(*) FROM events WHERE type LIKE 'surface.playback_%' "
            "AND json_extract(payload_json, '$.response_id') = 'RB'",
        )
        assert (started_b, any_playback_b) == (0, 0)
    finally:
        verdict.close()


def test_macos_say_process_ownership_timeout_and_terminal_payload(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """A timed-out say is killed before its queued successor starts."""
    db_path = tmp_path / "macos-say.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=0)
    player = _player()
    config = replace(
        _config(),
        enable_macos_say_fallback=True,
        response_timeout_s=0.05,
    )
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=config,
        start_player=False,
    )
    actions: list[str] = []
    process_count = 0

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeSayProcess:
        nonlocal process_count
        process_count += 1
        names = ("old", "next", "error", "after-error")
        name = names[process_count - 1]
        actions.append(f"spawn:{name}")
        return _FakeSayProcess(
            name,
            actions,
            complete_immediately=name in {"next", "after-error"},
            wait_error_once=name == "error",
        )

    try:
        with patch(
            "jarvis.surface.voice_media.asyncio.create_subprocess_exec",
            side_effect=_spawn,
        ):
            old = _emit_response(
                conn,
                response_id="RSAY-OLD",
                group_id="GSAY",
                turn_id="TSAY-OLD",
                text="old say",
            )
            asyncio.run(_submit_response(pipeline, old))
            successor = _emit_response(
                conn,
                response_id="RSAY-NEXT",
                group_id="GSAY",
                turn_id="TSAY-NEXT",
                text="next say",
            )
            asyncio.run(_submit_response(pipeline, successor))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            error = _emit_response(
                conn,
                response_id="RSAY-ERROR",
                group_id="GSAY-ERROR",
                turn_id="TSAY-ERROR",
                text="error say",
            )
            asyncio.run(_submit_response(pipeline, error))
            after_error = _emit_response(
                conn,
                response_id="RSAY-AFTER-ERROR",
                group_id="GSAY-ERROR",
                turn_id="TSAY-AFTER-ERROR",
                text="after error say",
            )
            asyncio.run(_submit_response(pipeline, after_error))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert actions.index("kill:old") < actions.index("spawn:next")
    assert "kill:error" in actions
    assert actions.index("kill:error") < actions.index("spawn:after-error")
    terminal_conn = open_event_log(db_path)
    try:
        terminals = {
            str(payload["response_id"]): (kind, payload)
            for kind, payload in _terminal_rows(terminal_conn)
        }
    finally:
        terminal_conn.close()
    old_kind, old_payload = terminals["RSAY-OLD"]
    next_kind, next_payload = terminals["RSAY-NEXT"]
    assert old_kind == "surface.playback_failed"
    assert old_payload["heard_through_sequence"] is None
    assert old_payload["provider"] == "macos_say"
    assert next_kind == "surface.playback_completed"
    assert next_payload["heard_through_sequence"] is None
    assert next_payload["provider"] == "macos_say"
    error_kind, error_payload = terminals["RSAY-ERROR"]
    assert error_kind == "surface.playback_failed"
    assert error_payload["heard_through_sequence"] is None
    assert error_payload["provider"] == "macos_say"


def test_macos_say_spawn_in_progress_cancellation_has_no_orphan(tmp_path: Path) -> None:
    """Foreground supersede cancels an in-flight say spawn before replacement."""
    db_path = tmp_path / "macos-say-spawn.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=0)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), enable_macos_say_fallback=True),
        start_player=False,
    )
    actions: list[str] = []
    old_spawn_entered = threading.Event()
    spawn_count = 0

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeSayProcess:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            actions.append("spawn-enter:old")
            old_spawn_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                actions.append("spawn-cancelled:old")
                raise
        actions.append("spawn:new")
        return _FakeSayProcess("new", actions, complete_immediately=True)

    try:
        with patch(
            "jarvis.surface.voice_media.asyncio.create_subprocess_exec",
            side_effect=_spawn,
        ):
            old = _emit_response(
                conn,
                response_id="RSPAWN-OLD",
                group_id="GSPAWN-OLD",
                turn_id="TSPAWN-OLD",
                text="old spawn",
            )
            asyncio.run(_submit_response(pipeline, old))
            assert old_spawn_entered.wait(timeout=1.0)
            new = _emit_response(
                conn,
                response_id="RSPAWN-NEW",
                group_id="GSPAWN-NEW",
                turn_id="TSPAWN-NEW",
                text="new spawn",
            )
            asyncio.run(_submit_response(pipeline, new))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert actions.index("spawn-cancelled:old") < actions.index("spawn:new")
    terminal_conn = open_event_log(db_path)
    try:
        old_payload = next(
            payload
            for kind, payload in _terminal_rows(terminal_conn)
            if kind == "surface.playback_interrupted"
            and payload["response_id"] == "RSPAWN-OLD"
        )
    finally:
        terminal_conn.close()
    assert old_payload["heard_through_sequence"] is None
    assert old_payload["provider"] == "macos_say"


def test_macos_say_late_spawn_debt_blocks_successor_until_reaped(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """A spawn ignoring its first cancellation remains actor-owned lane debt."""
    db_path = tmp_path / "macos-say-late-spawn.db"
    conn = open_event_log(db_path)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(candidate_count=0),
        player=_player(),
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), enable_macos_say_fallback=True),
        start_player=False,
    )
    actions: list[str] = []
    old_spawn_entered = threading.Event()
    allow_late_return = threading.Event()
    spawned = 0

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeSayProcess:
        nonlocal spawned
        spawned += 1
        if spawned == 1:
            actions.append("spawn-enter:old")
            old_spawn_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                actions.append("spawn-cancel-ignored:old")
                while not allow_late_return.is_set():  # noqa: ASYNC110
                    await asyncio.sleep(0.001)
            actions.append("spawn-return:old")
            return _FakeSayProcess("old", actions, complete_immediately=False)
        actions.append("spawn:new")
        return _FakeSayProcess("new", actions, complete_immediately=True)

    successor_error: list[BaseException] = []

    def _submit_successor(rows: list[tuple[int, Event]]) -> None:
        try:
            asyncio.run(_submit_response(pipeline, rows))
        except BaseException as exc:  # noqa: BLE001 - surface thread assertion
            successor_error.append(exc)

    successor_thread: threading.Thread | None = None
    try:
        with patch(
            "jarvis.surface.voice_media.asyncio.create_subprocess_exec",
            side_effect=_spawn,
        ):
            old = _emit_response(
                conn,
                response_id="RLATE-SPAWN-OLD",
                group_id="GLATE-SPAWN-OLD",
                turn_id="TLATE-SPAWN-OLD",
                text="old late spawn",
            )
            asyncio.run(_submit_response(pipeline, old))
            assert old_spawn_entered.wait(timeout=1.0)
            successor = _emit_response(
                conn,
                response_id="RLATE-SPAWN-NEW",
                group_id="GLATE-SPAWN-NEW",
                turn_id="TLATE-SPAWN-NEW",
                text="new spawn",
            )
            successor_thread = threading.Thread(
                target=_submit_successor,
                args=(successor,),
                name="late-say-successor-submit",
            )
            successor_thread.start()
            time.sleep(0.32)
            assert "spawn:new" not in actions
            assert successor_thread.is_alive()
            allow_late_return.set()
            successor_thread.join(timeout=2.0)
            assert not successor_thread.is_alive()
            assert not successor_error
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        allow_late_return.set()
        if successor_thread is not None:
            successor_thread.join(timeout=2.0)
        assert pipeline.close()
        conn.close()
    assert actions.index("spawn-return:old") < actions.index("kill:old")
    assert actions.index("kill:old") < actions.index("reaped:old")
    assert actions.index("reaped:old") < actions.index("spawn:new")
    assert not pipeline._fallback_janitors  # noqa: SLF001


def test_media_owner_startup_failures_and_bounded_shutdown(  # noqa: C901, PLR0915
    tmp_path: Path,
) -> None:
    """Startup, full-queue, provider-stop, and device-stop failures stay bounded."""
    db_path = tmp_path / "owner-bounds.db"
    baseline = len(_live_media_owner_threads())
    with pytest.raises(RuntimeError, match="failed to start"):
        voice_media.StreamingTTSPipeline(
            provider=_FakeProvider(candidate_count=1),
            player=_player(),
            conn_factory=lambda: (_ for _ in ()).throw(OSError("conn failed")),
            boot_high_water_id=0,
            config=replace(_config(), shutdown_timeout_s=0.2),
        )
    assert len(_live_media_owner_threads()) == baseline

    player_start_failure = _player()
    with (
        patch.object(player_start_failure, "start", side_effect=OSError("device failed")),
        pytest.raises(RuntimeError, match="failed to start"),
    ):
        voice_media.StreamingTTSPipeline(
            provider=_FakeProvider(candidate_count=1),
            player=player_start_failure,
            conn_factory=lambda: open_event_log(db_path),
            boot_high_water_id=0,
            config=replace(_config(), shutdown_timeout_s=0.2),
        )
    assert len(_live_media_owner_threads()) == baseline

    queue_conn = open_event_log(db_path)
    queue_rows = _emit_response(
        queue_conn,
        response_id="RQUEUE",
        group_id="GQUEUE",
        turn_id="TQUEUE",
        text="queue",
    )
    queue_pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(candidate_count=1),
        player=_player(),
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(
            _config(),
            command_queue_capacity=1,
            shutdown_timeout_s=0.2,
        ),
        start_player=False,
    )
    command_entered = threading.Event()
    command_release = threading.Event()
    original_handle = queue_pipeline._handle_command  # noqa: SLF001

    async def _blocked_handle(
        command: voice_media._MediaCommand,
    ) -> voice_media.MediaSubmitOutcome:
        command_entered.set()
        while not command_release.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.001)
        return await original_handle(command)

    submitters: list[threading.Thread] = []
    with patch.object(queue_pipeline, "_handle_command", side_effect=_blocked_handle):
        for row_id, event in queue_rows[:2]:
            submitter = threading.Thread(
                target=asyncio.run,
                args=(queue_pipeline.submit_event(row_id=row_id, event=event),),
            )
            submitter.start()
            submitters.append(submitter)
            if len(submitters) == 1:
                assert command_entered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            queue = queue_pipeline._queue  # noqa: SLF001
            if queue is not None and queue.full():
                break
            time.sleep(0.001)
        else:
            pytest.fail("normal command queue never reached capacity")
        queue_pipeline.request_close()
        command_release.set()
        started = time.monotonic()
        assert queue_pipeline.close(wait_timeout_s=0.2)
        assert time.monotonic() - started < 0.2
    for submitter in submitters:
        submitter.join(timeout=1.0)
        assert not submitter.is_alive()
    queue_conn.close()

    provider_gate = threading.Event()
    stuck_provider = _FakeProvider(candidate_count=1)

    def _stuck_provider_close() -> None:
        provider_gate.wait()

    stuck_provider.request_close = _stuck_provider_close  # type: ignore[method-assign]
    provider_pipeline = voice_media.StreamingTTSPipeline(
        provider=stuck_provider,
        player=_player(),
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), shutdown_timeout_s=0.12),
        start_player=False,
    )
    started = time.monotonic()
    assert not provider_pipeline.close(wait_timeout_s=0.12)
    assert time.monotonic() - started < 0.16
    assert not provider_pipeline.cleanup_complete
    provider_gate.set()
    deadline = time.monotonic() + 1.0
    while not provider_pipeline.cleanup_complete and time.monotonic() < deadline:
        time.sleep(0.002)
    assert provider_pipeline.close(wait_timeout_s=0.01)

    player_gate = threading.Event()
    stuck_player = _player()
    player_pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(candidate_count=1),
        player=stuck_player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), shutdown_timeout_s=0.12),
        start_player=False,
    )
    with patch.object(stuck_player, "stop", side_effect=player_gate.wait):
        started = time.monotonic()
        assert not player_pipeline.close(wait_timeout_s=0.12)
        assert time.monotonic() - started < 0.16
        assert not player_pipeline.cleanup_complete
        player_gate.set()
        deadline = time.monotonic() + 1.0
        while not player_pipeline.cleanup_complete and time.monotonic() < deadline:
            time.sleep(0.002)
        assert player_pipeline.close(wait_timeout_s=0.01)
    assert len(_live_media_owner_threads()) == baseline


def test_media_owner_stuck_startup_is_daemon_isolated_and_late_closed(
    tmp_path: Path,
) -> None:
    """Stuck conn/player startup returns once and closes every late resource."""
    db_path = tmp_path / "stuck-startup.db"
    baseline = len(_live_media_owner_threads())
    conn_gate = threading.Event()
    late_connections: list[sqlite3.Connection] = []

    def _stuck_conn() -> sqlite3.Connection:
        conn_gate.wait()
        connection = open_event_log(db_path)
        late_connections.append(connection)
        return connection

    started = time.monotonic()
    with pytest.raises(voice_media.StreamingMediaStartupError) as conn_error:
        voice_media.StreamingTTSPipeline(
            provider=_FakeProvider(candidate_count=1),
            player=_player(),
            conn_factory=_stuck_conn,
            boot_high_water_id=0,
            config=replace(_config(), shutdown_timeout_s=0.06),
            start_player=False,
        )
    assert time.monotonic() - started < 0.1
    assert conn_error.value.phase == "opening_connection"
    assert not conn_error.value.device_state_uncertain
    live_after_conn_timeout = _live_media_owner_threads()
    assert live_after_conn_timeout
    assert all(thread.daemon for thread in live_after_conn_timeout)
    conn_gate.set()
    deadline = time.monotonic() + 1.0
    while len(_live_media_owner_threads()) != baseline and time.monotonic() < deadline:
        time.sleep(0.002)
    assert len(_live_media_owner_threads()) == baseline
    assert len(late_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        late_connections[0].execute("SELECT 1")

    player_gate = threading.Event()
    player_stopped = threading.Event()
    stuck_player = _player()

    def _stuck_start() -> None:
        player_gate.wait()

    def _late_stop() -> None:
        player_stopped.set()

    with (
        patch.object(stuck_player, "start", side_effect=_stuck_start),
        patch.object(stuck_player, "stop", side_effect=_late_stop),
    ):
        started = time.monotonic()
        with pytest.raises(voice_media.StreamingMediaStartupError) as player_error:
            voice_media.StreamingTTSPipeline(
                provider=_FakeProvider(candidate_count=1),
                player=stuck_player,
                conn_factory=lambda: open_event_log(db_path),
                boot_high_water_id=0,
                config=replace(_config(), shutdown_timeout_s=0.06),
            )
        assert time.monotonic() - started < 0.1
        assert player_error.value.phase == "starting_player"
        assert player_error.value.device_state_uncertain
        assert all(thread.daemon for thread in _live_media_owner_threads())
        player_gate.set()
        assert player_stopped.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while len(_live_media_owner_threads()) != baseline and time.monotonic() < deadline:
            time.sleep(0.002)
    assert len(_live_media_owner_threads()) == baseline


def test_shutdown_deadline_is_concurrent_monotonic_min(tmp_path: Path) -> None:
    """A later long request cannot overwrite an earlier shutdown deadline."""
    db_path = tmp_path / "deadline-min.db"
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_FakeProvider(candidate_count=1),
        player=_player(),
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), shutdown_timeout_s=0.5),
        start_player=False,
    )
    publish = threading.Barrier(3)

    def _publish(timeout_s: float) -> None:
        publish.wait()
        pipeline._request_shutdown_deadline(timeout_s)  # noqa: SLF001

    long_thread = threading.Thread(target=_publish, args=(0.4,), name="deadline-long")
    short_thread = threading.Thread(target=_publish, args=(0.05,), name="deadline-short")
    long_thread.start()
    short_thread.start()
    started = time.monotonic()
    publish.wait()
    long_thread.join(timeout=1.0)
    short_thread.join(timeout=1.0)
    assert not long_thread.is_alive()
    assert not short_thread.is_alive()
    with pipeline._deadline_lock:  # noqa: SLF001
        chosen = pipeline._shutdown_deadline  # noqa: SLF001
    assert chosen <= started + 0.08
    close_started = time.monotonic()
    assert pipeline.close(wait_timeout_s=0.4)
    assert time.monotonic() - close_started < 0.1


class _FakeOutputStream:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        latency: float | None = None,
    ) -> None:
        self.active = True
        self._start_error = start_error
        if latency is not None:
            self.latency = latency

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


class _FakeSayProcess:
    def __init__(
        self,
        name: str,
        actions: list[str],
        *,
        complete_immediately: bool,
        wait_error_once: bool = False,
    ) -> None:
        self.name = name
        self.actions = actions
        self.returncode: int | None = 0 if complete_immediately else None
        self._wait_error_once = wait_error_once
        self._done = asyncio.Event()
        if complete_immediately:
            self._done.set()

    async def wait(self) -> int:
        if self._wait_error_once:
            self._wait_error_once = False
            msg = f"injected wait failure: {self.name}"
            raise OSError(msg)
        await self._done.wait()
        assert self.returncode is not None
        self.actions.append(f"reaped:{self.name}")
        return self.returncode

    def terminate(self) -> None:
        self.actions.append(f"terminate:{self.name}")

    def kill(self) -> None:
        self.actions.append(f"kill:{self.name}")
        self.returncode = -9
        self._done.set()


def _live_media_owner_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "jarvis-media-owner" and thread.is_alive()
    ]


class _FakeWebSocket:
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


def test_minimax_session_single_reader_writer_backpressure_and_watchdog() -> None:
    """Real session owns recv/send, bounds events, and ignores active-feed idleness."""

    async def _body() -> None:
        ws = _FakeWebSocket()

        async def _connect(
            _url: str,
            *,
            additional_headers: dict[str, str],
        ) -> _FakeWebSocket:
            assert additional_headers["Authorization"] == "Bearer key"
            return ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = voice_tts.MiniMaxTTSSession(
                api_key="key",
                endpoint="https://example.test",
                voice="voice",
                model="model",
                volume=5,
                sample_rate_hz=8_000,
                connect_timeout_s=0.2,
                first_chunk_timeout_s=0.2,
                between_chunk_timeout_s=0.2,
                idle_close_s=0.01,
                command_queue_capacity=1,
                audio_queue_capacity=1,
            )
            await session.open("R", 1)
            segment = voice_tts.TTSResponseSegment("R", 1, 0, "text")
            await session.send(segment)
            with pytest.raises(voice_tts.TTSConcurrentSendError):
                await session.send(voice_tts.TTSResponseSegment("R", 1, 1, "overlap"))
            iterator = session.audio_events().__aiter__()
            first_event: asyncio.Future[voice_tts.TTSAudioEvent] = asyncio.ensure_future(
                iterator.__anext__()
            )
            await asyncio.sleep(0.03)
            assert not ws.closed, "active reader feed must suppress idle watchdog"
            second_reader = session.audio_events().__aiter__()
            with pytest.raises(RuntimeError, match="exactly one reader"):
                await second_reader.__anext__()
            ws.audio_gate.set()
            assert isinstance(await first_event, voice_tts.TTSAudioChunk)
            assert isinstance(await iterator.__anext__(), voice_tts.TTSSegmentFinished)
            await asyncio.sleep(0)
            await session.finish()
        assert ws.max_recv_in_flight == 1
        assert ws.max_send_in_flight == 1
        assert [payload["event"] for payload in ws.sent] == [
            "task_start",
            "task_continue",
            "task_finish",
        ]

        idle_ws = _FakeWebSocket()

        async def _idle_connect(
            _url: str,
            *,
            additional_headers: dict[str, str],
        ) -> _FakeWebSocket:
            del additional_headers
            return idle_ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_idle_connect):
            idle_session = voice_tts.MiniMaxTTSSession(
                api_key="key",
                endpoint="https://example.test",
                voice="voice",
                model="model",
                volume=5,
                sample_rate_hz=8_000,
                connect_timeout_s=0.2,
                first_chunk_timeout_s=0.2,
                between_chunk_timeout_s=0.2,
                idle_close_s=0.02,
                command_queue_capacity=1,
                audio_queue_capacity=1,
            )
            await idle_session.open("IDLE", 2)
            with pytest.raises(voice_tts.TTSSessionClosedError, match="idle"):
                await asyncio.wait_for(
                    idle_session.audio_events().__anext__(),
                    timeout=0.2,
                )
            await idle_session.close()
            assert idle_ws.closed

    asyncio.run(_body())


def test_streaming_rollout_default_off_and_production_builder_gate(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shipped config is off; valid flags select the real persistent actor."""
    config = yaml.safe_load(Path("config/jarvis.yaml").read_text())
    assert config["realtime"]["enabled"] is False
    assert config["realtime"]["streaming_output"]["enabled"] is False
    db_path = tmp_path / "builder.db"
    conn = open_event_log(db_path)
    runtime = SimpleNamespace(
        config={
            "realtime": {
                "enabled": True,
                "streaming_output": {"enabled": True},
            },
        },
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        runtime_paths=SimpleNamespace(event_log=db_path),
        conn=conn,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")
    with patch.object(
        voice_tts,
        "_open_output_stream",
        return_value=_FakeOutputStream(),
    ):
        pipeline = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert isinstance(pipeline, voice_media.StreamingTTSPipeline)
        assert pipeline.close()
        runtime.config = {"realtime": {"enabled": False}}
        legacy = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert isinstance(legacy, voice_tts.TTSPipeline)
        assert legacy.close()
        runtime.config = {
            "realtime": {
                "enabled": True,
                "streaming_output": {
                    "enabled": True,
                    "command_queue_capacity": 0,
                },
            },
        }
        invalid_config_fallback = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert isinstance(invalid_config_fallback, voice_tts.TTSPipeline)
        assert invalid_config_fallback.close()
    runtime.config = {
        "realtime": {
            "enabled": True,
            "streaming_output": {"enabled": True},
        },
    }
    startup_error = OSError("injected PortAudio start failure")
    failed_stream = _FakeOutputStream(start_error=startup_error)
    legacy_stream = _FakeOutputStream()
    streaming_provider = _FakeProvider(candidate_count=1)
    legacy_provider = _FakeProvider(candidate_count=1)
    with (
        patch.object(
            voice_tts,
            "_open_output_stream",
            side_effect=[failed_stream, legacy_stream],
        ),
        patch.object(
            voice_tts,
            "MiniMaxWSClient",
            side_effect=[streaming_provider, legacy_provider],
        ),
    ):
        startup_fallback = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert isinstance(startup_fallback, voice_tts.TTSPipeline)
        assert cast("object", startup_fallback._provider) is legacy_provider  # noqa: SLF001
        assert startup_fallback.close()
    assert not failed_stream.active
    assert not legacy_stream.active
    first_failure = _FakeOutputStream(start_error=startup_error)
    second_failure = _FakeOutputStream(start_error=startup_error)
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=[
            first_failure,
            second_failure,
        ],
    ):
        text_only = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert text_only is None
    assert not first_failure.active
    assert not second_failure.active

    runtime.config["realtime"]["streaming_output"]["shutdown_timeout_s"] = 0.06
    stuck_player = _player()
    player_start_gate = threading.Event()
    late_player_stopped = threading.Event()

    def _stuck_player_start() -> None:
        player_start_gate.wait()

    with (
        patch.object(
            voice_tts,
            "AudioStreamPlayer",
            return_value=stuck_player,
        ) as player_factory,
        patch.object(stuck_player, "start", side_effect=_stuck_player_start),
        patch.object(stuck_player, "stop", side_effect=late_player_stopped.set),
        patch.object(
            voice_tts,
            "MiniMaxWSClient",
            return_value=_FakeProvider(candidate_count=1),
        ) as provider_factory,
    ):
        started = time.monotonic()
        startup_timeout_text_only = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert startup_timeout_text_only is None
        assert time.monotonic() - started < 0.1
        assert player_factory.call_count == 1
        assert provider_factory.call_count == 1
        assert all(thread.daemon for thread in _live_media_owner_threads())
        player_start_gate.set()
        assert late_player_stopped.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while _live_media_owner_threads() and time.monotonic() < deadline:
            time.sleep(0.002)
    assert not _live_media_owner_threads()
    conn.close()


def test_production_builder_falls_back_only_after_typed_closed_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertain Wave-2 output debt forbids an independent legacy owner."""
    db_path = tmp_path / "typed-output-startup.db"
    conn = open_event_log(db_path)
    runtime = SimpleNamespace(
        config={
            "realtime": {
                "enabled": True,
                "streaming_output": {"enabled": True},
            },
        },
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        runtime_paths=SimpleNamespace(event_log=db_path),
        conn=conn,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")
    provider = MagicMock()
    provider.streaming_candidate_count = 1

    uncertain_player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    uncertain_player.start.return_value = voice_tts.PlayerStartResult(
        "uncertain",
        11,
        "injected_open_debt",
    )
    uncertain_player.stop.return_value = voice_tts.PlayerStopResult(
        "uncertain",
        11,
        "injected_open_debt",
    )
    with (
        patch.object(voice_tts, "AudioStreamPlayer", return_value=uncertain_player) as factory,
        patch.object(voice_tts, "MiniMaxWSClient", return_value=provider),
    ):
        assert (
            inherent_loop._build_tts_pipeline(  # noqa: SLF001
                cast("Any", runtime),
                cast("Any", SimpleNamespace()),
            )
            is None
        )
    assert factory.call_count == 1
    assert uncertain_player.start.call_count == 1

    failed_closed_player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    failed_closed_player.start.return_value = voice_tts.PlayerStartResult(
        "failed_closed",
        21,
        "injected_closed_open_failure",
    )
    failed_closed_player.stop.return_value = voice_tts.PlayerStopResult(
        "already_closed",
        21,
        "already_closed",
    )
    legacy_player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    legacy_player.stop.return_value = voice_tts.PlayerStopResult(
        "closed",
        22,
        "closed",
    )
    with (
        patch.object(
            voice_tts,
            "AudioStreamPlayer",
            side_effect=[failed_closed_player, legacy_player],
        ) as factory,
        patch.object(voice_tts, "MiniMaxWSClient", return_value=provider),
    ):
        fallback = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
    assert isinstance(fallback, voice_tts.TTSPipeline)
    assert factory.call_count == 2
    assert fallback.close()
    conn.close()


def test_voice_bench_provenance_fails_closed_before_provider_use(tmp_path: Path) -> None:
    """Revision/config provenance debt makes the software gate ineligible/nonzero."""
    output = tmp_path / "ineligible.json"
    config_copy = tmp_path / "jarvis.yaml"
    config_copy.write_text(Path("config/jarvis.yaml").read_text())
    env = dict(os.environ)
    env.pop("MINIMAX_API_KEY", None)
    result = subprocess.run(  # noqa: S603 - fixed local script under test
        [
            sys.executable,
            "scripts/bench_voice_streaming_output.py",
            "--runs",
            "1",
            "--expected-revision",
            "0" * 40,
            "--config",
            str(config_copy),
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert result.returncode == 2
    payload = json.loads(output.read_text())
    assert payload["raw_runs"] == []
    assert payload["summary"]["software_streaming_output_gate"] == "INELIGIBLE"
    assert payload["summary"]["physical_dac_loopback_gate"] == "UNMEASURED"
    assert payload["summary"]["true_end_to_end_latency_gate"] == "UNMEASURED"
    reasons = payload["provenance"]["software_gate_ineligibility_reasons"]
    assert "git_revision_mismatch" in reasons
    assert "effective_config_source_mismatch" in reasons


def test_bounded_smoke_pass_artifact_marks_ab_gate_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing bounded device smoke never masquerades as legacy-vs-streaming A/B."""
    output = tmp_path / "bounded-smoke-pass.json"

    def _eligible_provenance(
        *,
        config_path: Path,
        text: str,
        expected_revision: str,
        media_config: voice_media.StreamingMediaConfig,
    ) -> tuple[dict[str, object], list[str]]:
        del config_path, text, expected_revision, media_config
        return {"software_gate_eligible": True}, []

    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")
    monkeypatch.setattr(voice_bench, "_provenance", _eligible_provenance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_voice_streaming_output.py",
            "--runs",
            "1",
            "--bounded-smoke-only",
            "--expected-revision",
            "integration-revision",
            "--output",
            str(output),
        ],
    )
    with patch.object(
        voice_bench,
        "_bounded_real_streaming_smoke",
        return_value={"status": "PASS", "failure": None},
    ):
        assert voice_bench.main() == 0
    payload = json.loads(output.read_text())
    assert payload["bounded_real_streaming_smoke"]["status"] == "PASS"
    assert payload["summary"]["bounded_real_streaming_smoke_gate"] == "PASS"
    assert payload["summary"]["software_streaming_output_gate"] == "NOT_RUN"
    assert payload["summary"]["physical_dac_loopback_gate"] == "UNMEASURED"
    assert payload["summary"]["true_end_to_end_latency_gate"] == "UNMEASURED"


def test_bounded_smoke_device_open_failure_still_writes_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device construction errors remain structured artifacts, not tracebacks."""
    output = tmp_path / "device-open-failure.json"

    def _eligible_provenance(
        *,
        config_path: Path,
        text: str,
        expected_revision: str,
        media_config: voice_media.StreamingMediaConfig,
    ) -> tuple[dict[str, object], list[str]]:
        del config_path, text, expected_revision, media_config
        return {"software_gate_eligible": True}, []

    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")
    monkeypatch.setattr(voice_bench, "_provenance", _eligible_provenance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_voice_streaming_output.py",
            "--runs",
            "1",
            "--bounded-smoke-only",
            "--expected-revision",
            "integration-revision",
            "--output",
            str(output),
        ],
    )
    with (
        patch.object(
            voice_bench,
            "AudioStreamPlayer",
            side_effect=OSError("injected device open failure"),
        ),
        patch.object(voice_bench, "MiniMaxWSClient") as provider_factory,
    ):
        assert voice_bench.main() == 1
    provider_factory.assert_not_called()
    payload = json.loads(output.read_text())
    smoke = payload["bounded_real_streaming_smoke"]
    assert smoke["status"] == "FAIL"
    assert smoke["failure"].startswith("device_open_failed:OSError:")
    assert smoke["silent_callback_baseline"] is None
    for name in (
        "player_state_after_accept",
        "player_state_at_callback_deadline",
        "player_state_before_cleanup",
        "player_state_after_cleanup",
    ):
        assert smoke[name] == {
            "sampled": False,
            "is_running": None,
            "callback_calls": None,
            "underflow_count": None,
            "bytes_pending": None,
        }
    assert payload["summary"]["software_streaming_output_gate"] == "NOT_RUN"
    assert payload["summary"]["bounded_real_streaming_smoke_gate"] == "FAIL"


def test_bounded_smoke_persists_player_counters_after_accept_and_deadline() -> None:
    """PASS and FAIL artifacts retain measured player state at both boundaries."""
    class _BenchPlayer:
        def __init__(self) -> None:
            self.is_running = True
            self.callback_calls = 11
            self.underflow_count = 2
            self._pending = 0

        def activate_generation(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(playback_generation_id=7)

        def begin_generation_segment(self, **_kwargs: object) -> None:
            return

        def write_generation(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            self._pending = 512
            return SimpleNamespace(sample_count=128)

        def poll_presentation(self) -> None:
            self.callback_calls = 12
            self._pending = 0

        def bytes_pending(self) -> int:
            return self._pending

        def stop(self) -> None:
            self.is_running = False

    async def _pcm(
        *,
        provider: voice_tts.MiniMaxWSClient,
        text: str,
        playback_generation_id: int,
    ) -> tuple[voice_tts.TTSAudioChunk, int, int]:
        del provider, text, playback_generation_id
        now = time.monotonic_ns()
        return voice_tts.TTSAudioChunk(0, np.ones(128, dtype="<i2").tobytes(), 48_000), now, now

    fake_player = _BenchPlayer()
    fake_provider = SimpleNamespace(request_close=lambda: None)
    clock = 0.0

    def _monotonic() -> float:
        nonlocal clock
        clock += 0.1
        return clock

    trace_point = SimpleNamespace(
        name="audio_output_first_nonzero_callback",
        monotonic_ns=time.monotonic_ns(),
    )
    with (
        patch.object(voice_bench, "AudioStreamPlayer", return_value=fake_player),
        patch.object(voice_bench, "MiniMaxWSClient", return_value=fake_provider),
        patch.object(voice_bench, "_first_streaming_pcm", side_effect=_pcm),
        patch.object(
            voice_bench,
            "_silent_callback_baseline",
            return_value={"callback_calls_delta": 11},
        ),
        patch.object(voice_bench, "realtime_trace_snapshot", return_value=(trace_point,)),
        patch.object(time, "monotonic", side_effect=_monotonic),
        patch.object(time, "sleep"),
    ):
        smoke = voice_bench._bounded_real_streaming_smoke(  # noqa: SLF001
            api_key="integration-placeholder",
            text="bounded smoke",
        )
    assert smoke["status"] == "PASS"
    assert smoke["player_state_after_accept"] == {
        "sampled": True,
        "is_running": True,
        "callback_calls": 11,
        "underflow_count": 2,
        "bytes_pending": 512,
    }
    assert smoke["player_state_at_callback_deadline"] == {
        "sampled": True,
        "is_running": True,
        "callback_calls": 12,
        "underflow_count": 2,
        "bytes_pending": 0,
    }
    after_cleanup = smoke["player_state_after_cleanup"]
    assert isinstance(after_cleanup, dict)
    assert after_cleanup["is_running"] is False


def test_segment_closed_before_audible_horizon_still_becomes_heard(tmp_path: Path) -> None:
    """Live ordering: SegmentFinished precedes the deferred presentation horizon."""
    db_path = tmp_path / "deferred-horizon.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    latency_s = 0.2
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=0.25,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=latency_s,
    )
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        rows = _emit_response(
            conn,
            response_id="RHEARD",
            group_id="GHEARD",
            turn_id="THEARD",
            text="第一句。",
        )
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, rows))
            deadline = time.monotonic() + 3.0
            # Network-paced generation closes the semantic boundary while the
            # presentation horizon is still deferred by the latency estimate.
            while time.monotonic() < deadline:
                if ("RHEARD", 0) in provider.provider_finals:
                    break
                time.sleep(0.001)
            else:
                pytest.fail("provider never finished the only segment")
            closed_at = time.monotonic()
            payload: dict[str, object] | None = None
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE type = 'surface.playback_checkpoint' ORDER BY id LIMIT 1",
                ).fetchone()
                if row is not None:
                    payload = json.loads(str(row[0]))
                    break
                time.sleep(0.002)
            heard_at = time.monotonic()
            assert payload is not None
            # A closed segment must not be claimed heard before the estimated
            # output latency has elapsed; only the horizon may promote it.
            assert heard_at - closed_at >= latency_s / 2
            assert payload["heard_through_sequence"] == 0
            assert payload["heard_text"] == "第一句。"
            assert payload["cursor_quality"] == "estimated"
            assert payload["submitted_samples"] == _Behavior().samples
    finally:
        assert pipeline.close()
        conn.close()


def test_escape_hatch_quality_survives_a_later_audible_report() -> None:
    """The first-observation branch preserves what `finish_segment` observed."""
    lease = GenerationLease(
        session_id="S",
        response_id="RHATCH",
        response_group_id="GHATCH",
        turn_id="THATCH",
        playback_generation_id=1,
        timeline_epoch=1,
    )
    ledger = PlaybackLedger(lease, sample_rate=8_000)
    ledger.begin_segment(sequence=0, text="已经听到的部分", segment_hash="hatch-0")
    ledger.accept_samples(sequence=0, sample_count=100)
    ledger.record_submitted(
        output_start_cursor=0,
        output_end_cursor=100,
        audibility_class="normal",
    )
    # The horizon crosses while the chunk is still open, so `record_audible`
    # skips it by its own `output_end_cursor is not None` guard and only
    # `finish_segment`'s escape hatch can write its quality — the ordering the
    # new first-observation branch must not disturb.
    ledger.record_audible(output_cursor=100, cursor_quality="estimated")
    ledger.finish_segment(sequence=0)
    assert ledger.snapshot().heard_through_sequence == 0
    # A later callback-report gap degrades the ledger cursor to an observed
    # `unknown`; the chunk keeps what the hatch observed for it.
    ledger.record_submitted(
        output_start_cursor=150,
        output_end_cursor=200,
        audibility_class="normal",
    )
    ledger.record_audible(output_cursor=100, cursor_quality="estimated")
    snapshot = ledger.snapshot()
    assert snapshot.cursor_quality == "estimated"
    assert snapshot.heard_through_sequence == 0
    assert snapshot.heard_text == "已经听到的部分"


def test_heard_prefix_quality_survives_an_earlier_report_gap(tmp_path: Path) -> None:
    """A report gap after a heard segment keeps the checkpoint quality real."""
    db_path = tmp_path / "heard-prefix-gap.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    # `_player()` estimates zero output latency, so the presentation horizon
    # advances inside the same poll that submits the samples: the escape hatch
    # in `finish_segment` would then fire and the first segment would already
    # be heard before the gap. A real latency estimate defers the horizon,
    # which is the live ordering this bug lives in.
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=0.25,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=0.2,
    )
    segment_samples = _Behavior().samples
    dropped = threading.Event()
    reports = player._callback_reports  # noqa: SLF001
    real_write = reports.write

    def _drop_second_segment_head(**report: Any) -> None:  # noqa: ANN401 - the ring's own kwargs
        # Exactly what the ring itself does when it is full: the report never
        # reaches the ledger, so the next one starts past `_submitted_cursor`.
        if not dropped.is_set() and report["output_start_cursor"] == segment_samples:
            dropped.set()
            return
        real_write(**report)

    reports.write = _drop_second_segment_head  # type: ignore[method-assign]
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    try:
        rows = _emit_response(
            conn,
            response_id="RGAP",
            group_id="GGAP",
            turn_id="TGAP",
            text=["heard prefix. ", "gapped tail."],
        )
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, rows))
            deadline = time.monotonic() + 3.0
            payload: dict[str, object] | None = None
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE type = 'surface.playback_checkpoint' ORDER BY id LIMIT 1",
                ).fetchone()
                if row is not None:
                    payload = json.loads(str(row[0]))
                    break
                time.sleep(0.005)
            assert dropped.is_set()
            assert payload is not None
            assert payload["heard_through_sequence"] == 0
            # The heard prefix is proven by an `estimated` report that lands
            # after the gap; the lease watermark stays an observed `unknown`.
            assert payload["heard_text"] == "heard prefix."
            assert payload["cursor_quality"] == "estimated"
    finally:
        assert pipeline.close()
        conn.close()


def test_realtime_output_device_reaches_both_builder_player_sites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`realtime.output_device` routes the player; absent keeps the system default.

    Both construction sites matter: the legacy fallback is not under
    `realtime.streaming_output` and must honour the same setting. The real
    player is still constructed; only `_open_output_stream` is faked, so no
    audio device is opened.
    """
    db_path = tmp_path / "device.db"
    conn = open_event_log(db_path)
    runtime = SimpleNamespace(
        config={},
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        runtime_paths=SimpleNamespace(event_log=db_path),
        conn=conn,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")
    seen: list[object] = []
    real_player = voice_tts.AudioStreamPlayer

    def _recording_player(**kwargs: object) -> voice_tts.AudioStreamPlayer:
        seen.append(kwargs.get("device"))
        return real_player(**cast("Any", kwargs))

    def _build(realtime: dict[str, object], expected: type) -> None:
        runtime.config = {"realtime": realtime}
        pipeline = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert pipeline is not None
        assert isinstance(pipeline, expected)
        assert pipeline.close()

    streaming = {"enabled": True, "streaming_output": {"enabled": True}}
    legacy = {"enabled": False}
    with (
        patch.object(voice_tts, "_open_output_stream", return_value=_FakeOutputStream()),
        patch.object(voice_tts, "AudioStreamPlayer", _recording_player),
    ):
        _build({**streaming, "output_device": "BlackHole 16ch"}, voice_media.StreamingTTSPipeline)
        _build({**legacy, "output_device": "BlackHole 16ch"}, voice_tts.TTSPipeline)
        assert seen == ["BlackHole 16ch", "BlackHole 16ch"]
        seen.clear()
        _build(dict(streaming), voice_media.StreamingTTSPipeline)
        _build(dict(legacy), voice_tts.TTSPipeline)
        assert seen == [None, None]
        seen.clear()
        # sounddevice also takes an integer index. Nothing here validates the
        # value, so a mistyped key cannot degrade to the system default.
        _build({**legacy, "output_device": 3}, voice_tts.TTSPipeline)
        assert seen == [3]
    conn.close()


def test_production_builder_puts_the_configured_request_volume_on_the_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emitted task_start carries vol 3 by default, the configured value otherwise."""
    db_path = tmp_path / "tts-volume.db"
    conn = open_event_log(db_path)
    runtime = SimpleNamespace(
        config={"realtime": {"enabled": False}},
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        runtime_paths=SimpleNamespace(event_log=db_path),
        conn=conn,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "integration-placeholder")

    def _emitted_vol(realtime_config: dict[str, object]) -> object:
        runtime.config = {"realtime": realtime_config}
        ws = _FakeWebSocket()

        async def _connect(
            _url: str,
            *,
            additional_headers: dict[str, str],
        ) -> _FakeWebSocket:
            del additional_headers
            return ws

        with patch.object(
            voice_tts,
            "_open_output_stream",
            return_value=_FakeOutputStream(),
        ):
            pipeline = inherent_loop._build_tts_pipeline(  # noqa: SLF001
                cast("Any", runtime),
                cast("Any", SimpleNamespace()),
            )
            assert isinstance(pipeline, voice_tts.TTSPipeline)
            provider = pipeline._provider  # noqa: SLF001

            async def _open_then_close() -> None:
                session = provider.create_tts_session(
                    endpoint_index=0,
                    idle_close_s=5.0,
                    command_queue_capacity=1,
                    audio_queue_capacity=1,
                )
                await session.open("RVOL", 1)
                await session.close()

            with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
                asyncio.run(_open_then_close())
            assert pipeline.close()

        assert ws.sent[0]["event"] == "task_start"
        return cast("dict[str, object]", ws.sent[0]["voice_setting"])["vol"]

    assert _emitted_vol({"enabled": False}) == 3
    assert _emitted_vol({"enabled": False, "tts_volume": 7}) == 7
    conn.close()


@pytest.mark.parametrize(
    ("reported_latency", "expected_ns"),
    [
        (0.035, 35_000_000),
        (0.0, 120_000_000),
        (None, 120_000_000),
        (1.5, 1_000_000_000),
        (12.0, 1_000_000_000),
    ],
    ids=[
        "host_reports_35ms",
        "degenerate_zero",
        "attribute_absent",
        "slow_device_clamped",
        "absurd_value_clamped",
    ],
)
def test_playback_started_carries_the_host_output_latency_or_the_configured_one(
    tmp_path: Path,
    reported_latency: float | None,
    expected_ns: int,
) -> None:
    """A degenerate 0.0 is not zero latency, and a real report is never shrunk.

    ``absurd_value`` deliberately supersedes the pin the previous card left
    here (``12.0 -> 120_000_000``).  This value gates ``record_audible``, so
    replacing a host report with a smaller number over-claims what was heard;
    above the ceiling it is clamped, not discarded.  ``1.5`` is the realistic
    Bluetooth figure that motivates the change and rides the same branch.
    """
    db_path = tmp_path / f"latency-{reported_latency}.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=8_000,
        ring_seconds=0.25,
        lazy_open=True,
        generation_safe=True,
        estimated_output_latency_s=0.12,
    )
    stream = _FakeOutputStream(latency=reported_latency)
    with patch.object(voice_tts, "_open_output_stream", return_value=stream):
        # The owner opens the device on its own startup, so the patched stream
        # must be in place before the pipeline exists.
        pipeline = voice_media.StreamingTTSPipeline(
            provider=provider,
            player=player,
            conn_factory=lambda: open_event_log(db_path),
            boot_high_water_id=0,
            config=_config(),
            start_player=True,
        )
    try:
        with _CallbackPump(player):
            rows = _emit_response(
                conn,
                response_id="RL",
                group_id="GL",
                turn_id="TL",
                text="latency on the wire",
            )
            asyncio.run(_submit_response(pipeline, rows))
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()

    verdict = open_event_log(db_path)
    try:
        started = verdict.execute(
            "SELECT payload_json FROM events WHERE type = 'surface.playback_started' "
            "AND json_extract(payload_json, '$.response_id') = 'RL'",
        ).fetchall()
        assert len(started) == 1
        payload = cast("dict[str, object]", json.loads(str(started[0][0])))
        assert _payload_int(payload, "estimated_output_latency_ns") == expected_ns
    finally:
        verdict.close()


_DECLICK_AMPLITUDE_INT16 = 24_000
_DECLICK_AMPLITUDE = np.float32(_DECLICK_AMPLITUDE_INT16) / np.float32(32_768)
_DECLICK_RESPONSE_SAMPLES = 1_600
# 1590 = 32 x 49 + 22, so the final pumped callback reads `actual = 22 < 32`.
# 1600 = 32 x 50 is block-aligned and never produces a short read.
_TAIL_RAMP_RESPONSE_SAMPLES = 1_590
_TAIL_RAMP_LAST_BLOCK = 22
_TAIL_RAMP_ALIGNED_SAMPLES = _TAIL_RAMP_RESPONSE_SAMPLES - _TAIL_RAMP_LAST_BLOCK


def _declick_pipeline(
    db_path: Path,
    *,
    response_id: str,
    samples: int = _DECLICK_RESPONSE_SAMPLES,
) -> tuple[
    voice_media.StreamingTTSPipeline,
    voice_tts.AudioStreamPlayer,
    _FakeProvider,
]:
    """Build a real pipeline whose one segment is loud, constant-amplitude PCM."""
    provider = _FakeProvider(
        {
            (response_id, 0): _Behavior(
                samples=samples,
                amplitude=_DECLICK_AMPLITUDE_INT16,
            ),
        },
        candidate_count=1,
    )
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    return pipeline, player, provider


def test_a_user_stop_fades_the_output_instead_of_stepping_to_silence(
    tmp_path: Path,
) -> None:
    """ADR-0006 D11: the interrupt amplitude edge is ramped, not a hard cut.

    The interrupt is issued through the pipeline's public stop entry point, so
    the signal asserted on below is what the production callback handed
    PortAudio while the production actor tombstoned the generation.
    """
    db_path = tmp_path / "declick-interrupt.db"
    conn = open_event_log(db_path)
    pipeline, player, provider = _declick_pipeline(db_path, response_id="RCLICK")
    hold = threading.Event()
    provider.final_gates[("RCLICK", 0)] = hold
    pump = _CallbackPump(player, record=True)
    blocks_before_cut = 10
    try:
        rows = _emit_response(
            conn,
            response_id="RCLICK",
            group_id="GCLICK",
            turn_id="TCLICK",
            text="这一句会被打断。",
        )
        asyncio.run(_submit_response(pipeline, rows))
        _pump_until(
            pump,
            lambda: _playback_rows_for(conn, "RCLICK", "surface.playback_started") == 1,
        )
        _pump_until(
            pump,
            lambda: _generation_ring(player).available_read() >= 32 * blocks_before_cut,
        )
        # Only the window around the cut is asserted on; every block in it is
        # a full read of committed PCM.
        pump.blocks.clear()
        played_at_cut_start = player.played_samples
        for _ in range(blocks_before_cut):
            pump.step()
        played_before = player.played_samples

        assert pipeline.stop_foreground_output("RCLICK") == "applied"

        for _ in range(10):
            pump.step()
        played_after = player.played_samples
    finally:
        hold.set()
        assert pipeline.close()
        conn.close()

    signal = pump.signal
    cut = 32 * blocks_before_cut
    pre_cut = float(signal[cut - 1])
    # (1) the cut really is from full amplitude, not from an already-silent block
    assert abs(pre_cut) >= 0.5
    assert np.all(signal[:cut] == _DECLICK_AMPLITUDE)
    # (2) a decay exists between the last audio sample and the first exact zero
    first_zero = int(np.flatnonzero(signal[cut:] == 0.0)[0]) + cut
    decay = np.abs(signal[cut:first_zero])
    assert len(decay) >= 2
    assert np.all((decay > 0.0) & (decay < abs(pre_cut)))
    assert np.all(np.diff(decay) <= 0.0)
    # (3) no residual step survives anywhere across the cut region
    edge = np.abs(np.diff(signal[cut - 1 : first_zero + 1]))
    assert float(edge.max()) <= abs(pre_cut) / (voice_tts._DECLICK_SAMPLES - 1) * 1.05  # noqa: SLF001
    # (4) it terminates at exactly 0.0 and stays there
    assert np.all(signal[first_zero:] == 0.0)
    # The bound in (3) scales with `_DECLICK_SAMPLES`, so it cannot catch a
    # fade that is merely declared too short to be heard as anything but a
    # click.  Pin the length itself, in samples and in real time.
    assert len(decay) == voice_tts._DECLICK_SAMPLES - 1  # noqa: SLF001
    assert voice_tts._DECLICK_SAMPLES / 48_000 >= 0.001  # noqa: SLF001
    # (5) the synthesized decay advanced no ledger state: no `_CallbackReport`
    # was written for it, so the audible horizon it feeds cannot have moved.
    assert played_before - played_at_cut_start == cut
    assert played_after == played_before

    verdict = open_event_log(db_path)
    try:
        # (6) the terminal is unchanged by the declick
        assert _playback_rows_for(verdict, "RCLICK", "surface.playback_interrupted") == 1
        assert _reason_of(verdict, "RCLICK") == "user_stop"
    finally:
        verdict.close()


def test_natural_completion_keeps_every_audible_sample_and_its_counts(
    tmp_path: Path,
) -> None:
    """The shared-site declick never eats content at end-of-generation.

    Fixing at the one site all six interrupt entry points converge on also
    covers natural completion; this is the row that proves the widening costs
    no audible content and no accounting.
    """
    reset_realtime_trace()
    db_path = tmp_path / "declick-completion.db"
    conn = open_event_log(db_path)
    pipeline, player, _provider = _declick_pipeline(db_path, response_id="RDONE")
    pump = _CallbackPump(player, record=True)
    try:
        rows = _emit_response(
            conn,
            response_id="RDONE",
            group_id="GDONE",
            turn_id="TDONE",
            text="这一句会读完。",
        )
        asyncio.run(_submit_response(pipeline, rows))
        # Every sample reaches the ring before the first callback runs, so no
        # block below is a mid-response underrun.
        _wait_until(
            lambda: _generation_ring(player).available_read() >= _DECLICK_RESPONSE_SAMPLES,
        )
        pump.blocks.clear()
        _pump_until(
            pump,
            lambda: _playback_rows_for(conn, "RDONE", "surface.playback_completed") == 1,
            timeout_s=5.0,
        )
        assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()

    signal = pump.signal
    # No sample carrying content is attenuated.
    assert np.all(signal[:_DECLICK_RESPONSE_SAMPLES] == _DECLICK_AMPLITUDE)
    tail = signal[_DECLICK_RESPONSE_SAMPLES:]
    sounding = np.flatnonzero(tail)
    decay_len = 0 if sounding.size == 0 else int(sounding[-1]) + 1
    assert decay_len <= voice_tts._DECLICK_SAMPLES  # noqa: SLF001
    assert np.all(np.diff(np.abs(tail[:decay_len])) <= 0.0)
    assert np.all(tail[decay_len:] == 0.0)

    verdict = open_event_log(db_path)
    try:
        kind, payload = _terminal_for(verdict, response_id="RDONE")
        assert kind == "surface.playback_completed"
        assert _payload_int(payload, "total_samples") == _DECLICK_RESPONSE_SAMPLES
        assert payload["heard_through_sequence"] == 0
        # ADR-0006 D12 discriminator: every read of this generation was full, so
        # no tail ramp was applied.  The short-read sibling below reads 22.
        assert _payload_int(payload, "tail_ramp_samples") == 0
    finally:
        verdict.close()
    # `tts_estimated_audible` is only recorded on a `fully_presented` snapshot.
    audible = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "tts_estimated_audible"
        and point.attributes.get("response_id") == "RDONE"
    ]
    assert len(audible) == 1
    assert audible[0].attributes["estimated_audible_samples"] == _DECLICK_RESPONSE_SAMPLES


def test_end_of_generation_short_read_ramps_inside_its_own_block(
    tmp_path: Path,
) -> None:
    """ADR-0006 D12: `0 < actual < frames` decays to zero, and says how far.

    The response is driven end to end through the pipeline's public submit
    path; the signal asserted on is what the production callback wrote into
    `outdata` while the production actor drove it.
    """
    reset_realtime_trace()
    db_path = tmp_path / "tail-ramp.db"
    conn = open_event_log(db_path)
    pipeline, player, _provider = _declick_pipeline(
        db_path,
        response_id="RTAIL",
        samples=_TAIL_RAMP_RESPONSE_SAMPLES,
    )
    pump = _CallbackPump(player, record=True)
    try:
        rows = _emit_response(
            conn,
            response_id="RTAIL",
            group_id="GTAIL",
            turn_id="TTAIL",
            text="这一句的末尾不对齐。",
        )
        asyncio.run(_submit_response(pipeline, rows))
        # Every sample reaches the ring before the first callback runs, so the
        # only short read below is the generation's last block.
        _wait_until(
            lambda: _generation_ring(player).available_read() >= _TAIL_RAMP_RESPONSE_SAMPLES,
        )
        pump.blocks.clear()
        _pump_until(
            pump,
            lambda: _playback_rows_for(conn, "RTAIL", "surface.playback_completed") == 1,
            timeout_s=5.0,
        )
        assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()

    signal = pump.signal
    aligned = _TAIL_RAMP_ALIGNED_SAMPLES
    end = _TAIL_RAMP_RESPONSE_SAMPLES
    ramp = _TAIL_RAMP_LAST_BLOCK
    # (1) nothing before this block is touched: the ramp did not reach backwards
    assert np.all(signal[:aligned] == _DECLICK_AMPLITUDE)
    # (2) the ramp starts at unity, not at a tail slice of `_DECLICK_RAMP`
    assert signal[aligned] == _DECLICK_AMPLITUDE
    # (3) strictly descending across the 22 real samples
    assert np.all(np.diff(signal[aligned:end]) < 0)
    # (4) it lands on exactly 0.0 on the last real sample
    assert signal[end - 1] == 0.0
    # (5) no residual one-sample step survives anywhere across the junction
    step = np.abs(np.diff(signal[aligned - 1 : end + 10]))
    assert float(step.max()) <= float(_DECLICK_AMPLITUDE) / (ramp - 1) * 1.01
    # (6) the pad and everything after stay exactly silent
    assert np.all(signal[end:] == 0.0)

    verdict = open_event_log(db_path)
    try:
        kind, payload = _terminal_for(verdict, response_id="RTAIL")
        assert kind == "surface.playback_completed"
        # The field the owner's forensics lacked: rows 1-6 are what it certifies.
        assert _payload_int(payload, "tail_ramp_samples") == ramp
        assert _payload_int(payload, "submitted_samples") == _TAIL_RAMP_RESPONSE_SAMPLES
        assert _payload_int(payload, "total_samples") == _TAIL_RAMP_RESPONSE_SAMPLES
        assert _payload_int(payload, "starvation_gaps") == 0
        assert _payload_int(payload, "host_underflows") == 0
    finally:
        verdict.close()
    # The ramp shaped a block, not a span: the ledger accounts every sample.
    audible = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "tts_estimated_audible"
        and point.attributes.get("response_id") == "RTAIL"
    ]
    assert len(audible) == 1
    assert audible[0].attributes["estimated_audible_samples"] == _TAIL_RAMP_RESPONSE_SAMPLES
