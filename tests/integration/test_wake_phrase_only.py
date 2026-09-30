"""A wake utterance that holds only "Hey Jarvis" is not a question.

2026-09-24 live: Allen said "Hey Jarvis", paused, and the acoustic endpoint
committed "Hey, Ja he." as a turn; the backend answered an empty question
before his real one arrived. The wake owner now keeps listening instead.
Said in one breath with the request, the wake phrase is cut off the front.
"""

# ruff: noqa: RUF001 — verbatim ASR output carries fullwidth commas.

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis.decision.tier0 import load_tier0_table, match_tier0
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_pipeline, voice_session
from tests.canary._helpers import repo_root
from tests.integration.test_wave3_single_audio_ingress import (
    _EnergySession,
    _FakeBackend,
    _FakeWakeEngine,
    _ingress,
    _RecordingPipeline,
    _wait_until,
)

if TYPE_CHECKING:
    from pathlib import Path

_AUDIO = b"\x10\x00" * 8000


def _pipeline(tmp_path: Path, text: str) -> voice_pipeline.VoicePipeline:
    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text=text, confidence=0.9, language_detected="en", emotion=None,
    )
    return voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=tmp_path,
    )


def _utterances(tmp_path: Path) -> int:
    conn = open_event_log(tmp_path / "events.db")
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'utterance.received'",
    ).fetchone()
    return int(count)


def _transcripts(tmp_path: Path) -> list[str]:
    conn = open_event_log(tmp_path / "events.db")
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.transcript') FROM events "
        "WHERE type = 'utterance.received' ORDER BY id",
    ).fetchall()
    return [str(text) for (text,) in rows]


@pytest.mark.parametrize(
    "text",
    [
        "Hey, Ja he.", "Hey, Javis hey.", "Hey, Ja, hey, Jara.", "Hey, ja, hey, ja.", "嘿贾维斯。",
        "嘿ja班javis斯。",
    ],
)
def test_a_wake_phrase_alone_writes_no_utterance(tmp_path: Path, text: str) -> None:
    """The transcripts the live log holds for a bare wake, word for word."""
    with pytest.raises(voice_pipeline.VoicePipelineWakeOnlyError):
        _pipeline(tmp_path, text).run_turn(
            audio_bytes=_AUDIO, turn_id="T-wake", channel="inherent_wake", language="zh-CN",
        )
    assert _utterances(tmp_path) == 0


@pytest.mark.parametrize(
    ("text", "channel"),
    [
        ("Hey Jarvis, what time is it?", "inherent_wake"),
        ("嘿傻呗，总结一下今天。", "inherent_wake"),
        ("Hey, Ja he.", "inherent_ptt"),
    ],
)
def test_a_question_or_a_ptt_press_still_commits(tmp_path: Path, text: str, channel: str) -> None:
    """Only a wake-channel transcript with nothing but the wake phrase is held back."""
    _pipeline(tmp_path, text).run_turn(
        audio_bytes=_AUDIO, turn_id="T-q", channel=channel, language="zh-CN",
    )
    assert _utterances(tmp_path) == 1


@pytest.mark.parametrize(
    ("text", "channel", "committed"),
    [
        # 2026-09-29, verbatim: the night run's words, missed by Tier 0 whole.
        ("嘿ja班javis斯,我要睡觉了，能让他继续跑吗？", "inherent_wake",
         "我要睡觉了，能让他继续跑吗？"),
        ("Hey, Jarvis, 现在几点了？", "inherent_wake", "现在几点了？"),
        ("嘿，现在几点了？", "inherent_wake", "现在几点了？"),
        # Words that only start like the wake phrase, and a press, stay whole.
        ("Javascript怎么学，告诉我", "inherent_wake", "Javascript怎么学，告诉我"),
        ("嘿ja班javis斯,我要睡觉了，能让他继续跑吗？", "inherent_ptt",
         "嘿ja班javis斯,我要睡觉了，能让他继续跑吗？"),
    ],
)
def test_a_wake_phrase_said_with_the_request_is_cut_off_the_front(
    tmp_path: Path, text: str, channel: str, committed: str,
) -> None:
    """The committed transcript starts at the request, where Tier 0 anchors."""
    _pipeline(tmp_path, text).run_turn(
        audio_bytes=_AUDIO, turn_id="T-lead", channel=channel, language="zh-CN",
    )
    assert _transcripts(tmp_path) == [committed]
    table = load_tier0_table(repo_root() / "config" / "tier0_patterns.yaml")
    hit = match_tier0(committed, table)
    if committed.startswith("我要睡觉了"):
        assert hit is not None
        assert hit.pattern_id == "night_start"
    if committed.startswith("现在几点了"):
        assert hit is not None
        assert hit.pattern_id == "time_now"


class _WakeOnlyFirst(_RecordingPipeline):
    """The first commit is a bare wake phrase; later ones are questions."""

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        super().run_turn(**kwargs)
        if len(self.calls) == 1:
            msg = "wake phrase only"
            raise voice_pipeline.VoicePipelineWakeOnlyError(msg)
        return MagicMock()


def test_after_a_bare_wake_the_next_utterance_commits_without_a_second_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One wake hit, two speech bursts: the second is heard as the question."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,  # noqa: SLF001 - the shipped Silero gate, loosened for one-frame onsets
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    wake_engine = _FakeWakeEngine(detections={0})
    pipeline = _WakeOnlyFirst()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=wake_engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=2.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
            ),
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch is not None
        for value in [0, 0, 0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: len(pipeline.calls) == 1)
        time.sleep(0.02)
        for value in [0, 0, 20_000, 21_000, 22_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: len(pipeline.calls) == 2)
        assert session.metrics().wake_detections == 1
        assert session.close().definitively_closed
