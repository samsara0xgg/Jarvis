"""MemoryManager — core memory interface for Jarvis.

Provides two public methods:
  - query(text, user_id) → formatted memory context for LLM prompt injection
  - save(messages, user_id, session_id) → extract and persist memories (async)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import numpy as np
import requests

from memory.embedder import Embedder
from memory.retriever import MemoryRetriever
from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

_EXTRACT_PROMPT_HEADER = """从以下对话中提取值得长期记住的信息。只提取用户说的内容。
忽略打招呼、设备操作指令（开灯/关灯/几点了）、纯闲聊。

记忆类型（category）：
- identity：身份信息（姓名、职业、住址等）
- preference：偏好（喜欢/不喜欢）
- relationship：人际关系（家人、朋友、同事）
- event：事件（发生过或即将发生的事，自含完整描述含时间）
- task：待办/承诺（要做的事，有截止时间）
- knowledge：用户教的知识（密码、技术知识等）

记忆粒度：
- event/task：输出自含的完整描述，包含时间和上下文
  例："用户 于2026年4月说下周一要去深圳参加技术峰会"
- 其他类型：输出原子事实
  例："用户 喜欢拿铁"

每条记忆必须：
- 自含（不依赖上下文就能理解）
- 包含主语（用用户名，不用"我"或"他"）
- event/task 包含时间信息

字段说明：
- key：简短语义标识，同类型同key的记忆视为同一件事的更新。
  identity/preference/relationship/knowledge 类型必须提供 key（不能为 null）。
  event/task 类型 key 为 null。
  例：name, location, favorite_drink, favorite_sport, sister, wifi_password
- expires：过期日期。event/task 类型必须提供 expires（事件日期+1天）。
  其他类型 expires 为 null。
  例：面试在4月7日 → expires = "2026-04-08"；周末爬山 → expires = 下周一日期

输出 JSON（严格 JSON，无注释）：
{
  "memories": [
    {"content": "...", "category": "identity|preference|relationship|event|task|knowledge",
     "key": "location 或 null", "importance": 1-10,
     "tags": ["标签1"], "time_ref": "2026-04-07 或 null",
     "expires": "2026-04-08 或 null"}
  ],
  "corrections": [],
  "profile_update": null,
  "episode_summary": "一句话概括这次对话",
  "mood": "neutral",
  "topics": ["主题1"]
}

profile_update 规则：如果提取到了 identity/preference/relationship 类的新信息，
输出更新后的完整画像 JSON（结构：identity/preferences/routines/relationships/pending/status）。
否则输出 null。

如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。这会帮助系统更新旧记忆。
corrections 格式：
  {"old_content": "被纠正的内容关键词", "new_content": "正确内容", "reason": "纠正原因"}
如果没有纠正，corrections 为空数组。

如果对话中没有值得记住的内容，memories 数组留空。"""

_DEDUP_PROMPT_HEADER = """判断新记忆与已有记忆的关系。

判断：
- ADD：全新信息，与已有记忆不重复
- UPDATE：已有记忆的更新版本（指明 target_id，保留信息量更大的版本）
- NONE：已存在，无需操作

输出 JSON（严格 JSON，无注释）：
{"action": "ADD 或 UPDATE 或 NONE", "target_id": "xxx 或 null"}"""

_MEMORY_USAGE_GUIDE = (
    "以上是你对用户的了解。像朋友一样自然地运用这些信息，"
    "不要像读档案一样列举。和当前话题无关的记忆不要强行提起。"
    "待关心的事项找合适的时机自然地提起，别像闹钟一样提醒。"
)

# Cosine thresholds for dedup — lower for same-category (catches "coffee" vs "latte")
_DEDUP_THRESHOLD_SAME_CAT = 0.55
_DEDUP_THRESHOLD_CROSS_CAT = 0.7

_MERGE_PROMPT_HEADER = """以下两条记忆语义相似，判断是否应该合并。

