"""Tests for memory.store — SQLite memory storage."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    """Fresh MemoryStore backed by a temp SQLite DB."""
    db_path = tmp_path / "test_memory.db"
    s = MemoryStore(db_path)
    yield s
    s.close()


class TestMemoryCRUD:
    """Basic create/read operations for memories."""

    def test_add_and_get(self, store: MemoryStore):
        emb = np.random.randn(512).astype(np.float32)
        mem_id = store.add_memory(
            "user1", "Allen 喜欢拿铁", "preference",
            importance=8.0, tags=["咖啡"], embedding=emb,
        )
        memories = store.get_active_memories("user1")
        assert len(memories) == 1
        assert memories[0]["id"] == mem_id
        assert memories[0]["content"] == "Allen 喜欢拿铁"
        assert memories[0]["category"] == "preference"
        assert memories[0]["importance"] == 8.0
        assert memories[0]["tags"] == ["咖啡"]
        assert memories[0]["active"] == 1
        assert memories[0]["embedding"] is not None
        np.testing.assert_allclose(memories[0]["embedding"], emb, atol=1e-6)

    def test_count_active(self, store: MemoryStore):
        assert store.count_active("user1") == 0
        store.add_memory("user1", "fact 1", "fact")
        store.add_memory("user1", "fact 2", "fact")
        store.add_memory("user2", "fact 3", "fact")
        assert store.count_active("user1") == 2
        assert store.count_active("user2") == 1

    def test_touch_memory(self, store: MemoryStore):
        mem_id = store.add_memory("user1", "test", "fact")
        memories = store.get_active_memories("user1")
        assert memories[0]["access_count"] == 0

        store.touch_memory(mem_id)
        store.touch_memory(mem_id)
        memories = store.get_active_memories("user1")
        assert memories[0]["access_count"] == 2

    def test_memory_summaries(self, store: MemoryStore):
        store.add_memory("user1", "Allen 喜欢跑步", "preference")
        store.add_memory("user1", "Allen 住温哥华", "fact")
        summaries = store.get_memory_summaries("user1")
        assert len(summaries) == 2
        assert "Allen 喜欢跑步" in summaries
        assert "Allen 住温哥华" in summaries

    def test_no_embedding(self, store: MemoryStore):
        """Memories can be stored without an embedding."""
        store.add_memory("user1", "no emb", "fact")
        memories = store.get_active_memories("user1")
        assert memories[0]["embedding"] is None


class TestSupersede:
    """Memory superseding (update without delete)."""

    def test_supersede_deactivates_old(self, store: MemoryStore):
        old_id = store.add_memory("user1", "住温哥华", "fact")
        new_id = store.add_memory("user1", "搬到多伦多", "fact")
        store.supersede_memory(old_id, new_id)

        active = store.get_active_memories("user1")
        assert len(active) == 1
        assert active[0]["id"] == new_id
        assert active[0]["content"] == "搬到多伦多"

    def test_deactivate_by_content(self, store: MemoryStore):
        store.add_memory("user1", "WiFi密码是abc123", "knowledge")
        assert store.count_active("user1") == 1

        result = store.deactivate_memory("user1", "WiFi密码")
        assert result is True
        assert store.count_active("user1") == 0

    def test_deactivate_no_match(self, store: MemoryStore):
        store.add_memory("user1", "Allen 喜欢拿铁", "preference")
        result = store.deactivate_memory("user1", "不存在的内容")
        assert result is False
        assert store.count_active("user1") == 1

    def test_deactivate_memory_by_id(self, store: MemoryStore):
        mem_id = store.add_memory("user1", "Allen 喜欢拿铁", "preference")
        assert store.count_active("user1") == 1
        result = store.deactivate_memory_by_id(mem_id)
        assert result is True
        assert store.count_active("user1") == 0

    def test_deactivate_memory_by_id_not_found(self, store: MemoryStore):
        store.add_memory("user1", "Allen 喜欢拿铁", "preference")
        result = store.deactivate_memory_by_id("nonexistent-id")
        assert result is False
        assert store.count_active("user1") == 1


class TestUserProfiles:
    """User profile CRUD."""

    def test_set_and_get_profile(self, store: MemoryStore):
        profile = {"identity": {"name": "Allen"}, "preferences": {"likes": ["拿铁"]}}
        store.set_profile("user1", profile)

        result = store.get_profile("user1")
        assert result == profile

    def test_get_nonexistent_profile(self, store: MemoryStore):
        assert store.get_profile("nobody") is None

    def test_update_profile(self, store: MemoryStore):
        store.set_profile("user1", {"name": "Allen"})
        store.set_profile("user1", {"name": "Allen", "location": "温哥华"})

        result = store.get_profile("user1")
        assert result["location"] == "温哥华"


class TestEpisodes:
    """Conversation episode storage."""

    def test_add_and_get_episodes(self, store: MemoryStore):
        store.add_episode("user1", "sess1", "聊了股票", "2026-04-02", mood="neutral", topics=["股票"])
        store.add_episode("user1", "sess2", "聊了天气", "2026-04-01", mood="happy", topics=["天气"])

        episodes = store.get_recent_episodes("user1", days=3)
        assert len(episodes) == 2
        assert episodes[0]["summary"] == "聊了股票"  # newest first

    def test_episodes_filtered_by_days(self, store: MemoryStore):
        store.add_episode("user1", "old", "很久以前", "2020-01-01")
        store.add_episode("user1", "new", "今天", "2026-04-02")

        episodes = store.get_recent_episodes("user1", days=3)
        # Only the recent one should appear (the 2020 one is >3 days old)
        summaries = [e["summary"] for e in episodes]
        assert "很久以前" not in summaries

    def test_episodes_user_isolated(self, store: MemoryStore):
        store.add_episode("user1", "s1", "user1的", "2026-04-02")
        store.add_episode("user2", "s2", "user2的", "2026-04-02")

        assert len(store.get_recent_episodes("user1", days=3)) == 1
        assert len(store.get_recent_episodes("user2", days=3)) == 1
