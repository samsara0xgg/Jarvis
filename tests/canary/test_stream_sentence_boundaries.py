"""Incremental D5 input-to-candidate tables across adversarial token boundaries."""

# ruff: noqa: RUF001 — bilingual sentence/quotation fixtures need exact punctuation.
from __future__ import annotations

import re

import pytest

from jarvis.decision.stream_sentences import SemanticAssembler, SemanticCandidate


def _assemble(
    text: str, width: int, *, first_clause_chars: int = 0,
) -> tuple[list[SemanticCandidate], SemanticAssembler]:
    assembler = SemanticAssembler(first_clause_chars=first_clause_chars)
    candidates: list[SemanticCandidate] = []
    for start in range(0, len(text), width):
        candidates.extend(assembler.feed(text[start : start + width]))
        if assembler.blocked_reason is not None:
            break
    candidates.extend(assembler.finish())
    return candidates, assembler


@pytest.mark.parametrize(
    "text",
    [
        "冰吸收热量后，水分子运动加快。固态的结构逐渐松开，冰就变成了水。",
        "Hello! How are you? Fine.",
        "Dr. Green measured 3.14 units. Prof. Brown agreed.",
        "For example, e.g. apples, fruit contains water. Try pears.",
        "The U.S. has states. The U.K. has counties.",
        "He holds a Ph.D. in physics. Nice.",
        "Her M.Sc. is in physics. His B.Sc. is in chemistry.",
        "A D.Phil. is a research degree. See Fig. 2 for details.",
        'He said, "Hello." Then she replied, "Hi!"',
        "他说：“冰正在融化。”随后停了下来。",
        "The number (3.14, approximately) is familiar. That is pi.",
        "第一行没有句号\n第二行有句号。",
        "\n\n  Ice absorbs heat.  It melts. \n",
        "A short final fragment",
        "Water absorbs energy from the warmer air, and the molecules gain motion, "
        "so the solid structure loosens, and the ice turns into liquid.",
        "A transformer reads text as tokens, then uses attention to decide which other "
        "tokens matter for understanding each one. It processes those relationships "
        "through layers and predicts the next token, repeating this to generate an answer.",
        "The long rolling hills near the coast stretch out toward the horizon under a pale "
        "morning sky while the quiet tide slowly drags the cold grey water over the sand.",
    ],
)
def test_plain_prose_keeps_exact_prefix_across_chunk_boundaries(text: str) -> None:
    """Wire chunking cannot split decimals/abbreviations or rewrite accepted text."""
    reference: list[str] | None = None
    for width in (1, 2, 5, len(text)):
        candidates, assembler = _assemble(text, width)
        assert assembler.blocked_reason is None, (text, width, assembler.blocked_reason)
        assert "".join(candidate.text for candidate in candidates) + assembler.pending_text == text
        parts = [candidate.text for candidate in candidates]
        for part in candidates:
            # 60 when a clause or sentence end lies within it, else up to 240.
            bound = 60 if re.search(r"[,;，；.!?。！？]", part.text[:60]) else 240
            assert 0 < len(part.text) <= bound, (text, width, part)
        if reference is None:
            reference = parts
        assert parts == reference, (text, width, parts, reference)


@pytest.mark.parametrize(
    "tail",
    [
        "https://example.com/a?b=1. Next.",
        "www.example.org/path. Next.",
        "example.com has information.",
        "example.xyz has information.",
        "example.中国 has information.",
        "Contact a.b@example.org. Next.",
        "Contact a!b@example.org. Next.",
        "Contact a?b@example.org. Next.",
        "Contact a!?b@example.org. Next.",
        "```python\nprint('hello.')\n```",
        "Use `print(1.5)` here.",
        "**Bold text.** Next.",
        "_Emphasis is incomplete.",
        "[A link](https://example.org).",
        "<voice>旧控制协议。</voice>",
        '{"tool": "send", "arguments": {}}',
        "# Heading\nMore.",
        "#Incomplete-heading",
        "~~Strikeout is incomplete.",
        "1. First point. Next.",
        "- A list item. Next.",
        'He said, "An unfinished quotation.',
        "A" * 2100,
        "The count is " + ",".join(["123"] * 70) + ".",
    ],
)
def test_unsupported_or_unbounded_tail_never_becomes_a_candidate(tail: str) -> None:
    """Earlier prose may stay committed; formatting and arbitrary cuts never escape."""
    prefix = "Ice absorbs heat. "
    for width in (1, 7, len(prefix + tail)):
        candidates, assembler = _assemble(prefix + tail, width)
        assert [candidate.text for candidate in candidates] == ["Ice absorbs heat."]
        assert assembler.blocked_reason is not None
        assert len(assembler.pending_text) <= 2048


