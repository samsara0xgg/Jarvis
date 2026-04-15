# Observer Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/observer_bench.py`, a CLI that benchmarks 8 candidate Observer models on Chinese dialogue → structured observation extraction, using function-calling (tool use) as the unified output path.

**Architecture:** Single-file async script (~700 lines). Zero invasion of `scripts/bench_llm_v3.py` — observer_bench rewrites its own `call_with_tools_*` functions, importing only pure data/compute helpers (`ModelSpec`, `extract_cache_metrics`, `calc_cost`, `make_bust_prefix`) from v3. Fixture workflow: Allen writes `seeds.yaml` → `--observer-generate` calls Opus to produce `fx_XXX.draft.json` → Allen edits & renames to `fx_XXX.json` for approval. Pilot-5 gates full run; any model with `tool_success<80%` or `F1<0.3` is auto-excluded via `run_meta.json`.

**Tech Stack:** Python 3.11+ · PEP 723 inline deps · `anthropic` + `openai` + `google-generativeai` + `groq` SDKs · `pyyaml` (new) · `tiktoken` · `tqdm` · reuses v3 infrastructure.

**Spec reference:** `docs/superpowers/specs/2026-04-15-observer-bench-design.md`

---

## File Structure

```
scripts/
├── bench_llm_v3.py          # MODIFY: append 2 entries to MODEL_CATALOG
└── observer_bench.py        # CREATE ~700 lines:
                             #   §1 Constants (SYSTEM_PROMPT, TOOL_DEF, CANDIDATES)
                             #   §2 Dataclasses (Seed, Fixture, Scores, ObserverResult)
                             #   §3 Fixture I/O (load_seeds, load_approved, save_draft)
                             #   §4 Prompt + tool kwargs builders
                             #   §5 Provider callers (Anthropic / OpenAI-compat / Gemini)
                             #   §6 Retry + result assembly
                             #   §7 Warmup + active spec resolution
                             #   §8 Evaluator (evaluate + aggregate)
                             #   §9 Fixture generator (Opus → .draft.json)
                             #   §10 Output (CSV + summary.md + run_meta.json)
                             #   §11 CLI + orchestration

tests/
└── test_observer_bench.py   # CREATE: unit tests (fixtures/evaluator/prompt, no real API)

bench_fixtures/observer_cn/  # CREATE + git tracked:
├── seeds.yaml               # Allen writes (task 15, manual)
├── fx_001.json              # Allen approves (task 16, manual: mv .draft.json)
└── ...

.gitignore                   # MODIFY: add `*.draft.json` pattern
```

---

## Task 1: Scaffold + v3 catalog additions + .gitignore

**Files:**
- Modify: `/Users/alllllenshi/Projects/jarvis/scripts/bench_llm_v3.py` (append 2 entries to `MODEL_CATALOG`)
- Create: `/Users/alllllenshi/Projects/jarvis/scripts/observer_bench.py`
- Create: `/Users/alllllenshi/Projects/jarvis/tests/test_observer_bench.py`
- Modify: `/Users/alllllenshi/Projects/jarvis/.gitignore` (add `*.draft.json`)
- Create: `/Users/alllllenshi/Projects/jarvis/bench_fixtures/observer_cn/` (empty dir, git-tracked via `.gitkeep`)
- Modify: `/Users/alllllenshi/Projects/jarvis/tests/test_bench_llm_v3.py` (update catalog count assertion)

- [ ] **Step 1: Append 2 ModelSpec entries to v3 MODEL_CATALOG**

Read `/Users/alllllenshi/Projects/jarvis/scripts/bench_llm_v3.py` lines 97-108, find the closing `)` of `MODEL_CATALOG` tuple. Insert before `ModelSpec("groq", ...)` and before closing `)`:

```python
    # --- Observer bench additions (2026-04-15) ---
    ModelSpec("google",   "gemini-2.5-flash",
              ("models/gemini-2.5-flash", "gemini-flash-latest"),
              0.30,  2.50, 1.00, 0.25, 4096),
    ModelSpec("deepseek", "deepseek-chat",
              ("deepseek-v3.2", "deepseek-v3"),
              0.27,  1.10, 1.00, 0.10, 1024),
```

(Exactly after the existing `grok-4-0709` line, before `groq/llama-3.3-70b-versatile`.)

- [ ] **Step 2: Update v3 catalog count test**

Modify `/Users/alllllenshi/Projects/jarvis/tests/test_bench_llm_v3.py` line 17:

```python
    assert len(b.MODEL_CATALOG) == 14  # 8 core + 4 xAI variants + 2 observer bench adds
```

Run: `python -m pytest tests/test_bench_llm_v3.py::test_module_imports -v`
Expected: PASS

- [ ] **Step 3: Create `.gitignore` entry for draft fixtures**

Append to `/Users/alllllenshi/Projects/jarvis/.gitignore`:

```
# Observer bench: draft fixtures before Allen approval
bench_fixtures/observer_cn/*.draft.json
```

- [ ] **Step 4: Create fixtures dir with .gitkeep**

Run:
```bash
mkdir -p /Users/alllllenshi/Projects/jarvis/bench_fixtures/observer_cn
touch /Users/alllllenshi/Projects/jarvis/bench_fixtures/observer_cn/.gitkeep
```

- [ ] **Step 5: Scaffold `scripts/observer_bench.py`**

Create `/Users/alllllenshi/Projects/jarvis/scripts/observer_bench.py`:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.42",
#   "openai>=1.50",
#   "google-generativeai>=0.8",
#   "groq>=0.11",
#   "tiktoken>=0.8",
#   "tqdm>=4.66",
#   "pyyaml>=6.0",
#   "plotly>=5.18",
#   "pandas>=2.0",
# ]
# ///
"""Observer Bench — 中文对话 → structured observation 抽取能力对照.

Zero invasion of bench_llm_v3.py. Reuses ModelSpec / calc_cost / extract_cache_metrics
/ make_bust_prefix as pure helpers, rewrites tool-call version of provider callers here.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable
from uuid import uuid4

# Make bench_llm_v3 importable (same scripts/ dir)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_llm_v3 as v3  # noqa: E402

LOGGER = logging.getLogger("observer_bench")

# ===== §1 CONSTANTS (filled in later tasks) =====
# ===== §2 DATACLASSES (later tasks) =====
# ===== §3 FIXTURE I/O (later tasks) =====
# ===== §4 PROMPT + TOOL BUILDERS (later tasks) =====
# ===== §5 PROVIDER CALLERS (later tasks) =====
# ===== §6 RETRY + ASSEMBLY (later tasks) =====
# ===== §7 WARMUP (later tasks) =====
# ===== §8 EVALUATOR (later tasks) =====
# ===== §9 FIXTURE GENERATOR (later tasks) =====
# ===== §10 OUTPUT (later tasks) =====
# ===== §11 CLI (later tasks) =====


def main() -> None:
    raise NotImplementedError("Built in Task 14")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Scaffold `tests/test_observer_bench.py`**

Create `/Users/alllllenshi/Projects/jarvis/tests/test_observer_bench.py`:

```python
"""Unit tests for scripts/observer_bench.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import observer_bench as ob  # noqa: E402


def test_module_imports():
    """Smoke: module imports cleanly + imports v3 as pure helper."""
    assert hasattr(ob, "v3")
    assert hasattr(ob.v3, "ModelSpec")
    assert hasattr(ob.v3, "calc_cost")
    assert hasattr(ob.v3, "extract_cache_metrics")
```

- [ ] **Step 7: Verify scaffolding**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `1 passed`

Run: `python scripts/observer_bench.py`
Expected: `NotImplementedError: Built in Task 14`

- [ ] **Step 8: Commit**

```bash
git add scripts/bench_llm_v3.py scripts/observer_bench.py \
        tests/test_bench_llm_v3.py tests/test_observer_bench.py \
        bench_fixtures/observer_cn/.gitkeep .gitignore
git commit -m "feat(observer-bench): scaffold + v3 catalog additions (gemini-2.5-flash, deepseek-chat)"
```

---

## Task 2: Constants — SYSTEM_PROMPT, TOOL_DEF, CANDIDATES

**Files:**
- Modify: `scripts/observer_bench.py` (fill §1 section)
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_observer_bench.py`:

```python
def test_observer_candidates_8_models():
    """Exactly 8 candidate Observer models per spec §8."""
    assert len(ob.OBSERVER_CANDIDATES) == 8
    expected = {
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gpt-5-mini",
        "grok-4-1-fast-non-reasoning",
        "grok-4.20-0309-non-reasoning",
        "llama-3.3-70b-versatile",
        "claude-haiku-4-5-20251001",
        "deepseek-chat",
    }
    assert set(ob.OBSERVER_CANDIDATES) == expected


def test_system_prompt_has_key_rules():
    """OBSERVER_SYSTEM_PROMPT must include the 8 critical sections per spec §6.1."""
    p = ob.OBSERVER_SYSTEM_PROMPT
    assert "PRIORITY EMOJI" in p
    assert "🔴" in p and "🟡" in p and "🟢" in p and "✅" in p
    assert "DISTINGUISH USER ASSERTIONS FROM QUESTIONS" in p
    assert "STATE CHANGES" in p
    assert "USER ASSERTIONS ARE AUTHORITATIVE" in p
    assert "PRESERVE UNUSUAL PHRASING" in p
    assert "PRECISE VERBS" in p
    assert "DETAILS IN ASSISTANT" in p
    assert "EMOTION" in p
    assert "中文" in p  # bilingual requirement


def test_tool_def_schema_shape():
    """OBSERVER_TOOL_DEF schema matches spec §6.2."""
    td = ob.OBSERVER_TOOL_DEF
    assert td["name"] == "record_observations"
    params = td["parameters"]
    assert params["type"] == "object"
    obs = params["properties"]["observations"]
    assert obs["type"] == "array"
    item = obs["items"]
    assert set(item["required"]) == {"priority", "time", "text"}
    assert set(item["properties"]["priority"]["enum"]) == {"🔴", "🟡", "🟢", "✅"}
    assert item["properties"]["time"]["pattern"] == r"^[0-2][0-9]:[0-5][0-9]$"
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: 3 failures ("OBSERVER_CANDIDATES not defined", etc.)

- [ ] **Step 3: Implement constants**

Replace `# ===== §1 CONSTANTS (filled in later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §1 CONSTANTS =====

OBSERVER_CANDIDATES: tuple[str, ...] = (
    "gemini-2.5-flash",                    # Mastra default
    "gemini-3-pro-preview",
    "gpt-5-mini",
    "grok-4-1-fast-non-reasoning",
    "grok-4.20-0309-non-reasoning",
    "llama-3.3-70b-versatile",
    "claude-haiku-4-5-20251001",
    "deepseek-chat",
)

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
  - ❌ BAD: 用户在 Stripe 工作 (丢失了 "从 Acme 换过来" 的语义)
  - ✅ GOOD: 用户从 Acme 换到 Stripe

## PRESERVE UNUSUAL PHRASING
- 用户说 "累死了" → observation 写 "用户说累死了" 或 "用户疲惫 (原话: 累死了)"
- 不要"洗成"教科书普通话

