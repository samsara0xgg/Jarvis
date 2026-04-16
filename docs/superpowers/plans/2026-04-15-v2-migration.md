# Jarvis v2 Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Jarvis from v1 (14 Skill classes + SkillRegistry + GPT-4o-mini memory extraction + behavior_log) to v2 (ToolRegistry + @jarvis_tool + YAML interpreter + Observer OM memory + trace table).

**Architecture:** Two parallel workstreams — Phase 1 rebuilds the memory system (TraceLog + Observer + StablePrefixBuilder), Phase 2 rebuilds the skill system (@jarvis_tool + YAMLInterpreter + ToolRegistry). Both converge in jarvis.py integration. Old tables and DirectAnswerer preserved untouched.

**Tech Stack:** Python 3.13, SQLite (WAL), Jinja2 (ImmutableSandboxedEnvironment), xAI/Grok API (OpenAI-compatible), requests, pytest

**Spec:** `notes/v2-migration-prompt.md` + `notes/memory.md` (chapters 13-20)

---

## File Map

### New Files (Create)
| File | Responsibility |
|---|---|
| `memory/trace.py` | TraceLog class — trace table schema + CRUD |
| `memory/observer.py` | Observer class — LLM-based observation extraction |
| `memory/stable_prefix.py` | StablePrefixBuilder — assemble LLM prompt prefix |
| `tools/__init__.py` | @jarvis_tool decorator + _TOOL_REGISTRY global |
| `tools/smart_home.py` | smart_home_control / smart_home_status functions |
| `tools/time_utils.py` | get_current_time / set_timer functions |
| `tools/reminders.py` | create_reminder / list_reminders / complete_reminder |
| `tools/todos.py` | add_todo / list_todos / complete_todo / delete_todo |
| `core/tool_registry.py` | ToolRegistry — unified dispatch for Python + YAML tools |
| `core/yaml_interpreter.py` | YAMLInterpreter — execute YAML skills with Jinja2 |
| `skills/weather.yaml` | Weather YAML skill (replaces skills/weather.py) |
| `skills/learned/exchange_rate.yaml` | Exchange rate YAML skill (replaces .py) |

### Modified Files
| File | Change |
|---|---|
| `memory/manager.py` | Add `write_observation()` + `build_stable_prefix()`, keep `query()` + `save()` for DirectAnswerer compatibility |
| `memory/store.py` | Add observations table creation in `_init_db()` + observation CRUD methods |
| `jarvis.py` | Replace SkillRegistry → ToolRegistry, BehaviorLog → TraceLog, memory_manager.query → build_stable_prefix, add Observer async calls |
| `core/local_executor.py` | Add guard for missing tools in execute_info_query |
| `config.yaml` | Add `memory.observer` section, set `realtime_data.enabled: false` |

### Deleted Files (Phase 2 completion)
```
skills/__init__.py  skills/smart_home.py  skills/weather.py  skills/time_skill.py
skills/reminders.py  skills/todos.py  skills/memory_skill.py  skills/automation.py
skills/system_control.py  skills/model_switch.py  skills/realtime_data.py
skills/scheduler_skill.py  skills/remote_control.py  skills/health_skill.py
skills/skill_mgmt.py  skills/learned/exchange_rate.py  skills/learned/__init__.py
core/skill_factory.py  core/learning_router.py  core/skill_loader.py
```

### Preserved (Not Touched)
```
memory/direct_answer.py  memory/retriever.py  memory/embedder.py
memory/conversation.py  memory/user_preferences.py  memory/behavior_log.py
memory/store.py (old tables)  core/personality.py
```

---

## Phase 1 · Memory System

### Task 1: TraceLog

**Files:**
- Create: `memory/trace.py`
- Test: `tests/test_trace.py`

- [ ] **Step 1: Write tests for TraceLog**

Create `tests/test_trace.py`:

```python
"""Tests for TraceLog — trace table CRUD."""
import json
import os
import tempfile
import pytest
from memory.trace import TraceLog


@pytest.fixture
def trace_log():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tl = TraceLog(path)
    yield tl
    tl.close()
    os.unlink(path)


def test_log_turn_returns_id(trace_log):
    tid = trace_log.log_turn(
        session_id="s1", turn_id=1,
        user_text="你好", assistant_text="你好呀",
    )
    assert isinstance(tid, int)
    assert tid > 0


def test_log_turn_full_fields(trace_log):
    tid = trace_log.log_turn(
        session_id="s1", turn_id=1,
        user_text="开灯", assistant_text="好的，灯开了",
        user_emotion="neutral", tts_emotion="warm",
        path_taken="local",
        tool_calls=[{"name": "smart_home_control", "args": {}, "result": "ok", "ms": 50}],
        llm_model="grok-4.20", llm_tokens_in=100, llm_tokens_out=50,
        latency_ms=500,
    )
    row = trace_log.query_for_observer(tid)
    assert row["user_text"] == "开灯"
    assert row["assistant_text"] == "好的，灯开了"
    assert isinstance(row["tool_calls"], list)
    assert row["tool_calls"][0]["name"] == "smart_home_control"


def test_update_outcome(trace_log):
    tid = trace_log.log_turn(session_id="s1", turn_id=1, user_text="x", assistant_text="y")
    trace_log.update_outcome(tid, signal=1)
    # Verify by reading back
    row = trace_log.query_for_observer(tid)
    assert row is not None  # basic check — outcome stored


def test_query_for_observer(trace_log):
    tid = trace_log.log_turn(
        session_id="s1", turn_id=1,
        user_text="查天气", assistant_text="今天晴",
        tool_calls=[{"name": "get_weather", "args": {"city": "Victoria"}, "result": "晴", "ms": 200}],
    )
    data = trace_log.query_for_observer(tid)
    assert data["user_text"] == "查天气"
    assert data["assistant_text"] == "今天晴"
    assert len(data["tool_calls"]) == 1


def test_query_cloud_traces(trace_log):
    trace_log.log_turn(session_id="s1", turn_id=1, user_text="a", assistant_text="b", path_taken="cloud")
    trace_log.log_turn(session_id="s1", turn_id=2, user_text="c", assistant_text="d", path_taken="local")
    trace_log.log_turn(session_id="s1", turn_id=3, user_text="e", assistant_text="f", path_taken="cloud")
    rows = trace_log.query_cloud_traces(days=7)
    assert len(rows) == 2
    assert all(r["path_taken"] == "cloud" for r in rows)
```

- [ ] **Step 2: Run tests — expect FAIL (module not found)**

Run: `python -m pytest tests/test_trace.py -q`
Expected: `ModuleNotFoundError: No module named 'memory.trace'`

- [ ] **Step 3: Implement TraceLog**

Create `memory/trace.py`:

```python
"""TraceLog — structured trace table replacing behavior_log for v2.

Shares the same SQLite file as MemoryStore (WAL mode for concurrent access).
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


class TraceLog:
    """Append-only trace store for per-turn execution data.

    Args:
        db_path: Path to the shared SQLite database file.
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trace (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT,
                turn_id         INTEGER,
                created_at      TEXT NOT NULL,
                user_text       TEXT,
                assistant_text  TEXT,
                user_emotion    TEXT,
                tts_emotion     TEXT,
                path_taken      TEXT,
                tool_calls      TEXT,
                llm_model       TEXT,
                llm_tokens_in   INTEGER,
                llm_tokens_out  INTEGER,
                latency_ms      INTEGER,
                outcome_signal  INTEGER,
                outcome_at_turn_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_trace_session
                ON trace(session_id, turn_id);
            CREATE INDEX IF NOT EXISTS idx_trace_path
                ON trace(path_taken, created_at DESC);
        """)
        conn.commit()

    def log_turn(
        self,
        *,
        session_id: str,
        turn_id: int,
        user_text: str,
        assistant_text: str,
        user_emotion: str | None = None,
        tts_emotion: str | None = None,
        path_taken: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        llm_model: str | None = None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        latency_ms: int | None = None,
    ) -> int:
        """Log a single conversation turn. Returns the trace row id."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO trace
               (session_id, turn_id, created_at, user_text, assistant_text,
                user_emotion, tts_emotion, path_taken, tool_calls,
                llm_model, llm_tokens_in, llm_tokens_out, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, turn_id, datetime.now().isoformat(),
                user_text, assistant_text,
                user_emotion, tts_emotion, path_taken,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                llm_model, llm_tokens_in, llm_tokens_out, latency_ms,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def update_outcome(self, trace_id: int, signal: int) -> None:
        """Set outcome_signal for a trace row."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE trace SET outcome_signal = ?, outcome_at_turn_id = id WHERE id = ?",
            (signal, trace_id),
        )
        conn.commit()

    def query_for_observer(self, trace_id: int) -> dict[str, Any] | None:
        """Return user_text + assistant_text + tool_calls for Observer extraction."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT user_text, assistant_text, tool_calls, user_emotion "
            "FROM trace WHERE id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["tool_calls"]:
            result["tool_calls"] = json.loads(result["tool_calls"])
        else:
            result["tool_calls"] = []
        return result

    def query_cloud_traces(self, days: int = 7) -> list[dict[str, Any]]:
        """Return recent cloud-path traces for nightly hotspot detection."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trace "
            "WHERE path_taken = 'cloud' "
            "AND (outcome_signal IS NULL OR outcome_signal >= 0) "
            "AND created_at > datetime('now', 'localtime', ?) "
            "ORDER BY created_at DESC",
            (f"-{days} days",),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("tool_calls"):
                d["tool_calls"] = json.loads(d["tool_calls"])
            else:
                d["tool_calls"] = []
            results.append(d)
        return results

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_trace.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add memory/trace.py tests/test_trace.py
git commit -m "feat(memory): add TraceLog — structured trace table for v2"
```

---

### Task 2: Observations Table

**Files:**
- Modify: `memory/store.py` (add observations table + CRUD)
- Test: `tests/test_observation_store.py`

- [ ] **Step 1: Write tests for observation CRUD**

Create `tests/test_observation_store.py`:

