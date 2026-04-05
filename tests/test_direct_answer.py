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

    def test_single_candidate_skips_margin_check(self, answerer: DirectAnswerer):
        """With only 1 candidate, margin check is not applied."""
        content = "Allen 的妹妹叫小美"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="identity", key="sister",
            importance=8.0, embedding=emb,
        )
        # Same text -> cosine 1.0, single candidate -> no margin check
        result = answerer.try_answer(content, "user1")
        assert result is not None
        assert "小美" in result

    def test_margin_too_small_returns_none(self, answerer: DirectAnswerer):
        """When top-1 and top-2 scores are too close, return None."""
        base = np.ones(512, dtype=np.float32)
        base /= np.linalg.norm(base)

        # Two nearly identical embeddings -> margin ~ 0
        emb1 = base.copy()
        emb2 = base.copy()
        emb2[0] += 0.001
        emb2 /= np.linalg.norm(emb2)

        answerer._store.add_memory(
            user_id="user_m", content="Allen 喜欢拿铁",
            category="preference", key="drink1",
            importance=8.0, embedding=emb1,
        )
        answerer._store.add_memory(
            user_id="user_m", content="Allen 喜欢绿茶",
            category="preference", key="drink2",
            importance=8.0, embedding=emb2,
        )

        # Query embedding = base -> very close scores for both
        answerer._embedder.encode = lambda _text: base.copy()
        result = answerer.try_answer("我喜欢喝什么", "user_m")
        assert result is None

    def test_margin_sufficient_returns_answer(self, answerer: DirectAnswerer):
        """When margin >= 0.08, return the answer normally."""
        v1 = np.zeros(512, dtype=np.float32)
        v1[0] = 1.0  # unit vector along dim 0

        v2 = np.zeros(512, dtype=np.float32)
        v2[1] = 1.0  # unit vector along dim 1 (orthogonal)

        answerer._store.add_memory(
            user_id="user_s", content="Allen 对花生过敏",
            category="knowledge", key="allergy",
            importance=9.0, embedding=v1,
        )
        answerer._store.add_memory(
            user_id="user_s", content="Allen 喜欢跑步",
            category="preference", key="sport",
            importance=7.0, embedding=v2,
        )

        # Query embedding = v1 -> score 1.0 for first, 0.0 for second -> margin 1.0
        answerer._embedder.encode = lambda _text: v1.copy()
        result = answerer.try_answer("我对什么过敏", "user_s")
        assert result is not None
        assert "花生" in result

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