## PRECISE VERBS — 动词保真
动词必须忠于原意·不弱化·不强化·不推断。
- "我买了 X" → "用户买了 X" ✓(不要写"用户考虑 X"或"用户提到 X")
- "我讨厌 Y" → "用户讨厌 Y" ✓(不要写"用户提到 Y"或"用户不太喜欢 Y")
- "我不在 Acme 了" → "用户不在 Acme" ✓(不要写"用户可能不在 Acme")
- 对 state change / correction 尤其关键: 动词决定信息是否还有效

## DETAILS IN ASSISTANT CONTENT — 保留具体信息
assistant 生成的具体数值·名称·参数·代码片段·必须保留进 observation·
不要压缩为概述。
- assistant "已调为暖黄 2700K" → observation 应记 "2700K 暖黄"·不是只记"暖黄"
- assistant "已设 4 个闹钟·6:30 6:45 7:00 7:15" → observation 应记 4 个时间点
- 原则: 能让未来 assistant 重放执行的细节不能丢

## EMOTION DETECTION
If user message has emotion hint (tired/angry/happy/...) → add 🟡 observation

## AUTHORITY
User assertions are authoritative. If user said X earlier and now asks about X,
the assertion is the ground truth, the question doesn't invalidate it.

## OUTPUT
Call tool `record_observations` ONLY. Do not output free text.
"""

OBSERVER_TOOL_DEF: dict[str, Any] = {
    "name": "record_observations",
    "description": "Record observations extracted from the conversation above.",
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
                            "description": "Priority emoji",
                        },
                        "time": {
                            "type": "string",
                            "pattern": r"^[0-2][0-9]:[0-5][0-9]$",
                            "description": "HH:MM 24h format",
                        },
                        "text": {
                            "type": "string",
                            "minLength": 4,
                            "maxLength": 300,
                            "description": "Observation text in Chinese",
                        },
                    },
                    "required": ["priority", "time", "text"],
                },
                "minItems": 0,
                "maxItems": 10,
            }
        },
        "required": ["observations"],
    },
}

FIXTURE_CATEGORIES: tuple[str, ...] = (
    "preference", "state_change", "temporal", "emotion",
    "smart_home", "correction", "multi_entity", "completion",
)

MAX_OUTPUT_TOKENS = 1024       # observation output can be longer than v3's 512
CALL_TIMEOUT_SEC = 60.0

# Pilot early-exit thresholds (per spec §9.4)
PILOT_TOOL_SUCCESS_THRESHOLD = 0.80
PILOT_F1_THRESHOLD = 0.30

# Generator model
FIXTURE_GENERATOR_MODEL = "claude-opus-4-6"
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): constants — SYSTEM_PROMPT + TOOL_DEF + 8 CANDIDATES"
```

---

## Task 3: Dataclasses

**Files:**
- Modify: `scripts/observer_bench.py`
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_observer_bench.py`:

```python
def test_seed_dataclass_fields():
    seed = ob.Seed(
        id="fx_001",
        category="smart_home",
        scene="test",
        user_emotion_hint="tired",
        tone_hint="casual",
        dialogue_length_hint="3-4 turns",
        must_capture=["a", "b"],
        must_not_hallucinate=["x"],
    )
    assert seed.id == "fx_001"
    assert seed.category == "smart_home"


def test_expected_observation_dataclass():
    exp = ob.ExpectedObservation(
        priority="🔴",
        must_contain_any_of=[["拿铁", "客厅"], ["偏好"]],
        semantic_description="用户偏好客厅灯暖黄",
    )
    assert exp.priority == "🔴"
    assert len(exp.must_contain_any_of) == 2


def test_fixture_dataclass():
    fx = ob.Fixture(
        id="fx_001",
        category="smart_home",
        seed_id="fx_001",
        generated_by="claude-opus-4-6",
        dialogue=[{"role": "user", "time": "14:28", "content": "hi"}],
        expected_observations=[
            ob.ExpectedObservation("🔴", [["hi"]], "greeting")
        ],
        must_not_contain_globally=["bad"],
    )
    assert fx.id == "fx_001"
    assert len(fx.dialogue) == 1
    assert len(fx.expected_observations) == 1


def test_scores_dataclass_defaults():
    s = ob.Scores(
        tool_success=False,
        precision=0.0, recall=0.0, f1=0.0,
        priority_accuracy=0.0, hallucination=False, extra_count=0,
    )
    assert s.tool_success is False
    assert s.f1 == 0.0
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q -k dataclass`
Expected: 4 failures (AttributeError)

- [ ] **Step 3: Implement dataclasses**

Replace `# ===== §2 DATACLASSES (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §2 DATACLASSES =====

@dataclass
class Seed:
    """Entry from seeds.yaml — Allen writes these."""
    id: str
    category: str                           # must be in FIXTURE_CATEGORIES
    scene: str
    user_emotion_hint: str
    tone_hint: str
    dialogue_length_hint: str
    must_capture: list[str]
    must_not_hallucinate: list[str]


@dataclass
class ExpectedObservation:
    """One expected observation in a fixture.ground_truth."""
    priority: str                           # 🔴/🟡/🟢/✅
    must_contain_any_of: list[list[str]]    # OR of (AND of keywords)
    semantic_description: str               # For human review only, not used by code


@dataclass
class Fixture:
    """Approved fx_XXX.json — dialogue + ground truth."""
    id: str
    category: str                           # mirrored from Seed.category
    seed_id: str
    generated_by: str                       # model ID that drafted this
    dialogue: list[dict[str, Any]]          # [{role, time, content, ...}]
    expected_observations: list[ExpectedObservation]
    must_not_contain_globally: list[str]
    generated_at: str = ""
    approved_by: str = ""
    approved_at: str = ""


@dataclass
class ObserverCall:
    """Raw result of one Observer API call."""
    observer_latency_ms: float
    total_ms: float
    model_obs: list[dict[str, Any]] | None  # None = tool_call failed
    raw_arguments: str                      # tool_call.function.arguments text (truncated)
    raw_response: Any                       # for extract_cache_metrics
    error: str = ""


@dataclass
class Scores:
    """Per-(model, fixture) evaluation result."""
    tool_success: bool
    precision: float
    recall: float
    f1: float
    priority_accuracy: float
    hallucination: bool
    extra_count: int


@dataclass
class ObserverResult:
    """CSV row — one per (model, fixture)."""
    timestamp: str
    model: str
    model_is_fallback: bool
    provider: str
    fixture_id: str
    fixture_category: str
    tool_success: bool
    precision: float
    recall: float
    f1: float
    priority_accuracy: float
    hallucination: bool
    extra_count: int
    expected_count: int
    matched_count: int
    observer_latency_ms: float
    actual_input_tokens_api: int
    output_tokens: int
    cost_usd: float
    model_output_raw: str                   # tool_call arguments, truncated 1000 chars
    error: str = ""
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): dataclasses (Seed, Fixture, Scores, ObserverResult)"
```

---

## Task 4: Fixture I/O (load seeds, load approved, save draft)

**Files:**
- Modify: `scripts/observer_bench.py`
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_observer_bench.py`:

```python
import tempfile
import json as _json
from pathlib import Path as _Path


def test_load_seeds_parses_yaml(tmp_path):
    seeds_yaml = tmp_path / "seeds.yaml"
    seeds_yaml.write_text("""
- id: fx_001
  category: smart_home
  scene: "智能家居 + 疲惫语气"
  user_emotion_hint: tired
  tone_hint: "口语化"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "偏好: 暖黄"
  must_not_hallucinate:
    - "蓝光"
""", encoding="utf-8")
    seeds = ob.load_seeds(seeds_yaml)
    assert len(seeds) == 1
    assert seeds[0].id == "fx_001"
    assert seeds[0].category == "smart_home"
    assert seeds[0].must_capture == ["偏好: 暖黄"]


def test_load_seeds_rejects_unknown_category(tmp_path):
    seeds_yaml = tmp_path / "seeds.yaml"
    seeds_yaml.write_text("""
- id: fx_001
  category: BOGUS
  scene: x
  user_emotion_hint: x
  tone_hint: x
  dialogue_length_hint: x
  must_capture: []
  must_not_hallucinate: []
""", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="unknown category"):
        ob.load_seeds(seeds_yaml)


def test_load_approved_fixtures_ignores_draft(tmp_path):
    """.draft.json files must be skipped (Allen hasn't approved them)."""
    approved = tmp_path / "fx_001.json"
    draft = tmp_path / "fx_002.draft.json"
    approved.write_text(_json.dumps({
        "id": "fx_001", "category": "smart_home", "seed_id": "fx_001",
        "generated_by": "claude-opus-4-6",
        "dialogue": [{"role": "user", "time": "14:28", "content": "hi"}],
        "expected_observations": [
            {"priority": "🔴", "must_contain_any_of": [["hi"]], "semantic_description": "x"}
        ],
        "must_not_contain_globally": [],
    }), encoding="utf-8")
    draft.write_text('{"id": "fx_002"}', encoding="utf-8")
    fxs = ob.load_approved_fixtures(tmp_path)
    assert len(fxs) == 1
    assert fxs[0].id == "fx_001"


def test_save_draft_fixture_writes_draft_suffix(tmp_path):
    fx = ob.Fixture(
        id="fx_003", category="preference", seed_id="fx_003",
        generated_by="claude-opus-4-6",
        dialogue=[], expected_observations=[], must_not_contain_globally=[],
    )
    path = ob.save_draft_fixture(fx, tmp_path)
    assert path.name == "fx_003.draft.json"
    assert path.exists()
    data = _json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == "fx_003"
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q -k "seed or fixture"`
Expected: 4 failures

- [ ] **Step 3: Implement Fixture I/O**

Replace `# ===== §3 FIXTURE I/O (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §3 FIXTURE I/O =====

import yaml


def load_seeds(path: Path) -> list[Seed]:
    """Load seeds.yaml → list[Seed]. Validates category enum."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"seeds.yaml must be a list, got {type(raw)}")
    seeds = []
    for entry in raw:
        if entry.get("category") not in FIXTURE_CATEGORIES:
            raise ValueError(
                f"seeds.yaml id={entry.get('id')}: unknown category "
                f"{entry.get('category')!r} (allowed: {FIXTURE_CATEGORIES})"
            )
        seeds.append(Seed(
            id=entry["id"],
            category=entry["category"],
            scene=entry.get("scene", ""),
            user_emotion_hint=entry.get("user_emotion_hint", "neutral"),
            tone_hint=entry.get("tone_hint", ""),
            dialogue_length_hint=entry.get("dialogue_length_hint", "3-4 turns"),
            must_capture=list(entry.get("must_capture", [])),
            must_not_hallucinate=list(entry.get("must_not_hallucinate", [])),
        ))
    return seeds


def _fixture_from_dict(d: dict[str, Any]) -> Fixture:
    """Parse fx_XXX.json dict → Fixture."""
    exps = [
        ExpectedObservation(
            priority=e["priority"],
            must_contain_any_of=[list(x) for x in e["must_contain_any_of"]],
            semantic_description=e.get("semantic_description", ""),
        )
        for e in d["expected_observations"]
    ]
    return Fixture(
        id=d["id"],
        category=d["category"],
        seed_id=d["seed_id"],
        generated_by=d["generated_by"],
        dialogue=list(d["dialogue"]),
        expected_observations=exps,
        must_not_contain_globally=list(d.get("must_not_contain_globally", [])),
        generated_at=d.get("generated_at", ""),
        approved_by=d.get("approved_by", ""),
        approved_at=d.get("approved_at", ""),
    )


def load_approved_fixtures(dir_path: Path) -> list[Fixture]:
    """Load fx_*.json (NOT .draft.json) from observer_cn/."""
    fxs = []
    for p in sorted(dir_path.glob("fx_*.json")):
        if p.name.endswith(".draft.json"):
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        fxs.append(_fixture_from_dict(data))
    return fxs


def _fixture_to_dict(fx: Fixture) -> dict[str, Any]:
    """Serialize Fixture → dict for JSON output."""
    return {
        "id": fx.id,
        "category": fx.category,
        "seed_id": fx.seed_id,
        "generated_by": fx.generated_by,
        "generated_at": fx.generated_at,
        "approved_by": fx.approved_by,
        "approved_at": fx.approved_at,
        "dialogue": fx.dialogue,
        "expected_observations": [
            {
                "priority": e.priority,
                "must_contain_any_of": e.must_contain_any_of,
                "semantic_description": e.semantic_description,
            }
            for e in fx.expected_observations
        ],
        "must_not_contain_globally": fx.must_not_contain_globally,
    }


def save_draft_fixture(fx: Fixture, dir_path: Path) -> Path:
    """Write fixture as fx_XXX.draft.json (Allen renames to approve)."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{fx.id}.draft.json"
    path.write_text(
        json.dumps(_fixture_to_dict(fx), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `12 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): fixture I/O (load_seeds, load_approved, save_draft)"
```

---

## Task 5: Prompt + tool kwargs builders

**Files:**
- Modify: `scripts/observer_bench.py`
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_observer_bench.py`:

```python
def test_build_observer_prompt_renders_all_roles():
    fx = ob.Fixture(
        id="fx_001", category="smart_home", seed_id="fx_001",
        generated_by="claude-opus-4-6",
        dialogue=[
            {"role": "user", "time": "14:28", "emotion": "tired", "content": "调灯"},
            {"role": "assistant", "time": "14:28", "content": "好的"},
            {"role": "tool", "name": "hue.set_color",
             "args": {"room": "living"}, "result": "ok"},
        ],
        expected_observations=[], must_not_contain_globally=[],
    )
    system, user_msg = ob.build_observer_prompt(fx)
    assert "record_observations" in system   # part of OBSERVER_SYSTEM_PROMPT
    assert "USER (14:28)" in user_msg
    assert "[情绪: tired]" in user_msg
    assert "ASSISTANT (14:28): 好的" in user_msg
    assert "TOOL_CALL hue.set_color" in user_msg
    assert "room" in user_msg and "living" in user_msg


def test_build_tool_call_kwargs_anthropic():
    kw = ob.build_tool_call_kwargs("anthropic")
    assert kw["tool_choice"] == {"type": "tool", "name": "record_observations"}
    assert kw["tools"][0]["name"] == "record_observations"
    assert "input_schema" in kw["tools"][0]


def test_build_tool_call_kwargs_openai_compat():
    for provider in ("openai", "xai", "groq", "deepseek"):
        kw = ob.build_tool_call_kwargs(provider)
        assert kw["tool_choice"]["type"] == "function"
        assert kw["tool_choice"]["function"]["name"] == "record_observations"
        assert kw["tools"][0]["type"] == "function"


def test_build_tool_call_kwargs_gemini():
    kw = ob.build_tool_call_kwargs("google")
    assert "function_declarations" in kw["tools"][0]
    assert kw["tool_config"]["function_calling_config"]["mode"] == "ANY"
    assert kw["tool_config"]["function_calling_config"]["allowed_function_names"] == [
        "record_observations"
    ]
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q -k "prompt or tool_call"`
Expected: 4 failures

- [ ] **Step 3: Implement builders**

Replace `# ===== §4 PROMPT + TOOL BUILDERS (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §4 PROMPT + TOOL BUILDERS =====

def build_observer_prompt(fixture: Fixture) -> tuple[str, str]:
    """Returns (system_prompt, user_message) for Observer call."""
    system = OBSERVER_SYSTEM_PROMPT

    lines = ["以下是一段对话，抽取 observation 并调用 record_observations：\n"]
    for turn in fixture.dialogue:
        role = turn.get("role")
        if role == "user":
            emo = turn.get("emotion", "")
            emo_suffix = f" [情绪: {emo}]" if emo else ""
            lines.append(f"USER ({turn.get('time', '??:??')}){emo_suffix}: {turn.get('content', '')}")
        elif role == "assistant":
            lines.append(f"ASSISTANT ({turn.get('time', '??:??')}): {turn.get('content', '')}")
        elif role == "tool":
            args_str = json.dumps(turn.get("args", {}), ensure_ascii=False)
            name = turn.get("name", "?")
            result = turn.get("result", "")
            lines.append(f"TOOL_CALL {name}({args_str}) → {result}")
        else:
            lines.append(f"[unknown role={role}] {turn.get('content', '')}")

    lines.append("\n请调用 record_observations 工具。")
    return system, "\n".join(lines)


def build_tool_call_kwargs(provider: str) -> dict[str, Any]:
    """Return provider-specific tool + tool_choice kwargs (spec §6.4)."""
    if provider == "anthropic":
        return {
            "tools": [{
                "name": OBSERVER_TOOL_DEF["name"],
                "description": OBSERVER_TOOL_DEF["description"],
                "input_schema": OBSERVER_TOOL_DEF["parameters"],
            }],
            "tool_choice": {"type": "tool", "name": "record_observations"},
        }
    if provider == "google":
        return {
            "tools": [{"function_declarations": [{
                "name": OBSERVER_TOOL_DEF["name"],
                "description": OBSERVER_TOOL_DEF["description"],
                "parameters": OBSERVER_TOOL_DEF["parameters"],
            }]}],
            "tool_config": {"function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": ["record_observations"],
            }},
        }
    # openai / xai / groq / deepseek (all OpenAI-compat)
    return {
        "tools": [{"type": "function", "function": OBSERVER_TOOL_DEF}],
        "tool_choice": {"type": "function", "function": {"name": "record_observations"}},
    }
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `16 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): prompt + tool kwargs builders (per-provider mapping)"
```

---

## Task 6: Anthropic provider caller (`call_with_tools_anthropic`)

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement Anthropic caller**

Insert after `# ===== §5 PROVIDER CALLERS (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §5 PROVIDER CALLERS =====

def _parse_anthropic_tool_call(final_message: Any) -> tuple[list[dict] | None, str]:
    """Extract record_observations arguments from Anthropic response."""
    for block in getattr(final_message, "content", []):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "record_observations":
            args = getattr(block, "input", None)
            if isinstance(args, dict):
                obs = args.get("observations", [])
                return obs if isinstance(obs, list) else None, json.dumps(args, ensure_ascii=False)[:1000]
    return None, ""


async def call_with_tools_anthropic(system: str, user_msg: str, model_id: str) -> ObserverCall:
    """Anthropic messages API with forced tool_choice record_observations."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    tool_kwargs = build_tool_call_kwargs("anthropic")

    # Bust prefix reused from v3 (v3 still unchanged; we just call it)
    bust = v3.make_bust_prefix()
    sys_with_bust = bust + system

    t0 = time.perf_counter()
    final_message = await client.messages.create(
        model=model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=sys_with_bust,
        messages=[{"role": "user", "content": user_msg}],
        **tool_kwargs,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    obs, raw_args = _parse_anthropic_tool_call(final_message)
    return ObserverCall(
        observer_latency_ms=elapsed_ms,
        total_ms=elapsed_ms,
        model_obs=obs,
        raw_arguments=raw_args,
        raw_response=final_message,
    )
```

- [ ] **Step 2: Verify module still imports**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import observer_bench; print('OK')"`
Expected: `OK`

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `16 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): Anthropic tool-call caller"
```

---

## Task 7: OpenAI-compat caller (serves openai/xai/groq/deepseek)

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement OpenAI-compat caller**

Append to §5 of `scripts/observer_bench.py`:

```python
def _parse_openai_tool_call(final_chunk: Any) -> tuple[list[dict] | None, str]:
    """Parse first tool_call from OpenAI-compat streaming/non-streaming response."""
    if final_chunk is None:
        return None, ""
    choices = getattr(final_chunk, "choices", None)
    if not choices:
        return None, ""
    msg = getattr(choices[0], "message", None) or getattr(choices[0], "delta", None)
    if msg is None:
        return None, ""
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return None, ""
    tc = tool_calls[0]
    fn = getattr(tc, "function", None)
    if fn is None or getattr(fn, "name", "") != "record_observations":
        return None, ""
    args_str = getattr(fn, "arguments", "") or ""
    try:
        parsed = json.loads(args_str)
    except json.JSONDecodeError:
        return None, args_str[:1000]
    obs = parsed.get("observations") if isinstance(parsed, dict) else None
    return obs if isinstance(obs, list) else None, args_str[:1000]


def _openai_token_param_for_model(provider: str, model_id: str) -> str:
    """GPT-5 / o1 / o3 require max_completion_tokens; others use max_tokens."""
    if provider == "openai" and (
        model_id.startswith("gpt-5") or model_id.startswith("o1") or model_id.startswith("o3")
    ):
        return "max_completion_tokens"
    return "max_tokens"


async def call_with_tools_openai_compat(
    system: str, user_msg: str, model_id: str, provider: str, base_url: str, api_key: str,
) -> ObserverCall:
    """Shared caller for OpenAI, xAI, Groq, DeepSeek (all OpenAI wire protocol)."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    tool_kwargs = build_tool_call_kwargs(provider)
    token_param = _openai_token_param_for_model(provider, model_id)

    bust = v3.make_bust_prefix()
    messages = [
        {"role": "system", "content": bust + system},
        {"role": "user", "content": user_msg},
    ]

    t0 = time.perf_counter()
    resp = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        **{token_param: MAX_OUTPUT_TOKENS},
        stream=False,  # non-stream: tool_calls fully assembled in response
        **tool_kwargs,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    obs, raw_args = _parse_openai_tool_call(resp)
    return ObserverCall(
        observer_latency_ms=elapsed_ms,
        total_ms=elapsed_ms,
        model_obs=obs,
        raw_arguments=raw_args,
        raw_response=resp,
    )
```

- [ ] **Step 2: Verify module imports**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import observer_bench as ob; print(ob._openai_token_param_for_model('openai', 'gpt-5-mini'))"`
Expected: `max_completion_tokens`

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `16 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): OpenAI-compat caller (openai/xai/groq/deepseek)"
```

---

## Task 8: Gemini provider caller

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement Gemini caller**

Append to §5 of `scripts/observer_bench.py`:

```python
def _parse_gemini_tool_call(response: Any) -> tuple[list[dict] | None, str]:
    """Extract record_observations from Gemini response candidates[0].content.parts."""
    try:
        cand = response.candidates[0]
        for part in cand.content.parts:
            fn = getattr(part, "function_call", None)
            if fn is None or getattr(fn, "name", "") != "record_observations":
                continue
            # fn.args is a proto.MapComposite — convert to dict
            args = dict(fn.args) if hasattr(fn, "args") else {}
            obs_proto = args.get("observations")
            if obs_proto is None:
                return None, json.dumps(args, ensure_ascii=False, default=str)[:1000]
            # Each observation is a proto.Struct — convert recursively
            obs_list = []
            for item in obs_proto:
                if hasattr(item, "items"):
                    obs_list.append(dict(item))
                else:
                    obs_list.append(item)
            return obs_list, json.dumps(args, ensure_ascii=False, default=str)[:1000]
    except (AttributeError, IndexError, TypeError):
        pass
    return None, ""


async def call_with_tools_gemini(system: str, user_msg: str, model_id: str) -> ObserverCall:
    """Google Gemini via google-generativeai SDK (sync, wrapped in to_thread)."""
    import google.generativeai as genai

    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    tool_kwargs = build_tool_call_kwargs("google")
    # Gemini accepts system_instruction separately
    model = genai.GenerativeModel(
        model_id,
        system_instruction=system,
        tools=tool_kwargs["tools"],
        tool_config=tool_kwargs["tool_config"],
    )
    bust = v3.make_bust_prefix()
    combined_user = bust + user_msg

    def _run() -> tuple[Any, float]:
        t0 = time.perf_counter()
        resp = model.generate_content(
            combined_user,
            generation_config={"max_output_tokens": MAX_OUTPUT_TOKENS},
        )
        return resp, (time.perf_counter() - t0) * 1000.0

    resp, elapsed_ms = await asyncio.to_thread(_run)
    obs, raw_args = _parse_gemini_tool_call(resp)
    return ObserverCall(
        observer_latency_ms=elapsed_ms,
        total_ms=elapsed_ms,
        model_obs=obs,
        raw_arguments=raw_args,
        raw_response=resp,
    )
```

- [ ] **Step 2: Verify module imports**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import observer_bench; print('OK')"`
Expected: `OK`

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `16 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): Gemini tool-call caller (function_call proto parsing)"
```

---

## Task 9: Retry wrapper + dispatch + result assembly

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement retry wrapper + dispatch**

Replace `# ===== §6 RETRY + ASSEMBLY (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §6 RETRY + ASSEMBLY =====

API_KEY_ENV_OBS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "google":    "GEMINI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "xai":       "XAI_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}

OPENAI_COMPAT_BASE_URLS = {
    "openai":   "https://api.openai.com/v1",
    "xai":      "https://api.x.ai/v1",
    "groq":     "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
}


def _is_rate_limit_obs(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg or "quota" in msg or "overloaded" in msg


def _is_fatal_obs(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "401" in msg or "403" in msg or "invalid api key" in msg or "400" in msg


async def _dispatch_by_provider(provider: str, model_id: str, system: str, user_msg: str) -> ObserverCall:
    """Route to the right caller by provider."""
    if provider == "anthropic":
        return await call_with_tools_anthropic(system, user_msg, model_id)
    if provider == "google":
        return await call_with_tools_gemini(system, user_msg, model_id)
    if provider in OPENAI_COMPAT_BASE_URLS:
        base_url = OPENAI_COMPAT_BASE_URLS[provider]
        api_key = os.environ[API_KEY_ENV_OBS[provider]]
        return await call_with_tools_openai_compat(
            system, user_msg, model_id, provider, base_url, api_key,
        )
    raise ValueError(f"Unknown provider: {provider}")


async def call_observer_with_retry(
    spec: v3.ModelSpec,
    active_model_id: str,
    model_is_fallback: bool,
    fixture: Fixture,
) -> ObserverResult:
    """Run Observer once with exponential backoff on rate limits; build ObserverResult."""
    system, user_msg = build_observer_prompt(fixture)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    last_err = ""
    for attempt in range(4):
        try:
            call = await asyncio.wait_for(
                _dispatch_by_provider(spec.provider, active_model_id, system, user_msg),
                timeout=CALL_TIMEOUT_SEC,
            )
            # Evaluate + assemble
            scores = evaluate(call.model_obs, fixture)
            metrics = v3.extract_cache_metrics(spec.provider, call.raw_response)
            cost = v3.calc_cost(
                cache_write_tokens=metrics["cache_write_tokens"],
                cache_read_tokens=metrics["cache_read_tokens"],
                prompt_total_tokens=metrics["prompt_total_tokens"],
                output_tokens=metrics["output_tokens"],
                spec=spec,
            )
            matched = int(scores.recall * len(fixture.expected_observations)) if fixture.expected_observations else 0
            return ObserverResult(
                timestamp=timestamp,
                model=active_model_id,
                model_is_fallback=model_is_fallback,
                provider=spec.provider,
                fixture_id=fixture.id,
                fixture_category=fixture.category,
                tool_success=scores.tool_success,
                precision=scores.precision,
                recall=scores.recall,
                f1=scores.f1,
                priority_accuracy=scores.priority_accuracy,
                hallucination=scores.hallucination,
                extra_count=scores.extra_count,
                expected_count=len(fixture.expected_observations),
                matched_count=matched,
                observer_latency_ms=call.observer_latency_ms,
                actual_input_tokens_api=metrics["prompt_total_tokens"],
                output_tokens=metrics["output_tokens"],
                cost_usd=cost,
                model_output_raw=call.raw_arguments,
                error="",
            )
        except asyncio.TimeoutError:
            last_err = "timeout"
            break
        except Exception as e:  # noqa: BLE001
            if _is_fatal_obs(e):
                last_err = f"fatal: {type(e).__name__}: {str(e)[:200]}"
                break
            if _is_rate_limit_obs(e):
                wait = 2 ** attempt
                LOGGER.warning("Rate limit on %s (%s), wait %ds, attempt %d",
                               active_model_id, fixture.id, wait, attempt + 1)
                await asyncio.sleep(wait)
                last_err = f"ratelimit: {str(e)[:200]}"
                continue
            await asyncio.sleep(2 ** attempt)
            last_err = f"{type(e).__name__}: {str(e)[:200]}"

    # Error path
    return ObserverResult(
        timestamp=timestamp,
        model=active_model_id,
        model_is_fallback=model_is_fallback,
        provider=spec.provider,
        fixture_id=fixture.id,
        fixture_category=fixture.category,
        tool_success=False,
        precision=0.0, recall=0.0, f1=0.0, priority_accuracy=0.0,
        hallucination=False, extra_count=0,
        expected_count=len(fixture.expected_observations),
        matched_count=0,
        observer_latency_ms=-1.0,
        actual_input_tokens_api=0,
        output_tokens=0,
        cost_usd=0.0,
        model_output_raw="",
        error=last_err or "unknown",
    )
```

- [ ] **Step 2: Verify module imports**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import observer_bench; print(observer_bench.API_KEY_ENV_OBS['deepseek'])"`
Expected: `DEEPSEEK_API_KEY`

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `16 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): retry wrapper + provider dispatch + ObserverResult assembly"
```

---

## Task 10: Evaluator

**Files:**
- Modify: `scripts/observer_bench.py`
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_observer_bench.py`:

```python
def _make_fx_one_expected():
    return ob.Fixture(
        id="fx_test", category="smart_home", seed_id="fx_test",
        generated_by="test",
        dialogue=[],
        expected_observations=[
            ob.ExpectedObservation(
                priority="🔴",
                must_contain_any_of=[["拿铁", "客厅"], ["暖黄"]],  # OR of AND
                semantic_description="x",
            ),
            ob.ExpectedObservation(
                priority="🟡",
                must_contain_any_of=[["累"], ["疲惫"]],
                semantic_description="y",
            ),
        ],
        must_not_contain_globally=["蓝光", "卧室"],
    )


def test_evaluate_tool_success_false_when_none():
    """model_obs=None → all scores 0, halluc False."""
    fx = _make_fx_one_expected()
    s = ob.evaluate(None, fx)
    assert s.tool_success is False
    assert s.precision == 0.0
    assert s.recall == 0.0
    assert s.f1 == 0.0
    assert s.priority_accuracy == 0.0
    assert s.hallucination is False
    assert s.extra_count == 0


def test_evaluate_perfect_match():
    """Both expected observations matched by correct keywords + priority."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户偏好客厅灯暖黄色"},
        {"priority": "🟡", "time": "14:28", "text": "用户表达疲惫"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.tool_success is True
    assert s.precision == 1.0
    assert s.recall == 1.0
    assert s.f1 == 1.0
    assert s.priority_accuracy == 1.0
    assert s.hallucination is False
    assert s.extra_count == 0


def test_evaluate_keyword_or_semantics():
    """must_contain_any_of OR: matching any one sub-list is enough."""
    fx = _make_fx_one_expected()
    # First expected: matches "暖黄" only (second sub-list)
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "暖黄色灯光设置"},
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0


def test_evaluate_hallucination_triggered():
    """must_not_contain_globally word in any obs → halluc=True."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户在卧室想要暖黄"},  # "卧室" triggers
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.hallucination is True


def test_evaluate_partial_recall_no_halluc():
    """Only 1 of 2 matched, no halluc words."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户偏好客厅拿铁"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 0.5
    assert s.precision == 1.0
    assert abs(s.f1 - 2/3) < 0.01
    assert s.hallucination is False


def test_evaluate_priority_wrong_still_counts_recall():
    """Model hit keywords but wrong priority → recall OK, priority_acc reduced."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🟡", "time": "14:28", "text": "客厅暖黄"},  # should be 🔴
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0
    assert s.priority_accuracy == 0.5  # 1 of 2 priorities matched


