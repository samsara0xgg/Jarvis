"""Tests for memory.manager — MemoryManager query/save pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from memory.manager import MemoryManager
from memory.store import MemoryStore


def _make_config(db_path: str) -> dict:
    return {
        "memory": {"db_path": db_path},
        "llm": {"api_key": "test-key", "model": "gpt-4o-mini"},
    }


@pytest.fixture()
def manager(tmp_path):
    """MemoryManager with a mock embedder (avoids loading real model)."""
    config = _make_config(str(tmp_path / "test.db"))
    mgr = MemoryManager(config)

    # Mock embedder to return deterministic vectors
    def mock_encode(text: str) -> np.ndarray:
        rng = np.random.RandomState(hash(text) % 2**31)
        v = rng.randn(512).astype(np.float32)
        v /= np.linalg.norm(v)
        return v

    mgr.embedder = MagicMock()
    mgr.embedder.encode = mock_encode
    return mgr


class TestQuery:
    """Memory query (read pipeline) tests."""

    def test_empty_returns_empty_string(self, manager: MemoryManager):
        result = manager.query("你好", "user1")
        assert result == ""

    def test_with_profile_only(self, manager: MemoryManager):
        manager.store.set_profile("user1", {
            "identity": {"name": "Allen", "location": "温哥华"},
            "preferences": {"likes": ["拿铁"]},
        })
        result = manager.query("你好", "user1")
        assert "<memory>" in result
        assert "Allen" in result
        assert "拿铁" in result

    def test_with_episodes(self, manager: MemoryManager):
        manager.store.add_episode(
            "user1", "s1", "聊了股票",
            datetime.now().strftime("%Y-%m-%d"),
        )
        result = manager.query("你好", "user1")
        assert "聊了股票" in result

    def test_with_memories(self, manager: MemoryManager):
        emb = manager.embedder.encode("Allen 喜欢跑步")
        manager.store.add_memory(
            "user1", "Allen 喜欢跑步", "preference",
            importance=7.0, embedding=emb,
        )
        result = manager.query("运动", "user1")
        assert "跑步" in result

    def test_full_inject_under_threshold(self, manager: MemoryManager):
        """Under 100 memories: all injected without embedding retrieval."""
        for i in range(5):
            emb = manager.embedder.encode(f"memory {i}")
            manager.store.add_memory("user1", f"memory {i}", "fact", embedding=emb)

        result = manager.query("anything", "user1")
        for i in range(5):
            assert f"memory {i}" in result

    def test_retrieval_over_threshold(self, manager: MemoryManager):
        """Over threshold: only top-k returned."""
        manager._full_inject_threshold = 5  # lower for testing
        for i in range(10):
            emb = manager.embedder.encode(f"memory {i}")
            manager.store.add_memory("user1", f"memory {i}", "fact", embedding=emb)

        result = manager.query("memory 0", "user1")
        assert "<memory>" in result
        # Should have some but not all 10
        count = sum(1 for i in range(10) if f"memory {i}" in result)
        assert count <= 5  # top_k = 5

    def test_pending_items_shown(self, manager: MemoryManager):
        today = datetime.now().strftime("%Y-%m-%d")
        manager.store.set_profile("user1", {
            "identity": {"name": "Allen"},
            "pending": [{"content": "面试结果", "date": today}],
        })
        result = manager.query("你好", "user1")
        assert "面试结果" in result
        assert "待关心" in result

    def test_future_pending_not_shown(self, manager: MemoryManager):
        manager.store.set_profile("user1", {
            "identity": {"name": "Allen"},
            "pending": [{"content": "未来的事", "date": "2099-12-31"}],
        })
        result = manager.query("你好", "user1")
        assert "[待关心]" not in result

    def test_memory_context_format(self, manager: MemoryManager):
        """Verify the <memory> block structure."""
        manager.store.set_profile("user1", {"identity": {"name": "Allen"}})
        manager.store.add_episode(
            "user1", "s1", "聊了天气",
            datetime.now().strftime("%Y-%m-%d"),
        )
        emb = manager.embedder.encode("fact")
        manager.store.add_memory("user1", "Allen 住温哥华", "fact", embedding=emb)

        result = manager.query("你好", "user1")
        assert result.startswith("<memory>")
        assert result.endswith("</memory>")
        assert "[关于用户]" in result
        assert "[最近]" in result
        assert "[记忆]" in result

    def test_memory_context_no_usage_guide(self, manager: MemoryManager):
        """Usage guide moved to personality.py — should NOT appear in memory context."""
        manager.store.set_profile("user1", {
            "identity": {"name": "Allen"},
        })
        result = manager.query("你好", "user1")
        assert "<memory>" in result
        assert "[使用原则]" not in result

    def test_memory_context_length_capped(self, manager: MemoryManager):
        """Memory context should not exceed budget + XML overhead."""
        for i in range(50):
            manager.store.add_memory(
                user_id="user1",
                content=f"这是第{i}条很长的测试记忆，包含各种信息细节。" * 3,
                category="knowledge",
                importance=5.0,
                embedding=np.random.randn(512).astype(np.float32),
            )
        result = manager.query("测试", "user1")
        # Budget is 2000 chars content + XML tags overhead
        assert len(result) < 2500


class TestSave:
    """Memory save (write pipeline) tests with mocked LLM."""

    def _mock_llm_response(self, manager: MemoryManager, extraction: dict):
        """Patch the extraction call to return a fixed result."""
        manager._call_llm_extract = MagicMock(return_value=extraction)

    def test_save_extracts_and_stores(self, manager: MemoryManager):
        self._mock_llm_response(manager, {
            "memories": [
                {"content": "Allen 喜欢拿铁", "category": "preference",
                 "importance": 8, "tags": ["咖啡"], "time_ref": None},
            ],
            "profile_update": {"identity": {"name": "Allen"}},
            "episode_summary": "聊了咖啡偏好",
            "mood": "neutral",
            "topics": ["咖啡"],
        })

        messages = [
            {"role": "user", "content": "我喜欢喝拿铁"},
            {"role": "assistant", "content": "好的，记住了"},
        ]
        manager.save(messages, "user1", "session1")

        # Verify memory was stored
        memories = manager.store.get_active_memories("user1")
        assert len(memories) == 1
        assert "拿铁" in memories[0]["content"]

        # Verify profile was updated
        profile = manager.store.get_profile("user1")
        assert profile["identity"]["name"] == "Allen"

        # Verify episode was stored
        episodes = manager.store.get_recent_episodes("user1")
        assert len(episodes) == 1
        assert "咖啡" in episodes[0]["summary"]

    def test_save_dedup_add(self, manager: MemoryManager):
        """New memory with no similar existing → ADD."""
        self._mock_llm_response(manager, {
            "memories": [
                {"content": "Allen 喜欢跑步", "category": "preference",
                 "importance": 6, "tags": []},
            ],
            "episode_summary": "聊了运动",
        })
        manager.save([{"role": "user", "content": "我喜欢跑步"}], "user1", "s1")
        assert manager.store.count_active("user1") == 1

    def test_save_empty_conversation(self, manager: MemoryManager):
        """Empty messages should not crash."""
        self._mock_llm_response(manager, None)
        manager.save([], "user1", "s1")
        assert manager.store.count_active("user1") == 0

    def test_save_llm_failure_graceful(self, manager: MemoryManager):
        """LLM failure should not crash."""
        manager._call_llm_extract = MagicMock(side_effect=Exception("API down"))
        manager.save(
            [{"role": "user", "content": "test"}], "user1", "s1",
        )
        assert manager.store.count_active("user1") == 0

    def test_save_no_memories_extracted(self, manager: MemoryManager):
        """Conversation with nothing worth remembering."""
        self._mock_llm_response(manager, {
            "memories": [],
            "episode_summary": "打了个招呼",
        })
        manager.save(
            [{"role": "user", "content": "你好"}], "user1", "s1",
        )
        assert manager.store.count_active("user1") == 0
        episodes = manager.store.get_recent_episodes("user1")
        assert len(episodes) == 1


class TestMessagesToText:
    """Conversation message formatting."""

    def test_simple_messages(self, manager: MemoryManager):
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好啊"},
        ]
        text = manager._messages_to_text(messages)
        assert "用户：你好" in text
        assert "小贾：你好啊" in text

    def test_content_blocks(self, manager: MemoryManager):
        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "好的"},
                {"type": "tool_use", "id": "t1", "name": "test", "input": {}},
            ]},
        ]
        text = manager._messages_to_text(messages)
        assert "好的" in text

    def test_empty_messages(self, manager: MemoryManager):
        assert manager._messages_to_text([]) == ""


class TestProfileToText:
    """Profile JSON to natural language conversion."""

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
        text = manager._profile_to_text({})
        assert text == ""


class TestSavePipeline:
    """Save pipeline edge-case tests."""

    def _mock_llm_response(self, manager: MemoryManager, extraction: dict):
        """Patch the extraction call to return a fixed result."""
        manager._call_llm_extract = MagicMock(return_value=extraction)

    def test_save_correction_supersedes(self, manager: MemoryManager):
        """When user corrects a memory, old one should be superseded."""
        emb = manager.embedder.encode("Allen 喜欢拿铁")
        manager.store.add_memory(
            user_id="user1", content="Allen 喜欢拿铁",
            category="preference", key="favorite_drink",
            importance=7.0, embedding=emb,
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
                "new_content": "喜欢美式，不喜欢拿铁",
                "reason": "用户纠正",
            }],
            "profile_update": None,
            "episode_summary": "Allen 纠正了饮品偏好",
            "mood": "neutral",
            "topics": ["偏好修正"],
        }
        self._mock_llm_response(manager, extraction)

        manager.save(
            [{"role": "user", "content": "不对，我喜欢美式不是拿铁"}],
            "user1", "session1",
        )

        active = manager.store.get_active_memories("user1")
        contents = [m["content"] for m in active]
        assert any("美式" in c for c in contents)
        assert not any(c == "Allen 喜欢拿铁" for c in contents)


class TestDedupDelete:
    """Dedup DELETE action and top_k expansion tests."""

    def _mock_llm_response(self, manager: MemoryManager, extraction: dict):
        """Patch the LLM call to return a fixed extraction result."""
        manager._call_openai_json = MagicMock(return_value=extraction)

    def test_dedup_delete_deactivates_old_and_adds_new(self, manager: MemoryManager):
        """DELETE action should deactivate the old memory and add the new one."""
        # Use a fixed embedding so old and new share the same vector (cosine=1.0),
        # which guarantees the LLM dedup path is reached.
        fixed_emb = np.random.RandomState(42).randn(512).astype(np.float32)
        fixed_emb /= np.linalg.norm(fixed_emb)

        old_id = manager.store.add_memory(
            user_id="user1", content="Allen 喜欢拿铁",
            category="preference", importance=7.0, embedding=fixed_emb.copy(),
        )

        # Override embedder to always return the same vector
        manager.embedder.encode = lambda text: fixed_emb.copy()

        # Mock extraction (via _call_llm_extract) and dedup (via _call_openai_json)
        manager._call_llm_extract = MagicMock(return_value={
            "memories": [{
                "content": "Allen 不喜欢拿铁，改喝美式了",
                "category": "preference",
                "importance": 8,
                "tags": [],
            }],
            "corrections": [],
            "episode_summary": "Allen 改了饮品偏好",
            "mood": "neutral",
            "topics": [],
        })
        manager._call_openai_json = MagicMock(
            return_value={"action": "DELETE", "target_id": old_id},
        )

        manager.save(
            [{"role": "user", "content": "我不喜欢拿铁了，改喝美式"}],
            "user1", "session1",
        )

        active = manager.store.get_active_memories("user1")
        contents = [m["content"] for m in active]
        # Old memory should be deactivated
        assert "Allen 喜欢拿铁" not in contents
        # New memory should be added
        assert any("美式" in c for c in contents)

    def test_dedup_top_k_is_10(self, manager: MemoryManager):
        """find_similar should be called with top_k=10."""
        # Add an existing memory so embedding path is triggered
        emb = manager.embedder.encode("existing memory")
        manager.store.add_memory(
            user_id="user1", content="existing memory",
            category="fact", importance=5.0, embedding=emb,
        )

        # Mock extraction and dedup separately
        manager._call_llm_extract = MagicMock(return_value={
            "memories": [{
                "content": "new memory content",
                "category": "fact",
                "importance": 5,
                "tags": [],
            }],
            "corrections": [],
            "episode_summary": "test",
            "mood": "neutral",
            "topics": [],
        })
        manager._call_openai_json = MagicMock(
            return_value={"action": "ADD", "target_id": None},
        )

        # Spy on find_similar
        original_find_similar = manager.retriever.find_similar
        find_similar_calls = []
        def spy_find_similar(*args, **kwargs):
            find_similar_calls.append(kwargs)
            return original_find_similar(*args, **kwargs)

        manager.retriever.find_similar = spy_find_similar

        manager.save(
            [{"role": "user", "content": "something"}],
            "user1", "session1",
        )

        assert len(find_similar_calls) >= 1
        assert find_similar_calls[0].get("top_k") == 10


class TestMaintain:
    """Weekly maintenance — duplicate merging."""

    def test_maintain_empty(self, manager: MemoryManager):
        """No memories → no-op."""
        result = manager.maintain("user1")
        assert result == {"merged": 0, "checked": 0, "skipped": 0}

    def test_maintain_no_duplicates(self, manager: MemoryManager):
        """Unrelated memories → nothing to merge."""
        emb1 = manager.embedder.encode("Allen likes coffee")
        emb2 = manager.embedder.encode("something completely different xyz")
        manager.store.add_memory("user1", "Allen likes coffee", "preference", embedding=emb1)
        manager.store.add_memory("user1", "something completely different xyz", "event", embedding=emb2)

        result = manager.maintain("user1")
        assert result["merged"] == 0
        assert manager.store.count_active("user1") == 2

    def test_maintain_merges_duplicates(self, manager: MemoryManager):
        """Two very similar same-category memories → LLM decides to merge."""
        emb = manager.embedder.encode("test content")
        id1 = manager.store.add_memory(
            "user1", "Allen 喜欢咖啡", "preference",
            embedding=emb.copy(),
        )
        id2 = manager.store.add_memory(
            "user1", "Allen 最爱的饮品是拿铁", "preference",
            embedding=emb.copy(),  # identical embedding = cosine 1.0
        )

        # Mock LLM to return MERGE, keeping the more informative one
        manager._call_openai_json = MagicMock(
            return_value={"action": "MERGE", "keep_id": id2},
        )

        result = manager.maintain("user1")
        assert result["merged"] == 1
        assert manager.store.count_active("user1") == 1

        # The kept one should be the more informative
        active = manager.store.get_active_memories("user1")
        assert active[0]["id"] == id2

    def test_maintain_keeps_both_when_llm_says_so(self, manager: MemoryManager):
        """LLM says KEEP_BOTH → both stay active."""
        emb = manager.embedder.encode("test")
        manager.store.add_memory("user1", "likes Italian food", "preference", embedding=emb.copy())
        manager.store.add_memory("user1", "likes Italian movies", "preference", embedding=emb.copy())

        manager._call_openai_json = MagicMock(
            return_value={"action": "KEEP_BOTH", "keep_id": None},
        )

        result = manager.maintain("user1")
        assert result["merged"] == 0
        assert result["skipped"] == 1
        assert manager.store.count_active("user1") == 2

    def test_maintain_skips_cross_category(self, manager: MemoryManager):
        """Similar memories in different categories → not checked."""
        emb = manager.embedder.encode("test")
        manager.store.add_memory("user1", "fact about coffee", "fact", embedding=emb.copy())
        manager.store.add_memory("user1", "preference about coffee", "preference", embedding=emb.copy())

        # Should not even call LLM — different categories
        manager._call_openai_json = MagicMock(return_value={"action": "MERGE", "keep_id": "x"})

        result = manager.maintain("user1")
        assert result["checked"] == 0
        assert manager._call_openai_json.call_count == 0

    def test_maintain_all(self, manager: MemoryManager):
        """maintain_all runs for all users."""
        emb = manager.embedder.encode("test")
        manager.store.add_memory("user1", "mem1", "fact", embedding=emb)
        manager.store.add_memory("user2", "mem2", "fact", embedding=emb)

        results = manager.maintain_all()
        assert "user1" in results
        assert "user2" in results


class TestPostprocessExtraction:
    """Tests for _postprocess_extraction validation and fix logic."""

    def test_key_missing_gets_derived(self, manager: MemoryManager):
        """identity/preference/relationship/knowledge without key gets a derived key."""
        for cat in ("identity", "preference", "relationship", "knowledge"):
            memories = [{"content": "Allen 住温哥华", "category": cat, "importance": 5}]
            result = manager._postprocess_extraction(memories)
            assert result[0]["key"], f"key should be derived for category={cat}"
            assert len(result[0]["key"]) == 8

    def test_expires_backfilled_from_time_ref(self, manager: MemoryManager):
        """event with time_ref but no expires -> expires = time_ref + 1 day."""
        memories = [{
            "content": "Allen 要去面试",
            "category": "event",
            "importance": 7,
            "time_ref": "2026-04-07",
        }]
        result = manager._postprocess_extraction(memories)
        assert result[0]["expires"] == "2026-04-08"

    def test_importance_clamped(self, manager: MemoryManager):
        """importance outside [1,10] gets clamped."""
        memories = [
            {"content": "fact A", "category": "event", "importance": 15},
            {"content": "fact B", "category": "event", "importance": -3},
        ]
        result = manager._postprocess_extraction(memories)
        assert result[0]["importance"] == 10
        assert result[1]["importance"] == 1

    def test_identity_importance_minimum_7(self, manager: MemoryManager):
        """identity importance < 7 gets bumped to 7."""
        memories = [{"content": "Allen 叫 Allen", "category": "identity", "importance": 3}]
        result = manager._postprocess_extraction(memories)
        assert result[0]["importance"] == 7

    def test_event_without_time_ref_no_expires_added(self, manager: MemoryManager):
        """event without time_ref -> expires should NOT be back-filled."""
        memories = [{"content": "Allen 提到了面试", "category": "event", "importance": 5}]
        result = manager._postprocess_extraction(memories)
        assert result[0].get("expires") is None

    def test_event_key_not_derived(self, manager: MemoryManager):
        """event/task categories should NOT get a derived key."""
        memories = [{"content": "Allen 要面试", "category": "event", "importance": 5}]
        result = manager._postprocess_extraction(memories)
        assert result[0].get("key") is None

    def test_task_expires_backfilled(self, manager: MemoryManager):
        """task with time_ref also gets expires back-filled."""
        memories = [{
            "content": "Allen 周一交报告",
            "category": "task",
            "importance": 6,
            "time_ref": "2026-04-07",
        }]
        result = manager._postprocess_extraction(memories)
        assert result[0]["expires"] == "2026-04-08"


class TestExtractFallback:
    """Test that function calling falls back to JSON mode on error."""

    def test_fc_failure_falls_back_to_json(self, manager: MemoryManager):
        """When FC request raises, should fall back to _call_llm_extract_json."""
        expected = {
            "memories": [{"content": "test", "category": "fact", "importance": 5}],
            "corrections": [],
            "episode_summary": "test",
            "mood": "neutral",
            "topics": [],
        }
        # Mock the JSON fallback to return a known value
        manager._call_llm_extract_json = MagicMock(return_value=expected)

        # Make requests.post raise so FC path fails
        with patch("memory.manager.requests.post", side_effect=Exception("FC error")):
            result = manager._call_llm_extract("对话", None, [], "用户")

        assert result == expected
        manager._call_llm_extract_json.assert_called_once()

    def test_fc_success_does_not_fallback(self, manager: MemoryManager):
        """When FC succeeds, should NOT call _call_llm_extract_json."""
        fc_response = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "arguments": json.dumps({
                                "memories": [],
                                "corrections": [],
                                "episode_summary": "ok",
                                "mood": "neutral",
                                "topics": [],
                            }),
                        },
                    }],
                },
            }],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = fc_response
        mock_resp.raise_for_status = MagicMock()

        manager._call_llm_extract_json = MagicMock()

        with patch("memory.manager.requests.post", return_value=mock_resp):
            result = manager._call_llm_extract("对话", None, [], "用户")

        assert result is not None
        assert result["episode_summary"] == "ok"
        manager._call_llm_extract_json.assert_not_called()
