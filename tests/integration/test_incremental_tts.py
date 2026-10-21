"""Incremental TTS from permitted segments: L2 lease rule and L5 segment loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.runtime.daemon import output_watchers
from jarvis.shared import Event
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback
from tests.support.media import (
    Behavior,
    CallbackPump,
    FakeProvider,
    RecordingBroadcaster,
    emit_response,
    submit_response,
)
from tests.support.tts import (
    Trail,
    append_cancel,
    append_chunk,
    append_emitted,
    append_open,
    make_pipeline,
    row_of,
    sha256_of,
    spoken_rows,
    tts_rows,
    wait_for_rows,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.surface import voice_media, voice_tts

_SEGMENTS = ("第一句。", "第二句。", "第三句。")

_GUARD_S = 5.0
"""How long a test waits for the media actor before it fails; not a time anything keeps."""
_POLL_S = 0.001
"""How long one ask of the gated settle waits on its gate; the gate ends it, not this."""


def test_incremental_trail_with_two_checkpoints_and_completion_stays_consistent(
    tmp_path: Path,
) -> None:
    """The activation binds only the first segment; the terminal binds the prepared concat."""
    conn = open_event_log(tmp_path / "incremental.db")
    try:
        trail = Trail(conn)
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
        assert spoken_rows(conn) == (True, "".join(_SEGMENTS))
    finally:
        conn.close()


def test_interrupted_incremental_trail_keeps_only_the_played_prefix(tmp_path: Path) -> None:
    """An interrupted incremental lease contributes only its fully played prefix."""
    conn = open_event_log(tmp_path / "interrupted.db")
    try:
        trail = Trail(conn)
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
        assert spoken_rows(conn) == (True, _SEGMENTS[0] + _SEGMENTS[1])
    finally:
        conn.close()


def test_incremental_completion_hash_must_match_the_prepared_concatenation(
    tmp_path: Path,
) -> None:
    """A completed terminal whose hash is not the prepared concatenation is rejected."""
    conn = open_event_log(tmp_path / "wrong-hash.db")
    try:
        trail = Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.emitted(_SEGMENTS[0] + _SEGMENTS[1])
        payload = {**trail._cursor(1), "speech_text_hash": sha256_of(_SEGMENTS[0])}  # noqa: SLF001
        assert trail.started is not None
        terminalize_playback(
            conn,
            event_type="surface.playback_completed",
            payload=payload,
            source_event_id=trail.started.event_uid,
        )
        assert spoken_rows(conn) == (False, None)
    finally:
        conn.close()


def test_incremental_activation_binds_the_first_prepared_segment(tmp_path: Path) -> None:
    """The activation digest must be the first prepared segment's hash."""
    conn = open_event_log(tmp_path / "first-hash.db")
    try:
        trail = Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment="not-the-first-segment")
        trail.prepare(0)
        trail.emitted(_SEGMENTS[0])
        trail.terminal("surface.playback_completed", through=0)
        assert spoken_rows(conn) == (False, None)
    finally:
        conn.close()


def test_non_incremental_activation_keeps_exact_full_text_equality(tmp_path: Path) -> None:
    """Without ``incremental`` the activation hash must still equal the full speech hash."""
    conn = open_event_log(tmp_path / "legacy.db")
    try:
        trail = Trail(conn)
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
                "speech_text_hash": sha256_of(_SEGMENTS[0]),
            },
            source_event_id=surface.event_uid,
        )
        trail.prepare(0)
        trail.prepare(1)
        trail.terminal("surface.playback_completed", through=1)
        assert spoken_rows(conn) == (False, None)
    finally:
        conn.close()


def test_voice_suffix_beyond_the_committed_chunks_is_one_more_prepared_segment(
    tmp_path: Path,
) -> None:
    """The emitted row's unspoken voice suffix validates as a segment it sourced."""
    conn = open_event_log(tmp_path / "suffix.db")
    try:
        trail = Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.checkpoint(0)
        surface = trail.emitted(_SEGMENTS[0] + _SUFFIX)
        trail.prepare(1, source=surface)
        assert trail.prepared[1] == _SUFFIX
        trail.terminal("surface.playback_completed", through=1)
        assert spoken_rows(conn) == (True, _SEGMENTS[0] + _SUFFIX)
    finally:
        conn.close()