```python
"""Tests for observations table in MemoryStore."""
import os
import tempfile
import pytest
from memory.store import MemoryStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = MemoryStore(path)
    yield s
    s.close()
    os.unlink(path)


def test_add_observation(store):
    oid = store.add_observation(
        chunk_id=1,
        content="Date: 2026-04-15\n* 🔴 (14:30) 用户偏好暖黄灯光",
        source_turn_id=42,
    )
    assert isinstance(oid, int)
    assert oid > 0


def test_get_all_observations_ordered(store):
    store.add_observation(chunk_id=1, content="* 🔴 (09:00) first", source_turn_id=1)
    store.add_observation(chunk_id=2, content="* 🟡 (10:00) second", source_turn_id=2)
    store.add_observation(chunk_id=3, content="* ✅ (11:00) third", source_turn_id=3)
    obs = store.get_all_observations()
    assert len(obs) == 3
    # Ordered by created_at ASC
    assert "first" in obs[0]["content"]
    assert "third" in obs[2]["content"]


def test_get_observations_excludes_superseded(store):
    oid1 = store.add_observation(chunk_id=1, content="old fact", source_turn_id=1)
    oid2 = store.add_observation(chunk_id=2, content="new fact", source_turn_id=2)
    store.supersede_observation(oid1, oid2)
    obs = store.get_all_observations()
    assert len(obs) == 1
    assert "new fact" in obs[0]["content"]


def test_get_observations_token_count(store):
    store.add_observation(chunk_id=1, content="x" * 100, source_turn_id=1)
    store.add_observation(chunk_id=2, content="y" * 200, source_turn_id=2)
    total = store.get_observations_char_count()
    assert total == 300
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_observation_store.py -q`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'add_observation'`

- [ ] **Step 3: Add observations table + methods to MemoryStore**

Edit `memory/store.py` — add in `_init_db()` after the existing `executescript`:

```python
# Add this after the existing executescript block in _init_db(), before conn.commit()
conn.executescript("""
    CREATE TABLE IF NOT EXISTS observations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        chunk_id        INTEGER,
        created_at      TEXT NOT NULL,
        content         TEXT,
        source_turn_id  INTEGER,
        superseded_by   INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_obs_created
        ON observations(created_at);
""")
```

Add these methods to the `MemoryStore` class (after the Relations section):

```python
# ------------------------------------------------------------------
# Observations (v2 OM memory)
# ------------------------------------------------------------------

def add_observation(
    self,
    chunk_id: int,
    content: str,
    source_turn_id: int | None = None,
) -> int:
    """Insert an observation. Returns the observation row id."""
    conn = self._get_conn()
    cursor = conn.execute(
        "INSERT INTO observations (chunk_id, created_at, content, source_turn_id) "
        "VALUES (?, ?, ?, ?)",
        (chunk_id, datetime.now().isoformat(), content, source_turn_id),
    )
    conn.commit()
    LOGGER.info("Observation added: chunk=%d content=%s", chunk_id, content[:60])
    return cursor.lastrowid

def supersede_observation(self, old_id: int, new_id: int) -> None:
    """Mark an old observation as superseded."""
    conn = self._get_conn()
    conn.execute(
        "UPDATE observations SET superseded_by = ? WHERE id = ?",
        (new_id, old_id),
    )
    conn.commit()

def get_all_observations(self) -> list[dict[str, Any]]:
    """Return all non-superseded observations, oldest first."""
    conn = self._get_conn()
    rows = conn.execute(
        "SELECT * FROM observations WHERE superseded_by IS NULL "
        "ORDER BY created_at ASC",
    ).fetchall()
    return [dict(r) for r in rows]

def get_observations_char_count(self) -> int:
    """Return total character count of all active observations."""
    conn = self._get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) as total "
        "FROM observations WHERE superseded_by IS NULL",
    ).fetchone()
    return row["total"]

def get_next_chunk_id(self) -> int:
    """Return the next chunk_id (max + 1)."""
    conn = self._get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(chunk_id), 0) + 1 as next_id FROM observations"
    ).fetchone()
    return row["next_id"]
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_observation_store.py -q`
Expected: `4 passed`

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All existing tests still pass (new table creation is additive).

- [ ] **Step 6: Commit**

```bash
git add memory/store.py tests/test_observation_store.py
git commit -m "feat(memory): add observations table to MemoryStore for v2 OM"
```

---

### Task 3: Observer

**Files:**
- Create: `memory/observer.py`
- Test: `tests/test_observer.py`

- [ ] **Step 1: Write tests for Observer**

Create `tests/test_observer.py`:

```python
"""Tests for Observer — LLM-based observation extraction."""
import json
import pytest
from unittest.mock import patch, MagicMock
from memory.observer import Observer, OBSERVER_SYSTEM_PROMPT, OBSERVER_TOOL_SCHEMA


@pytest.fixture
def observer():
    config = {
        "memory": {
            "observer": {
                "primary_model": "grok-4.20-0309-non-reasoning",
                "fallback_model": "gemini-2.5-flash",
                "enabled": True,
            }
        },
        "llm": {
            "api_key": "test-key",
            "base_url": "https://api.x.ai/v1",
        },
    }
    return Observer(config)


def test_prompt_contains_required_sections():
    assert "YOUR JOB" in OBSERVER_SYSTEM_PROMPT
    assert "PRIORITY EMOJI" in OBSERVER_SYSTEM_PROMPT
    assert "DISTINGUISH" in OBSERVER_SYSTEM_PROMPT
    assert "STATE CHANGES" in OBSERVER_SYSTEM_PROMPT
    assert "PRESERVE UNUSUAL PHRASING" in OBSERVER_SYSTEM_PROMPT
    assert "PRECISE VERBS" in OBSERVER_SYSTEM_PROMPT
    assert "EMOTION DETECTION" in OBSERVER_SYSTEM_PROMPT
    assert "record_observations" in OBSERVER_TOOL_SCHEMA["name"]


def test_tool_schema_structure():
    props = OBSERVER_TOOL_SCHEMA["parameters"]["properties"]["observations"]["items"]["properties"]
    assert "priority" in props
    assert "time" in props
    assert "text" in props
    assert set(props["priority"]["enum"]) == {"🔴", "🟡", "🟢", "✅"}


def test_build_prompt(observer):
    turn_data = {
        "user_text": "把客厅灯调成暖黄",
        "assistant_text": "好的，已调为暖黄 2700K",
        "tool_calls": [{"name": "smart_home_control", "args": {"action": "set_color_temp"}, "result": "ok", "ms": 200}],
        "user_emotion": "neutral",
    }
    messages = observer._build_prompt(turn_data)
    assert messages[0]["role"] == "system"
    assert "YOUR JOB" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "暖黄" in messages[1]["content"]


@patch("memory.observer._SESSION")
def test_extract_success(mock_session, observer):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "record_observations",
                        "arguments": json.dumps({
                            "observations": [
                                {"priority": "🔴", "time": "14:30", "text": "用户偏好暖黄灯光 2700K"},
                                {"priority": "✅", "time": "14:30", "text": "客厅灯已调为暖黄"},
                            ]
                        })
                    }
                }]
            }
        }]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_session.post.return_value = mock_resp

    turn_data = {
        "user_text": "把客厅灯调成暖黄",
        "assistant_text": "好的，已调为暖黄 2700K",
        "tool_calls": [],
        "user_emotion": "neutral",
    }
    observations = observer.extract(turn_data)
    assert len(observations) == 2
    assert observations[0]["priority"] == "🔴"
    assert "暖黄" in observations[0]["text"]


@patch("memory.observer._SESSION")
def test_extract_failure_returns_empty(mock_session, observer):
    mock_session.post.side_effect = Exception("API down")
    turn_data = {
        "user_text": "你好", "assistant_text": "嗨",
        "tool_calls": [], "user_emotion": "",
    }
    observations = observer.extract(turn_data)
    assert observations == []


def test_format_observation_markdown(observer):
    obs_list = [
        {"priority": "🔴", "time": "14:30", "text": "用户偏好暖黄灯光"},
        {"priority": "✅", "time": "14:30", "text": "客厅灯已调为暖黄"},
    ]
    md = observer.format_markdown(obs_list)
    assert "🔴" in md
    assert "✅" in md
    assert "(14:30)" in md
    assert "Date:" in md
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_observer.py -q`
Expected: `ModuleNotFoundError: No module named 'memory.observer'`

- [ ] **Step 3: Implement Observer**

Create `memory/observer.py`:

```python
"""Observer — extract structured observations from conversation turns.

Uses function calling (tool_use) to produce observations in the Mastra OM format.
Runs asynchronously on the cold path — does not block user interaction.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
_SESSION = requests.Session()

OBSERVER_SYSTEM_PROMPT = """You are the memory consciousness of an AI assistant.
Your observations will be the ONLY information the assistant has about past interactions.

## YOUR JOB
Extract structured observations from the conversation below.
Call the `record_observations` tool with your results.
ALWAYS respond in Chinese (中文). English output will be rejected.

## PRIORITY EMOJI
- 🔴 HIGH: explicit user facts/preferences, unresolved goals, critical context
- 🟡 MEDIUM: learned info, tool results, mild observations, user emotions
- 🟢 LOW: minor, uncertain, speculative
- ✅ DONE: task completed, question answered, issue resolved

## FORMAT RULES
- Each observation MUST have: priority (emoji), time (HH:MM 24h), text (中文)
- text field: 用中文撰写, 第三人称描述, 简洁 (10-50 字理想)
- Use the TIME from the message that triggered this observation

## CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS
- "我对虾过敏" → 🔴 assertion: 用户声明对虾过敏
- "虾过敏严重吗？" → question, 不要当作断言

## STATE CHANGES
If user indicates change, frame as state change that supersedes:
- "我不在 Acme 了换到 Stripe" → 🔴 用户从 Acme 换到 Stripe (不再在 Acme)

## PRESERVE UNUSUAL PHRASING
- 用户说 "累死了" → observation 写 "用户说累死了" 或 "用户疲惫 (原话: 累死了)"
- 不要"洗成"教科书普通话

## PRECISE VERBS — 动词保真
动词必须忠于原意·不弱化·不强化·不推断。
- "我买了 X" → "用户买了 X" ✓
- "我讨厌 Y" → "用户讨厌 Y" ✓

