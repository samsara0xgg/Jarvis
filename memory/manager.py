"""MemoryManager — core memory interface for Jarvis.

Provides two public methods:
  - query(text, user_id) → formatted memory context for LLM prompt injection
  - save(messages, user_id, session_id) → extract and persist memories (async)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta

import numpy as np
import requests

from memory.embedder import Embedder
from memory.retriever import MemoryRetriever
from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

_EXTRACT_PROMPT_HEADER = """从以下对话中提取值得长期记住的**新**信息。只提取用户说的内容。
忽略打招呼、设备操作指令（开灯/关灯/几点了）、纯闲聊。

重要：下方"已有记忆"列表中的内容已经存储，不要重复提取。只提取对话中出现的、不在已有记忆中的新信息。

记忆粒度：
- event/task：输出自含的完整描述，包含时间和上下文
  例："用户 于2026年4月说下周一要去深圳参加技术峰会"
- 其他类型：输出原子事实
  例："用户 喜欢拿铁"

每条记忆必须自含（不依赖上下文就能理解），包含主语（用用户名，不用"我"或"他"）。

如果用户提到相对时间（如"下周一"），必须转为绝对日期填入 time_ref。今天是 {today}。

profile_update 规则：如果提取到了 identity/preference/relationship 类的新信息，
输出更新后的完整画像 JSON（结构：identity/preferences/routines/relationships/pending/status）。
否则输出 null。

如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。
如果没有纠正，corrections 为空数组。

如果对话中没有值得记住的内容，memories 数组留空。"""

_EXTRACT_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_memories",
        "description": "从对话中提取值得长期记住的信息",
        "parameters": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "自含的记忆描述，包含主语",
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "identity", "preference", "relationship",
                                    "event", "task", "knowledge",
                                ],
                            },
                            "key": {
                                "type": "string",
                                "description": (
                                    "语义标识，如 name/location/favorite_drink。"
                                    "identity/preference/relationship/knowledge 必填"
                                ),
                            },
                            "importance": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "time_ref": {
                                "type": "string",
                                "description": "绝对日期如 2026-04-07，相对时间必须转为绝对日期",
                            },
                            "expires": {
                                "type": "string",
                                "description": "过期日期。event/task 必填，为 time_ref+1天",
                            },
                        },
                        "required": ["content", "category", "importance"],
                    },
                },
                "corrections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_content": {"type": "string"},
                            "new_content": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["old_content", "new_content"],
                    },
                },
                "profile_update": {
                    "type": "object",
                    "description": "更新后的用户画像，无变化时为空对象",
                },
                "episode_summary": {
                    "type": "string",
                    "description": "一句话概括对话",
                },
                "mood": {
                    "type": "string",
                    "enum": [
                        "neutral", "happy", "sad", "angry",
                        "anxious", "excited", "tired",
                    ],
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "memories", "corrections", "episode_summary", "mood", "topics",
            ],
        },
    },
}

_DEDUP_PROMPT_HEADER = """判断新记忆与已有记忆的关系。

判断：
- ADD：全新信息，与已有记忆不重复
- UPDATE：已有记忆的更新版本（指明 target_id，保留信息量更大的版本）
- DELETE：新信息与旧记忆矛盾，旧记忆已过时（指明 target_id，将被删除）
- NONE：已存在完全相同的信息，无需操作

输出 JSON（严格 JSON，无注释）：
{"action": "ADD 或 UPDATE 或 DELETE 或 NONE", "target_id": "xxx 或 null"}"""

# Cosine thresholds for dedup — gateway to LLM dedup call.
# Calibrated via scripts/calibrate_dedup.py on 45 Chinese memory pairs:
#   duplicates min=0.78, related-different max=0.79, unrelated max=0.38
# 0.65 same-cat catches all true duplicates while filtering most unrelated pairs.
_DEDUP_THRESHOLD_SAME_CAT = 0.65
_DEDUP_THRESHOLD_CROSS_CAT = 0.7

_MERGE_PROMPT_HEADER = """以下两条记忆语义相似，判断是否应该合并。

- MERGE：是同一件事的不同表述，输出 keep_id（保留信息量更大的那条）
- KEEP_BOTH：虽然相似但确实是不同的事

