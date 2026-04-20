# Jarvis Trace Schema v3 — One-Shot Implementation Plan

**Date drafted:** 2026-04-19
**Status:** ready for implementation
**Estimated touchpoints:** ~8 files, ~600 lines diff
**One-shot discipline:** do the full 31-column schema in one feature branch; no phased rollout. Half-baked observability is worse than none.

---

## Mission

Upgrade `memory/trace.py` from its current **16-column minimal schema** (write-only, consumer-less) to a **31-column production schema** that serves three concrete downstreams:

1. **Phase 3 auto-skill-learning** — `query_cloud_traces()` feeds hotspot detection → YAML skill compilation
2. **MCP server debug queries** — Claude Code asking "why did turn X fail", "cache hit rate by model", "slow turns last week"
3. **Long-term cost/latency analytics** — daily `cost_usd`, `ttfs_ms` trending, `finish_reason` distribution

The current table is a "埋点良好但没人用的黑洞"—5 columns are declared but never written (`tts_emotion / llm_model / llm_tokens_in/out / outcome_signal / outcome_at_turn_id`). This plan fixes that + adds 15 new columns + provides writers for all of them.

This is **not** an observability afterthought — it's Phase 3 Step 1's prerequisite. Step 1's SQL is `SELECT user_text, tool_calls, assistant_text FROM trace WHERE path_taken='cloud' AND outcome_signal>=0 AND created_at > datetime('now','-7 days')`. Without a populated `outcome_signal`, Phase 3's filter passes everything. Without `cache_read_input_tokens`, you can't analyze the xAI cache issue you just debugged tonight.

---

## Prerequisites — required reading before starting

Read these files (in order) to ground yourself:

1. `/Users/alllllenshi/Projects/jarvis/CLAUDE.md` — project rules (commits in English, no emojis, no hardcoded paths, use logging not print)
2. `/Users/alllllenshi/Projects/jarvis/memory/trace.py` — current 16-col schema + `TraceLog` class
3. `/Users/alllllenshi/Projects/jarvis/tests/test_trace.py` — existing test style to mirror
4. `/Users/alllllenshi/Projects/jarvis/jarvis.py` lines 690-720 and 1130-1180 — ASR normalize + current `log_turn` call site
5. `/Users/alllllenshi/Projects/jarvis/memory/manager.py` lines 320-360 — `write_observation` flow (trace_id passed as `source_turn_id` to observations table)
6. `/Users/alllllenshi/Projects/jarvis/core/asr_normalizer.py` — understand `_raw_text` origin (line 62 `normalize()` method)
7. `/Users/alllllenshi/Projects/jarvis/memory/store.py` lines 125-145 — observations FK to trace.id via `source_turn_id`

### Relevant user memory

- `feedback_no_emojis.md` — **zero emojis** in code, comments, commit messages
- `feedback_english_commits.md` — all commit messages in English
- `feedback_data_driven.md` — verified data only; test before claiming success
- `project_phase3_4_roadmap.md` — Phase 3 is the primary consumer
- `project_direction_tool_focused.md` — Jarvis is a tool-oriented assistant

### Project CLAUDE.md absolute rules
- Never hardcode paths / keys — read from `config.yaml`
- Never use `print` — use `logging`
- Don't modify `data/speechbrain_model/` or `data/sensevoice-small-int8/`
- Commit OK, **never push**
- Type hints + Google-style docstrings on every function
- No `Co-Authored-By` in commits

---

## Final Schema (31 columns, locked)