# --- L5 media owner: speak from the first permitted segment ------------------


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
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    try:
        with CallbackPump(player):
            open_row = append_open(conn, "RS")
            asyncio.run(
                submit_response(pipeline, [open_row, append_chunk(conn, "RS", 0, _SEGMENTS[0])])
            )
            wait_for_rows(conn, "surface.playback_segment_prepared", "RS", 1)
            late = [
                append_chunk(conn, "RS", 1, _SEGMENTS[1]),
                append_chunk(conn, "RS", 2, _SEGMENTS[2]),
            ]
            emitted_row = append_emitted(conn, "RS", "".join(_SEGMENTS) + _SUFFIX)
            outcomes = asyncio.run(submit_response(pipeline, [*late, emitted_row]))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 3
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = tts_rows(conn, "surface.playback_started", "RS")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "RS")
        completed = tts_rows(conn, "surface.playback_completed", "RS")
    finally:
        conn.close()
    assert len(started) == 1
    assert started[0][1]["incremental"] is True
    assert started[0][1]["speech_text_hash"] == sha256_of(_SEGMENTS[0])
    assert started[0][2] == open_row[1].event_uid
    assert started[0][0] < prepared[0][0] < emitted_row[0] < prepared[1][0]
    assert [row[1]["sequence"] for row in prepared] == [0, 1, 2, 3]
    assert [row[1]["speech_text"] for row in prepared] == [*_SEGMENTS, _SUFFIX]
    assert prepared[3][1]["source_chunk_event_uid"] == emitted_row[1].event_uid
    assert len(completed) == 1
    spoken = "".join(row[1]["speech_text"] for row in prepared)
    assert completed[0][1]["speech_text_hash"] == sha256_of(spoken)
    assert completed[0][1]["heard_text"] == spoken
    assert provider.sent == [("RS", 0), ("RS", 1), ("RS", 2), ("RS", 3)]


def _legacy_trail(tmp_path: Path, *, speak_from_segments: bool) -> list[tuple[Any, ...]]:
    db_path = tmp_path / f"legacy-{int(speak_from_segments)}.db"
    conn = open_event_log(db_path)
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=speak_from_segments)
    try:
        with CallbackPump(player):
            rows = emit_response(
                conn,
                response_id="RLEG",
                group_id="GLEG",
                turn_id="TLEG",
                text=["<voice>第一句。", "第二句。</voice><document>不可朗读。</document>"],
            )
            outcomes = asyncio.run(submit_response(pipeline, rows))
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
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=False)
    try:
        with CallbackPump(player):
            rows = [append_open(conn, "ROFF"), append_chunk(conn, "ROFF", 0, _SEGMENTS[0])]
            emitted_row = append_emitted(conn, "ROFF", _SEGMENTS[0] + _SUFFIX)
            asyncio.run(submit_response(pipeline, [*rows, emitted_row]))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = tts_rows(conn, "surface.playback_started", "ROFF")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "ROFF")
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
    pipeline, _ = make_pipeline(db_path, FakeProvider(), speak_from_segments=True)
    try:
        rows = [
            append_open(conn, "RQ", attention_channel="queue_review"),
            append_chunk(conn, "RQ", 0, _SEGMENTS[0]),
            append_emitted(conn, "RQ", _SEGMENTS[0]),
        ]
        outcomes = asyncio.run(submit_response(pipeline, rows))
        assert [outcome.status for outcome in outcomes] == ["accepted", "terminal", "terminal"]
        assert pipeline.wait_until_idle(timeout_s=1.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert tts_rows(conn, "surface.playback_started", "RQ") == []
    finally:
        conn.close()


def test_a_typed_turn_the_watcher_dropped_is_not_replayed_by_the_next_spoken_turn(
    tmp_path: Path,
) -> None:
    """ADR 0181: the ordered drain crosses a dropped typed turn's rows; it must not play them."""
    db_path = tmp_path / "typed.db"
    conn = open_event_log(db_path)
    pipeline, player = make_pipeline(
        db_path,
        FakeProvider(candidate_count=1),
        speak_from_segments=False,
        silent_intent_channels=output_watchers.TTS_SILENT_CHANNELS,
    )
    try:
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"channel": "cli_stdin", "turn_id": "T-RTYPED", "transcript": "t"},
        )
        # Committed, as the watcher saw them, but never submitted: it dropped the turn.
        append_open(conn, "RTYPED", attention_channel="voice_notify")
        append_chunk(conn, "RTYPED", 0, _SEGMENTS[0])
        append_emitted(conn, "RTYPED", _SEGMENTS[0])
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"channel": "inherent_ptt", "turn_id": "T-RSPOKEN", "transcript": "s"},
        )
        spoken = [
            append_open(conn, "RSPOKEN", attention_channel="voice_notify"),
            append_chunk(conn, "RSPOKEN", 0, _SEGMENTS[0]),
            append_emitted(conn, "RSPOKEN", _SEGMENTS[0]),
        ]
        with CallbackPump(player):
            asyncio.run(submit_response(pipeline, spoken))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert tts_rows(conn, "surface.playback_started", "RTYPED") == []
        assert len(tts_rows(conn, "surface.playback_started", "RSPOKEN")) == 1
    finally:
        conn.close()


