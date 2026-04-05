"""Multi-signal memory retrieval — cosine + recency + importance + access."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np

from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

# Category-dependent half-life (days) for importance decay.
# Based on cognitive science: semantic memory (facts) resists forgetting,
# episodic memory (events) decays faster.
_DECAY_HALF_LIFE: dict[str, float] = {
    "identity": 365.0,
    "knowledge": 365.0,
    "relationship": 180.0,
    "preference": 180.0,
    "event": 30.0,
    "task": 14.0,
}


def _days_since(iso_timestamp: str | None) -> float:
    """Return days elapsed since an ISO timestamp (0 if None or invalid)."""
    if not iso_timestamp:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return max((datetime.now() - dt).total_seconds() / 86400, 0.0)
    except (ValueError, TypeError):
        return 0.0


class MemoryRetriever:
    """Retrieve the most relevant memories using multi-signal scoring.

    Scoring formula (unit-weighted to [0, 1] per signal):
        score = 0.40 * cosine_similarity
              + 0.25 * recency_score
              + 0.20 * importance_score
              + 0.15 * access_frequency_score

    Args:
        store: The MemoryStore to query.
    """

    W_COSINE = 0.40
    W_RECENCY = 0.25
    W_IMPORTANCE = 0.20
    W_ACCESS = 0.15

    # Cold-start weights: when all access_count == 0, cosine dominates
    W_COLD_COSINE = 0.60
    W_COLD_RECENCY = 0.10
    W_COLD_IMPORTANCE = 0.25
    W_COLD_ACCESS = 0.05

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def retrieve(
        self,
        query_embedding: np.ndarray,
        user_id: str,
        top_k: int = 5,
        exclude_ids: set[str] | None = None,
        touch: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the top-k most relevant active memories for a query.

        Args:
            touch: If True (default), update access_count/last_accessed on
                returned memories. Set False for speculative lookups (e.g.
                DirectAnswer probing) that should not inflate access counts.

        Args:
            query_embedding: Unit-norm query vector.
            user_id: User whose memories to search.
            top_k: Maximum number of results.
            exclude_ids: Memory IDs to skip (e.g. already in Tier 1/2).

        Returns:
            List of memory dicts, scored and sorted by relevance.
        """
        exclude = exclude_ids or set()
        memories = self.store.get_active_memories(user_id)

        # Filter out excluded and embedding-less memories
        candidates = [
            m for m in memories
            if m["id"] not in exclude and m["embedding"] is not None
        ]

        if not candidates:
            return []

        # Build embedding matrix
        embeddings = np.stack([m["embedding"] for m in candidates])

        # 1. Cosine similarity (embeddings are already unit-norm)
        cos_scores = embeddings @ query_embedding

        # 2. Recency score: 1 / (1 + days_since_last_access)
        recency_scores = np.array([
            1.0 / (1.0 + _days_since(m["last_accessed"]))
            for m in candidates
        ])

        # 3. Importance score with category-dependent decay + access reinforcement
        #    decay = 1 / (1 + staleness / half_life)
        #    reinforcement = 1 + 0.05 * min(access_count, 20), capped at 2x
        staleness_days = np.array([
            _days_since(m["last_accessed"]) for m in candidates
        ])
        half_lives = np.array([
            _DECAY_HALF_LIFE.get(m.get("category", ""), 90.0) for m in candidates
        ])
        decay = 1.0 / (1.0 + staleness_days / half_lives)
        reinforcement = np.minimum(
            1.0 + 0.05 * np.array([min(m["access_count"], 20) for m in candidates]),
            2.0,
        )
        importance_scores = np.array([
            m["importance"] / 10.0 for m in candidates
        ]) * decay * reinforcement

        # 4. Access frequency score: capped and normalized
        access_scores = np.array([
            min(m["access_count"], 10) / 10.0 for m in candidates
        ])

        # 5. Expiry penalty: halve score for expired memories
        today = datetime.now().strftime("%Y-%m-%d")
        expiry_factor = np.array([
            0.5 if (m.get("expires") and m["expires"] < today) else 1.0
            for m in candidates
        ])

        # Cold-start detection: if all access counts are 0, boost cosine weight
        all_cold = bool(np.all(access_scores == 0))
        if all_cold:
            # Cold start: cosine dominates, importance secondary
            scores = (
                self.W_COLD_COSINE * cos_scores
                + self.W_COLD_RECENCY * recency_scores
                + self.W_COLD_IMPORTANCE * importance_scores
                + self.W_COLD_ACCESS * access_scores
            ) * expiry_factor
            LOGGER.debug("Cold-start weights applied (cosine=0.60)")
        else:
            # Normal mode
            scores = (
                self.W_COSINE * cos_scores
                + self.W_RECENCY * recency_scores
                + self.W_IMPORTANCE * importance_scores
                + self.W_ACCESS * access_scores
            ) * expiry_factor

        # Sort descending, take top_k
        top_indices = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in top_indices:
            mem = candidates[idx]
            mem["_score"] = float(scores[idx])
            results.append(mem)

        # Batch-touch retrieved memories (skip for speculative lookups)
        if touch:
            self.store.touch_many([m["id"] for m in results])

        LOGGER.info(
            "Retrieved %d memories for user %s (from %d candidates)",
            len(results), user_id, len(candidates),
        )
        return results

    def find_similar(
        self,
        embedding: np.ndarray,
        user_id: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Find the most similar existing memories by pure cosine similarity.

        Used during save pipeline for dedup. Does NOT touch access counts.

        Returns:
            List of memory dicts with an added ``_score`` field.
        """
        memories = self.store.get_active_memories(user_id)
        candidates = [m for m in memories if m["embedding"] is not None]

        if not candidates:
            return []

        embeddings = np.stack([m["embedding"] for m in candidates])
        scores = embeddings @ embedding

        top_indices = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in top_indices:
            mem = candidates[idx]
            mem["_score"] = float(scores[idx])
            results.append(mem)

        return results
