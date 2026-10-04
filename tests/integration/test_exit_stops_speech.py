"""ADR 0138: leaving conversation mode from the surface is a dismissal said without words.

The companion's exit turned the mode off and named one response to stop, and the
answer that was audible kept talking. Now ``POST /inherent/controls`` taking
the mode from on to off ends every answer still being written, stops what is
audible and drops what is queued, through the same session callbacks as a spoken
退下, but says nothing back (yilun 2026-10-04).
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.response_run import (
    ResponseCancelledError,
    legacy_full_text_policy,
    start_response_run,
)
from jarvis.runtime import inherent_loop
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_media
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_incremental_tts import _chunk, _open, _pipeline
from tests.integration.test_soft_barge_in import _Rig
from tests.integration.test_wave2_streaming_media import (
    _await_playback_started,
    _CallbackPump,
    _config,
    _emit_response,
    _FakeProvider,
    _playback_rows_for,
    _player,
    _submit_response,
)
from tests.integration.test_wave4a_response_run import _LLM_CONFIG, _make_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.decision.response_run import ResponseRun
    from jarvis.runtime import JarvisRuntime


def _app(controls: VoiceControls, dismiss: Callable[[], None]) -> TestClient:
    return TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                controls=controls,
                dismiss_callable=dismiss,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("before", "body", "dismissed"),
    [
        (True, {"conversation": False}, 1),
        (True, {"conversation": True}, 0),
        (False, {"conversation": False}, 0),
        (False, {"conversation": True}, 0),
        (True, {"mic_muted": True}, 0),
    ],
)
def test_only_conversation_going_from_on_to_off_dismisses(
    before: bool, body: dict[str, bool], dismissed: int,  # noqa: FBT001 - pytest parameter
) -> None:
    """The exit is the on-to-off edge; voice's own flips never come through this route."""
    calls: list[str] = []
    client = _app(VoiceControls(conversation=before), lambda: calls.append("dismiss"))
    assert client.post("/inherent/controls", json=body).status_code == 200
    assert len(calls) == dismissed


def test_a_failing_dismissal_still_answers_the_flip() -> None:
    """The mode is off either way; the surface needs the state back."""
    def _boom() -> None:
        raise RuntimeError

    reply = _app(VoiceControls(conversation=True), _boom).post(
        "/inherent/controls", json={"conversation": False},
    )
    assert reply.status_code == 200
    assert reply.json()["conversation"] is False


def test_exit_stops_the_audible_answer_drops_the_queued_one_and_says_nothing(
    tmp_path: Path,
) -> None:
    """One answer audible, another queued behind it, the surface turns the mode off."""
    db_path = tmp_path / "exit.db"
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

    rig = _Rig(
        tmp_path,
        "",
        output_active=pipeline.is_output_active,
        stop_speaking=lambda: pipeline.stop_foreground_output(None, reason="barge_in"),
    )
    try:
        with _CallbackPump(player):
            older = _emit_response(
                conn, response_id="RA", group_id="G", turn_id="TA", text="这是一个很长的回答。",
            )
            asyncio.run(_submit_response(pipeline, older))
            _await_playback_started(conn, "RA")
            wanted = _emit_response(
                conn, response_id="RB", group_id="G", turn_id="TB", text="你想要的回答。",
            )
            asyncio.run(_submit_response(pipeline, wanted))
            assert _playback_rows_for(conn, "RB", "surface.playback_started") == 0

            controls = VoiceControls(conversation=True)
            reply = _app(controls, rig.session.dismiss).post(
                "/inherent/controls", json={"conversation": False},
            )
            assert reply.json()["conversation"] is False
            hold.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
        interrupted = _playback_rows_for(conn, "RA", "surface.playback_interrupted")
        queued_started = _playback_rows_for(conn, "RB", "surface.playback_started")
    finally:
        hold.set()
        rig.close()
        assert pipeline.close()
        conn.close()
    assert (interrupted, queued_started) == (1, 0)
    assert rig.answers == []
    assert rig.output == ["cancel runs"]