- MERGE：是同一件事的不同表述，输出 keep_id（保留信息量更大的那条）
- KEEP_BOTH：虽然相似但确实是不同的事

输出 JSON（严格 JSON，无注释）：
{"action": "MERGE 或 KEEP_BOTH", "keep_id": "xxx 或 null"}"""

_MAX_MEMORY_CHARS = 1200  # ~500 tokens Chinese

# Maintenance: max pairs to check per run, max LLM calls per run
_MAINTAIN_MAX_PAIRS = 10
_MAINTAIN_COSINE_THRESHOLD = 0.8


class MemoryManager:
    """Manages Jarvis's long-term memory — query and save.

    Args:
        config: Parsed application configuration.
    """

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

        # Decide retrieval strategy
        if active_count < self._full_inject_threshold:
            # Small memory: inject everything
            relevant = self.store.get_active_memories(user_id)
        else:
            # Large memory: embedding retrieval
            query_emb = self.embedder.encode(text)
            relevant = self.retriever.retrieve(
                query_emb, user_id, top_k=5,
            )

        return self._format_memory_context(profile, episodes, relevant)

    def maintain(self, user_id: str) -> dict:
        """Run periodic maintenance: find and merge semantic duplicates.

        Designed to run in a background scheduler (e.g. weekly).

        Returns:
            {"merged": int, "checked": int, "skipped": int}
        """
        ids, contents, categories, embeddings = self.store.get_embedding_index(user_id)
        if embeddings is None or len(ids) < 2:
            return {"merged": 0, "checked": 0, "skipped": 0}

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
        return {"merged": merged, "checked": len(pairs), "skipped": skipped}

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

    def save(self, messages: list[dict], user_id: str, session_id: str) -> None:
        """Extract memories from a completed conversation and persist.

        Designed to run asynchronously (in a background thread).

        Args:
            messages: Full conversation message list.
            user_id: Authenticated user ID.
            session_id: Conversation session identifier.
        """
        try:
            self._save_inner(messages, user_id, session_id)
        except Exception:
            self.logger.exception("Memory save failed for user %s", user_id)

    # ------------------------------------------------------------------
    # Save pipeline
    # ------------------------------------------------------------------

    def _save_inner(self, messages: list[dict], user_id: str, session_id: str) -> None:
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

        # 2. Process each extracted memory
        for mem in extraction.get("memories", []):
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
                # Same category+key → deterministic UPDATE
                new_id = self._add_extracted_memory(
                    user_id, content, category, key, mem, mem_embedding,
                )
                self.store.supersede_memory(existing_by_key["id"], new_id)
            else:
                # No key match → fall back to embedding similarity
                similar = self.retriever.find_similar(mem_embedding, user_id, top_k=5)

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
                    # NONE → skip

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
            self.store.add_episode(
                user_id=user_id,
                session_id=session_id,
                summary=episode_summary,
                date=datetime.now().strftime("%Y-%m-%d"),
                mood=extraction.get("mood"),
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
    # LLM calls
    # ------------------------------------------------------------------

    def _call_llm_extract(
        self,
        conversation: str,
        profile: dict | None,
        existing: list[str],
        user_name: str,
    ) -> dict | None:
        """Call LLM to extract memories from a conversation."""
        profile_str = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "{}"
        existing_str = "\n".join(f"- {e}" for e in existing[-30:]) if existing else "(无)"

        prompt = (
            _EXTRACT_PROMPT_HEADER
            + "\n\n当前用户名：" + user_name
            + "\n\n当前用户画像：\n" + profile_str
            + "\n\n已有记忆（避免重复）：\n" + existing_str
            + "\n\n对话内容：\n" + conversation
        )
        return self._call_openai_json(prompt)

    def _call_llm_dedup(
        self, new_content: str, similar: list[dict],
    ) -> dict:
        """Call LLM to decide ADD/UPDATE/NONE for a potentially duplicate memory."""
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
            + "\n\n[使用原则] " + _MEMORY_USAGE_GUIDE
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