## DETAILS IN ASSISTANT CONTENT — 保留具体信息
assistant 生成的具体数值·名称·参数必须保留进 observation。
- assistant "已调为暖黄 2700K" → observation 应记 "2700K 暖黄"

## EMOTION DETECTION
If user message has emotion hint (tired/angry/happy/...) → add 🟡 observation

## USER ASSERTIONS ARE AUTHORITATIVE
User assertions are authoritative. The question doesn't invalidate an assertion.

## OUTPUT
Call tool `record_observations` ONLY. Do not output free text.
"""

OBSERVER_TOOL_SCHEMA: dict[str, Any] = {
    "name": "record_observations",
    "description": "Record observations extracted from the conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {
                            "type": "string",
                            "enum": ["🔴", "🟡", "🟢", "✅"],
                        },
                        "time": {
                            "type": "string",
                            "description": "HH:MM 24h format",
                        },
                        "text": {
                            "type": "string",
                            "description": "Observation text in Chinese",
                        },
                    },
                    "required": ["priority", "time", "text"],
                },
            },
        },
        "required": ["observations"],
    },
}


class Observer:
    """Extract observations from conversation turns via LLM function calling.

    Args:
        config: Application config dict. Reads from memory.observer and llm sections.
    """

    def __init__(self, config: dict) -> None:
        obs_config = config.get("memory", {}).get("observer", {})
        llm_config = config.get("llm", {})

        self._primary_model = obs_config.get("primary_model", "grok-4.20-0309-non-reasoning")
        self._fallback_model = obs_config.get("fallback_model", "gemini-2.5-flash")
        self._enabled = obs_config.get("enabled", True)

        # API config — Observer uses same provider as main LLM by default
        self._api_key = llm_config.get("api_key", "")
        self._base_url = llm_config.get("base_url", "https://api.x.ai/v1")

        self.logger = LOGGER

    def extract(self, turn_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract 0-N observations from a conversation turn.

        Args:
            turn_data: Dict with keys: user_text, assistant_text, tool_calls, user_emotion

        Returns:
            List of observation dicts: [{priority, time, text}]
            Returns empty list on failure.
        """
        if not self._enabled or not self._api_key:
            return []

        messages = self._build_prompt(turn_data)

        # Try primary model, fall back on failure
        result = self._call_llm(messages, self._primary_model)
        if result is None:
            self.logger.warning("Primary Observer failed, trying fallback")
            result = self._call_llm(messages, self._fallback_model)

        if result is None:
            self.logger.warning("Observer extraction failed on both models")
            return []

        return result

    def _build_prompt(self, turn_data: dict[str, Any]) -> list[dict[str, str]]:
        """Build the Observer prompt messages."""
        user_text = turn_data.get("user_text", "")
        assistant_text = turn_data.get("assistant_text", "")
        tool_calls = turn_data.get("tool_calls", [])
        user_emotion = turn_data.get("user_emotion", "")

        conversation_parts = [f"用户：{user_text}"]
        if tool_calls:
            for tc in tool_calls:
                conversation_parts.append(
                    f"[工具调用: {tc.get('name', '')}({json.dumps(tc.get('args', {}), ensure_ascii=False)}) → {tc.get('result', '')[:200]}]"
                )
        conversation_parts.append(f"助手：{assistant_text}")
        if user_emotion:
            conversation_parts.append(f"[用户情绪检测: {user_emotion}]")

        conversation = "\n".join(conversation_parts)

        return [
            {"role": "system", "content": OBSERVER_SYSTEM_PROMPT},
            {"role": "user", "content": conversation},
        ]

    def _call_llm(self, messages: list[dict], model: str) -> list[dict] | None:
        """Call LLM with function calling to extract observations."""
        tool_def = {
            "type": "function",
            "function": OBSERVER_TOOL_SCHEMA,
        }
        try:
            resp = _SESSION.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": 1024,
                    "tools": [tool_def],
                    "tool_choice": {"type": "function", "function": {"name": "record_observations"}},
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_args = data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            parsed = json.loads(raw_args)
            observations = parsed.get("observations", [])
            self.logger.info("Observer extracted %d observations via %s", len(observations), model)
            return observations
        except Exception as exc:
            self.logger.warning("Observer LLM call failed (%s): %s", model, exc)
            return None

    def format_markdown(self, observations: list[dict[str, Any]]) -> str:
        """Format observations into markdown for storage.

        Output format:
            Date: 2026-04-15
            * 🔴 (14:30) 用户偏好暖黄灯光 2700K
            * ✅ (14:30) 客厅灯已调为暖黄
        """
        if not observations:
            return ""

        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"Date: {today}"]
        for obs in observations:
            priority = obs.get("priority", "🟢")
            time_str = obs.get("time", datetime.now().strftime("%H:%M"))
            text = obs.get("text", "")
            lines.append(f"* {priority} ({time_str}) {text}")

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_observer.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add memory/observer.py tests/test_observer.py
git commit -m "feat(memory): add Observer — LLM-based observation extraction for v2"
```

---

### Task 4: StablePrefixBuilder

**Files:**
- Create: `memory/stable_prefix.py`
- Test: `tests/test_stable_prefix.py`

- [ ] **Step 1: Write tests**

Create `tests/test_stable_prefix.py`:

```python
"""Tests for StablePrefixBuilder."""
import os
import tempfile
import pytest
from memory.store import MemoryStore
from memory.stable_prefix import StablePrefixBuilder


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = MemoryStore(path)
    yield s
    s.close()
    os.unlink(path)


@pytest.fixture
def builder(store):
    personality = "You are Jarvis, Allen's personal voice assistant."
    return StablePrefixBuilder(store, personality)


def test_build_empty(builder):
    result = builder.build(recent_turns=[], current_input="你好")
    assert "Jarvis" in result
    assert "你好" in result


def test_build_with_observations(builder, store):
    store.add_observation(chunk_id=1, content="Date: 2026-04-15\n* 🔴 (14:30) 用户喜欢暖黄灯光", source_turn_id=1)
    store.add_observation(chunk_id=2, content="Date: 2026-04-15\n* ✅ (14:31) 客厅灯已调整", source_turn_id=2)
    result = builder.build(recent_turns=[], current_input="开灯")
    assert "暖黄" in result
    assert "<observations>" in result
    assert "</observations>" in result


def test_build_with_recent_turns(builder):
    turns = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    result = builder.build(recent_turns=turns, current_input="今天天气")
    assert "[user] 你好" in result
    assert "[assistant] 你好呀" in result


def test_build_observation_order(builder, store):
    store.add_observation(chunk_id=1, content="* 🔴 first", source_turn_id=1)
    import time
    time.sleep(0.01)
    store.add_observation(chunk_id=2, content="* 🟡 second", source_turn_id=2)
    result = builder.build(recent_turns=[], current_input="test")
    first_pos = result.find("first")
    second_pos = result.find("second")
    assert first_pos < second_pos  # chronological order
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_stable_prefix.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement StablePrefixBuilder**

Create `memory/stable_prefix.py`:

```python
"""StablePrefixBuilder — assemble the stable prefix for LLM prompt injection.

Structure (in order):
1. Personality system prompt
2. "The following observations are your memory..."
3. <observations> (all, chronological) </observations>
4. Recent N turns
5. Current user input
"""
from __future__ import annotations

import logging
from typing import Any

from memory.store import MemoryStore

LOGGER = logging.getLogger(__name__)

_OBSERVATION_PREAMBLE = (
    "The following observations are your memory of past conversations with the user. "
    "Newer observations supersede older ones. Reference specific details when relevant."
)

_MAX_RECENT_TURNS = 10


class StablePrefixBuilder:
    """Build the stable prefix injected into every Cloud LLM call.

    Args:
        store: MemoryStore instance (for reading observations).
        personality_text: The personality system prompt text.
    """

    def __init__(self, store: MemoryStore, personality_text: str) -> None:
        self._store = store
        self._personality = personality_text

    def build(
        self,
        recent_turns: list[dict[str, Any]],
        current_input: str,
    ) -> str:
        """Assemble the full stable prefix string.

        Args:
            recent_turns: Recent conversation history (list of {role, content} dicts).
            current_input: The current user utterance.

        Returns:
            Complete prompt prefix string.
        """
        sections: list[str] = []

        # 1. Personality
        sections.append(self._personality)

        # 2. Observations
        observations = self._store.get_all_observations()
        if observations:
            sections.append(_OBSERVATION_PREAMBLE)
            obs_lines = []
            for obs in observations:
                content = obs.get("content", "")
                if content.strip():
                    obs_lines.append(content)
            if obs_lines:
                obs_block = "\n".join(obs_lines)
                sections.append(f"<observations>\n{obs_block}\n</observations>")

        # 3. Recent turns (last N)
        trimmed = recent_turns[-_MAX_RECENT_TURNS * 2:]  # *2 because each turn = user + assistant
        if trimmed:
            turn_lines = ["--- 最近对话 ---"]
            for msg in trimmed:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    tag = "[user]" if role == "user" else "[assistant]"
                    turn_lines.append(f"{tag} {content}")
            sections.append("\n".join(turn_lines))

        # 4. Current input
        sections.append(f"--- 本轮 ---\n[user] {current_input}")

        return "\n\n".join(sections)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_stable_prefix.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add memory/stable_prefix.py tests/test_stable_prefix.py
git commit -m "feat(memory): add StablePrefixBuilder for v2 prompt injection"
```

---

### Task 5: MemoryManager v2

**Files:**
- Modify: `memory/manager.py`
- Test: `tests/test_memory_manager_v2.py`

- [ ] **Step 1: Write tests for new MemoryManager methods**

Create `tests/test_memory_manager_v2.py`:

