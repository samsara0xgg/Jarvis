"""Tests for memory.observer — LLM-based observation extraction."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from memory.observer import (
    OBSERVER_SYSTEM_PROMPT,
    OBSERVER_TOOL_SCHEMA,
    Observer,
)


def _make_config(**overrides) -> dict:
    cfg = {
        "llm": {"api_key": "test-key", "base_url": "https://api.x.ai/v1"},
        "memory": {
            "observer": {
                "enabled": True,
                "primary_model": "grok-4.20-0309-non-reasoning",
                "fallback_model": "gemini-2.5-flash",
            },
        },
    }
    cfg.update(overrides)
    return cfg


class TestPromptAndSchema:
    """Validate OBSERVER_SYSTEM_PROMPT and OBSERVER_TOOL_SCHEMA constants."""

    def test_prompt_contains_required_sections(self):
        required = [
            "YOUR JOB",
            "PRIORITY EMOJI",
            "DISTINGUISH",
            "STATE CHANGES",
            "PRESERVE",
            "PRECISE VERBS",
            "EMOTION",
            "record_observations",
        ]
        for section in required:
            assert section in OBSERVER_SYSTEM_PROMPT, f"Missing: {section}"

    def test_prompt_has_priority_emojis(self):
        for emoji in ("🔴", "🟡", "🟢", "✅"):
            assert emoji in OBSERVER_SYSTEM_PROMPT

    def test_tool_schema_structure(self):
        assert OBSERVER_TOOL_SCHEMA["name"] == "record_observations"
        props = OBSERVER_TOOL_SCHEMA["parameters"]["properties"]["observations"]
        item_props = props["items"]["properties"]
        assert "priority" in item_props
        assert "time" in item_props
        assert "text" in item_props
        assert item_props["priority"]["enum"] == ["🔴", "🟡", "🟢", "✅"]
        assert props["items"]["required"] == ["priority", "time", "text"]


class TestBuildPrompt:
    """Test Observer._build_prompt message construction."""

    def test_basic_structure(self):
        obs = Observer(_make_config())
        msgs = obs._build_prompt({
            "user_text": "你好",
            "assistant_text": "你好呀！",
        })
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == OBSERVER_SYSTEM_PROMPT
        assert msgs[1]["role"] == "user"
        assert "用户：你好" in msgs[1]["content"]
        assert "助手：你好呀！" in msgs[1]["content"]

    def test_includes_tool_calls(self):
        obs = Observer(_make_config())
        msgs = obs._build_prompt({
            "user_text": "开灯",
            "assistant_text": "好的",
            "tool_calls": [
                {"name": "smart_home", "args": {"action": "on"}, "result": "灯已开"},
            ],
        })
        content = msgs[1]["content"]
        assert "工具调用" in content
        assert "smart_home" in content

    def test_includes_emotion(self):
        obs = Observer(_make_config())
        msgs = obs._build_prompt({
            "user_text": "累死了",
            "assistant_text": "辛苦了",
            "user_emotion": "tired",
        })
        content = msgs[1]["content"]
        assert "用户情绪检测" in content
        assert "tired" in content

    def test_omits_optional_fields_when_absent(self):
        obs = Observer(_make_config())
        msgs = obs._build_prompt({
            "user_text": "你好",
            "assistant_text": "嗨",
        })
        content = msgs[1]["content"]
        assert "工具调用" not in content
        assert "用户情绪检测" not in content


class TestExtract:
    """Test Observer.extract with mocked HTTP."""

    def _mock_response(self, observations: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "record_observations",
                            "arguments": json.dumps({"observations": observations}),
                        },
                    }],
                },
            }],
        }
        return resp

    @patch("memory.observer._SESSION")
    def test_extract_success(self, mock_session):
        observations = [
            {"priority": "🔴", "time": "14:30", "text": "用户偏好暖黄灯光"},
        ]
        mock_session.post.return_value = self._mock_response(observations)

        obs = Observer(_make_config())
        result = obs.extract({
            "user_text": "我喜欢暖黄灯光",
            "assistant_text": "好的，已记住",
        })
        assert len(result) == 1
        assert result[0]["priority"] == "🔴"
        assert result[0]["text"] == "用户偏好暖黄灯光"

    @patch("memory.observer._SESSION")
    def test_extract_failure_returns_empty(self, mock_session):
        mock_session.post.side_effect = Exception("Network error")

        obs = Observer(_make_config())
        result = obs.extract({
            "user_text": "你好",
            "assistant_text": "你好呀",
        })
        assert result == []

    @patch("memory.observer._SESSION")
    def test_extract_fallback_on_primary_failure(self, mock_session):
        observations = [
            {"priority": "🟡", "time": "10:00", "text": "用户打了招呼"},
        ]
        # Primary fails, fallback succeeds
        mock_session.post.side_effect = [
            Exception("Primary down"),
            self._mock_response(observations),
        ]

        obs = Observer(_make_config())
        result = obs.extract({
            "user_text": "你好",
            "assistant_text": "嗨",
        })
        assert len(result) == 1
        # Verify fallback model was used
        assert mock_session.post.call_count == 2

    @patch("memory.observer._SESSION")
    def test_extract_disabled_returns_empty(self, mock_session):
        cfg = _make_config()
        cfg["memory"]["observer"]["enabled"] = False
        obs = Observer(cfg)
        result = obs.extract({"user_text": "hi", "assistant_text": "hey"})
        assert result == []
        mock_session.post.assert_not_called()


class TestFormatMarkdown:
    """Test Observer.format_markdown output."""

    def test_format_observation_markdown(self):
        obs = Observer(_make_config())
        observations = [
            {"priority": "🔴", "time": "14:30", "text": "用户偏好暖黄灯光 2700K"},
            {"priority": "✅", "time": "14:30", "text": "客厅灯已调为暖黄"},
        ]
        md = obs.format_markdown(observations)
        assert "Date:" in md
        assert "* 🔴 (14:30) 用户偏好暖黄灯光 2700K" in md
        assert "* ✅ (14:30) 客厅灯已调为暖黄" in md

    def test_format_empty_observations(self):
        obs = Observer(_make_config())
        md = obs.format_markdown([])
        assert md == ""


class TestConfigDefaults:
    """Test that config defaults are applied correctly."""

    def test_defaults_applied(self):
        cfg = {"llm": {"api_key": "k"}, "memory": {}}
        obs = Observer(cfg)
        assert obs._primary_model == "grok-4.20-0309-non-reasoning"
        assert obs._fallback_model == "gemini-2.5-flash"
        assert obs._enabled is True
        assert obs._base_url == "https://api.x.ai/v1"

    def test_custom_config(self):
        cfg = _make_config()
        cfg["memory"]["observer"]["primary_model"] = "custom-model"
        obs = Observer(cfg)
        assert obs._primary_model == "custom-model"
