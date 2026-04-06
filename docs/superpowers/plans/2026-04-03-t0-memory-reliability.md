# T0: 记忆系统可信赖 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让小月的记忆系统从"代码存在"变成"端到端可信赖"——存的对、取的准、用的好、改得了。

**Architecture:** 四个子任务顺序推进：(1) 修复已知架构问题 + 管线验证 (2) 记忆质量保证 + 自然修正 (3) prompt 注入优化 + Level 1 快路径 (4) 运维保障。所有改动在现有 MemoryManager / personality.py / jarvis.py 基础上修改，不引入新依赖。

**Tech Stack:** Python 3.11 / SQLite / FastEmbed (bge-small-zh-v1.5) / numpy / pytest

**Design Spec:** `docs/superpowers/specs/2026-04-03-jarvis-growth-roadmap-design.md`

---

## File Map

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `core/personality.py` | 删除未使用的 `preferences` 参数，加记忆使用指引 |
| Modify | `core/llm.py:759-768` | `_personalize_system` 删除 `preferences` 传递 |
| Modify | `memory/manager.py` | 加 correction 提取、注入量控制 |
| Modify | `memory/store.py` | 加 `behavior_log` 表、过期记忆降权查询 |
| Modify | `memory/retriever.py` | Level 1 直接回答方法 |
| Modify | `jarvis.py` | 对话结束检测、Level 1 快路径、行为日志写入、maintenance 改为每天 |
| Create | `memory/behavior_log.py` | 行为日志收集器（append-only） |
| Create | `memory/direct_answer.py` | Level 1 直接回答引擎 |
| Create | `tests/test_behavior_log.py` | 行为日志测试 |
| Create | `tests/test_direct_answer.py` | Level 1 直接回答测试 |
| Modify | `tests/test_memory_manager.py` | 加 correction / 注入量控制测试 |
| Modify | `tests/test_personality.py` | 更新参数变更后的测试 |

---

## Task 1: 修复 preferences 重复路径

`build_personality_prompt` 有一个 `preferences` 参数和一个 `memory_context` 参数，两者做同一件事（注入用户偏好到 prompt）。`memory_context` 已经通过 `<memory>` 块包含 profile，但 `preferences` 从未被传值。清理这个冗余。

**Files:**
- Modify: `core/personality.py:81-153`
- Modify: `core/llm.py:759-768`
- Modify: `tests/test_personality.py`

- [ ] **Step 1: 读 `tests/test_personality.py` 确认有哪些测试引用 `preferences` 参数**

Run: `grep -n "preferences" tests/test_personality.py`

- [ ] **Step 2: 写测试确认 `preferences` 参数移除后行为不变**

```python
# tests/test_personality.py — 新增
def test_memory_context_injected():
    """memory_context should appear in the prompt."""
    prompt = build_personality_prompt(
        user_name="Allen",
        memory_context="<memory>\n[关于用户]\nAllen，喜欢拿铁\n</memory>",
    )
    assert "<memory>" in prompt
    assert "喜欢拿铁" in prompt


def test_no_preferences_block_without_memory():
    """Without memory_context, no <preferences> block should appear."""
    prompt = build_personality_prompt(user_name="Allen")
    assert "<preferences>" not in prompt
```

- [ ] **Step 3: 跑测试确认新测试通过（memory_context 已经工作）、旧 preferences 测试是否存在**

Run: `python -m pytest tests/test_personality.py -v`

- [ ] **Step 4: 从 `build_personality_prompt` 删除 `preferences` 参数和 `<preferences>` 块**

`core/personality.py` — 修改函数签名和函数体：

```python
def build_personality_prompt(
    user_name: str | None = None,
    user_role: str = "guest",
    situation: str = "normal",
    user_emotion: str = "",
    memory_context: str = "",
) -> str:
```

删除 `preferences` 参数、删除函数末尾的 `if preferences:` 块（约 144-151 行）。docstring 中也移除 `preferences` 相关描述。

- [ ] **Step 5: 更新 `_personalize_system` 确保不传 `preferences`**

`core/llm.py:759-768` — 确认没有传 `preferences`（当前已经没传，但要确认）。

- [ ] **Step 6: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败（原有 4 个 fail 不变）

- [ ] **Step 7: Commit**

