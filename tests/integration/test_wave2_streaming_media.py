"""Wave 2 streaming output integration, churn, replay, and rollout gates."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
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
            pcm = np.full(self._behavior.samples, 2000, dtype="<i2").tobytes()
            yield voice_tts.TTSAudioChunk(
                sequence=segment.sequence,
                pcm=pcm,
                sample_rate_hz=self._behavior.sample_rate_hz,
            )
            if self._behavior.outcome == "fail_after":
                msg = "fake failure after accepted prefix"
                raise OSError(msg)
            await asyncio.sleep(self._behavior.final_delay_s)
            self._provider.provider_finals.append(self._response_id)
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
        self.provider_finals: list[str] = []
        self.aborted: list[tuple[str, str]] = []

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


def test_generation_cas_races_and_thousand_cycle_churn() -> None:
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
        assert (
            player.write_generation(
                np.ones(8, dtype=np.float32).tobytes(),
                expected_playback_generation_id=lease.playback_generation_id,
                segment_sequence=0,
            )
            is not None
        )
        player.interrupt_generation(
            expected_playback_generation_id=lease.playback_generation_id,
        )
        player._callback(np.zeros((8, 1), dtype=np.float32), 8, None, None)  # noqa: SLF001
        player.retire_generation(lease.playback_generation_id)
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


class _FakeOutputStream:
    active = True

    def start(self) -> None:
        return

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


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
    conn.close()
