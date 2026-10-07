"""``realtime.final_asr: whisper``: a voice turn's words from local Whisper, as 言文 hears them.

docs/plans/whisper-final-asr-proposal.md. mlx-whisper runs only on Apple
silicon, so a stand-in module takes its place: it records every call and
answers from a script. Everything around it is real: the recognizer's
decode rules, 言文's gates, the voice pipeline and its Event Log, and the
runtime's choice between SenseVoice and Whisper.
"""

# ruff: noqa: RUF001 — Whisper's own fullwidth punctuation.
from __future__ import annotations

import importlib.machinery
import math
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from jarvis.runtime import inherent_loop
from jarvis.runtime.dictation import COMMAND_PROMPT, load_user_terms, whisper_ears
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline

if TYPE_CHECKING:
    from pathlib import Path

_SIMPLIFIED = "以下是普通话的简体中文转录。"


class _Whisper:
    """Stands in for ``mlx_whisper``: ``texts`` answer the calls in order."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.language = "zh"
        self.logprob = -0.1

    def transcribe(self, _audio: np.ndarray, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.calls.append(kwargs)
        self.started.set()
        self.release.wait(5)
        text = self.texts.pop(0) if self.texts else ""
        return {"text": text, "language": self.language, "segments": [
            {"avg_logprob": self.logprob, "no_speech_prob": 0.0},
        ]}


@pytest.fixture
def whisper(monkeypatch: pytest.MonkeyPatch) -> _Whisper:
    """``import mlx_whisper`` finds the stand-in for the length of one test."""
    fake = _Whisper([])
    module = types.ModuleType("mlx_whisper")
    module.__spec__ = importlib.machinery.ModuleSpec("mlx_whisper", None)
    module.transcribe = fake.transcribe  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return fake


def _speech(seconds: float, level: float) -> bytes:
    """A tone at RMS ``level`` (full scale 1.0), PCM16 mono 16 kHz."""
    t = np.arange(int(seconds * 16000)) / 16000
    wave = level * math.sqrt(2) * np.sin(2 * np.pi * 220 * t)
    return (wave * 32767).astype("<i2").tobytes()


def test_whisper_decodes_as_yanwen_does(whisper: _Whisper) -> None:
    """Chinese, no window feeding the next, the word list after the simplified prompt."""
    whisper.texts = ["Jarvis 在吗？"]
    ears = voice_asr.MlxWhisperRecognizer(language="zh", terms=lambda: ["Jarvis", "言文"])
    result = ears.recognize(_speech(1.0, 0.1))
    assert result.text == "Jarvis 在吗？"
    (call,) = whisper.calls
    assert call["language"] == "zh"
    assert call["condition_on_previous_text"] is False
    assert call["temperature"] == 0.0
    assert call["initial_prompt"] == f"{_SIMPLIFIED} Common terms: Jarvis, 言文."


def test_a_looped_transcript_is_heard_again_without_the_list(whisper: _Whisper) -> None:
    """言文 e94d055: three repeats of one phrase, then temperature fallback, no word list."""
    whisper.texts = ["我都知道我都知道我都知道我都知道", "我都知道。"]
    ears = voice_asr.MlxWhisperRecognizer(language="zh", terms=lambda: ["Jarvis"])
    assert ears.recognize(_speech(1.0, 0.1)).text == "我都知道。"
    first, again = whisper.calls
    assert first["temperature"] == 0.0
    assert again["temperature"] == (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    assert again["initial_prompt"] == _SIMPLIFIED


def test_the_command_pass_decodes_short_and_hears_a_loop_as_nothing(whisper: _Whisper) -> None:
    """ADR 0137: at most 16 tokens, and a loop is no command, never retried hotter."""
    whisper.texts = ["退下退下退下退下退下退下", "退下。"]
    ears = whisper_ears(language="zh", prompt="常用口令：退下。")
    assert ears is not None
    assert ears.recognize(_speech(1.0, 0.1)).text == ""
    (call,) = whisper.calls
    assert call["sample_len"] == 16
    assert call["temperature"] == 0.0


@pytest.mark.parametrize("language", ["zh", "en"])
def test_a_voice_turn_hears_a_loop_as_nothing_without_a_hotter_retry(
    whisper: _Whisper, language: str,
) -> None:
    """2026-10-03: his laughter looped, a 4.6 s hotter retry still looped; SenseVoice must stand."""
    whisper.texts = ["beginning, beginning, beginning, beginning,", "beginning."]
    ears = whisper_ears(language=language, retry_loops=False)
    assert ears is not None
    assert ears.recognize(_speech(1.0, 0.1)).text == ""
    assert len(whisper.calls) == 1


@pytest.mark.parametrize(
    ("heard", "kept"),
    [
        ("谢谢观看。", ""),
        ("字幕由Amara.org社区提供", ""),
        ("Thanks for watching!", ""),
        (f"{_SIMPLIFIED}", ""),
        ("谢谢观看这部电影的人不多。", "谢谢观看这部电影的人不多。"),
    ],
)
def test_subtitle_credits_and_the_prompt_echo_are_silence(
    whisper: _Whisper, heard: str, kept: str,
) -> None:
    """What Whisper writes for room noise is dropped only when it is all there is."""
    whisper.texts = [heard]
    assert voice_asr.MlxWhisperRecognizer(language="zh").recognize(_speech(1.0, 0.1)).text == kept


def test_dictation_hears_a_stretch_as_yana_does(whisper: _Whisper) -> None:
    """ADR 0110: Whisper tells the language; the word list only within one 30 s window.

    Quiet speech is heard and a dead mic is not; Chinese clauses get full-width punctuation.
    """
    ears = whisper_ears(language="", terms=lambda: ["Claude Code"])
    assert ears is not None
    whisper.texts = ["用Claude Code跑一下,好吗?", "后半段"]
    assert voice_asr.dictation_text(_speech(2.0, 0.005), ears) == (
        "用Claude Code跑一下，好吗？", "zh",
    )
    assert voice_asr.dictation_text(_speech(31.0, 0.1), ears).text == "后半段"
    short, long = whisper.calls
    assert short["language"] is None
    assert short["initial_prompt"] == "Common terms: Claude Code."
    assert long["initial_prompt"] is None
    assert voice_asr.dictation_text(_speech(2.0, 0.002), ears).text == ""
    assert len(whisper.calls) == 2  # the dead mic never reached the model


@pytest.mark.parametrize(
    ("heard", "language", "logprob", "kept"),
    [
        ("好", "zh", -0.1, "好"),
        ("Okay.", "en", -0.1, "Okay."),
        ("you", "en", -2.0, ""),
        ("ねえ", "ja", -0.1, ""),
        ("This is longer than five.", "en", -2.0, "This is longer than five."),
    ],
)
def test_a_short_fragment_in_another_language_is_noise(
    whisper: _Whisper, heard: str, language: str, logprob: float, kept: str,
) -> None:
    """言字 a50ba52: Chinese is never too short; short non-Chinese only as confident English."""
    whisper.texts, whisper.language, whisper.logprob = [heard], language, logprob
    ears = whisper_ears(language="")
    assert ears is not None
    assert voice_asr.dictation_text(_speech(1.0, 0.1), ears).text == kept


def _final() -> tuple[voice_asr.WhisperFinalRecognizer, MagicMock]:
    partials = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    partials.partial_text.return_value = "明天"
    ears = voice_asr.WhisperFinalRecognizer(
        whisper=voice_asr.MlxWhisperRecognizer(language="zh"), partials=partials,
    )
    return ears, partials


def test_only_a_dead_mic_or_a_click_is_cut_before_the_model(whisper: _Whisper) -> None:
    """言文's floor: 0.15 s and some 0.2 s at 0.003 RMS. SenseVoice's 0.01 cut quiet speech."""
    whisper.texts = ["明天呢？"]
    ears, partials = _final()
    assert ears.recognize(_speech(0.1, 0.1)).text == ""  # a click
    assert ears.recognize(_speech(1.0, 0.002)).text == ""  # a dead mic
    assert whisper.calls == []
    quiet = _speech(1.0, 0.005)
    assert voice_asr.too_quiet_for_speech(quiet)  # SenseVoice's floor would drop it
    assert ears.recognize(quiet).text == "明天呢？"
    assert ears.partial_text(quiet) == "明天"
    partials.partial_text.assert_called_once_with(quiet)


@pytest.mark.parametrize(
    ("heard", "said"),
    [("好", "好。"), ("pause", "pause."), ("停，", "停，"), ("好的。", "好的。")],
)
def test_a_bare_answer_is_closed_the_way_sensevoice_closes_it(
    whisper: _Whisper, heard: str, said: str,
) -> None:
    """SenseVoice's text normalization ends every sentence; the rules after it expect that."""
    whisper.texts = [heard]
    ears, _partials = _final()
    assert ears.recognize(_speech(0.5, 0.1)).text == said


