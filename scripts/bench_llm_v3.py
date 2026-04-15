# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.42",
#   "openai>=1.50",
#   "google-generativeai>=0.8",
#   "groq>=0.11",
#   "tiktoken>=0.8",
#   "tqdm>=4.66",
#   "plotly>=5.18",
#   "pandas>=2.0",
# ]
# ///
"""Jarvis LLM selection benchmark — TTFT / recall / real cache-hit behavior."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

LOGGER = logging.getLogger("bench_llm_v3")

# ===== CONSTANTS =====

PRICING_SNAPSHOT_DATE = "2026-04-14"

TASKS = {
    "simple":    "今天温哥华天气怎么样？",
    "recall":    "根据以上观察，Allen 最喜欢喝的咖啡是什么？请用一句话回答。",
    "synthesis": "根据以上观察，Allen 最近一个月的生活状态如何？给出 3 点简短建议。",
}

NEEDLE_LINE = "* 🟠 (14:28) Allen 最喜欢喝拿铁，尤其是 Revolver 咖啡馆的日晒耶加雪菲豆. (meaning 2026-04-09)"
DISTRACTOR_LINE = "* 🟢 (11:04) Allen 提到过同事喜欢美式咖啡. (meaning 2026-04-08)"

OBSERVATION_TEMPLATES = [
    "早上喝了第一杯咖啡，今天 Standard Brew",
    "昨晚睡眠 7h 20min，Oura ring 评分 82",
    "在 Main St 新开的面包店排队",
    "下午和 Ryan 讨论了 Jarvis 的 ASR latency",
    "跑步 5km，配速 5:30，在 Stanley Park",
    "晚饭吃了 ramen，Santouka 新开分店",
    "会议讨论 OM observation loop 的 v2 设计",
    "买了新的 Sennheiser 耳机，音质不错",
    "下雨，Vancouver 典型天气",
    "Lulu 来家里吃饭，做了番茄意面",
]
assert len(OBSERVATION_TEMPLATES) >= 10  # 启动时 sanity check (expanded in Task 2)

VERBS = ["观察", "提到", "记录", "体验到", "反馈"]
EMOJIS = ["🟢", "🟡", "🟠", "🔵", "🟣"]

# ===== DATACLASSES =====

@dataclass(frozen=True)
class ModelSpec:
    provider: str
    primary_id: str
    fallback_ids: tuple[str, ...]
    input_price_per_1m: float
    output_price_per_1m: float
    cache_write_multiplier: float  # Anthropic 1.25, others 1.00
    cache_read_multiplier: float   # 0.10-1.00 (1.00 means no cache discount)
    min_cache_tokens: int

MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("anthropic", "claude-sonnet-4-6",           ("claude-sonnet-4-5",),                3.00,  15.00, 1.25, 0.10, 1024),
    ModelSpec("anthropic", "claude-opus-4-6",             ("claude-opus-4-5",),                 15.00,  75.00, 1.25, 0.10, 1024),
    ModelSpec("anthropic", "claude-haiku-4-5-20251001",   ("claude-haiku-4-5",),                 1.00,   5.00, 1.25, 0.10, 1024),
    ModelSpec("openai",    "gpt-5",                       ("gpt-4o",),                           2.50,  10.00, 1.00, 0.50, 1024),
    ModelSpec("openai",    "gpt-5-mini",                  ("gpt-4o-mini",),                      0.15,   0.60, 1.00, 0.50, 1024),
    ModelSpec("google",    "gemini-3-pro-preview",        ("models/gemini-3-pro-preview",
                                                           "gemini-2.5-pro",
                                                           "models/gemini-2.5-pro"),              1.25,   5.00, 1.00, 0.25, 4096),
    ModelSpec("xai",       "grok-4-1-fast-non-reasoning", ("grok-4",),                           0.20,   0.50, 1.00, 0.25, 1024),
    ModelSpec("groq",      "llama-3.3-70b-versatile",     (),                                    0.59,   0.79, 1.00, 1.00, 999_999),
)

@dataclass
class CallResult:
    timestamp: str
    model: str
    model_is_fallback: bool
    provider: str
    nominal_tokens_cl100k: int
    actual_input_tokens_api: int
    task: str
    cache_state: str          # "cold" | "warm" | "stale"
    run_idx: int              # 0=cold, 1=warm1, 2=warm2
    ttft_ms: float
    total_ms: float
    output_tokens: int
    tokens_per_second: float
    answer: str
    answer_correct: bool | None   # None for non-recall
    cache_actually_hit: bool
    cache_write_tokens: int
    cache_read_tokens: int
    cache_hit_ratio: float
    cost_usd: float
    cache_window_risk: bool = False
    error: str = ""

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ===== (sections below added in later tasks) =====

def main() -> None:
    raise NotImplementedError("Built in Task 14")

if __name__ == "__main__":
    main()
