"""ADR 0138: leaving conversation mode from the surface is a dismissal said without words.

The companion's exit turned the mode off and named one response to stop, and the
answer that was audible kept talking. Now ``POST /inherent/controls`` taking
the mode from on to off stops what is audible, drops what is queued, and says
the goodbye line, through the same session callbacks as a spoken 退下.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime import inherent_loop
from jarvis.shared import lang
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_media
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_lifecycle_commentary import _make_runtime, _only_phrase
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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


def test_exit_stops_the_audible_answer_drops_the_queued_one_and_says_goodbye(
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

    def _goodbye(turn_id: str, reason: str, _text: str) -> None:
        assert reason == "dismissed"
        with contextlib.closing(open_event_log(db_path)) as own:  # her thread, her connection
            line = _emit_response(
                own, response_id="RC", group_id="GBYE", turn_id=turn_id, text="好的 我先退下了。",
                phase="commentary",
            )
        asyncio.run(_submit_response(pipeline, line))

    rig = _Rig(
        tmp_path,
        "",
        output_active=pipeline.is_output_active,
        stop_speaking=lambda: pipeline.stop_foreground_output(None, reason="barge_in"),
        answer_words=_goodbye,
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
            _await_playback_started(conn, "RC")
            hold.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
        interrupted = _playback_rows_for(conn, "RA", "surface.playback_interrupted")
        queued_started = _playback_rows_for(conn, "RB", "surface.playback_started")
        goodbye_started = _playback_rows_for(conn, "RC", "surface.playback_started")
    finally:
        hold.set()
        rig.close()
        assert pipeline.close()
        conn.close()
    assert (interrupted, queued_started, goodbye_started) == (1, 0, 1)
    assert rig.output == ["supersede"]


def test_the_goodbye_of_an_exit_with_no_words_is_the_dismissal_line(tmp_path: Path) -> None:
    """No words to take a language from: the system language's wording, as for any fixed line."""
    runtime = _make_runtime(tmp_path, commentary=False)

    inherent_loop._say_conversation_line(runtime, "T-exit", "dismissed", "")  # noqa: SLF001

    assert _only_phrase(runtime.conn) in lang.variants("conversation.dismissed")
