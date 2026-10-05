"""ADR 0165: provider silence is dropped at generation start and junctions, speech never touched."""

# ruff: noqa: RUF001 - fullwidth punctuation is the thing under test
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np
import pytest

from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface import voice_media, voice_tts
from jarvis.surface.tts_silence import SilenceTrimConfig, SilenceTrimmer
from jarvis.surface.voice_ledger import OutputTimelineSnapshot, StalePlaybackGeneration
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

_RATE = 8_000  # 1 ms = 8 samples; 5 ms window = 40, 20 ms pre-roll = 160
_CONFIG = SilenceTrimConfig(
    threshold_db=-50.0, window_ms=5.0, preroll_ms=20.0, fade_ms=5.0,
    max_cut_ms=400.0, clause_pause_ms=120.0, sentence_pause_ms=250.0,
)
_PREROLL, _WINDOW, _CAP = 160, 40, 3_200
_CLAUSE, _SENTENCE = 960, 2_000
_LEAD, _TONE, _TAIL = 2_000, 800, 3_000


def _tone(n: int = _TONE) -> np.ndarray:
    wave: np.ndarray = (0.4 * np.sin(np.arange(n) * 0.3 + 1.0)).astype("<f4")
    return wave


def _zeros(n: int) -> np.ndarray:
    return np.zeros(n, dtype="<f4")


def _segment(lead: int = _LEAD, tail: int = _TAIL) -> np.ndarray:
    return np.concatenate([_zeros(lead), _tone(), _zeros(tail)])


def _trim(
    segments: list[tuple[str, np.ndarray]], *, chunk: int = 500,
) -> tuple[list[np.ndarray], SilenceTrimmer, list[int]]:
    trimmer = SilenceTrimmer(sample_rate_hz=_RATE, config=_CONFIG)
    outputs, dropped = [], []
    for text, audio in segments:
        trimmer.begin_segment(text)
        parts = [trimmer.feed(audio[i : i + chunk].tobytes()) for i in range(0, len(audio), chunk)]
        parts.append(trimmer.end_segment())
        outputs.append(np.frombuffer(b"".join(parts), dtype="<f4"))
        dropped.append(trimmer.head_dropped)
    return outputs, trimmer, dropped


def _first_last_sound(samples: np.ndarray) -> tuple[int, int]:
    heard = np.flatnonzero(samples)
    return int(heard[0]), int(heard[-1])


def test_the_start_of_a_generation_keeps_only_a_pre_roll() -> None:
    """The first segment starts at a short pre-roll, not at the provider's silence."""
    (out,), _, (dropped,) = _trim([("你好。", _segment())])
    first, _ = _first_last_sound(out)
    assert _PREROLL <= first <= _PREROLL + _WINDOW  # pre-roll, plus the window the onset needs
    assert dropped == _LEAD - first
    assert np.array_equal(out[first : first + _TONE], _tone())


@pytest.mark.parametrize(
    ("text", "pause"),
    [
        ("你好，", _CLAUSE), ("你好,", _CLAUSE), ("你好、", _CLAUSE), ("你好；", _CLAUSE),
        ("你好;", _CLAUSE), ("你好：", _CLAUSE), ("你好:", _CLAUSE),
        ("你好。", _SENTENCE), ("你好！", _SENTENCE), ("你好？", _SENTENCE), ("你好.", _SENTENCE),
        ("你好!", _SENTENCE), ("你好?", _SENTENCE), ("你好…", _SENTENCE),
        ("你好", _SENTENCE), ('他说，"好"，', _CLAUSE), ("你好。」", _SENTENCE),
        ("你好， ", _CLAUSE),
    ],
)
def test_a_junction_pause_follows_the_punctuation_that_ended_the_segment(
    text: str, pause: int,
) -> None:
    """Clause marks give the short pause, sentence ends and anything else the long one."""
    first, second = _segment(), _segment()
    (a, b), _, _ = _trim([(text, first), ("next。", second)])
    _, a_last = _first_last_sound(a)
    b_first, _ = _first_last_sound(b)
    junction = (len(a) - a_last - 1) + b_first
    # The pause is the target, plus at most one window at each cut (the onset and the
    # decay of a word are above the threshold only after the window fills).
    assert pause <= junction <= pause + 2 * _WINDOW
    # The raw junction was lead + tail: the cap is a real cut.
    assert junction < _LEAD + _TAIL


