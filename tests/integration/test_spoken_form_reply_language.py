"""ADR 0045 + ADR 0052: the ``reply_language`` setting pins the spoken form's language.

Drives the real ``decide()`` with a stub model that records each request's system prompt.
Real case (2026-10-01): a Chinese option tapped on an ask card ran as Allen's words, the
written answer was English under ``reply_language: en``, and the spoken form was Chinese.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    _SPOKEN_FORM_PROMPT_EN,
    _SPOKEN_FORM_PROMPT_ZH,
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.decision.stream_envelope import split_envelope
from jarvis.decision.tier0 import load_tier0_table
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared import lang
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from collections.abc import Iterator

TIER0_TABLE = Path(__file__).parents[2] / "config" / "tier0_patterns.yaml"

TRANSCRIPT = "日期: 这周五，10月2日"  # noqa: RUF001 — fullwidth comma, a Chinese ask-card answer.
LONG_ENGLISH = (
    "Friday, October 2 works for the review. I put it on the calendar and moved the "
    "other meeting on that day to Monday so nothing overlaps with it. The invite went to "
    "everyone on the thread, the room is booked from ten to eleven, and the agenda doc "
    "is linked in the event description for anyone who wants to read ahead."
)
SPOKEN = "Friday works."


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _RecordingClient:
    """Answers the first request with ``answer``, the spoken-form request with a short line."""

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.systems: list[str] = []
        self.model = "stub-model"

    @property
    def last_input_tokens(self) -> int | None:
        return 0

    @property
    def last_output_tokens(self) -> int | None:
        return 0

    @property
    def last_finish_reason(self) -> str | None:
        return "stop"

    @contextmanager
    def fresh_context(self) -> Iterator[_RecordingClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002
        system: str,
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.systems.append(system)
        text = self._answer if len(self.systems) == 1 else SPOKEN
        return ChatResult(
            text=text,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _spoken_form_system(
    tmp_path: Path, reply_language: str, *, answer: str = LONG_ENGLISH, channel: str | None = None
) -> tuple[list[str], str]:
    """The system prompts of one turn's requests, and its final plan text."""
    llm = _RecordingClient(answer)
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        reply_language=reply_language,
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": TRANSCRIPT, "turn_id": "T_lang"}
        | ({} if channel is None else {"channel": channel}),
        correlation={"turn_id": "T_lang"},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return llm.systems, result.response_plan.text


@pytest.mark.parametrize(
    ("reply_language", "prompt"),
    [
        ("en", _SPOKEN_FORM_PROMPT_EN),
        ("follow", _SPOKEN_FORM_PROMPT_ZH),
    ],
)
def test_reply_language_picks_the_spoken_form_prompt(
    tmp_path: Path, reply_language: str, prompt: str
) -> None:
    """A Chinese transcript and a long English answer: ``en`` speaks English, ``follow`` Chinese."""
    systems, text = _spoken_form_system(tmp_path, reply_language)
    assert len(systems) == 2, "one answer request, one spoken-form request"
    assert systems[1] == prompt
    voice, document = split_envelope(text)[:2]
    assert (voice, document) == (SPOKEN, LONG_ENGLISH)


def test_a_typed_turn_makes_no_spoken_form_request_and_shows_its_answer_once(
    tmp_path: Path,
) -> None:
    """ADR 0181: the owner's typed turn is never spoken, so nothing rewrites its answer."""
    (tmp_path / "typed").mkdir()
    (tmp_path / "voice").mkdir()
    systems, text = _spoken_form_system(tmp_path / "typed", "en", channel="cli_stdin")
    assert len(systems) == 1
    assert text == LONG_ENGLISH
    # Positive control: the same answer to a turn he spoke still gets the rewrite.
    systems, text = _spoken_form_system(tmp_path / "voice", "en", channel="inherent_ptt")
    assert len(systems) == 2
    assert split_envelope(text)[:2] == (SPOKEN, LONG_ENGLISH)


def test_short_english_answer_under_en_is_spoken_as_written(tmp_path: Path) -> None:
    """``en`` makes a short plain English answer "in the reply language": no rewrite at all."""
    systems, text = _spoken_form_system(tmp_path, "en", answer="Friday works.")
    assert len(systems) == 1
    assert text == "Friday works."


def test_short_english_answer_under_follow_is_rewritten_in_chinese(tmp_path: Path) -> None:
    """Without the setting, a Chinese transcript still gets a Chinese rewrite (ADR 0045)."""
    systems, _ = _spoken_form_system(tmp_path, "follow", answer="Friday works.")
    assert systems[1:] == [_SPOKEN_FORM_PROMPT_ZH]


def _tier0_answer(tmp_path: Path, transcript: str, reply_language: str) -> str:
    """One real Tier 0 turn (no model call) with the system language English."""
    lang.set_language("en")

    class _NoModel:
        model = "stub-model"

        def chat(self, **_: object) -> ChatResult:
            msg = "Tier 0 never asks the model"
            raise AssertionError(msg)

    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts"
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", build_default_registry()),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", _NoModel()),
        system_prompt="stub system prompt",
        tier0_table=load_tier0_table(TIER0_TABLE),
        reply_language=reply_language,
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": "T_tier0"},
        correlation={"turn_id": "T_tier0"},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return split_envelope(result.response_plan.text)[0] or result.response_plan.text


@pytest.mark.parametrize(
    ("transcript", "reply_language", "language", "opening"),
    [
        ("现在几点？", "follow", "zh", "现在是"),  # noqa: RUF001 — Allen's fullwidth question mark.
        ("今天几号", "follow", "zh", "今天是"),
        ("What time is it?", "follow", "en", "It's "),
        ("What's the date?", "follow", "en", "Today is "),
        ("现在几点？", "en", "en", "It's "),  # noqa: RUF001
        ("What time is it?", "zh", "zh", "现在是"),
    ],
)
def test_tier0_answer_follows_the_reply_language(
    tmp_path: Path, transcript: str, reply_language: str, language: str, opening: str
) -> None:
    """System language en: a Tier 0 time or date answer is in his words' language, or the pin."""
    said = _tier0_answer(tmp_path, transcript, reply_language)
    assert said.startswith(opening), said
    assert any("\u4e00" <= ch <= "\u9fff" for ch in said) is (language == "zh"), said
    if opening in ("现在是", "It's "):  # the clock itself, not only the sentence around it
        assert ("AM" in said or "PM" in said) is (language == "en"), said
