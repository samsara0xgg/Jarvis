"""Every answer the surface shows gets one ``voice spoken``, voiced or not.

The star-core companion shows an answer's text with a speaking face from its
first chunk until ``voice spoken`` names that turn (desktop/resonance
``model.ts``). Only a player sent it, so a turn no player voices kept her
talking for good: a tier-0 answer routed to ``badge_card``, a GPT-Live
delegation (ADR-0016 D8), any turn on a boot without a speech pipeline, and
an answer that was queued behind another and dropped with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import PHONE_VOICE_CHANNEL, emit_event, open_event_log
from jarvis.surface.terminal_ui import Device, _own_voice
from tests.integration.test_incremental_tts import _cancel, _chunk, _open, _pipeline, _wait_for
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _emit_response,
    _FakeProvider,
    _RecordingBroadcaster,
    _submit_response,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


class _Wire:
    """The broadcaster calls the two watchers make, recorded in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def broadcast_open(self, ev: Any) -> None:  # noqa: ANN401 - Event
        self.sent.append(("open", str(ev.payload["turn_id"]), ""))

    async def broadcast_chunk(self, ev: Any) -> None:  # noqa: ANN401 - Event
        self.sent.append(("append", str(ev.payload["turn_id"]), ""))

    async def broadcast_done(self, ev: Any) -> None:  # noqa: ANN401 - Event
        self.sent.append(("done", str(ev.payload["turn_id"]), ""))

    async def broadcast_op(self, op: str, *, turn_id: str, **_: object) -> None:
        self.sent.append((op, turn_id, ""))

    async def broadcast_voice(self, phase: str, *, turn_id: str, **payload: object) -> None:
        self.sent.append((phase, turn_id, str(payload.get("output_outcome", ""))))


def _answer(conn: sqlite3.Connection, turn_id: str, *, attention_channel: str | None) -> None:
    header: dict[str, object] = {"turn_id": turn_id, "query": "q", "kind": "text"}
    if attention_channel is not None:
        header["attention_channel"] = attention_channel
    emit_event(conn, type="surface.response_open", payload=header)
    emit_event(conn, type="surface.response_chunk", payload={"turn_id": turn_id, "text": "a."})
    emit_event(conn, type="surface.response_emitted", payload={"turn_id": turn_id, "text": "a."})


async def _watch(watcher: Any, emit: Any) -> None:  # noqa: ANN401 - coroutine and callback
    task = asyncio.create_task(watcher)
    await asyncio.sleep(0.05)
    emit()
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_an_answer_no_player_voices_is_announced_spoken_as_suppressed(tmp_path: Path) -> None:
    """A badge_card answer and a GPT-Live delegation each end with one suppressed ``spoken``."""
    conn = open_event_log(tmp_path / "events.db")
    wire = _Wire()
    pipeline = MagicMock()

    def emit() -> None:
        _answer(conn, "T-card", attention_channel="badge_card")
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"turn_id": "T-live", "transcript": "查天气", "channel": "gpt_live"},
        )
        _answer(conn, "T-live", attention_channel=None)
        _answer(conn, "T-voice", attention_channel="voice_notify")

    asyncio.run(
        _watch(
            inherent_loop._tts_watcher(  # noqa: SLF001 - the production watcher
                conn=conn, pipeline=pipeline, poll_interval_s=0.01, broadcaster=wire,  # type: ignore[arg-type]
            ),
            emit,
        ),
    )
    assert wire.sent == [
        ("spoken", "T-card", "suppressed"),
        ("spoken", "T-live", "suppressed"),
    ]
    pipeline.begin_turn.assert_called_once_with("T-voice", gate_mode="sentence")


def test_a_boot_without_speech_ends_every_answer_with_no_voice(tmp_path: Path) -> None:
    """No speech pipeline: ``done`` is followed by ``spoken`` so no face waits for audio."""
    conn = open_event_log(tmp_path / "events.db")
    wire = _Wire()
    asyncio.run(
        _watch(
            inherent_loop._response_watcher(  # noqa: SLF001 - the production watcher
                SimpleNamespace(conn=conn),  # type: ignore[arg-type]
                wire,  # type: ignore[arg-type]
                poll_interval_s=0.01,
                voiced=False,
            ),
            lambda: _answer(conn, "T-1", attention_channel="voice_notify"),
        ),
    )
    assert wire.sent == [
        ("open", "T-1", ""),
        ("append", "T-1", ""),
        ("done", "T-1", ""),
        ("spoken", "T-1", "no_voice"),
    ]


def test_a_brain_ends_a_typed_turn_with_a_word_a_voice_terminal_passes_on(tmp_path: Path) -> None:
    """A brain has no speaker: a typed turn ends ``suppressed``, any other ``no_voice``.

    The voice terminal's relay drops ``no_voice`` because its own player says the real
    ``spoken``, but its player is never given a typed turn (ADR 0181), so for that turn the
    brain's word is the only one and must get through. A phone's turn keeps ``no_voice``.
    """
    conn = open_event_log(tmp_path / "events.db")
    wire = _Wire()

    def emit() -> None:
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"turn_id": "T-typed", "transcript": "hello", "channel": "cli_stdin"},
        )
        _answer(conn, "T-typed", attention_channel=None)
        _answer(conn, "T-voice", attention_channel="voice_notify")
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"turn_id": "T-phone", "transcript": "hi", "channel": PHONE_VOICE_CHANNEL},
        )
        _answer(conn, "T-phone", attention_channel=None)

    asyncio.run(
        _watch(
            inherent_loop._response_watcher(  # noqa: SLF001 - the production watcher
                SimpleNamespace(conn=conn),  # type: ignore[arg-type]
                wire,  # type: ignore[arg-type]
                poll_interval_s=0.01,
                voiced=False,
            ),
            emit,
        ),
    )
    ends = [(turn, outcome) for phase, turn, outcome in wire.sent if phase == "spoken"]
    assert ends == [
        ("T-typed", "suppressed"), ("T-voice", "no_voice"), ("T-phone", "no_voice"),
    ]
    speaking = Device(speaks=lambda: True)
    relayed = [
        _own_voice(
            speaking,
            json.dumps({"op": "voice", "payload": {
                "phase": "spoken", "turn_id": turn, "output_outcome": outcome,
            }}),
        )
        for turn, outcome in ends
    ]
    assert [r is not None for r in relayed] == [True, False, False]


def test_an_answer_dropped_from_the_queue_is_announced_spoken(tmp_path: Path) -> None:
    """Cancelling the playing answer drops the one queued behind it, and says so."""
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
            first = [_open(conn, "RA"), _chunk(conn, "RA", 0, "第一句。")]
            asyncio.run(_submit_response(pipeline, first))
            _wait_for(conn, "surface.playback_segment_prepared", "RA", 1)
            queued = _emit_response(
                conn, response_id="RB", group_id="G-RA", turn_id="T-RB", text="排队的后续。",
            )
            asyncio.run(_submit_response(pipeline, queued))
            asyncio.run(_submit_response(pipeline, [_cancel(conn, "RA")]))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert "ui:spoken:T-RA:interrupted" in actions
    assert "ui:spoken:T-RB:dropped" in actions