def test_tag_before_playback_started_falls_back_to_emitted_time_speaking(
    tmp_path: Path,
) -> None:
    """A tagged chunk before any playback keeps the run on the emitted-time path."""
    db_path = tmp_path / "tag-before.db"
    conn = open_event_log(db_path)
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    try:
        with CallbackPump(player):
            rows = [
                append_open(conn, "RTB"),
                append_chunk(conn, "RTB", 0, "<voice>第一句。"),
                append_chunk(conn, "RTB", 1, "第二句。</voice>"),
            ]
            emitted_row = append_emitted(conn, "RTB", "第一句。第二句。")
            outcomes = asyncio.run(submit_response(pipeline, [*rows, emitted_row]))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * 4
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        started = tts_rows(conn, "surface.playback_started", "RTB")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "RTB")
        completed = tts_rows(conn, "surface.playback_completed", "RTB")
    finally:
        conn.close()
    assert len(started) == 1
    assert started[0][0] > emitted_row[0]
    assert "incremental" not in started[0][1]
    assert [row[1]["speech_text"] for row in prepared] == ["第一句。", "第二句。"]
    assert completed[0][1]["speech_text_hash"] == sha256_of("第一句。第二句。")


def test_tag_after_playback_started_fails_the_run_and_stops_segments(tmp_path: Path) -> None:
    """A tag after playback started stops segments with playback_failed(stream_chunk_tagged)."""
    db_path = tmp_path / "tag-after.db"
    conn = open_event_log(db_path)
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    try:
        with CallbackPump(player):
            first = [append_open(conn, "RTA"), append_chunk(conn, "RTA", 0, _SEGMENTS[0])]
            asyncio.run(submit_response(pipeline, first))
            wait_for_rows(conn, "surface.playback_segment_prepared", "RTA", 1)
            tagged = append_chunk(conn, "RTA", 1, "<document>不可朗读。</document>")
            assert asyncio.run(submit_response(pipeline, [tagged]))[0].status == "accepted"
            assert pipeline.wait_until_idle(timeout_s=2.0)
            late = append_emitted(conn, "RTA", _SEGMENTS[0] + "<document>不可朗读。</document>")
            assert asyncio.run(submit_response(pipeline, [late]))[0].status == "terminal"
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        failed = tts_rows(conn, "surface.playback_failed", "RTA")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "RTA")
    finally:
        conn.close()
    assert [row[1]["sequence"] for row in prepared] == [0]
    assert len(failed) == 1
    assert failed[0][1]["reason"] == "stream_chunk_tagged"
    assert failed[0][0] > prepared[0][0]


# --- Mid-stream cancel reaches the active playback --------------------------


def test_cancel_mid_stream_interrupts_playback_through_the_real_watcher(
    tmp_path: Path,
) -> None:
    """_tts_watcher feeds response.cancelled to the media owner, which interrupts."""
    db_path = tmp_path / "cancel.db"
    conn = open_event_log(db_path)
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    watcher_conn = open_event_log(db_path)

    async def _watch(until: str) -> None:
        watcher = asyncio.create_task(
            output_watchers.tts_watcher(
                conn=watcher_conn,
                pipeline=pipeline,
                poll_interval_s=0.005,
            ),
        )
        try:
            deadline = time.monotonic() + 2.0
            while not tts_rows(conn, until, "RC"):
                if time.monotonic() >= deadline:
                    pytest.fail(f"{until} never became durable")
                await asyncio.sleep(0.005)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    try:
        with CallbackPump(player):
            append_open(conn, "RC")
            append_chunk(conn, "RC", 0, _SEGMENTS[0])
            asyncio.run(_watch("surface.playback_segment_prepared"))
            cancel_row = append_cancel(conn, "RC")
            asyncio.run(_watch("surface.playback_interrupted"))
            assert pipeline.wait_until_idle(timeout_s=2.0)
            late = append_chunk(conn, "RC", 1, _SEGMENTS[1])
            assert asyncio.run(submit_response(pipeline, [late]))[0].status == "terminal"
            assert not pipeline._after_drain  # noqa: SLF001
    finally:
        assert pipeline.close()
        watcher_conn.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        interrupted = tts_rows(conn, "surface.playback_interrupted", "RC")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "RC")
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
    provider = FakeProvider(candidate_count=1)
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    try:
        with CallbackPump(player):
            first = [append_open(conn, "RA"), append_chunk(conn, "RA", 0, "第一句。")]
            asyncio.run(submit_response(pipeline, first))
            wait_for_rows(conn, "surface.playback_segment_prepared", "RA", 1)
            queued = emit_response(
                conn, response_id="RB", group_id="G-RA", turn_id="T-RA", text="排队的后续。"
            )
            statuses = [o.status for o in asyncio.run(submit_response(pipeline, queued))]
            assert statuses == ["accepted"] * 3
            assert len(pipeline._after_drain) == 1  # noqa: SLF001
            cancelled = asyncio.run(submit_response(pipeline, [append_cancel(conn, "RA")]))
            assert cancelled[0].status == "accepted"
            assert not pipeline._after_drain  # noqa: SLF001
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert len(tts_rows(conn, "surface.playback_interrupted", "RA")) == 1
        assert tts_rows(conn, "surface.playback_started", "RB") == []
    finally:
        conn.close()