def test_evaluate_extra_observations():
    """Extra observations boost total but do not block recall."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "客厅暖黄"},
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
        {"priority": "🟢", "time": "14:28", "text": "用户住在温哥华"},  # extra
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0
    assert abs(s.precision - 2/3) < 0.01
    assert s.extra_count == 1


def test_evaluate_tool_success_invalid_priority():
    """Invalid priority emoji → tool_success=False."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "❓", "time": "14:28", "text": "bogus priority"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.tool_success is False
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q -k evaluate`
Expected: 8 failures

- [ ] **Step 3: Implement evaluator**

Replace `# ===== §8 EVALUATOR (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §8 EVALUATOR =====

_TIME_RE = re.compile(r"^[0-2]\d:[0-5]\d$")
_VALID_PRIORITIES = {"🔴", "🟡", "🟢", "✅"}


def evaluate(model_obs: list[dict] | None, fixture: Fixture) -> Scores:
    """Pure rule-based evaluation per spec §7.

    Matching rule: for each expected_observation, greedily find first model_obs
    whose text satisfies any one sub-list of must_contain_any_of (AND within list).
    """
    # Guard: tool_call failed → all scores 0
    if model_obs is None:
        return Scores(
            tool_success=False,
            precision=0.0, recall=0.0, f1=0.0,
            priority_accuracy=0.0, hallucination=False, extra_count=0,
        )

    # Tool call field validity
    tool_success = (
        isinstance(model_obs, list)
        and all(
            isinstance(o, dict)
            and o.get("priority") in _VALID_PRIORITIES
            and isinstance(o.get("time"), str) and _TIME_RE.match(o["time"])
            and isinstance(o.get("text"), str) and len(o["text"]) >= 4
            for o in model_obs
        )
    )

    # Greedy matching (expected → model_obs)
    matched_model: set[int] = set()
    matched_expected: set[int] = set()
    priority_correct = 0
    for ei, exp in enumerate(fixture.expected_observations):
        for mi, obs in enumerate(model_obs):
            if mi in matched_model:
                continue
            text = obs.get("text", "") if isinstance(obs, dict) else ""
            # must_contain_any_of: OR of AND
            if any(
                all(kw in text for kw in keyword_list)
                for keyword_list in exp.must_contain_any_of
            ):
                matched_expected.add(ei)
                matched_model.add(mi)
                if obs.get("priority") == exp.priority:
                    priority_correct += 1
                break  # one expected → at most one model_obs

    recall = len(matched_expected) / len(fixture.expected_observations) if fixture.expected_observations else 0.0
    precision = len(matched_model) / len(model_obs) if model_obs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    priority_acc = priority_correct / len(matched_expected) if matched_expected else 0.0

    halluc = any(
        any(bad in obs.get("text", "") for bad in fixture.must_not_contain_globally)
        for obs in model_obs
        if isinstance(obs, dict)
    )

    extra = max(0, len(model_obs) - len(fixture.expected_observations))

    return Scores(
        tool_success=tool_success,
        precision=precision,
        recall=recall,
        f1=f1,
        priority_accuracy=priority_acc,
        hallucination=halluc,
        extra_count=extra,
    )
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `24 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): rule-based evaluator (tool_success guard + OR-of-AND matching)"
```