def test_a_one_word_card_answer_becomes_a_turn(whisper: _Whisper, tmp_path: Path) -> None:
    """Through the real pipeline: a bare 「好」 would be one character, which the filter drops."""
    whisper.texts = ["好"]
    ears, _partials = _final()
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=ears,
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=None,
    )
    pipeline.run_turn(
        audio_bytes=_speech(0.5, 0.1), turn_id="T-yes", channel="inherent_ptt", language="zh-CN",
    )
    conn = open_event_log(tmp_path / "events.db")
    (transcript,) = conn.execute(
        "SELECT json_extract(payload_json, '$.transcript') FROM events "
        "WHERE type = 'utterance.received'",
    ).fetchone()
    assert transcript == "好。"


def test_his_first_words_after_a_quiet_spell_warm_whisper_once(whisper: _Whisper) -> None:
    """言文 c257562: idle 20 s, one silent pass starts when he starts talking; no second one."""
    ears, _partials = _final()
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=MagicMock(), recognizer=ears, normalizer=MagicMock(), broadcaster=None,
        artifacts_dir=None,
    )
    whisper.release.clear()
    pipeline.warm_input_model()
    assert whisper.started.wait(2)
    pipeline.warm_input_model()  # still warming: no second pass
    whisper.release.set()
    deadline = time.monotonic() + 2
    while len(whisper.calls) < 1 or ears._whisper._warming.locked():  # noqa: SLF001 - wait for the pass
        assert time.monotonic() < deadline
        time.sleep(0.01)
    pipeline.warm_input_model()  # just used: warm enough
    time.sleep(0.05)
    assert len(whisper.calls) == 1