@pytest.mark.parametrize("chunk", [1, 7, 100, 333, 4_096, 100_000])
def test_speech_is_never_touched_and_chunking_does_not_matter(chunk: int) -> None:
    """Speech and a mid-segment pause pass unchanged however the audio is chunked."""
    rng = np.random.default_rng(3)
    speech = np.concatenate([_tone(), (0.2 * rng.standard_normal(1_500)).astype("<f4")])
    audio = np.concatenate([_zeros(900), speech, _zeros(300), _tone(), _zeros(2_500)])
    (out,), _, _ = _trim([("好。", audio)], chunk=chunk)
    (whole,), _, _ = _trim([("好。", audio)], chunk=len(audio))
    assert np.array_equal(out, whole)
    # Every sample above the threshold is kept, in order and unchanged.
    loud = audio[np.abs(audio) > 0.01]
    assert np.array_equal(out[np.abs(out) > 0.01], loud)
    # The 300-sample pause inside the segment is released whole when speech resumes.
    start = int(np.flatnonzero(out == speech[0])[0])
    assert np.array_equal(out[start : start + len(speech) + 300 + _TONE][: len(speech)], speech)
    assert not out[start + len(speech) + _WINDOW : start + len(speech) + 300 - _WINDOW].any()


def test_a_held_tail_is_released_unchanged_when_speech_resumes() -> None:
    """A silence inside a segment is held, then released whole when speech comes back."""
    for pause in (500, 1_500, 9_000):  # shorter than the tail keep, longer, beyond the hold bound
        audio = np.concatenate([_zeros(300), _tone(), _zeros(pause), _tone(), _zeros(_TAIL)])
        (out,), _, _ = _trim([("好。", audio)])
        first = _tone_at(out, 0)
        second = _tone_at(out, first + _TONE)
        assert second - (first + _TONE) == pause, pause
        assert not out[first + _TONE : second].any()


def _tone_at(samples: np.ndarray, start: int) -> int:
    """Index of the first whole tone at or after ``start``."""
    return start + int(np.flatnonzero(samples[start:] == _tone()[0])[0])


def test_the_lead_cut_never_exceeds_the_cap() -> None:
    """Silence beyond the hard cap is released, not cut."""
    (out,), _, (dropped,) = _trim([("好。", _segment(lead=9_000))])
    assert dropped == _CAP
    assert _tone_at(out, 0) == 9_000 - _CAP  # what the cap leaves is released unchanged


def test_a_cut_fades_only_silence_and_no_cut_changes_nothing() -> None:
    """The fade touches only silence at a cut; an uncut segment is byte-identical."""
    quiet = (0.0005 * np.sin(np.arange(2_000) * 0.7)).astype("<f4")  # -63 dBFS room tone
    audio = np.concatenate([quiet, _tone(), quiet])
    (out,), _, (dropped,) = _trim([("好。", audio)])
    assert dropped > 0
    assert out[0] == 0.0
    assert np.all(np.abs(out[: _WINDOW]) <= np.abs(quiet).max())
    first, _ = _first_last_sound(np.where(np.abs(out) > 0.01, out, 0.0))
    assert np.array_equal(out[first : first + _TONE], _tone())
    # Silence shorter than the pre-roll: nothing to cut, so the input comes out as it went in.
    short = np.concatenate([quiet[:100], _tone(), quiet[:100]])
    (kept,), _, (none,) = _trim([("好。", short)])
    assert none == 0
    assert np.array_equal(kept, short)


def test_an_all_silent_segment_does_not_stall_the_next_junction() -> None:
    """A segment with no speech loses at most the cap and leaves the next one its pre-roll."""
    (a, b), _, _ = _trim([("好。", _zeros(4_000)), ("好。", _segment())])
    assert len(a) == 4_000 - _CAP  # the cap bounds what a silent segment loses
    assert b[: _PREROLL].sum() == 0
    assert np.array_equal(b[np.abs(b) > 0.01], _tone()[np.abs(_tone()) > 0.01])