```sql
CREATE TABLE trace (
  -- ═══ Identity (5) ═══
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id                TEXT    NOT NULL,
  turn_id                   INTEGER NOT NULL,
  user_id                   TEXT    NOT NULL DEFAULT 'default_user',
  created_at                TEXT    NOT NULL,

  -- ═══ Input / Output (5) ═══
  user_text                 TEXT,                    -- normalize-后的文本（下游消费这个）
  assistant_text            TEXT,                    -- assistant final response
  user_emotion              TEXT,                    -- SenseVoice: happy/angry/sad/neutral/fear/surprise/disgust
  tts_emotion               TEXT,                    -- TTS playback intended emotion
  input_metadata            TEXT,                    -- JSON, see schema below

  -- ═══ Triggering / Routing (4) ═══
  trigger_source            TEXT,                    -- enum: wake_word/continuation/web_text/web_voice/proactive/test
  parent_trace_id           INTEGER,                 -- FK → trace.id, NULL if standalone
  path_taken                TEXT,                    -- enum: unknown/resume/farewell/memory_shortcut/keyword_rule/memory_l1/local/cloud
  intent_route_score        REAL,                    -- router confidence [0, 1], NULL if no LLM router

  -- ═══ Tools (1) ═══
  tool_calls                TEXT,                    -- JSON array: [{name, args, result, ms}]

  -- ═══ LLM (5) ═══
  llm_model                 TEXT,                    -- e.g. "grok-4.20", NULL when path != cloud
  llm_tokens_in             INTEGER,
  llm_tokens_out            INTEGER,
  cache_read_input_tokens   INTEGER,                 -- prompt cache hit (all providers)
  llm_metadata              TEXT,                    -- JSON, see schema below

  -- ═══ Memory (1) ═══
  memory_query_ids          TEXT,                    -- JSON, see schema below

  -- ═══ Context (1) ═══
  prompt_version            TEXT,                    -- SHA-256 hash prefix (16 chars) of system prompt

  -- ═══ Performance (3) ═══
  latency_ms                INTEGER,                 -- total end-to-end (duplicates latency_breakdown.total_ms for index use)
  ttfs_ms                   INTEGER,                 -- time to first sound (user-perceived)
  latency_breakdown         TEXT,                    -- JSON, see schema below

  -- ═══ Lifecycle (6) ═══
  end_reason                TEXT,                    -- enum: success/interrupted/error/timeout/cancelled
  error                     TEXT,                    -- exception message + short traceback, NULL on success
  finish_reason             TEXT,                    -- provider: stop/length/tool_calls/content_filter/NULL
  cost_usd                  REAL,                    -- computed from llm_pricing × tokens
  outcome_signal            INTEGER,                 -- -1 / 0 / +1 / NULL
  outcome_at_turn_id        INTEGER,                 -- FK → trace.id of the turn that inferred this outcome

  CHECK (outcome_signal IS NULL OR outcome_signal IN (-1, 0, 1)),
  CHECK (end_reason IS NULL OR end_reason IN ('success', 'interrupted', 'error', 'timeout', 'cancelled'))
);

CREATE INDEX idx_trace_session  ON trace(session_id, turn_id);
CREATE INDEX idx_trace_path     ON trace(path_taken, created_at DESC);
CREATE INDEX idx_trace_user     ON trace(user_id, created_at DESC);
CREATE INDEX idx_trace_outcome  ON trace(outcome_signal, created_at DESC)
  WHERE outcome_signal IS NOT NULL;
CREATE INDEX idx_trace_errors   ON trace(created_at DESC)
  WHERE error IS NOT NULL;
```

### Design principles baked in (do not violate)

1. **One column one question** — every column must correspond to a concrete query pattern
2. **Stable enums in columns + CHECK constraints** — `outcome_signal` / `end_reason` are stable 3-value / 5-value enums
3. **path_taken does NOT have DB CHECK** — the 8-value enum is stable per user but DB-level CHECK blocks future additions (SQLite ALTER limitation). Enforce via Python Enum in `trace.py`.
4. **Volatile / provider-specific structure → JSON columns** — 5 JSON columns absorb evolution without ALTER TABLE
5. **`latency_ms` + `latency_breakdown` coexist intentionally** — column for `AVG()` / index; JSON for drill-down

---

## Enum Values (lock these in Python Enums in `memory/trace.py`)

```python
class TriggerSource(str, Enum):
    WAKE_WORD = "wake_word"       # voice wake via "Hey Jarvis"
    CONTINUATION = "continuation"  # turn after interrupt-resume
    WEB_TEXT = "web_text"          # web UI text input (handle_text entry)
    WEB_VOICE = "web_voice"        # web UI microphone
    PROACTIVE = "proactive"        # Jarvis-initiated (Phase 4, future)
    TEST = "test"                  # system_tests harness


class PathTaken(str, Enum):
    UNKNOWN = "unknown"
    RESUME = "resume"
    FAREWELL = "farewell"
    MEMORY_SHORTCUT = "memory_shortcut"
    KEYWORD_RULE = "keyword_rule"
    MEMORY_L1 = "memory_l1"
    LOCAL = "local"
    CLOUD = "cloud"


class EndReason(str, Enum):
    SUCCESS = "success"            # normal completion (default)
    INTERRUPTED = "interrupted"    # user spoke during TTS, soft-stopped
    ERROR = "error"                # exception raised
    TIMEOUT = "timeout"            # component timeout
    CANCELLED = "cancelled"        # user explicit cancel command


class FinishReason(str, Enum):
    STOP = "stop"                  # natural end
    LENGTH = "length"              # max_tokens hit
    TOOL_CALLS = "tool_calls"      # model invoked tools
    CONTENT_FILTER = "content_filter"  # provider policy
```

### Overlap clarification: `end_reason` vs `finish_reason`

- `end_reason` = why the **entire turn** ended (Jarvis-level)
- `finish_reason` = what the **LLM generation** terminated with (provider-level)

They can differ. Example: LLM finishes cleanly (`finish_reason=stop`) but user interrupts TTS (`end_reason=interrupted`). Both must be recorded.

---

## JSON Column Schemas (freeze these — consumers expect stable shape)

### `input_metadata`

Writers SHOULD always emit the same keys (with `null` when data is absent), NOT omit keys:

```json
{
  "asr_text_raw": "str | null",       // pre-normalize raw text; null if equal to user_text
  "asr_confidence": "float | null",    // 0-1 from ASR; null if not provided
  "vad_duration_ms": "int | null",     // speech audio duration
  "audio_path": "str | null"           // recorded .wav path; null if not saved
}
```

For this migration: only `asr_text_raw` will be populated (from `_raw_text` at jarvis.py:699). Other fields stay null — don't invent values.

### `tool_calls`

Array, one entry per tool invocation:

```json
[
  {
    "name": "str",          // tool name (e.g., "turn_on_light")
    "args": {},             // arg dict (JSON-serializable)
    "result": {},           // result object or string; error payload if tool failed
    "ms": "int"             // execution time milliseconds
  }
]
```

**Bug to fix**: `jarvis.py:1148-1152` currently writes `result=""` and `ms=0`. These are empty placeholders. Actually populate them from tool invocation results. Look at the tool_use loop in `_process_turn` — the tool results and timings are computed; thread them into the trace tool_calls entries.

### `llm_metadata`

```json
{
  "provider": "str",                      // "xai" | "openai" | "anthropic" | "groq" | "cerebras"
  "conv_id": "str | null",                // xAI x-grok-conv-id header value
  "response_id": "str | null",            // chatcmpl-... / msg_... / xAI equivalent
  "streaming": "bool",                    // was this streamed (almost always true for cloud path)
  "fallback_used": "bool",                // did primary→secondary fallback fire
  "truncated_by_interrupt": "bool",       // did end_reason=interrupted cut generation
  "full_response": "str | null",          // if truncated: full LLM output; else null (assistant_text has it)
  "cache_creation_input_tokens": "int | null"  // Anthropic-only; null otherwise
}
```

Notes:
- `response_id` lives here (we decided not to promote to column — low frequency use)
- `cache_creation_input_tokens` lives here (Anthropic-only, 90%+ NULL if promoted to column)

### `memory_query_ids`

```json
{
  "observation_ids": [1, 2, 3],       // IDs from observations table
  "top_k_scores": [0.89, 0.76, 0.65]  // aligned scores
}
```

Store **IDs only**, not observation content. Content is rehydrated via `JOIN observations`.

### `latency_breakdown`