def _runtime(tmp_path: Path, final_asr: str) -> Any:  # noqa: ANN401 - a stand-in runtime
    vocab = tmp_path / "vocab.yaml"
    vocab.write_text("user: [Jarvis, 言文]\nauto: [自动词]\n", encoding="utf-8")
    return SimpleNamespace(config={
        "realtime": {"final_asr": final_asr},
        "dictation": {"vocab_path": str(vocab)},
    })


def test_the_switch_picks_the_recognizer(whisper: _Whisper, tmp_path: Path) -> None:
    """``whisper`` → Whisper finals, his user terms in the prompt; anything else → SenseVoice."""
    sensevoice = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    chosen = inherent_loop._final_recognizer(_runtime(tmp_path, "whisper"), sensevoice)  # noqa: SLF001
    assert isinstance(chosen, voice_asr.WhisperFinalRecognizer)
    whisper.texts = ["在吗？"]
    chosen.recognize(_speech(0.5, 0.1))
    assert whisper.calls[0]["initial_prompt"] == f"{_SIMPLIFIED} Common terms: Jarvis, 言文."
    assert load_user_terms(tmp_path / "vocab.yaml") == ["Jarvis", "言文"]
    assert inherent_loop._final_recognizer(  # noqa: SLF001
        _runtime(tmp_path, "sensevoice"), sensevoice,
    ) is sensevoice


def test_without_mlx_whisper_the_switch_keeps_sensevoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaged app ships without mlx-whisper: the switch falls back, the turn still hears."""
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # import fails, find_spec says None
    sensevoice = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    chosen = inherent_loop._final_recognizer(_runtime(tmp_path, "whisper"), sensevoice)  # noqa: SLF001
    assert chosen is sensevoice


def test_hybrid_gives_the_two_passes_the_word_list_but_not_the_command_pass(
    whisper: _Whisper, tmp_path: Path,
) -> None:
    """ADR 0150: the ADR 0137 command pass keeps its own prompt and its 16-token cap."""
    runtime = _runtime(tmp_path, "hybrid")
    runtime.config["realtime"]["final_asr_terms"] = ["Jarvis", "co-op"]
    sensevoice = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    chosen = inherent_loop._final_recognizer(runtime, sensevoice)  # noqa: SLF001
    assert isinstance(chosen, voice_asr.HybridFinalRecognizer)
    for ears, language in ((chosen._whisper_zh, "zh"), (chosen._whisper_en, "en")):  # noqa: SLF001
        whisper.calls.clear()
        whisper.texts = ["ok"]
        ears.recognize(_speech(1.0, 0.1))
        prompt = whisper.calls[0]["initial_prompt"]
        assert prompt.endswith("Common terms: Jarvis, co-op.")
        assert prompt.startswith(_SIMPLIFIED) is (language == "zh")
    command = chosen._whisper_command  # noqa: SLF001
    assert command is not None
    whisper.calls.clear()
    whisper.texts = ["退下。"]
    command.recognize(_speech(1.0, 0.1))
    assert whisper.calls[0]["initial_prompt"] == COMMAND_PROMPT
    assert whisper.calls[0]["sample_len"] == 16