def test_the_switch_and_bounds_are_config_keys() -> None:
    """The switch is off by default; every bound parses and a bad value is refused."""
    assert voice_media.StreamingMediaConfig().trim_silence is False
    parsed = voice_media.streaming_media_config_from_mapping({
        "trim_silence": True, "trim_threshold_db": -45, "trim_preroll_ms": 30,
        "trim_clause_pause_ms": 100, "trim_sentence_pause_ms": 200,
    })
    assert (parsed.trim_silence, parsed.trim_threshold_db) == (True, -45.0)
    assert parsed.trim_preroll_ms == 30.0
    assert (parsed.trim_clause_pause_ms, parsed.trim_sentence_pause_ms) == (100.0, 200.0)
    for bad in ({"trim_threshold_db": 3}, {"trim_threshold_db": True}, {"trim_fade_ms": 0},
                {"trim_silence": "yes"}):
        with pytest.raises(ValueError, match=r"realtime\.streaming_output"):
            voice_media.streaming_media_config_from_mapping(bad)
    with pytest.raises(ValueError, match="trim_threshold_db"):
        replace(_config(), trim_threshold_db=0.0)


# -- the pipeline: every sample position follows the written timeline -----------------------

_TEXTS = ("你好，", "世界。")
_A_WORD = 1  # text offset of the first word, 你
_LEAD_MS = _LEAD / (_RATE / 1000)


def _script() -> dict[int, tuple[np.ndarray, tuple[tuple[int, float], ...]]]:
    """Provider audio per segment, with the word ends the provider would report."""
    tone_ms = _TONE / (_RATE / 1000)
    return {
        sequence: (
            (_segment() * 32768).astype("<i2"),
            ((1, _LEAD_MS + tone_ms / 2), (len(text), _LEAD_MS + tone_ms)),
        )
        for sequence, text in enumerate(_TEXTS)
    }


class _TrimSession(_FakeSession):
    async def _events(self) -> AsyncIterator[voice_tts.TTSAudioEvent]:
        while not self._closed:
            segment = await self._segments.get()
            audio, ends = _script()[segment.sequence]
            yield voice_tts.TTSAudioChunk(
                sequence=segment.sequence, pcm=b"", sample_rate_hz=_RATE,
                timing_text=segment.text, word_boundaries=ends,
            )
            raw = audio.tobytes()
            for i in range(0, len(raw), 1_000):
                yield voice_tts.TTSAudioChunk(
                    sequence=segment.sequence, pcm=raw[i : i + 1_000], sample_rate_hz=_RATE,
                )
            yield voice_tts.TTSSegmentFinished(sequence=segment.sequence)


class _TrimProvider(_FakeProvider):
    def create_tts_session(
        self, *, endpoint_index: int, language: str, idle_close_s: float,
        command_queue_capacity: int, audio_queue_capacity: int,
    ) -> voice_tts.TTSSession:
        del language, idle_close_s, command_queue_capacity, audio_queue_capacity
        return _TrimSession(self, endpoint_index=endpoint_index)


class _Speech:
    """One two-segment answer through the real pipeline and player."""

    def __init__(self, tmp_path: Path, *, trim: bool) -> None:
        db = tmp_path / f"speech-{trim}.db"
        self.conn = open_event_log(db)
        self.player = _player(ring_seconds=2.0)
        self.pipeline = voice_media.StreamingTTSPipeline(
            provider=_TrimProvider(candidate_count=1), player=self.player,
            conn_factory=lambda: open_event_log(db), boot_high_water_id=0,
            config=replace(_config(), trim_silence=trim), start_player=False,
        )
        _asked(self.conn, "T")
        rows = _emit_response(
            self.conn, response_id="R", group_id="G", turn_id="T", text=list(_TEXTS),
        )
        asyncio.run(_submit_response(self.pipeline, rows))
        _wait_until(lambda: self.alignment().keys() == {0, 1})
        self.generation = next(
            int(e.payload["playback_generation_id"]) for e in iter_events(self.conn)
            if e.type == "surface.playback_alignment"
        )
        _wait_until(lambda: self.snapshot().all_segments_closed)

    def alignment(self) -> dict[int, list[list[int]]]:
        return {
            int(e.payload["sequence"]): e.payload["word_boundaries"]
            for e in iter_events(self.conn) if e.type == "surface.playback_alignment"
        }

    def snapshot(self) -> OutputTimelineSnapshot:
        snapshot = self.player.poll_generation(self.generation)
        assert not isinstance(snapshot, StalePlaybackGeneration)
        return snapshot

    def close(self) -> None:
        self.pipeline.stop_foreground_output("R", reason="barge_in")
        self.pipeline.wait_until_idle(timeout_s=2)
        assert self.pipeline.close()
        self.conn.close()


def _raw_provider_audio() -> np.ndarray:
    return np.concatenate([(_script()[i][0] / 32768).astype("<f4") for i in (0, 1)])