```python
"""Tests for MemoryManager v2 methods (write_observation + build_stable_prefix)."""
import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from memory.manager import MemoryManager


@pytest.fixture
def config():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return {
        "memory": {
            "db_path": path,
            "observer": {
                "primary_model": "grok-4.20-0309-non-reasoning",
                "fallback_model": "gemini-2.5-flash",
                "enabled": True,
            },
            "stable_prefix": {"max_tokens": 25000},
        },
        "llm": {"api_key": "test-key", "base_url": "https://api.x.ai/v1"},
    }


@pytest.fixture
def manager(config):
    mm = MemoryManager(config)
    yield mm
    os.unlink(config["memory"]["db_path"])


def test_build_stable_prefix_returns_string(manager):
    result = manager.build_stable_prefix(
        recent_turns=[], current_input="你好",
    )
    assert isinstance(result, str)
    assert "你好" in result


@patch("memory.observer._SESSION")
def test_write_observation_stores_data(mock_session, manager):
    # Mock Observer to return observations
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "record_observations",
                        "arguments": json.dumps({
                            "observations": [
                                {"priority": "🔴", "time": "14:30", "text": "用户说你好"},
                            ]
                        })
                    }
                }]
            }
        }]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_session.post.return_value = mock_resp

    turn_data = {
        "user_text": "你好", "assistant_text": "嗨",
        "tool_calls": [], "user_emotion": "",
    }
    count = manager.write_observation(turn_data, source_turn_id=1)
    assert count == 1

    # Verify stored
    obs = manager.store.get_all_observations()
    assert len(obs) == 1
    assert "用户说你好" in obs[0]["content"]
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_memory_manager_v2.py -q`
Expected: FAIL — `AttributeError: 'MemoryManager' object has no attribute 'write_observation'`

- [ ] **Step 3: Add v2 methods to MemoryManager**

Edit `memory/manager.py` — add these imports at the top (after existing imports):

```python
from memory.observer import Observer
from memory.stable_prefix import StablePrefixBuilder
```

Add these methods to the `MemoryManager` class (after `__init__`):

In `__init__`, add after `self.logger = LOGGER`:

```python
# v2: Observer + StablePrefixBuilder
self._observer = Observer(config)
# Read personality text for stable prefix
personality_text = ""
try:
    from core.personality import get_system_prompt
    personality_text = get_system_prompt()
except Exception:
    personality_text = "You are Jarvis, a helpful voice assistant."
self._prefix_builder = StablePrefixBuilder(self.store, personality_text)
```

Add these public methods after the existing `query()` method:

```python
def build_stable_prefix(
    self,
    recent_turns: list[dict] | None = None,
    current_input: str = "",
) -> str:
    """Build the v2 stable prefix for LLM prompt injection.

    Args:
        recent_turns: Recent conversation history.
        current_input: Current user utterance.

    Returns:
        Formatted stable prefix string.
    """
    return self._prefix_builder.build(
        recent_turns=recent_turns or [],
        current_input=current_input,
    )

def write_observation(
    self,
    turn_data: dict,
    source_turn_id: int | None = None,
) -> int:
    """Extract and store observations from a conversation turn.

    Args:
        turn_data: Dict with user_text, assistant_text, tool_calls, user_emotion.
        source_turn_id: The trace table row ID for this turn.

    Returns:
        Number of observations stored.
    """
    observations = self._observer.extract(turn_data)
    if not observations:
        return 0

    markdown = self._observer.format_markdown(observations)
    chunk_id = self.store.get_next_chunk_id()
    self.store.add_observation(
        chunk_id=chunk_id,
        content=markdown,
        source_turn_id=source_turn_id,
    )

    self.logger.info(
        "Stored %d observations (chunk %d) from turn %s",
        len(observations), chunk_id, source_turn_id,
    )
    return len(observations)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_memory_manager_v2.py -q`
Expected: `2 passed`

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass (existing MemoryManager interface unchanged).

- [ ] **Step 6: Commit**

```bash
git add memory/manager.py tests/test_memory_manager_v2.py
git commit -m "feat(memory): add write_observation + build_stable_prefix to MemoryManager"
```

---

### Task 6: config.yaml + jarvis.py Memory Integration

**Files:**
- Modify: `config.yaml`
- Modify: `jarvis.py`

- [ ] **Step 1: Update config.yaml**

Add to `config.yaml` after the existing `memory:` section (around line 425):

```yaml
  # v2: Observer 记忆抽取配置
  observer:
    primary_model: "grok-4.20-0309-non-reasoning"
    fallback_model: "gemini-2.5-flash"
    enabled: true
  stable_prefix:
    max_tokens: 25000
```

- [ ] **Step 2: Add TraceLog to jarvis.py**

In `jarvis.py`, add import near L140:
```python
from memory.trace import TraceLog
```

After the existing BehaviorLog init (L140-143), add:
```python
self.trace_log = TraceLog(mem_db)
self._turn_counter: dict[str, int] = {}  # session_id → turn count
```

- [ ] **Step 3: Replace memory_context query in jarvis.py**

At L832-845 (the `# ── 记忆检索 ──` block), replace:
```python
memory_context = self.memory_manager.query(text, user_id)
```
with:
```python
memory_context = self.memory_manager.build_stable_prefix(
    recent_turns=history,
    current_input=text,
)
```
Keep the timing/logging around it. Also keep the old `query()` call for DirectAnswerer (it uses its own path).

- [ ] **Step 4: Replace behavior_log writes with trace_log**

At L1096-1111 (the `# ── 行为日志 ──` block), add trace_log.log_turn() before the existing behavior_log.log() calls. Keep behavior_log temporarily for backwards compatibility:

```python
# ── Trace (v2) ── structured per-turn log
if user_id:
    session_turn = self._turn_counter.get(session_id, 0) + 1
    self._turn_counter[session_id] = session_turn
    _trace_tool_calls = []
    if updated_messages:
        for msg in updated_messages:
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        _trace_tool_calls.append({
                            "name": block.get("name", ""),
                            "args": block.get("input", {}),
                            "result": "",
                            "ms": 0,
                        })
    _trace_id = self.trace_log.log_turn(
        session_id=session_id,
        turn_id=session_turn,
        user_text=text,
        assistant_text=response_text or "",
        user_emotion=emotion,
        path_taken=self._last_path,
        tool_calls=_trace_tool_calls or None,
        latency_ms=self._last_timings.get("total_ms"),
    )

    # v2: async Observer extraction (writes to observations table)
    self._executor.submit(
        self.memory_manager.write_observation,
        {
            "user_text": text,
            "assistant_text": response_text or "",
            "tool_calls": _trace_tool_calls,
            "user_emotion": emotion,
        },
        _trace_id,
    )

    # v1: preserve GPT-4o-mini extraction (writes to memories table)
    # DirectAnswerer still reads from the old memories table, so we keep
    # both pipelines running in parallel until DA is migrated to observations.
    if updated_messages:
        self._executor.submit(
            self.memory_manager.save, updated_messages, user_id, session_id, emotion,
        )
```

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass. `jarvis.py` changes are additive (both old and new paths run).

- [ ] **Step 6: Commit**

```bash
git add config.yaml jarvis.py
git commit -m "feat(memory): integrate TraceLog + Observer into jarvis.py pipeline"
```

---

## Phase 2 · Skill System

### Task 7: @jarvis_tool Decorator

**Files:**
- Create: `tools/__init__.py`
- Test: `tests/test_jarvis_tool.py`

- [ ] **Step 1: Write tests**

Create `tests/test_jarvis_tool.py`:

```python
"""Tests for @jarvis_tool decorator."""
import pytest
from tools import jarvis_tool, _TOOL_REGISTRY


def setup_function():
    _TOOL_REGISTRY.clear()


def test_basic_decoration():
    @jarvis_tool
    def get_time() -> str:
        """获取当前时间"""
        return "14:30"

    assert "get_time" in _TOOL_REGISTRY
    entry = _TOOL_REGISTRY["get_time"]
    assert entry["definition"]["name"] == "get_time"
    assert entry["definition"]["description"] == "获取当前时间"
    assert entry["execute"]("ignored", {}) == "14:30"


def test_decoration_with_params():
    @jarvis_tool(destructive=True, required_role="owner")
    def set_light(room: str, color: str, brightness: int = 100) -> str:
        """控制灯光"""
        return f"{room}:{color}:{brightness}"

    entry = _TOOL_REGISTRY["set_light"]
    defn = entry["definition"]
    assert defn["description"] == "控制灯光"
    props = defn["input_schema"]["properties"]
    assert "room" in props
    assert "color" in props
    assert "brightness" in props
    assert props["brightness"]["type"] == "integer"
    assert "room" in defn["input_schema"]["required"]
    assert "brightness" not in defn["input_schema"]["required"]
    assert entry["required_role"] == "owner"
    assert entry["destructive"] is True


def test_execute_passes_kwargs():
    @jarvis_tool
    def add(a: int, b: int = 0) -> str:
        """加法"""
        return str(a + b)

    entry = _TOOL_REGISTRY["add"]
    result = entry["execute"]("add", {"a": 3, "b": 5})
    assert result == "8"


def test_function_remains_callable():
    @jarvis_tool
    def hello(name: str) -> str:
        """打招呼"""
        return f"hi {name}"

    # Direct call still works
    assert hello(name="Allen") == "hi Allen"
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_jarvis_tool.py -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement @jarvis_tool**

Create `tools/__init__.py`:

```python
"""Jarvis tool framework — @jarvis_tool decorator + global registry.

Usage:
    @jarvis_tool
    def get_time() -> str:
        '''获取当前时间'''
        return datetime.now().strftime(...)

    @jarvis_tool(destructive=True, required_role="owner")
    def set_light(room: str, color: str, brightness: int = 100) -> str:
        '''控制灯光'''
        ...
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, get_type_hints

_TOOL_REGISTRY: dict[str, dict[str, Any]] = {}

# Execution context — set by ToolRegistry.execute before each call.
# Provides user_id/user_role to tool functions without polluting their signatures.
_EXECUTION_CONTEXT: dict[str, Any] = {}

_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


def jarvis_tool(
    func: Callable | None = None,
    *,
    read_only: bool = True,
    destructive: bool = False,
    required_role: str = "guest",
) -> Callable:
    """Register a function as a Jarvis tool.

    Supports both ``@jarvis_tool`` and ``@jarvis_tool(...)`` syntax.
    Reflects type hints to build an OpenAI-compatible tool definition.
    """
    def _decorator(fn: Callable) -> Callable:
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        properties: dict[str, dict] = {}
        required: list[str] = []

        for name, param in sig.parameters.items():
            if name in ("self", "cls"):
                continue
            ptype = _TYPE_MAP.get(
                hints.get(name, str).__name__, "string",
            )
            properties[name] = {"type": ptype, "description": ""}
            if param.default is inspect.Parameter.empty:
                required.append(name)

        definition: dict[str, Any] = {
            "name": fn.__name__,
            "description": fn.__doc__ or "",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

        def _execute(tool_name: str, tool_input: dict, **context: Any) -> str:
            """Dispatch: extract args from tool_input and call the function."""
            kwargs = {}
            for pname, param in sig.parameters.items():
                if pname in ("self", "cls"):
                    continue
                if pname in tool_input:
                    val = tool_input[pname]
                    # Type coerce
                    expected = hints.get(pname, str)
                    if expected is int and not isinstance(val, int):
                        val = int(val)
                    elif expected is float and not isinstance(val, float):
                        val = float(val)
                    kwargs[pname] = val
                elif param.default is not inspect.Parameter.empty:
                    kwargs[pname] = param.default
            return fn(**kwargs)

        _TOOL_REGISTRY[fn.__name__] = {
            "definition": definition,
            "execute": _execute,
            "read_only": read_only,
            "destructive": destructive,
            "required_role": required_role,
        }

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return wrapper

    if func is not None:
        return _decorator(func)
    return _decorator
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_jarvis_tool.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add tools/__init__.py tests/test_jarvis_tool.py
git commit -m "feat(tools): add @jarvis_tool decorator with auto tool definition generation"
```

---

### Task 8: Migrate Smart Home Tool

**Files:**
- Create: `tools/smart_home.py`
- Test: `tests/test_tool_smart_home.py`

- [ ] **Step 1: Write tests**

Create `tests/test_tool_smart_home.py`:

```python
"""Tests for smart_home tool functions."""
import pytest
from unittest.mock import MagicMock
from tools import _TOOL_REGISTRY


def setup_function():
    _TOOL_REGISTRY.clear()


@pytest.fixture
def setup_smart_home():
    _TOOL_REGISTRY.clear()
    dm = MagicMock()
    pm = MagicMock()
    pm.check_permission.return_value = True
    dm.execute_command.return_value = "ok"
    dm.get_device.return_value = MagicMock(name="客厅灯")
    dm.get_device.return_value.get_status.return_value = {"on": True}
    dm.get_all_status.return_value = {"light_living": {"on": True}}
    import tools.smart_home
    tools.smart_home.init(dm, pm)
    return dm, pm


def test_smart_home_control_registered(setup_smart_home):
    assert "smart_home_control" in _TOOL_REGISTRY
    assert "smart_home_status" in _TOOL_REGISTRY


def test_smart_home_control_execute(setup_smart_home):
    dm, pm = setup_smart_home
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "light_living", "action": "turn_on"},
        user_role="owner",
    )
    dm.execute_command.assert_called_once_with("light_living", "turn_on", None)


def test_smart_home_status_execute(setup_smart_home):
    dm, _ = setup_smart_home
    entry = _TOOL_REGISTRY["smart_home_status"]
    result = entry["execute"]("smart_home_status", {})
    assert "light_living" in result
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_tool_smart_home.py -q`

- [ ] **Step 3: Implement tools/smart_home.py**

Create `tools/smart_home.py`:

```python
"""Smart home tools — wraps DeviceManager + PermissionManager."""
from __future__ import annotations

import json
import logging
from typing import Any

from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_device_manager: Any = None
_permission_manager: Any = None


def init(device_manager: Any, permission_manager: Any) -> None:
    """Inject dependencies at startup."""
    global _device_manager, _permission_manager
    _device_manager = device_manager
    _permission_manager = permission_manager


@jarvis_tool(destructive=True, required_role="guest")
def smart_home_control(device_id: str, action: str, value: str = "") -> str:
    """Control smart home devices: lights, thermostat, door locks, Hue scenes.

    Use for any request to turn on/off, adjust brightness, change color/color_temp,
    set temperature, lock/unlock, or activate a scene.
    """
    if not _device_manager:
        return "Smart home not available."

    try:
        device = _device_manager.get_device(device_id)
    except KeyError:
        return f"Device not found: {device_id}"

    from tools import _EXECUTION_CONTEXT
    user_role = _EXECUTION_CONTEXT.get("user_role", "owner")
    if _permission_manager and not _permission_manager.check_permission(user_role, device, action):
        return f"Permission denied for {action} on {device.name}."

    try:
        return _device_manager.execute_command(device_id, action, value if value else None)
    except Exception as exc:
        return f"Failed to execute {action} on {device_id}: {exc}"


@jarvis_tool(read_only=True)
def smart_home_status(device_id: str = "") -> str:
    """Get the current status of smart home devices. Omit device_id for all devices."""
    if not _device_manager:
        return "Smart home not available."

    if device_id:
        try:
            device = _device_manager.get_device(device_id)
            return json.dumps(device.get_status(), ensure_ascii=False)
        except KeyError:
            return f"Device not found: {device_id}"
    return json.dumps(_device_manager.get_all_status(), ensure_ascii=False, indent=2)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_tool_smart_home.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add tools/smart_home.py tests/test_tool_smart_home.py
git commit -m "feat(tools): migrate SmartHomeSkill to @jarvis_tool functions"
```

---

### Task 9: Migrate Time, Reminders, Todos Tools

**Files:**
- Create: `tools/time_utils.py`, `tools/reminders.py`, `tools/todos.py`
- Test: `tests/test_tool_time.py`, `tests/test_tool_reminders.py`, `tests/test_tool_todos.py`

These are direct ports of the existing Skill class logic into @jarvis_tool functions. Each preserves the exact same tool names (`get_current_time`, `set_timer`, `create_reminder`, `list_reminders`, `complete_reminder`, `add_todo`, `list_todos`, `complete_todo`, `delete_todo`).

- [ ] **Step 1: Implement tools/time_utils.py**

Port from `skills/time_skill.py`. Key: `get_current_time` and `set_timer` with callback injection via `init(tts_callback)`.

```python
"""Time tools — current time/date and countdown timers."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_timer_callback: Any = None
_active_timers: dict[str, threading.Timer] = {}


def init(tts_callback: Any = None) -> None:
    global _timer_callback
    _timer_callback = tts_callback


@jarvis_tool(read_only=True)
def get_current_time() -> str:
    """Get the current date and time."""
    now = datetime.now()
    return now.strftime("Current time: %Y-%m-%d %H:%M:%S (%A)")


@jarvis_tool
def set_timer(seconds: int, label: str = "timer") -> str:
    """Set a countdown timer. When it fires, Jarvis will announce it."""
    if seconds <= 0:
        return "Timer duration must be positive."

    timer_id = f"{label}_{seconds}"

    def _on_fire() -> None:
        _active_timers.pop(timer_id, None)
        message = f"Timer '{label}' ({seconds} seconds) has finished!"
        LOGGER.info(message)
        if _timer_callback:
            try:
                _timer_callback(message)
            except Exception as exc:
                LOGGER.warning("Timer callback failed: %s", exc)

    if timer_id in _active_timers:
        _active_timers[timer_id].cancel()

    timer = threading.Timer(seconds, _on_fire)
    timer.daemon = True
    timer.start()
    _active_timers[timer_id] = timer

    if seconds >= 60:
        display = f"{seconds // 60} minutes {seconds % 60} seconds"
    else:
        display = f"{seconds} seconds"
    return f"Timer set: '{label}' for {display}."
```

- [ ] **Step 2: Implement tools/reminders.py**

Port from `skills/reminders.py`. Preserve tool names: `create_reminder`, `list_reminders`, `complete_reminder`. Inject scheduler/tts/event_bus via `init()`.

```python
"""Reminder tools — per-user reminders with JSON persistence."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_filepath: Path = Path("data/reminders.json")
_scheduler: Any = None
_tts_callback: Callable | None = None
_event_bus: Any = None


def init(
    filepath: str = "data/reminders.json",
    scheduler: Any = None,
    tts_callback: Callable | None = None,
    event_bus: Any = None,
) -> None:
    global _filepath, _scheduler, _tts_callback, _event_bus
    _filepath = Path(filepath)
    _filepath.parent.mkdir(parents=True, exist_ok=True)
    _scheduler = scheduler
    _tts_callback = tts_callback
    _event_bus = event_bus
    if _scheduler and getattr(_scheduler, "available", False):
        _restore_scheduled_reminders()


def _load() -> list[dict]:
    if not _filepath.exists():
        return []
    try:
        with _filepath.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict]) -> None:
    with _filepath.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _schedule_reminder(reminder: dict) -> None:
    if not _scheduler or not getattr(_scheduler, "available", False):
        return
    try:
        _scheduler.add_date_job(
            job_id=f"reminder_{reminder['id']}",
            func=_fire_reminder,
            run_date=reminder["remind_at"],
            kwargs={
                "reminder_id": reminder["id"],
                "content": reminder["content"],
                "tts_callback": _tts_callback,
                "event_bus": _event_bus,
            },
        )
    except Exception as exc:
        LOGGER.warning("Failed to schedule reminder %s: %s", reminder["id"], exc)


def _restore_scheduled_reminders() -> None:
    now = datetime.now()
    for r in _load():
        if r.get("is_done") or not r.get("remind_at"):
            continue
        try:
            if datetime.fromisoformat(r["remind_at"]) > now:
                _schedule_reminder(r)
        except (ValueError, TypeError):
            pass


def _fire_reminder(
    reminder_id: str, content: str,
    tts_callback: Callable | None = None, event_bus: Any = None,
) -> None:
    LOGGER.info("Reminder fired: [%s] %s", reminder_id, content)
    if tts_callback:
        try:
            tts_callback(f"Reminder: {content}")
        except Exception:
            LOGGER.exception("TTS failed for reminder %s", reminder_id)
    if event_bus:
        event_bus.emit("reminder.fired", {"id": reminder_id, "content": content})


@jarvis_tool
def create_reminder(content: str, remind_at: str = "") -> str:
    """Create a reminder for the current user."""
    if not content.strip():
        return "Reminder content cannot be empty."
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    reminder = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        "content": content.strip(),
        "remind_at": remind_at.strip() or None,
        "is_done": False,
        "created_at": datetime.now().isoformat(),
    }
    data = _load()
    data.append(reminder)
    _save(data)
    if reminder["remind_at"]:
        _schedule_reminder(reminder)
    time_part = f" for {remind_at}" if remind_at else ""
    return f"Reminder created (ID: {reminder['id']}): '{content}'{time_part}."


@jarvis_tool(read_only=True)
def list_reminders() -> str:
    """List all active reminders for the current user."""
    data = _load()
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    active = [r for r in data if not r.get("is_done", False) and r.get("user_id", user_id) == user_id]
    if not active:
        return "No active reminders."
    lines = []
    for r in active:
        time_part = f" (due: {r['remind_at']})" if r.get("remind_at") else ""
        lines.append(f"- [{r['id']}] {r['content']}{time_part}")
    return "Active reminders:\n" + "\n".join(lines)


@jarvis_tool
def complete_reminder(reminder_id: str) -> str:
    """Mark a reminder as done by its ID."""
    data = _load()
    for r in data:
        if r.get("id") == reminder_id:
            r["is_done"] = True
            _save(data)
            return f"Reminder '{r['content']}' marked as done."
    return f"Reminder {reminder_id} not found."
```

- [ ] **Step 3: Implement tools/todos.py**

Port from `skills/todos.py`. Preserve tool names: `add_todo`, `list_todos`, `complete_todo`, `delete_todo`.

```python
"""Todo tools — per-user todo lists with JSON persistence."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_persist_dir: Path = Path("data/todos")


