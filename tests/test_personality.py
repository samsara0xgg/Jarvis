"""Tests for personality system."""

from __future__ import annotations

from unittest.mock import patch
from datetime import datetime

import pytest

from core.personality import (
    build_identity_block,
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


class TestBuildIdentityBlock:
    """Block 1 of Assembler — static, cache-friendly."""

    def test_contains_operating_kernel(self):
        block = build_identity_block()
        assert "小月" in block
        assert "personal operating layer" in block
        assert "<xiaoyue_kernel>" in block
        assert "</xiaoyue_kernel>" in block

    def test_contains_output_contract(self):
        block = build_identity_block()
        assert "<output_contract>" in block
        assert "<voice>" in block
        assert "<document>" in block
        assert "tool result explicitly confirms" in block

    def test_general_style_without_phrase_patch_list(self):
        block = build_identity_block()
        assert "persona performance" in block
        assert "fixed openings" in block
        assert "别吊着" not in block
        assert "少来" not in block
        assert "在，怎么了" not in block

    def test_no_situation_content(self):
        """Identity block must be free of dynamic (time/emotion/user-status) content."""
        block = build_identity_block()
        time_markers = ["清早", "上午", "下午", "傍晚", "晚上", "这会儿"]
        assert not any(m in block for m in time_markers)
        assert "<situation>" not in block
        # Static identity can mention Allen; dynamic user-status markers
        # like "现在是X在跟你说话" or guest "不认识" belong to Block 4.
        assert "现在是" not in block
        assert "不认识" not in block

    def test_nsfw_off_contains_refusal_clause(self):
        set_nsfw_mode(False)
        try:
            block = build_identity_block()
            assert "sexual roleplay" in block or "erotic content" in block
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

    def test_emotion_happy(self):
        block = build_situation_block(user_emotion="HAPPY")
        assert "高兴" in block

    def test_emotion_angry(self):
        block = build_situation_block(user_emotion="ANGRY")
        assert "气头" in block

    def test_no_emotion_when_empty(self):
        """Empty user_emotion should not introduce emotion guidance."""
        block = build_situation_block(user_emotion="")
        assert "不开心" not in block
        assert "高兴" not in block
        assert "气头" not in block

    def test_user_name_present(self):
        block = build_situation_block(user_name="Allen", user_role="owner")
        assert "Allen" in block

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
        assert "<xiaoyue_kernel>" not in block
        assert "<output_contract>" not in block

    def test_no_memory_context_parameter(self):
        import inspect
        sig = inspect.signature(build_situation_block)
        assert "memory_context" not in sig.parameters
