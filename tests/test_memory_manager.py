"""Tests for memory.manager — v3 MemoryManager API."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from memory.manager import MemoryManager


def _make_config(db_path: str) -> dict:
    return {
        "memory": {
            "db_path": db_path,
            "observer": {
                "enabled": True,
                "primary_model": "test-model",
                "fallback_model": "test-fallback",
            },
        },
        "llm": {
            "api_key": "test-key",
            "model": "gpt-4o-mini",
            "base_url": "https://api.example.com/v1",
        },
    }


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(_make_config(str(tmp_path / "test.db")))


class TestProfileToText:
    """Profile JSON to natural language conversion (Block 2 renderer)."""

    def test_full_profile(self, manager: MemoryManager):
        profile = {
            "identity": {"name": "Allen", "occupation": "程序员", "location": "温哥华"},
            "preferences": {"likes": ["拿铁", "跑步"], "dislikes": ["加班"]},
            "routines": {"周六": "跑步"},
            "status": "在做智能家居项目",
        }
        text = manager._profile_to_text(profile)
        assert "Allen" in text
        assert "程序员" in text
        assert "温哥华" in text
        assert "拿铁" in text
        assert "跑步" in text
        assert "加班" in text
        assert "智能家居" in text

    def test_empty_profile(self, manager: MemoryManager):
        assert manager._profile_to_text({}) == ""

    def test_none_profile(self, manager: MemoryManager):
        assert manager._profile_to_text(None) == ""


class TestBuildPromptContext:
    """MemoryManager.build_prompt_context + get_last_prompt_context."""

    def test_returns_prompt_context(self, manager: MemoryManager):
        from memory.hot.assembler import PromptContext
        ctx = manager.build_prompt_context(
            text="你好",
            user_id="allen",
            history=[],
        )
        assert isinstance(ctx, PromptContext)
        names = [b.name for b in ctx.blocks]
        assert "identity" in names
        assert "observations" in names
        assert "situation" in names

    def test_current_text_appended_to_messages(self, manager: MemoryManager):
        ctx = manager.build_prompt_context(
            text="当前输入",
            user_id="allen",
            history=[{"role": "user", "content": "之前"}],
        )
        assert ctx.messages[-1] == {"role": "user", "content": "当前输入"}
        assert ctx.messages[0] == {"role": "user", "content": "之前"}

    def test_situation_kwargs_forwarded(self, manager: MemoryManager):
        ctx = manager.build_prompt_context(
            text="x", user_id="allen", history=[],
            user_name="Allen", user_role="owner",
            user_emotion="SAD", situation="urgent",
        )
        situation = next(b for b in ctx.blocks if b.name == "situation")
        assert "Allen" in situation.content
        assert "不开心" in situation.content
        assert "严肃" in situation.content or "紧急" in situation.content

    def test_get_last_prompt_context_none_before_build(self, manager: MemoryManager):
        assert manager.get_last_prompt_context() is None

    def test_get_last_prompt_context_tracks_most_recent(self, manager: MemoryManager):
        ctx1 = manager.build_prompt_context(text="a", user_id="u", history=[])
        assert manager.get_last_prompt_context() is ctx1
        ctx2 = manager.build_prompt_context(text="b", user_id="u", history=[])
        assert manager.get_last_prompt_context() is ctx2


class TestWriteObservation:
    """MemoryManager.write_observation — Observer integration."""

    @patch("memory.cold.observer._SESSION")
    def test_stores_data(self, mock_session, manager: MemoryManager):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "record_observations",
                            "arguments": json.dumps({
                                "observations": [
                                    {"priority": "🔴", "time": "14:30", "text": "用户说你好"},
                                ]
                            })
                        }
                    }]
                }
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        turn_data = {
            "user_text": "你好",
            "assistant_text": "嗨",
            "tool_calls": [],
            "user_emotion": "",
        }
        count = manager.write_observation(turn_data, source_turn_id=1)
        assert count == 1

        obs = manager.store.get_all_observations()
        assert len(obs) == 1
        assert "用户说你好" in obs[0]["content"]

    @patch("memory.cold.observer._SESSION")
    def test_returns_zero_when_empty(self, mock_session, manager: MemoryManager):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "record_observations",
                            "arguments": json.dumps({"observations": []})
                        }
                    }]
                }
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        turn_data = {
            "user_text": "开灯",
            "assistant_text": "好的",
            "tool_calls": [],
            "user_emotion": "",
        }
        count = manager.write_observation(turn_data, source_turn_id=2)
        assert count == 0
        assert manager.store.get_all_observations() == []

    @patch("memory.cold.observer._SESSION")
    def test_multiple(self, mock_session, manager: MemoryManager):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "record_observations",
                            "arguments": json.dumps({
                                "observations": [
                                    {"priority": "🔴", "time": "10:00", "text": "用户住在温哥华"},
                                    {"priority": "🟡", "time": "10:00", "text": "用户喜欢拿铁"},
                                ]
                            })
                        }
                    }]
                }
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        turn_data = {
            "user_text": "我住温哥华，喜欢拿铁",
            "assistant_text": "好的，记住了",
            "tool_calls": [],
            "user_emotion": "",
        }
        count = manager.write_observation(turn_data, source_turn_id=3)
        assert count == 2
        obs = manager.store.get_all_observations()
        assert len(obs) == 1  # stored as single chunk
        assert "温哥华" in obs[0]["content"]
        assert "拿铁" in obs[0]["content"]
