"""Level 1 direct answer engine — answer from memory without LLM.

Uses multi-signal scoring (cosine + recency + importance + access) via
MemoryRetriever for higher hit rate than pure cosine matching.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

# Multi-signal combined score threshold (retriever score range ~0.2-0.9)
_SIMILARITY_THRESHOLD = 0.35
# Minimum raw cosine — prevents recency/importance from dominating
_MIN_COSINE = 0.55
# Margin between top-1 and top-2 multi-signal scores
_MARGIN_THRESHOLD = 0.05
_ANSWERABLE_CATEGORIES = {"preference", "identity", "knowledge", "relationship"}

_ANSWER_TEMPLATES = {
    "preference": "你跟我说过，{content}",
    "identity": "我记得，{content}",
    "knowledge": "你之前告诉过我，{content}",
    "relationship": "我记得，{content}",
}


class DirectAnswerer:
    """Try to answer a query directly from memory, without LLM.

    Uses MemoryRetriever's multi-signal scoring (cosine + recency +
    importance + access) instead of pure cosine for better hit rate.

    Args:
        store: The MemoryStore to query.
        embedder: The Embedder for encoding queries.
    """

    def __init__(self, store: MemoryStore, embedder: Any) -> None:
        self._store = store
        self._embedder = embedder
        from memory.retriever import MemoryRetriever
        self._retriever = MemoryRetriever(store)

    @staticmethod
    def _is_question(text: str) -> bool:
        """Check if text looks like a question (not a statement)."""
        text = text.strip().rstrip("。.！!")
        # Explicit question markers
        if text.endswith(("？", "?", "吗", "呢", "吧", "啊")):
            return True
        # Common question patterns
        question_words = (
            "什么", "哪", "几", "多少", "怎么", "为什么", "谁",
            "是不是", "有没有", "能不能", "可不可以",
        )
        if any(w in text for w in question_words):
            return True
        # Imperative queries (not commands): "告诉我X", "X来着", "说一下X"
        query_patterns = ("告诉我", "来着", "说一下", "想知道", "记得我的")
        return any(w in text for w in query_patterns)

    def try_answer(self, query: str, user_id: str) -> str | None:
        """Attempt to answer a query using stored memories.

        Returns:
            A natural language answer string, or None if no confident match.
        """
        # Gate 0: only answer questions, not statements
        if not self._is_question(query):
            return None

        # Quick check: any answerable memories at all?
        candidates = [
            m for m in self._store.get_memories_by_categories(user_id, _ANSWERABLE_CATEGORIES)
            if m.get("embedding") is not None
        ]
        if not candidates:
            return None

        # Use retriever for multi-signal scoring — touch=False to avoid
        # inflating access_count for memories that DA ultimately rejects
        query_emb = self._embedder.encode(query)
        results = self._retriever.retrieve(query_emb, user_id, top_k=5, touch=False)

        # Filter to answerable categories only
        answerable = [
            r for r in results
            if r.get("category") in _ANSWERABLE_CATEGORIES
        ]
        if not answerable:
            return None

        best = answerable[0]
        best_score = best["_score"]

        # Gate 1: multi-signal combined score threshold
        if best_score < _SIMILARITY_THRESHOLD:
            LOGGER.info("Level 1 skipped: score too low (%.3f)", best_score)
            return None

        # Gate 2: raw cosine safety net
        best_emb = best.get("embedding")
        if best_emb is not None:
            raw_cosine = float(query_emb @ best_emb)
            if raw_cosine < _MIN_COSINE:
                LOGGER.info(
                    "Level 1 skipped: raw cosine too low (%.3f)", raw_cosine,
                )
                return None

        # Gate 3: margin between top-1 and top-2
        if len(answerable) >= 2:
            margin = best_score - answerable[1]["_score"]
            if margin < _MARGIN_THRESHOLD:
                LOGGER.info(
                    "Level 1 skipped: margin too small (%.3f)", margin,
                )
                return None

        category = best.get("category", "knowledge")
        # Clean up content: replace sentence-initial "用户" with "你"
        # Only replace when "用户" is a standalone subject (followed by space
        # or at start), not part of compound words like "用户界面"
        content = best["content"]
        if content.startswith("用户 "):
            content = "你" + content[3:]
        elif content.startswith("用户"):
            content = "你" + content[2:]
        template = _ANSWER_TEMPLATES.get(category, "我记得，{content}")

        # Touch only on successful answer (not during failed probes)
        self._store.touch_memory(best["id"])

        LOGGER.info(
            "Level 1 direct answer: score=%.3f category=%s content=%s",
            best_score, category, content[:60],
        )
        return template.format(content=content)
