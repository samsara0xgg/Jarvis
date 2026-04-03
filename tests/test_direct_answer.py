"""Tests for memory.direct_answer — Level 1 memory-based direct answers."""
from __future__ import annotations
from unittest.mock import MagicMock
import numpy as np
import pytest
from memory.direct_answer import DirectAnswerer


@pytest.fixture()
def answerer(tmp_path):
    from memory.store import MemoryStore
    store = MemoryStore(str(tmp_path / "test.db"))

    def mock_encode(text):
        rng = np.random.RandomState(hash(text) % 2**31)
        v = rng.randn(512).astype(np.float32)
        v /= np.linalg.norm(v)
        return v

    embedder = MagicMock()
    embedder.encode = mock_encode
    return DirectAnswerer(store, embedder)


class TestDirectAnswerer:
    def test_no_memories_returns_none(self, answerer: DirectAnswerer):
        result = answerer.try_answer("我喜欢喝什么", "user1")
        assert result is None

    def test_low_similarity_returns_none(self, answerer: DirectAnswerer):
        """Unrelated memory should not trigger direct answer."""
        emb = answerer._embedder.encode("Allen 住在温哥华")
        answerer._store.add_memory(
            user_id="user1", content="Allen 住在温哥华",
            category="identity", key="location",
            importance=8.0, embedding=emb,
        )
        result = answerer.try_answer("今天天气怎么样", "user1")
        assert result is None

    def test_high_similarity_preference_returns_answer(self, answerer: DirectAnswerer):
        """High-similarity preference query should return direct answer."""
        content = "Allen 喜欢喝拿铁"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="preference", key="favorite_drink",
            importance=8.0, embedding=emb,
        )
        # Same text → same mock vector → cosine = 1.0
        result = answerer.try_answer(content, "user1")
        assert result is not None
        assert "拿铁" in result

    def test_wrong_category_returns_none(self, answerer: DirectAnswerer):
        """Event category should not trigger direct answer."""
        content = "Allen 明天要出差"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="event",
            importance=7.0, embedding=emb,
        )
        result = answerer.try_answer(content, "user1")
        assert result is None

    def test_only_answerable_categories_queried(self, answerer: DirectAnswerer):
        """get_memories_by_categories is called with _ANSWERABLE_CATEGORIES, not all memories."""
        from unittest.mock import patch, MagicMock
        content = "Allen 喜欢绿茶"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user2", content=content,
            category="preference", key="drink",
            importance=8.0, embedding=emb,
        )
        # Also add an event that should NOT surface
        answerer._store.add_memory(
            user_id="user2", content="Allen 明天开会",
            category="event", importance=9.0,
        )
        with patch.object(
            answerer._store, "get_memories_by_categories",
            wraps=answerer._store.get_memories_by_categories,
        ) as mock_method:
            answerer.try_answer(content, "user2")
            mock_method.assert_called_once()
            called_categories = mock_method.call_args[0][1]
            assert "preference" in called_categories
            assert "event" not in called_categories
