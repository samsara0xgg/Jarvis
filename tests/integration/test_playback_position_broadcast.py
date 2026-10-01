"""The screen is told where her voice is, so captions follow it and stop with it (ADR 0112)."""

from __future__ import annotations

import asyncio
import itertools
import threading
from typing import TYPE_CHECKING

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_ledger, voice_media, voice_tts
from tests.integration.test_previous_answer_line import _asked
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _config,
    _emit_response,
    _player,
    _submit_response,
    _wait_until,
)
from tests.integration.test_word_playback_cursor import (
    _RATE,
    _SAMPLES_PER_WORD,
    _TEXT,
    _WORDS,
    _AlignedProvider,
)

if TYPE_CHECKING:
    from pathlib import Path

_LETTERS = sum(ch.isalnum() for ch in _TEXT)  # the fifty numbers: 9 + 41 * 2 digits


class _Wire:
    """What the companion would receive: voice phases in the order they were sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self._lock = threading.Lock()

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        with self._lock:
            self.sent.append((phase, {"turn_id": turn_id, **payload}))

    def playing(self) -> list[dict[str, object]]:
        with self._lock:
            return [payload for phase, payload in self.sent if phase == "playing"]

    def phases(self) -> list[str]:
        with self._lock:
            return [phase for phase, _payload in self.sent]


def _start(tmp_path: Path) -> tuple[
    voice_media.StreamingTTSPipeline, voice_tts.AudioStreamPlayer, _CallbackPump, _Wire,
]:
    db = tmp_path / "position.db"
    conn = open_event_log(db)
    player = _player(ring_seconds=1.0)
    wire = _Wire()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=_AlignedProvider(candidate_count=1), player=player,
        conn_factory=lambda: open_event_log(db), boot_high_water_id=0,
        config=_config(), broadcaster=wire, start_player=False,
    )
    _asked(conn, "T")
    rows = _emit_response(conn, response_id="R", group_id="G", turn_id="T", text=_TEXT)
    asyncio.run(_submit_response(pipeline, rows))
    _wait_until(lambda: player.bytes_pending() == len(_WORDS) * _SAMPLES_PER_WORD * 4)
    conn.close()
    return pipeline, player, _CallbackPump(player), wire


def _letters(words: int) -> int:
    return sum(ch.isalnum() for ch in "".join(_WORDS[:words]))


def test_position_follows_the_words_played_and_is_sent_only_when_it_changes(tmp_path: Path) -> None:
    """Fifteen of fifty numbers played: the report says so; the segment reaches all fifty."""
    pipeline, _, pump, wire = _start(tmp_path)
    try:
        for _ in range(15):
            pump.step(frames=_SAMPLES_PER_WORD)
        _wait_until(lambda: any(p["played"] == _letters(15) for p in wire.playing()))
        reports = wire.playing()
        assert all(p["turn_id"] == "T" and p["held"] is False for p in reports)
        assert all(p["ahead"] == _LETTERS for p in reports)  # one segment: all of it
        played = [p["played"] for p in reports]
        assert played == sorted(played)
        assert all(a != b for a, b in itertools.pairwise(reports))
        assert played[-1] == _letters(15)
    finally:
        assert pipeline.close()


def test_a_hold_stops_the_position_and_letting_go_moves_it_on(tmp_path: Path) -> None:
    """A soft barge-in holds her; the report says held, and nothing moves until she goes on."""
    pipeline, _, pump, wire = _start(tmp_path)
    try:
        for _ in range(10):
            pump.step(frames=_SAMPLES_PER_WORD)
        _wait_until(lambda: any(p["played"] == _letters(10) for p in wire.playing()))
        pipeline.pause_speaking(True)  # noqa: FBT003 - the capture side's one bit
        _wait_until(lambda: wire.playing()[-1]["held"] is True)
        held_at = wire.playing()[-1]["played"]
        for _ in range(10):
            pump.step(frames=_SAMPLES_PER_WORD)  # the device keeps calling; she is silent
        assert wire.playing()[-1]["played"] == held_at
        pipeline.pause_speaking(False)  # noqa: FBT003
        _wait_until(lambda: wire.playing()[-1]["held"] is False)
        for _ in range(5):
            pump.step(frames=_SAMPLES_PER_WORD)
        _wait_until(lambda: wire.playing()[-1]["played"] == _letters(15))
    finally:
        assert pipeline.close()


def test_a_stop_is_reported_before_the_terminal_event(tmp_path: Path) -> None:
    """The captions freeze when the audio is cut, not after the terminal commit."""
    pipeline, _, pump, wire = _start(tmp_path)
    try:
        for _ in range(12):
            pump.step(frames=_SAMPLES_PER_WORD)
        _wait_until(lambda: any(p["played"] == _letters(12) for p in wire.playing()))
        pipeline.stop_foreground_output("R", reason="barge_in")
        assert pipeline.wait_until_idle(timeout_s=2)
        phases = wire.phases()
        assert phases[-1] == "spoken"
        last = wire.playing()[-1]
        assert last["held"] is True
        assert last["played"] == _letters(12)
        last_playing = max(i for i, phase in enumerate(phases) if phase == "playing")
        assert last_playing < phases.index("spoken")
    finally:
        assert pipeline.close()


def test_a_duck_keeps_her_place_though_it_stops_the_heard_text() -> None:
    """Quieter speech is still in place for the captions, which heard_text does not count."""
    lease = voice_ledger.GenerationLease("S", "R", "G", "T", 1, 1)
    ledger = voice_ledger.PlaybackLedger(lease, sample_rate=_RATE)
    ledger.begin_segment(sequence=0, text="Hello there. ", segment_hash="a")
    accepted = ledger.accept_samples(sequence=0, sample_count=1_000)
    ledger.finish_segment(sequence=0)
    ledger.begin_segment(sequence=1, text="Second one.", segment_hash="b")
    ledger.accept_samples(sequence=1, sample_count=1_000)
    ledger.finish_segment(sequence=1)
    ledger.record_submitted(
        output_start_cursor=accepted.output_start_cursor, output_end_cursor=1_000,
        audibility_class="attenuated",
    )
    ledger.record_audible(output_cursor=1_000, cursor_quality="estimated")
    snapshot = ledger.snapshot()
    assert snapshot.heard_text == ""  # ducked: not counted as heard
    assert snapshot.played_letters == len("Hellothere")  # but she has got there
    assert snapshot.playing_letters == len("Hellothere") + len("Secondone")