---

## Task 11: Warmup + active spec resolution

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement warmup**

Replace `# ===== §7 WARMUP (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §7 WARMUP =====

@dataclass
class ActiveObserver:
    spec: v3.ModelSpec
    active_model_id: str
    is_fallback: bool


def _provider_has_key_obs(provider: str) -> bool:
    primary = os.environ.get(API_KEY_ENV_OBS[provider])
    fallback = os.environ.get("GOOGLE_API_KEY") if provider == "google" else None
    return bool(primary or fallback)


async def warmup_observer_one(spec: v3.ModelSpec) -> tuple[str, bool] | None:
    """Return (active_id, is_fallback) if any candidate works, else None.

    Isolated payload: pure user message "Just say: OK", no system prompt,
    no tools. Zero byte overlap with Observer test prefixes.
    """
    candidates: list[tuple[str, bool]] = [(spec.primary_id, False)]
    candidates.extend((fid, True) for fid in spec.fallback_ids)

    for mid, is_fb in candidates:
        try:
            if spec.provider == "anthropic":
                from anthropic import AsyncAnthropic
                client = AsyncAnthropic()
                await client.messages.create(
                    model=mid, max_tokens=5,
                    messages=[{"role": "user", "content": "Just say: OK"}],
                )
            elif spec.provider == "google":
                if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
                    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
                import google.generativeai as genai
                genai.configure(api_key=os.environ["GEMINI_API_KEY"])
                await asyncio.to_thread(
                    lambda: genai.GenerativeModel(mid).generate_content(
                        "Just say: OK",
                        generation_config={"max_output_tokens": 5},
                    )
                )
            else:  # openai / xai / groq / deepseek
                from openai import AsyncOpenAI
                client = AsyncOpenAI(
                    base_url=OPENAI_COMPAT_BASE_URLS[spec.provider],
                    api_key=os.environ[API_KEY_ENV_OBS[spec.provider]],
                )
                token_param = _openai_token_param_for_model(spec.provider, mid)
                await client.chat.completions.create(
                    model=mid,
                    messages=[{"role": "user", "content": "Just say: OK"}],
                    **{token_param: 5},
                )
            return (mid, is_fb)
        except Exception as e:  # noqa: BLE001
            LOGGER.warning("warmup failed for %s/%s: %s", spec.provider, mid, str(e)[:120])
            continue
    return None


async def resolve_active_observers(candidate_ids: tuple[str, ...]) -> list[ActiveObserver]:
    """Filter MODEL_CATALOG down to candidate_ids with working keys + warmup passes."""
    active: list[ActiveObserver] = []
    specs_to_try = [s for s in v3.MODEL_CATALOG
                    if s.primary_id in candidate_ids and _provider_has_key_obs(s.provider)]
    skipped_no_key = [s for s in v3.MODEL_CATALOG
                      if s.primary_id in candidate_ids and not _provider_has_key_obs(s.provider)]

    for s in skipped_no_key:
        print(f"  ✗ {s.provider}/{s.primary_id} — missing {API_KEY_ENV_OBS[s.provider]}")

    results = await asyncio.gather(*(warmup_observer_one(s) for s in specs_to_try))
    for spec, result in zip(specs_to_try, results):
        if result is None:
            print(f"  ✗ {spec.provider}/{spec.primary_id} — warmup exhausted fallbacks")
            continue
        mid, is_fb = result
        marker = "↪" if is_fb else "✓"
        suffix = " (fallback)" if is_fb else ""
        print(f"  {marker} {spec.provider}/{mid}{suffix}")
        active.append(ActiveObserver(spec, mid, is_fb))
    return active
```

- [ ] **Step 2: Verify module imports**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `24 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): warmup + resolve_active_observers (isolated payload)"
```

---

## Task 12: Fixture generator (Opus → .draft.json)

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement fixture generator**

Replace `# ===== §9 FIXTURE GENERATOR (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §9 FIXTURE GENERATOR =====

FIXTURE_GEN_SYSTEM_PROMPT = """You are a fixture writer for a Chinese Observer benchmark.
Your job: given a seed spec, produce a realistic Chinese dialogue + ground-truth
`expected_observations` for the Observer model to extract.

## OUTPUT
Return a JSON object with this exact shape:
{
  "dialogue": [
    {"role": "user", "time": "HH:MM", "emotion": "tired|happy|angry|neutral|...", "content": "..."},
    {"role": "assistant", "time": "HH:MM", "content": "..."},
    {"role": "tool", "name": "...", "args": {...}, "result": "..."}     // optional, only if seed scene needs it
  ],
  "expected_observations": [
    {
      "priority": "🔴|🟡|🟢|✅",
      "must_contain_any_of": [["keyword1", "keyword2"], ["synonym"]],
      "semantic_description": "一句中文描述"
    }
  ],
  "must_not_contain_globally": ["hallucination1", "hallucination2"]
}

## RULES
- Dialogue must feel NATURAL CHINESE, not textbook. Follow seed's tone_hint precisely.
- Times HH:MM format, 24-hour. Stay consistent within the dialogue (usually same minute).
- `must_contain_any_of`: provide 2-3 sub-lists per expected observation for robust matching.
- `semantic_description` in Chinese, for human review only (not used in evaluation).
- `must_not_contain_globally`: 2-5 words that SHOULD NOT appear in any observation
  (hallucinations the Observer might produce).
- Use seed.must_capture as a strict checklist — produce one expected_observation per item.

## OUTPUT FORMAT
JSON only. No markdown fences. No commentary. No prose.
"""


def _seed_to_user_prompt(seed: Seed) -> str:
    return json.dumps({
        "id": seed.id,
        "category": seed.category,
        "scene": seed.scene,
        "user_emotion_hint": seed.user_emotion_hint,
        "tone_hint": seed.tone_hint,
        "dialogue_length_hint": seed.dialogue_length_hint,
        "must_capture": seed.must_capture,
        "must_not_hallucinate": seed.must_not_hallucinate,
    }, ensure_ascii=False, indent=2)


async def generate_fixture_draft(seed: Seed, generator_model: str = FIXTURE_GENERATOR_MODEL) -> Fixture:
    """Call Opus with the seed, return Fixture parsed from Opus JSON."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    msg = await client.messages.create(
        model=generator_model,
        max_tokens=4096,
        system=FIXTURE_GEN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _seed_to_user_prompt(seed)}],
    )

    # Extract text from content blocks
    text = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")

    # Strip markdown fences if Opus added them despite instructions
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Opus returned invalid JSON for seed {seed.id}: {e}\n{text[:500]}")

    # Assemble Fixture
    exps = [
        ExpectedObservation(
            priority=e["priority"],
            must_contain_any_of=[list(x) for x in e["must_contain_any_of"]],
            semantic_description=e.get("semantic_description", ""),
        )
        for e in data["expected_observations"]
    ]
    return Fixture(
        id=seed.id,
        category=seed.category,
        seed_id=seed.id,
        generated_by=generator_model,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dialogue=data["dialogue"],
        expected_observations=exps,
        must_not_contain_globally=list(data.get("must_not_contain_globally", [])),
    )


async def run_fixture_generation(
    seeds_path: Path,
    fixtures_dir: Path,
    generator_model: str = FIXTURE_GENERATOR_MODEL,
) -> list[Path]:
    """For each seed without an existing fx_XXX.json (approved) OR .draft.json (in-progress),
    call Opus to generate .draft.json. Return paths written.
    """
    seeds = load_seeds(seeds_path)
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for seed in seeds:
        approved = fixtures_dir / f"{seed.id}.json"
        draft = fixtures_dir / f"{seed.id}.draft.json"
        if approved.exists():
            print(f"  ⏭  {seed.id} — already approved, skip")
            continue
        if draft.exists():
            print(f"  ⏭  {seed.id} — draft exists, skip (delete to regenerate)")
            continue

        print(f"  ⚙  {seed.id} — generating via {generator_model}...")
        try:
            fx = await generate_fixture_draft(seed, generator_model)
            path = save_draft_fixture(fx, fixtures_dir)
            print(f"  ✓ {seed.id} → {path.name}")
            written.append(path)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {seed.id} — generation failed: {e}")

    return written
```

- [ ] **Step 2: Verify module imports**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import observer_bench; print('OK')"`
Expected: `OK`

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `24 passed`

- [ ] **Step 3: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): fixture generator (Opus → .draft.json)"
```

---

## Task 13: Output writers (CSV + summary.md + run_meta.json)

**Files:**
- Modify: `scripts/observer_bench.py`
- Modify: `tests/test_observer_bench.py`

- [ ] **Step 1: Write failing test for CSV writer**

Append to `tests/test_observer_bench.py`:

```python
def test_write_observer_csv(tmp_path):
    r = ob.ObserverResult(
        timestamp="2026-04-15T14:30:00+00:00",
        model="gemini-2.5-flash", model_is_fallback=False,
        provider="google", fixture_id="fx_001",
        fixture_category="smart_home",
        tool_success=True,
        precision=0.9, recall=0.8, f1=0.85,
        priority_accuracy=0.75,
        hallucination=False, extra_count=0,
        expected_count=3, matched_count=2,
        observer_latency_ms=1200.0,
        actual_input_tokens_api=500, output_tokens=80,
        cost_usd=0.0005,
        model_output_raw='{"observations":[...]}',
    )
    path = ob.write_observer_csv([r], tmp_path)
    assert path.name == "results.csv"
    content = path.read_text(encoding="utf-8")
    assert "gemini-2.5-flash" in content
    assert "fx_001" in content
    assert "smart_home" in content
    assert "0.85" in content   # f1


