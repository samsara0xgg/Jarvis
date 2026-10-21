"""A stop reaches whatever answer the companion shows, audible yet or not.

The companion shows an answer from its ``open``, which leaves once the whole
answer exists (ADR 0108). Two stops missed it:

- While Jarvis was still thinking there was no response id to name, so the
  stop named the previous answer's and the new one was spoken anyway.
- An answer parked while Allen talked (ADR 0053), or queued behind another,
  was on screen but not the playing one, so ``foreground_output`` answered
  ``stale`` and the answer played as soon as the hold lifted.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import drive_turn, make_response_cancel_callable, make_turn_cancel_callable
from jarvis.runtime.inherent_loop import _turn_over_or_answering
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.support.media import CallbackPump, FakeProvider, RecordingBroadcaster, submit_response
from tests.support.model import ScriptedModel
from tests.support.response_run import emit_intent, make_runtime, open_run
from tests.support.tts import append_chunk, append_emitted, append_open, make_pipeline, tts_rows

if TYPE_CHECKING:
    from pathlib import Path



def test_a_turn_still_thinking_is_stopped_by_its_turn(tmp_path: Path) -> None:
    """``turn_id`` cancels that turn's open runs whole, and only that turn's."""
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    thinking = open_run(runtime, response_id="RESP-think", turn_id="T-think")
    other = open_run(runtime, response_id="RESP-other", turn_id="T-other")
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T-http",
            broadcaster=InherentBroadcaster(),
            cancel_response_callable=make_response_cancel_callable(runtime),
            cancel_turn_callable=make_turn_cancel_callable(runtime),
        ),
    )
    try:
        with TestClient(app) as client:
            stop = {"turn_id": "T-think", "reason": "user_stop"}
            first = client.post("/inherent/cancel-response", json=stop).json()
            again = client.post("/inherent/cancel-response", json=stop).json()
            neither = client.post("/inherent/cancel-response", json={"reason": "user_stop"})
        cancelled = runtime.conn.execute(
            "SELECT json_extract(payload_json, '$.response_id'), "
            "json_extract(payload_json, '$.reason') FROM events "
            "WHERE type = 'response.cancelled'",
        ).fetchall()
    finally:
        runtime.conn.close()
    assert first == {"outcome": "cancelled"}
    assert again == {"outcome": "no_open_run"}
    assert neither.status_code == 422
    assert cancelled == [("RESP-think", "user_stop")]
    assert thinking.cancellation_token.is_cancelled
    assert not other.cancellation_token.is_cancelled


def test_a_stop_before_the_run_opens_still_stops_the_turn(tmp_path: Path) -> None:
    """Pressed after turn.started but before the run opens: no model, no words."""
    model = ScriptedModel()  # asked once, it fails the test
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True, model=model)
    intent = emit_intent(runtime.conn, "T-early", "Explain how a transformer works.")
    try:
        outcome = make_turn_cancel_callable(runtime)("T-early", "user_stop")
        with pytest.raises(ResponseCancelledError):
            drive_turn(
                runtime, user_intent_event=intent, available_surfaces=frozenset(),
                streaming_enabled=True,
            )
        rows = runtime.conn.execute(
            "SELECT type, json_extract(payload_json, '$.reason') FROM events "
            "WHERE type IN ('response.cancelled', 'response.completed', 'turn.ended', "
            "'surface.response_chunk', 'surface.playback_started')",
        ).fetchall()
        wait_line_owed = not _turn_over_or_answering(runtime.conn, "T-early")
        assert runtime.response_runs is not None
        leaked = runtime.response_runs.open_runs()
        again = make_turn_cancel_callable(runtime)("T-early", "user_stop")
    finally:
        runtime.conn.close()
    assert outcome == "stopped_before_start"
    assert model.requests == []
    assert rows == [("response.cancelled", "user_stop")]
    assert not wait_line_owed
    assert leaked == ()
    assert again == "no_open_run"


def test_a_stop_drops_an_answer_parked_while_allen_talks(tmp_path: Path) -> None:
    """The answer she shows but has not started is never said once stopped."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    actions: list[str] = []
    pipeline, player = make_pipeline(
        db_path,
        FakeProvider(candidate_count=1),
        speak_from_segments=True,
        broadcaster=RecordingBroadcaster(actions),
    )
    try:
        with CallbackPump(player):
            opened = append_open(conn, "RP")
            asyncio.run(submit_response(pipeline, [opened]))
            pipeline.hold_output(held=True)  # Allen coughs as the answer comes
            rest = [
                append_chunk(conn, "RP", 0, "会议改到三点。"),
                append_emitted(conn, "RP", "会议改到三点。"),
            ]
            asyncio.run(submit_response(pipeline, rest))
            time.sleep(0.2)
            parked = tts_rows(conn, "surface.playback_started", "RP")
            outcome = pipeline.stop_foreground_output("RP")
            pipeline.hold_output(held=False)  # his cough comes to nothing
            time.sleep(0.2)
            assert pipeline.wait_until_idle(timeout_s=2.0)
        started = tts_rows(conn, "surface.playback_started", "RP")
    finally:
        assert pipeline.close()
        conn.close()
    assert parked == []
    assert outcome == "applied"
    assert started == []
    assert "ui:spoken:T-RP:dropped" in actions