def test_flag_off_cancel_drops_the_buffered_response(tmp_path: Path) -> None:
    """The terminal wiring is ungated: a cancelled run never speaks at emitted time."""
    db_path = tmp_path / "cancel-off.db"
    conn = open_event_log(db_path)
    pipeline, _ = make_pipeline(db_path, FakeProvider(), speak_from_segments=False)
    try:
        rows = [
            append_open(conn, "RD"),
            append_chunk(conn, "RD", 0, _SEGMENTS[0]),
            append_cancel(conn, "RD"),
            append_emitted(conn, "RD", _SEGMENTS[0]),
        ]
        outcomes = asyncio.run(submit_response(pipeline, rows))
        assert [o.status for o in outcomes] == ["accepted", "accepted", "accepted", "terminal"]
        assert pipeline.wait_until_idle(timeout_s=1.0)
    finally:
        assert pipeline.close()
        conn.close()
    conn = open_event_log(db_path)
    try:
        assert tts_rows(conn, "surface.playback_started", "RD") == []
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
        "silent_channels": output_watchers.TTS_SILENT_CHANNELS,
        "consumer": "test",
    }
    assert output_watchers.drop_for_silent_channel(opened, **kwargs)
    assert silent == {"TQ"}
    assert output_watchers.drop_for_silent_channel(cancelled, **kwargs)
    assert silent == set()


def test_a_typed_turn_is_never_played_but_a_spoken_one_is() -> None:
    """ADR 0181: the talk field's `cli_stdin` is silent like a Live relay; his voice is not."""
    opened = Event(
        event_uid="E-open",
        type="surface.response_open",
        schema_version=1,
        ts_epoch_ms=0,
        payload={"turn_id": "TT", "attention_channel": "voice_notify"},
        source_event_id=None,
        correlation={},
    )
    kwargs: dict[str, Any] = {
        "turn_id": "TT",
        "silent_turns": set(),
        "silent_channels": output_watchers.TTS_SILENT_CHANNELS,
        "consumer": "test",
    }
    drop = output_watchers.drop_for_silent_channel
    assert drop(opened, intent_channel="cli_stdin", **kwargs)
    assert not drop(opened, intent_channel="inherent_ptt", **kwargs)


# --- L5 media owner: the response budget is per segment, not per generation ---

_BUDGET_S = 1.0
_SEGMENT_DELAY_S = 0.4
_BUDGET_SEGMENTS = ("第一句话。", "第二句话。", "第三句话。", "第四句话。")