All keys always present (null when phase didn't execute):

```json
{
  "asr_ms": "int | null",
  "route_ms": "int | null",
  "memory_query_ms": "int | null",
  "direct_answer_ms": "int | null",
  "local_exec_ms": "int | null",
  "llm_first_ms": "int | null",    // first LLM token
  "tts_first_ms": "int | null",    // first TTS audio chunk played
  "total_ms": "int"                // always set (= latency_ms column)
}
```

---

## Task Breakdown

### Task 1 — Migration Script (new file: `memory/trace_migration.py`)

**Goal**: Idempotent function that upgrades an existing v2 (16-col) trace table to v3 (31-col).

**Why rebuild, not ALTER**: SQLite cannot add CHECK constraints via ALTER. Since we want 2 CHECKs (outcome_signal, end_reason), we must rebuild.

**Steps**:

```python
def migrate_trace_v2_to_v3(conn: sqlite3.Connection) -> bool:
    """Upgrade trace table from v2 (16 cols) to v3 (31 cols).

    Idempotent: returns False and does nothing if already v3.
    Returns True if migration happened.

    Checks for v3 by looking for 'user_id' column in the existing trace table.
    """
    # 1. Detect current version
    cursor = conn.execute("PRAGMA table_info(trace)")
    columns = {row[1] for row in cursor.fetchall()}
    if "user_id" in columns:
        return False  # already v3

    # 2. Backup via copy (not SQL) — caller should have backed up the DB file,
    #    but we also rename old table as a safety net
    conn.executescript("ALTER TABLE trace RENAME TO trace_v2_backup;")

    # 3. Create new v3 table (full schema with CHECKs)
    conn.executescript("""
        CREATE TABLE trace (
            ... (full schema from above)
        );
    """)

    # 4. Copy old data, defaulting new columns
    conn.executescript("""
        INSERT INTO trace (
            id, session_id, turn_id, user_id, created_at,
            user_text, assistant_text, user_emotion, tts_emotion,
            path_taken, tool_calls, llm_model,
            llm_tokens_in, llm_tokens_out, latency_ms,
            outcome_signal, outcome_at_turn_id
        )
        SELECT
            id, session_id, turn_id, 'default_user', created_at,
            user_text, assistant_text, user_emotion, tts_emotion,
            path_taken, tool_calls, llm_model,
            llm_tokens_in, llm_tokens_out, latency_ms,
            outcome_signal, outcome_at_turn_id
        FROM trace_v2_backup;
    """)

    # 5. Create indexes
    conn.executescript("""
        CREATE INDEX idx_trace_session  ON trace(session_id, turn_id);
        CREATE INDEX idx_trace_path     ON trace(path_taken, created_at DESC);
        CREATE INDEX idx_trace_user     ON trace(user_id, created_at DESC);
        CREATE INDEX idx_trace_outcome  ON trace(outcome_signal, created_at DESC)
          WHERE outcome_signal IS NOT NULL;
        CREATE INDEX idx_trace_errors   ON trace(created_at DESC)
          WHERE error IS NOT NULL;
    """)

    # 6. Keep trace_v2_backup for one week (caller can DROP after verifying v3 works).
    #    Do NOT drop it here — that decision is the operator's.

    conn.commit()
    return True
```

Call this from `TraceLog._init_db()` AFTER the normal `CREATE TABLE IF NOT EXISTS`. The init must handle both:
- Fresh install (empty DB) → `CREATE TABLE` with v3 schema directly
- Existing v2 → `ALTER TABLE RENAME` → create v3 → copy → done

Logic:
```python
def _init_db(self):
    conn = self._get_conn()
    # Detect if trace exists at all
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trace'"
    )
    exists = cursor.fetchone() is not None

    if not exists:
        # Fresh install — create v3 directly
        conn.executescript(FULL_V3_SCHEMA_SQL)
        conn.commit()
        return

    # Table exists — maybe v2, maybe v3
    migrate_trace_v2_to_v3(conn)
```

### Task 2 — `TraceLog` Class API

Update `memory/trace.py`:

```python
def log_turn(
    self,
    *,
    # --- Identity ---
    session_id: str,
    turn_id: int,
    user_id: str = "default_user",

    # --- Input / Output ---
    user_text: str,
    assistant_text: str,
    user_emotion: str | None = None,
    tts_emotion: str | None = None,
    input_metadata: dict | None = None,  # auto-serialized to JSON

    # --- Triggering / Routing ---
    trigger_source: str | None = None,
    parent_trace_id: int | None = None,
    path_taken: str | None = None,
    intent_route_score: float | None = None,

    # --- Tools ---
    tool_calls: list[dict] | None = None,

    # --- LLM ---
    llm_model: str | None = None,
    llm_tokens_in: int | None = None,
    llm_tokens_out: int | None = None,
    cache_read_input_tokens: int | None = None,
    llm_metadata: dict | None = None,

    # --- Memory ---
    memory_query_ids: dict | None = None,

    # --- Context ---
    prompt_version: str | None = None,

    # --- Performance ---
    latency_ms: int | None = None,
    ttfs_ms: int | None = None,
    latency_breakdown: dict | None = None,

    # --- Lifecycle ---
    end_reason: str | None = None,
    error: str | None = None,
    finish_reason: str | None = None,
    cost_usd: float | None = None,
    # (outcome_signal / outcome_at_turn_id set via update_outcome, not log_turn)
) -> int:
    """Log a single conversation turn. Returns the inserted row ID."""
    ...
```

The body builds the SQL INSERT with all 29 columns (ID and created_at auto-populated). JSON fields (`input_metadata`, `tool_calls`, `llm_metadata`, `memory_query_ids`, `latency_breakdown`) serialize via `json.dumps(..., ensure_ascii=False)` when not None, store as NULL when None.

Update `update_outcome` to also accept `at_turn_id`:

```python
def update_outcome(
    self,
    trace_id: int,
    signal: int,
    at_turn_id: int | None = None,
) -> None:
    """Set outcome signal (and optionally the inferring turn) for a trace row.

    Args:
        trace_id: Trace row to update.
        signal: -1 / 0 / +1.
        at_turn_id: The trace.id of the turn from which this outcome was inferred.
    """
    ...
```

Add new query method for MCP / debug use:

```python
def query_for_debug(
    self,
    session_id: str | None = None,
    user_id: str | None = None,
    hours: int = 24,
    only_errors: bool = False,
    only_interrupted: bool = False,
) -> list[dict]:
    """Fetch traces matching filters, newest first. Deserializes JSON columns.

    Use by MCP server (future) and tests.
    """
    ...
```

Deserialize all 5 JSON columns in the returned dicts (same pattern as existing `query_for_observer`).

### Task 3 — New module: `memory/outcome_detector.py`

**Purpose**: scan the current turn's user_text for conservative positive/negative signals, return -1 / 0 / +1 / None.

```python
"""Conservative outcome signal detector for trace feedback.

Scans user utterances for clear approval/disapproval patterns. Designed
to minimize false positives — ambiguous cases return None (NULL outcome).

Phase 3 Step 1 treats NULL as 'unknown' and does not filter on it, so
erring conservative keeps the training pipeline honest.
"""
from __future__ import annotations

import re

# Positive: short approving utterances, no negation
_POSITIVE_PATTERNS = [
    r"^好的?[。!！.]?$",
    r"^(好嘞|行|对|对的|没错|可以|棒|厉害)[。!！.]?$",
    r"^(谢谢|多谢|感谢)(你|啦|了)?[。!！.]?$",
    r"^就是(这样|这个|它)[。!！.]?$",
]

# Negative: clear correction / disagreement
_NEGATIVE_PATTERNS = [
    r"^不对[。!！.]?$",
    r"^错了?[。!！.]?$",
    r"^(再来|重新|重试)(一?遍|一?次)?[。!！.]?$",
    r"^不是(这样|这个|它|啊)?[。!！.]?$",
    r"^(你)?理解错了?[。!！.]?$",
    r"^(不|别)[。!！.]?$",
]

_POS_RE = [re.compile(p) for p in _POSITIVE_PATTERNS]
_NEG_RE = [re.compile(p) for p in _NEGATIVE_PATTERNS]


def detect_outcome(user_text: str) -> int | None:
    """Return +1 / -1 / None based on conservative pattern match.

    Only fires on short, unambiguous utterances. Longer user utterances
    that *contain* the trigger word (e.g., "谢谢你刚才说的那件事其实...")
    are NOT matched because the patterns are anchored to start/end with
    optional punctuation only.
    """
    text = user_text.strip()
    if not text or len(text) > 30:  # long utterances: skip
        return None
    for r in _POS_RE:
        if r.match(text):
            return 1
    for r in _NEG_RE:
        if r.match(text):
            return -1
    return None
```

**Tests**: cover both patterns, long utterances (should return None), ambiguous cases, empty/whitespace.

### Task 4 — Cost Calculator

Add pricing table to `config.yaml` (at top level):

```yaml
# LLM pricing per 1M tokens (USD)
llm_pricing:
  grok-4.20:           {input: 3.00, output: 15.00, cache_read: 0.30}
  grok-reasoning:      {input: 5.00, output: 20.00, cache_read: 0.50}
  grok-4-1-fast:       {input: 0.50, output: 1.50,  cache_read: 0.05}
  claude-opus-4-7:     {input: 15.00, output: 75.00, cache_read: 1.50, cache_write: 18.75}
  claude-sonnet-4-6:   {input: 3.00, output: 15.00, cache_read: 0.30, cache_write: 3.75}
  llama-3.3-70b:       {input: 0.59, output: 0.79,  cache_read: 0.0}   # Groq
  llama3.1-8b:         {input: 0.10, output: 0.10,  cache_read: 0.0}   # Cerebras
  gpt-4o-mini:         {input: 0.15, output: 0.60,  cache_read: 0.075}
```

Keep updated as prices change; these are starter values—verify against current provider pricing.

Add helper in `memory/trace.py` (or new `memory/pricing.py`):

```python
def compute_cost_usd(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read_in: int | None,
    cache_write_in: int | None,
    pricing_table: dict,
) -> float | None:
    """Compute USD cost for one LLM turn. Returns None when inputs missing."""
    if not model or tokens_in is None or tokens_out is None:
        return None
    entry = pricing_table.get(model)
    if not entry:
        return None  # unknown model → skip (warn in logs once)

    # Normal input is (tokens_in - cache_read_in) since cache hits bill at cache_read rate
    non_cached_in = tokens_in - (cache_read_in or 0) - (cache_write_in or 0)
    non_cached_in = max(0, non_cached_in)

    cost = (
        non_cached_in     * entry["input"]       / 1_000_000 +
        tokens_out        * entry["output"]      / 1_000_000 +
        (cache_read_in or 0)  * entry.get("cache_read", 0)  / 1_000_000 +
        (cache_write_in or 0) * entry.get("cache_write", entry["input"] * 1.25) / 1_000_000
    )
    return round(cost, 6)
```

### Task 5 — `jarvis.py` Instrumentation (12 hook points)

The existing single `log_turn` call at **L1154** becomes the aggregation point. Data gets staged into `self._last_*` attributes throughout the turn, then assembled and flushed.

Initialize all at L707-719 (where `_last_path` etc. are currently reset):

```python
# ── Trace v3 staging (reset at turn start) ──
self._last_route = None
self._last_path = "unknown"
self._last_device_ops = []
self._last_memory_hits = ""
self._last_timings: dict[str, int | None] = {
    "asr_ms": None, "route_ms": None, "memory_query_ms": None,
    "direct_answer_ms": None, "local_exec_ms": None,
    "llm_first_ms": None, "tts_first_ms": None, "total_ms": None,
}
self._last_tts_emotion: str | None = None
self._last_llm_metadata: dict = {
    "provider": None, "conv_id": None, "response_id": None,
    "streaming": False, "fallback_used": False,
    "truncated_by_interrupt": False, "full_response": None,
    "cache_creation_input_tokens": None,
}
self._last_llm_usage: dict = {
    "tokens_in": None, "tokens_out": None,
    "cache_read_input_tokens": None,
}
self._last_finish_reason: str | None = None
self._last_memory_query_ids: dict | None = None
self._last_intent_route_score: float | None = None
self._last_interrupted = False
self._last_error: str | None = None
self._last_trace_id: int | None = None  # for outcome inference across turns
self._last_parent_trace_id: int | None = None
```

Add to `__init__` (one-time):
```python
import hashlib
self._prompt_version = hashlib.sha256(
    self.personality_prompt.encode("utf-8")
).hexdigest()[:16]
```

(If `personality_prompt` isn't the right variable name — grep for the system prompt string and use its value. Commit to a stable reference.)

#### Hook points by location

**1. Trigger source** — entry points set this:
- `handle_text()` (L1198): `self._trigger_source_override = "web_text"` before calling `_process_turn`, or pass as kwarg through `_process_turn(text, ..., trigger_source="web_text")`
- The voice wake path in `run()` / main loop: default `trigger_source="wake_word"`
- After an interrupt-resume: `trigger_source="continuation"`
- System test harness: `trigger_source="test"`

Easiest: add `trigger_source: str = "wake_word"` as `_process_turn` kwarg with default, override at each entry.

**2. input_metadata** — at L702, after normalize:
```python
_input_meta = {
    "asr_text_raw": _raw_text if _raw_text != text else None,
    "asr_confidence": None,       # ASR layer doesn't currently return this
    "vad_duration_ms": None,      # VAD layer doesn't expose this yet
    "audio_path": None,            # audio not persisted
}
```
Don't omit keys; emit `null` for unknown.

**3. intent_route_score** — inspect the router call. If it returns a score, capture. If not, leave None.
- Grep for where Groq/Cerebras is called for routing (likely in `core/intent_router.py` or similar)
- If the router returns raw confidence, pass it back through `_process_turn`'s local route result
- If not available without router refactor: leave None for this migration, note in a comment

**4. memory_query_ids** — after memory query (near L850, `_last_timings["memory_query_ms"]` is set there):
```python
# Capture memory query results
if memory_hits and isinstance(memory_hits, list):
    self._last_memory_query_ids = {
        "observation_ids": [h.get("id") for h in memory_hits if h.get("id")],
        "top_k_scores":    [h.get("score") for h in memory_hits if h.get("score") is not None],
    }
```
Exact field names depend on `memory_manager.query()` return shape—inspect and match.

**5. tts_emotion** — in TTS dispatch path, wherever emotion gets passed to TTS engine:
```python
self._last_tts_emotion = tts_emotion  # whatever local var holds the emotion tag
```
Look for `tts` or `audio_stream_player` calls that take an emotion parameter.

**6. ttfs_ms** — capture the first TTS audio chunk timestamp. Where is the first audio chunk triggered?
- If `AudioStreamPlayer` has a "first-chunk" hook: register callback that records `time.monotonic() - turn_start_time`
- If not, add one — in `core/audio_stream_player.py` (or wherever the class lives), invoke callback on first `write_chunk()` of a turn
- Store in `self._last_timings["tts_first_ms"]` AND `self._last_ttfs_ms`

**7. LLM usage + metadata** — at LLM response completion point (L1037 area captures `llm_first_ms`; find where the stream ends):
```python
# Populate from final LLM response object
response_usage = llm_response.get("usage", {}) if isinstance(llm_response, dict) else {}
self._last_llm_usage["tokens_in"] = response_usage.get("prompt_tokens") or response_usage.get("input_tokens")
self._last_llm_usage["tokens_out"] = response_usage.get("completion_tokens") or response_usage.get("output_tokens")

# Cache tokens (provider-specific)
prompt_details = response_usage.get("prompt_tokens_details", {})
self._last_llm_usage["cache_read_input_tokens"] = (
    prompt_details.get("cached_tokens")              # OpenAI / xAI
    or response_usage.get("cache_read_input_tokens")  # Anthropic
    or 0
)

# Anthropic-only cache creation tokens
cache_create = response_usage.get("cache_creation_input_tokens")
if cache_create is not None:
    self._last_llm_metadata["cache_creation_input_tokens"] = cache_create

# Response ID and finish_reason
self._last_llm_metadata["response_id"] = (
    llm_response.get("id")        # all providers
)
self._last_finish_reason = llm_response.get("choices", [{}])[0].get("finish_reason")

# Provider identification from current llm preset
self._last_llm_metadata["provider"] = self.llm.current_provider  # e.g., "xai"
self._last_llm_metadata["streaming"] = True  # cloud path is always streamed
```

Exact attribute names depend on `self.llm` — grep `core/llm.py` for the response shape.

**8. fallback_used** — if the LLM layer falls back from primary to secondary model (e.g., Groq failed → Cerebras), set:
```python
self._last_llm_metadata["fallback_used"] = True
```
Set at the fallback site in `core/llm.py` or router.

**9. truncated_by_interrupt** — at the interrupt handling site (where `self._cancel` gets set mid-LLM-stream):
```python
self._last_llm_metadata["truncated_by_interrupt"] = True
# Also save the full LLM output before truncation for analysis
self._last_llm_metadata["full_response"] = intended_full_text
self._last_interrupted = True
```

**10. end_reason + error** — wrap the body of `_process_turn` in try/except:
```python
def _process_turn(self, text, ...):
    turn_start_time = time.monotonic()
    try:
        # ... existing body ...
        # Determine end_reason at the end:
        if self._cancel.is_set() or self._last_interrupted:
            end_reason = "interrupted"
        else:
            end_reason = "success"
    except Exception as exc:
        end_reason = "error"
        import traceback
        self._last_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
        response_text = response_text or ""  # ensure non-null
        # Re-raise after we get to the finally? Or swallow? Current jarvis.py
        # behavior for errors is to propagate — preserve that. Log before re-raise:
        self._flush_trace(
            session_id, session_turn, user_id, text, response_text,
            emotion, end_reason, turn_start_time,
        )
        raise
    else:
        self._flush_trace(
            session_id, session_turn, user_id, text, response_text,
            emotion, end_reason, turn_start_time,
        )
```

Extract the `log_turn` call into a helper `_flush_trace()` that assembles all `self._last_*` state and cost computation, then calls `trace_log.log_turn()`.

**11. Outcome detection** — at the start of `_process_turn`, BEFORE the main processing:
```python
from memory.outcome_detector import detect_outcome

outcome = detect_outcome(text)
if outcome is not None and self._last_trace_id is not None:
    # This turn's text implies an outcome for the previous turn
    # (Update happens AFTER this turn logs, so we have this_turn_id to pass as at_turn_id)
    self._pending_outcome_update = (self._last_trace_id, outcome)
    # Also: this turn is semantically a correction → parent_trace_id link
    if outcome == -1:
        self._last_parent_trace_id = self._last_trace_id
else:
    self._pending_outcome_update = None
```

Then in `_flush_trace`, AFTER `log_turn` returns the new trace_id:
```python
new_trace_id = self.trace_log.log_turn(...)
self._last_trace_id = new_trace_id  # for next turn's outcome detection

if self._pending_outcome_update:
    prev_id, signal = self._pending_outcome_update
    self.trace_log.update_outcome(prev_id, signal=signal, at_turn_id=new_trace_id)
    self._pending_outcome_update = None
```

**12. cost_usd** — at `_flush_trace` time:
```python
cost = compute_cost_usd(
    model=self._last_llm_usage.get("model") or llm_model,
    tokens_in=self._last_llm_usage.get("tokens_in"),
    tokens_out=self._last_llm_usage.get("tokens_out"),
    cache_read_in=self._last_llm_usage.get("cache_read_input_tokens"),
    cache_write_in=self._last_llm_metadata.get("cache_creation_input_tokens"),
    pricing_table=self.config.get("llm_pricing", {}),
)
```
Pass `cost_usd=cost` to log_turn.

### Task 6 — Tests

Update `tests/test_trace.py` and add new test files.

**Test matrix** (minimum coverage):

1. **Migration tests** — `tests/test_trace_migration.py`:
   - Fresh install: v3 schema created directly
   - v2 → v3 migration: existing rows copied with `user_id='default_user'`, new cols NULL
   - Idempotent: second call returns False, no change
   - Backup table `trace_v2_backup` exists after migration

2. **TraceLog v3 API tests** — extend `tests/test_trace.py`:
   - `log_turn` with all 29 writable kwargs populated → verify every column in DB matches
   - `log_turn` with only required kwargs → verify JSON columns are NULL
   - JSON columns serialize: dict in → JSON text in DB → dict out via query
   - `update_outcome(trace_id, signal, at_turn_id)` sets both columns
   - CHECK constraint: `log_turn` with invalid `outcome_signal` value raises `IntegrityError`
   - CHECK constraint: invalid `end_reason` raises `IntegrityError`
   - `query_cloud_traces` still works post-migration
   - `query_for_debug` filters by session / user / error / interrupted / hours

3. **OutcomeDetector tests** — `tests/test_outcome_detector.py`:
   - Clear positives: "好的", "谢谢", "对" → +1
   - Clear negatives: "不对", "错了", "再来" → -1
   - Embedded triggers (long utterance): "谢谢你刚才说的那件事其实…" → None
   - Empty / whitespace → None
   - Long utterance (>30 chars) → None
   - Punctuation tolerance: "好的。" "好的!" "好的" all → +1

4. **Cost compute tests** — `tests/test_pricing.py`:
   - Known model with all inputs → correct USD
   - Unknown model → None (no crash)
   - Missing tokens → None
   - Cache read reduces non-cached input
   - Anthropic cache write at 1.25x

5. **jarvis.py integration** — update `tests/test_jarvis_trace.py` (create if missing):
   - Full turn via `handle_text` writes a complete trace row with all populated fields (per what's feasible in tests)
   - `trigger_source="web_text"` recorded
   - `prompt_version` set and stable across turns
   - `end_reason` correctly set to "success" on normal, "error" on exception

**Do NOT delete or significantly alter existing tests** — they must still pass. Extend them.

**Test commands**:
```bash
cd /Users/alllllenshi/Projects/jarvis
python -m pytest tests/test_trace.py tests/test_trace_migration.py tests/test_outcome_detector.py tests/test_pricing.py -v
python -m pytest tests/ -q   # full suite must stay green
```

### Task 7 — Smoke Test

After all tests pass:

1. Back up the current DB: `cp data/memory/jarvis_memory.db data/memory/jarvis_memory.db.bak.pre_v3`
2. Start Jarvis once (no wake word for speed): `python jarvis.py --no-wake`
3. Press Enter to trigger one voice turn — speak something simple ("你好")
4. Inspect the new row:
   ```bash
   sqlite3 data/memory/jarvis_memory.db "SELECT * FROM trace ORDER BY id DESC LIMIT 1;" -header -column
   ```
5. Verify: user_id set, path_taken set, end_reason='success', cost_usd populated (if cloud path), all JSON columns are valid JSON (not raw Python dict repr)
6. Exit. Backup is safe.

If the smoke test passes, commit. If not, diagnose before committing.

---

## Boundaries — DO NOT touch

1. `data/speechbrain_model/` and `data/sensevoice-small-int8/` — per CLAUDE.md
2. `personality.py` without explicit user approval — per feedback_prompt_approval
3. `config.yaml`'s `asr_corrections` / `asr_aliases` — don't add new entries here
4. `memory/store.py` observations schema — the `source_turn_id` FK still works; no schema change needed on that side
5. The legacy `behavior_log.py` writes in `jarvis.py` — leave them as-is for this migration (don't remove yet). Next phase can remove after verifying v3 has everything behavior_log used to provide.
6. `memory/trace.py`'s `query_for_observer()` — keep it working for backwards compat. Observer in `memory/manager.py` currently passes turn_data directly, not via trace query. No change needed.
7. Wake word engine, VAD, ASR model files — none of these change for this task
8. Do not modify any `tests/test_*.py` except to extend (not delete or weaken)

---

## Commit Strategy

One feature branch, multiple commits (one per logical task), commit messages in English. Examples:

```
feat(trace): add trace_v2 → v3 migration with rebuild strategy
feat(trace): extend TraceLog.log_turn signature to v3 schema (29 cols)
feat(memory): add OutcomeDetector for semantic outcome signal
feat(memory): add LLM cost calculator with pricing table
feat(jarvis): instrument _process_turn for v3 trace fields
test(trace): cover v3 schema, migration, outcome detector, pricing
```

No `Co-Authored-By` lines. No emojis anywhere.

**Never push.** User will push when ready.

---

## Done Criteria (all must be true)

- [ ] `memory/trace.py` has 31-column schema matching this doc exactly
- [ ] `memory/trace_migration.py` exists and is idempotent; auto-runs on `TraceLog` init
- [ ] `memory/outcome_detector.py` exists with conservative regex patterns
- [ ] Pricing table in `config.yaml` under `llm_pricing`
- [ ] `jarvis.py` writes all 29 writable fields on every turn (null where data genuinely absent)
- [ ] `update_outcome` called when outcome detected; `at_turn_id` populated
- [ ] All 4 CHECK constraints in place (2 at table level)
- [ ] 5 indexes created
- [ ] `python -m pytest tests/ -q` — full suite green
- [ ] Smoke test: one live turn produces a fully-populated row, no nulls except where genuinely N/A (e.g., `llm_*` NULL for non-cloud path)
- [ ] Existing `data/memory/jarvis_memory.db` migrates successfully; `trace_v2_backup` table preserved
- [ ] No hardcoded paths, keys, or magic strings
- [ ] Type hints + Google docstrings on every new public function

---

## Open Questions for the Executor

If you encounter any of these, flag to the user before proceeding (do not guess):

1. **ASR / VAD doesn't expose confidence or duration** — confirm fields stay null for this migration rather than changing ASR layer in scope
2. **Router doesn't return confidence score** — same: keep `intent_route_score` null, note as future work
3. **`AudioStreamPlayer` has no first-chunk callback** — confirm whether to add a minimal callback OR skip `ttfs_ms` for this migration
4. **LLM layer response shape differs from expected** — grep `core/llm.py`, confirm usage dict field names before coding
5. **If any existing test fails due to schema change** — report the failure rather than silently weakening the test

---

## One-Line Summary

Migrate `trace` from "埋点良好但无 consumer 的 16 列黑洞" to "Phase 3-ready + MCP-queryable 的 31 列带 feedback loop 的完整观测表"，一次打完，tests 全绿，smoke tested，不推 remote。
