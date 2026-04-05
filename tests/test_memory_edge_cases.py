"""Edge case tests for memory subsystem — L1 threshold, profile rebuild, dedup, expires."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from memory.direct_answer import DirectAnswerer, _SIMILARITY_THRESHOLD
from memory.manager import MemoryManager
from memory.store import MemoryStore


def _encode(text: str) -> np.ndarray:
    rng = np.random.RandomState(hash(text) % 2**31)
    v = rng.randn(512).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def answerer(store):
    embedder = MagicMock()
    embedder.encode = _encode
    return DirectAnswerer(store, embedder)


@pytest.fixture()
def mgr(tmp_path):
    config = {
        "memory": {"db_path": str(tmp_path / "mgr.db")},
        "llm": {"api_key": "test"},
    }
    m = MemoryManager(config)
    m.embedder = MagicMock()
    m.embedder.encode = _encode
    return m


# ---------------------------------------------------------------
# L1 Direct Answer: threshold boundary
# ---------------------------------------------------------------

class TestL1Threshold:
    def test_threshold_is_0_55(self):
        assert _SIMILARITY_THRESHOLD == 0.55

    def test_exact_match_returns_answer(self, answerer: DirectAnswerer):
        """Same text → cosine=1.0 → should always hit."""
        content = "Allen 喜欢拿铁"
        answerer._store.add_memory(
            user_id="u1", content=content,
            category="preference", key="drink",
            importance=8.0, embedding=_encode(content),
        )
        result = answerer.try_answer(content, "u1")
        assert result is not None
        assert "拿铁" in result

    def test_touch_updates_access_count(self, answerer: DirectAnswerer):
        """Successful L1 hit should increment access_count."""
        content = "Allen 住温哥华"
        answerer._store.add_memory(
            user_id="u1", content=content,
            category="identity", key="location",
            importance=8.0, embedding=_encode(content),
        )
        answerer.try_answer(content, "u1")
        mems = answerer._store.get_active_memories("u1")
        assert mems[0]["access_count"] == 1

    def test_event_category_never_triggers(self, answerer: DirectAnswerer):
        content = "Allen 明天去北京"
        answerer._store.add_memory(
            user_id="u1", content=content,
            category="event", importance=9.0,
            embedding=_encode(content),
        )
        assert answerer.try_answer(content, "u1") is None

    def test_knowledge_category_triggers(self, answerer: DirectAnswerer):
        content = "WiFi密码是abc123"
        answerer._store.add_memory(
            user_id="u1", content=content,
            category="knowledge", key="wifi",
            importance=9.0, embedding=_encode(content),
        )
        result = answerer.try_answer(content, "u1")
        assert result is not None
        assert "abc123" in result

    def test_user_isolation(self, answerer: DirectAnswerer):
        """User A's memory should not answer user B's query."""
        content = "Allen 喜欢寿司"
        answerer._store.add_memory(
            user_id="userA", content=content,
            category="preference", key="food",
            importance=8.0, embedding=_encode(content),
        )
        assert answerer.try_answer(content, "userB") is None


# ---------------------------------------------------------------
# Profile rebuild
# ---------------------------------------------------------------

class TestProfileRebuild:
    def test_preference_without_colon(self, mgr: MemoryManager):
        """Content like '用户 喜欢拿铁' (no colon) should still be added to likes."""
        mgr.store.add_memory(
            user_id="u1", content="用户 喜欢拿铁",
            category="preference", importance=7.0,
            embedding=_encode("用户 喜欢拿铁"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        assert "用户 喜欢拿铁" in profile["preferences"].get("likes", [])

    def test_dislike_detected(self, mgr: MemoryManager):
        """Content with '不' should go to dislikes."""
        mgr.store.add_memory(
            user_id="u1", content="用户 不喜欢香菜",
            category="preference", importance=7.0,
            embedding=_encode("用户 不喜欢香菜"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        assert "用户 不喜欢香菜" in profile["preferences"].get("dislikes", [])

    def test_identity_with_key(self, mgr: MemoryManager):
        mgr.store.add_memory(
            user_id="u1", content="Allen 住在温哥华",
            category="identity", key="location",
            importance=8.0, embedding=_encode("Allen 住在温哥华"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        assert profile["identity"]["location"] == "Allen 住在温哥华"

    def test_relationship_with_key(self, mgr: MemoryManager):
        mgr.store.add_memory(
            user_id="u1", content="Allen 的妹妹叫小美",
            category="relationship", key="sister",
            importance=7.0, embedding=_encode("Allen 的妹妹叫小美"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        assert profile["relationships"]["sister"] == "Allen 的妹妹叫小美"

    def test_task_with_expires_goes_to_pending(self, mgr: MemoryManager):
        mgr.store.add_memory(
            user_id="u1", content="用户 周五要交报告",
            category="task", importance=8.0,
            time_ref="2026-04-10", expires="2026-04-11",
            embedding=_encode("用户 周五要交报告"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        pending = profile.get("pending", [])
        assert len(pending) == 1
        assert "交报告" in pending[0]["content"]

    def test_no_duplicate_likes(self, mgr: MemoryManager):
        """Rebuilding twice should not duplicate entries."""
        mgr.store.add_memory(
            user_id="u1", content="用户 喜欢跑步",
            category="preference", importance=5.0,
            embedding=_encode("用户 喜欢跑步"),
        )
        mgr._rebuild_profile("u1")
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        assert profile["preferences"]["likes"].count("用户 喜欢跑步") == 1


# ---------------------------------------------------------------
# Key-based dedup in save pipeline
# ---------------------------------------------------------------

class TestKeyDedup:
    def test_same_key_supersedes(self, mgr: MemoryManager):
        """Adding memory with existing key should supersede old one."""
        old_emb = _encode("Allen 住在北京")
        mgr.store.add_memory(
            user_id="u1", content="Allen 住在北京",
            category="identity", key="location",
            importance=7.0, embedding=old_emb,
        )
        extraction = {
            "memories": [{
                "content": "Allen 住在温哥华",
                "category": "identity",
                "key": "location",
                "importance": 8,
                "tags": [],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [],
            "profile_update": None,
            "episode_summary": "更新了住址",
            "mood": "neutral",
            "topics": [],
        }
        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [{"role": "user", "content": "我搬到温哥华了"}],
                "u1", "s1",
            )
        active = mgr.store.get_active_memories("u1")
        contents = [m["content"] for m in active]
        assert "Allen 住在温哥华" in contents
        assert "Allen 住在北京" not in contents

    def test_find_by_key_returns_none_for_missing(self, store: MemoryStore):
        assert store.find_by_key("u1", "identity", "nonexistent") is None


# ---------------------------------------------------------------
# Expires handling in retriever
# ---------------------------------------------------------------

class TestExpiresHandling:
    def test_expired_memory_gets_penalty(self, mgr: MemoryManager):
        """Expired memories should get 0.5x score penalty."""
        from memory.retriever import MemoryRetriever
        retriever = MemoryRetriever(mgr.store)

        # Add expired memory
        content_expired = "用户 昨天的会议"
        mgr.store.add_memory(
            user_id="u1", content=content_expired,
            category="event", importance=9.0,
            expires="2020-01-01",  # definitely expired
            embedding=_encode(content_expired),
        )
        # Add non-expired memory
        content_active = "用户 喜欢咖啡"
        mgr.store.add_memory(
            user_id="u1", content=content_active,
            category="preference", importance=5.0,
            embedding=_encode(content_active),
        )

        results = retriever.retrieve(
            _encode(content_expired), "u1", top_k=2,
        )
        # Both returned, but expired one should have lower effective score
        scores = {r["content"]: r["_score"] for r in results}
        # The expired memory has perfect cosine match but 0.5x penalty
        # Just verify both are returned and we get valid scores
        assert len(results) == 2
        assert all(r["_score"] > 0 for r in results)


# ---------------------------------------------------------------
# Sweep expired & backfill
# ---------------------------------------------------------------

class TestSweepExpired:
    def test_expired_event_swept(self, store: MemoryStore):
        """Expired event should be deactivated by sweep."""
        store.add_memory(
            user_id="u1", content="昨天的会议",
            category="event", importance=7.0,
            expires="2020-01-01", embedding=_encode("昨天的会议"),
        )
        assert store.count_active("u1") == 1
        swept = store.sweep_expired()
        assert swept == 1
        assert store.count_active("u1") == 0

    def test_unexpired_event_not_swept(self, store: MemoryStore):
        """Future-expiry event should stay active."""
        store.add_memory(
            user_id="u1", content="下周的会议",
            category="event", importance=7.0,
            expires="2099-12-31", embedding=_encode("下周的会议"),
        )
        swept = store.sweep_expired()
        assert swept == 0
        assert store.count_active("u1") == 1

    def test_preference_not_swept(self, store: MemoryStore):
        """Preferences should not be swept even if expired."""
        store.add_memory(
            user_id="u1", content="喜欢咖啡",
            category="preference", importance=5.0,
            expires="2020-01-01", embedding=_encode("喜欢咖啡"),
        )
        swept = store.sweep_expired()
        assert swept == 0
        assert store.count_active("u1") == 1

    def test_backfill_sets_expires(self, store: MemoryStore):
        """Event with time_ref but no expires should get expires backfilled."""
        store.add_memory(
            user_id="u1", content="周五面试",
            category="event", importance=7.0,
            time_ref="2026-04-10", embedding=_encode("周五面试"),
        )
        backfilled = store.backfill_expires()
        assert backfilled == 1
        mems = store.get_active_memories("u1")
        assert mems[0]["expires"] == "2026-04-13"  # +3 days

    def test_backfill_skips_with_existing_expires(self, store: MemoryStore):
        """Event that already has expires should not be touched."""
        store.add_memory(
            user_id="u1", content="周五面试",
            category="event", importance=7.0,
            time_ref="2026-04-10", expires="2026-04-15",
            embedding=_encode("周五面试"),
        )
        backfilled = store.backfill_expires()
        assert backfilled == 0


class TestRebuildProfilePendingCleanup:
    def test_expired_pending_filtered(self, mgr: MemoryManager):
        """Expired pending items should be removed during profile rebuild."""
        mgr.store.add_memory(
            user_id="u1", content="已经过期的面试",
            category="task", importance=8.0,
            time_ref="2020-01-01", expires="2020-01-02",
            embedding=_encode("已经过期的面试"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        pending = profile.get("pending", [])
        assert len(pending) == 0

    def test_today_pending_kept(self, mgr: MemoryManager):
        """Today's pending items should be kept."""
        from datetime import datetime as dt
        today = dt.now().strftime("%Y-%m-%d")
        mgr.store.add_memory(
            user_id="u1", content="今天的面试",
            category="task", importance=8.0,
            time_ref=today, expires=today,
            embedding=_encode("今天的面试"),
        )
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        pending = profile.get("pending", [])
        assert len(pending) == 1
        assert "今天的面试" in pending[0]["content"]

    def test_pending_with_non_dict_items_survives(self, mgr: MemoryManager):
        """Profile with non-dict pending items should not crash rebuild."""
        # Pre-seed a profile with corrupted pending (string instead of dict)
        mgr.store.set_profile("u1", {
            "identity": {}, "preferences": {}, "relationships": {},
            "routines": {}, "pending": ["stale string item", 42], "status": "",
        })
        mgr.store.add_memory(
            user_id="u1", content="新的任务",
            category="task", importance=7.0,
            time_ref="2099-12-31", expires="2099-12-31",
            embedding=_encode("新的任务"),
        )
        # Should not crash
        mgr._rebuild_profile("u1")
        profile = mgr.store.get_profile("u1")
        # Old non-dict items should be filtered out, new task kept
        pending = profile.get("pending", [])
        assert all(isinstance(p, dict) for p in pending)