def test_compute_pilot_pass_rules():
    """Spec §9.4: pass iff tool_success_rate >= 0.80 AND mean_f1 >= 0.30."""
    scores_pass = [
        ob.Scores(tool_success=True, precision=0.5, recall=0.5, f1=0.5,
                  priority_accuracy=0.5, hallucination=False, extra_count=0)
        for _ in range(5)
    ]
    assert ob.compute_pilot_pass(scores_pass) is True

    # 60% tool success → fail
    scores_low_tool = scores_pass[:3] + [
        ob.Scores(tool_success=False, precision=0.0, recall=0.0, f1=0.0,
                  priority_accuracy=0.0, hallucination=False, extra_count=0)
        for _ in range(2)
    ]
    assert ob.compute_pilot_pass(scores_low_tool) is False

    # F1 avg 0.2 → fail
    scores_low_f1 = [
        ob.Scores(tool_success=True, precision=0.2, recall=0.2, f1=0.2,
                  priority_accuracy=0.5, hallucination=False, extra_count=0)
        for _ in range(5)
    ]
    assert ob.compute_pilot_pass(scores_low_f1) is False
```

- [ ] **Step 2: Run tests, verify failures**

Run: `python -m pytest tests/test_observer_bench.py -q -k "csv or pilot_pass"`
Expected: 2 failures

- [ ] **Step 3: Implement output writers**

Replace `# ===== §10 OUTPUT (later tasks) =====` in `scripts/observer_bench.py`:

```python
# ===== §10 OUTPUT =====

CSV_FIELDS_OBS = [
    "timestamp", "model", "model_is_fallback", "provider",
    "fixture_id", "fixture_category",
    "tool_success",
    "precision", "recall", "f1", "priority_accuracy",
    "hallucination", "extra_count",
    "expected_count", "matched_count",
    "observer_latency_ms",
    "actual_input_tokens_api", "output_tokens", "cost_usd",
    "model_output_raw", "error",
]


def write_observer_csv(results: list[ObserverResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "results.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS_OBS)
        w.writeheader()
        for r in results:
            row = asdict(r)
            # Truncate raw + strip newlines
            row["model_output_raw"] = row["model_output_raw"].replace("\n", " ")[:1000]
            w.writerow(row)
    return path


def compute_pilot_pass(scores: list[Scores]) -> bool:
    """Spec §9.4: pass iff tool_success_rate >= 0.80 AND mean_f1 >= 0.30."""
    if not scores:
        return False
    tool_rate = sum(1 for s in scores if s.tool_success) / len(scores)
    mean_f1 = sum(s.f1 for s in scores) / len(scores)
    return tool_rate >= PILOT_TOOL_SUCCESS_THRESHOLD and mean_f1 >= PILOT_F1_THRESHOLD


def _group_by_model(results: list[ObserverResult]) -> dict[tuple[str, str], list[ObserverResult]]:
    groups: dict[tuple[str, str], list[ObserverResult]] = {}
    for r in results:
        groups.setdefault((r.provider, r.model), []).append(r)
    return groups


def _aggregate_model_metrics(rows: list[ObserverResult]) -> dict[str, Any]:
    """Macro-avg per-model metrics from per-fixture rows."""
    if not rows:
        return {}
    ok_rows = [r for r in rows if not r.error]
    if not ok_rows:
        ok_rows = rows
    n = len(ok_rows)
    tool_rate = sum(1 for r in ok_rows if r.tool_success) / n
    precision = sum(r.precision for r in ok_rows) / n
    recall = sum(r.recall for r in ok_rows) / n
    f1 = sum(r.f1 for r in ok_rows) / n
    prio = sum(r.priority_accuracy for r in ok_rows) / n
    halluc_rate = sum(1 for r in ok_rows if r.hallucination) / n
    latencies = [r.observer_latency_ms for r in ok_rows if r.observer_latency_ms > 0]
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else -1.0
    p95_idx = int(len(latencies_sorted) * 0.95)
    p95 = latencies_sorted[min(p95_idx, len(latencies_sorted) - 1)] if latencies_sorted else -1.0
    cost_per_100 = (sum(r.cost_usd for r in ok_rows) / n) * 100 if n else 0.0
    return dict(
        n=n, tool_rate=tool_rate, precision=precision, recall=recall, f1=f1,
        priority_accuracy=prio, halluc_rate=halluc_rate,
        latency_p50=p50, latency_p95=p95, cost_per_100=cost_per_100,
    )


def render_observer_summary(results: list[ObserverResult], output_dir: Path,
                            run_args: dict[str, Any]) -> Path:
    """Render summary.md with 5+1 tables per spec §10.2."""
    path = output_dir / "summary.md"
    groups = _group_by_model(results)
    model_keys_sorted = sorted(groups.keys(), key=lambda k: (
        -_aggregate_model_metrics(groups[k]).get("f1", 0.0)  # desc F1
    ))

    lines = [
        f"# Observer Bench — {run_args.get('timestamp', '?')}",
        "",
        f"**Mode:** `{run_args.get('mode', '?')}` · "
        f"**Total calls:** {len(results)} · "
        f"**Errors:** {sum(1 for r in results if r.error)} · "
        f"**Total cost:** ${sum(r.cost_usd for r in results):.2f}",
        "",
    ]

    # Table 1: Main ranking
    lines += [
        "## Table 1 — 主排名 (按 F1 降序)",
        "",
        "| Model | F1 | Precision | Recall | Priority Acc | Halluc Rate | Tool Success |",
        "|---|---|---|---|---|---|---|",
    ]
    for prov, model in model_keys_sorted:
        m = _aggregate_model_metrics(groups[(prov, model)])
        lines.append(
            f"| {prov}/{model} | {m['f1']:.2f} | {m['precision']:.2f} | {m['recall']:.2f} "
            f"| {m['priority_accuracy']:.2f} | {m['halluc_rate']*100:.0f}% | {m['tool_rate']*100:.0f}% |"
        )
    lines.append("")

    # Table 2: Cost + latency
    lines += [
        "## Table 2 — 成本延迟",
        "",
        "| Model | $/100 calls | Latency p50 | Latency p95 |",
        "|---|---|---|---|",
    ]
    for prov, model in model_keys_sorted:
        m = _aggregate_model_metrics(groups[(prov, model)])
        lines.append(
            f"| {prov}/{model} | ${m['cost_per_100']:.3f} | "
            f"{m['latency_p50']:.0f}ms | {m['latency_p95']:.0f}ms |"
        )
    lines.append("")

    # Table 3a: F1 by category
    lines += [
        "## Table 3a — F1 按 fixture category 分解",
        "",
        "| Model | " + " | ".join(FIXTURE_CATEGORIES) + " |",
        "|---|" + "|".join(["---"] * len(FIXTURE_CATEGORIES)) + "|",
    ]
    for prov, model in model_keys_sorted:
        row = [f"{prov}/{model}"]
        for cat in FIXTURE_CATEGORIES:
            cat_rows = [r for r in groups[(prov, model)] if r.fixture_category == cat and not r.error]
            if not cat_rows:
                row.append("—")
            else:
                f1 = sum(r.f1 for r in cat_rows) / len(cat_rows)
                row.append(f"{f1:.2f}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Table 3b: F1 by priority
    lines += [
        "## Table 3b — F1 按 priority 分解",
        "",
        "| Model | 🔴 F1 | 🟡 F1 | 🟢 F1 | ✅ F1 |",
        "|---|---|---|---|---|",
    ]
    for prov, model in model_keys_sorted:
        # per-priority: for each priority, find expected_observations of that priority and compute F1
        # This requires re-doing matching at priority level; for now, show overall F1 gated by matches
        # Simplification: priority_accuracy already aggregates per-priority correctness
        rows = [r for r in groups[(prov, model)] if not r.error]
        # We don't store per-priority breakdown directly; report overall per-priority placeholder
        # (Full per-priority F1 requires carrying per-observation priority match; v1 keeps it simple)
        row = [f"{prov}/{model}"]
        for _pr in ("🔴", "🟡", "🟢", "✅"):
            row.append("TBD")   # Per-priority breakdown requires richer CSV; reserve for v1.1
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("_Note: per-priority F1 requires per-observation tracking not in CSV v1. "
                 "Table 3b is a placeholder; see `priority_accuracy` column for now._")
    lines.append("")

    # Table 4: Hallucination samples (manual halluc_type left blank)
    lines += [
        "## Table 4 — Hallucination 样例 (halluc_type 由 Allen 审核标注)",
        "",
        "| fixture_id | model | halluc_type | 触发的 observation 文本 |",
        "|---|---|---|---|",
    ]
    halluc_rows = [r for r in results if r.hallucination]
    for r in halluc_rows[:20]:   # cap to 20 rows
        text = r.model_output_raw[:200].replace("|", "\\|")
        lines.append(f"| {r.fixture_id} | {r.provider}/{r.model} | _TBD_ | {text} |")
    if not halluc_rows:
        lines.append("| (无 hallucination 记录) | | | |")
    lines.append("")

    # Table 5: Recommendation (agent fills in after looking at Table 1)
    lines += [
        "## Table 5 — 推荐 (由 Allen 基于上方数据填写)",
        "",
        "### 🥇 主 Observer: _(按 Table 1 F1 最高 + Halluc 最低选)_",
        "",
        "### 🥈 Fallback: _(按不同 provider 选一个 F1 接近的)_",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_run_meta_observer(
    results: list[ObserverResult],
    output_dir: Path,
    run_args: dict[str, Any],
    pilot_pass_map: dict[str, bool] | None = None,
    pilot_exit_reason: dict[str, str] | None = None,
) -> Path:
    """Write run_meta.json. In pilot mode, include pilot_pass per model."""
    path = output_dir / "run_meta.json"
    meta = {
        "mode": run_args.get("mode"),
        "timestamp": run_args.get("timestamp"),
        "total_calls": len(results),
        "errors_total": sum(1 for r in results if r.error),
        "cost_usd_total": round(sum(r.cost_usd for r in results), 4),
        "elapsed_sec": run_args.get("elapsed_sec", -1),
        "pricing_snapshot_date": v3.PRICING_SNAPSHOT_DATE,
        "fixtures_used": sorted(set(r.fixture_id for r in results)),
        "active_models": run_args.get("active_models", []),
        "pilot_pass": pilot_pass_map or {},
        "pilot_exit_reason": pilot_exit_reason or {},
        "args": run_args.get("raw_args", {}),
    }
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `26 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/observer_bench.py tests/test_observer_bench.py
git commit -m "feat(observer-bench): CSV + summary.md (5+1 tables) + run_meta.json with pilot_pass"
```

---

## Task 14: CLI + orchestration

**Files:**
- Modify: `scripts/observer_bench.py`

- [ ] **Step 1: Implement CLI + orchestration**

Replace the `main()` stub at the end of `scripts/observer_bench.py` (and `# ===== §11 CLI (later tasks) =====`):