def test_the_player_gets_trimmed_audio_and_off_is_the_provider_audio_byte_for_byte(
    tmp_path: Path,
) -> None:
    """Through the real pipeline: off is the provider audio unchanged, on keeps all speech."""
    raw = _raw_provider_audio()
    played = {}
    for trim in (False, True):
        speech = _Speech(tmp_path, trim=trim)
        try:
            frames = speech.snapshot().accepted_samples
            pump = _CallbackPump(speech.player, record=True)
            for _ in range(frames // 500 + 1):
                pump.step(frames=500)
            played[trim] = pump.signal[:frames]
        finally:
            speech.close()
    assert np.array_equal(played[False], raw)
    # Two leads and two tails are cut back; every sample of speech is still there, in order.
    assert len(played[True]) <= len(raw) - 2 * (_LEAD - _PREROLL - _WINDOW) - (_TAIL - _SENTENCE)
    assert np.array_equal(played[True][np.abs(played[True]) > 0.01], raw[np.abs(raw) > 0.01])


def test_word_timing_shifts_by_the_trimmed_lead(tmp_path: Path) -> None:
    """Provider ms count from the provider's start; the written segment starts later."""
    found = {}
    for trim in (False, True):
        speech = _Speech(tmp_path, trim=trim)
        try:
            found[trim] = (speech.alignment(), speech.snapshot().accepted_samples)
        finally:
            speech.close()
    (plain, plain_total), (trimmed, trimmed_total) = found[False], found[True]
    # Off: provider ms to samples, segment 1 starting after all of segment 0's audio.
    assert plain[0] == [[1, _LEAD + _TONE // 2], [3, _LEAD + _TONE]]
    assert plain[1] == [[1, len(_segment()) + _LEAD + _TONE // 2],
                        [3, len(_segment()) + _LEAD + _TONE]]
    # On: segment 0's boundaries move back by exactly the lead dropped (the onset sits one
    # pre-roll plus at most a window after the new start), its words stay _TONE // 2 apart.
    first_word = trimmed[0][0][1]
    assert _PREROLL + _TONE // 2 <= first_word <= _PREROLL + _WINDOW + _TONE // 2
    assert trimmed[0][1][1] - first_word == _TONE // 2
    # Segment 1 starts at the end of the written segment 0 (lead, tone, clause-sized tail) and
    # not at the end of the provider's: its first word is that start plus its own trimmed lead.
    segment_0 = trimmed[0][1][1] + (_CLAUSE - _PREROLL) + _WINDOW  # last word end + tail kept
    assert abs(trimmed[1][0][1] - (segment_0 + _PREROLL + _TONE // 2)) <= 2 * _WINDOW
    assert trimmed[1][0][1] < plain[1][0][1] - 2 * (_LEAD - _PREROLL) + 2 * _WINDOW
    assert trimmed_total < plain_total


def test_heard_text_captions_and_already_said_follow_the_written_timeline(
    tmp_path: Path,
) -> None:
    """Stop at the first word end of the trimmed timeline; the provider's offset says nothing."""
    speech = _Speech(tmp_path, trim=True)
    try:
        first_word_end = speech.alignment()[0][0][1]
        assert first_word_end < _LEAD  # untrimmed this is _LEAD + _TONE // 2
        pump = _CallbackPump(speech.player)
        pump.step(frames=first_word_end - 1)
        before = speech.snapshot()
        assert (before.heard_text, before.played_letters) == ("", 0)
        pump.step(frames=1)
        after = speech.snapshot()
        assert (after.heard_partial_sequence, after.heard_partial_text_end) == (0, _A_WORD)
        assert after.heard_text == _TEXTS[0][:_A_WORD]
        assert (after.played_letters, after.playing_letters) == (1, 2)
        _wait_until(lambda: any(
            e.type == "surface.playback_checkpoint"
            and e.payload.get("heard_text") == _TEXTS[0][:_A_WORD]
            for e in iter_events(speech.conn)
        ))
        speech.pipeline.stop_foreground_output("R", reason="barge_in")
        assert speech.pipeline.wait_until_idle(timeout_s=2)
        kind, payload = _terminal_for(speech.conn, response_id="R")
        assert kind == "surface.playback_interrupted"
        assert payload["heard_text"] == _TEXTS[0][:_A_WORD]
        # ADR 0083: the next turn is told where she stopped.
        line = _next_turn_line(speech.conn)
        assert line is not None
        assert f'{_TEXTS[0][:_A_WORD]}"; the rest was not spoken' in line
    finally:
        speech.close()
