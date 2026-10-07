"""``realtime.final_asr: hybrid`` (ADR 0132): Whisper writes the long turns, SenseVoice the rest.

SenseVoice and Whisper are stand-ins with scripted words, languages and delays; everything
around them is real: the recognizer's choice and its background worker, the voice pipeline
and its Event Log, and the duplex session's assembler on a scripted ingress.
"""

# ruff: noqa: SLF001, RUF001 - the map's bound is only visible in the recognizer; fullwidth question marks
from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_pipeline, voice_session
from tests.integration.test_wave3_single_audio_ingress import (
    _EnergySession,
    _FakeBackend,
    _FakeWakeEngine,
    _ingress,
    _wait_until,
)

if TYPE_CHECKING:
    from pathlib import Path

_FRAME_BYTES = voice_audio.SILERO_CHUNK_SAMPLES * 2
_SENSEVOICE_WORDS = "SenseVoice heard this."


def _audio(seconds: float) -> bytes:
    """A tone loud enough for both recognizers' level gates, PCM16 mono 16 kHz."""
    t = np.arange(int(seconds * 16_000)) / 16_000
    return (0.1 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


def _sensevoice(language: str, text: str = _SENSEVOICE_WORDS, delay_s: float = 0.0) -> MagicMock:
    def recognize(_audio_pcm: bytes) -> voice_asr.TranscriptionResult:
        time.sleep(delay_s)
        return voice_asr.TranscriptionResult(text, 0.9, language, "NEUTRAL")

    fake = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    fake.recognize.side_effect = recognize
    return fake


def _whisper(
    words: str | Exception | None = None,
    *,
    language: str = "zh",
    delay_s: float = 0.0,
    gate: threading.Event | None = None,
) -> MagicMock:
    """Answers ``words``, or ``heard <bytes>`` when none is given: a test sees which audio it heard.

    ``gate`` holds every pass until set; ``fake.peak`` is the most passes ever running at once.
    """
    lock = threading.Lock()
    running = [0]

    def recognize(audio_pcm: bytes) -> voice_asr.TranscriptionResult:
        with lock:
            running[0] += 1
            fake.peak = max(fake.peak, running[0])
        try:
            if gate is not None:
                gate.wait(5)
            time.sleep(delay_s)
            if isinstance(words, Exception):
                raise words
            text = f"heard {len(audio_pcm)}" if words is None else words
            return voice_asr.TranscriptionResult(text, 0.5, language, None)
        finally:
            with lock:
                running[0] -= 1

    fake = MagicMock(spec=voice_asr.MlxWhisperRecognizer)
    fake.recognize.side_effect = recognize
    fake.last_used = 0.0
    fake.peak = 0
    return fake


def _hybrid(
    sensevoice: MagicMock,
    zh: MagicMock | None = None,
    en: MagicMock | None = None,
    command: MagicMock | None = None,
) -> voice_asr.HybridFinalRecognizer:
    return voice_asr.HybridFinalRecognizer(
        sensevoice=sensevoice,
        whisper_zh=zh or _whisper(),
        whisper_en=en or _whisper(),
        whisper_command=command,
    )


@pytest.mark.parametrize(
    ("speech_s", "text", "whisper_calls"),
    [
        (0.9, _SENSEVOICE_WORDS, 0),
        (0.999, _SENSEVOICE_WORDS, 0),
        (1.0, "whisper heard this.", 1),
        (4.0, "whisper heard this.", 1),
    ],
)
def test_one_second_of_speech_is_where_whisper_takes_over(
    speech_s: float, text: str, whisper_calls: int,
) -> None:
    """Under 1 s Whisper invents words ("Yeah" became "Thank you"): SenseVoice's text stands."""
    zh = _whisper("whisper heard this")
    ears = _hybrid(_sensevoice("zh"), zh=zh)
    assert ears.recognize_prepared("U1", _audio(2.0), speech_s).text == text
    assert zh.recognize.call_count == whisper_calls


@pytest.mark.parametrize(
    ("language", "hearer", "said", "kept"),
    [
        ("zh", "zh", "你好吗", "你好吗。"),
        ("yue", "zh", "你好吗？", "你好吗？"),  # SenseVoice misreading Mandarin as Cantonese
        ("en", "en", "Count to ten", "Count to ten."),
        ("en", "en", "Count to ten.", "Count to ten."),
    ],
)
def test_whisper_hears_in_the_language_sensevoice_named(
    language: str, hearer: str, said: str, kept: str,
) -> None:
    """Chinese and Cantonese go to the Chinese Whisper, English to its own; bare text is closed."""
    whispers = {"zh": _whisper(said), "en": _whisper(said, language="en")}
    ears = _hybrid(_sensevoice(language), zh=whispers["zh"], en=whispers["en"])
    heard = ears.recognize_prepared("U1", _audio(2.0), 2.0)
    assert {name: w.recognize.call_count for name, w in whispers.items()} == {
        "zh": int(hearer == "zh"), "en": int(hearer == "en"),
    }
    assert heard.text == kept
    assert heard.language_detected == language  # SenseVoice's, even where Whisper reports its own
    assert heard.emotion == "NEUTRAL"


def test_another_language_stays_with_sensevoice() -> None:
    """Japanese has no Whisper here: neither is called and SenseVoice's result is returned."""
    zh, en = _whisper("zh"), _whisper("en")
    heard = _hybrid(_sensevoice("ja"), zh=zh, en=en).recognize_prepared("U1", _audio(2.0), 2.0)
    assert heard.text == _SENSEVOICE_WORDS
    assert heard.language_detected == "ja"
    assert zh.recognize.call_count == en.recognize.call_count == 0


@pytest.mark.parametrize("words", ["", "  ", RuntimeError("mlx failed")])
def test_an_empty_or_failing_whisper_keeps_sensevoices_words(words: str | Exception) -> None:
    """The upgrade never costs the turn: nothing from Whisper, or Whisper raising, falls back."""
    ears = _hybrid(_sensevoice("zh"), zh=_whisper(words))
    assert ears.recognize_prepared("U1", _audio(2.0), 2.0).text == _SENSEVOICE_WORDS


def test_whispers_level_gate_still_applies() -> None:
    """A dead mic or a click never reaches the model, whatever the VAD called speech."""
    zh = _whisper("whisper heard this")
    ears = _hybrid(_sensevoice("zh"), zh=zh)
    assert ears.recognize_prepared("U1", bytes(64_000), 2.0).text == _SENSEVOICE_WORDS
    assert ears.recognize_prepared("U1", _audio(0.1), 2.0).text == _SENSEVOICE_WORDS
    assert zh.recognize.call_count == 0


def test_a_prepared_pass_is_reused_and_the_commit_waits_only_for_what_is_left() -> None:
    """Prepared at the pause, collected at the commit: the commit's audio extends the prefix."""
    prepared_audio = _audio(2.0)
    commit_audio = prepared_audio + bytes(18 * _FRAME_BYTES)  # the silence up to the endpoint
    zh, sensevoice = _whisper(delay_s=0.6), _sensevoice("zh")
    ears = _hybrid(sensevoice, zh=zh)
    ears.prepare("U1", prepared_audio, 2.0)
    time.sleep(0.3)  # the commit comes half way through the pass
    committed_at = time.monotonic()
    heard = ears.recognize_prepared("U1", commit_audio, 0.0)  # the fallback's speech is not used
    waited = time.monotonic() - committed_at
    assert heard.text == f"heard {len(prepared_audio)}."
    assert zh.recognize.call_count == sensevoice.recognize.call_count == 1
    assert 0.1 < waited < 0.5, f"waited {waited:.2f} s for the rest of a 0.6 s pass begun 0.3 s ago"


def test_a_short_utterance_is_heard_at_the_commit_not_at_the_pause() -> None:
    """Under 1 s nothing is prepared: SenseVoice decodes the committed audio, as it does alone."""
    sensevoice = _sensevoice("zh")
    ears = _hybrid(sensevoice)
    at_the_pause = _audio(0.5)
    at_the_commit = at_the_pause + bytes(18 * _FRAME_BYTES)
    ears.prepare("U1", at_the_pause, 0.5)
    assert not ears._prepared
    assert ears.recognize_prepared("U1", at_the_commit, 0.5).text == _SENSEVOICE_WORDS
    sensevoice.recognize.assert_called_once_with(at_the_commit)


def test_a_discarded_prepare_is_not_used() -> None:
    """Allen spoke again: the pass for the shorter audio is dropped, the commit hears its own."""
    prepared_audio = _audio(2.0)
    commit_audio = prepared_audio + _audio(1.0)
    ears = _hybrid(_sensevoice("zh"))
    ears.prepare("U1", prepared_audio, 2.0)
    ears.discard("U1")
    heard = ears.recognize_prepared("U1", commit_audio, 3.0)
    assert heard.text == f"heard {len(commit_audio)}."


def test_a_stale_prepare_is_not_used() -> None:
    """Audio that does not extend what was prepared is heard fresh, with the fallback's speech."""
    ears = _hybrid(_sensevoice("zh"))
    ears.prepare("U1", _audio(2.0), 2.0)
    other = _audio(1.5)
    assert ears.recognize_prepared("U1", other, 1.5).text == f"heard {len(other)}."
    # Nothing left under the id, and a short fallback keeps SenseVoice.
    assert ears.recognize_prepared("U1", other, 0.5).text == _SENSEVOICE_WORDS


def test_the_newest_prepare_per_utterance_wins() -> None:
    """A second pause replaces the first; the commit collects the later, longer pass."""
    first, second = _audio(2.0), _audio(3.0)
    ears = _hybrid(_sensevoice("zh"))
    ears.prepare("U1", first, 2.0)
    ears.prepare("U1", second, 3.0)
    heard = ears.recognize_prepared("U1", second + bytes(_FRAME_BYTES), 0.0)
    assert heard.text == f"heard {len(second)}."


def test_prepared_passes_are_dropped_when_collected_and_bounded_when_not() -> None:
    """A commit frees its entry; utterances that never commit cannot grow the map past its bound."""
    gate = threading.Event()
    ears = _hybrid(_sensevoice("zh"), zh=_whisper(gate=gate))
    audio = _audio(2.0)
    for index in range(10):
        ears.prepare(f"U{index}", audio, 2.0)
    assert set(ears._prepared) == {f"U{index}" for index in range(6, 10)}
    gate.set()
    ears.recognize_prepared("U9", audio, 2.0)
    assert "U9" not in ears._prepared
    ears.discard("U8")
    assert "U8" not in ears._prepared


def test_the_worker_hears_one_pass_at_a_time_and_ends_when_idle() -> None:
    """A running pass cannot be interrupted, so passes queue behind it; no thread lingers idle."""
    zh = _whisper(delay_s=0.05)
    ears = _hybrid(_sensevoice("zh"), zh=zh)
    audio = _audio(2.0)
    for index in range(3):
        ears.prepare(f"U{index}", audio, 2.0)
    for index in range(3):
        assert ears.recognize_prepared(f"U{index}", audio, 2.0).text == f"heard {len(audio)}."
    assert zh.peak == 1
    _wait_until(lambda: ears._worker is None)


def test_without_a_prepared_pass_run_turn_hears_with_the_speech_the_session_measured(
    tmp_path: Path,
) -> None:
    """Through the real pipeline: a supplied utterance id and speech_s reach recognize_prepared."""
    zh = _whisper("whisper heard this")
    pipeline = _pipeline(tmp_path, _hybrid(_sensevoice("zh"), zh=zh))
    for utterance_id, speech_s, turn in (("U-long", 2.0, "T-long"), ("U-short", 0.5, "T-short")):
        pipeline.run_turn(
            audio_bytes=_audio(2.0), turn_id=turn, channel="inherent_wake", language="zh-CN",
            utterance_id=utterance_id, speech_s=speech_s,
        )
    assert _transcripts(tmp_path) == ["whisper heard this.", _SENSEVOICE_WORDS]
    assert zh.recognize.call_count == 1


def _pipeline(tmp_path: Path, recognizer: Any) -> voice_pipeline.VoicePipeline:  # noqa: ANN401
    return voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=None,
    )


