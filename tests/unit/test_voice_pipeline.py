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
