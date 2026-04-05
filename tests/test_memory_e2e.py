"""End-to-end memory integration tests.

Verifies the full pipeline: save → extract → store → query → inject.
Uses mocked LLM for extraction but real SQLite + embedder mock.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from memory.manager import MemoryManager
from memory.direct_answer import DirectAnswerer
from memory.behavior_log import BehaviorLog


def _make_config(db_path: str) -> dict:
    return {
        "memory": {"db_path": db_path},
        "llm": {"api_key": "test-key", "model": "gpt-4o-mini"},
    }


def _deterministic_encode(text: str) -> np.ndarray:
    rng = np.random.RandomState(hash(text) % 2**31)
    v = rng.randn(512).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


@pytest.fixture()
def setup(tmp_path):
    db_path = str(tmp_path / "e2e.db")
    config = _make_config(db_path)
    mgr = MemoryManager(config)
    mgr.embedder = MagicMock()
    mgr.embedder.encode = _deterministic_encode
    answerer = DirectAnswerer(mgr.store, mgr.embedder)
    blog = BehaviorLog(db_path)
    return mgr, answerer, blog


class TestMemoryE2E:
    def test_save_then_query_contains_memory(self, setup):
        """After saving a conversation, query should return the extracted fact."""
        mgr, answerer, blog = setup
        extraction = {
            "memories": [{
                "content": "Allen 喜欢拿铁",
                "category": "preference",
                "key": "favorite_drink",
                "importance": 8,
                "tags": ["饮品"],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [],
            "profile_update": {
                "identity": {"name": "Allen"},
                "preferences": {"likes": ["拿铁"]},
                "relationships": {},
                "routines": {},
                "pending": [],
                "status": "",
            },
            "episode_summary": "Allen 说了他喜欢喝拿铁",
            "mood": "neutral",
            "topics": ["饮品偏好"],
        }
        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [
                    {"role": "user", "content": "我喜欢喝拿铁"},
                    {"role": "assistant", "content": "好的，记住了！"},
                ],
                "allen", "session1",
            )
        context = mgr.query("喝什么", "allen")
        assert "拿铁" in context
        assert "<memory>" in context

    def test_save_then_direct_answer(self, setup):
        """After saving, DirectAnswerer should answer related queries."""
        mgr, answerer, blog = setup
        content = "Allen 喜欢拿铁"
        emb = _deterministic_encode(content)
        mgr.store.add_memory(
            user_id="allen", content=content,
            category="preference", key="favorite_drink",
            importance=8.0, embedding=emb,
        )
        result = answerer.try_answer(content, "allen")
        assert result is not None
        assert "拿铁" in result

    def test_correction_supersedes_old_memory(self, setup):
        """Saving a correction should deactivate the old memory."""
        mgr, answerer, blog = setup
        old_emb = _deterministic_encode("Allen 喜欢拿铁")
        mgr.store.add_memory(
            user_id="allen", content="Allen 喜欢拿铁",
            category="preference", key="favorite_drink",
            importance=7.0, embedding=old_emb,
        )
        extraction = {
            "memories": [{
                "content": "Allen 喜欢美式，不喜欢拿铁",
                "category": "preference",
                "key": "favorite_drink",
                "importance": 8,
                "tags": ["饮品"],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [{
                "old_content": "喜欢拿铁",
                "new_content": "喜欢美式",
                "reason": "用户纠正",
            }],
            "profile_update": None,
            "episode_summary": "Allen 纠正饮品偏好为美式",
            "mood": "neutral",
            "topics": ["偏好修正"],
        }
        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [{"role": "user", "content": "不对，我喜欢美式不是拿铁"}],
                "allen", "session2",
            )
        active = mgr.store.get_active_memories("allen")
        contents = [m["content"] for m in active]
        assert any("美式" in c for c in contents)
        assert not any(c == "Allen 喜欢拿铁" for c in contents)

    def test_behavior_log_records(self, setup):
        """BehaviorLog should record events correctly."""
        mgr, answerer, blog = setup
        blog.log("allen", "skill_call", {"skill": "weather"})
        blog.log("allen", "conversation", {"text": "今天天气", "route": "cloud"})
        events = blog.get_events("allen")
        assert len(events) == 2

    def test_profile_rebuilds_from_memories(self, setup):
        """Profile should auto-rebuild when preference memories are saved."""
        mgr, answerer, blog = setup
        extraction = {
            "memories": [{
                "content": "Allen 喜欢拿铁",
                "category": "preference",
                "key": "favorite_drink",
                "importance": 8,
                "tags": [],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [],
            "profile_update": None,
            "episode_summary": "聊了饮品偏好",
            "mood": "neutral",
            "topics": [],
        }
        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [{"role": "user", "content": "我喜欢拿铁"}],
                "allen", "session1",
            )
        profile = mgr.store.get_profile("allen")
        assert profile is not None

    def test_memory_injection_capped(self, setup):
        """Large number of memories should still produce bounded context."""
        mgr, answerer, blog = setup
        for i in range(50):
            emb = _deterministic_encode(f"记忆{i}")
            mgr.store.add_memory(
                user_id="allen",
                content=f"这是第{i}条测试记忆" * 5,
                category="knowledge",
                importance=5.0,
                embedding=emb,
            )
        context = mgr.query("测试", "allen")
        # Budget is 2000 chars content + XML tags overhead
        assert len(context) < 2500

    def test_usage_guide_not_in_memory_context(self, setup):
        """Usage guide moved to personality.py — should NOT appear in memory context."""
        mgr, answerer, blog = setup
        mgr.store.set_profile("allen", {"identity": {"name": "Allen"}})
        context = mgr.query("你好", "allen")
        assert "[使用原则]" not in context
