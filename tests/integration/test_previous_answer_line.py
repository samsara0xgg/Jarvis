"""The next turn is told exactly how much of the last spoken answer Allen heard.

The heard prefix is the concatenation of the prepared speech segments, and a
segment is the chunk after sentence stripping and speech cleanup: "It is 3
PM. " plays as "It is 3 PM.". The next turn compared that prefix with the
raw voice text, so an English answer heard whole came back as `interrupted
after "…"`, and the model apologised for or repeated words Allen had heard.
The opposite held for an answer cancelled before its first sample: with no
callback there was no cursor, and no cursor read as heard whole. And only
the turn right before was looked at, so a half-sentence dropped for the one
after it hid the cut answer before it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import _previous_answer_line
from jarvis.decision.packet import assemble_packet
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_incremental_tts import (
    _cancel,
    _chunk,
    _emitted,
    _open,
    _pipeline,
    _wait_for,
)
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _emit_response,
    _FakeProvider,
    _submit_response,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _asked(conn: sqlite3.Connection, turn_id: str) -> None:
    emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": turn_id, "transcript": "synthetic", "channel": "inherent_wake"},
    )


def _next_turn_line(conn: sqlite3.Connection) -> str | None:
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": "next", "transcript": "and?", "channel": "inherent_wake"},
    )
    packet = assemble_packet(trigger, conn)
    return _previous_answer_line(packet)


@pytest.mark.parametrize(
    "chunks",
    [
        ("It is 3 PM. ", "The meeting starts at four."),
        ("三点了。\n", "会议**四点**开始。"),
    ],
)
def test_an_answer_played_to_its_end_is_not_reported_interrupted(
    tmp_path: Path, chunks: tuple[str, str]
) -> None:
    """Whitespace between sentences and stripped markup are not "the rest"."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    pipeline, player = _pipeline(
        db_path, _FakeProvider(candidate_count=1), speak_from_segments=True
    )
    try:
        _asked(conn, "T-RP")
        with _CallbackPump(player):
            rows = [_open(conn, "RP")]
            rows += [_chunk(conn, "RP", index, text) for index, text in enumerate(chunks)]
            rows.append(_emitted(conn, "RP", "".join(chunks)))
            asyncio.run(_submit_response(pipeline, rows))
            _wait_for(conn, "surface.playback_completed", "RP", 1)
            assert pipeline.wait_until_idle(timeout_s=2.0)
        assert _next_turn_line(conn) is None
    finally:
        assert pipeline.close()
        conn.close()


def _play_cut(conn: sqlite3.Connection, db_path: Path, *, gated: int) -> None:
    """Allen asks; the answer's segment ``gated`` never gets audio and is cancelled."""
    provider = _FakeProvider(candidate_count=1)
    provider.segment_gates[("RP", gated)] = threading.Event()  # never opens
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    try:
        _asked(conn, "T-RP")
        with _CallbackPump(player):
            rows = [_open(conn, "RP"), _chunk(conn, "RP", 0, "It is 3 PM. ")]
            rows.append(_chunk(conn, "RP", 1, "The meeting starts at four."))
            asyncio.run(_submit_response(pipeline, rows))
            if gated:
                _wait_for(conn, "surface.playback_checkpoint", "RP", 1)
            else:
                _wait_for(conn, "surface.playback_segment_prepared", "RP", 1)
            asyncio.run(_submit_response(pipeline, [_cancel(conn, "RP")]))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()


_CUT = 'Previous answer: interrupted after "It is 3 PM."; the rest was not spoken'


def test_an_answer_cut_after_its_first_sentence_still_says_where(tmp_path: Path) -> None:
    """The heard-whole fix does not hide a real interruption: the heard sentence is quoted."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    try:
        _play_cut(conn, db_path, gated=1)
        assert _next_turn_line(conn) == _CUT
    finally:
        conn.close()


def test_an_answer_cancelled_before_its_first_sample_was_not_heard(tmp_path: Path) -> None:
    """No sample submitted, no callback, no cursor quality: still nothing was spoken."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    try:
        _play_cut(conn, db_path, gated=0)
        line = _next_turn_line(conn)
    finally:
        conn.close()
    assert line == "Previous answer: interrupted before any of it was spoken"


def test_a_turn_dropped_unanswered_does_not_hide_the_cut_before_it(tmp_path: Path) -> None:
    """Allen cuts in with a half-sentence that gets no answer, then finishes his thought."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    try:
        _play_cut(conn, db_path, gated=1)
        _asked(conn, "T-half")
        assert _next_turn_line(conn) == _CUT
    finally:
        conn.close()


def test_an_answer_given_after_the_cut_ends_the_look_back(tmp_path: Path) -> None:
    """Only the last answer counts: a later one Allen got makes the old cut history."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    try:
        _play_cut(conn, db_path, gated=1)
        _asked(conn, "T-later")
        _emit_response(conn, response_id="RL", group_id="GL", turn_id="T-later", text="Sure.")
        assert _next_turn_line(conn) is None
    finally:
        conn.close()
