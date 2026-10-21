"""ADR 0138: leaving conversation mode from the surface is a dismissal said without words.

The companion's exit turned the mode off and named one response to stop, and the
answer that was audible kept talking. Now ``POST /inherent/controls`` taking
the mode from on to off ends every answer still being written, stops what is
audible and drops what is queued, through the same session callbacks as a spoken
退下, but says nothing back (yilun 2026-10-04).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

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
from tests.support.barge_in import BargeInRig
from tests.support.media import (
    CallbackPump,
    FakeProvider,
    await_playback_started,
    emit_response,
    make_player,
    media_config,
    playback_rows_for,
    submit_response,
)
from tests.support.response_run import LLM_CONFIG, make_runtime
from tests.support.tts import append_chunk, append_open, make_pipeline

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
    provider = FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RA", 0)] = hold
    player = make_player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=media_config(),
        start_player=False,
    )

    rig = BargeInRig(
        tmp_path,
        "",
        output_active=pipeline.is_output_active,
        stop_speaking=lambda: pipeline.stop_foreground_output(None, reason="barge_in"),
    )
    try:
        with CallbackPump(player):
            older = emit_response(
                conn, response_id="RA", group_id="G", turn_id="TA", text="这是一个很长的回答。",
            )
            asyncio.run(submit_response(pipeline, older))
            await_playback_started(conn, "RA")
            wanted = emit_response(
                conn, response_id="RB", group_id="G", turn_id="TB", text="你想要的回答。",
            )
            asyncio.run(submit_response(pipeline, wanted))
            assert playback_rows_for(conn, "RB", "surface.playback_started") == 0

            controls = VoiceControls(conversation=True)
            reply = _app(controls, rig.session.dismiss).post(
                "/inherent/controls", json={"conversation": False},
            )
            assert reply.json()["conversation"] is False
            hold.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
        interrupted = playback_rows_for(conn, "RA", "surface.playback_interrupted")
        queued_started = playback_rows_for(conn, "RB", "surface.playback_started")
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
    phase: str = "final",
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
    factory = LLMSessionFactory(LLM_CONFIG)
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
        phase=phase,  # type: ignore[arg-type]
    )
    assert runtime.response_runs is not None
    runtime.response_runs.register(run)
    return run


def _answer_started(runtime: JarvisRuntime, turn_id: str) -> None:
    """The turn's answer opened on the wire, so it has started (a chunk is on its way)."""
    emit_event(
        runtime.conn,
        type="surface.response_open",
        payload={"turn_id": turn_id, "query": "q", "kind": "text"},
    )


def test_exit_cancels_every_open_run_that_would_speak_and_leaves_the_rest(
    tmp_path: Path,
) -> None:
    """Whatever their age or number; a document, a silent channel and a background run stay.

    A Live relay and a typed turn (ADR 0181) are the silent channels: nothing of them speaks.
    A spoken turn whose answer has not started stays too (ADR 0269): it answers in text.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    _run(runtime, "R-voice-old", "T-old")
    _answer_started(runtime, "T-old")
    _run(runtime, "R-voice-new", "T-new")
    _run(runtime, "R-typed", "T-typed", trigger="surface.user_intent", intent_channel="cli_stdin")
    _run(runtime, "R-document", "T-doc", channel="document")
    _run(runtime, "R-live", "T-live", trigger="surface.user_intent", intent_channel="gpt_live")
    _run(runtime, "R-sweep", "T-sweep", trigger=None)

    inherent_loop._make_cancel_voice_runs(runtime)()  # noqa: SLF001

    assert {run.response_id for run in runtime.response_runs.open_runs()} == {
        "R-voice-new", "R-document", "R-live", "R-sweep", "R-typed",
    }
    cancelled = {
        (row[0], row[1])
        for row in runtime.conn.execute(
            "SELECT json_extract(payload_json, '$.response_id'), "
            "json_extract(payload_json, '$.reason') FROM events WHERE type = 'response.cancelled'",
        )
    }
    assert cancelled == {("R-voice-old", "user_stop")}
    # A run of one of these turns that opens later is cancelled at its open.
    assert runtime.response_runs.turn_stopped("T-old")
    assert not runtime.response_runs.turn_stopped("T-live")
    assert runtime.response_runs.turn_text_only("T-new")
    assert not runtime.response_runs.turn_stopped("T-new")

def test_a_run_still_generating_at_exit_never_reaches_the_speaker(tmp_path: Path) -> None:
    """Its first sentence is audible, the rest unwritten: exit stops it and it cannot go on.

    A second turn that had said nothing yet is not stopped with it (ADR 0269).
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    db_path = runtime.runtime_paths.event_log
    run = _run(runtime, "RG", "T-RG")
    other = _run(runtime, "R-other", "T-other")
    provider = FakeProvider(candidate_count=1)
    hold = threading.Event()
    provider.final_gates[("RG", 0)] = hold
    pipeline, player = make_pipeline(db_path, provider, speak_from_segments=True)
    conn = open_event_log(db_path)
    rig = BargeInRig(
        tmp_path,
        "",
        output_active=pipeline.is_output_active,
        stop_speaking=lambda: pipeline.stop_foreground_output(None, reason="barge_in"),
        cancel_voice_runs=inherent_loop._make_cancel_voice_runs(runtime),  # noqa: SLF001
    )
    try:
        with CallbackPump(player):
            asyncio.run(submit_response(pipeline, [append_open(conn, "RG")]))
            asyncio.run(submit_response(pipeline, [append_chunk(conn, "RG", 0, "会议改到三点。")]))
            await_playback_started(conn, "RG")

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
        interrupted = playback_rows_for(conn, "RG", "surface.playback_interrupted")
        completed = playback_rows_for(conn, "RG", "surface.playback_completed")
    finally:
        hold.set()
        rig.close()
        assert pipeline.close()
        conn.close()
    assert (interrupted, completed) == (1, 0)
    # No further segment can be written, and a later run of the turn is cancelled at its open.
    with pytest.raises(ResponseCancelledError):
        run.check_cancelled("next sentence")
    # The other turn had not started answering (ADR 0269): it goes on, in text.
    other.check_cancelled("next sentence")
    assert runtime.response_runs.turn_stopped("T-RG")
    assert [run.response_id for run in runtime.response_runs.open_runs()] == ["R-other"]