def _transcripts(tmp_path: Path) -> list[str]:
    conn = open_event_log(tmp_path / "events.db")
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.transcript') FROM events "
        "WHERE type = 'utterance.received' ORDER BY id",
    ).fetchall()
    return [transcript for (transcript,) in rows]


class _SpyPipeline(voice_pipeline.VoicePipeline):
    """The real pipeline, noting every hint the session hands it."""

    hints: list[tuple[str, str, int, float]]

    def prepare_final(self, utterance_id: str, audio_bytes: bytes, speech_s: float) -> None:
        self.hints.append(("prepare", utterance_id, len(audio_bytes), speech_s))
        super().prepare_final(utterance_id, audio_bytes, speech_s)

    def discard_final(self, utterance_id: str) -> None:
        self.hints.append(("discard", utterance_id, 0, 0.0))
        super().discard_final(utterance_id)


def test_the_session_prepares_each_pause_discards_on_speech_and_commits_the_prepared_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Conversation mode, real pipeline: 5 silent frames are no pause, 7 are, speech discards it.

    The endpoint is 12 silent frames here (24 in production); the pause that ends the utterance
    is prepared at its sixth, and the committed turn is the words of that pass, not a new one.
    """
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS, "record", voice_audio.VadThresholds(0.4, -45.0, 1, 1, 12),
    )
    zh = _whisper()
    pipeline = _SpyPipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_hybrid(_sensevoice("zh"), zh=zh),
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=None,
    )
    pipeline.hints = []
    backend = _FakeBackend()
    ingress = _ingress(backend)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=_FakeWakeEngine(detections=set()),
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=10.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
            ),
            conversation=lambda: True,
        )
        assert session.start().started
    epoch = ingress.stream_epoch
    assert epoch is not None

    def frames(value: int, count: int) -> None:
        for _ in range(count):
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)

    try:
        frames(0, 4)  # armed and idle: the pre-roll
        frames(10_000, 40)
        frames(0, 5)  # under 6 silent frames: not a pause
        frames(10_000, 20)
        assert pipeline.hints == []
        frames(0, 8)  # a pause: prepared at its sixth silent frame
        frames(10_000, 4)  # speech again: discarded
        frames(10_000, 10)
        frames(0, 14)  # the pause that ends it: prepared at the sixth, committed at the twelfth
        _wait_until(
            lambda: len(pipeline.hints) == 4 and len(_transcripts(tmp_path)) == 1, timeout_s=5,
        )
    finally:
        assert session.close().definitively_closed

    first, resumed, last, collected = pipeline.hints
    assert [hint[0] for hint in pipeline.hints] == ["prepare", "discard", "prepare", "discard"]
    assert len({hint[1] for hint in pipeline.hints}) == 1  # one utterance throughout
    # Speech runs from the first to the last speech frame, quiet gaps inside it counted.
    assert first[3] == pytest.approx((40 + 5 + 20) * 0.032)
    assert last[3] == pytest.approx((40 + 5 + 20 + 8 + 4 + 10) * 0.032)
    # What grew between the two prepares: 2 more silent frames of the first pause, the 4 + 10
    # speech frames, and 6 silent frames of the second pause.
    assert last[2] - first[2] == (2 + 4 + 10 + 6) * _FRAME_BYTES
    assert resumed[1] == collected[1]  # the last discard is the commit's cleanup, a no-op by then
    # The turn is the prepared pass's words; a fresh pass over the committed audio (12 silent
    # frames in, 6 more than prepared) would say "heard <that length>".
    assert _transcripts(tmp_path) == [f"heard {last[2]}."]
    assert zh.recognize.call_count <= 2  # the first pause's pass may or may not have started


def test_each_finished_pass_is_announced_even_when_its_entry_was_dropped_meanwhile() -> None:
    """ADR 0145: the captions use a pass for the audio up to a pause after Allen kept speaking."""
    told: list[tuple[str, int, str]] = []
    gate = threading.Event()
    ears = _hybrid(_sensevoice("zh"), zh=_whisper("你好吗", gate=gate))
    ears.on_prepared_text(lambda *heard: told.append(heard))
    audio = _audio(2.0)
    ears.prepare("U1", audio, 2.0)
    _wait_until(lambda: ears._whisper_zh.peak == 1)  # the pass is running
    ears.discard("U1")  # he spoke again: nobody will collect it
    gate.set()
    _wait_until(lambda: told == [("U1", len(audio), "你好吗。")])


def test_a_pass_with_no_words_or_a_failing_listener_costs_nothing() -> None:
    """Nothing is announced for empty text, and a listener that raises never breaks the pass."""
    told: list[str] = []
    ears = _hybrid(_sensevoice("zh", text=""), zh=_whisper(""))
    ears.on_prepared_text(lambda *heard: told.append(heard[2]))
    ears.prepare("U1", _audio(2.0), 2.0)
    assert ears.recognize_prepared("U1", _audio(2.0), 2.0).text == ""
    ears = _hybrid(_sensevoice("zh"), zh=_whisper("好"))
    ears.on_prepared_text(lambda *_heard: 1 / 0)
    ears.prepare("U2", _audio(2.0), 2.0)
    assert ears.recognize_prepared("U2", _audio(2.0), 2.0).text == "好。"
    assert told == []


@pytest.mark.parametrize("hybrid", [True, False])
def test_the_captions_show_the_prepared_pass_as_settled_and_the_rest_as_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, hybrid: bool,
) -> None:
    """Conversation mode, real pipeline and session: a pause's Whisper words become ``settled``.

    SenseVoice's partials say 尾 and the number of frames they were given; Whisper's pass says
    "whisper words". After the pause the caption is Whisper's words plus what was said after it.
    With SenseVoice alone the caption is the partials, as before, and carries no split.
    """
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS, "record", voice_audio.VadThresholds(0.4, -45.0, 1, 1, 40),
    )
    sensevoice = _sensevoice("zh")
    sensevoice.partial_text.side_effect = lambda audio: f"尾{len(audio) // _FRAME_BYTES}"
    broadcaster = MagicMock()
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_hybrid(sensevoice, zh=_whisper("whisper words")) if hybrid else sensevoice,
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=None,
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=_FakeWakeEngine(detections=set()),
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=10.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
                partial_asr=voice_session.PartialAsrConfig(captions=True, interval_ms=32),
            ),
            conversation=lambda: True,
        )
        assert session.start().started
    epoch = ingress.stream_epoch
    assert epoch is not None

    def partials() -> list[dict[str, Any]]:
        return [
            {"phase": call.args[0], **call.kwargs}
            for call in broadcaster.broadcast_voice_sync.call_args_list
            if call.args[0] == "partial"
        ]

    def frames(value: int, count: int) -> None:
        for _ in range(count):
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)

    try:
        frames(0, 4)
        frames(10_000, 40)  # 1.3 s of speech: long enough for Whisper
        frames(0, 8)  # a pause: the pass for the audio so far
        if hybrid:
            _wait_until(
                lambda: any(p["settled"] == "whisper words" for p in partials()), timeout_s=5,
            )
        else:
            time.sleep(0.3)  # nothing settles it; give a pass the time it would need
        frames(10_000, 6)  # more words after it; captions move on with each frame
        _wait_until(
            lambda: frames(10_000, 1) or "尾" in partials()[-1]["text"], timeout_s=5,
        )
    finally:
        assert session.close().definitively_closed

    shown = partials()
    last = shown[-1]
    if not hybrid:
        assert all(set(p) == {"phase", "turn_id", "text"} for p in shown)
        assert "whisper words" not in last["text"]
        return
    assert shown[0]["settled"] == ""  # before any pass the whole caption is tail
    assert last["settled"] == "whisper words"
    assert last["text"] == last["settled"] + last["tail"]
    # The tail decodes only the frames after the cut, so it is far shorter than the utterance.
    assert int(last["tail"].removeprefix("尾")) < 40


def test_a_recognizer_that_prepares_nothing_has_no_prepared_text(tmp_path: Path) -> None:
    """SenseVoice alone and Whisper alone: registering a listener is a no-op, captions stay tail."""
    told: list[tuple[str, int, str]] = []
    for recognizer in (
        _sensevoice("zh"),
        voice_asr.WhisperFinalRecognizer(
            whisper=MagicMock(spec=voice_asr.MlxWhisperRecognizer),
            partials=_sensevoice("zh"),
        ),
    ):
        pipeline = _pipeline(tmp_path, recognizer)
        assert not pipeline.on_prepared_text(lambda *heard: told.append(heard))
    assert told == []
    assert _pipeline(tmp_path, _hybrid(_sensevoice("zh"))).on_prepared_text(lambda *_heard: None)


@pytest.mark.parametrize(
    ("first", "again", "kept", "second_passes"),
    [
        ("对下。", "退下", "退下。", 1),  # misheard 退下, heard again as the command
        ("对下。", "退下吧。", "退下吧。", 1),
        ("你是谁？", "再见。", "你是谁？", 0),  # a question is not a mishearing
        ("对下。", "Peace out!", "对下。", 1),  # English from the second pass is never a command
        ("开灯。", "开灯。", "开灯。", 1),  # heard again, nothing new: the first stands
        ("对下。", "对象。", "对下。", 1),
        ("退下。", "退下。", "退下。", 0),  # a command already
        ("等我一下。", "退下。", "等我一下。", 0),
        ("请你退下吧。", "退下。", "请你退下吧。", 0),  # six characters: not a short line
        ("嗯嗯。", "退下。", "嗯嗯。", 0),  # a listening sound already
    ],
)
def test_a_short_unclear_line_is_heard_again_for_a_command(
    first: str, again: str, kept: str, second_passes: int,
) -> None:
    """ADR 0137: only a 2-4 character non-question line that is no command gets a second hearing."""
    command = _whisper(again)
    zh = _whisper("whisper must not hear a short clip")
    ears = _hybrid(_sensevoice("zh", first), zh=zh, command=command)
    heard = ears.recognize_prepared("U1", _audio(0.5), 0.5)
    assert heard.text == kept
    assert command.recognize.call_count == second_passes
    assert heard.language_detected == "zh"
    assert heard.emotion == "NEUTRAL"


def test_a_long_turns_short_looking_whisper_text_is_heard_again_too() -> None:
    """The check runs on whichever transcript was decided, Whisper's included."""
    command = _whisper("退下")
    ears = _hybrid(_sensevoice("zh"), zh=_whisper("对下"), command=command)
    assert ears.recognize_prepared("U1", _audio(2.0), 2.0).text == "退下。"
    assert command.recognize.call_count == 1


def test_a_failing_second_hearing_keeps_the_first_transcript() -> None:
    """A second pass that raises costs nothing."""
    command = _whisper(RuntimeError("mlx failed"))
    ears = _hybrid(_sensevoice("zh", "对下。"), command=command)
    assert ears.recognize_prepared("U1", _audio(0.5), 0.5).text == "对下。"
    assert command.recognize.call_count == 1


def test_no_second_hearing_without_the_command_whisper_or_for_other_languages() -> None:
    """No ``whisper_command`` is the old behavior; English lines are never heard again."""
    plain = _hybrid(_sensevoice("zh", "对下。"))
    assert plain.recognize_prepared("U1", _audio(0.5), 0.5).text == "对下。"
    command = _whisper("退下")
    ears = _hybrid(_sensevoice("en", "对下。"), command=command)
    assert ears.recognize_prepared("U1", _audio(0.5), 0.5).text == "对下。"
    assert command.recognize.call_count == 0