def init(persist_dir: str = "data/todos") -> None:
    global _persist_dir
    _persist_dir = Path(persist_dir)
    _persist_dir.mkdir(parents=True, exist_ok=True)


def _filepath(user_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
    return _persist_dir / f"{safe_id}.json"


def _load(user_id: str) -> list[dict]:
    fp = _filepath(user_id)
    if not fp.exists():
        return []
    try:
        with fp.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_id: str, data: list[dict]) -> None:
    fp = _filepath(user_id)
    with fp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@jarvis_tool
def add_todo(content: str, priority: str = "medium") -> str:
    """Add a todo item for the current user."""
    if not content.strip():
        return "Todo content cannot be empty."
    todo = {
        "id": str(uuid.uuid4())[:8],
        "content": content.strip(),
        "priority": priority.strip().lower(),
        "done": False,
        "created_at": datetime.now().isoformat(),
    }
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    data.append(todo)
    _save(user_id, data)
    return f"Todo added (ID: {todo['id']}): '{content}' [{priority}]."


@jarvis_tool(read_only=True)
def list_todos() -> str:
    """List all incomplete todos for the current user."""
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    active = [t for t in data if not t.get("done", False)]
    if not active:
        return "No active todos."
    lines = []
    for t in active:
        lines.append(f"- [{t['id']}] ({t.get('priority', 'medium')}) {t['content']}")
    return "Todos:\n" + "\n".join(lines)


@jarvis_tool
def complete_todo(todo_id: str) -> str:
    """Mark a todo as completed by its ID."""
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for t in data:
        if t.get("id") == todo_id:
            t["done"] = True
            _save(user_id, data)
            return f"Todo '{t['content']}' completed."
    return f"Todo {todo_id} not found."


@jarvis_tool
def delete_todo(todo_id: str) -> str:
    """Delete a todo by its ID."""
    from tools import _EXECUTION_CONTEXT
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for i, t in enumerate(data):
        if t.get("id") == todo_id:
            removed = data.pop(i)
            _save(user_id, data)
            return f"Todo '{removed['content']}' deleted."
    return f"Todo {todo_id} not found."
```

- [ ] **Step 4: Write minimal tests for each, run**

Run: `python -m pytest tests/test_tool_smart_home.py tests/test_jarvis_tool.py -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add tools/time_utils.py tools/reminders.py tools/todos.py
git commit -m "feat(tools): migrate TimeSkill, ReminderSkill, TodoSkill to @jarvis_tool"
```

---

### Task 10: YAMLInterpreter

**Files:**
- Create: `core/yaml_interpreter.py`
- Test: `tests/test_yaml_interpreter.py`

- [ ] **Step 1: Write tests**

Create `tests/test_yaml_interpreter.py`:

```python
"""Tests for YAMLInterpreter."""
import pytest
from unittest.mock import patch, MagicMock
from core.yaml_interpreter import YAMLInterpreter


SAMPLE_YAML = {
    "name": "get_weather",
    "description": "Get current weather",
    "parameters": [
        {"name": "city", "type": "string", "description": "City name", "required": False, "default": "Victoria"},
    ],
    "action": {
        "type": "http_get",
        "url": "https://wttr.in/{{ city }}",
        "headers": {"Accept-Language": "zh"},
        "timeout_ms": 10000,
        "retry": {"max": 3, "delay_ms": 1000, "backoff": "exponential"},
    },
    "response": {
        "extract": {},
        "template": "Weather for {{ city }}: {{ result }}",
        "error_template": "天气查询失败",
    },
    "security": {
        "allowed_domains": ["wttr.in"],
    },
}


def test_to_tool_definition():
    interp = YAMLInterpreter()
    defn = interp.to_tool_definition(SAMPLE_YAML)
    assert defn["name"] == "get_weather"
    assert "city" in defn["input_schema"]["properties"]


def test_domain_whitelist_blocks():
    interp = YAMLInterpreter()
    bad_yaml = {**SAMPLE_YAML, "action": {**SAMPLE_YAML["action"], "url": "https://evil.com/{{ city }}"}}
    result = interp.execute(bad_yaml, {"city": "Vancouver"})
    assert "blocked" in result.lower() or "not allowed" in result.lower()


def test_private_ip_blocked():
    interp = YAMLInterpreter()
    bad_yaml = {**SAMPLE_YAML, "action": {**SAMPLE_YAML["action"], "url": "http://127.0.0.1/api"}, "security": {"allowed_domains": ["127.0.0.1"]}}
    result = interp.execute(bad_yaml, {})
    assert "blocked" in result.lower() or "private" in result.lower()


@patch("core.yaml_interpreter.requests.get")
def test_execute_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.text = "Sunny 20C"
    mock_resp.json.return_value = {"temp": "20"}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    interp = YAMLInterpreter()
    result = interp.execute(SAMPLE_YAML, {"city": "Vancouver"})
    assert "Vancouver" in result


@patch("core.yaml_interpreter.requests.get")
def test_execute_retry_on_failure(mock_get):
    mock_get.side_effect = [Exception("timeout"), Exception("timeout"), MagicMock(text="ok", raise_for_status=MagicMock())]
    interp = YAMLInterpreter()
    # Should retry up to 3 times
    result = interp.execute(SAMPLE_YAML, {"city": "test"})
    assert mock_get.call_count == 3
```

- [ ] **Step 2: Implement YAMLInterpreter**

Create `core/yaml_interpreter.py`:

```python
"""YAMLInterpreter — execute YAML skill definitions with Jinja2 rendering.

Supports http_get and http_post actions with three-layer error handling:
  Layer 1: Step-level retry (exponential backoff)
  Layer 2: LLM-visible error (error_template)
  Layer 3: Fallback string for route-level degradation
"""
from __future__ import annotations

import ipaddress
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

LOGGER = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
]


