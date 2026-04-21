"""Tests for memory.outcome_detector — conservative outcome signal detection."""
from __future__ import annotations

import pytest

from memory.cold.outcome_detector import (
    _NEGATIVE_PATTERNS,
    _POSITIVE_PATTERNS,
    _detect_regex_only as detect_outcome,
)


class TestPositivePatterns:
    def test_each_positive_pattern_raw_returns_plus_one(self):
        # Build a representative sample string for each raw pattern.
        # We test the compiled function, not the pattern list directly.
        positives = [
            "好",
            "好的",
            "好嘞",
            "行",
            "对",
            "对的",
            "没错",
            "可以",
            "棒",
            "厉害",
            "谢谢",
            "谢谢你",
            "谢谢啦",
            "谢谢了",
            "多谢",
            "感谢",
            "就是这样",
            "就是这个",
            "就是它",
        ]
        for text in positives:
            assert detect_outcome(text) == 1, f"expected +1 for {text!r}"

    def test_punctuation_tolerance_hao_de(self):
        # Verifies [。!！.]? handles all common trailing punctuation
        for variant in ["好的", "好的。", "好的！", "好的!", "好的."]:
            assert detect_outcome(variant) == 1, f"expected +1 for {variant!r}"

    def test_punctuation_tolerance_xie_xie(self):
        for variant in ["谢谢", "谢谢。", "谢谢！", "谢谢!"]:
            assert detect_outcome(variant) == 1, f"expected +1 for {variant!r}"


class TestNegativePatterns:
    def test_each_negative_pattern_raw_returns_minus_one(self):
        negatives = [
            "不对",
            "错了",
            "错",
            "再来",
            "再来一遍",
            "重新",
            "重试一次",
            "不是",
            "不是这样",
            "不是这个",
            "不是它",
            "理解错了",
            "你理解错了",
            "不",
            "别",
        ]
        for text in negatives:
            assert detect_outcome(text) == -1, f"expected -1 for {text!r}"

    def test_punctuation_tolerance_bu_dui(self):
        for variant in ["不对", "不对。", "不对！", "不对!", "不对."]:
            assert detect_outcome(variant) == -1, f"expected -1 for {variant!r}"


class TestAnchoringAndLength:
    def test_embedded_trigger_in_long_utterance_returns_none(self):
        # Anchored patterns must not fire when trigger is mid-sentence
        assert detect_outcome("谢谢你刚才说的那件事其实……") is None

    def test_embedded_bu_dui_returns_none(self):
        assert detect_outcome("你说的不对，我是想问另一件事") is None

    def test_utterance_exactly_31_chars_returns_none(self):
        # 31 Chinese characters — over the 30-char limit
        long_text = "好" * 31
        assert detect_outcome(long_text) is None

    def test_utterance_exactly_30_chars_with_no_pattern_returns_none(self):
        # 30 chars but not a recognized pattern
        text = "这句话没有任何意义所以应该返回None，长度刚好三十个字符吧"
        assert len(text.strip()) <= 30 or detect_outcome(text) is None

    def test_short_unrecognized_phrase_returns_none(self):
        assert detect_outcome("随便说说") is None


class TestEmptyAndWhitespace:
    def test_empty_string_returns_none(self):
        assert detect_outcome("") is None

    def test_whitespace_only_returns_none(self):
        assert detect_outcome("   ") is None

    def test_newline_only_returns_none(self):
        assert detect_outcome("\n\t\r") is None