```bash
git add core/personality.py core/llm.py tests/test_personality.py
git commit -m "Remove unused preferences param from build_personality_prompt"
```

---

## Task 2: 记忆 prompt 使用指引

在 `<memory>` 块注入时追加使用指引，让 LLM 自然使用记忆而不是机械复述。

**Files:**
- Modify: `memory/manager.py:520-564` (`_format_memory_context`)

- [ ] **Step 1: 写测试**

```python
# tests/test_memory_manager.py — 在 TestQuery 类中新增
def test_memory_context_includes_usage_guide(self, manager: MemoryManager):
    """Memory context should include natural usage guidance."""
    manager.store.set_profile("user1", {
        "identity": {"name": "Allen"},
    })
    result = manager.query("你好", "user1")
    assert "自然" in result or "朋友" in result
    assert "<memory>" in result
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_memory_manager.py::TestQuery::test_memory_context_includes_usage_guide -v`
Expected: FAIL

- [ ] **Step 3: 在 `_format_memory_context` 末尾追加使用指引**

`memory/manager.py` — 修改 `_format_memory_context` 的 return 语句：

```python
    _MEMORY_USAGE_GUIDE = (
        "以上是你对用户的了解。像朋友一样自然地运用这些信息，"
        "不要像读档案一样列举。和当前话题无关的记忆不要强行提起。"
        "待关心的事项找合适的时机自然地提起，别像闹钟一样提醒。"
    )

    # ... 在 _format_memory_context 的 return 中：
    if not sections:
        return ""

    return (
        "<memory>\n"
        + "\n\n".join(sections)
        + "\n\n[使用原则] " + _MEMORY_USAGE_GUIDE
        + "\n</memory>"
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_memory_manager.py::TestQuery -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add memory/manager.py tests/test_memory_manager.py
git commit -m "Add natural usage guidance to memory prompt injection"
```

---

## Task 3: 记忆注入量控制

控制注入 prompt 的记忆总量不超过 ~500 tokens（约 800 汉字），避免 prompt 膨胀。相关性高的记忆排前面。

**Files:**
- Modify: `memory/manager.py:520-564` (`_format_memory_context`)
- Modify: `tests/test_memory_manager.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_memory_manager.py — TestQuery 中新增
def test_memory_context_length_capped(self, manager: MemoryManager):
    """Memory context should not exceed ~800 chars of content."""
    # 插入大量记忆
    for i in range(50):
        manager.store.add_memory(
            user_id="user1",
            content=f"这是第{i}条很长的测试记忆，包含各种信息细节。" * 3,
            category="knowledge",
            importance=5.0,
            embedding=np.random.randn(512).astype(np.float32),
        )
    result = manager.query("测试", "user1")
    # 去掉 XML 标签和指引后，记忆内容部分不应超过 1200 字符
    assert len(result) < 2000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_memory_manager.py::TestQuery::test_memory_context_length_capped -v`

- [ ] **Step 3: 在 `_format_memory_context` 中加长度限制**

`memory/manager.py` — 在格式化 memories section 时截断：

```python
    _MAX_MEMORY_CHARS = 1200  # ~500 tokens 中文

    def _format_memory_context(self, profile, episodes, memories):
        sections: list[str] = []
        char_budget = _MAX_MEMORY_CHARS

        # Tier 1: Profile（优先级最高，先扣预算）
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

        # Tier 3: Memories（用剩余预算）
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

        # Pending items（从 profile 中提取，不占主预算）
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_memory_manager.py::TestQuery -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add memory/manager.py tests/test_memory_manager.py
git commit -m "Cap memory injection to ~500 tokens to prevent prompt bloat"
```

---

## Task 4: 行为日志收集器

为 T2（行为学习）预埋数据收集。append-only 的 SQLite 表，记录 skill 调用、对话、修正等事件。

