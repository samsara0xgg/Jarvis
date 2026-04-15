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
    "今天上 gym 练腿，DOMS 明显",
    "路过 Kitsilano 海滩，日落很好看",
    "在 Commercial Dr 的 Prado Cafe 工作了两小时",
    "Prairie Dog 的书架又空了一半",
    "跟妈妈视频 45min，她让我多休息",
    "在 Starbucks 买了最后一杯 flat white",
    "整理了这周的 Linear tickets, 三个漏掉的",
    "和 Kevin 一起吃日料, Minami 的招牌卷",
    "Jarvis 在 RPi 上跑稳了 2 小时",
    "换了 ergonomic 键盘, Keychron Q1",
    "去了一趟 IKEA 买显示器支架",
    "下午 4 点做了 latte art 练习",
    "在 Bard on the Beach 看《哈姆雷特》",
    "去打了 booster shot, 左臂酸一天",
    "研究了 OpenWakeWord 的模型格式",
    "Steelhead Coffee 的 anaerobic 处理豆不错",
    "家里的 Hue 灯又掉线了，重配网",
    "和 Allen (同名) 讨论了 ECAPA 声纹模型",
    "Mount Pleasant 的周四 market",
    "看了一篇 Claude prompt caching 的新论文",
]
assert len(OBSERVATION_TEMPLATES) >= 30  # 启动时 sanity check

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

import tiktoken

# ===== FIXTURES =====

_SIZE_TO_STEM = {
    2000: "2k",
    10000: "10k",
    30000: "30k",
    100000: "100k",
    200000: "200k",
}


def _observation_line(rng: random.Random, day: int) -> str:
    emoji = rng.choice(EMOJIS)
    hh = rng.randint(6, 23)
    mm = rng.randint(0, 59)
    verb = rng.choice(VERBS)
    content = rng.choice(OBSERVATION_TEMPLATES)
    dd = 1 + (day % 13)  # 2026-04-01..2026-04-13 cycling
    return f"* {emoji} ({hh:02d}:{mm:02d}) Allen {verb}: {content}. (meaning 2026-04-{dd:02d})"


def generate_fake_notes(target_tokens: int, seed: int = 42) -> str:
    """Deterministic Chinese observation notes with needle + distractor inserted."""
    rng = random.Random(seed + target_tokens)
    enc = tiktoken.get_encoding("cl100k_base")

    # Generate filler lines until approaching target.
    # Budget leaves ~150 token headroom for needle (~59) + distractor (~37) +
    # the last filler line we appended after the break-check (~30-40 tokens).
    lines: list[str] = []
    budget = target_tokens - 150
    for day in range(10_000):  # upper bound safety
        if len(enc.encode("\n".join(lines))) >= budget:
            break
        lines.append(_observation_line(rng, day))

    # Insert needle + distractor
    needle_pos = int(len(lines) * (0.5 + rng.uniform(-0.04, 0.04)))
    gap = rng.randint(10, 20)
    distractor_pos = max(1, needle_pos - gap)

    # Insert distractor first (earlier index), so needle position stays correct
    lines.insert(distractor_pos, DISTRACTOR_LINE)
    lines.insert(needle_pos + 1, NEEDLE_LINE)

    return "\n".join(lines)


def load_notes(target_tokens: int, fixtures_dir: Path) -> str:
    """Load fixture from disk; generate + cache on first access.

    Fixed-on-disk strategy ensures byte-level identical content across runs,
    so Anthropic cache can hit across process restarts.
    """
    stem = _SIZE_TO_STEM.get(target_tokens)
    if stem is None:
        raise ValueError(f"Unsupported context size: {target_tokens}")
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    path = fixtures_dir / f"fake_notes_{stem}.txt"
    if not path.exists():
        content = generate_fake_notes(target_tokens)
        path.write_text(content, encoding="utf-8")
    return path.read_text(encoding="utf-8")


def verify_recall(answer: str) -> bool:
    """Strict double-keyword check for recall correctness.

    Requires "拿铁" (the drink type) AND a brand keyword (Revolver or 耶加).
    This filters out false positives like "Allen 可能喜欢拿铁" (guess-based hits).
    Distractor-only answers ("美式") are explicitly rejected.
    """
    has_type = "拿铁" in answer
    has_brand = "Revolver" in answer or "耶加" in answer
    has_needle = has_type and has_brand
    false_positive = "美式" in answer and not has_needle
    return has_needle and not false_positive

# ===== (sections below added in later tasks) =====

def main() -> None:
    raise NotImplementedError("Built in Task 14")

if __name__ == "__main__":
    main()