```python
# ===== §11 CLI =====

DEFAULT_SEEDS_PATH = Path("bench_fixtures/observer_cn/seeds.yaml")
DEFAULT_FIXTURES_DIR = Path("bench_fixtures/observer_cn")


async def _observer_generate(seeds_path: Path, fixtures_dir: Path) -> None:
    print(f"▸ Reading seeds from {seeds_path}")
    if not seeds_path.exists():
        raise SystemExit(f"seeds.yaml not found at {seeds_path}. Create it first.")
    written = await run_fixture_generation(seeds_path, fixtures_dir)
    print()
    print(f"▸ Generated {len(written)} .draft.json files in {fixtures_dir}")
    print("  Next step: open each fx_XXX.draft.json, edit,")
    print(f"             then rename: mv {fixtures_dir}/fx_XXX.draft.json {fixtures_dir}/fx_XXX.json")


def _load_pilot_pass(results_root: Path) -> tuple[dict[str, bool], dict[str, str]]:
    """Find the most recent observer-pilot run_meta.json, extract pilot_pass map."""
    if not results_root.exists():
        return {}, {}
    candidates = sorted(results_root.glob("observer_*/run_meta.json"), reverse=True)
    for meta_path in candidates:
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            if m.get("mode") == "observer-pilot" and m.get("pilot_pass"):
                return m["pilot_pass"], m.get("pilot_exit_reason", {})
        except (json.JSONDecodeError, OSError):
            continue
    return {}, {}


async def _run_observer_matrix(
    active: list[ActiveObserver],
    fixtures: list[Fixture],
) -> list[ObserverResult]:
    """For each (model, fixture), run call_observer_with_retry."""
    from tqdm.asyncio import tqdm as async_tqdm

    total = len(active) * len(fixtures)
    pbar = async_tqdm(total=total, desc="observer", unit="call")
    running_cost = [0.0]
    error_count = [0]

    async def _one(a: ActiveObserver, fx: Fixture) -> ObserverResult:
        r = await call_observer_with_retry(a.spec, a.active_model_id, a.is_fallback, fx)
        pbar.update(1)
        running_cost[0] += r.cost_usd
        if r.error:
            error_count[0] += 1
        pbar.set_postfix(cost=f"${running_cost[0]:.2f}", errors=error_count[0])
        return r

    # per-provider Semaphore(1) to serialize same-provider calls
    provider_sems: dict[str, asyncio.Semaphore] = {
        a.spec.provider: asyncio.Semaphore(1) for a in active
    }

    async def _with_sem(a: ActiveObserver, fx: Fixture) -> ObserverResult:
        async with provider_sems[a.spec.provider]:
            return await _one(a, fx)

    tasks = [_with_sem(a, fx) for a in active for fx in fixtures]
    results = await asyncio.gather(*tasks)
    pbar.close()
    return results


async def _main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

    seeds_path = Path(args.seeds or DEFAULT_SEEDS_PATH)
    fixtures_dir = Path(args.fixtures_dir)

    # Sub-command: --observer-generate
    if args.observer_generate:
        await _observer_generate(seeds_path, fixtures_dir)
        return

    # Resolve plan
    if args.model:
        target = [s for s in v3.MODEL_CATALOG
                  if s.primary_id == args.model or args.model in s.fallback_ids]
        if not target:
            raise SystemExit(f"Model '{args.model}' not in catalog.")
        candidate_ids = (target[0].primary_id,)
        mode = "model"
    elif args.observer_pilot:
        candidate_ids = OBSERVER_CANDIDATES
        mode = "observer-pilot"
    elif args.observer:
        candidate_ids = OBSERVER_CANDIDATES
        mode = "observer"
    else:
        raise SystemExit("Specify one of: --observer / --observer-pilot / --observer-generate / --model <id>")

    # Load fixtures
    all_fixtures = load_approved_fixtures(fixtures_dir)
    if mode == "observer-pilot":
        # Take fx_001 ~ fx_005 only
        pilot_ids = {f"fx_{i:03d}" for i in range(1, 6)}
        fixtures = [f for f in all_fixtures if f.id in pilot_ids]
    else:
        fixtures = all_fixtures

    if not fixtures:
        raise SystemExit(
            f"No approved fixtures in {fixtures_dir}. "
            f"Run --observer-generate first, then rename .draft.json → .json to approve."
        )

    # --observer (full): apply pilot early-exit
    pilot_pass_map: dict[str, bool] = {}
    pilot_exit_reason: dict[str, str] = {}
    if mode == "observer" and not args.include_failed_pilot:
        pass_map, exit_reason = _load_pilot_pass(Path("bench_results"))
        if pass_map:
            before = len(candidate_ids)
            candidate_ids = tuple(cid for cid in candidate_ids if pass_map.get(cid, True))
            excluded = set(OBSERVER_CANDIDATES) - set(candidate_ids)
            if excluded:
                print(f"▸ Pilot early-exit: skipping {len(excluded)} models")
                for cid in sorted(excluded):
                    print(f"  ✗ {cid} — {exit_reason.get(cid, 'failed pilot')}")
                print(f"  (use --include-failed-pilot to override)")

    # Dry run
    if args.dry_run:
        n_calls = len(candidate_ids) * len(fixtures)
        est_cost = n_calls * 0.012   # rough ballpark
        print(f"[DRY RUN] mode: --{mode}")
        print(f"[DRY RUN] candidates: {candidate_ids}")
        print(f"[DRY RUN] fixtures: {len(fixtures)} ({[f.id for f in fixtures]})")
        print(f"[DRY RUN] total calls: {n_calls}")
        print(f"[DRY RUN] estimated cost: ~${est_cost:.2f}")
        return

    # Warmup
    print(f"\n▸ Warmup for {len(candidate_ids)} candidates (isolated payload):")
    active = await resolve_active_observers(candidate_ids)
    if not active:
        raise SystemExit("No models survived warmup. Aborting.")

    # Run matrix
    print(f"\n▸ Running --{mode}: {len(active)} models × {len(fixtures)} fixtures = "
          f"{len(active) * len(fixtures)} calls")
    t0 = time.monotonic()
    results = await _run_observer_matrix(active, fixtures)
    elapsed = time.monotonic() - t0

    # Compute pilot_pass map (only in observer-pilot mode)
    if mode == "observer-pilot":
        for a in active:
            model_rows = [r for r in results if r.model == a.active_model_id]
            scores = [
                Scores(
                    tool_success=r.tool_success, precision=r.precision, recall=r.recall,
                    f1=r.f1, priority_accuracy=r.priority_accuracy,
                    hallucination=r.hallucination, extra_count=r.extra_count,
                ) for r in model_rows
            ]
            ok = compute_pilot_pass(scores)
            pilot_pass_map[a.active_model_id] = ok
            if not ok:
                tool_rate = sum(1 for s in scores if s.tool_success) / len(scores) if scores else 0.0
                mean_f1 = sum(s.f1 for s in scores) / len(scores) if scores else 0.0
                pilot_exit_reason[a.active_model_id] = (
                    f"tool_success={tool_rate:.0%} (need ≥{PILOT_TOOL_SUCCESS_THRESHOLD:.0%}), "
                    f"F1={mean_f1:.2f} (need ≥{PILOT_F1_THRESHOLD:.2f})"
                )

    # Output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_dir = Path(args.output_dir) if args.output_dir else Path(f"bench_results/observer_{timestamp}")
    csv_path = write_observer_csv(results, output_dir)
    summary_path = render_observer_summary(results, output_dir, run_args={
        "mode": mode, "timestamp": timestamp, "elapsed_sec": elapsed,
    })
    meta_path = write_run_meta_observer(results, output_dir, run_args={
        "mode": mode, "timestamp": timestamp, "elapsed_sec": elapsed,
        "active_models": [a.active_model_id for a in active],
        "raw_args": {k: v for k, v in vars(args).items() if not callable(v)},
    }, pilot_pass_map=pilot_pass_map, pilot_exit_reason=pilot_exit_reason)

    errors = sum(1 for r in results if r.error)
    print(f"\n▸ Done in {elapsed/60:.1f} min "
          f"({len(results)} calls, {errors} errors, "
          f"${sum(r.cost_usd for r in results):.2f})")
    print(f"  CSV:     {csv_path}")
    print(f"  Summary: {summary_path}")
    print(f"  Meta:    {meta_path}")
    if mode == "observer-pilot":
        passed = sum(1 for v in pilot_pass_map.values() if v)
        print(f"  Pilot pass: {passed}/{len(pilot_pass_map)}")
        for mid, reason in pilot_exit_reason.items():
            print(f"    ✗ {mid}: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="observer_bench", description="Chinese Observer benchmark")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--observer", action="store_true", help="Run full observer bench")
    mode_group.add_argument("--observer-pilot", action="store_true", help="Run pilot (fx_001~fx_005)")
    mode_group.add_argument("--observer-generate", action="store_true",
                             help="Generate draft fixtures from seeds.yaml via Opus")
    mode_group.add_argument("--model", type=str, help="Single model override")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--fixtures-dir", type=str, default=str(DEFAULT_FIXTURES_DIR))
    parser.add_argument("--seeds", type=str, default=None)
    parser.add_argument("--include-failed-pilot", action="store_true",
                        help="Don't skip models that failed pilot")
    args = parser.parse_args()

    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify module imports + CLI help**

Run: `python scripts/observer_bench.py --help`
Expected: argparse help output showing all flags

Run: `python -m pytest tests/test_observer_bench.py -q`
Expected: `26 passed`

- [ ] **Step 3: Dry-run smoke (no API calls)**

Create a minimal test seeds + fixtures to verify CLI wiring (remove after):

```bash
mkdir -p /tmp/ob_test
cat > /tmp/ob_test/seeds.yaml <<'EOF'
- id: fx_001
  category: smart_home
  scene: test
  user_emotion_hint: neutral
  tone_hint: test
  dialogue_length_hint: 2 turns
  must_capture: ["test"]
  must_not_hallucinate: []
EOF

python scripts/observer_bench.py --observer-pilot --dry-run \
    --seeds /tmp/ob_test/seeds.yaml --fixtures-dir /tmp/ob_test
```

Expected: Prints `[DRY RUN]` lines, fails with "No approved fixtures" (since no fx_001.json exists). That's the expected message — fixture approval is Allen's manual step.

Clean up: `rm -rf /tmp/ob_test`

- [ ] **Step 4: Commit**

```bash
git add scripts/observer_bench.py
git commit -m "feat(observer-bench): CLI + orchestration + pilot early-exit"
```

---

## Task 15: Manual — Allen writes 5 pilot seeds

**Files:**
- Create: `/Users/alllllenshi/Projects/jarvis/bench_fixtures/observer_cn/seeds.yaml`

This task is **manual** — Allen must write the seed clauses. No code changes.

- [ ] **Step 1: Create `bench_fixtures/observer_cn/seeds.yaml` with 5 pilot seeds**

Allen writes 5 seeds covering 5 different categories. Example skeleton:

```yaml
# bench_fixtures/observer_cn/seeds.yaml
#
# Pilot 阶段: 5 条覆盖 5 个 category (快速验证评测口径)
# Full 阶段: 扩到 20 条, 按 §5.1 分布要求

