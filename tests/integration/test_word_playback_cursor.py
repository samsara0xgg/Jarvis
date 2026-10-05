"""Provider word ends survive the real player, durable replay and next-turn prompt."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.surface import voice_ledger, voice_media, voice_tts
from jarvis.surface.voice_ledger import StalePlaybackGeneration
from tests.integration.test_incremental_tts import _spoken, _Trail
from tests.integration.test_previous_answer_line import _asked, _next_turn_line
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _config,
    _emit_response,
    _FakeProvider,
    _FakeSession,
    _player,
    _submit_response,
    _terminal_for,
    _wait_until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_WORDS = tuple(f"{number}、" for number in range(1, 51))
_TEXT = "".join(_WORDS)
_PREFIX = "".join(_WORDS[:15])
_SAMPLES_PER_WORD = 80
_RATE = 8_000


class _AlignedSession(_FakeSession):
    scale = 1.0  # the share of the audio the provider's word times span

    async def _events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:
        segment = await self._segments.get()
        assert segment.text == _TEXT
        # MiniMax's word "end" is where the word starts (voiced onset for the first).
        ends = tuple(
            (len("".join(_WORDS[:index])), (index - 1) * 10.0 * self.scale)
            for index in range(1, len(_WORDS) + 1)
        )
        yield voice_tts.TTSAudioChunk(
            sequence=segment.sequence, pcm=b"", sample_rate_hz=_RATE,
            timing_text=segment.text, word_boundaries=ends,
        )
        yield voice_tts.TTSAudioChunk(
            sequence=segment.sequence,
            pcm=np.full(len(_WORDS) * _SAMPLES_PER_WORD, 2_000, dtype="<i2").tobytes(),
            sample_rate_hz=_RATE,
        )
        yield voice_tts.TTSSegmentFinished(sequence=segment.sequence)


class _SqueezedSession(_AlignedSession):
    scale = 0.3  # every word "said" in a third of the real audio, as MiniMax often does


class _AlignedProvider(_FakeProvider):
    session = _AlignedSession

    def create_tts_session(
        self, *, endpoint_index: int, language: str, idle_close_s: float,
        command_queue_capacity: int, audio_queue_capacity: int,
    ) -> voice_tts.TTSSession:
        del language, idle_close_s, command_queue_capacity, audio_queue_capacity
        return self.session(self, endpoint_index=endpoint_index)


@pytest.mark.parametrize("quiet_tail", [False, True])
@pytest.mark.parametrize("silent_reply", [False, True])
def test_counting_cut_at_fifteen_keeps_fifteen_in_next_turn(
    tmp_path: Path, *, quiet_tail: bool, silent_reply: bool,
) -> None:
    """All fifty numbers are one segment; quiet later samples cannot erase fifteen."""
    db = tmp_path / "words.db"
    conn = open_event_log(db)
    player = _player(ring_seconds=1.0)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_AlignedProvider(candidate_count=1), player=player,
        conn_factory=lambda: open_event_log(db), boot_high_water_id=0,
        config=_config(), start_player=False,
    )
    try:
        _asked(conn, "T")
        rows = _emit_response(conn, response_id="R", group_id="G", turn_id="T", text=_TEXT)
        asyncio.run(_submit_response(pipeline, rows))
        _wait_until(lambda: player.bytes_pending() == len(_WORDS) * _SAMPLES_PER_WORD * 4)
        pump = _CallbackPump(player)
        for _ in range(15):
            pump.step(frames=_SAMPLES_PER_WORD)
        _wait_until(lambda: any(
            event.type == "surface.playback_checkpoint"
            and event.payload.get("heard_text") == _PREFIX
            for event in iter_events(conn)
        ))
        if quiet_tail:
            # Under the heard floor: words at the barge-in yield (0.2) count as heard (ADR 0083).
            player.set_gain(0.1, ramp_ms=0)
            pump.step(frames=_SAMPLES_PER_WORD)
        pipeline.stop_foreground_output("R", reason="barge_in")
        assert pipeline.wait_until_idle(timeout_s=2)
        kind, payload = _terminal_for(conn, response_id="R")
        assert kind == "surface.playback_interrupted"
        assert payload["heard_text"] == _PREFIX
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert heard is not None
        assert heard.text == _PREFIX
        if silent_reply:
            _asked(conn, "T-silent")
            rows = _emit_response(
                conn, response_id="R-silent", group_id="G-silent",
                turn_id="T-silent", text=_TEXT,
            )
            asyncio.run(_submit_response(pipeline, rows))
            _wait_until(lambda: player.bytes_pending() > 0)
            pipeline.stop_foreground_output("R-silent", reason="barge_in")
            assert pipeline.wait_until_idle(timeout_s=2)
        line = _next_turn_line(conn)
        assert line is not None
        assert '15、"; the rest was not spoken' in line
        assert "50" not in line
    finally:
        assert pipeline.close()
        conn.close()


@pytest.mark.parametrize("durable", [False, True])
def test_partial_cursor_requires_a_durable_word_mapping(tmp_path: Path, *, durable: bool) -> None:
    """A text hash alone cannot manufacture the prefix; the exact word end is required."""
    conn = open_event_log(tmp_path / "mapping.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_TEXT)
        start = trail.start(first_segment=_TEXT)
        trail.prepare(0)
        identity = {
            "session_id": "S", "response_id": "R", "turn_id": "T", "playback_generation_id": 1,
        }
        if durable:
            emit_event(conn, type="surface.playback_alignment", payload={
                **identity, "sequence": 0,
                "speech_text_hash": hashlib.sha256(_TEXT.encode()).hexdigest(),
                "word_boundaries": [[len(_PREFIX), 1_200]],
            }, source_event_id=start.event_uid)
        emit_event(conn, type="surface.playback_checkpoint", payload={
            **identity, "heard_through_sequence": None, "submitted_samples": 1_200,
            "estimated_audible_samples": 1_200,
            "heard_partial_sequence": 0, "heard_partial_text_end": len(_PREFIX),
            "heard_text": _PREFIX, "heard_text_hash": hashlib.sha256(_PREFIX.encode()).hexdigest(),
            "cursor_quality": "estimated",
        }, source_event_id=start.event_uid)
        assert _spoken(conn) == ((True, _PREFIX) if durable else (False, None))
    finally:
        conn.close()


def test_streaming_subtitle_tail_is_not_a_confirmed_word() -> None:
    """The provider revises its final in-progress word in the next update."""
    raw = {"text": "one two", "timestamped_words": [
        {"word": "one", "word_begin": 0, "word_end": 3, "time_begin": 0, "time_end": 200},
        {"word": " ", "word_begin": 3, "word_end": 4, "time_begin": 200, "time_end": 240},
        {"word": "two", "word_begin": 4, "word_end": 7, "time_begin": 240, "time_end": 300},
    ]}
    assert voice_tts._subtitle_boundaries(raw, "one two", final=False) == ((3, 200.0), (4, 240.0))  # noqa: SLF001
    assert voice_tts._subtitle_boundaries(raw, "one two", final=True)[-1] == (7, 300.0)  # noqa: SLF001
    assert voice_tts._subtitle_boundaries(raw, "other words", final=True) == ()  # noqa: SLF001


def test_minimax_blocks_numeric_syllables_and_summary_only_terminal() -> None:  # noqa: C901
    """Replay the real provider shapes through its session reader, across segments."""
    def word(text: str, start: int, end: int, begin: int, finish: int) -> dict[str, object]:
        return {"word": text, "word_begin": start, "word_end": end,
                "time_begin": begin, "time_end": finish}

    first = [word("14", 0, 2, 0, 100), word("14", 0, 2, 100, 200),
             word("、", 2, 3, 200, 250), word("15", 3, 5, 250, 300)]
    settled = [*first, word("15", 3, 5, 300, 400), word("、", 5, 6, 400, 450)]
    second = [word("16", 6, 8, 450, 550), word("16", 6, 8, 550, 650),
              word("。", 8, 9, 650, 700)]
    again = [word("17", 0, 2, 0, 100), word("17", 0, 2, 100, 200),
             word("。", 2, 3, 200, 250)]

    def row(text: str, start: int, end: int, words: object, *, final: bool = False) -> str:
        return json.dumps({"data": {"subtitle": {
            "text": text, "text_begin": start, "text_end": end, "timestamped_words": words,
        }}, "is_final": final})

    class Socket:
        def __init__(self) -> None:
            self.rows = iter([
                "{}", "{}", row("14、15、", 0, 6, first), row("14、15、", 0, 6, settled),
                row("16。", 6, 9, second), row("16。", 6, 9, None, final=True),
                row("17。", 0, 3, again), row("17。", 0, 3, None, final=True),
            ])

        async def recv(self) -> str:
            return next(self.rows)

        async def send(self, _raw: str) -> None:
            pass

        async def close(self) -> None:
            pass

    async def run() -> None:
        async def connect(*_args: object, **_kwargs: object) -> Socket:
            return Socket()

        with patch.object(voice_tts, "_ws_connect", side_effect=connect):
            session = voice_tts.MiniMaxTTSSession(
                api_key="test", endpoint="https://example.test", voice="voice", model="model",
                volume=1, language="en",
                sample_rate_hz=8_000, connect_timeout_s=1, first_chunk_timeout_s=1,
                between_chunk_timeout_s=1, idle_close_s=60, command_queue_capacity=2,
                audio_queue_capacity=16,
            )
            try:
                await session.open("R", 1)
                iterator = session.audio_events().__aiter__()
                results = []
                for sequence, text in enumerate(("14、15、16。", "17。")):
                    await session.send(voice_tts.TTSResponseSegment("R", 1, sequence, text))
                    boundaries: list[tuple[int, float]] = []
                    async for event in iterator:
                        if isinstance(event, voice_tts.TTSSegmentFinished):
                            break
                        boundaries.extend(event.word_boundaries)
                    results.append(boundaries)
                assert results == [
                    [(2, 200), (3, 250), (5, 400), (6, 450), (8, 650), (9, 700)],
                    [(2, 200), (3, 250)],
                ]
            finally:
                await session.close()

    asyncio.run(run())


@pytest.mark.parametrize("ended", ["interrupted", "completed"])
def test_empty_confirmed_text_does_not_mean_no_audio(tmp_path: Path, ended: str) -> None:
    """An uncertain cursor cannot turn actual playback into an unspoken answer."""
    conn = open_event_log(tmp_path / "uncertain.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_TEXT)
        start = trail.start(first_segment=_TEXT)
        trail.prepare(0)
        trail.emitted(_TEXT)
        emit_event(conn, type=f"surface.playback_{ended}", payload={
            "session_id": "S", "response_id": "R", "turn_id": "T", "playback_generation_id": 1,
            "heard_through_sequence": None, "submitted_samples": 1_200,
            "heard_text": "", "heard_text_hash": hashlib.sha256(b"").hexdigest(),
            "cursor_quality": "estimated", "reason": "barge_in",
            "speech_text_hash": hashlib.sha256(_TEXT.encode()).hexdigest(),
        }, source_event_id=start.event_uid)
        line = _next_turn_line(conn)
        assert line is not None
        assert "before any of it was spoken" not in line
        expected = (
            "playback completed" if ended == "completed" else "exact stop position is unknown"
        )
        assert expected in line
    finally:
        conn.close()


def test_squeezed_provider_timing_keeps_no_word_ends(tmp_path: Path) -> None:
    """Words squeezed into a third of the audio are not believed: the segment stays whole."""
    db = tmp_path / "squeezed.db"
    conn = open_event_log(db)
    player = _player(ring_seconds=1.0)
    provider = _AlignedProvider(candidate_count=1)
    provider.session = _SqueezedSession
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider, player=player, conn_factory=lambda: open_event_log(db),
        boot_high_water_id=0, config=_config(), start_player=False,
    )
    try:
        _asked(conn, "T")
        rows = _emit_response(conn, response_id="R", group_id="G", turn_id="T", text=_TEXT)
        asyncio.run(_submit_response(pipeline, rows))
        _wait_until(lambda: player.bytes_pending() == len(_WORDS) * _SAMPLES_PER_WORD * 4)
        _wait_until(lambda: getattr(player.poll_generation(1), "all_segments_closed", False))
        pump = _CallbackPump(player)
        for _ in range(15):
            pump.step(frames=_SAMPLES_PER_WORD)
        snapshot = player.poll_generation(1)
        assert not isinstance(snapshot, StalePlaybackGeneration)
        # Fifteen of fifty words have played; the provider's squeezed times would say all fifty.
        assert (snapshot.played_letters, snapshot.heard_text) == (0, "")
        assert snapshot.playing_letters == sum(ch.isalnum() for ch in _TEXT)
        assert not [e for e in iter_events(conn) if e.type == "surface.playback_alignment"]
    finally:
        assert pipeline.close()
        conn.close()


_SECOND = 48_000
# Word ends as MiniMax sent them (ms to samples at 48 kHz), written as the ledger gets them.
_SQUEEZED = (  # RESP9d9ec3 seg 0: 17 words stamped within 1.15 s, the voice runs 3.3 s
    (1, 1625), (2, 9817), (3, 15961), (4, 18009), (5, 24153), (6, 26201), (7, 28249),
    (8, 30297), (9, 32345), (10, 34393), (11, 36441), (12, 38489), (13, 40537), (14, 42585),
    (15, 44633), (16, 46681), (17, 48729),
)
_ONSETS = (  # "好,有事再叫我。": each word's time is where it starts (好 is the voiced onset)
    (1, 171), (2, 213), (3, 853), (4, 981), (5, 1109), (6, 1237), (7, 1365), (8, 1536),
)


@pytest.mark.parametrize(
    ("boundaries", "scale", "segment_ms", "expected"),
    [
        # More than a third of the audio unspoken at the last word: the timing is not true.
        (_SQUEEZED, 1, 3_500, ()),
        # Word i is done where word i+1 starts; the last is done with the segment (not listed).
        (
            _ONSETS, 48, 1_700,
            ((1, 213 * 48), (2, 853 * 48), (3, 981 * 48), (4, 1109 * 48),
             (5, 1237 * 48), (6, 1365 * 48), (7, 1536 * 48)),
        ),
        # Three words say too little to doubt them.
        (((1, 100), (2, 200), (3, 300)), 48, 10_000, ((1, 200 * 48), (2, 300 * 48))),
        # One word has no successor: the segment keeps no word ends.
        (((1, 100),), 48, 400, ()),
    ],
)
def test_provider_word_times_become_word_ends_or_nothing(
    boundaries: tuple[tuple[int, int], ...], scale: int, segment_ms: int,
    expected: tuple[tuple[int, int], ...],
) -> None:
    """Data-driven: the rule on the shapes MiniMax really sent (kept recordings, 2026-10-05)."""
    scaled = tuple((end, ms * scale) for end, ms in boundaries)
    assert voice_ledger.settle_boundaries(
        scaled, segment_start=0, segment_end=segment_ms * _SECOND // 1000,
    ) == expected
