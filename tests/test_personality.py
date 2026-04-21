"""Tests for personality system."""

from __future__ import annotations

from unittest.mock import patch
from datetime import datetime

import pytest

from core.personality import (
    build_identity_block,
    build_personality_prompt,
    build_situation_block,
    get_time_slot,
    set_nsfw_mode,
)


class TestGetTimeSlot:
    def test_early_morning(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 6, 0)
            assert get_time_slot() == "early_morning"

    def test_morning(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 9, 0)
            assert get_time_slot() == "morning"

    def test_afternoon(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 14, 0)
            assert get_time_slot() == "afternoon"

    def test_evening(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 18, 0)
            assert get_time_slot() == "evening"

    def test_night(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 21, 0)
            assert get_time_slot() == "night"

    def test_late_night(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 2, 0)
            assert get_time_slot() == "late_night"

    def test_late_night_midnight(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 0, 0)
            assert get_time_slot() == "late_night"

    def test_late_night_23(self):
        with patch("core.personality.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 30, 23, 0)
            assert get_time_slot() == "late_night"


class TestBuildPersonalityPrompt:
    def test_base_personality_always_present(self):
        prompt = build_personality_prompt()
        assert "小月" in prompt
        assert "管家" in prompt

    def test_no_ai_references(self):
        prompt = build_personality_prompt()
        assert "AI" not in prompt
        assert "人工智能" not in prompt
        assert "助手" not in prompt

    def test_includes_user_name(self):
        prompt = build_personality_prompt(user_name="Allen", user_role="owner")
        assert "Allen" in prompt

    def test_guest_user(self):
        prompt = build_personality_prompt(user_name=None, user_role="guest")
        assert "不认识" in prompt
        assert "声纹注册" in prompt

    def test_urgent_situation(self):
        prompt = build_personality_prompt(situation="urgent")
        assert "严肃" in prompt or "紧急" in prompt

    def test_error_situation(self):
        prompt = build_personality_prompt(situation="error")
        assert "故障" in prompt or "诚实" in prompt

    def test_time_context_included(self):
        prompt = build_personality_prompt()
        time_keywords = ["清早", "上午", "下午", "傍晚", "晚上", "这会儿"]
        assert any(k in prompt for k in time_keywords)

    def test_tool_rules_included(self):
        prompt = build_personality_prompt()
        assert "工具" in prompt

    def test_emotion_context_injected(self):
        prompt = build_personality_prompt(user_emotion="SAD")
        assert "不开心" in prompt

    def test_emotion_happy(self):
        prompt = build_personality_prompt(user_emotion="HAPPY")
        assert "高兴" in prompt

    def test_emotion_angry(self):
        prompt = build_personality_prompt(user_emotion="ANGRY")
        assert "气头" in prompt

    def test_no_memory_usage_guide_tail(self):
        """Memory usage guide tail belongs to v1; the split wrapper must not carry it."""
        prompt = build_personality_prompt()
        assert "像朋友一样自然地运用" not in prompt
        assert "别像闹钟一样提醒" not in prompt


class TestBuildIdentityBlock:
    """Block 1 of Assembler — static, cache-friendly."""

    def test_contains_personality(self):
        block = build_identity_block()
        assert "小月" in block
        assert "管家" in block
        assert "<personality>" in block
        assert "</personality>" in block

    def test_contains_output_rules(self):
        block = build_identity_block()
        assert "<output_rules>" in block
        assert "</output_rules>" in block
        assert "工具" in block

    def test_no_situation_content(self):
        """Identity block must be free of dynamic (time/emotion/user-status) content."""
        block = build_identity_block()
        time_markers = ["清早", "上午", "下午", "傍晚", "晚上", "这会儿"]
        assert not any(m in block for m in time_markers)
        assert "<situation>" not in block
        # "Allen 的私人管家" is static identity text; dynamic user-status markers
        # like "现在是X在跟你说话" or guest "不认识" belong to Block 4.
        assert "现在是" not in block
        assert "不认识" not in block

    def test_nsfw_off_contains_refusal_clause(self):
        set_nsfw_mode(False)
        try:
            block = build_identity_block()
            assert "色情" in block or "调教类" in block
        finally:
            set_nsfw_mode(False)

    def test_nsfw_on_contains_murasame(self):
        set_nsfw_mode(True)
        try:
            block = build_identity_block()
            assert "Murasame" in block or "村雨" in block
        finally:
            set_nsfw_mode(False)

    def test_no_memory_context_parameter(self):
        """Identity block must not accept memory_context — it is pure static identity."""
        import inspect
        sig = inspect.signature(build_identity_block)
        assert "memory_context" not in sig.parameters


class TestBuildSituationBlock:
    """Block 4 of Assembler — dynamic per-turn, no cache."""

    def test_wraps_in_situation_tag(self):
        block = build_situation_block()
        assert block.startswith("<situation>")
        assert block.endswith("</situation>")

    def test_contains_time_slot(self):
        block = build_situation_block()
        time_keywords = ["清早", "上午", "下午", "傍晚", "晚上", "这会儿"]
        assert any(k in block for k in time_keywords)

    def test_emotion_injected_when_present(self):
        block = build_situation_block(user_emotion="SAD")
        assert "不开心" in block

    def test_no_emotion_when_empty(self):
        """Empty user_emotion should not introduce emotion guidance."""
        block = build_situation_block(user_emotion="")
        assert "不开心" not in block
        assert "高兴" not in block
        assert "气头" not in block

    def test_user_name_present(self):
        block = build_situation_block(user_name="Allen", user_role="owner")
        assert "Allen" in block

    def test_guest_prompts_voiceprint_registration(self):
        block = build_situation_block(user_name=None, user_role="guest")
        assert "不认识" in block
        assert "声纹注册" in block

    def test_urgent_situation_marker(self):
        block = build_situation_block(situation="urgent")
        assert "严肃" in block or "紧急" in block

    def test_error_situation_marker(self):
        block = build_situation_block(situation="error")
        assert "故障" in block or "诚实" in block

    def test_rapid_situation_marker(self):
        block = build_situation_block(situation="rapid")
        assert "简短" in block or "连续" in block

    def test_no_personality_content(self):
        """Situation block must be free of static identity content."""
        block = build_situation_block(user_name="Allen")
        assert "<personality>" not in block
        assert "<output_rules>" not in block

    def test_no_memory_context_parameter(self):
        import inspect
        sig = inspect.signature(build_situation_block)
        assert "memory_context" not in sig.parameters


class TestBuildPersonalityPromptDeprecated:
    """Legacy wrapper: identity + situation concatenation, emits DeprecationWarning."""

    def test_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning):
            build_personality_prompt(user_name="Allen")

    def test_result_equals_identity_plus_situation(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            wrapper = build_personality_prompt(
                user_name="Allen", user_role="owner",
                situation="normal", user_emotion="HAPPY",
            )
        expected = (
            build_identity_block(user_role="owner")
            + "\n\n"
            + build_situation_block(
                user_name="Allen", user_role="owner",
                user_emotion="HAPPY", situation="normal",
            )
        )
        assert wrapper == expected
