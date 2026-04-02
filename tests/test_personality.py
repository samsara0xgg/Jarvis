"""Tests for personality system."""

from __future__ import annotations

from unittest.mock import patch
from datetime import datetime

import pytest

from core.personality import build_personality_prompt, get_time_slot


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
        assert "小贾" in prompt
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

    def test_preferences_included(self):
        prefs = {"灯光偏好": "暖白光", "空调温度": "25度"}
        prompt = build_personality_prompt(preferences=prefs)
        assert "暖白光" in prompt
        assert "25度" in prompt

    def test_no_preferences(self):
        prompt = build_personality_prompt(preferences=None)
        assert "暖白光" not in prompt  # specific prefs should not appear

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
