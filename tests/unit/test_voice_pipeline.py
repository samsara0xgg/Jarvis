"""ADR-0005 voice_pipeline — composition + VOICE_INPUT_LOCK invariants."""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline

if TYPE_CHECKING:
    from pathlib import Path


def _make_recognizer_returning(text: str) -> voice_asr.AsrRecognizer:
    rec = MagicMock(spec=voice_asr.AsrRecognizer)
    rec.recognize.return_value = voice_asr.TranscriptionResult(
        text=text,
        confidence=0.9,
        language_detected="zh-CN",
        emotion="NEUTRAL",
    )
    return rec


def test_run_turn_normalizes_before_emit(tmp_path: Path) -> None:
    """ADR §8 fix #1: emit_event must see the normalized transcript."""
    norm = voice_asr.AsrNormalizer(
        corrections=[], aliases={"卧室主灯": ["卧室灯"]}, fuzzy_enabled=False,
    )
    recognizer = _make_recognizer_returning("打开卧室灯")
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=None,  # PTT-style; no broadcast on this path
        artifacts_dir=tmp_path,
    )
    audio = b"\x10\x00" * 16000  # non-silent
    ev = pipeline.run_turn(
        audio_bytes=audio,
        turn_id="T1",
        channel="inherent_ptt",
        language="zh-CN",
    )
    assert ev.payload["transcript"] == "打开卧室主灯", "normalize must run before emit"


def test_run_turn_raises_empty_for_silent_audio(tmp_path: Path) -> None:
    """ADR §8 fix #3: unified empty-filter rejects silent audio."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    recognizer = _make_recognizer_returning("你好")
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    audio = b"\x00" * 2000  # zero RMS
    with pytest.raises(voice_pipeline.VoicePipelineEmptyError):
        pipeline.run_turn(
            audio_bytes=audio,
            turn_id="T2",
            channel="inherent_ptt",
            language="zh-CN",
        )


def test_run_turn_raises_busy_when_lock_is_held(tmp_path: Path) -> None:
    """ADR §8 fix #2: VOICE_INPUT_LOCK is the wake/PTT mutex."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_make_recognizer_returning("你好"),
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )

    voice_pipeline.VOICE_INPUT_LOCK.acquire()
    try:
        with pytest.raises(voice_pipeline.VoiceInputBusyError):
            pipeline.run_turn(
                audio_bytes=b"\x10\x00" * 16000,
                turn_id="T3",
                channel="inherent_ptt",
                language="zh-CN",
                lock_acquire_timeout_s=0.1,
            )
    finally:
        voice_pipeline.VOICE_INPUT_LOCK.release()


def test_run_turn_releases_lock_on_exception(tmp_path: Path) -> None:
    """The lock is released whether emit succeeds, raises empty, or raises ASR error."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_make_recognizer_returning("你好"),
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    # First turn: empty -> raises. Lock must be free afterward.
    with pytest.raises(voice_pipeline.VoicePipelineEmptyError):
        pipeline.run_turn(
            audio_bytes=b"\x00" * 2000,
            turn_id="T4a",
            channel="inherent_ptt",
            language="zh-CN",
        )
    # Should succeed: lock was released.
    ev = pipeline.run_turn(
        audio_bytes=b"\x10\x00" * 16000,
        turn_id="T4b",
        channel="inherent_ptt",
        language="zh-CN",
    )
    assert ev.payload["transcript"] == "你好"


def test_run_turn_skips_inner_acquire_when_lock_held_by_caller(tmp_path: Path) -> None:
    """ADR-0005 §8 fix #2 review: caller may hold the lock; run_turn must skip its inner acquire."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好", confidence=0.9, language_detected=None, emotion=None,
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )

    # Caller holds the lock; run_turn must NOT try to acquire (would deadlock).
    voice_pipeline.VOICE_INPUT_LOCK.acquire()
    try:
        ev = pipeline.run_turn(
            audio_bytes=b"\x10\x00" * 16000,
            turn_id="T9",
            channel="inherent_wake",
            language="zh-CN",
            lock_already_held=True,
        )
        assert ev.payload["transcript"] == "你好"
        # Lock should STILL be held after run_turn returns (caller owns the release).
        assert voice_pipeline.VOICE_INPUT_LOCK.locked()
    finally:
        voice_pipeline.VOICE_INPUT_LOCK.release()


def test_run_turn_skips_broadcast_when_broadcast_disabled(tmp_path: Path) -> None:
    """PTT path: caller passes broadcast=False so phase envelopes don't fire (ADR §6)."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    broadcaster = MagicMock()
    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好", confidence=0.9, language_detected=None, emotion=None,
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=broadcaster,
        artifacts_dir=tmp_path,
    )

    pipeline.run_turn(
        audio_bytes=b"\x10\x00" * 16000,
        turn_id="T10",
        channel="inherent_ptt",
        language="zh-CN",
        broadcast=False,
    )
    broadcaster.broadcast_voice_sync.assert_not_called()


def test_run_turn_skips_broadcast_empty_when_broadcast_disabled(tmp_path: Path) -> None:
    """PTT path: even the empty-utterance branch must not broadcast (ADR §6)."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    broadcaster = MagicMock()
    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好", confidence=0.9, language_detected=None, emotion=None,
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=broadcaster,
        artifacts_dir=tmp_path,
    )

    with pytest.raises(voice_pipeline.VoicePipelineEmptyError):
        pipeline.run_turn(
            audio_bytes=b"\x00" * 2000,  # zero RMS -> empty filter trips
            turn_id="T11",
            channel="inherent_ptt",
            language="zh-CN",
            broadcast=False,
        )
    broadcaster.broadcast_voice_sync.assert_not_called()
