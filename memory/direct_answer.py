"""Level 1 direct answer engine — answer from memory without LLM.

Only triggers for high-confidence factual queries (preference, identity,
knowledge) with cosine similarity > 0.85.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.85
_ANSWERABLE_CATEGORIES = {"preference", "identity", "knowledge"}

_ANSWER_TEMPLATES = {
    "preference": "你跟我说过，{content}",
    "identity": "我记得，{content}",
    "knowledge": "你之前告诉过我，{content}",
}


class DirectAnswerer:
    """Try to answer a query directly from memory, without LLM.

    Args:
        store: The MemoryStore to query.
        embedder: The Embedder for encoding queries.
    """

    def __init__(self, store: MemoryStore, embedder: Any) -> None:
        self._store = store
        self._embedder = embedder

    def try_answer(self, query: str, user_id: str) -> str | None:
        """Attempt to answer a query using stored memories.

        Returns:
            A natural language answer string, or None if no confident match.
        """
        candidates = [
            m for m in self._store.get_memories_by_categories(user_id, _ANSWERABLE_CATEGORIES)
            if m.get("embedding") is not None
        ]
        if not candidates:
            return None

        query_emb = self._embedder.encode(query)
        embeddings = np.stack([m["embedding"] for m in candidates])
        scores = embeddings @ query_emb

        best_idx = int(np.argmax(scores))
        best_score = float(scores[best_idx])

        if best_score < _SIMILARITY_THRESHOLD:
            return None

        best = candidates[best_idx]
        category = best.get("category", "knowledge")
        content = best["content"]
        template = _ANSWER_TEMPLATES.get(category, "我记得，{content}")

        self._store.touch_memory(best["id"])

        LOGGER.info(
            "Level 1 direct answer: score=%.3f category=%s content=%s",
            best_score, category, content[:60],
        )
        return template.format(content=content)
