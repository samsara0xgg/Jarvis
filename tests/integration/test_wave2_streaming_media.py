"""Wave 2 streaming output integration, churn, replay, and rollout gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Self, cast
from unittest.mock import patch

import numpy as np
import pytest
import yaml

from jarvis.runtime import inherent_loop
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback
from jarvis.surface import voice_media, voice_tts
from jarvis.surface.voice_ledger import ForegroundBusy, StalePlaybackGeneration

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from typing import Any

    from jarvis.shared import Event


@dataclass(frozen=True)
class _Behavior:
    outcome: Literal["success", "fail_before", "fail_after"] = "success"
    final_delay_s: float = 0.01
    samples: int = 160
    sample_rate_hz: int = 8_000


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
            pcm = np.full(self._behavior.samples, 2000, dtype="<i2").tobytes()
            yield voice_tts.TTSAudioChunk(
                sequence=segment.sequence,
                pcm=pcm,
                sample_rate_hz=self._behavior.sample_rate_hz,
            )
            if self._behavior.outcome == "fail_after":
                await asyncio.sleep(self._behavior.final_delay_s)
                msg = "fake failure after accepted prefix"
                raise OSError(msg)
            final_gate = self._provider.final_gates.get(
                (self._response_id, segment.sequence),
            )
            while (  # noqa: ASYNC110
                final_gate is not None and not final_gate.is_set() and not self._closed
            ):
                await asyncio.sleep(0.001)
            await asyncio.sleep(self._behavior.final_delay_s)
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
    def __init__(self, player: voice_tts.AudioStreamPlayer) -> None:
        self._player = player
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="test-portaudio-pump")

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        assert not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            out = np.zeros((32, 1), dtype=np.float32)
            self._player._callback(out, 32, None, None)  # noqa: SLF001
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
    assert np.all(output[:16, 0] == np.float32(0.75))

    player.interrupt_generation(
        expected_playback_generation_id=second.playback_generation_id,
    )
    cycle_start = threading.Barrier(3)
    cycle_done = threading.Barrier(3)
    current: dict[str, object] = {}
    worker_errors: list[BaseException] = []
    callback_outputs: list[np.ndarray] = []

    def _late_provider_writer() -> None:
        try:
            for cycle in range(1_000):
                cycle_start.wait()
                lease = cast("Any", current["lease"])
                player.write_generation(
                    np.full(8, cycle + 1, dtype=np.float32).tobytes(),
                    expected_playback_generation_id=lease.playback_generation_id,
                    segment_sequence=0,
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
    writer.start()
    callback.start()
    for cycle in range(1_000):
        lease = player.activate_generation(
            session_id="S",
            response_id=f"RC{cycle}",
            response_group_id=f"GC{cycle}",
            turn_id=f"TC{cycle}",
        )
        assert not isinstance(lease, ForegroundBusy)
        player.begin_generation_segment(
            expected_playback_generation_id=lease.playback_generation_id,
            sequence=0,
            text="x",
            segment_hash="x",
        )
        current["lease"] = lease
        cycle_start.wait()
        player.interrupt_generation(
            expected_playback_generation_id=lease.playback_generation_id,
        )
        cycle_done.wait()
        settled: object | None = None
        for _settle_poll in range(1_000):
            settled = player.settle_interrupted_generation(
                expected_playback_generation_id=lease.playback_generation_id,
            )
            if settled is not None:
                break
            time.sleep(0)
        assert settled is not None
        player.retire_generation(lease.playback_generation_id)
    writer.join(timeout=2.0)
    callback.join(timeout=2.0)
    assert not writer.is_alive()
    assert not callback.is_alive()
    assert not worker_errors
    for cycle, output in enumerate(callback_outputs):
        nonzero = output[output != 0]
        assert nonzero.size == 0 or np.all(nonzero == np.float32(cycle + 1))
    assert player.active_lease is None

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
    assert np.count_nonzero(post_output) == 0
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


def test_checkpoint_persists_during_later_provider_feed_and_retries(tmp_path: Path) -> None:
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
            assert ("RCP", 1) in provider.sent
            assert ("RCP", 0) in provider.provider_finals
            assert ("RCP", 1) not in provider.provider_finals
            assert checkpoint_attempts >= 2
            second_gate.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
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
    finally:
        terminal_conn.close()
    assert old_payload["heard_through_sequence"] == 0
    assert old_payload["heard_text"] == "第一句。"
    assert "第二句" not in str(old_payload["heard_text"])
    assert "不可朗读" not in str(old_payload["heard_text"])


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


def test_media_owner_startup_failures_and_bounded_shutdown(tmp_path: Path) -> None:  # noqa: PLR0915
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
    assert provider_pipeline.close(wait_timeout_s=0.12)
    assert time.monotonic() - started < 0.16
    provider_gate.set()

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
        assert player_pipeline.close(wait_timeout_s=0.12)
        assert time.monotonic() - started < 0.16
    player_gate.set()
    assert len(_live_media_owner_threads()) == baseline


class _FakeOutputStream:
    def __init__(self, *, start_error: BaseException | None = None) -> None:
        self.active = True
        self._start_error = start_error

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


def test_streaming_rollout_default_off_and_production_builder_gate(
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
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=[
            failed_stream,
            legacy_stream,
        ],
    ):
        startup_fallback = inherent_loop._build_tts_pipeline(  # noqa: SLF001
            cast("Any", runtime),
            cast("Any", SimpleNamespace()),
        )
        assert isinstance(startup_fallback, voice_tts.TTSPipeline)
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
    assert not _live_media_owner_threads()
    conn.close()
