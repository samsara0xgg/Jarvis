"""ADR 0135: the live message and the request after tool results end with the language line.

Drives the real ``decide()`` on a scripted model whose requests record the messages it was sent.
Real case (2026-10-02): with ``reply_language: follow`` the model answered in the other
language on 7% of turns; one line after his words fixed 10 of 28 such samples to 0.
After English tool results the same line at the end of the request kept a Chinese ask
answered in Chinese (offline replay: 5 of 5 English without it, 0 of 5 with it).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import DEFAULT_MAX_TOOL_ITERATIONS, decide
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import ScriptedModel, tool_call
from tests.support.turn_guards import guard_context

if TYPE_CHECKING:
    from pathlib import Path

ENGLISH_LINE = "[Reply language for this turn: English]"
CHINESE_LINE = "[Reply language for this turn: Chinese]"


def _live(model: ScriptedModel) -> list[str]:
    """The last message of each request the model was sent."""
    return [str(request.messages[-1]["content"]) for request in model.requests]


def _run(
    tmp_path: Path, transcript: str, reply_language: str, *, call_a_tool: bool = False
) -> tuple[ScriptedModel, list[str]]:
    """Drive one turn; the scripted model, and what ``record_sent_message`` kept."""
    llm = ScriptedModel(*([tool_call("kept")] if call_a_tool else []), otherwise="Okay.")
    kept: list[str] = []
    conn = open_event_log(tmp_path / "events.db")
    ctx = replace(
        guard_context(conn, tmp_path, llm, DEFAULT_MAX_TOOL_ITERATIONS),
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
    return llm, kept


def _live_message(tmp_path: Path, transcript: str, reply_language: str) -> tuple[str, list[str]]:
    """The first request's last message, and what ``record_sent_message`` kept."""
    llm, kept = _run(tmp_path, transcript, reply_language)
    return _live(llm)[0], kept


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


@pytest.mark.parametrize(
    ("transcript", "line"),
    [
        ("What is on my calendar tomorrow?", ENGLISH_LINE),
        ("帮我上网搜一下明天温哥华会不会下雨", CHINESE_LINE),
    ],
)
def test_request_after_tool_results_ends_with_the_line(
    tmp_path: Path, transcript: str, line: str
) -> None:
    """English tool results sit between his words and the answer: the line goes last.

    Under a note naming his words, so the item never reads as a new, empty turn
    (2026-10-07: alone, it sent the model back to an older request in the history).
    """
    llm, kept = _run(tmp_path, transcript, "follow", call_a_tool=True)
    # the second request; a third is the answer check
    assert _live(llm)[1] == f'[Not new words from the user: still answering "{transcript}"]\n{line}'

    assert kept == [_live(llm)[0]]  # the stored message is still the live one, line once


@pytest.mark.parametrize("transcript", ["明日の会議は何時ですか", "   "])
def test_request_after_tool_results_has_no_line_when_there_is_none(
    tmp_path: Path, transcript: str
) -> None:
    """Kana words and no words name no language, after tool results as before them."""
    llm, _ = _run(tmp_path, transcript, "follow", call_a_tool=True)
    assert "Reply language" not in _live(llm)[1]
