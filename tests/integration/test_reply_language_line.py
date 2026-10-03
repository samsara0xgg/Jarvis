"""ADR 0135: the live user message ends with a line naming the reply language.

Drives the real ``decide()`` with a stub model that records the messages it was sent.
Real case (2026-10-02): with ``reply_language: follow`` the model answered in the other
language on 7% of turns; one line after his words fixed 10 of 28 such samples to 0.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ENGLISH_LINE = "[Reply language for this turn: English]"
CHINESE_LINE = "[Reply language for this turn: Chinese]"


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path


class _RecordingClient:
    def __init__(self) -> None:
        self.live: list[str] = []
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
        messages: list[dict[str, Any]],
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.live.append(str(messages[-1]["content"]))
        return ChatResult(
            text="Okay.",
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _live_message(tmp_path: Path, transcript: str, reply_language: str) -> tuple[str, list[str]]:
    """The first request's last message, and what ``record_sent_message`` kept."""
    llm = _RecordingClient()
    kept: list[str] = []
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
        record_sent_message=kept.append,
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": "T_line"},
        correlation={"turn_id": "T_line"},
    )
    decide(trigger, ctx)
    return llm.live[0], kept


@pytest.mark.parametrize(
    ("transcript", "reply_language", "line"),
    [
        ("What is the plan for Friday's review?", "follow", ENGLISH_LINE),
        ("周五的评审怎么安排？", "follow", CHINESE_LINE),  # noqa: RUF001 — fullwidth question mark.
        ("周五的评审怎么安排？", "en", ENGLISH_LINE),  # noqa: RUF001 — a pinned language beats his words.
        ("What is the plan for Friday's review?", "zh", CHINESE_LINE),
    ],
)
def test_live_message_ends_with_the_reply_language(
    tmp_path: Path, transcript: str, reply_language: str, line: str
) -> None:
    """Zh and en words, and a pinned setting over them, each end the message with one line."""
    live, kept = _live_message(tmp_path, transcript, reply_language)
    assert live.endswith(f"{transcript}\n\n{line}")
    assert live.count("Reply language for this turn") == 1
    assert kept == [live]  # later turns replay the message as it was sent


@pytest.mark.parametrize(
    "transcript",
    ["明日の会議は何時ですか", "내일 회의는 몇 시예요", "ありがとう thanks"],
)
def test_kana_and_hangul_words_get_no_line(tmp_path: Path, transcript: str) -> None:
    """The follow rule knows zh and en only: Japanese and Korean words get no line."""
    live, _ = _live_message(tmp_path, transcript, "follow")
    assert "Reply language" not in live
    assert live.endswith(transcript)


def test_no_words_get_no_line_under_follow(tmp_path: Path) -> None:
    """No words and nothing pinned: nothing to name."""
    live, _ = _live_message(tmp_path, "   ", "follow")
    assert "Reply language" not in live