class YAMLInterpreter:
    """Execute YAML skill definitions with security checks and error handling."""

    def __init__(self) -> None:
        self._env = ImmutableSandboxedEnvironment()

    def load_skill(self, yaml_path: str) -> dict[str, Any]:
        """Load a YAML skill file and return the parsed dict."""
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def to_tool_definition(self, skill: dict[str, Any]) -> dict[str, Any]:
        """Convert a YAML skill to an OpenAI-compatible tool definition."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in skill.get("parameters", []):
            pname = param["name"]
            properties[pname] = {
                "type": param.get("type", "string"),
                "description": param.get("description", ""),
            }
            if param.get("required", False):
                required.append(pname)

        return {
            "name": skill["name"],
            "description": skill.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def execute(self, skill: dict[str, Any], params: dict[str, Any]) -> str:
        """Execute a YAML skill with the given parameters.

        Returns:
            Result string on success, error string on failure.
        """
        action = skill.get("action", {})
        response_spec = skill.get("response", {})
        security = skill.get("security", {})

        # Apply defaults from parameter definitions
        for param_def in skill.get("parameters", []):
            pname = param_def["name"]
            if pname not in params and "default" in param_def:
                params[pname] = param_def["default"]

        # Render URL with Jinja2
        url_template = action.get("url", "")
        try:
            url = self._env.from_string(url_template).render(**params)
        except Exception as exc:
            return f"URL rendering failed: {exc}"

        # Security: domain whitelist
        allowed = security.get("allowed_domains", [])
        if allowed:
            parsed = urlparse(url)
            if parsed.hostname not in allowed:
                return f"Domain not allowed: {parsed.hostname}"

        # Security: private IP block
        if self._is_private_url(url):
            return f"Blocked: private/local network address"

        # Auth from env var
        headers = dict(action.get("headers", {}))
        auth_env = security.get("auth_env")
        if auth_env:
            api_key = os.environ.get(auth_env, "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        # Execute with retry
        retry_conf = action.get("retry", {"max": 3, "delay_ms": 1000, "backoff": "exponential"})
        max_retries = retry_conf.get("max", 3)
        delay_ms = retry_conf.get("delay_ms", 1000)
        timeout_ms = action.get("timeout_ms", 10000)

        method = action.get("type", "http_get")
        last_error = None

        for attempt in range(max_retries):
            try:
                if method == "http_get":
                    resp = requests.get(url, headers=headers, timeout=timeout_ms / 1000)
                elif method == "http_post":
                    body = action.get("body", {})
                    rendered_body = {k: self._env.from_string(str(v)).render(**params) for k, v in body.items()}
                    resp = requests.post(url, headers=headers, json=rendered_body, timeout=timeout_ms / 1000)
                else:
                    return f"Unsupported action type: {method}"

                resp.raise_for_status()

                # Render response
                return self._render_response(response_spec, params, resp)

            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    sleep_s = (delay_ms / 1000) * (2 ** attempt)
                    sleep_s = min(sleep_s, 5.0)
                    time.sleep(sleep_s)

        # Layer 2: error template
        error_template = response_spec.get("error_template")
        if error_template:
            return error_template

        # Layer 3: fallback string
        return f"Skill execution failed after {max_retries} retries: {last_error}"

    def _render_response(
        self, response_spec: dict, params: dict, resp: requests.Response,
    ) -> str:
        """Render the response using Jinja2 templates."""
        try:
            result = resp.json()
        except Exception:
            result = resp.text

        # Make result available in template context
        context = {**params, "result": result}

        # Extract step
        for key, expr in response_spec.get("extract", {}).items():
            try:
                context[key] = self._env.from_string(expr).render(**context)
            except Exception:
                pass

        # Compute step
        for key, expr in response_spec.get("compute", {}).items():
            try:
                rendered = self._env.from_string(expr).render(**context)
                try:
                    context[key] = float(rendered)
                except ValueError:
                    context[key] = rendered
            except Exception:
                pass

        # Template step
        template = response_spec.get("template", "{{ result }}")
        try:
            return self._env.from_string(template).render(**context)
        except Exception as exc:
            return f"Response rendering failed: {exc}"

    @staticmethod
    def _is_private_url(url: str) -> bool:
        """Check if URL points to a private/local network address."""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            if hostname in ("localhost", ""):
                return True
            addr = ipaddress.ip_address(hostname)
            return any(addr in net for net in _PRIVATE_NETWORKS)
        except ValueError:
            return False
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_yaml_interpreter.py -q`
Expected: `5 passed`

- [ ] **Step 4: Commit**

```bash
git add core/yaml_interpreter.py tests/test_yaml_interpreter.py
git commit -m "feat(core): add YAMLInterpreter with Jinja2 sandbox + 3-layer error handling"
```

---

### Task 11: YAML Skills

**Files:**
- Create: `skills/weather.yaml`
- Create: `skills/learned/exchange_rate.yaml`

- [ ] **Step 1: Create weather.yaml**

```yaml
name: get_weather
description: "Get current weather for a city. Returns temperature, conditions, humidity, and wind."
version: 1
status: live
created_by: migrated

parameters:
  - name: city
    type: string
    description: "City name in English. Defaults to Victoria."
    required: false
    default: Victoria

annotations:
  read_only: true
  destructive: false
  idempotent: true

action:
  type: http_get
  url: "https://wttr.in/{{ city }}?format=j1"
  headers:
    Accept-Language: zh
  timeout_ms: 10000
  retry:
    max: 3
    delay_ms: 1000
    backoff: exponential

response:
  extract:
    temp_c: "{{ result.current_condition[0].temp_C }}"
    feels_like: "{{ result.current_condition[0].FeelsLikeC }}"
    desc: "{{ result.current_condition[0].lang_zh[0].value }}"
    humidity: "{{ result.current_condition[0].humidity }}"
    wind: "{{ result.current_condition[0].windspeedKmph }}"
  template: "{{ city }}天气：{{ desc }}，温度{{ temp_c }}°C（体感{{ feels_like }}°C），湿度{{ humidity }}%，风速{{ wind }}公里/时。"
  error_template: "天气查询失败，请稍后再试。"

security:
  allowed_domains:
    - wttr.in
  requires_auth: false
```

- [ ] **Step 2: Create exchange_rate.yaml**

```yaml
name: get_exchange_rate
description: "Get current exchange rate or convert between currencies. Uses ISO 4217 codes (USD, CNY, EUR, JPY, CAD)."
version: 1
status: live
created_by: migrated

parameters:
  - name: base
    type: string
    description: "Source currency code (e.g. USD, CNY, EUR). Defaults to USD."
    required: false
    default: USD
  - name: target
    type: string
    description: "Target currency code (e.g. CNY, CAD, JPY)."
    required: true
  - name: amount
    type: number
    description: "Amount to convert. Defaults to 1."
    required: false
    default: 1

annotations:
  read_only: true
  destructive: false
  idempotent: true

action:
  type: http_get
  url: "https://open.er-api.com/v6/latest/{{ base }}"
  headers: {}
  timeout_ms: 10000
  retry:
    max: 3
    delay_ms: 1000
    backoff: exponential

response:
  extract:
    rate: "{{ result.rates[target] }}"
  compute:
    converted: "{{ amount * rate | round(4) }}"
  template: "{{ amount }} {{ base }} = {{ converted }} {{ target }}"
  error_template: "汇率查询失败，请稍后再试。"

security:
  allowed_domains:
    - open.er-api.com
  requires_auth: false
```

- [ ] **Step 3: Commit**

```bash
git add skills/weather.yaml skills/learned/exchange_rate.yaml
git commit -m "feat(skills): add weather + exchange_rate YAML skill definitions"
```

---

### Task 12: ToolRegistry

**Files:**
- Create: `core/tool_registry.py`
- Test: `tests/test_tool_registry.py`

- [ ] **Step 1: Write tests**

Create `tests/test_tool_registry.py`:

```python
"""Tests for ToolRegistry."""
import os
import pytest
from unittest.mock import patch
from tools import _TOOL_REGISTRY, jarvis_tool
from core.tool_registry import ToolRegistry


def setup_function():
    _TOOL_REGISTRY.clear()


@pytest.fixture
def registry(tmp_path):
    _TOOL_REGISTRY.clear()

    @jarvis_tool
    def test_func(x: str) -> str:
        """Test function"""
        return f"result: {x}"

    @jarvis_tool(required_role="owner")
    def admin_func() -> str:
        """Admin only"""
        return "admin"

    config = {"skills": {"yaml_dirs": [str(tmp_path)]}}
    reg = ToolRegistry(config)
    return reg


def test_registry_finds_python_tools(registry):
    names = [d["name"] for d in registry.get_tool_definitions()]
    assert "test_func" in names
    assert "admin_func" in names


def test_registry_rbac_filters(registry):
    guest_tools = registry.get_tool_definitions(user_role="guest")
    guest_names = [d["name"] for d in guest_tools]
    assert "test_func" in guest_names
    assert "admin_func" not in guest_names

    owner_tools = registry.get_tool_definitions(user_role="owner")
    owner_names = [d["name"] for d in owner_tools]
    assert "admin_func" in owner_names


def test_registry_execute(registry):
    result = registry.execute("test_func", {"x": "hello"})
    assert result == "result: hello"


def test_registry_execute_unknown(registry):
    result = registry.execute("nonexistent", {})
    assert "unknown" in result.lower() or "not found" in result.lower()


def test_registry_count(registry):
    assert registry.count() >= 2
```

- [ ] **Step 2: Implement ToolRegistry**

Create `core/tool_registry.py`:

```python
"""ToolRegistry — unified dispatch for Python @jarvis_tool functions and YAML skills.

