"""ADR 0256: `<react>EMOJI</react>` in her reply is cut out and becomes her reaction.

The filter is checked on its own, whole and split at every position; then through the real
turn driver and a localhost peer shaped like /v1/responses, on a typed turn and a spoken one,
for what reaches speech, the chunks and the plan.
"""

# ruff: noqa: RUF001 - the replies under test are Chinese, with fullwidth punctuation
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import decide
from jarvis.decision.reaction_markup import ReactionFilter, split_reaction
from jarvis.shared import lang
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - the autouse fixture the peer needs
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_turn_guards import _context, _ScriptedModel
from tests.integration.test_wire_routine_streaming import _drive, _intent, _payloads

if TYPE_CHECKING:
    from pathlib import Path

THUMB = "\U0001f44d"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"

CASES = (
    # (reply, text left, emoji)
    (f"<react>{THUMB}</react>", "", THUMB),
    (f"好的<react>{THUMB}</react>，晚安。", "好的，晚安。", THUMB),
    (f"<react>{THUMB}</react>\n\n好的。", "\n\n好的。", THUMB),
    (f"<voice>好的。<react>{THUMB}</react></voice>", "<voice>好的。</voice>", THUMB),
    (f"<REACT> {THUMB} </React>ok", "ok", THUMB),
    (f"<react>{THUMB}</react>a<react>\U0001f602</react>", "a", THUMB),
    ("<react>nope</react><react>" + THUMB + "</react>", "", THUMB),
    (f"<react>{FAMILY}</react>", "", FAMILY),
    ("<react>words, not an emoji</react>hello", "hello", None),
    (f"{THUMB}<react></react>", THUMB, None),
    (f"a < b and <re c <react>{THUMB}</react>", "a < b and <re c ", THUMB),
    (f"<react>{THUMB}", "", THUMB),
    ("x <react>" + "y" * 80, "x <react>" + "y" * 80, None),
    ("no markup <rea", "no markup <rea", None),
)


@pytest.mark.parametrize(("reply", "left", "emoji"), CASES)
def test_the_tag_is_cut_out_whole_and_at_every_split(
    reply: str, left: str, emoji: str | None,
) -> None:
    """The same text and emoji come out however the reply is cut into pieces."""
    assert split_reaction(reply) == (left, emoji)
    for cut in range(len(reply) + 1):
        piecewise = ReactionFilter()
        shown = piecewise.feed(reply[:cut]) + piecewise.feed(reply[cut:]) + piecewise.finish()
        assert (shown, piecewise.emoji) == (left, emoji), cut
    one_by_one = ReactionFilter()
    shown = "".join(one_by_one.feed(char) for char in reply) + one_by_one.finish()
    assert (shown, one_by_one.emoji) == (left, emoji)


def test_a_tag_is_held_back_and_never_leaks_into_a_piece() -> None:
    """Mid-stream, no piece returned holds any part of a tag that turned out to be one."""
    stream = ReactionFilter()
    pieces = [stream.feed(part) for part in ("好的<rea", "ct>\U0001f44d</re", "act>嗯")]
    assert pieces == ["好的", "", "嗯"]


# --- through the turn driver -------------------------------------------------------------------


