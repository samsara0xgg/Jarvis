"""Tests for LLMClient sentence-divider tweaks (WP4).

Covers abbreviation guard + faster_first_response. Builds an LLMClient
without invoking ``__init__`` so we don't need real API keys.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.llm import LLMClient


def _make_client(
    *,
    abbrev_protect: bool = True,
    faster_first: bool = True,
    is_first: bool = True,
) -> LLMClient:
    """Build an LLMClient without network init, with explicit divider knobs."""
    with patch.object(LLMClient, "__init__", lambda self, cfg, **kw: None):
        c = LLMClient.__new__(LLMClient)
        c._abbrev_protect = abbrev_protect
        c._faster_first_response = faster_first
        c._is_first_sentence = is_first
        return c


def _flush_all(client: LLMClient, text: str, force: bool = True) -> tuple[list[str], str]:
    """Helper: feed the whole text once, capture sentences emitted, return (sentences, leftover)."""
    out: list[str] = []
    leftover = client._flush_sentences(text, on_sentence=out.append, force=force)
    return out, leftover


class TestAbbreviationGuard:
    def test_does_not_split_on_dr(self):
        c = _make_client(faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "Dr. Smith said hello.")
        assert sents == ["Dr. Smith said hello."]

    def test_does_not_split_on_eg(self):
        c = _make_client(faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "Use it, e.g. for testing.")
        assert sents == ["Use it, e.g. for testing."]

    def test_disabled_splits_on_dr(self):
        c = _make_client(abbrev_protect=False, faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "Dr. Smith said hello.")
        # With guard off, every period splits, so we get two sentences.
        assert sents == ["Dr.", "Smith said hello."]

    def test_streaming_partial_dr(self):
        # Buffer ends with "Dr" — no period yet, no split, return whole buffer.
        c = _make_client(faster_first=False, is_first=False)
        out: list[str] = []
        leftover = c._flush_sentences("Dr", on_sentence=out.append, force=False)
        assert out == []
        assert leftover == "Dr"

    def test_streaming_dr_then_period(self):
        # Streaming mode (force=False): buffer ends at "Dr." with nothing after
        # — abbreviation guard should keep us waiting.
        c = _make_client(faster_first=False, is_first=False)
        out: list[str] = []
        leftover = c._flush_sentences("Dr.", on_sentence=out.append, force=False)
        assert out == []
        assert leftover == "Dr."


class TestDecimalGuard:
    def test_does_not_split_on_decimal(self):
        c = _make_client(faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "3.14 is pi.")
        assert sents == ["3.14 is pi."]


class TestFasterFirstResponse:
    def test_first_sentence_splits_on_chinese_comma(self):
        c = _make_client(faster_first=True, is_first=True)
        sents, _ = _flush_all(c, "你好，世界。")
        assert sents == ["你好，", "世界。"]

    def test_first_sentence_splits_on_ascii_comma(self):
        c = _make_client(faster_first=True, is_first=True)
        sents, _ = _flush_all(c, "Hello, world.")
        assert sents == ["Hello,", "world."]

    def test_only_first_sentence_uses_comma(self):
        # After splitting once, _is_first_sentence flips False; later commas
        # inside subsequent sentences should NOT trigger another split.
        c = _make_client(faster_first=True, is_first=True)
        sents, _ = _flush_all(c, "好，懂了。然后，我们继续。")
        assert sents == ["好，", "懂了。", "然后，我们继续。"]

    def test_disabled_does_not_split_on_comma(self):
        c = _make_client(faster_first=False, is_first=True)
        sents, _ = _flush_all(c, "你好，世界。")
        assert sents == ["你好，世界。"]

    def test_non_first_sentence_does_not_split_on_comma(self):
        c = _make_client(faster_first=True, is_first=False)
        sents, _ = _flush_all(c, "你好，世界。")
        assert sents == ["你好，世界。"]


class TestCompositeBehavior:
    def test_chinese_period_splits_normally(self):
        c = _make_client(faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "你好。世界。")
        assert sents == ["你好。", "世界。"]

    def test_abbrev_then_chinese_period(self):
        c = _make_client(faster_first=False, is_first=False)
        sents, _ = _flush_all(c, "Dr. 王说你好。")
        assert sents == ["Dr. 王说你好。"]

    def test_decimal_inside_first_sentence_with_comma(self):
        c = _make_client(faster_first=True, is_first=True)
        sents, _ = _flush_all(c, "圆周率是 3.14，你知道吗？")
        assert sents == ["圆周率是 3.14，", "你知道吗？"]