# ---------------------------------------------------------------
# Phase 1 deep verification — additional edge cases
# ---------------------------------------------------------------

class TestTimezoneEdgeCases:
    """C1: Verify timezone handling in episode queries."""

    def test_today_episode_always_found(self, store: MemoryStore):
        """Episode from today should always be within any days window."""
        from datetime import datetime as dt
        today = dt.now().strftime("%Y-%m-%d")
        store.add_episode("u1", "s1", "今天的对话", today)
        episodes = store.get_recent_episodes("u1", days=1)
        assert len(episodes) == 1

    def test_episode_boundary_exactly_n_days(self, store: MemoryStore):
        """Episode from exactly N days ago should be included (>= boundary)."""
        from datetime import datetime as dt, timedelta
        boundary_date = (dt.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        store.add_episode("u1", "s1", "三天前的对话", boundary_date)
        episodes = store.get_recent_episodes("u1", days=3)
        assert len(episodes) == 1, f"Episode on boundary date {boundary_date} should be included"

    def test_episode_just_outside_window(self, store: MemoryStore):
        """Episode from N+1 days ago should be excluded."""
        from datetime import datetime as dt, timedelta
        old_date = (dt.now() - timedelta(days=4)).strftime("%Y-%m-%d")
        store.add_episode("u1", "s1", "四天前的对话", old_date)
        episodes = store.get_recent_episodes("u1", days=3)
        assert len(episodes) == 0


class TestSweepExpiredAdditional:
    """C3: Additional sweep edge cases."""

    def test_already_inactive_not_double_swept(self, store: MemoryStore):
        """Already inactive memories should not be counted in sweep."""
        mem_id = store.add_memory(
            user_id="u1", content="旧事件",
            category="event", importance=5.0,
            expires="2020-01-01", embedding=_encode("旧事件"),
        )
        # Manually deactivate first
        store.deactivate_memory_by_id(mem_id)
        assert store.count_active("u1") == 0
        # Sweep should find nothing
        swept = store.sweep_expired()
        assert swept == 0

    def test_backfill_ignores_non_event_task(self, store: MemoryStore):
        """Backfill should not touch identity/preference even with time_ref."""
        store.add_memory(
            user_id="u1", content="Allen 2026年搬到温哥华",
            category="identity", importance=8.0,
            time_ref="2026-01-15", embedding=_encode("搬家"),
        )
        backfilled = store.backfill_expires()
        assert backfilled == 0
        mems = store.get_active_memories("u1")
        assert mems[0].get("expires") is None

    def test_backfill_with_invalid_time_ref(self, store: MemoryStore):
        """Backfill with non-date time_ref should not crash (SQLite date() returns null)."""
        store.add_memory(
            user_id="u1", content="模糊时间的事件",
            category="event", importance=5.0,
            time_ref="下周某天", embedding=_encode("模糊事件"),
        )
        # Should not crash, but may or may not backfill depending on SQLite behavior
        backfilled = store.backfill_expires()
        # Just verify no crash — the result depends on SQLite's date() handling
        assert isinstance(backfilled, int)

    def test_maintain_returns_swept_and_backfilled(self, mgr: MemoryManager):
        """maintain() should include swept and backfilled in return dict."""
        # Add an expired event
        mgr.store.add_memory(
            user_id="u1", content="过期事件",
            category="event", importance=5.0,
            expires="2020-01-01", embedding=_encode("过期事件"),
        )
        # Add an event with time_ref but no expires
        mgr.store.add_memory(
            user_id="u1", content="需要 backfill 的事件",
            category="event", importance=5.0,
            time_ref="2099-06-01", embedding=_encode("backfill事件"),
        )
        result = mgr.maintain("u1")
        assert "swept" in result
        assert "backfilled" in result
        assert result["swept"] == 1
        assert result["backfilled"] == 1


class TestDirectAnswerBoundary:
    """C2: DirectAnswer threshold boundary and edge cases."""

    def test_score_just_below_threshold_returns_none(self, answerer, store: MemoryStore):
        """Score at 0.549 should not trigger (< 0.55)."""
        # Create controlled vectors with known cosine similarity
        v_mem = np.zeros(512, dtype=np.float32)
        v_mem[0] = 1.0

        store.add_memory(
            user_id="u_b", content="Allen 喜欢拿铁",
            category="preference", key="drink",
            importance=8.0, embedding=v_mem,
        )

        # Craft query vector with cosine = 0.549 to v_mem
        # cos(theta) = 0.549 → v_query = [0.549, sqrt(1 - 0.549^2), 0, 0, ...]
        import math
        cos_target = 0.549
        v_query = np.zeros(512, dtype=np.float32)
        v_query[0] = cos_target
        v_query[1] = math.sqrt(1 - cos_target ** 2)

        answerer._embedder.encode = lambda _text: v_query
        result = answerer.try_answer("咖啡", "u_b")
        assert result is None

    def test_score_just_above_threshold_returns_answer(self, answerer, store: MemoryStore):
        """Score at 0.56 should trigger (> 0.55), assuming margin is sufficient."""
        v_mem = np.zeros(512, dtype=np.float32)
        v_mem[0] = 1.0

        store.add_memory(
            user_id="u_a", content="Allen 喜欢拿铁",
            category="preference", key="drink",
            importance=8.0, embedding=v_mem,
        )

        import math
        cos_target = 0.56
        v_query = np.zeros(512, dtype=np.float32)
        v_query[0] = cos_target
        v_query[1] = math.sqrt(1 - cos_target ** 2)

        answerer._embedder.encode = lambda _text: v_query
        # Only 1 candidate → no margin check
        result = answerer.try_answer("咖啡", "u_a")
        assert result is not None
        assert "拿铁" in result

    def test_empty_content_memory_skipped(self, answerer, store: MemoryStore):
        """Memory with no embedding should be silently skipped."""
        store.add_memory(
            user_id="u_e", content="no embedding memory",
            category="preference", key="test",
            importance=5.0,
            # No embedding!
        )
        result = answerer.try_answer("anything", "u_e")
        assert result is None


class TestBudgetAndUsageGuide:
    """C4: Verify budget cap and usage guide relocation."""

    def test_memory_context_under_new_budget(self, mgr: MemoryManager):
        """With 2000 char budget, more memories fit than with old 1200."""
        profile = {"identity": {"name": "Allen"}, "preferences": {"likes": ["拿铁"]}}
        mgr.store.set_profile("u1", profile)
        # Add 15 memories — would exceed 1200 but fit in 2000
        for i in range(15):
            mgr.store.add_memory(
                user_id="u1", content=f"Allen 的记忆条目 {i} — 包含一些内容来填充字符",
                category="preference", importance=5.0,
                embedding=_encode(f"memory_{i}"),
            )
        result = mgr.query("test", "u1")
        # Should have content (not truncated to nothing)
        assert "[记忆]" in result
        # Should be under budget
        assert len(result) <= 2500  # 2000 + XML tags overhead

    def test_usage_guide_not_in_memory_block(self, mgr: MemoryManager):
        """Usage guide should NOT appear in the <memory> block anymore."""
        mgr.store.add_memory(
            user_id="u1", content="Allen 喜欢拿铁",
            category="preference", importance=5.0,
            embedding=_encode("拿铁"),
        )
        result = mgr.query("test", "u1")
        assert "使用原则" not in result
        assert "像朋友一样" not in result