def test_a_spoken_reply_loses_the_tag_before_a_word_is_spoken(tmp_path: Path) -> None:
    """Chunks, the plan and the spoken text hold no tag, though the peer cuts it in pieces."""
    reply = f"好的<react>{THUMB}</react>，晚安。再见。"
    with _Peer([[("final_answer", reply)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-spoken", "记一下"))
    chunks = [payload["text"] for payload in _payloads(runtime.conn, "surface.response_chunk")]
    assert "".join(chunks) == result.response_plan.text == "好的，晚安。再见。"
    assert "react" not in "".join(chunks) + result.response_plan.text
    (emitted,) = _payloads(runtime.conn, "surface.response_emitted")
    assert emitted["voice_text"] == "好的，晚安。再见。"


def test_a_typed_reply_that_is_only_the_tag_is_an_empty_answer_that_passes_the_gates(
    tmp_path: Path,
) -> None:
    """Nothing to stream, no failure line, no second request: the turn simply has no words."""
    with _Peer([[("final_answer", f"<react>{THUMB}</react>")]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-typed", "我到家了"))
    assert result.response_plan.text == ""
    assert len(peer.requests) == 1
    assert _payloads(runtime.conn, "surface.response_chunk") == []
    assert _payloads(runtime.conn, "turn.failed") == []
    (ended,) = _payloads(runtime.conn, "turn.ended")
    assert ended["turn_id"] == "turn-typed"


def _budget_runtime(tmp_path: Path, url: str) -> Any:  # noqa: ANN401 - JarvisRuntime
    """A spoken-route runtime that may call one tool, so its second request is the budget's."""
    runtime = _spoken_runtime(tmp_path, url)
    return replace(runtime, config={**runtime.config, "llm": {"max_tool_iterations": 1}})


@pytest.mark.parametrize("channel", ["cli_stdin", "inherent_ptt"])
def test_the_wrap_up_after_the_tool_budget_that_is_only_a_tag(tmp_path: Path, channel: str) -> None:
    """Typed: it is the answer. Spoken: the limit line it always got for an empty wrap-up."""
    outputs = [[("call", "kept")], [("final_answer", f"<react>{THUMB}</react>")]]
    with _Peer(outputs) as peer:
        runtime = _budget_runtime(tmp_path, peer.url)
        intent = emit_event(
            runtime.conn,
            type="surface.user_intent",
            payload={"transcript": "我记过什么", "turn_id": "T-budget", "channel": channel},
            correlation={"turn_id": "T-budget"},
        )
        result = _drive(runtime, intent)
    assert len(peer.requests) == 2
    limit = lang.t("tool_budget.exhausted", lang="zh")
    assert result.response_plan.text == ("" if channel == "cli_stdin" else limit)


def _decide_batch(
    tmp_path: Path, model: _ScriptedModel, channel: str, budget: int = 8,
) -> Any:  # noqa: ANN401 - DecideResult
    conn = open_event_log(tmp_path / "events.db")
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "我到家了", "turn_id": "T-batch", "channel": channel},
            correlation={"turn_id": "T-batch"},
        )
        return decide(trigger, _context(conn, tmp_path, model, budget))
    finally:
        conn.close()


def test_the_batch_loop_cuts_the_tag_and_hands_the_reaction_on(tmp_path: Path) -> None:
    """The plan holds the words alone, and the result carries the emoji."""
    model = _ScriptedModel(f"到家就好。<react>{THUMB}</react>")
    result = _decide_batch(tmp_path, model, "cli_stdin")
    assert (result.response_plan.text, result.reaction) == ("到家就好。", THUMB)


def test_the_batch_loop_answers_a_typed_message_with_a_tag_alone(tmp_path: Path) -> None:
    """One request, an empty answer, no limit line and no second try."""
    model = _ScriptedModel(f"<react>{THUMB}</react>")
    result = _decide_batch(tmp_path, model, "cli_stdin")
    assert (result.response_plan.text, result.reaction) == ("", THUMB)
    assert len(model.requests) == 1


@pytest.mark.parametrize("channel", ["cli_stdin", "inherent_ptt"])
def test_the_batch_loops_wrap_up_that_is_only_a_tag(tmp_path: Path, channel: str) -> None:
    """Typed: the answer. Spoken: the limit line, as for any empty wrap-up."""
    model = _ScriptedModel(("kept", {}), f"<react>{THUMB}</react>")
    result = _decide_batch(tmp_path, model, channel, budget=1)
    limit = lang.t("tool_budget.exhausted", lang="zh")
    assert result.response_plan.text == ("" if channel == "cli_stdin" else limit)
    assert result.reaction == THUMB


def test_a_reply_without_a_tag_has_no_reaction(tmp_path: Path) -> None:
    """The field stays empty, and the plan is the reply untouched."""
    result = _decide_batch(tmp_path, _ScriptedModel("到家就好。"), "cli_stdin")
    assert (result.response_plan.text, result.reaction) == ("到家就好。", None)