def test_coalescing_cannot_reorder_formatting_and_forced_subclause() -> None:
    """A later domain blocks this clause before its size can force a split."""
    text = "Hi. Hello, " + "x" * 39 + " example.com more words."
    for width in (1, 2, 7, len(text)):
        candidates, assembler = _assemble(text, width)
        assert [candidate.text for candidate in candidates] == ["Hi."]
        assert assembler.blocked_reason == "unsupported_speech_syntax"


def test_decimal_waits_for_lookahead_and_finalization_cannot_replay() -> None:
    """A token ending in 3. is not a finished sentence while generation is open."""
    assembler = SemanticAssembler()
    assert assembler.feed("The value is 3.") == ()
    assert assembler.feed("14. Another sentence") == (
        SemanticCandidate("The value is 3.14.", "sentence"),
    )
    assert assembler.finish() == (SemanticCandidate(" Another sentence", "final"),)
    with pytest.raises(RuntimeError, match="already finished"):
        assembler.finish()
    with pytest.raises(RuntimeError, match="closed or blocked"):
        assembler.feed("Must not replay.")


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("今天北京是晴天，气温二十度左右。明天也差不多。",
         ["今天北京是晴天，", "气温二十度左右。", "明天也差不多。"]),
        ("好，我记下了。", ["好，我记下了。"]),  # a clause under 6 waits for the sentence
        ("我觉得，嗯，这个可以。", ["我觉得，嗯，", "这个可以。"]),
        ("价格是1,000元，不贵。", ["价格是1,000元，", "不贵。"]),  # 1,000 is no clause end
        ("他说（第一，第二）很好，对吧。", ["他说（第一，第二）很好，", "对吧。"]),
        ("Sure, I can do that. And then, more.", ["Sure, I can do that.", " And then, more."]),
        ("Well then, let us go, now.", ["Well then,", " let us go, now."]),
        ("好的，明白", ["好的，明白"]),
        ("一二三四五，", ["一二三四五，"]),
    ],
)
def test_the_first_clause_can_end_the_first_candidate(text: str, parts: list[str]) -> None:
    """first_clause_chars=6: only the first candidate may end at a clause end."""
    for width in (1, 2, 5, len(text)):
        candidates, assembler = _assemble(text, width, first_clause_chars=6)
        assert assembler.blocked_reason is None, (text, width, assembler.blocked_reason)
        assert [candidate.text for candidate in candidates] == parts, (text, width)


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        (
            "A transformer reads text as tokens, then uses attention to decide which other "
            "tokens matter for understanding each one. It processes those relationships "
            "through layers and predicts the next token, repeating this to generate an answer.",
            [
                "A transformer reads text as tokens,",
                " then uses attention to decide which other tokens matter for understanding "
                "each one.",
                " It processes those relationships through layers and predicts the next token,",
                " repeating this to generate an answer.",
            ],
        ),
        (
            "The long rolling hills near the coast stretch out toward the horizon under a pale "
            "morning sky while the quiet tide slowly drags the cold grey water over the sand.",
            [
                "The long rolling hills near the coast stretch out toward the horizon under a "
                "pale morning sky while the quiet tide slowly drags the cold grey water over "
                "the sand.",
            ],
        ),
    ],
)
def test_a_long_clause_streams_at_its_first_boundary_within_240(
    text: str, parts: list[str],
) -> None:
    """No boundary inside 60 code points: the first one up to 240 ends the candidate."""
    for width in (1, 2, 5, len(text)):
        candidates, assembler = _assemble(text, width)
        assert assembler.blocked_reason is None, (text, width, assembler.blocked_reason)
        assert [candidate.text for candidate in candidates] == parts, (text, width)