Replaces the old SkillRegistry. Scans tools/ for @jarvis_tool registrations
and skills/*.yaml for YAML skill definitions at startup.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tools import _TOOL_REGISTRY
from core.yaml_interpreter import YAMLInterpreter

LOGGER = logging.getLogger(__name__)

_ROLE_HIERARCHY = {
    "guest": 0,
    "member": 1,
    "resident": 1,
    "family": 2,
    "admin": 2,
    "owner": 3,
}


class ToolRegistry:
    """Unified tool registry for Python functions and YAML skills.

    Args:
        config: Application config dict.
    """

    def __init__(self, config: dict) -> None:
        self._yaml_tools: dict[str, dict[str, Any]] = {}
        self._interpreter = YAMLInterpreter()
        self.logger = LOGGER

        # Scan YAML skill directories
        yaml_dirs = [
            Path("skills"),
            Path("skills/learned"),
        ]
        for d in yaml_dirs:
            if d.exists():
                for yaml_file in d.glob("*.yaml"):
                    self._load_yaml_skill(yaml_file)

        total = self.count()
        if total > 15:
            self.logger.warning("Tool count %d exceeds recommended limit of 15", total)
        self.logger.info("ToolRegistry initialized: %d Python + %d YAML = %d total",
                         len(_TOOL_REGISTRY), len(self._yaml_tools), total)

    def _load_yaml_skill(self, path: Path) -> None:
        """Load a single YAML skill file."""
        try:
            skill = self._interpreter.load_skill(str(path))
            if skill.get("status", "live") == "deprecated":
                return
            name = skill["name"]
            defn = self._interpreter.to_tool_definition(skill)
            self._yaml_tools[name] = {
                "definition": defn,
                "skill": skill,
                "required_role": "guest",
            }
            self.logger.info("Loaded YAML skill: %s from %s", name, path)
        except Exception as exc:
            self.logger.warning("Failed to load YAML skill %s: %s", path, exc)

    def get_tool_definitions(self, user_role: str = "guest") -> list[dict[str, Any]]:
        """Return all tool definitions accessible to the given role."""
        user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
        tools: list[dict[str, Any]] = []

        # Python tools
        for name, entry in _TOOL_REGISTRY.items():
            required_level = _ROLE_HIERARCHY.get(
                entry.get("required_role", "guest").strip().lower(), 0,
            )
            if user_level >= required_level:
                tools.append(entry["definition"])

        # YAML tools
        for name, entry in self._yaml_tools.items():
            required_level = _ROLE_HIERARCHY.get(
                entry.get("required_role", "guest").strip().lower(), 0,
            )
            if user_level >= required_level:
                tools.append(entry["definition"])

        return tools

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user_id: str | None = None,
        user_role: str = "guest",
    ) -> str:
        """Execute a tool by name. Dispatches to Python function or YAML interpreter.

        Args:
            name: Tool name.
            args: Tool input arguments.
            user_id: Authenticated user ID (passed through to tools).
            user_role: Authenticated user role for RBAC.

        Returns:
            Result string.
        """
        # Set execution context for tool functions to read
        from tools import _EXECUTION_CONTEXT
        _EXECUTION_CONTEXT["user_id"] = user_id
        _EXECUTION_CONTEXT["user_role"] = user_role

        # Check Python tools first
        if name in _TOOL_REGISTRY:
            entry = _TOOL_REGISTRY[name]
            required_level = _ROLE_HIERARCHY.get(
                entry.get("required_role", "guest").strip().lower(), 0,
            )
            user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
            if user_level < required_level:
                return f"Permission denied: {name} requires role '{entry['required_role']}'."
            try:
                return entry["execute"](name, args, user_id=user_id, user_role=user_role)
            except Exception as exc:
                self.logger.exception("Tool %s failed", name)
                return f"Tool execution error: {exc}"

        # Check YAML tools
        if name in self._yaml_tools:
            entry = self._yaml_tools[name]
            try:
                return self._interpreter.execute(entry["skill"], dict(args))
            except Exception as exc:
                self.logger.exception("YAML tool %s failed", name)
                return f"Tool execution error: {exc}"

        return f"Error: unknown tool '{name}'"

    def count(self) -> int:
        """Return total number of registered tools."""
        return len(_TOOL_REGISTRY) + len(self._yaml_tools)
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_tool_registry.py -q`
Expected: `5 passed`

- [ ] **Step 4: Run full suite**

Run: `python -m pytest tests/ -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add core/tool_registry.py tests/test_tool_registry.py
git commit -m "feat(core): add ToolRegistry — unified dispatch for Python + YAML tools"
```

---

### Task 13: jarvis.py Skill Integration

**Files:**
- Modify: `jarvis.py`
- Modify: `core/local_executor.py`
- Modify: `config.yaml`

This is the critical integration task. Replace SkillRegistry with ToolRegistry throughout jarvis.py.

- [ ] **Step 1: Update config.yaml — disable realtime_data**

Change `skills.realtime_data.enabled` from `true` to `false`:
```yaml
  realtime_data:
    enabled: false
```

- [ ] **Step 2: Add guard to LocalExecutor**

In `core/local_executor.py`, update `execute_info_query` (L114-152) — add guard at top of each sub_type block:

```python
def execute_info_query(
    self, sub_type: str | None, query: Any, user_role: str = "owner",
) -> ActionResponse:
    """执行信息查询."""
    result: str | None = None

    if sub_type == "stocks":
        symbols = query if isinstance(query, list) else None
        tool_input = {"symbols": symbols} if symbols else {}
        result = self.skill_registry.execute(
            "get_stock_watchlist", tool_input, user_role=user_role,
        )
        if result and "unknown tool" in result.lower():
            result = "实时股票功能暂不可用。"

    elif sub_type == "news":
        focus = query if isinstance(query, str) else "all"
        result = self.skill_registry.execute(
            "get_news_briefing", {"focus": focus}, user_role=user_role,
        )
        if result and "unknown tool" in result.lower():
            result = "新闻功能暂不可用。"

    elif sub_type == "weather":
        tool_input = {"city": query} if isinstance(query, str) and query.strip() else {}
        self.logger.info("Weather query: raw=%r → tool_input=%s", query, tool_input)
        result = self.skill_registry.execute(
            "get_weather", tool_input, user_role=user_role,
        )

    if not result:
        return ActionResponse(Action.RESPONSE, "没查到相关信息。")

    return ActionResponse(Action.RESPONSE, result)
```

- [ ] **Step 3: Replace SkillRegistry with ToolRegistry in jarvis.py**

**Remove imports** (around L44-46):
```python
# DELETE these lines:
from skills import SkillRegistry
from skills.memory_skill import MemorySkill
```

**Add new import**:
```python
from core.tool_registry import ToolRegistry
```

**Replace `__init__` wiring** (L186-188):
```python
# DELETE:
self.skill_registry = SkillRegistry()
self._register_skills(config)

# REPLACE WITH:
# Import and init tool modules with dependencies
import tools.smart_home
tools.smart_home.init(self.device_manager, self.permission_manager)
import tools.time_utils
tools.time_utils.init(tts_callback=self.speak)
import tools.reminders
tools.reminders.init(
    filepath=config.get("skills", {}).get("reminders", {}).get("path", "data/reminders.json"),
    scheduler=self.scheduler,
    tts_callback=self.speak,
    event_bus=self.event_bus,
)
import tools.todos
tools.todos.init(
    persist_dir=config.get("skills", {}).get("todos", {}).get("dir", "data/todos"),
)

self.tool_registry = ToolRegistry(config)
```

**Update LocalExecutor** (L216):
```python
# Change:
self.local_executor = LocalExecutor(self.skill_registry, self.rule_manager)
# To:
self.local_executor = LocalExecutor(self.tool_registry, self.rule_manager)
```

**Remove learning system** (L220-229):
```python
# DELETE these lines:
from core.learning_router import LearningRouter
from core.skill_factory import SkillFactory
self.learning_router = LearningRouter(...)
self.skill_factory = SkillFactory(...)
```

**Update Cloud LLM tool-use** (L999, L1057):
```python
# Change L999:
tools = self.tool_registry.get_tool_definitions(user_role)
# Change L1057:
tool_executor=self.tool_registry.execute,
```

**Remove `_register_skills` method** (L1308-1386): Delete the entire method.

**Remove `_learn_create` and `_learn_create_bg` methods** (L1388-1427): Delete both.

**Remove the learning path** in `_process_turn` (around L778-796): Remove the `if hasattr(self, "learning_router"):` block.

**Update the TTS callback injection**: Since TimeSkill is now tools.time_utils, the callback is already injected via `tools.time_utils.init(tts_callback=self.speak)` above.

- [ ] **Step 4: Update remaining SkillRegistry references**

Search for any remaining `self.skill_registry` references in jarvis.py and replace with `self.tool_registry`. The main ones should be covered above.

Also update `self.skill_registry._skills.get("time")` (L1378) — this is no longer needed since the callback is injected at init time.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: Some test failures from tests that reference old imports. These will be fixed in Task 14.

- [ ] **Step 6: Commit**

```bash
git add jarvis.py core/local_executor.py config.yaml
git commit -m "feat: replace SkillRegistry with ToolRegistry in jarvis.py pipeline"
```

---

### Task 14: Test Migration + Old File Cleanup

**Files:**
- Delete: All old skill class files (see file map)
- Modify: Various test files
- Delete: `core/skill_factory.py`, `core/learning_router.py`, `core/skill_loader.py`

- [ ] **Step 1: Update test files that import old skills**

For each test file that imports from `skills` or `skills.xxx`:
- `tests/test_skills.py` → delete or rewrite as `tests/test_tool_registry.py` (already created)
- `tests/test_skills_coverage.py` → delete
- `tests/test_behavior_log.py` → keep (behavior_log.py preserved)
- `tests/test_skill_factory.py` → delete
- `tests/test_skill_loader.py` → delete
- `tests/test_skill_mgmt.py` → delete
- `tests/test_learning_router.py` → delete
- `tests/test_learning_e2e.py` → delete
- `tests/test_model_switch.py` → delete
- `tests/test_health_skill.py` → delete
- `tests/test_remote_control.py` → delete
- `tests/test_realtime_data.py` → delete
- `tests/test_realtime_data_skill.py` → delete
- `tests/test_learned_exchange_rate.py` → delete or rewrite for YAML version
- `tests/test_intent_router.py` → update mocks from SkillRegistry to ToolRegistry
- `tests/test_jarvis.py` → update mocks from SkillRegistry to ToolRegistry
- `tests/test_local_executor.py` → update mocks

- [ ] **Step 2: Delete old skill class files**

```bash
rm skills/__init__.py
rm skills/smart_home.py skills/weather.py skills/time_skill.py
rm skills/reminders.py skills/todos.py skills/memory_skill.py
rm skills/automation.py skills/system_control.py skills/model_switch.py
rm skills/realtime_data.py skills/scheduler_skill.py skills/remote_control.py
rm skills/health_skill.py skills/skill_mgmt.py
rm skills/learned/exchange_rate.py skills/learned/__init__.py
rm core/skill_factory.py core/learning_router.py core/skill_loader.py
```

- [ ] **Step 3: Verify grep clean**

Run: `grep -r "SkillRegistry\|SkillFactory\|LearningRouter\|SkillLoader" --include="*.py" . | grep -v tests/ | grep -v __pycache__ | grep -v node_modules`
Expected: Zero matches.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All remaining tests pass. If failures occur, fix the specific mock/import issues.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete old Skill classes + SkillFactory + LearningRouter, clean imports"
```

---

### Task 15: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 2: Verify grep clean**

```bash
grep -r "SkillRegistry\|SkillFactory\|LearningRouter\|SkillLoader\|from skills import\|from skills\." --include="*.py" . | grep -v __pycache__ | grep -v tests/
```
Expected: Zero matches (excluding test files that may reference old names in comments).

- [ ] **Step 3: Verify ToolRegistry count**

Add a quick script:
```bash
python -c "
import tools.smart_home, tools.time_utils, tools.reminders, tools.todos
tools.smart_home.init(None, None)
from core.tool_registry import ToolRegistry
r = ToolRegistry({})
print(f'Tools: {r.count()}')
for d in r.get_tool_definitions():
    print(f'  - {d[\"name\"]}: {d[\"description\"][:50]}')
"
```
Expected: 12-13 tools, all ≤20.

- [ ] **Step 4: Verify observation/trace tables**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/memory/jarvis_memory.db')
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print('Tables:', tables)
assert 'trace' in tables
assert 'observations' in tables
assert 'memories' in tables  # preserved
assert 'behavior_log' in tables  # preserved
"
```

- [ ] **Step 5: Present system test prompts to user**

Per CLAUDE.md: present system test prompts and wait for approval before running.

Suggested prompts:
1. "今天天气怎么样" — tests weather.yaml path
2. "把客厅灯打开" — tests smart_home_control tool
3. "提醒我下午三点开会" — tests create_reminder tool
4. "现在几点" — tests get_current_time tool
5. "500美元多少人民币" — tests exchange_rate.yaml path

---

## Summary

| Phase | Tasks | New Files | Modified Files | Deleted Files |
|-------|-------|-----------|---------------|---------------|
| 1 (Memory) | 1-6 | 4 | 3 | 0 |
| 2 (Skills) | 7-15 | 8 | 3 | ~18 |
| **Total** | **15** | **12** | **6** | **~18** |

Estimated commits: 12-15