输出 JSON（严格 JSON，无注释）：
{"action": "MERGE 或 KEEP_BOTH", "keep_id": "xxx 或 null"}"""

_MAX_MEMORY_CHARS = 2000  # ~800 tokens Chinese

# Maintenance: max pairs to check per run, max LLM calls per run
_MAINTAIN_MAX_PAIRS = 10
_MAINTAIN_COSINE_THRESHOLD = 0.8


class MemoryManager:
    """Manages Jarvis's long-term memory — query and save.

    Args:
        config: Parsed application configuration.
    """

    _RELATION_KEYWORDS = frozenset((
        "妹妹", "弟弟", "姐姐", "哥哥", "爸爸", "妈妈",
        "女朋友", "男朋友", "老婆", "老公", "女友", "男友",
        "儿子", "女儿", "朋友", "同事", "同学",
    ))

    def __init__(self, config: dict) -> None:
        mem_config = config.get("memory", {})
        db_path = mem_config.get("db_path", "data/memory/jarvis_memory.db")
        self.store = MemoryStore(db_path)
        self.embedder = Embedder()
        self.retriever = MemoryRetriever(self.store)
        self._full_inject_threshold = int(mem_config.get("full_inject_threshold", 100))

        # LLM config for extraction / dedup calls
        llm_config = config.get("llm", {})
        self._llm_api_key = llm_config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self._llm_model = llm_config.get("model", "gpt-4o-mini")
        self._llm_base_url = llm_config.get("base_url") or "https://api.openai.com/v1"

        self.logger = LOGGER

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, text: str, user_id: str) -> str:
        """Build the ``<memory>`` block to inject into the LLM system prompt.

        Args:
            text: The user's current utterance (for Tier 3 retrieval).
            user_id: Authenticated user ID.

        Returns:
            Formatted ``<memory>...</memory>`` string, or empty string
            if no memories exist yet.
        """
        profile = self.store.get_profile(user_id)
        episodes = self.store.get_recent_episodes(user_id, days=3)
        active_count = self.store.count_active(user_id)

        if active_count == 0 and not profile and not episodes:
            return ""

        # Gradual retrieval: always sorted by relevance
        if active_count <= 20:
            # Very few memories: inject all, sorted by importance (desc)
            relevant = self.store.get_active_memories(user_id)
            relevant.sort(key=lambda m: m.get("importance", 5), reverse=True)
        else:
            # >20: embedding retrieval with dynamic top_k
            query_emb = self.embedder.encode(text)
            # "- " prefix (2) + average Chinese memory content (~20 chars) + newline (1)
            avg_mem_len = 23
            # Reserve ~600 chars for profile + episodes; rest for memories
            budget_for_memories = max(200, _MAX_MEMORY_CHARS - 600)
            top_k = min(active_count, max(5, budget_for_memories // avg_mem_len))
            relevant = self.retriever.retrieve(
                query_emb, user_id, top_k=top_k,
            )

        return self._format_memory_context(profile, episodes, relevant, user_id)

    def maintain(self, user_id: str) -> dict:
        """Run periodic maintenance: sweep expired, compress episodes, merge duplicates.

        Designed to run in a background scheduler (e.g. weekly).

        Returns:
            {"merged": int, "checked": int, "skipped": int,
             "swept": int, "backfilled": int}
        """
        swept = self.store.sweep_expired()
        backfilled = self.store.backfill_expires()
        if swept or backfilled:
            self.logger.info("Maintenance: swept %d expired, backfilled %d expires", swept, backfilled)

        # Compress old episodes into weekly digests
        self._compress_episodes(user_id)

        base_result = {"merged": 0, "checked": 0, "skipped": 0,
                       "swept": swept, "backfilled": backfilled}

        ids, contents, categories, embeddings = self.store.get_embedding_index(user_id)
        if embeddings is None or len(ids) < 2:
            return base_result

        # All-pairs cosine similarity (embeddings are unit-norm)
        sim_matrix = embeddings @ embeddings.T

        # Find pairs above threshold, same category only
        pairs: list[tuple[int, int, float]] = []
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                if categories[i] != categories[j]:
                    continue
                score = float(sim_matrix[i, j])
                if score >= _MAINTAIN_COSINE_THRESHOLD:
                    pairs.append((i, j, score))

        # Sort by similarity descending, cap at max pairs
        pairs.sort(key=lambda x: x[2], reverse=True)
        pairs = pairs[:_MAINTAIN_MAX_PAIRS]

        merged = 0
        skipped = 0
        for i, j, score in pairs:
            decision = self._call_llm_merge(
                ids[i], contents[i], ids[j], contents[j],
            )
            if decision.get("action") == "MERGE":
                keep_id = decision.get("keep_id")
                remove_id = ids[j] if keep_id == ids[i] else ids[i]
                if keep_id in (ids[i], ids[j]):
                    self.store.supersede_memory(remove_id, keep_id)
                    merged += 1
                else:
                    skipped += 1
            else:
                skipped += 1

        self.logger.info(
            "Maintenance for %s: checked=%d, merged=%d, skipped=%d",
            user_id, len(pairs), merged, skipped,
        )
        return {"merged": merged, "checked": len(pairs), "skipped": skipped,
                "swept": swept, "backfilled": backfilled}

    def maintain_all(self) -> dict:
        """Run maintenance for all users. Returns per-user stats."""
        user_ids = self.store.get_all_user_ids()
        results = {}
        for uid in user_ids:
            try:
                results[uid] = self.maintain(uid)
            except Exception:
                self.logger.exception("Maintenance failed for user %s", uid)
                results[uid] = {"error": True}
        return results

    def _compress_episodes(self, user_id: str) -> None:
        """Compress episodes older than 7 days into weekly digests.

        Groups old episodes by ISO week and creates a simple concatenated
        digest per week. Does not use LLM (maintenance should not depend
        on API keys).
        """
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        rows = self.store.get_episodes_before(user_id, cutoff)

        if not rows:
            return

        # Group by ISO week
        weeks: dict[str, list[dict]] = {}
        for row in rows:
            dt = datetime.strptime(row["date"], "%Y-%m-%d")
            week_key = dt.strftime("%Y-W%W")
            weeks.setdefault(week_key, []).append(
                {"date": row["date"], "summary": row["summary"]}
            )

        created = 0
        for week_key, entries in weeks.items():
            if len(entries) <= 1:
                continue  # single episode — no need to compress

            # Check if digest already exists for this week
            dates = [e["date"] for e in entries]
            period_start, period_end = min(dates), max(dates)
            if self.store.digest_exists(user_id, period_start, period_end):
                continue

            # Build digest: simple concatenation (no LLM dependency)
            summaries = [e["summary"] for e in entries]
            digest = "；".join(summaries[:5])
            if len(summaries) > 5:
                digest += f"（共 {len(summaries)} 条对话）"

            self.store.add_digest(user_id, period_start, period_end, digest)
            created += 1

        if created:
            self.logger.info(
                "Compressed %d weeks of episodes for user %s", created, user_id,
            )

    def save(
        self,
        messages: list[dict],
        user_id: str,
        session_id: str,
        detected_emotion: str = "",
    ) -> None:
        """Extract memories from a completed conversation and persist.

        Designed to run asynchronously (in a background thread).

        Args:
            messages: Full conversation message list.
            user_id: Authenticated user ID.
            session_id: Conversation session identifier.
            detected_emotion: ASR-detected emotion label (e.g. "happy", "sad").
                Overrides LLM-guessed mood when present.
        """
        try:
            self._save_inner(messages, user_id, session_id, detected_emotion)
        except Exception:
            self.logger.exception("Memory save failed for user %s", user_id)

    # ------------------------------------------------------------------
    # Save pipeline
    # ------------------------------------------------------------------

    def _save_inner(
        self,
        messages: list[dict],
        user_id: str,
        session_id: str,
        detected_emotion: str = "",
    ) -> None:
        """Inner save logic — extract, dedup, store."""
        conversation_text = self._messages_to_text(messages)
        if not conversation_text.strip():
            return

        # Gather context for extraction prompt
        profile = self.store.get_profile(user_id)
        existing = self.store.get_memory_summaries(user_id)

        # Resolve user display name from profile
        user_name = "用户"
        if profile and isinstance(profile.get("identity"), dict):
            user_name = profile["identity"].get("name", "用户")

        # 1. LLM extraction
        extraction = self._call_llm_extract(
            conversation_text, profile, existing, user_name,
        )
        if not extraction:
            return

        # 1b. Process corrections — deactivate contradicted memories before adding new ones
        for correction in extraction.get("corrections", []):
            old_kw = correction.get("old_content", "")
            if old_kw:
                deactivated = self.store.deactivate_memory(user_id, old_kw)
                if deactivated:
                    self.logger.info("Memory corrected: deactivated '%s'", old_kw)

        # 1c. Post-process: validate and fix extracted memories
        memories = self._postprocess_extraction(extraction.get("memories", []))

        # 2. Process each extracted memory
        for mem in memories:
            content = mem.get("content", "").strip()
            if not content:
                continue

            category = mem.get("category", "fact")
            key = mem.get("key")  # None for events
            mem_embedding = self.embedder.encode(content)

            # --- Dedup: key-first (deterministic), then embedding (fuzzy) ---
            existing_by_key = None
            if key and category != "event":
                existing_by_key = self.store.find_by_key(user_id, category, key)

            if existing_by_key:
                # Same category+key — check if content actually changed
                old_content = existing_by_key.get("content", "")
                if old_content.strip() == content.strip():
                    # Identical content → skip (LLM re-extracted existing memory)
                    self.logger.debug("Skipping identical re-extraction: %s", content[:40])
                    continue
                # Content changed → UPDATE (supersede old with new)
                new_id = self._add_extracted_memory(
                    user_id, content, category, key, mem, mem_embedding,
                )
                self.store.supersede_memory(existing_by_key["id"], new_id)
            else:
                # No key match → fall back to embedding similarity
                similar = self.retriever.find_similar(mem_embedding, user_id, top_k=10)

                if similar:
                    top_score = similar[0]["_score"]
                    same_cat = similar[0].get("category") == category
                    threshold = _DEDUP_THRESHOLD_SAME_CAT if same_cat else _DEDUP_THRESHOLD_CROSS_CAT
                else:
                    top_score = 0.0
                    threshold = _DEDUP_THRESHOLD_CROSS_CAT

                if top_score < threshold:
                    # Clearly new → ADD
                    self._add_extracted_memory(
                        user_id, content, category, key, mem, mem_embedding,
                    )
                else:
                    # Possibly duplicate → LLM decides
                    decision = self._call_llm_dedup(content, similar[:5])
                    if decision.get("action") == "ADD":
                        self._add_extracted_memory(
                            user_id, content, category, key, mem, mem_embedding,
                        )
                    elif decision.get("action") == "UPDATE":
                        target_id = decision.get("target_id")
                        if target_id:
                            new_id = self._add_extracted_memory(
                                user_id, content, category, key, mem, mem_embedding,
                            )
                            self.store.supersede_memory(target_id, new_id)
                    elif decision.get("action") == "DELETE":
                        target_id = decision.get("target_id")
                        if target_id:
                            self.store.deactivate_memory_by_id(target_id)
                            self.logger.info("Memory conflict resolved: deactivated '%s'", target_id)
                        else:
                            self.logger.warning(
                                "DELETE without target_id for: %s", content[:60],
                            )
                        # 无论是否成功删除旧记忆，都添加新记忆（新信息优先）
                        self._add_extracted_memory(
                            user_id, content, category, key, mem, mem_embedding,
                        )
                    # NONE → skip

        # 2b. Extract relations from relationship memories
        #      Also check identity memories with relationship keywords
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            cat = mem.get("category", "")
            if cat == "relationship":
                self._extract_and_store_relation(user_id, content, mem.get("key"))
            elif cat == "identity" and any(kw in content for kw in self._RELATION_KEYWORDS):
                self._extract_and_store_relation(user_id, content, mem.get("key"))

        # 3. Update profile
        #    Use LLM's profile_update if provided, otherwise auto-build from memories
        profile_update = extraction.get("profile_update")
        if profile_update and isinstance(profile_update, dict):
            self.store.set_profile(user_id, profile_update)
        else:
            # Check if any profile-relevant categories were extracted
            profile_categories = {"identity", "preference", "relationship"}
            has_profile_change = any(
                mem.get("category") in profile_categories
                for mem in extraction.get("memories", [])
            )
            if has_profile_change:
                self._rebuild_profile(user_id)

        # 4. Store episode summary
        episode_summary = extraction.get("episode_summary", "").strip()
        if episode_summary:
            # ASR emotion overrides LLM-guessed mood (acoustic signal more reliable)
            mood = detected_emotion if detected_emotion else extraction.get("mood")
            self.store.add_episode(
                user_id=user_id,
                session_id=session_id,
                summary=episode_summary,
                date=datetime.now().strftime("%Y-%m-%d"),
                mood=mood,
                topics=extraction.get("topics"),
            )

        self.logger.info(
            "Memory save complete: %d memories, profile_updated=%s, episode=%s",
            len(extraction.get("memories", [])),
            profile_update is not None,
            bool(episode_summary),
        )

    def _rebuild_profile(self, user_id: str) -> None:
        """Auto-build user profile from top memories when LLM doesn't provide one.

        Groups active memories by profile-relevant categories and constructs
        a structured profile JSON. Falls back gracefully if memories are sparse.
        """
        memories = self.store.get_active_memories(user_id)
        existing_profile = self.store.get_profile(user_id) or {}

        profile: dict = {
            "identity": existing_profile.get("identity", {}),
            "preferences": existing_profile.get("preferences", {}),
            "relationships": existing_profile.get("relationships", {}),
            "routines": existing_profile.get("routines", {}),
            "pending": existing_profile.get("pending", []),
            "status": existing_profile.get("status", ""),
        }

        # Extract identity facts
        for m in memories:
            cat = m.get("category", "")
            key = m.get("key", "")
            content = m.get("content", "")
            if not content:
                continue

            if cat == "identity" and key:
                profile["identity"][key] = content

            elif cat == "preference":
                # 用 content 整句作为偏好描述，不再尝试解析 "key: value" 格式
                # （LLM 提取的 content 格式不统一，partition 容易出错）
                if "不" in content or "讨厌" in content or "不喜欢" in content:
                    dislikes = profile["preferences"].setdefault("dislikes", [])
                    if content not in dislikes:
                        dislikes.append(content)
                else:
                    likes = profile["preferences"].setdefault("likes", [])
                    if content not in likes:
                        likes.append(content)

            elif cat == "relationship" and key:
                profile["relationships"][key] = content

            elif cat == "task":
                expires = m.get("expires") or m.get("time_ref")
                if expires:
                    pending = profile.setdefault("pending", [])
                    # Avoid duplicate pending items
                    existing_topics = {p.get("content", "") for p in pending if isinstance(p, dict)}
                    if content not in existing_topics:
                        pending.append({"content": content, "date": expires})

        # Filter out expired and corrupted pending items
        today = datetime.now().strftime("%Y-%m-%d")
        if profile.get("pending"):
            profile["pending"] = [
                p for p in profile["pending"]
                if isinstance(p, dict) and p.get("date", "9999") >= today
            ]

        self.store.set_profile(user_id, profile)
        self.logger.info("Profile auto-rebuilt for user %s", user_id)

    def _add_extracted_memory(
        self,
        user_id: str,
        content: str,
        category: str,
        key: str | None,
        mem: dict,
        embedding: "np.ndarray",
    ) -> str:
        """Add a single extracted memory to the store. Returns the memory ID."""
        return self.store.add_memory(
            user_id=user_id,
            content=content,
            category=category,
            key=key,
            importance=max(1.0, min(10.0, float(mem.get("importance", 5)))),
            tags=mem.get("tags"),
            time_ref=mem.get("time_ref"),
            expires=mem.get("expires"),
            source="extracted",
            embedding=embedding,
        )

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def _postprocess_extraction(self, memories: list[dict]) -> list[dict]:
        """Validate and fix extracted memories.

        Fixes:
          - Derive ``key`` for identity/preference/relationship/knowledge
            when missing.
          - Back-fill ``expires`` from ``time_ref`` for event/task.
          - Clamp ``importance`` to [1, 10]; enforce minimum 7 for identity.
        """
        for mem in memories:
            category = mem.get("category", "")
            key = mem.get("key")
            content = mem.get("content", "")

            # 1. key missing — try pattern matching, fallback to hash
            if category in ("identity", "preference", "relationship", "knowledge") and not key:
                mem["key"] = self._derive_key(content)


            # 2. expires missing — back-fill from time_ref + 1 day
            if category in ("event", "task"):
                time_ref = mem.get("time_ref")
                if time_ref and not mem.get("expires"):
                    try:
                        dt = datetime.strptime(time_ref, "%Y-%m-%d")
                        mem["expires"] = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    except ValueError:
                        pass

            # 3. importance — clamp to [1, 10]
            imp = mem.get("importance", 5)
            try:
                imp = int(imp)
            except (TypeError, ValueError):
                imp = 5
            mem["importance"] = max(1, min(10, imp))
            if category == "identity" and mem["importance"] < 7:
                mem["importance"] = 7

        return memories

    # Common Chinese content → semantic key mappings
    # Literal substring → key (checked with `in`, order matters: specific before general)
    _KEY_LITERALS: list[tuple[str, str]] = [
        ("住在", "location"), ("住", "location"),
        ("叫", "name"), ("名字", "name"),
        ("工作", "company"), ("上班", "company"),
        ("喜欢喝", "favorite_drink"), ("爱喝", "favorite_drink"),
        ("喜欢吃", "favorite_food"), ("爱吃", "favorite_food"),
        ("不喜欢吃", "dislike_food"), ("讨厌吃", "dislike_food"),
        ("不喜欢", "dislike"), ("讨厌", "dislike"),
        ("喜欢", "preference"), ("爱好", "hobby"),
        ("过敏", "allergy"),
        ("密码", "password"),
        ("生日", "birthday"),
        ("妹妹", "sister"), ("姐姐", "sister"),
        ("弟弟", "brother"), ("哥哥", "brother"),
        ("女朋友", "girlfriend"), ("男朋友", "boyfriend"),
        ("老婆", "wife"), ("老公", "husband"),
        ("爸爸", "father"), ("妈妈", "mother"),
        ("车", "car"),
    ]
    # Regex patterns (only for patterns that need regex metacharacters)
    _KEY_REGEX: list[tuple["re.Pattern[str]", str]] = [
        (re.compile(r"在.*公司"), "company"),
        (re.compile(r"开.*车"), "car"),
    ]

    def _derive_key(self, content: str) -> str:
        """Derive a semantic key from memory content via pattern matching."""
        for substring, key in self._KEY_LITERALS:
            if substring in content:
                return key
        for pattern, key in self._KEY_REGEX:
            if pattern.search(content):
                return key
        # Fallback: hash
        LOGGER.debug("No key pattern matched: %s — using hash", content[:40])
        return hashlib.md5(content[:30].encode()).hexdigest()[:8]

    def _extract_and_store_relation(
        self, user_id: str, content: str, key: str | None,
    ) -> None:
        """Extract entity pair from relationship memory and store in relations table."""
        source = relation = target = None

        # Pattern: "X 的 Y 叫/是 Z"
        match = re.search(r'(\S+)\s*的\s*(\S+?)\s*[叫是]\s*(\S+)', content)
        if match:
            source, relation, target = match.group(1), match.group(2), match.group(3)

        # Pattern: "X 有个/有一个 Y 叫 Z"
        if not source:
            match = re.search(r'(\S+)\s*有[个一][个]?\s*(\S+?)\s*叫\s*(\S+)', content)
            if match:
                source, relation, target = match.group(1), match.group(2), match.group(3)

        # Fallback: use key as relation
        if not source and key:
            parts = content.split(None, 1)
            if len(parts) >= 2:
                source = parts[0]
                relation = key
                target = parts[1][:20].rstrip("，。、")

        if not source or not target:
            return

        # Dedup: check if this exact relation already exists
        existing = self.store.get_relations(user_id, entity=source)
        for rel in existing:
            if rel["relation"] == relation and rel["target_entity"] == target:
                self.logger.debug("Relation already exists: %s -[%s]-> %s", source, relation, target)
                return

        self.store.add_relation(user_id, source, relation, target)
        self.logger.info("Relation extracted: %s -[%s]-> %s", source, relation, target)

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _call_llm_extract(
        self,
        conversation: str,
        profile: dict | None,
        existing: list[str],
        user_name: str,
    ) -> dict | None:
        """Call LLM to extract memories via function calling, with JSON fallback."""
        profile_str = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "{}"
        existing_str = "\n".join(f"- {e}" for e in existing[-30:]) if existing else "(无)"
        today = datetime.now().strftime("%Y-%m-%d")

        system_msg = (
            _EXTRACT_PROMPT_HEADER.format(today=today)
            + "\n\n当前用户名：" + user_name
            + "\n\n当前用户画像：\n" + profile_str
            + "\n\n已有记忆（避免重复）：\n" + existing_str
        )

        if not self._llm_api_key:
            self.logger.warning("No LLM API key for memory extraction")
            return None

        try:
            resp = requests.post(
                f"{self._llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._llm_model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": conversation},
                    ],
                    "temperature": 0,
                    "max_tokens": 1000,
                    "tools": [_EXTRACT_TOOL],
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": "extract_memories"},
                    },
                },
                timeout=15,
            )
            resp.raise_for_status()
            raw_args = (
                resp.json()["choices"][0]["message"]["tool_calls"][0]
                ["function"]["arguments"]
            )
            result = json.loads(raw_args)
            self.logger.info("Memory extraction via function calling succeeded")
            return result
        except Exception as exc:
            self.logger.warning(
                "Function calling extraction failed (%s), falling back to JSON mode",
                exc,
            )
            return self._call_llm_extract_json(
                conversation, profile, existing, user_name,
            )

    def _call_llm_extract_json(
        self,
        conversation: str,
        profile: dict | None,
        existing: list[str],
        user_name: str,
    ) -> dict | None:
        """Fallback: extract memories via free-text JSON output."""
        profile_str = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "{}"
        existing_str = "\n".join(f"- {e}" for e in existing[-30:]) if existing else "(无)"
        today = datetime.now().strftime("%Y-%m-%d")

        json_format_guide = (
            '\n\n输出 JSON：'
            '{"memories": [{"content": "...", "category": "identity|preference|'
            'relationship|event|task|knowledge", "key": "或null", "importance": 1-10, '
            '"tags": [], "time_ref": "2026-04-07或null", "expires": "2026-04-08或null"}], '
            '"corrections": [], "profile_update": null, '
            '"episode_summary": "一句话", "mood": "neutral", "topics": []}'
        )
        prompt = (
            _EXTRACT_PROMPT_HEADER.format(today=today)
            + json_format_guide
            + "\n\n当前用户名：" + user_name
            + "\n\n当前用户画像：\n" + profile_str
            + "\n\n已有记忆（避免重复）：\n" + existing_str
            + "\n\n对话内容：\n" + conversation
        )
        return self._call_openai_json(prompt)

    def _call_llm_dedup(
        self, new_content: str, similar: list[dict],
    ) -> dict:
        """Call LLM to decide ADD/UPDATE/DELETE/NONE for a potentially duplicate memory."""
        similar_str = "\n".join(
            f'{i+1}. [id: {m["id"]}] {m["content"]}'
            for i, m in enumerate(similar)
        )
        prompt = (
            _DEDUP_PROMPT_HEADER
            + "\n\n新记忆：" + new_content
            + "\n\n最相似的已有记忆：\n" + similar_str
        )
        result = self._call_openai_json(prompt)
        if result and "action" in result:
            return result
        return {"action": "ADD", "target_id": None}

    def _call_llm_merge(
        self, id_a: str, content_a: str, id_b: str, content_b: str,
    ) -> dict:
        """Call LLM to decide MERGE/KEEP_BOTH for two similar memories."""
        prompt = (
            _MERGE_PROMPT_HEADER
            + "\n\n记忆A [id: " + id_a + "]：" + content_a
            + "\n记忆B [id: " + id_b + "]：" + content_b
        )
        result = self._call_openai_json(prompt)
        if result and "action" in result:
            return result
        return {"action": "KEEP_BOTH", "keep_id": None}

    def _call_openai_json(self, prompt: str) -> dict | None:
        """Make a simple OpenAI-compatible chat completion call expecting JSON."""
        if not self._llm_api_key:
            self.logger.warning("No LLM API key for memory extraction")
            return None

        try:
            resp = requests.post(
                f"{self._llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 1000,
                    "response_format": {"type": "json_object"},
                },
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            return json.loads(raw)
        except Exception as exc:
            self.logger.warning("Memory LLM call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_memory_context(
        self,
        profile: dict | None,
        episodes: list[dict],
        memories: list[dict],
        user_id: str = "",
    ) -> str:
        """Format the three tiers into a ``<memory>`` prompt block."""
        sections: list[str] = []
        char_budget = _MAX_MEMORY_CHARS

        # Tier 1: Profile (highest priority)
        if profile:
            profile_text = self._profile_to_text(profile)
            if profile_text:
                sections.append(f"[关于用户]\n{profile_text}")
                char_budget -= len(profile_text)

        # Tier 2: Recent episodes
        if episodes and char_budget > 0:
            ep_lines = []
            for ep in episodes[:5]:
                line = f"{ep['date']}：{ep['summary']}"
                if char_budget - len(line) < 0:
                    break
                ep_lines.append(line)
                char_budget -= len(line)
            if ep_lines:
                sections.append("[最近]\n" + "\n".join(ep_lines))

        # Tier 2b: Recent digests (older context)
        if user_id and char_budget > 0:
            digests = self.store.get_recent_digests(user_id, limit=4)
            if digests:
                digest_lines = []
                for d in digests:
                    line = f"{d['period_start']}~{d['period_end']}：{d['digest']}"
                    if char_budget - len(line) < 0:
                        break
                    digest_lines.append(line)
                    char_budget -= len(line)
                if digest_lines:
                    sections.append("[更早]\n" + "\n".join(digest_lines))

        # Tier 3: Memories (remaining budget)
        if memories and char_budget > 0:
            mem_lines = []
            for m in memories:
                if isinstance(m, dict) and m.get("content"):
                    line = f"- {m['content']}"
                    if char_budget - len(line) < 0:
                        break
                    mem_lines.append(line)
                    char_budget -= len(line)
            if mem_lines:
                sections.append("[记忆]\n" + "\n".join(mem_lines))

        # Pending items (from profile, outside main budget)
        if profile and profile.get("pending"):
            today = datetime.now().strftime("%Y-%m-%d")
            due_items = []
            for item in profile["pending"]:
                if isinstance(item, dict) and item.get("date", "9999") <= today:
                    due_items.append(f"- {item.get('content', '')}")
            if due_items:
                sections.append("[待关心]\n" + "\n".join(due_items))

        if not sections:
            return ""

        return (
            "<memory>\n"
            + "\n\n".join(sections)
            + "\n</memory>"
        )

    def _profile_to_text(self, profile: dict) -> str:
        """Convert profile JSON to concise natural language."""
        parts: list[str] = []

        identity = profile.get("identity", {})
        if identity:
            id_parts = []
            if identity.get("name"):
                id_parts.append(identity["name"])
            if identity.get("occupation"):
                id_parts.append(identity["occupation"])
            if identity.get("location"):
                id_parts.append(f"住{identity['location']}")
            if identity.get("traits"):
                id_parts.extend(identity["traits"])
            if id_parts:
                parts.append("，".join(id_parts) + "。")

        prefs = profile.get("preferences", {})
        if prefs.get("likes"):
            parts.append("喜欢：" + "、".join(prefs["likes"]) + "。")
        if prefs.get("dislikes"):
            parts.append("不喜欢：" + "、".join(prefs["dislikes"]) + "。")

        relationships = profile.get("relationships", {})
        if relationships:
            r_parts = [f"{k}：{v}" for k, v in relationships.items()]
            parts.append("关系：" + "；".join(r_parts) + "。")

        routines = profile.get("routines", {})
        if routines:
            r_parts = [f"{k}：{v}" for k, v in routines.items()]
            parts.append("习惯：" + "；".join(r_parts) + "。")

        status = profile.get("status")
        if status:
            parts.append(f"近况：{status}")

        return "\n".join(parts)

    def _messages_to_text(self, messages: list[dict]) -> str:
        """Convert conversation messages to readable text for extraction."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            text_parts.append(f"[工具结果: {block.get('content', '')}]")
                    else:
                        text_parts.append(str(block))
                text = " ".join(text_parts)
            else:
                text = str(content)

            if text.strip():
                prefix = "用户" if role == "user" else "小贾"
                lines.append(f"{prefix}：{text.strip()}")

        return "\n".join(lines)
