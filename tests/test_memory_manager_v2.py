"""Tests for MemoryManager v2 methods: build_stable_prefix + write_observation."""

from __future__ import annotations

import json
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
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
    """MemoryManager with mock embedder for v2 tests."""
    config = _make_config(str(tmp_path / "test_v2.db"))
    mgr = MemoryManager(config)

    # Mock embedder to avoid loading real model
    def mock_encode(text: str) -> np.ndarray:
        rng = np.random.RandomState(hash(text) % 2**31)
        v = rng.randn(512).astype(np.float32)
        v /= np.linalg.norm(v)
        return v

    mgr.embedder = MagicMock()
    mgr.embedder.encode = mock_encode
    return mgr


def test_build_stable_prefix_returns_string(manager: MemoryManager):
    result = manager.build_stable_prefix(recent_turns=[], current_input="你好")
    assert isinstance(result, str)
    assert "你好" in result


def test_build_stable_prefix_defaults(manager: MemoryManager):
    """Calling with no args should still work (defaults to empty)."""
    result = manager.build_stable_prefix()
    assert isinstance(result, str)


@patch("memory.observer._SESSION")
def test_write_observation_stores_data(mock_session, manager: MemoryManager):
    # Mock Observer LLM response with function call
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


@patch("memory.observer._SESSION")
def test_write_observation_returns_zero_when_empty(mock_session, manager: MemoryManager):
    """Observer returns no observations -> write_observation returns 0."""
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


@patch("memory.observer._SESSION")
def test_write_observation_multiple(mock_session, manager: MemoryManager):
    """Multiple observations in one extraction."""
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


def test_existing_query_still_works(manager: MemoryManager):
    """v1 query() must still work after v2 additions."""
    result = manager.query("你好", "user1")
    assert result == ""  # empty store returns ""