class _Submitted:
    """A media actor that only records the response rows the watcher hands it."""

    def __init__(self, boot_high_water_event_log_id: int) -> None:
        self.boot_high_water_event_log_id = boot_high_water_event_log_id
        self.rows: list[tuple[str, str]] = []

    async def submit_event(self, *, row_id: int, event: Any, origin: str) -> SimpleNamespace:  # noqa: ANN401 - Event
        del row_id, origin
        self.rows.append((str(event.type), str(event.payload["turn_id"])))
        return SimpleNamespace(status="accepted")


class _Wire:
    """The ``voice spoken`` words the TTS watcher puts on the wire, in order."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, object]] = []

    async def broadcast_voice(self, _phase: str, *, turn_id: str, **payload: object) -> None:
        self.spoken.append((turn_id, payload.get("output_outcome")))


def test_leaving_voice_mode_keeps_a_spoken_turn_that_has_not_started_answering(
    tmp_path: Path,
) -> None:
    """Live 2026-10-14 21:18: the companion's exit cancelled a sentence he had just spoken.

    The turn's run stays open and its answer reaches the screen, but the speaker is given
    none of it; the wait line of the same turn and an answer already under way are ended.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    registry = runtime.response_runs
    assert registry is not None
    pending = _run(runtime, "R-pending", "T-pending")
    _run(runtime, "R-wait", "T-pending", phase="commentary")
    started = _run(runtime, "R-started", "T-started")
    _answer_started(runtime, "T-started")

    cancel_runs = inherent_loop._make_cancel_voice_runs(runtime)  # noqa: SLF001
    reply = _app(VoiceControls(conversation=True), cancel_runs).post(
        "/inherent/controls", json={"conversation": False},
    )
    assert reply.json()["conversation"] is False

    assert {run.response_id for run in registry.open_runs()} == {"R-pending"}
    pending.check_cancelled("next sentence")
    with pytest.raises(ResponseCancelledError):
        started.check_cancelled("next sentence")
    assert not registry.turn_stopped("T-pending")

    wire = _Wire()
    pipeline = _Submitted(inherent_loop._latest_id(runtime.conn))  # noqa: SLF001

    def answer() -> None:
        for kind in ("response_open", "response_chunk", "response_emitted"):
            emit_event(
                runtime.conn,
                type=f"surface.{kind}",
                payload={"turn_id": "T-pending", "query": "q", "kind": "text", "text": "好。"},
            )

    async def watch() -> None:
        task = asyncio.create_task(
            inherent_loop._tts_watcher(  # noqa: SLF001 - the production watcher
                conn=runtime.conn, poll_interval_s=0.01,
                pipeline=pipeline,  # type: ignore[arg-type]
                broadcaster=wire,  # type: ignore[arg-type]
                text_only=registry.turn_text_only,
            ),
        )
        await asyncio.sleep(0.05)
        answer()
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(watch())
    assert pipeline.rows == []
    assert wire.spoken == [("T-pending", "suppressed")]
