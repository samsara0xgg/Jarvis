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
from tests.integration.test_incremental_tts import _chunk, _emitted, _open, _pipeline, _rows
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _FakeProvider,
    _RecordingBroadcaster,
    _submit_response,
)
from tests.integration.test_wave4a_response_run import (
    _emit_intent,
    _make_runtime,
    _open_run,
    _script_decide,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_a_turn_still_thinking_is_stopped_by_its_turn(tmp_path: Path) -> None:
    """``turn_id`` cancels that turn's open runs whole, and only that turn's."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    thinking = _open_run(runtime, response_id="RESP-think", turn_id="T-think")
    other = _open_run(runtime, response_id="RESP-other", turn_id="T-other")
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


def test_a_stop_before_the_run_opens_still_stops_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressed after turn.started but before the run opens: no model, no words."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    intent = _emit_intent(runtime.conn, "T-early", "Explain how a transformer works.")
    asked: list[str] = []
    _script_decide(monkeypatch, lambda *_a, **_k: asked.append("decide"))
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
    assert asked == []
    assert rows == [("response.cancelled", "user_stop")]
    assert not wait_line_owed
    assert leaked == ()
    assert again == "no_open_run"


def test_a_stop_drops_an_answer_parked_while_allen_talks(tmp_path: Path) -> None:
    """The answer she shows but has not started is never said once stopped."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)
    actions: list[str] = []
    pipeline, player = _pipeline(
        db_path,
        _FakeProvider(candidate_count=1),
        speak_from_segments=True,
        broadcaster=_RecordingBroadcaster(actions),
    )
    try:
        with _CallbackPump(player):
            opened = _open(conn, "RP")
            asyncio.run(_submit_response(pipeline, [opened]))
            pipeline.hold_output(held=True)  # Allen coughs as the answer comes
            rest = [_chunk(conn, "RP", 0, "会议改到三点。"), _emitted(conn, "RP", "会议改到三点。")]
            asyncio.run(_submit_response(pipeline, rest))
            time.sleep(0.2)
            parked = _rows(conn, "surface.playback_started", "RP")
            outcome = pipeline.stop_foreground_output("RP")
            pipeline.hold_output(held=False)  # his cough comes to nothing
            time.sleep(0.2)
            assert pipeline.wait_until_idle(timeout_s=2.0)
        started = _rows(conn, "surface.playback_started", "RP")
    finally:
        assert pipeline.close()
        conn.close()
    assert parked == []
    assert outcome == "applied"
    assert started == []
    assert "ui:spoken:T-RP:dropped" in actions