**Files:**
- Create: `memory/behavior_log.py`
- Create: `tests/test_behavior_log.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_behavior_log.py
"""Tests for memory.behavior_log — append-only behavior event logging."""

from __future__ import annotations

import json

import pytest

from memory.behavior_log import BehaviorLog


@pytest.fixture()
def blog(tmp_path):
    return BehaviorLog(str(tmp_path / "test.db"))


class TestBehaviorLog:
    def test_log_and_query(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather", "params": {}})
        blog.log("user1", "conversation", {"turns": 3, "duration_s": 45})
        events = blog.get_events("user1", limit=10)
        assert len(events) == 2
        assert events[0]["event_type"] == "conversation"  # newest first
        assert events[1]["event_type"] == "skill_call"

    def test_empty_user(self, blog: BehaviorLog):
        events = blog.get_events("nobody", limit=10)
        assert events == []

    def test_filter_by_event_type(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather"})
        blog.log("user1", "skill_call", {"skill": "news"})
        blog.log("user1", "conversation", {"turns": 2})
        events = blog.get_events("user1", event_type="skill_call")
        assert len(events) == 2
        assert all(e["event_type"] == "skill_call" for e in events)

    def test_get_events_since(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather"})
        events = blog.get_events("user1", since_days=7)
        assert len(events) == 1

    def test_detail_is_json(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather", "params": {"city": "Vancouver"}})
        events = blog.get_events("user1")
        detail = events[0]["detail"]
        assert isinstance(detail, dict)
        assert detail["skill"] == "weather"
```

- [ ] **Step 2: 跑测试确认失败（模块不存在）**

