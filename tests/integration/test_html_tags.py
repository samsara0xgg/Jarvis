"""Stray HTML in her answer: ``<br>`` is a newline, other tags are dropped, text and code stay.

The helper is checked on its own, whole and split at every position; then through the real turn
driver and a localhost peer shaped like /v1/responses (what reaches speech, the chunks and the
plan), the batch loop's full-text path, and an older record's replay.
"""

# ruff: noqa: RUF001 - the replies under test are Chinese, with fullwidth punctuation
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.shared.html_tags import HtmlTagFilter, strip_html
from jarvis.state.memory_db import _plain
from tests.support.reaction_markup import decide_batch
from tests.support.routine import drive_streaming_turn, logged_payloads
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - the autouse fixture the peer needs
    spoken_intent,
    spoken_runtime,
)
from tests.support.turn_guards import StepModel

if TYPE_CHECKING:
    from pathlib import Path

CASES = (
    # (text, text left)
    ("第一行<br>第二行", "第一行\n第二行"),
    ("a<br/>b<BR />c<br class='x'>d", "a\nb\nc\nd"),
    ("a <br> b", "a \n b"),
    ("<p>你好</p>", "你好"),
    ("<b>x</b> and <i>y</i> and <span style='c'>z</span>", "x and y and z"),
    ("a < b and 3 <4 and <3 and x<y", "a < b and 3 <4 and <3 and x<y"),
    ("<voice>好的。</voice><document>d</document>", "<voice>好的。</voice><document>d</document>"),
    ("List<String> and <unknown>", "List<String> and <unknown>"),
    ("`<br>` stays and ```\n<p>x</p>\n``` too", "`<br>` stays and ```\n<p>x</p>\n``` too"),
    ("tail <br", "tail <br"),
)


@pytest.mark.parametrize(("text", "left"), CASES)
def test_the_tags_are_dealt_with_whole_and_at_every_split(text: str, left: str) -> None:
    """The same text comes out however the reply is cut into pieces (code excepted when cut)."""
    assert strip_html(text) == left
    if "`" in text:
        return
    for cut in range(len(text) + 1):
        piecewise = HtmlTagFilter()
        shown = piecewise.feed(text[:cut]) + piecewise.feed(text[cut:]) + piecewise.finish()
        assert shown == left, cut


def test_a_cut_tag_is_held_back_and_never_leaks_into_a_piece() -> None:
    """Mid-stream, no piece returned holds any part of a tag that turned out to be one."""
    stream = HtmlTagFilter()
    pieces = [stream.feed(part) for part in ("好的<b", "r>嗯", "<", "/b>。")]
    assert pieces == ["好的", "\n嗯", "", "。"]


def test_an_older_record_replays_without_the_tags() -> None:
    """History shows the words: the tag already in a record never reaches the prompt."""
    assert _plain("<voice>好的<br>晚安</voice>") == "好的\n晚安"
    assert _plain("a < b") == "a < b"


def test_a_spoken_reply_with_br_is_spoken_and_kept_with_a_newline(tmp_path: Path) -> None:
    """Chunks, the plan and the spoken text hold a newline and no tag, though the peer cuts it."""
    reply = "好的。<br>早点休息，晚安。"
    with SpokenPeer([[("final_answer", reply)]]) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-spoken", "记一下"))
    chunks = "".join(p["text"] for p in logged_payloads(runtime.conn, "surface.response_chunk"))
    assert chunks == result.response_plan.text == "好的。\n早点休息，晚安。"
    (emitted,) = logged_payloads(runtime.conn, "surface.response_emitted")
    assert emitted["voice_text"] == "好的。\n早点休息，晚安。"
    assert "<" not in chunks


def test_a_written_part_with_tags_is_cleaned_and_its_code_kept(tmp_path: Path) -> None:
    """The document of an enveloped reply loses <br> and <b>, not the html inside code ticks."""
    reply = "<voice>好的。</voice><document>第一行<br>第二行 <b>y</b> `<p>hi</p>`</document>"
    with SpokenPeer([[("final_answer", reply)]]) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-doc", "写个页面"))
    shown = "<document>\n第一行\n第二行 y `<p>hi</p>`\n</document>"
    assert result.response_plan.text.endswith(shown)


def test_the_batch_loop_full_text_has_the_newline_and_no_tag(tmp_path: Path) -> None:
    """The non-streamed path finalises the same way."""
    result = decide_batch(tmp_path, StepModel("到家就好。<br>早点休息。 a < b"), "cli_stdin")
    assert result.response_plan.text == "到家就好。\n早点休息。 a < b"
