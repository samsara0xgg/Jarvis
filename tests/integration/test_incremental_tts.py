"""Incremental TTS from permitted segments: L2 lease rule and L5 segment loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared import Event
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback, terminalize_response
from jarvis.surface import voice_media, voice_tts
from tests.integration.test_wave2_streaming_media import (
    _Behavior,
    _CallbackPump,
    _config,
    _emit_response,
    _FakeProvider,
    _player,
    _submit_response,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_SEGMENTS = ("第一句。", "第二句。", "第三句。")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Trail:
    """Emit one incremental playback trail row by row, in Event Log order."""

    def __init__(self, conn: sqlite3.Connection, *, response_id: str = "R") -> None:
        self.conn = conn
        self.response_id = response_id
        self.chunks: list[Event] = []
        self.started: Event | None = None
        self.prepared: list[str] = []
        emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": "T", "transcript": "synthetic", "channel": "voice"},
        )
        self.open = emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T",
                "query": "q",
                "kind": "stream",
                "response_id": response_id,
                "response_group_id": "G",
                "phase": "final",
                "channel": "speech",
            },
        )

    def _base(self) -> dict[str, Any]:
        return {
            "session_id": "S",
            "response_id": self.response_id,
            "turn_id": "T",
            "playback_generation_id": 1,
        }

    def chunk(self, text: str) -> Event:
        event = emit_event(
            self.conn,
            type="surface.response_chunk",
            payload={
                "turn_id": "T",
                "response_id": self.response_id,
                "sequence": len(self.chunks),
                "text": text,
                "segment_hash": _sha(text),
            },
        )
        self.chunks.append(event)
        return event

    def start(self, *, first_segment: str, source: Event | None = None) -> Event:
        self.started = emit_event(
            self.conn,
            type="surface.playback_started",
            payload={
                **self._base(),
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": _sha(first_segment),
                "incremental": True,
            },
            source_event_id=(source or self.open).event_uid,
        )
        return self.started

    def prepare(self, sequence: int, *, source: Event | None = None) -> None:
        assert self.started is not None
        chunk = source or self.chunks[sequence]
        text = str(chunk.payload.get("text") or chunk.payload["voice_text"])
        if source is not None:
            committed = "".join(str(item.payload["text"]) for item in self.chunks)
            text = str(chunk.payload["voice_text"])[len(committed) :]
        self.prepared.append(text)
        emit_event(
            self.conn,
            type="surface.playback_segment_prepared",
            payload={
                **self._base(),
                "sequence": sequence,
                "speech_text": text,
                "speech_text_hash": _sha(text),
                "segment_hash": _sha(text),
                "source_chunk_event_uid": chunk.event_uid,
            },
            source_event_id=self.started.event_uid,
        )

    def _cursor(self, through: int) -> dict[str, Any]:
        heard = "".join(self.prepared[: through + 1])
        return {
            **self._base(),
            "heard_through_sequence": through,
            "submitted_samples": 160 * (through + 1),
            "heard_text": heard,
            "heard_text_hash": _sha(heard),
            "cursor_quality": "estimated",
        }

    def checkpoint(self, through: int) -> None:
        assert self.started is not None
        emit_event(
            self.conn,
            type="surface.playback_checkpoint",
            payload=self._cursor(through),
            source_event_id=self.started.event_uid,
        )

    def emitted(self, voice_text: str) -> Event:
        return emit_event(
            self.conn,
            type="surface.response_emitted",
            payload={
                "turn_id": "T",
                "text": voice_text,
                "voice_text": voice_text,
                "response_id": self.response_id,
                "phase": "final",
                "channel": "speech",
            },
        )

    def terminal(self, event_type: str, *, through: int) -> None:
        assert self.started is not None
        payload = self._cursor(through)
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = _sha("".join(self.prepared))
        else:
            payload["reason"] = "synthetic stop"
        terminalize_playback(
            self.conn,
            event_type=event_type,
            payload=payload,
            source_event_id=self.started.event_uid,
        )


def _spoken(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    history = fold_conversation_history(iter_events(conn))
    record = history.turns[0].responses[0]
    heard = record.spoken_heard.text if record.spoken_heard is not None else None
    return history.consistent and record.consistent, heard


def test_incremental_trail_with_two_checkpoints_and_completion_stays_consistent(
    tmp_path: Path,
) -> None:
    """The activation binds only the first segment; the terminal binds the prepared concat."""
    conn = open_event_log(tmp_path / "incremental.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.checkpoint(0)
        trail.chunk(_SEGMENTS[2])
        trail.prepare(2)
        trail.checkpoint(1)
        trail.emitted("".join(_SEGMENTS))
        trail.terminal("surface.playback_completed", through=2)
        assert _spoken(conn) == (True, "".join(_SEGMENTS))
    finally:
        conn.close()


def test_interrupted_incremental_trail_keeps_only_the_played_prefix(tmp_path: Path) -> None:
    """An interrupted incremental lease contributes only its fully played prefix."""
    conn = open_event_log(tmp_path / "interrupted.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.checkpoint(0)
        trail.checkpoint(1)
        trail.chunk(_SEGMENTS[2])
        trail.prepare(2)
        trail.terminal("surface.playback_interrupted", through=1)
        assert _spoken(conn) == (True, _SEGMENTS[0] + _SEGMENTS[1])
    finally:
        conn.close()


def test_incremental_completion_hash_must_match_the_prepared_concatenation(
    tmp_path: Path,
) -> None:
    """A completed terminal whose hash is not the prepared concatenation is rejected."""
    conn = open_event_log(tmp_path / "wrong-hash.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.emitted(_SEGMENTS[0] + _SEGMENTS[1])
        payload = {**trail._cursor(1), "speech_text_hash": _sha(_SEGMENTS[0])}  # noqa: SLF001
        assert trail.started is not None
        terminalize_playback(
            conn,
            event_type="surface.playback_completed",
            payload=payload,
            source_event_id=trail.started.event_uid,
        )
        assert _spoken(conn) == (False, None)
    finally:
        conn.close()


def test_incremental_activation_binds_the_first_prepared_segment(tmp_path: Path) -> None:
    """The activation digest must be the first prepared segment's hash."""
    conn = open_event_log(tmp_path / "first-hash.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment="not-the-first-segment")
        trail.prepare(0)
        trail.emitted(_SEGMENTS[0])
        trail.terminal("surface.playback_completed", through=0)
        assert _spoken(conn) == (False, None)
    finally:
        conn.close()


def test_non_incremental_activation_keeps_exact_full_text_equality(tmp_path: Path) -> None:
    """Without ``incremental`` the activation hash must still equal the full speech hash."""
    conn = open_event_log(tmp_path / "legacy.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.chunk(_SEGMENTS[1])
        surface = trail.emitted(_SEGMENTS[0] + _SEGMENTS[1])
        trail.started = emit_event(
            conn,
            type="surface.playback_started",
            payload={
                **trail._base(),  # noqa: SLF001
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": _sha(_SEGMENTS[0]),
            },
            source_event_id=surface.event_uid,
        )
        trail.prepare(0)
        trail.prepare(1)
        trail.terminal("surface.playback_completed", through=1)
        assert _spoken(conn) == (False, None)
    finally:
        conn.close()


def test_voice_suffix_beyond_the_committed_chunks_is_one_more_prepared_segment(
    tmp_path: Path,
) -> None:
    """The emitted row's unspoken voice suffix validates as a segment it sourced."""
    conn = open_event_log(tmp_path / "suffix.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.checkpoint(0)
        surface = trail.emitted(_SEGMENTS[0] + _SUFFIX)
        trail.prepare(1, source=surface)
        assert trail.prepared[1] == _SUFFIX
        trail.terminal("surface.playback_completed", through=1)
        assert _spoken(conn) == (True, _SEGMENTS[0] + _SUFFIX)
    finally:
        conn.close()


# --- L5 media owner: speak from the first permitted segment ------------------


def _pipeline(
    db_path: Path,
    provider: _FakeProvider,
    *,
    speak_from_segments: bool,
    response_timeout_s: float | None = None,
) -> tuple[voice_media.StreamingTTSPipeline, voice_tts.AudioStreamPlayer]:
    player = _player()
    config = replace(_config(), speak_from_segments=speak_from_segments)
    if response_timeout_s is not None:
        config = replace(config, response_timeout_s=response_timeout_s)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=config,
        start_player=False,
    )
    return pipeline, player


def _row(conn: sqlite3.Connection, event: Event) -> tuple[int, Event]:
    found = conn.execute("SELECT id FROM events WHERE event_uid = ?", (event.event_uid,)).fetchone()
    assert found is not None
    return int(found[0]), event


def _open(
    conn: sqlite3.Connection,
    response_id: str,
    *,
    kind: str = "stream",
    attention_channel: str | None = None,
) -> tuple[int, Event]:
    payload: dict[str, Any] = {
        "turn_id": "T-" + response_id,
        "query": "q",
        "kind": kind,
        "response_id": response_id,
        "response_group_id": "G-" + response_id,
        "phase": "final",
        "channel": "speech",
    }
    if attention_channel is not None:
        payload["attention_channel"] = attention_channel
    return _row(conn, emit_event(conn, type="surface.response_open", payload=payload))


def _chunk(
    conn: sqlite3.Connection, response_id: str, sequence: int, text: str
) -> tuple[int, Event]:
    return _row(
        conn,
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={
                "turn_id": "T-" + response_id,
                "text": text,
                "response_id": response_id,
                "response_group_id": "G-" + response_id,
                "sequence": sequence,
                "phase": "final",
                "channel": "speech",
                "segment_hash": _sha(text),
            },
        ),
    )


def _emitted(conn: sqlite3.Connection, response_id: str, voice_text: str) -> tuple[int, Event]:
    return _row(
        conn,
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": "T-" + response_id,
                "text": voice_text,
                "voice_text": voice_text,
                "response_id": response_id,
                "response_group_id": "G-" + response_id,
                "phase": "final",
                "channel": "speech",
            },
        ),
    )


