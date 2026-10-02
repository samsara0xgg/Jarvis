"""A TTS session connected while Allen talks carries his answer.

Every answer opened its own MiniMax session once it was written: TCP, TLS
(0.11-0.15 s to api-uw on 2026-09-29), the upgrade and the task handshake
all sat between the written answer and her first word. ADR 0053's hold,
set while Allen's words come in, now connects the next answer's first
session, and the answer binds it. A spare that is stale or never connected
is not used; the answer connects its own, as before. MiniMax reads a task in
the language fixed at its task_start, so the spare reads the system language
and an answer in the other language does not take it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from jarvis.shared import lang
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_media, voice_tts
from tests.integration.test_incremental_tts import _wait_for
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _config,
    _emit_response,
    _FakeProvider,
    _FakeWebSocket,
    _player,
    _submit_response,
    _wait_until,
)

if TYPE_CHECKING:
    from pathlib import Path


def _answer_after_he_talked(
    tmp_path: Path, provider: _FakeProvider, text: str = "Hi.",
) -> None:
    """Allen talks (the hold), stops, and his answer is spoken to its end."""
    db = tmp_path / "events.db"
    conn = open_event_log(db)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider, player=player, conn_factory=lambda: open_event_log(db),
        boot_high_water_id=0, config=_config(), start_player=False,
    )
    try:
        pipeline.hold_output(held=True)
        _wait_until(lambda: "connect" in provider.actions)
        pipeline.hold_output(held=False)
        with _CallbackPump(player):
            rows = _emit_response(conn, response_id="R", group_id="G", turn_id="T", text=text)
            asyncio.run(_submit_response(pipeline, rows))
            _wait_for(conn, "surface.playback_completed", "R", 1)
        assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()


def test_the_session_connected_while_he_talks_speaks_his_answer(tmp_path: Path) -> None:
    """One session in all: connected during the hold, then bound to the answer."""
    lang.set_language("en")
    provider = _FakeProvider(candidate_count=1)
    _answer_after_he_talked(tmp_path, provider)
    assert provider.created == 1
    assert provider.languages == ["en"]
    assert provider.actions[:2] == ["connect", "open:R"]


def test_a_spare_in_the_other_language_is_not_used(tmp_path: Path) -> None:
    """The spare was started for zh; an English answer connects its own for en."""
    provider = _FakeProvider(candidate_count=1)
    _answer_after_he_talked(tmp_path, provider)
    assert provider.languages == ["zh", "en"]
    assert provider.opened == [("R", 0)]


def test_a_spare_in_the_answers_language_is_used(tmp_path: Path) -> None:
    """A Chinese answer binds the zh spare started for the system language."""
    provider = _FakeProvider(candidate_count=1)
    _answer_after_he_talked(tmp_path, provider, text="\u4f60\u597d\u3002")
    assert provider.languages == ["zh"]


def test_the_next_spare_reads_the_language_of_the_last_answer(tmp_path: Path) -> None:
    """A Chinese answer makes the next spare zh, whatever the system language, and it is used."""
    lang.set_language("en")
    provider = _FakeProvider(candidate_count=1)
    db = tmp_path / "events.db"
    conn = open_event_log(db)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider, player=player, conn_factory=lambda: open_event_log(db),
        boot_high_water_id=0, config=_config(), start_player=False,
    )
    try:
        with _CallbackPump(player):
            for turn, response_id in enumerate(("R1", "R2"), start=1):
                pipeline.hold_output(held=True)
                _wait_until(lambda n=turn: provider.actions.count("connect") == n)
                pipeline.hold_output(held=False)
                rows = _emit_response(
                    conn, response_id=response_id, group_id=f"G{turn}", turn_id=f"T{turn}",
                    text="\u4f60\u597d\u3002",
                )
                asyncio.run(_submit_response(pipeline, rows))
                _wait_for(conn, "surface.playback_completed", response_id, 1)
        assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    # R1: an en spare misses a Chinese answer; R2: the zh spare is taken, nothing new is created.
    assert provider.languages == ["en", "zh", "zh"]
    assert provider.opened == [("R1", 0), ("R2", 0)]
    assert provider.created == 3


def test_a_stale_spare_is_closed_and_the_answer_connects_its_own(tmp_path: Path) -> None:
    """Past its age a spare may already be closed by MiniMax: never trusted."""
    provider = _FakeProvider(candidate_count=1)
    with patch.object(voice_media, "_SPARE_SESSION_MAX_AGE_S", -1.0):
        _answer_after_he_talked(tmp_path, provider)
    assert provider.created == 2
    assert provider.opened == [("R", 0)]


def test_a_spare_that_never_connected_is_not_used(tmp_path: Path) -> None:
    """A failed early connect costs nothing: the answer opens a session as before."""
    provider = _FakeProvider(candidate_count=1)
    provider.connect_error = OSError("connection refused")
    _answer_after_he_talked(tmp_path, provider)
    assert provider.created == 2
    assert provider.opened == [("R", 0)]


def test_a_connected_minimax_session_binds_without_a_second_handshake() -> None:
    """``connect`` does the transport and the task handshake; ``open`` only binds."""

    async def _body() -> tuple[int, list[str]]:
        ws = _FakeWebSocket()
        connects = 0

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            nonlocal connects
            del additional_headers
            connects += 1
            return ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = voice_tts.MiniMaxTTSSession(
                api_key="key", endpoint="https://example.test", voice="voice", model="model",
                volume=5, language="en",
                sample_rate_hz=8_000, connect_timeout_s=0.2,
                first_chunk_timeout_s=0.2, between_chunk_timeout_s=0.2, idle_close_s=1.0,
                command_queue_capacity=1, audio_queue_capacity=1,
            )
            await session.connect()
            await session.open("R", 1)
            await session.send(voice_tts.TTSResponseSegment("R", 1, 0, "text"))
            ws.audio_gate.set()
            events = session.audio_events().__aiter__()
            assert isinstance(await events.__anext__(), voice_tts.TTSAudioChunk)
            assert isinstance(await events.__anext__(), voice_tts.TTSSegmentFinished)
            await session.finish()
        return connects, [str(payload["event"]) for payload in ws.sent]

    assert asyncio.run(_body()) == (1, ["task_start", "task_continue", "task_finish"])


_AMBIGUOUS_ENGLISH = "It\u2019s 6:47 p.m. in Victoria."
_CHINESE = "\u73b0\u5728\u662f\u4e0b\u53486\u70b947\u5206\u3002"


@pytest.mark.parametrize(
    ("text", "expected"),
    [(_AMBIGUOUS_ENGLISH, "en"), (_CHINESE, "zh"), ("Open \u5fae\u4fe1 please", "zh"), ("", "en")],
)
def test_the_text_decides_the_language_a_voice_reads(text: str, expected: str) -> None:
    """Any CJK character makes zh; Latin-only text, however short or ambiguous, is en."""
    assert lang.text_language(text) == expected


@pytest.mark.parametrize(
    ("session_language", "boost"), [("en", "English"), ("zh", "Chinese")],
)
def test_the_streaming_task_start_names_the_language(
    session_language: lang.Language, boost: str,
) -> None:
    """The prewarm path: the language is in the task_start before any text exists."""
    async def _body() -> dict[str, object]:
        ws = _FakeWebSocket()

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            del additional_headers
            return ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = voice_tts.MiniMaxTTSSession(
                api_key="key", endpoint="https://example.test", voice="voice", model="model",
                volume=5, language=session_language,
                sample_rate_hz=8_000, connect_timeout_s=0.2,
                first_chunk_timeout_s=0.2, between_chunk_timeout_s=0.2, idle_close_s=1.0,
                command_queue_capacity=1, audio_queue_capacity=1,
            )
            await session.connect()
            await session.close()
        return ws.sent[0]

    started = asyncio.run(_body())
    assert started["event"] == "task_start"
    assert started["language_boost"] == boost


@pytest.mark.parametrize(
    ("text", "boost"), [(_AMBIGUOUS_ENGLISH, "English"), (_CHINESE, "Chinese")],
)
def test_the_legacy_task_start_names_the_language_of_its_text(text: str, boost: str) -> None:
    """The one-shot client knows its text, so the text picks the language."""
    async def _body() -> dict[str, object]:
        ws = _FakeWebSocket()
        ws.audio_gate.set()

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            del additional_headers
            return ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            await voice_tts.MiniMaxWSClient(api_key="key").synthesize(text)
        return ws.sent[0]

    started = asyncio.run(_body())
    assert started["event"] == "task_start"
    assert started["language_boost"] == boost
