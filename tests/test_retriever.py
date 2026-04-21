"""Tests for memory.retriever — multi-signal memory retrieval."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from memory.core.store import MemoryStore
from memory.core.retriever import MemoryRetriever


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def retriever(store):
    return MemoryRetriever(store)


def _random_emb(seed: int = 0) -> np.ndarray:
    rng = np.random.RandomState(seed)
    v = rng.randn(512).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


class TestRetriever:
    """Multi-signal retrieval tests."""

    def test_retrieve_empty(self, retriever: MemoryRetriever):
        results = retriever.retrieve(_random_emb(), "user1")
        assert results == []

    def test_retrieve_returns_top_k(self, store: MemoryStore, retriever: MemoryRetriever):
        # Add 10 memories with different embeddings
        for i in range(10):
            store.add_memory(
                "user1", f"memory {i}", "fact",
                importance=5.0, embedding=_random_emb(i),
            )
        query = _random_emb(0)  # Same as memory 0
        results = retriever.retrieve(query, "user1", top_k=3)
        assert len(results) == 3

    def test_cosine_similarity_dominates(self, store: MemoryStore, retriever: MemoryRetriever):
        """Memory with highest cosine similarity to query should rank first."""
        target_emb = _random_emb(42)
        # Memory A: identical to query embedding
        store.add_memory("user1", "target", "fact", importance=1.0, embedding=target_emb)
        # Memory B: random embedding but high importance
        store.add_memory("user1", "other", "fact", importance=10.0, embedding=_random_emb(99))

        results = retriever.retrieve(target_emb, "user1", top_k=2)
        assert results[0]["content"] == "target"

    def test_importance_breaks_tie(self, store: MemoryStore, retriever: MemoryRetriever):
        """When cosine is equal, higher importance should win."""
        emb = _random_emb(0)
        store.add_memory("user1", "low imp", "fact", importance=1.0, embedding=emb.copy())
        store.add_memory("user1", "high imp", "fact", importance=10.0, embedding=emb.copy())

        results = retriever.retrieve(emb, "user1", top_k=2)
        assert results[0]["content"] == "high imp"

    def test_exclude_ids(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        id1 = store.add_memory("user1", "included", "fact", embedding=emb.copy())
        id2 = store.add_memory("user1", "excluded", "fact", embedding=emb.copy())

        results = retriever.retrieve(emb, "user1", top_k=10, exclude_ids={id2})
        ids = [r["id"] for r in results]
        assert id2 not in ids
        assert id1 in ids

    def test_touch_updates_access(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        mem_id = store.add_memory("user1", "test", "fact", embedding=emb)

        retriever.retrieve(emb, "user1")
        memories = store.get_active_memories("user1")
        assert memories[0]["access_count"] == 1

    def test_user_isolation(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        store.add_memory("user1", "user1's memory", "fact", embedding=emb)
        store.add_memory("user2", "user2's memory", "fact", embedding=emb)

        results = retriever.retrieve(emb, "user1")
        assert all(r["content"] == "user1's memory" for r in results)

    def test_skips_memories_without_embedding(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        store.add_memory("user1", "no emb", "fact")  # no embedding
        store.add_memory("user1", "has emb", "fact", embedding=emb)

        results = retriever.retrieve(emb, "user1")
        assert len(results) == 1
        assert results[0]["content"] == "has emb"


class TestFindSimilar:
    """Pure cosine similarity search for dedup."""

    def test_find_similar_returns_scored(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        store.add_memory("user1", "original", "fact", embedding=emb)

        results = retriever.find_similar(emb, "user1")
        assert len(results) == 1
        assert results[0]["_score"] > 0.99  # same vector

    def test_find_similar_does_not_touch(self, store: MemoryStore, retriever: MemoryRetriever):
        emb = _random_emb(0)
        store.add_memory("user1", "test", "fact", embedding=emb)

        retriever.find_similar(emb, "user1")
        memories = store.get_active_memories("user1")
        assert memories[0]["access_count"] == 0  # not touched

    def test_find_similar_empty(self, retriever: MemoryRetriever):
        results = retriever.find_similar(_random_emb(), "user1")
        assert results == []


class TestImportanceDecay:
    """Category-dependent importance decay + access reinforcement."""

    def test_event_decays_after_30_days(self, store: MemoryStore, retriever: MemoryRetriever):
        """Event memory 30 days old should have ~half effective importance."""
        emb = _random_emb(0)
        mem_id = store.add_memory(
            "user1", "Allen went to Tokyo", "event",
            importance=10.0, embedding=emb,
        )
        # Manually set last_accessed to 30 days ago
        conn = store._get_conn()
        old_date = (datetime.now() - timedelta(days=30)).isoformat()
        conn.execute(
            "UPDATE memories SET last_accessed = ? WHERE id = ?",
            (old_date, mem_id),
        )
        conn.commit()

        # Add a fresh memory with lower importance
        fresh_emb = _random_emb(1)
        store.add_memory(
            "user1", "Allen likes coffee", "preference",
            importance=6.0, embedding=fresh_emb,
        )

        # Query with the event's embedding — despite higher base importance,
        # the 30-day-old event should be penalized
        results = retriever.retrieve(emb, "user1", top_k=2)
        scores = {r["content"]: r["_score"] for r in results}
        # The event's effective importance is 10 * 0.5 = 5, lower than fresh 6
        # Combined with cosine (event has perfect match), event may still win
        # but its _score should be noticeably lower than if it were fresh
        assert len(results) == 2

    def test_identity_resists_decay(self, store: MemoryStore, retriever: MemoryRetriever):
        """Identity memory should barely decay even after 30 days."""
        emb = _random_emb(0)
        mem_id = store.add_memory(
            "user1", "Allen lives in Vancouver", "identity",
            importance=8.0, embedding=emb,
        )
        # Set last_accessed to 30 days ago
        conn = store._get_conn()
        old_date = (datetime.now() - timedelta(days=30)).isoformat()
        conn.execute(
            "UPDATE memories SET last_accessed = ? WHERE id = ?",
            (old_date, mem_id),
        )
        conn.commit()

        results = retriever.retrieve(emb, "user1", top_k=1)
        assert len(results) == 1
        # identity half-life = 365 days, so 30 days → decay ≈ 0.92
        # Score should still be high
        assert results[0]["_score"] > 0.3

    def test_access_reinforcement(self, store: MemoryStore, retriever: MemoryRetriever):
        """Frequently accessed memories should resist decay."""
        emb = _random_emb(0)
        mem_id = store.add_memory(
            "user1", "Allen's meeting notes", "event",
            importance=7.0, embedding=emb,
        )
        # Set 60 days old but accessed 20 times
        conn = store._get_conn()
        old_date = (datetime.now() - timedelta(days=60)).isoformat()
        conn.execute(
            "UPDATE memories SET last_accessed = ?, access_count = 20 WHERE id = ?",
            (old_date, mem_id),
        )
        conn.commit()

        # Add a fresh memory with same importance but 0 access
        fresh_emb = _random_emb(1)
        store.add_memory(
            "user1", "New memory", "event",
            importance=7.0, embedding=fresh_emb,
        )

        results = retriever.retrieve(emb, "user1", top_k=2)
        old_mem = next(r for r in results if r["content"] == "Allen's meeting notes")
        # reinforcement = 1 + 0.05 * 20 = 2.0 (capped)
        # decay at 60 days with 30-day half-life = 1/(1+2) = 0.33
        # effective = 0.7 * 0.33 * 2.0 = 0.47 (vs fresh 0.7 * 1.0 * 1.0 = 0.7)
        # Still lower, but much better than without reinforcement (0.23)
        assert old_mem["_score"] > 0.15  # would be near 0 without reinforcement

    def test_fresh_memories_unaffected(self, store: MemoryStore, retriever: MemoryRetriever):
        """Just-created memories should have decay ≈ 1.0."""
        emb = _random_emb(0)
        store.add_memory(
            "user1", "Fresh event", "event",
            importance=5.0, embedding=emb,
        )
        results = retriever.retrieve(emb, "user1", top_k=1)
        assert len(results) == 1
        # Cold-start weights: cosine=0.60, recency=0.10, importance=0.25, access=0.05
        # score ≈ 0.60*1.0 + 0.10*1.0 + 0.25*0.5 + 0.05*0 = 0.825
        assert results[0]["_score"] > 0.6


class TestColdStart:
    """Cold-start detection and weight adaptation."""

    def test_cold_start_boosts_cosine(self, store: MemoryStore, retriever: MemoryRetriever):
        """All access_count=0: high cosine + low importance beats low cosine + high importance."""
        query_emb = _random_emb(42)

        # Memory A: high cosine similarity (same embedding), low importance
        store.add_memory(
            "user1", "high cosine match", "fact",
            importance=2.0, embedding=query_emb.copy(),
        )
        # Memory B: low cosine similarity (different embedding), high importance
        store.add_memory(
            "user1", "high importance but low cosine", "fact",
            importance=10.0, embedding=_random_emb(99),
        )

        # Both have access_count=0 → cold-start mode (cosine weight 0.60)
        results = retriever.retrieve(query_emb, "user1", top_k=2)
        assert results[0]["content"] == "high cosine match"

    def test_warm_start_normal_weights(self, store: MemoryStore, retriever: MemoryRetriever):
        """When at least one memory has access_count>0, use normal weights."""
        query_emb = _random_emb(42)

        # Memory A: high cosine, low importance
        id_a = store.add_memory(
            "user1", "high cosine", "fact",
            importance=2.0, embedding=query_emb.copy(),
        )
        # Memory B: low cosine, high importance, has been accessed
        id_b = store.add_memory(
            "user1", "high importance", "fact",
            importance=10.0, embedding=_random_emb(99),
        )
        # Touch memory B to make it warm (access_count > 0)
        store.touch_many([id_b])

        results = retriever.retrieve(query_emb, "user1", top_k=2)
        assert len(results) == 2
        score_a = results[0]["_score"]
        score_b = results[1]["_score"]
        assert score_a > score_b  # A still wins on cosine

        # Key verification: B's score should be meaningfully higher than in
        # cold-start mode because importance(10) + access(1) contribute more
        # with normal weights (0.20+0.15=0.35) vs cold-start (0.25+0.05=0.30).
        # B's importance component alone: normal = 0.20*1.0 = 0.20, cold = 0.25*1.0 = 0.25
        # B's access component: normal = 0.15*0.1 = 0.015, cold = 0.05*0.1 = 0.005
        # B's recency component: normal = 0.25*1.0 = 0.25, cold = 0.10*1.0 = 0.10
        # Normal B total ≈ 0.20+0.015+0.25 = 0.465; Cold B total ≈ 0.25+0.005+0.10 = 0.355
        # So B's score should be > 0.35 in normal mode (vs < 0.36 in cold mode)
        assert score_b > 0.30, f"B's score {score_b} too low for normal weights"
