"""Tests for memory.store — SQLite memory storage."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
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


class TestRelations:
    """Entity relationship storage."""

    def test_add_and_get_relation(self, store: MemoryStore):
        rel_id = store.add_relation("u1", "Allen", "妹妹", "小美", "mem1")
        assert rel_id
        rels = store.get_relations("u1")
        assert len(rels) == 1
        assert rels[0]["source_entity"] == "Allen"
        assert rels[0]["target_entity"] == "小美"

    def test_get_relations_by_entity(self, store: MemoryStore):
        store.add_relation("u1", "Allen", "妹妹", "小美")
        store.add_relation("u1", "Allen", "女朋友", "Sarah")
        store.add_relation("u1", "Allen", "同事", "Tom")
        rels = store.get_relations("u1", entity="Allen")
        assert len(rels) == 3
        rels = store.get_relations("u1", entity="小美")
        assert len(rels) == 1

    def test_get_all_entities(self, store: MemoryStore):
        store.add_relation("u1", "Allen", "妹妹", "小美")
        store.add_relation("u1", "Allen", "女朋友", "Sarah")
        entities = store.get_all_entities("u1")
        assert "Allen" in entities
        assert "小美" in entities
        assert "Sarah" in entities

    def test_user_isolation(self, store: MemoryStore):
        store.add_relation("u1", "Allen", "妹妹", "小美")
        store.add_relation("u2", "Bob", "弟弟", "Tom")
        assert len(store.get_relations("u1")) == 1
        assert len(store.get_relations("u2")) == 1


class TestEpisodes:
    """Conversation episode storage."""

    def test_add_and_get_episodes(self, store: MemoryStore):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        store.add_episode("user1", "sess1", "聊了股票", today, mood="neutral", topics=["股票"])
        store.add_episode("user1", "sess2", "聊了天气", yesterday, mood="happy", topics=["天气"])

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

    def test_episode_dedup_skips_similar(self, store: MemoryStore):
        """Same-day episode with substantially similar summary should be skipped."""
        ep1 = store.add_episode("user1", "s1", "聊了咖啡偏好和拿铁", "2026-04-02")
        assert ep1 is not None

        # Very similar summary on the same day
        ep2 = store.add_episode("user1", "s2", "聊了咖啡偏好和拿铁咖啡", "2026-04-02")
        assert ep2 is None  # should be skipped

        episodes = store.get_recent_episodes("user1", days=3)
        assert len(episodes) == 1

    def test_episode_dedup_allows_different(self, store: MemoryStore):
        """Different content on the same day should NOT be skipped."""
        ep1 = store.add_episode("user1", "s1", "聊了咖啡偏好", "2026-04-02")
        assert ep1 is not None

        ep2 = store.add_episode("user1", "s2", "讨论了周末爬山计划", "2026-04-02")
        assert ep2 is not None

        episodes = store.get_recent_episodes("user1", days=3)
        assert len(episodes) == 2

    def test_episode_dedup_different_day_allowed(self, store: MemoryStore):
        """Same summary on different days should NOT be skipped."""
        ep1 = store.add_episode("user1", "s1", "聊了咖啡偏好", "2026-04-01")
        ep2 = store.add_episode("user1", "s2", "聊了咖啡偏好", "2026-04-02")
        assert ep1 is not None
        assert ep2 is not None


class TestEpisodeDigests:
    """Episode digest storage."""

    def test_add_and_get_digest(self, store: MemoryStore):
        d_id = store.add_digest("user1", "2026-03-01", "2026-03-07", "聊了工作；聊了爬山")
        assert d_id is not None

        digests = store.get_recent_digests("user1", limit=4)
        assert len(digests) == 1
        assert digests[0]["digest"] == "聊了工作；聊了爬山"
        assert digests[0]["period_start"] == "2026-03-01"
        assert digests[0]["period_end"] == "2026-03-07"

    def test_digest_ordering(self, store: MemoryStore):
        """Digests should be returned newest first."""
        store.add_digest("user1", "2026-02-01", "2026-02-07", "二月第一周")
        store.add_digest("user1", "2026-03-01", "2026-03-07", "三月第一周")
        store.add_digest("user1", "2026-01-01", "2026-01-07", "一月第一周")

        digests = store.get_recent_digests("user1", limit=4)
        assert len(digests) == 3
        assert digests[0]["digest"] == "三月第一周"
        assert digests[1]["digest"] == "二月第一周"
        assert digests[2]["digest"] == "一月第一周"

    def test_digest_user_isolated(self, store: MemoryStore):
        store.add_digest("user1", "2026-03-01", "2026-03-07", "user1的周")
        store.add_digest("user2", "2026-03-01", "2026-03-07", "user2的周")

        assert len(store.get_recent_digests("user1")) == 1
        assert len(store.get_recent_digests("user2")) == 1

    def test_digest_limit(self, store: MemoryStore):
        for i in range(6):
            store.add_digest("user1", f"2026-0{i+1}-01", f"2026-0{i+1}-07", f"week {i}")

        digests = store.get_recent_digests("user1", limit=3)
        assert len(digests) == 3