- id: fx_001
  category: smart_home
  scene: "智能家居 + 疲惫语气"
  user_emotion_hint: tired
  tone_hint: "口语化·带抱怨·短句·允许粗口"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "偏好: 客厅灯暖黄色 (🔴)"
    - "情绪: 用户疲惫 (🟡)"
    - "完成: 灯调节任务 (✅)"
  must_not_hallucinate:
    - "蓝光"
    - "冷白"
    - "卧室"

- id: fx_002
  category: preference
  scene: "食物过敏声明"
  user_emotion_hint: neutral
  tone_hint: "平静陈述"
  dialogue_length_hint: "2-3 turns"
  must_capture:
    - "过敏: 虾 (🔴, 不可变)"
  must_not_hallucinate:
    - "喜欢虾"
    - "不喜欢虾"

- id: fx_003
  category: state_change
  scene: "工作变更"
  user_emotion_hint: neutral
  tone_hint: "随口提起·附带抱怨新同事"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "状态变更: 从 Acme 换到 Stripe (🔴, 明确替换)"
  must_not_hallucinate:
    - "Acme 工作"   # 老信息不能保留
    - "同时在两家"

- id: fx_004
  category: temporal
  scene: "定提醒"
  user_emotion_hint: neutral
  tone_hint: "事务性"
  dialogue_length_hint: "2-3 turns"
  must_capture:
    - "提醒: 明天下午 3 点开会 (🔴, 带具体时间)"
  must_not_hallucinate:
    - "今天"
    - "下周"

- id: fx_005
  category: emotion
  scene: "生气抱怨上司"
  user_emotion_hint: angry
  tone_hint: "激动·断句·可能粗口"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "情绪: 用户愤怒 (🟡)"
    - "实体: 上司 (🟡)"
  must_not_hallucinate:
    - "辞职"   # 未说就不能推断
    - "恨"     # 动词不可强化
```

- [ ] **Step 2: Commit seeds**

```bash
git add bench_fixtures/observer_cn/seeds.yaml
git commit -m "data(observer-bench): 5 pilot seeds (smart_home/preference/state_change/temporal/emotion)"
```

**Note to Allen**: 重点是 `tone_hint` 要具体 (不要 "自然" 这种空词), `must_not_hallucinate` 要列 Opus 大概率会产生的污染词。

---

## Task 16: Manual — Allen runs --observer-generate, edits, approves

**Files:** (created by Opus)
- `bench_fixtures/observer_cn/fx_001.draft.json` → `fx_001.json` (after Allen approves)
- `bench_fixtures/observer_cn/fx_002.draft.json` → `fx_002.json`
- `bench_fixtures/observer_cn/fx_003.draft.json` → `fx_003.json`
- `bench_fixtures/observer_cn/fx_004.draft.json` → `fx_004.json`
- `bench_fixtures/observer_cn/fx_005.draft.json` → `fx_005.json`

- [ ] **Step 1: Set Anthropic key**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

- [ ] **Step 2: Generate drafts**

```bash
cd ~/Projects/jarvis
uv run python scripts/observer_bench.py --observer-generate
```

Expected output:
```
▸ Reading seeds from bench_fixtures/observer_cn/seeds.yaml
  ⚙  fx_001 — generating via claude-opus-4-6...
  ✓ fx_001 → fx_001.draft.json
  ⚙  fx_002 — generating via claude-opus-4-6...
  ✓ fx_002 → fx_002.draft.json
  ...
▸ Generated 5 .draft.json files in bench_fixtures/observer_cn
```

Expected cost: ~$1.00 (5 Opus calls)

- [ ] **Step 3: Allen reviews each draft**

For each `fx_XXX.draft.json`:
1. Open in VS Code / vim
2. Read `dialogue` — is it natural Chinese? Does tone match `tone_hint`? If teacher-style, edit.
3. Read `expected_observations`:
   - `priority` correct?
   - `must_contain_any_of` has 2-3 sub-lists each?
   - Keywords actually appear somewhere the observer would reasonably produce?
4. Read `must_not_contain_globally` — any obvious hallucinations missing?
5. Save file.

- [ ] **Step 4: Approve by renaming**

```bash
cd bench_fixtures/observer_cn
for f in fx_*.draft.json; do
  approved="${f%.draft.json}.json"
  echo "Review: $f"
  read -p "Approve as $approved? (y/n) " ans
  [[ "$ans" == "y" ]] && mv "$f" "$approved"
done
cd -
```

- [ ] **Step 5: Commit approved fixtures**

```bash
git add bench_fixtures/observer_cn/fx_*.json
git commit -m "data(observer-bench): 5 pilot fixtures approved by Allen"
```

---

## Task 17: Manual — Allen runs `--observer-pilot` + reviews

**Files:** (outputs only)
- `bench_results/observer_<timestamp>/results.csv`
- `bench_results/observer_<timestamp>/summary.md`
- `bench_results/observer_<timestamp>/run_meta.json`

- [ ] **Step 1: Set all 6 API keys**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-proj-...
export GEMINI_API_KEY=AIza...
export GROQ_API_KEY=gsk_...
export XAI_API_KEY=xai-...
export DEEPSEEK_API_KEY=sk-...    # NEW — get from platform.deepseek.com
```

- [ ] **Step 2: Dry-run first**

```bash
uv run python scripts/observer_bench.py --observer-pilot --dry-run
```
Expected: plan showing 8 models × 5 fixtures = 40 calls, ~$0.50

- [ ] **Step 3: Run pilot for real**

```bash
uv run python scripts/observer_bench.py --observer-pilot
```

Expected: ~3 min, ~$0.35, 40 calls, generates `bench_results/observer_YYYY-MM-DD_HHMM/`

- [ ] **Step 4: Review summary.md**

```bash
LATEST=$(ls -td bench_results/observer_*/ | head -1)
cat "$LATEST/summary.md"
```

**Red flags to look for:**
- 所有模型 F1 < 0.2: 评测算法太严 → 扩 `must_contain_any_of` sub-lists
- 所有模型 F1 > 0.95: fixture 太简单, ground truth 太宽松 → 收紧关键词
- Halluc rate 全 0: `must_not_contain_globally` 列表不够锋利 → 加常见幻觉词
- Haiku tool_success < 80%: 确认 Pack 03 断言, 记入 pilot_pass=false

- [ ] **Step 5: Review Hallucination table**

Look at Table 4 in summary.md. For each halluc=True row, manually annotate `halluc_type`:
- `(a) 凭空造事实`: 不存在的实体/地点
- `(b) 过度推断`: 从短陈述扩张
- `(c) 格式污染`: emoji/priority 误用

Allen can edit summary.md directly to add these annotations (it's git-tracked).

- [ ] **Step 6: Decide next step**

- ✅ **评测口径合理 + pilot 数据有意义** → 继续 Task 15 扩展 seeds.yaml 到 20 条, 回到 Task 16
- ❌ **评测算法需要调** → 改 `scripts/observer_bench.py` §8 evaluate, 补 test 案例到 test_observer_bench.py, 重跑 Task 17

- [ ] **Step 7: Commit pilot run record (optional)**

Pilot results are under `bench_results/` which is `.gitignore`'d. If Allen wants to capture a specific summary for reference:

```bash
cp bench_results/observer_<ts>/summary.md docs/observer-bench-pilot-2026-04-15.md
git add docs/observer-bench-pilot-2026-04-15.md
git commit -m "docs(observer-bench): pilot summary 2026-04-15"
```

---

## Post-Plan: Full Run

After pilot validates and Allen expands to 20 seeds + approves 20 fixtures:

```bash
uv run python scripts/observer_bench.py --observer
```

Expected: ≤160 calls (pilot early-exit may cut Haiku + old Grok), ~$1.50-2.00, ~15 min.
Output: `bench_results/observer_<ts>/summary.md` with full data.

Allen analyzes, updates `config.yaml`:
```yaml
observer:
  primary:
    provider: <winner>
    model: <winner model id>
  fallback:
    provider: <fallback provider>
    model: <fallback model id>
```

---

## Self-Review

**Spec coverage map:**

| Spec section | Implemented in task |
|---|---|
| §1 动机 | (context only) |
| §2 目标 | Task 14 (CLI modes) |
| §3 文件结构 | Task 1 (scaffold) |
| §4 架构总览 | Distributed across Tasks 1-14 |
| §5.1 seeds.yaml schema | Task 4 (load_seeds validation) + Task 15 (Allen writes) |
| §5.2 fx_XXX.json schema | Task 4 (load_approved_fixtures + save_draft) |
| §5.3 fixture workflow | Task 16 (manual: Opus → Allen approve) |
| §6.1 OBSERVER_SYSTEM_PROMPT | Task 2 |
| §6.2 OBSERVER_TOOL_DEF | Task 2 |
| §6.3 build_observer_prompt | Task 5 |
| §6.4 build_tool_call_kwargs | Task 5 |
| §6.5 Zero-invasion v3 callers | Tasks 6, 7, 8 |
| §7.1 evaluate | Task 10 |
| §7.2 per-model aggregation | Task 13 (_aggregate_model_metrics) |
| §7.3 评测口径边界 | Task 10 (tests cover boundaries) |
| §8 Model catalog additions | Task 1 |
| §9.1 CLI 命令 | Task 14 |
| §9.2 Args | Task 14 |
| §9.3 安全设施 | Task 11 (warmup) + Task 9 (retry) |
| §9.4 Pilot early-exit | Task 13 (compute_pilot_pass) + Task 14 (_load_pilot_pass) |
| §10.1 CSV fields | Task 13 |
| §10.2 Summary tables | Task 13 (5+1 tables) |
| §10.3 run_meta.json | Task 13 (write_run_meta_observer) |
| §10.4 chart.html | Deferred to v1.1 (optional per spec) |
| §11 成本估算 | Task 14 (dry-run estimate) |
| §12 已知限制 | (documented, no code) |
| §13 成功标准 | Task 17 (Allen review) |

**Gaps identified during self-review:**
1. **§10.4 chart.html** — spec says "可选", I didn't add a task. Acceptable — user can add later via Task 13 extension.
2. **§10.2 Table 3b per-priority F1** — implemented as placeholder ("TBD") in Task 13, since fully implementing per-priority F1 requires per-observation priority tracking not in CSV v1. This is a known limitation noted in the summary table itself.
3. **§10.2 Table 4 halluc_type** — left as manual annotation column (Allen edits summary.md post-run). Matches spec expectation.

**Placeholder scan:** 
- "TBD" in summary Table 3b placeholders: intentional (self-documented limitation)
- No TODO/XXX/FIXME found

**Type consistency:** `Seed` / `Fixture` / `Scores` / `ObserverCall` / `ObserverResult` / `ActiveObserver` used consistently. Field names match across dataclass + CSV + summary rendering.

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-15-observer-bench.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (1-14), review between, pause at 15-17 (manual Allen tasks)

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
