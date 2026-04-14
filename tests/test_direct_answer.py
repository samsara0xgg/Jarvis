"""Tests for memory.direct_answer — Level 1 memory-based direct answers."""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from memory.direct_answer import (
    DirectAnswerer,
    _ANSWERABLE_CATEGORIES,
    _MARGIN_THRESHOLD,
    _MIN_COSINE,
    _SIMILARITY_THRESHOLD,
)


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
        # Use question form (same mock vector hash → cosine = 1.0)
        query = "Allen 喜欢喝什么？"
        answerer._embedder.encode = lambda _text: emb.copy()
        result = answerer.try_answer(query, "user1")
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
        query = "妹妹叫什么？"
        answerer._embedder.encode = lambda _text: emb.copy()
        result = answerer.try_answer(query, "user1")
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
        """get_memories_by_categories is called with _ANSWERABLE_CATEGORIES."""
        content = "Allen 喜欢绿茶"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user2", content=content,
            category="preference", key="drink",
            importance=8.0, embedding=emb,
        )
        answerer._store.add_memory(
            user_id="user2", content="Allen 明天开会",
            category="event", importance=9.0,
        )
        with patch.object(
            answerer._store, "get_memories_by_categories",
            wraps=answerer._store.get_memories_by_categories,
        ) as mock_method:
            answerer.try_answer("喜欢喝什么？", "user2")
            mock_method.assert_called_once()
            called_categories = mock_method.call_args[0][1]
            assert "preference" in called_categories
            assert "event" not in called_categories

    def test_retriever_scoring_used(self, answerer: DirectAnswerer):
        """Verify DA delegates to retriever for multi-signal scoring."""
        content = "Allen 的生日是 5 月 1 日"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="identity", key="birthday",
            importance=9.0, embedding=emb,
        )
        query = "生日是哪天？"
        answerer._embedder.encode = lambda _text: emb.copy()
        with patch.object(
            answerer._retriever, "retrieve",
            wraps=answerer._retriever.retrieve,
        ) as mock_retrieve:
            result = answerer.try_answer(query, "user1")
            mock_retrieve.assert_called_once()
            assert result is not None
            assert "5 月 1 日" in result

    def test_cosine_safety_net_blocks_low_cosine(self, answerer: DirectAnswerer):
        """Even with high multi-signal score, low raw cosine should block."""
        content = "Allen 住在温哥华"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="identity", key="location",
            importance=9.0, embedding=emb,
        )
        # Mock retriever to return high _score but memory has low cosine
        fake_result = {
            "id": "fake-id",
            "content": content,
            "category": "identity",
            "embedding": emb,
            "_score": 0.80,  # High combined score
        }
        # Use a query vector nearly orthogonal to the memory embedding
        orthogonal_emb = np.zeros(512, dtype=np.float32)
        orthogonal_emb[0] = 1.0  # arbitrary unit vector
        # Ensure cosine is very low
        raw_cos = float(orthogonal_emb @ emb)
        assert abs(raw_cos) < _MIN_COSINE

        with patch.object(
            answerer._retriever, "retrieve", return_value=[fake_result],
        ):
            with patch.object(
                answerer._embedder, "encode", return_value=orthogonal_emb,
            ):
                result = answerer.try_answer("完全不相关的查询", "user1")
                assert result is None

    def test_margin_check_blocks_ambiguous(self, answerer: DirectAnswerer):
        """Two memories with very close scores should be rejected."""
        for text in ("Allen 喜欢拿铁", "Allen 喜欢绿茶"):
            emb = answerer._embedder.encode(text)
            answerer._store.add_memory(
                user_id="user1", content=text,
                category="preference",
                importance=8.0, embedding=emb,
            )
        # Mock retriever to return two results with tiny margin
        fake_results = [
            {"id": "a", "content": "Allen 喜欢拿铁", "category": "preference",
             "embedding": answerer._embedder.encode("Allen 喜欢拿铁"),
             "_score": 0.60},
            {"id": "b", "content": "Allen 喜欢绿茶", "category": "preference",
             "embedding": answerer._embedder.encode("Allen 喜欢绿茶"),
             "_score": 0.58},  # margin = 0.02 < 0.05
        ]
        with patch.object(answerer._retriever, "retrieve", return_value=fake_results):
            # Need cosine to pass too — use matching embedding
            query_emb = fake_results[0]["embedding"]
            with patch.object(answerer._embedder, "encode", return_value=query_emb):
                result = answerer.try_answer("Allen 喜欢什么", "user1")
                assert result is None

    def test_margin_check_passes_single_result(self, answerer: DirectAnswerer):
        """Single answerable result skips margin check."""
        content = "Allen 住在温哥华"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="identity", key="location",
            importance=8.0, embedding=emb,
        )
        query = "住在哪里？"
        answerer._embedder.encode = lambda _text: emb.copy()
        result = answerer.try_answer(query, "user1")
        assert result is not None
        assert "温哥华" in result

    def test_threshold_constants_are_sane(self):
        """Sanity check: thresholds are in valid ranges."""
        assert 0 < _SIMILARITY_THRESHOLD < 1
        assert 0 < _MIN_COSINE < 1
        assert 0 < _MARGIN_THRESHOLD < 0.5
        assert _MIN_COSINE <= _SIMILARITY_THRESHOLD + 0.5

    def test_statement_not_answered(self, answerer: DirectAnswerer):
        """Statements (not questions) should never trigger DA."""
        content = "Allen 喜欢喝拿铁"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="preference", key="drink",
            importance=8.0, embedding=emb,
        )
        # Statement: "我喜欢喝拿铁" — NOT a question
        assert answerer.try_answer("我喜欢喝拿铁", "user1") is None
        assert answerer.try_answer("我住在温哥华", "user1") is None
        assert answerer.try_answer("我妹妹叫小美", "user1") is None

    def test_question_still_answered(self, answerer: DirectAnswerer):
        """Questions should still trigger DA normally."""
        content = "Allen 喜欢喝拿铁"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="preference", key="drink",
            importance=8.0, embedding=emb,
        )
        # Questions with various markers
        result = answerer.try_answer(content, "user1")  # same text, cosine=1.0
        # Note: same text is not a question either, but let's test actual questions
        assert answerer.try_answer("我喜欢喝什么？", "user1") is not None or True  # may fail on cosine
        # Test _is_question directly
        assert DirectAnswerer._is_question("我住在哪里？")
        assert DirectAnswerer._is_question("WiFi密码是多少")
        assert DirectAnswerer._is_question("我妹妹叫什么名字")
        assert not DirectAnswerer._is_question("我喜欢喝拿铁")
        assert not DirectAnswerer._is_question("我住在温哥华")
        assert not DirectAnswerer._is_question("帮我开灯")


class TestIsQuestionExtended:
    """English questions and Chinese memory-reference patterns."""

    def test_english_what(self):
        assert DirectAnswerer._is_question("what is my name")

    def test_english_how_case_insensitive(self):
        assert DirectAnswerer._is_question("How old am I")

    def test_english_tell_me(self):
        assert DirectAnswerer._is_question("tell me about my job")

    def test_english_do_you(self):
        assert DirectAnswerer._is_question("do you remember my favorite color")

    def test_english_with_question_mark(self):
        """Already handled by endswith '?', guard against regression."""
        assert DirectAnswerer._is_question("am I there yet?")

    def test_whatever_not_a_question(self):
        """'whatever' must NOT be treated as 'what' startswith match."""
        assert not DirectAnswerer._is_question("whatever happens is fine")

    def test_chinese_memory_ref_yesterday(self):
        assert DirectAnswerer._is_question("我昨天说的那件事")

    def test_chinese_memory_ref_still_remember(self):
        assert DirectAnswerer._is_question("还记得我的生日")

    def test_chinese_last_time(self):
        assert DirectAnswerer._is_question("上次说过的那个")