def _rows(
    conn: sqlite3.Connection, event_type: str, response_id: str
) -> list[tuple[int, dict[str, Any], str | None]]:
    found = conn.execute(
        "SELECT id, payload_json, source_event_id FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
        (event_type, response_id),
    ).fetchall()
    return [(int(row[0]), json.loads(str(row[1])), row[2]) for row in found]


def _wait_for(conn: sqlite3.Connection, event_type: str, response_id: str, count: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(_rows(conn, event_type, response_id)) >= count:
            return
        time.sleep(0.005)
    pytest.fail(f"{event_type} x{count} for {response_id} never became durable")


_SUFFIX = (
    "这是一条没有被流式门放行却在最终文本里出现的长句子。它超过了六十个码点的安全子句上限。"
    "所以只能在响应发出之后作为最后一段被朗读。"
)
_SAFE_SUBCLAUSE_CAP = 60
assert len(_SUFFIX) > _SAFE_SUBCLAUSE_CAP


def test_stream_response_starts_speaking_from_its_first_permitted_segment(
    tmp_path: Path,
) -> None:
    """playback_started and the first prepared segment land before response_emitted."""
    db_path = tmp_path / "stream.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    try:
        with _CallbackPump(player):
            open_row = _open(conn, "RS")
            asyncio.run(_submit_response(pipeline, [open_row, _chunk(conn, "RS", 0, _SEGMENTS[0])]))
            _wait_for(conn, "surface.playback_segment_prepared", "RS", 1)
            late = [_chunk(conn, "RS", 1, _SEGMENTS[1]), _chunk(conn, "RS", 2, _SEGMENTS[2])]
            emitted_row = _emitted(conn, "RS", "".join(_SEGMENTS) + _SUFFIX)
            outcomes = asyncio.run(_submit_response(pipeline, [*late, emitted_row]))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 3
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = _rows(conn, "surface.playback_started", "RS")
        prepared = _rows(conn, "surface.playback_segment_prepared", "RS")
        completed = _rows(conn, "surface.playback_completed", "RS")
    finally:
        conn.close()
    assert len(started) == 1
    assert started[0][1]["incremental"] is True
    assert started[0][1]["speech_text_hash"] == _sha(_SEGMENTS[0])
    assert started[0][2] == open_row[1].event_uid
    assert started[0][0] < prepared[0][0] < emitted_row[0] < prepared[1][0]
    assert [row[1]["sequence"] for row in prepared] == [0, 1, 2, 3]
    assert [row[1]["speech_text"] for row in prepared] == [*_SEGMENTS, _SUFFIX]
    assert prepared[3][1]["source_chunk_event_uid"] == emitted_row[1].event_uid
    assert len(completed) == 1
    spoken = "".join(row[1]["speech_text"] for row in prepared)
    assert completed[0][1]["speech_text_hash"] == _sha(spoken)
    assert completed[0][1]["heard_text"] == spoken
    assert provider.sent == [("RS", 0), ("RS", 1), ("RS", 2), ("RS", 3)]


def _legacy_trail(tmp_path: Path, *, speak_from_segments: bool) -> list[tuple[Any, ...]]:
    db_path = tmp_path / f"legacy-{int(speak_from_segments)}.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=speak_from_segments)
    try:
        with _CallbackPump(player):
            rows = _emit_response(
                conn,
                response_id="RLEG",
                group_id="GLEG",
                turn_id="TLEG",
                text=["<voice>第一句。", "第二句。</voice><document>不可朗读。</document>"],
            )
            outcomes = asyncio.run(_submit_response(pipeline, rows))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 4
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        found = conn.execute(
            "SELECT type, payload_json FROM events WHERE type IN (?, ?, ?, ?) ORDER BY id",
            (
                "surface.response_emitted",
                "surface.playback_started",
                "surface.playback_segment_prepared",
                "surface.playback_completed",
            ),
        ).fetchall()
    finally:
        conn.close()
    trail: list[tuple[Any, ...]] = []
    for kind, raw in found:
        payload = json.loads(str(raw))
        trail.append(
            (
                kind,
                payload.get("sequence"),
                payload.get("speech_text"),
                payload.get("speech_text_hash"),
                payload.get("incremental"),
            ),
        )
    return trail


def test_legacy_text_response_keeps_the_emitted_time_trail_with_the_flag_on(
    tmp_path: Path,
) -> None:
    """A kind="text" response with tags produces the flag-off trail exactly."""
    off = _legacy_trail(tmp_path, speak_from_segments=False)
    on = _legacy_trail(tmp_path, speak_from_segments=True)
    assert on == off
    kinds = [row[0] for row in on]
    assert kinds.index("surface.response_emitted") < kinds.index("surface.playback_started")
    assert [row[2] for row in on if row[0] == "surface.playback_segment_prepared"] == [
        "第一句。",
        "第二句。",
    ]
    assert all(row[4] is None for row in on)


def test_flag_off_stream_response_speaks_its_suffix_only_at_emitted_time(
    tmp_path: Path,
) -> None:
    """A1 is ungated: with the flag off the suffix is still spoken, after emitted."""
    db_path = tmp_path / "off-suffix.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=False)
    try:
        with _CallbackPump(player):
            rows = [_open(conn, "ROFF"), _chunk(conn, "ROFF", 0, _SEGMENTS[0])]
            emitted_row = _emitted(conn, "ROFF", _SEGMENTS[0] + _SUFFIX)
            asyncio.run(_submit_response(pipeline, [*rows, emitted_row]))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = _rows(conn, "surface.playback_started", "ROFF")
        prepared = _rows(conn, "surface.playback_segment_prepared", "ROFF")
    finally:
        conn.close()
    assert len(started) == 1
    assert started[0][0] > emitted_row[0]
    assert "incremental" not in started[0][1]
    assert [row[1]["speech_text"] for row in prepared] == [_SEGMENTS[0], _SUFFIX]


def test_queue_review_open_never_starts_playback(tmp_path: Path) -> None:
    """The silent-channel guard is evaluated on the open row, ahead of any chunk."""
    db_path = tmp_path / "silent.db"
    conn = open_event_log(db_path)
    pipeline, _ = _pipeline(db_path, _FakeProvider(), speak_from_segments=True)
    try:
        rows = [
            _open(conn, "RQ", attention_channel="queue_review"),
            _chunk(conn, "RQ", 0, _SEGMENTS[0]),
            _emitted(conn, "RQ", _SEGMENTS[0]),
        ]
        outcomes = asyncio.run(_submit_response(pipeline, rows))
        assert [outcome.status for outcome in outcomes] == ["accepted", "terminal", "terminal"]
        assert pipeline.wait_until_idle(timeout_s=1.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert _rows(conn, "surface.playback_started", "RQ") == []
    finally:
        conn.close()


def test_tag_before_playback_started_falls_back_to_emitted_time_speaking(
    tmp_path: Path,
) -> None:
    """A tagged chunk before any playback keeps the run on the emitted-time path."""
    db_path = tmp_path / "tag-before.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    try:
        with _CallbackPump(player):
            rows = [
                _open(conn, "RTB"),
                _chunk(conn, "RTB", 0, "<voice>第一句。"),
                _chunk(conn, "RTB", 1, "第二句。</voice>"),
            ]
            emitted_row = _emitted(conn, "RTB", "第一句。第二句。")
            outcomes = asyncio.run(_submit_response(pipeline, [*rows, emitted_row]))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 4
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = _rows(conn, "surface.playback_started", "RTB")
        prepared = _rows(conn, "surface.playback_segment_prepared", "RTB")
        completed = _rows(conn, "surface.playback_completed", "RTB")
    finally:
        conn.close()
    assert len(started) == 1
    assert started[0][0] > emitted_row[0]
    assert "incremental" not in started[0][1]
    assert [row[1]["speech_text"] for row in prepared] == ["第一句。", "第二句。"]
    assert completed[0][1]["speech_text_hash"] == _sha("第一句。第二句。")


def test_tag_after_playback_started_fails_the_run_and_stops_segments(tmp_path: Path) -> None:
    """A tag after playback started stops segments with playback_failed(stream_chunk_tagged)."""
    db_path = tmp_path / "tag-after.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    try:
        with _CallbackPump(player):
            first = [_open(conn, "RTA"), _chunk(conn, "RTA", 0, _SEGMENTS[0])]
            asyncio.run(_submit_response(pipeline, first))
            _wait_for(conn, "surface.playback_segment_prepared", "RTA", 1)
            tagged = _chunk(conn, "RTA", 1, "<document>不可朗读。</document>")
            assert asyncio.run(_submit_response(pipeline, [tagged]))[0].status == "accepted"
            assert pipeline.wait_until_idle(timeout_s=2.0)
            late = _emitted(conn, "RTA", _SEGMENTS[0] + "<document>不可朗读。</document>")
            assert asyncio.run(_submit_response(pipeline, [late]))[0].status == "terminal"
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        failed = _rows(conn, "surface.playback_failed", "RTA")
        prepared = _rows(conn, "surface.playback_segment_prepared", "RTA")
    finally:
        conn.close()
    assert [row[1]["sequence"] for row in prepared] == [0]
    assert len(failed) == 1
    assert failed[0][1]["reason"] == "stream_chunk_tagged"
    assert failed[0][0] > prepared[0][0]


# --- Mid-stream cancel reaches the active playback --------------------------


def _cancel(conn: sqlite3.Connection, response_id: str) -> tuple[int, Event]:
    outcome = terminalize_response(
        conn,
        event_type="response.cancelled",
        payload={
            "response_id": response_id,
            "response_group_id": "G-" + response_id,
            "turn_id": "T-" + response_id,
            "reason": "operator_request",
        },
    )
    return _row(conn, outcome.event)


def test_cancel_mid_stream_interrupts_playback_through_the_real_watcher(
    tmp_path: Path,
) -> None:
    """_tts_watcher feeds response.cancelled to the media owner, which interrupts."""
    db_path = tmp_path / "cancel.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    watcher_conn = open_event_log(db_path)

    async def _watch(until: str) -> None:
        watcher = asyncio.create_task(
            inherent_loop._tts_watcher(  # noqa: SLF001 - production watcher integration
                conn=watcher_conn,
                pipeline=pipeline,
                poll_interval_s=0.005,
            ),
        )
        try:
            deadline = time.monotonic() + 2.0
            while not _rows(conn, until, "RC"):
                if time.monotonic() >= deadline:
                    pytest.fail(f"{until} never became durable")
                await asyncio.sleep(0.005)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    try:
        with _CallbackPump(player):
            _open(conn, "RC")
            _chunk(conn, "RC", 0, _SEGMENTS[0])
            asyncio.run(_watch("surface.playback_segment_prepared"))
            cancel_row = _cancel(conn, "RC")
            asyncio.run(_watch("surface.playback_interrupted"))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            late = _chunk(conn, "RC", 1, _SEGMENTS[1])
            assert asyncio.run(_submit_response(pipeline, [late]))[0].status == "terminal"
            assert not pipeline._after_drain  # noqa: SLF001
    finally:
        assert pipeline.close()
        watcher_conn.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        interrupted = _rows(conn, "surface.playback_interrupted", "RC")
        prepared = _rows(conn, "surface.playback_segment_prepared", "RC")
    finally:
        conn.close()
    assert len(interrupted) == 1
    assert interrupted[0][1]["reason"] == "response_cancelled"
    assert cancel_row[0] < interrupted[0][0]
    assert [row[1]["sequence"] for row in prepared] == [0]
    assert all(row[0] < interrupted[0][0] for row in prepared)
    assert provider.aborted == [("RC", "response_cancelled")]


def test_cancel_of_the_active_response_drains_the_queued_lane(tmp_path: Path) -> None:
    """After the interrupt the cancel path leaves nothing pending in _after_drain."""
    db_path = tmp_path / "cancel-lane.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    try:
        with _CallbackPump(player):
            first = [_open(conn, "RA"), _chunk(conn, "RA", 0, "第一句。")]
            asyncio.run(_submit_response(pipeline, first))
            _wait_for(conn, "surface.playback_segment_prepared", "RA", 1)
            queued = _emit_response(
                conn, response_id="RB", group_id="G-RA", turn_id="T-RA", text="排队的后续。"
            )
            statuses = [o.status for o in asyncio.run(_submit_response(pipeline, queued))]
            assert statuses == ["accepted"] * 3
            assert len(pipeline._after_drain) == 1  # noqa: SLF001
            cancelled = asyncio.run(_submit_response(pipeline, [_cancel(conn, "RA")]))
            assert cancelled[0].status == "accepted"
            assert not pipeline._after_drain  # noqa: SLF001
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert len(_rows(conn, "surface.playback_interrupted", "RA")) == 1
        assert _rows(conn, "surface.playback_started", "RB") == []
    finally:
        conn.close()


def test_flag_off_cancel_drops_the_buffered_response(tmp_path: Path) -> None:
    """The terminal wiring is ungated: a cancelled run never speaks at emitted time."""
    db_path = tmp_path / "cancel-off.db"
    conn = open_event_log(db_path)
    pipeline, _ = _pipeline(db_path, _FakeProvider(), speak_from_segments=False)
    try:
        rows = [
            _open(conn, "RD"),
            _chunk(conn, "RD", 0, _SEGMENTS[0]),
            _cancel(conn, "RD"),
            _emitted(conn, "RD", _SEGMENTS[0]),
        ]
        outcomes = asyncio.run(_submit_response(pipeline, rows))
        assert [o.status for o in outcomes] == ["accepted", "accepted", "accepted", "terminal"]
        assert pipeline.wait_until_idle(timeout_s=1.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert _rows(conn, "surface.playback_started", "RD") == []
    finally:
        conn.close()


def test_silent_turn_is_forgotten_on_the_run_terminal() -> None:
    """A cancelled queue_review turn leaves no entry behind in silent_turns."""
    silent: set[str] = set()
    opened = Event(
        event_uid="E-open",
        type="surface.response_open",
        schema_version=1,
        ts_epoch_ms=0,
        payload={"turn_id": "TQ", "attention_channel": "queue_review"},
        source_event_id=None,
        correlation={},
    )
    cancelled = replace(
        opened, event_uid="E-cancel", type="response.cancelled", payload={"turn_id": "TQ"}
    )
    kwargs: dict[str, Any] = {
        "turn_id": "TQ",
        "silent_turns": silent,
        "silent_channels": inherent_loop._TTS_SILENT_CHANNELS,  # noqa: SLF001
        "consumer": "test",
    }
    assert inherent_loop._drop_for_silent_channel(opened, **kwargs)  # noqa: SLF001
    assert silent == {"TQ"}
    assert inherent_loop._drop_for_silent_channel(cancelled, **kwargs)  # noqa: SLF001
    assert silent == set()


# --- L5 media owner: the response budget is per segment, not per generation ---

_BUDGET_S = 1.0
_SEGMENT_DELAY_S = 0.4
_BUDGET_SEGMENTS = ("第一句话。", "第二句话。", "第三句话。", "第四句话。")


def _budget_pipeline(
    db_path: Path,
    *,
    segment_delay_s: float,
) -> tuple[voice_media.StreamingTTSPipeline, voice_tts.AudioStreamPlayer, _FakeProvider]:
    """A non-live pipeline whose provider spends ``segment_delay_s`` on every segment."""
    provider = _FakeProvider(candidate_count=1)
    provider.behaviors[("RB", 0)] = _Behavior(final_delay_s=segment_delay_s)
    pipeline, player = _pipeline(
        db_path,
        provider,
        speak_from_segments=False,
        response_timeout_s=_BUDGET_S,
    )
    return pipeline, player, provider


def _run_budget_response(
    db_path: Path,
    conn: sqlite3.Connection,
    *,
    texts: tuple[str, ...],
    segment_delay_s: float,
) -> None:
    """Submit open + chunks + emitted together, so playback runs the non-live path."""
    pipeline, player, _ = _budget_pipeline(db_path, segment_delay_s=segment_delay_s)
    try:
        with _CallbackPump(player):
            rows = [_open(conn, "RB")]
            rows += [_chunk(conn, "RB", index, text) for index, text in enumerate(texts)]
            rows.append(_emitted(conn, "RB", "".join(texts)))
            asyncio.run(_submit_response(pipeline, rows))
            assert pipeline.wait_until_idle(timeout_s=10.0)
    finally:
        assert pipeline.close()


def test_a_multi_segment_answer_longer_than_the_budget_still_completes(
    tmp_path: Path,
) -> None:
    """The budget bounds one segment, so four under-budget segments outlast it and finish.

    Four segments at 0.4 s of provider I/O each spend 1.6 s against
    ``response_timeout_s`` of 1.0 s: every individual segment is well under the
    bound, the generation as a whole is well over it. ``submitted_samples ==
    total_samples`` restates completion rather than adding an independent fact;
    the discriminating assertions are the completed row and the absent failure row.
    """
    db_path = tmp_path / "budget-completes.db"
    conn = open_event_log(db_path)
    try:
        started = time.monotonic()
        _run_budget_response(
            db_path,
            conn,
            texts=_BUDGET_SEGMENTS,
            segment_delay_s=_SEGMENT_DELAY_S,
        )
        elapsed = time.monotonic() - started
    finally:
        conn.close()
    conn = open_event_log(db_path)
    try:
        completed = _rows(conn, "surface.playback_completed", "RB")
        failed = _rows(conn, "surface.playback_failed", "RB")
        prepared = _rows(conn, "surface.playback_segment_prepared", "RB")
    finally:
        conn.close()
    assert elapsed > _BUDGET_S
    assert failed == []
    assert len(completed) == 1
    payload = completed[0][1]
    assert payload["submitted_samples"] == payload["total_samples"]
    assert payload["heard_through_sequence"] == prepared[-1][1]["sequence"]
    assert [row[1]["speech_text"] for row in prepared] == list(_BUDGET_SEGMENTS)


def test_a_single_segment_over_the_budget_still_fails_the_run_and_is_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One segment past the bound keeps failing with tts_response_timeout, now audibly."""
    db_path = tmp_path / "budget-bites.db"
    conn = open_event_log(db_path)
    try:
        with caplog.at_level(logging.WARNING, logger="jarvis.surface.voice_media"):
            _run_budget_response(
                db_path,
                conn,
                texts=(_BUDGET_SEGMENTS[0],),
                segment_delay_s=_BUDGET_S * 4,
            )
    finally:
        conn.close()
    conn = open_event_log(db_path)
    try:
        failed = _rows(conn, "surface.playback_failed", "RB")
        assert _rows(conn, "surface.playback_completed", "RB") == []
    finally:
        conn.close()
    assert len(failed) == 1
    assert failed[0][1]["reason"] == "tts_response_timeout"
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert any("RB" in message and "response_timeout_s" in message for message in warnings)
