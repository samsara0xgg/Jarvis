"""ADR-0008 D2/D12 pre-route data: tool cues route to tools, envelopes stay tag-free."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.decision.pre_route import load_tool_cues, match_tool_cue
from jarvis.decision.stream_envelope import StreamEnvelopeSplitter

_CUES = load_tool_cues(Path(__file__).resolve().parents[2] / "config" / "tool_cues.yaml")


_CASES: list[tuple[str, str | None]] = [
    ("帮我打开这个文件", "zh_imperative_opener"),
    ("把刚才的邮件发给他", "zh_imperative_opener"),
    ("删掉桌面上的截图", "zh_action_verb"),
    ("查一下今天的日程", "zh_action_verb"),
    ("这个仓库现在什么状态", "zh_tool_noun"),
    ("please open the terminal", "en_imperative_opener"),
    ("Run the tests again", "en_action_verb"),
    ("what is in my clipboard", "en_tool_noun"),
    ("冰为什么会融化？", None),  # noqa: RUF001 - CJK question mark is real input
    ("今天有哪些待办", "zh_tool_noun"),
    ("今天的早报呢", "zh_tool_noun"),
    ("本地知识里有什么", "zh_tool_noun"),
    ("my briefing for today", "en_tool_noun"),
    ("你好", None),
    ("为什么天空是蓝色的", None),
    ("why is the sky blue", None),
    ("给我讲个笑话", "zh_imperative_opener"),
]


@pytest.mark.parametrize(("utterance", "expected"), _CASES)
def test_tool_cue_table_is_broad_and_conservative(utterance: str, expected: str | None) -> None:
    """Every shipped cue id is reachable and casual questions match nothing."""
    hit = match_tool_cue(utterance, _CUES)
    assert (hit.id if hit is not None else None) == expected


def test_every_shipped_cue_is_hit_by_the_table_test() -> None:
    """A cue no case above can trigger is dead configuration."""
    assert {cue.id for cue in _CUES} == {expected for _, expected in _CASES if expected}


def _drive(deltas: tuple[str, ...]) -> tuple[list[str], str, str, bool]:
    splitter = StreamEnvelopeSplitter()
    forwarded = [splitter.feed(delta) for delta in deltas]
    tail = splitter.finish()
    return [text for text in forwarded if text], tail.voice_tail, tail.document, tail.enveloped


@pytest.mark.parametrize(
    ("deltas", "voice", "document", "enveloped"),
    [
        (
            (
                "<voi",
                "ce>\n冰从周围吸收热量。",
                "这些热量来自空气。\n</vo",
                "ice>\n<docu",
                "ment>\n0°C 以上融化。\n</document>",
            ),
            "冰从周围吸收热量。这些热量来自空气。",
            "0°C 以上融化。",
            True,
        ),
        (
            ("冰从周围吸收热量。", "这些热量来自空气"),
            "冰从周围吸收热量。这些热量来自空气",
            "",
            False,
        ),
        (
            (
                "  \n",
                "<voice>你好。</voice>",
            ),
            "你好。",
            "",
            True,
        ),
        (("<3 度以下会结冰。",), "<3 度以下会结冰。", "", False),
        (("<voice>会变成水</vo",), "会变成水</vo", "", True),
        (("<voice>你好。</voice> 忽略 <document>doc</document> 尾巴",), "你好。", "doc", True),
    ],
)
def test_envelope_splitter_forwards_only_tag_free_voice(
    deltas: tuple[str, ...], voice: str, document: str, *, enveloped: bool
) -> None:
    """Held prefixes never leak a tag; whitespace around bodies is not spoken."""
    forwarded, voice_tail, got_document, got_enveloped = _drive(deltas)
    assert all("<" not in text or not got_enveloped for text in forwarded)
    assert "".join(forwarded) + voice_tail == voice
    assert (got_document, got_enveloped) == (document, enveloped)