def _run(  # noqa: PLR0913 - one run's trigger, channel and turn
    runtime: JarvisRuntime,
    response_id: str,
    turn_id: str,
    *,
    trigger: str | None = "utterance.received",
    intent_channel: str = "inherent_wake",
    channel: str = "both",
) -> ResponseRun:
    """An open, registered run whose turn Allen's words started (``trigger=None``: nobody's)."""
    event = emit_event(
        runtime.conn,
        type=trigger or "surface.conversation_words",
        payload={
            "transcript": "你好", "turn_id": turn_id, "channel": intent_channel, "reason": "wait",
        },
        correlation={"turn_id": turn_id},
    )
    factory = LLMSessionFactory(_LLM_CONFIG)
    snapshot = factory.snapshot(None)
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=event.event_uid,
        request_client=factory.create(snapshot, response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash="e", preset_snapshot_hash=snapshot.snapshot_hash,
        ),
        response_id=response_id,
        channel=channel,  # type: ignore[arg-type]
    )
    assert runtime.response_runs is not None
    runtime.response_runs.register(run)
    return run


def test_exit_cancels_every_open_run_that_would_speak_and_leaves_the_rest(
    tmp_path: Path,
) -> None:
    """Whatever their age or number; a document, a silent channel and a background run stay."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    _run(runtime, "R-voice-old", "T-old")
    _run(runtime, "R-voice-new", "T-new")
    _run(runtime, "R-typed", "T-typed", trigger="surface.user_intent", intent_channel="cli_stdin")
    _run(runtime, "R-document", "T-doc", channel="document")
    _run(runtime, "R-live", "T-live", trigger="surface.user_intent", intent_channel="gpt_live")
    _run(runtime, "R-sweep", "T-sweep", trigger=None)

    inherent_loop._make_cancel_voice_runs(runtime)()  # noqa: SLF001

    assert {run.response_id for run in runtime.response_runs.open_runs()} == {
        "R-document", "R-live", "R-sweep",
    }
    cancelled = {
        (row[0], row[1])
        for row in runtime.conn.execute(
            "SELECT json_extract(payload_json, '$.response_id'), "
            "json_extract(payload_json, '$.reason') FROM events WHERE type = 'response.cancelled'",
        )
    }
    assert cancelled == {
        ("R-voice-old", "user_stop"), ("R-voice-new", "user_stop"), ("R-typed", "user_stop"),
    }
    # A run of one of these turns that opens later is cancelled at its open.
    assert runtime.response_runs.turn_stopped("T-old")
    assert not runtime.response_runs.turn_stopped("T-live")

def test_a_run_still_generating_at_exit_never_reaches_the_speaker(tmp_path: Path) -> None:
    """Its first sentence is audible, the rest unwritten: exit stops it and it cannot go on."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    db_path = runtime.runtime_paths.event_log
    run = _run(runtime, "RG", "T-RG")
    other = _run(runtime, "R-other", "T-other")
    provider = _FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RG", 0)] = hold
    pipeline, player = _pipeline(db_path, provider, speak_from_segments=True)
    conn = open_event_log(db_path)
    rig = _Rig(
        tmp_path,
        "",
        output_active=pipeline.is_output_active,
        stop_speaking=lambda: pipeline.stop_foreground_output(None, reason="barge_in"),
        cancel_voice_runs=inherent_loop._make_cancel_voice_runs(runtime),  # noqa: SLF001
    )
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, [_open(conn, "RG")]))
            asyncio.run(_submit_response(pipeline, [_chunk(conn, "RG", 0, "会议改到三点。")]))
            _await_playback_started(conn, "RG")

            reply = _app(VoiceControls(conversation=True), rig.session.dismiss).post(
                "/inherent/controls", json={"conversation": False},
            )
            assert reply.json()["conversation"] is False
            cancelled = conn.execute(
                "SELECT json_extract(payload_json, '$.reason') FROM events "
                "WHERE type = 'response.cancelled' "
                "AND json_extract(payload_json, '$.response_id') = 'RG'",
            ).fetchall()
            assert cancelled == [("user_stop",)]
            hold.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
        interrupted = _playback_rows_for(conn, "RG", "surface.playback_interrupted")
        completed = _playback_rows_for(conn, "RG", "surface.playback_completed")
    finally:
        hold.set()
        rig.close()
        assert pipeline.close()
        conn.close()
    assert (interrupted, completed) == (1, 0)
    # No further segment can be written, and a later run of the turn is cancelled at its open.
    with pytest.raises(ResponseCancelledError):
        run.check_cancelled("next sentence")
    with pytest.raises(ResponseCancelledError):
        other.check_cancelled("next sentence")
    assert runtime.response_runs.turn_stopped("T-RG")
    assert not runtime.response_runs.open_runs()