def _budget_pipeline(
    db_path: Path,
    *,
    segment_delay_s: float,
) -> tuple[voice_media.StreamingTTSPipeline, voice_tts.AudioStreamPlayer, FakeProvider]:
    """A non-live pipeline whose provider spends ``segment_delay_s`` on every segment."""
    provider = FakeProvider(candidate_count=1)
    provider.behaviors[("RB", 0)] = Behavior(final_delay_s=segment_delay_s)
    pipeline, player = make_pipeline(
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
        with CallbackPump(player):
            rows = [append_open(conn, "RB")]
            rows += [append_chunk(conn, "RB", index, text) for index, text in enumerate(texts)]
            rows.append(append_emitted(conn, "RB", "".join(texts)))
            asyncio.run(submit_response(pipeline, rows))
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
        activation = tts_rows(conn, "surface.playback_started", "RB")
        completed = tts_rows(conn, "surface.playback_completed", "RB")
        failed = tts_rows(conn, "surface.playback_failed", "RB")
        prepared = tts_rows(conn, "surface.playback_segment_prepared", "RB")
    finally:
        conn.close()
    assert elapsed > _BUDGET_S
    # No `incremental` flag on the activation is the observable proof this ran
    # the non-live path, the one the budget used to bound as a whole.
    assert "incremental" not in activation[0][1]
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
        failed = tts_rows(conn, "surface.playback_failed", "RB")
        assert tts_rows(conn, "surface.playback_completed", "RB") == []
    finally:
        conn.close()
    assert len(failed) == 1
    assert failed[0][1]["reason"] == "tts_response_timeout"
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert any("RB" in message and "response_timeout_s" in message for message in warnings)


def test_a_late_response_terminal_cannot_double_release_a_committing_playback(
    tmp_path: Path,
) -> None:
    """The owner's live window: L3's terminal lands while media is committing one.

    With ``speak_from_segments: true`` playback runs while the L3 run is still
    open, so a provider failure and an L3 ``response.failed`` can overlap.  The
    second exit would cancel ``_play_response`` inside its own ``finally`` and
    broadcast a contradictory ``spoken`` frame after the CAS said AlreadyTerminal.
    """
    db_path = tmp_path / "late-response-terminal.db"
    conn = open_event_log(db_path)
    provider = FakeProvider(
        {("RG", 0): Behavior("fail_after", final_delay_s=0.01)},
        candidate_count=1,
    )
    actions: list[str] = []
    pipeline, player = make_pipeline(
        db_path,
        provider,
        speak_from_segments=True,
        broadcaster=RecordingBroadcaster(actions),
    )
    real_settle = player.settle_interrupted_generation
    settle_allowed = threading.Event()
    snapshot_unsettled = threading.Event()

    def _gated_settle(**kwargs: Any) -> Any:  # noqa: ANN401 - passthrough seam
        # The real settle waits for the device callback, which runs on its own thread. This
        # one waits for the test's gate, and each ask waits on it a moment: the actor
        # spins on asking, and a spin that never lets go of the interpreter starves the
        # submitting thread past the snapshot's own half-second bound.
        snapshot_unsettled.set()
        if not settle_allowed.wait(_POLL_S):
            return None
        return real_settle(**kwargs)

    submitted: list[str] = []
    failed_row: list[tuple[int, Event]] = []

    def _submit_late_terminal() -> None:
        row_id, event = failed_row[0]
        outcome = asyncio.run(
            pipeline.submit_event(row_id=row_id, event=event, origin="direct"),
        )
        submitted.append(outcome.status)

    late = threading.Thread(target=_submit_late_terminal, name="late-response-terminal")
    try:
        with (
            CallbackPump(player),
            patch.object(player, "settle_interrupted_generation", side_effect=_gated_settle),
        ):
            first = [append_open(conn, "RG"), append_chunk(conn, "RG", 0, _SEGMENTS[0])]
            asyncio.run(submit_response(pipeline, first))
            wait_for_rows(conn, "surface.playback_started", "RG", 1)
            # L3 ends the run independently, after every row this response has
            # already produced, so the actor sees it as a fresh command.
            failed_row.append(
                row_of(
                    conn,
                    emit_event(
                        conn,
                        type="response.failed",
                        payload={
                            "response_id": "RG",
                            "response_group_id": "G-RG",
                            "turn_id": "T-RG",
                            "reason": "l3_run_failed_in_the_same_window",
                        },
                    ),
                ),
            )
            # The media side owns an exit already: `_fail_active` has published
            # `terminal_commit_pending` and is spinning in `_interrupt_snapshot`,
            # which asks the gated settle again and again.
            assert snapshot_unsettled.wait(_GUARD_S), "media never reached its own terminal commit"
            late.start()
            # The actor's drain loop has taken the `response.failed` command once
            # its submit is answered, and the gate is still shut: the answer came
            # from inside the commit window. Only then let the snapshot settle.
            late.join(timeout=_GUARD_S)
            assert not late.is_alive(), "the late terminal was not answered during the commit"
            settle_allowed.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        settle_allowed.set()
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()

    assert submitted == ["accepted"]
    spoken = [action for action in actions if action.startswith("ui:spoken:T-RG:")]
    assert spoken == ["ui:spoken:T-RG:failed"]
    verdict = open_event_log(db_path)
    try:
        terminals = [
            kind
            for kind in (
                "surface.playback_completed",
                "surface.playback_interrupted",
                "surface.playback_failed",
            )
            if tts_rows(verdict, kind, "RG")
        ]
        assert terminals == ["surface.playback_failed"]
        assert tts_rows(verdict, "surface.playback_lane_isolated", "RG") == []
    finally:
        verdict.close()