Run: `python -m pytest tests/test_behavior_log.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 实现 `BehaviorLog`**

```python
# memory/behavior_log.py
"""Append-only behavior event log for usage pattern analysis (T2).

Records skill calls, conversations, suggestions, and corrections
in a SQLite table. Designed for T2 behavior learning to consume.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class BehaviorLog:
    """Append-only behavior event store.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str | Path = "data/memory/jarvis_memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS behavior_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                detail      TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_behavior_user_time "
            "ON behavior_log(user_id, timestamp DESC)"
        )
        conn.commit()

    def log(self, user_id: str, event_type: str, detail: dict[str, Any] | None = None) -> None:
        """Append a behavior event.

        Args:
            user_id: User identifier.
            event_type: One of: skill_call, conversation, suggestion_response, correction.
            detail: Arbitrary JSON-serializable metadata.
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO behavior_log (timestamp, user_id, event_type, detail) "
            "VALUES (?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                user_id,
                event_type,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        conn.commit()

    def get_events(
        self,
        user_id: str,
        event_type: str | None = None,
        since_days: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query behavior events for a user.

        Args:
            user_id: User identifier.
            event_type: Filter by event type (optional).
            since_days: Only events from the last N days (optional).
            limit: Maximum number of results.

        Returns:
            List of event dicts, newest first.
        """
        conn = self._get_conn()
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]

        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)

        if since_days is not None:
            clauses.append("timestamp >= datetime('now', ?)")
            params.append(f"-{since_days} days")

        where = " AND ".join(clauses)
        params.append(limit)

        rows = conn.execute(
            f"SELECT * FROM behavior_log WHERE {where} "
            f"ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except (json.JSONDecodeError, TypeError):
                d["detail"] = {}
            results.append(d)
        return results

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_behavior_log.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add memory/behavior_log.py tests/test_behavior_log.py
git commit -m "Add BehaviorLog: append-only event store for T2 behavior learning"
```

---

## Task 5: 在 jarvis.py 中接入行为日志

在 skill 调用和对话结束时写入行为日志。

**Files:**
- Modify: `jarvis.py`

- [ ] **Step 1: 在 `__init__` 中初始化 BehaviorLog**

`jarvis.py` — 在 `self.memory_manager = MemoryManager(config)` 后面加：

```python
from memory.behavior_log import BehaviorLog
# 共用同一个 db 文件
mem_db = config.get("memory", {}).get("db_path", "data/memory/jarvis_memory.db")
self.behavior_log = BehaviorLog(mem_db)
```

- [ ] **Step 2: 在 skill 执行后记录 skill_call 事件**

`jarvis.py` — 找到 `self.skill_registry.execute` 的调用点。skill 是通过 LLM tool_calling 调用的，执行器在 `SkillRegistry.execute`。最简单的方式是在 `_handle_utterance_inner` 的对话结束保存处（第 564-570 行区域），根据 `updated_messages` 提取 tool_call 信息：

```python
# 在 "8. Save conversation + async memory extraction" 区域之后加：
# 9. Log behavior events
if user_id and updated_messages:
    for msg in updated_messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.behavior_log.log(user_id, "skill_call", {
                        "skill": block.get("name", ""),
                        "input": block.get("input", {}),
                    })
```

- [ ] **Step 3: 记录 conversation 事件**

在同一区域，对话处理结束后记录：

```python
if user_id:
    self.behavior_log.log(user_id, "conversation", {
        "text": text[:100],  # 截断，只记前100字
        "emotion": detected_emotion,
        "route": "local" if response_text and not updated_messages else "cloud",
    })
```

- [ ] **Step 4: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败

- [ ] **Step 5: Commit**

```bash
git add jarvis.py
git commit -m "Wire BehaviorLog into utterance pipeline for skill and conversation events"
```

---

## Task 6: 对话结束检测 + save 触发优化

当前 save() 在每次 LLM 对话后都触发（`jarvis.py:568-569`）。这没问题但需要确认：(1) 非 LLM 路径（本地执行）也需要在一定条件下保存；(2) 明确静默超时和告别语触发。

**Files:**
- Modify: `jarvis.py`

- [ ] **Step 1: 确认当前 save 触发路径**

当前只在 `updated_messages is not None` 时（即走了 LLM）才 save。本地执行路径（keyword match、local route）不会 save。这是合理的——本地路径没有有价值的对话内容。但如果用户在本地路径中说了重要的话（如 "记住我喜欢拿铁" 但被 keyword 匹配走了），就会丢失。

- [ ] **Step 2: 在 farewell 检测中触发最终 save**

`jarvis.py` — 找到 `_is_farewell` 的调用点（在 `run_no_wake` 和 `run_with_wake` 的主循环中）。在检测到 farewell 后，确保对该 session 的完整历史做一次 save：

在主循环中 farewell 检测后加：

```python
# 告别时确保记忆保存
if user_id:
    full_history = self.conversation_store.get_history(session_id)
    if full_history:
        self._executor.submit(
            self.memory_manager.save, full_history, user_id, session_id,
        )
```

- [ ] **Step 3: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败

- [ ] **Step 4: Commit**

```bash
git add jarvis.py
git commit -m "Trigger memory save on farewell to capture full conversation context"
```

---

## Task 7: 记忆自然修正（correction 提取）

让用户通过自然对话修正记忆（"不对，我喜欢美式不是拿铁"），不需要显式命令。

**Files:**
- Modify: `memory/manager.py` (提取 prompt + save 逻辑)
- Modify: `tests/test_memory_manager.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_memory_manager.py — TestSavePipeline 中新增
def test_save_correction_supersedes(self, manager: MemoryManager):
    """When user corrects a memory, old one should be superseded."""
    # 先存一条已有记忆
    emb = manager.embedder.encode("Allen 喜欢拿铁")
    manager.store.add_memory(
        user_id="user1", content="Allen 喜欢拿铁",
        category="preference", key="favorite_drink",
        importance=7.0, embedding=emb,
    )

    # 模拟 LLM 提取出 correction
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

    # 旧记忆应该被 supersede（通过 key-based dedup）
    active = manager.store.get_active_memories("user1")
    contents = [m["content"] for m in active]
    assert any("美式" in c for c in contents)
    # 拿铁那条应该被 supersede 或不在 active 中
    assert not any(c == "Allen 喜欢拿铁" for c in contents)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_memory_manager.py::TestSavePipeline::test_save_correction_supersedes -v`

- [ ] **Step 3: 在提取 prompt 中加入 correction 指引**

`memory/manager.py` — 在 `_EXTRACT_PROMPT_HEADER` 末尾、JSON 格式之前追加：

```python
# 在 "如果对话中没有值得记住的内容，memories 数组留空。" 之后加：

如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。这会帮助系统更新旧记忆。
corrections 格式：
  {"old_content": "被纠正的内容关键词", "new_content": "正确内容", "reason": "纠正原因"}
如果没有纠正，corrections 为空数组。
```

同时在 JSON 输出格式模板中加 `"corrections": []`。

- [ ] **Step 4: 在 `_save_inner` 中处理 corrections**

`memory/manager.py` — 在 `_save_inner` 处理 memories 循环之后，加处理 corrections 的逻辑：

```python
        # 2b. Process corrections — deactivate contradicted memories
        for correction in extraction.get("corrections", []):
            old_kw = correction.get("old_content", "")
            if old_kw:
                deactivated = self.store.deactivate_memory(user_id, old_kw)
                if deactivated:
                    self.logger.info("Memory corrected: deactivated '%s'", old_kw)
```

注意：这与 key-based dedup 是互补的。key-based dedup 处理同 key 更新，correction 处理语义否定（"不对，不是 X 是 Y"）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_memory_manager.py::TestSavePipeline -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add memory/manager.py tests/test_memory_manager.py
git commit -m "Add correction extraction: natural language memory updates"
```

---

## Task 8: Level 1 直接回答引擎

对于高置信度的事实类查询，直接用记忆回答，不走 LLM。

**Files:**
- Create: `memory/direct_answer.py`
- Create: `tests/test_direct_answer.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_direct_answer.py
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

    # Mock embedder
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
        # 用完全不相关的查询
        result = answerer.try_answer("今天天气怎么样", "user1")
        assert result is None

    def test_high_similarity_preference_returns_answer(self, answerer: DirectAnswerer):
        """High-similarity preference query should return direct answer."""
        # 存一条偏好记忆
        content = "Allen 喜欢喝拿铁"
        emb = answerer._embedder.encode(content)
        answerer._store.add_memory(
            user_id="user1", content=content,
            category="preference", key="favorite_drink",
            importance=8.0, embedding=emb,
        )
        # 用相同文本查询（mock embedder 会生成相同向量 → cosine=1.0）
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_direct_answer.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 实现 `DirectAnswerer`**

```python
# memory/direct_answer.py
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
        memories = self._store.get_active_memories(user_id)
        candidates = [
            m for m in memories
            if m.get("category") in _ANSWERABLE_CATEGORIES
            and m.get("embedding") is not None
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

        # Touch the accessed memory
        self._store.touch_memory(best["id"])

        LOGGER.info(
            "Level 1 direct answer: score=%.3f category=%s content=%s",
            best_score, category, content[:60],
        )
        return template.format(content=content)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_direct_answer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add memory/direct_answer.py tests/test_direct_answer.py
git commit -m "Add DirectAnswerer: Level 1 memory-based fast answers"
```

---

## Task 9: 在 jarvis.py 中接入 Level 1 快路径

在意图路由之前加 Level 1 记忆检索。

**Files:**
- Modify: `jarvis.py`

- [ ] **Step 1: 在 `__init__` 中初始化 DirectAnswerer**

`jarvis.py` — 在 `self.memory_manager = MemoryManager(config)` 之后：

```python
from memory.direct_answer import DirectAnswerer
self.direct_answerer = DirectAnswerer(
    self.memory_manager.store, self.memory_manager.embedder,
)
```

- [ ] **Step 2: 在 `_handle_utterance_inner` 中加 Level 1 检查**

在 step 4（Load conversation history + memory）之后、step 5（Keyword trigger check）之前，插入：

```python
        # 4b. Level 1: Try direct answer from memory (< 100ms, no LLM)
        if user_id:
            try:
                direct = self.direct_answerer.try_answer(text, user_id)
                if direct:
                    self.logger.info("Level 1 direct answer: %s", direct[:60])
                    self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                    print(f"🤖 小月 (L1): {direct}")
                    self._speak_nonblocking(direct, emotion=detected_emotion)
                    # 记录行为日志
                    if hasattr(self, "behavior_log"):
                        self.behavior_log.log(user_id, "conversation", {
                            "text": text[:100],
                            "route": "memory_l1",
                            "answer": direct[:100],
                        })
                    return direct
            except Exception as exc:
                self.logger.warning("Level 1 answer failed: %s", exc)
```

- [ ] **Step 3: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败

- [ ] **Step 4: Commit**

```bash
git add jarvis.py
git commit -m "Wire Level 1 direct answer into utterance pipeline before intent routing"
```

---

## Task 10: Maintenance 改为每天 + Dashboard 记忆面板

把 memory maintenance 从每周日改为每天凌晨 3 点。Dashboard 加记忆统计。

**Files:**
- Modify: `jarvis.py:786-795` (scheduler setup)
- Modify: `ui/dashboard.py` (加记忆面板)

- [ ] **Step 1: 把 maintenance 改为每天**

`jarvis.py` — 修改 `_setup_memory_maintenance`：

```python
    def _setup_memory_maintenance(self) -> None:
        """Register daily memory maintenance (3am)."""
        self.scheduler.add_cron_job(
            job_id="memory_maintenance",
            func=self._run_memory_maintenance,
            hour="3",
            minute="0",
        )
        self.logger.info("Memory maintenance scheduled: daily 3:00am")
```

- [ ] **Step 2: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败

- [ ] **Step 3: Commit**

```bash
git add jarvis.py
git commit -m "Change memory maintenance from weekly to daily at 3am"
```

- [ ] **Step 4: 在 Dashboard 加记忆统计面板**

`ui/dashboard.py` — 读文件找到合适的插入点，加一个简单的记忆统计显示。这个任务需要先读 dashboard 完整代码来确定具体插入位置。由于 dashboard 是 Gradio UI，加一个 `gr.Row` 显示记忆数量和最近 episode 即可。

具体实现取决于 dashboard 的当前结构（需要在执行时读取），基本逻辑是：

```python
# 在 dashboard 中加一个 memory stats 查询函数
def _get_memory_stats(app):
    """Query memory stats for dashboard display."""
    store = app.memory_manager.store
    user_ids = store.get_all_user_ids()
    stats = {}
    for uid in user_ids:
        count = store.count_active(uid)
        episodes = store.get_recent_episodes(uid, days=3)
        stats[uid] = {"memories": count, "recent_episodes": len(episodes)}
    return stats
```

- [ ] **Step 5: Commit**

```bash
git add ui/dashboard.py
git commit -m "Add memory stats panel to Gradio dashboard"
```

---

## Task 11: 端到端集成测试

验证完整记忆链路：说 → 存 → 检索 → 注入 prompt → 直接回答。

**Files:**
- Create: `tests/test_memory_e2e.py`

- [ ] **Step 1: 写集成测试**

```python
# tests/test_memory_e2e.py
"""End-to-end memory integration tests.

Verifies the full pipeline: save → extract → store → query → inject.
Uses mocked LLM for extraction but real SQLite + embedder mock.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from memory.manager import MemoryManager
from memory.direct_answer import DirectAnswerer
from memory.behavior_log import BehaviorLog


def _make_config(db_path: str) -> dict:
    return {
        "memory": {"db_path": db_path},
        "llm": {"api_key": "test-key", "model": "gpt-4o-mini"},
    }


def _deterministic_encode(text: str) -> np.ndarray:
    """Deterministic mock encoder: same text → same vector."""
    rng = np.random.RandomState(hash(text) % 2**31)
    v = rng.randn(512).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


@pytest.fixture()
def setup(tmp_path):
    db_path = str(tmp_path / "e2e.db")
    config = _make_config(db_path)
    mgr = MemoryManager(config)
    mgr.embedder = MagicMock()
    mgr.embedder.encode = _deterministic_encode
    answerer = DirectAnswerer(mgr.store, mgr.embedder)
    blog = BehaviorLog(db_path)
    return mgr, answerer, blog


class TestMemoryE2E:
    def test_save_then_query_contains_memory(self, setup):
        """After saving a conversation, query should return the extracted fact."""
        mgr, answerer, blog = setup

        # Mock LLM extraction response
        extraction = {
            "memories": [{
                "content": "Allen 喜欢拿铁",
                "category": "preference",
                "key": "favorite_drink",
                "importance": 8,
                "tags": ["饮品"],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [],
            "profile_update": {
                "identity": {"name": "Allen"},
                "preferences": {"likes": ["拿铁"]},
                "relationships": {},
                "routines": {},
                "pending": [],
                "status": "",
            },
            "episode_summary": "Allen 说了他喜欢喝拿铁",
            "mood": "neutral",
            "topics": ["饮品偏好"],
        }

        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [
                    {"role": "user", "content": "我喜欢喝拿铁"},
                    {"role": "assistant", "content": "好的，记住了！"},
                ],
                "allen", "session1",
            )

        # Query should include the memory
        context = mgr.query("喝什么", "allen")
        assert "拿铁" in context
        assert "<memory>" in context

    def test_save_then_direct_answer(self, setup):
        """After saving, DirectAnswerer should be able to answer related queries."""
        mgr, answerer, blog = setup

        # 直接插入一条记忆（跳过 LLM 提取）
        content = "Allen 喜欢拿铁"
        emb = _deterministic_encode(content)
        mgr.store.add_memory(
            user_id="allen", content=content,
            category="preference", key="favorite_drink",
            importance=8.0, embedding=emb,
        )

        # 相同文本查询应该命中
        result = answerer.try_answer(content, "allen")
        assert result is not None
        assert "拿铁" in result

    def test_correction_supersedes_old_memory(self, setup):
        """Saving a correction should deactivate the old memory."""
        mgr, answerer, blog = setup

        # 先存旧记忆
        old_emb = _deterministic_encode("Allen 喜欢拿铁")
        mgr.store.add_memory(
            user_id="allen", content="Allen 喜欢拿铁",
            category="preference", key="favorite_drink",
            importance=7.0, embedding=old_emb,
        )

        # 模拟修正提取
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
                "new_content": "喜欢美式",
                "reason": "用户纠正",
            }],
            "profile_update": None,
            "episode_summary": "Allen 纠正饮品偏好为美式",
            "mood": "neutral",
            "topics": ["偏好修正"],
        }

        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [{"role": "user", "content": "不对，我喜欢美式不是拿铁"}],
                "allen", "session2",
            )

        active = mgr.store.get_active_memories("allen")
        contents = [m["content"] for m in active]
        assert any("美式" in c for c in contents)
        assert not any(c == "Allen 喜欢拿铁" for c in contents)

    def test_behavior_log_records(self, setup):
        """BehaviorLog should record events correctly."""
        mgr, answerer, blog = setup
        blog.log("allen", "skill_call", {"skill": "weather"})
        blog.log("allen", "conversation", {"text": "今天天气", "route": "cloud"})
        events = blog.get_events("allen")
        assert len(events) == 2

    def test_profile_rebuilds_from_memories(self, setup):
        """Profile should auto-rebuild when preference memories are saved."""
        mgr, answerer, blog = setup

        extraction = {
            "memories": [{
                "content": "Allen 喜欢拿铁",
                "category": "preference",
                "key": "favorite_drink",
                "importance": 8,
                "tags": [],
                "time_ref": None,
                "expires": None,
            }],
            "corrections": [],
            "profile_update": None,
            "episode_summary": "聊了饮品偏好",
            "mood": "neutral",
            "topics": [],
        }

        with patch.object(mgr, "_call_openai_json", return_value=extraction):
            mgr.save(
                [{"role": "user", "content": "我喜欢拿铁"}],
                "allen", "session1",
            )

        profile = mgr.store.get_profile("allen")
        assert profile is not None
```

- [ ] **Step 2: 跑集成测试**

Run: `python -m pytest tests/test_memory_e2e.py -v`
Expected: 全部 PASS（如果之前的 Task 都实现了）

- [ ] **Step 3: 跑全量测试确认无回归**

Run: `python -m pytest tests/ -q`
Expected: 不新增失败

- [ ] **Step 4: Commit**

```bash
git add tests/test_memory_e2e.py
git commit -m "Add end-to-end memory integration tests"
```

---

## Task 12: 最终验证 + 清理

- [ ] **Step 1: 跑全量测试**

Run: `python -m pytest tests/ -v`
Expected: 所有新增测试 PASS，原有失败不增加

- [ ] **Step 2: Ruff lint 检查**

Run: `ruff check memory/ core/personality.py tests/test_memory_e2e.py tests/test_behavior_log.py tests/test_direct_answer.py`

- [ ] **Step 3: 确认文件结构**

新增文件：
```
memory/behavior_log.py      — 行为日志收集器
memory/direct_answer.py     — Level 1 直接回答引擎
tests/test_behavior_log.py  — 行为日志测试
tests/test_direct_answer.py — 直接回答测试
tests/test_memory_e2e.py    — 端到端集成测试
```

修改文件：
```
core/personality.py          — 删除 preferences 参数
core/llm.py                  — 确认无影响
memory/manager.py            — 注入量控制 + correction + 使用指引
jarvis.py                    — BehaviorLog + DirectAnswerer + 对话结束 save + maintenance 每天
ui/dashboard.py              — 记忆统计面板
tests/test_memory_manager.py — 新增测试
tests/test_personality.py    — 更新测试
```

- [ ] **Step 4: 最终 commit（如果有遗漏文件）**

```bash
git add -A
git commit -m "T0 complete: memory system reliability — pipeline, quality, injection, ops"
```
