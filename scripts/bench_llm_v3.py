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

# ===== COST =====

def calc_cost(
    cache_write_tokens: int,
    cache_read_tokens: int,
    prompt_total_tokens: int,
    output_tokens: int,
    spec: ModelSpec,
) -> float:
    """Three-segment pricing: regular + cache_write + cache_read + output.

    Anthropic's cache_creation is 1.25× base price (cold write premium),
    cache_read is 0.10× (90% discount). OpenAI has no write premium but
    applies 0.50× cache_read. Groq has no cache (multipliers == 1.0 means
    'no discount' — and cache tokens are always 0 there anyway).
    """
    regular_input = prompt_total_tokens - cache_write_tokens - cache_read_tokens
    if regular_input < 0:
        LOGGER.warning(
            "calc_cost: regular_input < 0 (total=%d write=%d read=%d) — clamp 0",
            prompt_total_tokens, cache_write_tokens, cache_read_tokens,
        )
        regular_input = 0
    cost = regular_input      * spec.input_price_per_1m /  1e6
    cost += cache_write_tokens * spec.input_price_per_1m * spec.cache_write_multiplier / 1e6
    cost += cache_read_tokens  * spec.input_price_per_1m * spec.cache_read_multiplier  / 1e6
    cost += output_tokens      * spec.output_price_per_1m / 1e6
    return cost

# ===== CACHE METRICS =====

def _get(obj: Any, path: str, default: Any = 0) -> Any:
    """Safe attribute/key chain fetch (e.g., 'usage.prompt_tokens_details.cached_tokens')."""
    for part in path.split("."):
        if obj is None:
            return default
        if hasattr(obj, part):
            obj = getattr(obj, part)
        elif isinstance(obj, dict):
            obj = obj.get(part, default)
        else:
            return default
    return obj if obj is not None else default


def extract_cache_metrics(provider: str, response: Any) -> dict[str, int]:
    """Normalize per-provider cache usage into a uniform dict.

    Returns keys:
        cache_write_tokens:   Anthropic only (cache_creation); else 0.
        cache_read_tokens:    provider-specific cache-hit count; 0 if none.
        prompt_total_tokens:  total input tokens (sum of regular + write + read).
        output_tokens:        generated tokens.
    """
    if provider == "anthropic":
        input_tokens = _get(response, "usage.input_tokens", 0)
        write = _get(response, "usage.cache_creation_input_tokens", 0)
        read = _get(response, "usage.cache_read_input_tokens", 0)
        output = _get(response, "usage.output_tokens", 0)
        return {
            "cache_write_tokens": write,
            "cache_read_tokens": read,
            "prompt_total_tokens": input_tokens + write + read,
            "output_tokens": output,
        }

    if provider == "google":
        prompt_total = _get(response, "usage_metadata.prompt_token_count", 0)
        cached = _get(response, "usage_metadata.cached_content_token_count", 0)
        output = _get(response, "usage_metadata.candidates_token_count", 0)
        return {
            "cache_write_tokens": 0,
            "cache_read_tokens": cached,
            "prompt_total_tokens": prompt_total,
            "output_tokens": output,
        }

    if provider == "groq":
        prompt_total = _get(response, "usage.prompt_tokens", 0)
        output = _get(response, "usage.completion_tokens", 0)
        return {
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
            "prompt_total_tokens": prompt_total,
            "output_tokens": output,
        }

    # openai / xai use OpenAI-compat response shape
    prompt_total = _get(response, "usage.prompt_tokens", 0)
    cached = _get(response, "usage.prompt_tokens_details.cached_tokens", 0)
    output = _get(response, "usage.completion_tokens", 0)
    # xAI cache support is undocumented as of 2026-04; warn once if we never see the field
    if provider == "xai":
        _maybe_warn_xai_cache_shape(response)
    return {
        "cache_write_tokens": 0,
        "cache_read_tokens": cached,
        "prompt_total_tokens": prompt_total,
        "output_tokens": output,
    }


_XAI_CACHE_SHAPE_CHECKED = False


def _maybe_warn_xai_cache_shape(response: Any) -> None:
    """Print once on first xAI response whether prompt_tokens_details.cached_tokens is present.

    xAI docs don't explicitly confirm OpenAI-compat cache fields. This banner makes
    the actual behavior visible in the first call output, symmetric with the
    Anthropic cache_control debug print.
    """
    global _XAI_CACHE_SHAPE_CHECKED
    if _XAI_CACHE_SHAPE_CHECKED:
        return
    _XAI_CACHE_SHAPE_CHECKED = True
    details = _get(response, "usage.prompt_tokens_details", None)
    cached = _get(response, "usage.prompt_tokens_details.cached_tokens", None)
    print("=" * 78)
    print("XAI FIRST CALL RESPONSE (DEBUG) — checking cache field availability")
    print(f"  usage.prompt_tokens_details present:       {details is not None}")
    print(f"  usage.prompt_tokens_details.cached_tokens: {cached}")
    if details is None or cached is None:
        print("  ⚠️  xAI does NOT expose OpenAI-compat cache metrics. Treating as no cache.")
    else:
        print(f"  ✓ xAI exposes cache_read_tokens = {cached}. OpenAI-compat confirmed.")
    print("=" * 78, flush=True)

# ===== PROVIDERS =====

MAX_OUTPUT_TOKENS = 512   # Keep small — we measure TTFT, not generation depth
CALL_TIMEOUT_SEC = 90.0


def make_bust_prefix() -> str:
    """Per-task prefix that defeats auto-caching (OpenAI/xAI) across test boundaries.

    Uses 16-hex-char session id (64 bits — collision-free in practice) plus a
    nanosecond timestamp so two calls in the same process always differ. Kept
    deliberately short (<30 cl100k tokens) so it doesn't skew context_size.
    """
    return f"# Session: {uuid4().hex[:16]}\n# Timestamp: {time.time_ns()}\n\n"


@dataclass
class CallSpec:
    """Immutable input to a single API call."""
    model_spec: ModelSpec
    active_model_id: str          # primary_id or a fallback
    bust_prefix: str
    notes: str
    task_name: str

    @property
    def query(self) -> str:
        return TASKS[self.task_name]

    @property
    def system_content(self) -> str:
        return self.bust_prefix + self.notes


_ANTHROPIC_DEBUG_PRINTED = False


async def call_anthropic(cs: CallSpec) -> dict[str, Any]:
    """Anthropic with cache_control on system prompt prefix.

    On first call of the whole process, prints raw usage to stdout for
    cache_control validation (v2 died silently with cache_control syntax errors).
    """
    global _ANTHROPIC_DEBUG_PRINTED
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    system_blocks = [{
        "type": "text",
        "text": cs.system_content,
        "cache_control": {"type": "ephemeral"},
    }]

    t0 = time.perf_counter()
    ttft_ms: float | None = None
    answer_parts: list[str] = []
    final_message = None

    async with client.messages.stream(
        model=cs.active_model_id,
        system=system_blocks,
        messages=[{"role": "user", "content": cs.query}],
        max_tokens=MAX_OUTPUT_TOKENS,
    ) as stream:
        async for chunk in stream.text_stream:
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - t0) * 1000.0
            answer_parts.append(chunk)
        final_message = await stream.get_final_message()

    total_ms = (time.perf_counter() - t0) * 1000.0

    if not _ANTHROPIC_DEBUG_PRINTED:
        _ANTHROPIC_DEBUG_PRINTED = True
        print("=" * 78)
        print(f"ANTHROPIC FIRST CALL RESPONSE (DEBUG) — {cs.active_model_id} / {cs.task_name}")
        print(f"  usage.input_tokens:                 {final_message.usage.input_tokens}")
        print(f"  usage.cache_creation_input_tokens:  "
              f"{getattr(final_message.usage, 'cache_creation_input_tokens', 0)}")
        print(f"  usage.cache_read_input_tokens:      "
              f"{getattr(final_message.usage, 'cache_read_input_tokens', 0)}")
        print(f"  usage.output_tokens:                {final_message.usage.output_tokens}")
        print("  ^ cache_creation > 0 on FIRST cold call means cache_control is working.")
        print("=" * 78, flush=True)

    return {
        "ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
        "total_ms": total_ms,
        "answer": "".join(answer_parts),
        "raw_response": final_message,
    }


async def call_openai_compat(cs: CallSpec, base_url: str, api_key: str) -> dict[str, Any]:
    """Shared entrypoint for OpenAI, xAI (Grok), and Groq (all use OpenAI wire protocol)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    answer_parts: list[str] = []

    stream = await client.chat.completions.create(
        model=cs.active_model_id,
        messages=[
            {"role": "system", "content": cs.system_content},
            {"role": "user", "content": cs.query},
        ],
        max_tokens=MAX_OUTPUT_TOKENS,
        stream=True,
        stream_options={"include_usage": True},  # critical for cache metrics
    )

    final_chunk = None
    async for chunk in stream:
        final_chunk = chunk
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        content = getattr(delta, "content", None)
        if content:
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - t0) * 1000.0
            answer_parts.append(content)

    total_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
        "total_ms": total_ms,
        "answer": "".join(answer_parts),
        "raw_response": final_chunk,
    }


async def call_openai(cs: CallSpec) -> dict[str, Any]:
    return await call_openai_compat(
        cs,
        base_url="https://api.openai.com/v1",
        api_key=os.environ["OPENAI_API_KEY"],
    )


async def call_xai(cs: CallSpec) -> dict[str, Any]:
    return await call_openai_compat(
        cs,
        base_url="https://api.x.ai/v1",
        api_key=os.environ["XAI_API_KEY"],
    )


async def call_groq(cs: CallSpec) -> dict[str, Any]:
    return await call_openai_compat(
        cs,
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ["GROQ_API_KEY"],
    )


async def call_gemini(cs: CallSpec) -> dict[str, Any]:
    """Gemini with inline notes (CachedContent storage fees out-of-scope).

    Google's google-generativeai SDK is sync. We wrap in a thread.
    TTFT via iterating stream chunks.
    """
    import google.generativeai as genai

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(cs.active_model_id)

    def _run() -> dict[str, Any]:
        t0 = time.perf_counter()
        ttft_ms: float | None = None
        answer_parts: list[str] = []

        # Single combined prompt (Gemini treats system + user as content list)
        # Using inline send — CachedContent adds per-cache storage cost + management
        # that's out-of-scope for this benchmark.
        combined = cs.system_content + "\n\n" + cs.query
        stream = model.generate_content(
            combined,
            stream=True,
            generation_config={"max_output_tokens": MAX_OUTPUT_TOKENS},
        )
        for chunk in stream:
            text = getattr(chunk, "text", "") or ""
            if text:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000.0
                answer_parts.append(text)
        # `stream` itself exposes the merged GenerateContentResponse after iteration.
        # usage_metadata lives on the stream object (NOT on individual chunks).
        # resolve() is idempotent and safe to call after iteration completes.
        try:
            stream.resolve()
        except Exception:
            pass

        total_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "ttft_ms": ttft_ms if ttft_ms is not None else total_ms,
            "total_ms": total_ms,
            "answer": "".join(answer_parts),
            "raw_response": stream,  # has .usage_metadata after resolve
        }

    return await asyncio.to_thread(_run)


PROVIDER_DISPATCH: dict[str, Callable[[CallSpec], Awaitable[dict[str, Any]]]] = {
    "anthropic": call_anthropic,
    "openai": call_openai,
    "xai": call_xai,
    "groq": call_groq,
    "google": call_gemini,
}

# ===== EXECUTION =====

class RateLimited(Exception): ...
class FatalAPIError(Exception): ...


def _is_rate_limit(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg or "quota" in msg or "overloaded" in msg


def _is_fatal(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "401" in msg or "403" in msg or "invalid api key" in msg or "400" in msg


async def call_api_with_retry(
    cs: CallSpec,
    cache_state: str,
    run_idx: int,
    nominal_tokens: int,
    model_is_fallback: bool,
) -> CallResult:
    """Run one API call with exponential backoff on rate limits; build CallResult."""
    dispatch = PROVIDER_DISPATCH[cs.model_spec.provider]

    last_err: str = ""
    for attempt in range(4):  # 1 + 3 retries
        try:
            raw = await asyncio.wait_for(dispatch(cs), timeout=CALL_TIMEOUT_SEC)
            metrics = extract_cache_metrics(cs.model_spec.provider, raw["raw_response"])
            cost = calc_cost(
                cache_write_tokens=metrics["cache_write_tokens"],
                cache_read_tokens=metrics["cache_read_tokens"],
                prompt_total_tokens=metrics["prompt_total_tokens"],
                output_tokens=metrics["output_tokens"],
                spec=cs.model_spec,
            )
            hit_ratio = (
                metrics["cache_read_tokens"] / metrics["prompt_total_tokens"]
                if metrics["prompt_total_tokens"] > 0 else 0.0
            )
            answer = raw["answer"][:500]
            answer_correct = verify_recall(raw["answer"]) if cs.task_name == "recall" else None
            tps = (
                metrics["output_tokens"] / (raw["total_ms"] / 1000.0)
                if raw["total_ms"] > 0 else 0.0
            )
            return CallResult(
                timestamp=_utcnow_iso(),
                model=cs.active_model_id,
                model_is_fallback=model_is_fallback,
                provider=cs.model_spec.provider,
                nominal_tokens_cl100k=nominal_tokens,
                actual_input_tokens_api=metrics["prompt_total_tokens"],
                task=cs.task_name,
                cache_state=cache_state,
                run_idx=run_idx,
                ttft_ms=raw["ttft_ms"],
                total_ms=raw["total_ms"],
                output_tokens=metrics["output_tokens"],
                tokens_per_second=tps,
                answer=answer,
                answer_correct=answer_correct,
                cache_actually_hit=metrics["cache_read_tokens"] > 0,
                cache_write_tokens=metrics["cache_write_tokens"],
                cache_read_tokens=metrics["cache_read_tokens"],
                cache_hit_ratio=hit_ratio,
                cost_usd=cost,
            )
        except asyncio.TimeoutError:
            last_err = "timeout"
            break  # don't retry timeouts
        except Exception as e:  # noqa: BLE001
            if _is_fatal(e):
                last_err = f"fatal: {type(e).__name__}: {str(e)[:200]}"
                break
            if _is_rate_limit(e):
                wait = 2 ** attempt
                LOGGER.warning(
                    "Rate limit on %s (%s), wait %ds, attempt %d",
                    cs.active_model_id, cs.task_name, wait, attempt + 1,
                )
                await asyncio.sleep(wait)
                last_err = f"ratelimit: {str(e)[:200]}"
                continue
            # transient 5xx
            await asyncio.sleep(2 ** attempt)
            last_err = f"{type(e).__name__}: {str(e)[:200]}"

    return CallResult(
        timestamp=_utcnow_iso(),
        model=cs.active_model_id,
        model_is_fallback=model_is_fallback,
        provider=cs.model_spec.provider,
        nominal_tokens_cl100k=nominal_tokens,
        actual_input_tokens_api=0,
        task=cs.task_name,
        cache_state=cache_state,
        run_idx=run_idx,
        ttft_ms=-1.0,
        total_ms=-1.0,
        output_tokens=0,
        tokens_per_second=0.0,
        answer="",
        answer_correct=None,
        cache_actually_hit=False,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cache_hit_ratio=0.0,
        cost_usd=0.0,
        error=last_err or "unknown",
    )


# ===== WARMUP / ACTIVE SPEC RESOLUTION =====

API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "google":    "GEMINI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "xai":       "XAI_API_KEY",
}


def _provider_has_key(provider: str) -> bool:
    primary = os.environ.get(API_KEY_ENV[provider])
    fallback = os.environ.get("GOOGLE_API_KEY") if provider == "google" else None
    return bool(primary or fallback)


def _ensure_google_key_env():
    """google-generativeai reads GEMINI_API_KEY by default; accept GOOGLE_API_KEY too."""
    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]


async def warmup_one(spec: ModelSpec) -> tuple[str, bool] | None:
    """Return (active_model_id, is_fallback) if any ID works, else None.

    Uses a minimal isolated payload — pure user message, NO system prompt —
    to guarantee zero-byte overlap with real test prefixes. This prevents
    OpenAI/xAI auto-prefix cache from polluting the first cold call.
    """
    candidates: list[tuple[str, bool]] = [(spec.primary_id, False)]
    candidates.extend((fid, True) for fid in spec.fallback_ids)

    for mid, is_fb in candidates:
        try:
            if spec.provider == "anthropic":
                from anthropic import AsyncAnthropic
                client = AsyncAnthropic()
                await client.messages.create(
                    model=mid,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "Just say: OK"}],
                )
            elif spec.provider == "google":
                _ensure_google_key_env()
                import google.generativeai as genai
                genai.configure(api_key=os.environ["GEMINI_API_KEY"])
                await asyncio.to_thread(
                    lambda: genai.GenerativeModel(mid).generate_content(
                        "Just say: OK",
                        generation_config={"max_output_tokens": 5},
                    )
                )
            else:  # openai-compat: openai, xai, groq
                from openai import AsyncOpenAI
                base_urls = {
                    "openai": "https://api.openai.com/v1",
                    "xai":    "https://api.x.ai/v1",
                    "groq":   "https://api.groq.com/openai/v1",
                }
                client = AsyncOpenAI(
                    base_url=base_urls[spec.provider],
                    api_key=os.environ[API_KEY_ENV[spec.provider]],
                )
                await client.chat.completions.create(
                    model=mid,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "Just say: OK"}],
                )
            return (mid, is_fb)
        except Exception as e:  # noqa: BLE001
            LOGGER.warning("warmup failed for %s/%s: %s", spec.provider, mid, str(e)[:120])
            continue
    return None


@dataclass
class ActiveSpec:
    model_spec: ModelSpec
    active_model_id: str
    is_fallback: bool


async def resolve_active_specs() -> list[ActiveSpec]:
    """Check keys, warmup each model, return the list with working IDs."""
    _ensure_google_key_env()
    specs_to_try = [s for s in MODEL_CATALOG if _provider_has_key(s.provider)]
    skipped = [s for s in MODEL_CATALOG if not _provider_has_key(s.provider)]
    for s in skipped:
        print(f"✗ {s.provider}/{s.primary_id} — missing {API_KEY_ENV[s.provider]}")

    active: list[ActiveSpec] = []
    results = await asyncio.gather(*(warmup_one(s) for s in specs_to_try))
    for spec, result in zip(specs_to_try, results):
        if result is None:
            print(f"✗ {spec.provider}/{spec.primary_id} — warmup exhausted all fallbacks")
            continue
        mid, is_fb = result
        marker = "↪" if is_fb else "✓"
        print(f"{marker} {spec.provider}/{mid}" + (" (fallback)" if is_fb else ""))
        active.append(ActiveSpec(spec, mid, is_fb))
    return active


CACHE_WINDOW_RISK_SEC = 100.0  # 3× Anthropic's 300s TTL, very defensive


async def run_one_mc_block(
    active: ActiveSpec,
    ctx_size: int,
    notes: str,
    sem: asyncio.Semaphore,
    task_names: tuple[str, ...] = ("simple", "recall", "synthesis"),
    progress_cb: Callable[[CallResult], None] | None = None,
) -> list[CallResult]:
    """Run len(task_names) tasks × (1 cold + 2 warm) calls for one (model, context).

    Full mode: 3 tasks × 3 calls = 9 calls.
    --decision mode: 1 task × 3 calls = 3 calls (saves 3× cost).
    """
    all_results: list[CallResult] = []
    async with sem:  # per-provider serialize
        for task_name in task_names:
            bust_prefix = make_bust_prefix()
            cs = CallSpec(
                model_spec=active.model_spec,
                active_model_id=active.active_model_id,
                bust_prefix=bust_prefix,
                notes=notes,
                task_name=task_name,
            )
            t_start = time.monotonic()
            task_results: list[CallResult] = []
            for run_idx in range(3):
                cache_state = "cold" if run_idx == 0 else "warm"
                r = await call_api_with_retry(
                    cs, cache_state, run_idx, ctx_size, active.is_fallback
                )
                task_results.append(r)
                if progress_cb:
                    progress_cb(r)
            t_elapsed = time.monotonic() - t_start
            cache_window_risk = t_elapsed > CACHE_WINDOW_RISK_SEC
            if cache_window_risk:
                LOGGER.warning(
                    "%s ctx=%d task=%s took %.1fs (>%.0fs) — warm cache may have expired",
                    active.active_model_id, ctx_size, task_name, t_elapsed,
                    CACHE_WINDOW_RISK_SEC,
                )
            for r in task_results:
                r.cache_window_risk = cache_window_risk
            all_results.extend(task_results)
    return all_results


async def run_all_mc_blocks(
    active_specs: list[ActiveSpec],
    context_sizes: list[int],
    fixtures_dir: Path,
    task_names: tuple[str, ...] = ("simple", "recall", "synthesis"),
    progress_cb: Callable[[CallResult], None] | None = None,
) -> list[CallResult]:
    """Cross-provider concurrent; per-provider Semaphore(1) for atomicity."""
    provider_sems: dict[str, asyncio.Semaphore] = {
        p: asyncio.Semaphore(1) for p in {a.model_spec.provider for a in active_specs}
    }
    # Pre-load all fixtures (avoids race on first-time generation across coroutines)
    notes_cache = {c: load_notes(c, fixtures_dir) for c in context_sizes}

    coros = []
    for active in active_specs:
        for ctx_size in context_sizes:
            coros.append(run_one_mc_block(
                active,
                ctx_size,
                notes_cache[ctx_size],
                provider_sems[active.model_spec.provider],
                task_names=task_names,
                progress_cb=progress_cb,
            ))
    lists = await asyncio.gather(*coros, return_exceptions=False)
    flat: list[CallResult] = []
    for sub in lists:
        flat.extend(sub)
    return flat


# ===== OUTPUT =====

CSV_FIELDS = [
    "timestamp", "model", "model_is_fallback", "provider",
    "nominal_tokens_cl100k", "actual_input_tokens_api",
    "task", "cache_state", "run_idx",
    "ttft_ms", "total_ms", "output_tokens", "tokens_per_second",
    "answer", "answer_correct",
    "cache_actually_hit", "cache_write_tokens", "cache_read_tokens", "cache_hit_ratio",
    "cost_usd", "cache_window_risk", "error",
]


def write_csv(results: list[CallResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "results.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in results:
            row = asdict(r)
            row["answer"] = row["answer"].replace("\n", " ")[:500]
            w.writerow(row)
    return path


def _fmt_ms(x: float) -> str:
    return f"{x:.0f}" if x >= 0 else "ERR"


def render_summary_md(
    results: list[CallResult],
    output_dir: Path,
    run_args: dict[str, Any],
) -> Path:
    """Five-table markdown report. Tables pivot by nominal_tokens_cl100k."""
    path = output_dir / "summary.md"
    # Group by (provider, model)
    model_keys: list[tuple[str, str]] = []
    for r in results:
        k = (r.provider, r.model)
        if k not in model_keys:
            model_keys.append(k)

    context_sizes = sorted({r.nominal_tokens_cl100k for r in results})

    def model_label(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    lines = [
        f"# Bench LLM v3 — {run_args['timestamp']}",
        "",
        f"**Mode:** `{run_args['mode']}` · **Total calls:** {len(results)} · "
        f"**Errors:** {sum(1 for r in results if r.error)} · "
        f"**Total cost:** ${sum(r.cost_usd for r in results):.2f}",
        "",
        f"**Context buckets** (nominal cl100k tokens): {context_sizes}",
        "",
    ]

    # ===== Table 1: Warm TTFT by Context =====
    lines += [
        "## Table 1 — TTFT × Context (warm cache, avg ms, n=2)",
        "",
        "| Model | " + " | ".join(f"{c//1000}k" for c in context_sizes) + " |",
        "|-------|" + "|".join(["---"] * len(context_sizes)) + "|",
    ]
    for provider, model in model_keys:
        row = [model_label(provider, model)]
        for c in context_sizes:
            subset = [r for r in results
                      if r.provider == provider and r.model == model and r.nominal_tokens_cl100k == c]
            warm_ttfts = [r.ttft_ms for r in subset if r.cache_state == "warm" and r.ttft_ms >= 0]
            if not warm_ttfts:
                row.append("—")
            else:
                avg = sum(warm_ttfts) / len(warm_ttfts)
                # stability check
                unstable = len(warm_ttfts) >= 2 and (max(warm_ttfts) - min(warm_ttfts)) / avg > 0.3
                marker = " *unstable*" if unstable else ""
                row.append(f"{avg:.0f}{marker}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # ===== Table 2: Recall accuracy =====
    lines += [
        "## Table 2 — Recall 准确率 × Context (strict double-keyword)",
        "",
        "| Model | " + " | ".join(f"{c//1000}k" for c in context_sizes) + " | Overall |",
        "|-------|" + "|".join(["---"] * (len(context_sizes) + 1)) + "|",
    ]
    for provider, model in model_keys:
        row = [model_label(provider, model)]
        total_correct = total_attempts = 0
        for c in context_sizes:
            subset = [r for r in results
                      if r.provider == provider and r.model == model
                      and r.nominal_tokens_cl100k == c and r.task == "recall"
                      and r.answer_correct is not None]
            if not subset:
                row.append("—")
                continue
            correct = sum(1 for r in subset if r.answer_correct)
            total_correct += correct
            total_attempts += len(subset)
            row.append(f"{correct}/{len(subset)}")
        overall = f"{total_correct}/{total_attempts}" if total_attempts else "—"
        row.append(overall)
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # ===== Table 3: Cold vs Warm TTFT at largest context =====
    largest = max(context_sizes)
    lines += [
        f"## Table 3 — Cold vs Warm TTFT @ {largest//1000}k (ms)",
        "",
        "| Model | Cold | Warm (avg, n=2) | Speedup |",
        "|-------|------|------|------|",
    ]
    for provider, model in model_keys:
        subset = [r for r in results
                  if r.provider == provider and r.model == model and r.nominal_tokens_cl100k == largest]
        cold = next((r.ttft_ms for r in subset if r.cache_state == "cold" and r.ttft_ms >= 0), -1.0)
        warms = [r.ttft_ms for r in subset if r.cache_state == "warm" and r.ttft_ms >= 0]
        warm = sum(warms) / len(warms) if warms else -1.0
        speedup = f"{cold/warm:.2f}×" if cold > 0 and warm > 0 else "—"
        lines.append(f"| {model_label(provider, model)} | {_fmt_ms(cold)} | {_fmt_ms(warm)} | {speedup} |")
    lines.append("")

    # ===== Table 4: Real cache hit rate =====
    lines += [
        "## Table 4 — 真实 Cache 命中率 (揭露黑箱)",
        "",
        "| Model | 官方声明 | 实测 read ratio (warm) | write_tokens (cold) | read_tokens (warm avg) |",
        "|-------|---------|----------------------|---------------------|----------------------|",
    ]
    for provider, model in model_keys:
        subset = [r for r in results
                  if r.provider == provider and r.model == model and r.nominal_tokens_cl100k == largest]
        warm_rows = [r for r in subset if r.cache_state == "warm" and not r.error]
        cold_rows = [r for r in subset if r.cache_state == "cold" and not r.error]
        if not warm_rows:
            declared = "—"
            ratio = "—"
            w_tok = "—"
            r_tok = "—"
        else:
            declared = {
                "anthropic": "显式",
                "openai": "自动",
                "xai": "自动",
                "google": "显式",
                "groq": "无",
            }.get(provider, "?")
            avg_ratio = sum(r.cache_hit_ratio for r in warm_rows) / len(warm_rows)
            ratio = f"{avg_ratio*100:.1f}%"
            w_tok = str(sum(r.cache_write_tokens for r in cold_rows) // max(len(cold_rows), 1))
            r_tok = str(sum(r.cache_read_tokens for r in warm_rows) // max(len(warm_rows), 1))
        lines.append(f"| {model_label(provider, model)} | {declared} | {ratio} | {w_tok} | {r_tok} |")
    lines.append("")

    # ===== Table 5: Cost comparison =====
    lines += [
        "## Table 5 — 成本对比 ($/100 × 30k 对话, 含 1 cold + 2 warm)",
        "",
        "| Model | $/100 calls @ 30k | 含 cache 节省估 |",
        "|-------|------|------|",
    ]
    target_ctx = 30000 if 30000 in context_sizes else largest
    for provider, model in model_keys:
        subset = [r for r in results
                  if r.provider == provider and r.model == model
                  and r.nominal_tokens_cl100k == target_ctx and not r.error]
        if not subset:
            lines.append(f"| {model_label(provider, model)} | — | — |")
            continue
        avg_cost_per_call = sum(r.cost_usd for r in subset) / len(subset)
        cost_per_100 = avg_cost_per_call * 100
        saving_note = "有 cache" if any(r.cache_read_tokens > 0 for r in subset) else "无 cache"
        lines.append(f"| {model_label(provider, model)} | ${cost_per_100:.2f} | {saving_note} |")
    lines.append("")

    # ===== Footnotes =====
    risky = [r for r in results if r.cache_window_risk]
    if risky:
        lines += [
            "## Notes",
            "",
            f"- `cache_window_risk=True` fired for {len(risky)} rows "
            f"(task block > {CACHE_WINDOW_RISK_SEC:.0f}s). Filter in analysis.",
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_run_meta(
    results: list[CallResult],
    output_dir: Path,
    run_args: dict[str, Any],
) -> Path:
    path = output_dir / "run_meta.json"
    meta = {
        "mode": run_args["mode"],
        "timestamp": run_args["timestamp"],
        "total_calls": len(results),
        "errors_total": sum(1 for r in results if r.error),
        "cost_usd_total": round(sum(r.cost_usd for r in results), 4),
        "cache_window_risk_count": sum(1 for r in results if r.cache_window_risk),
        "elapsed_sec": run_args.get("elapsed_sec", -1),
        "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
        "active_models": run_args.get("active_models", []),
        "args": run_args.get("raw_args", {}),
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def render_chart_html(results: list[CallResult], output_dir: Path) -> Path | None:
    """Optional plotly bar chart: TTFT × context × model, faceted by cache_state."""
    try:
        import plotly.express as px
        import pandas as pd
    except ImportError:
        LOGGER.warning("plotly/pandas missing, skipping chart.html")
        return None
    df = pd.DataFrame([asdict(r) for r in results if r.ttft_ms > 0])
    if df.empty:
        return None
    df["model_label"] = df["provider"] + "/" + df["model"]
    df["ctx_k"] = (df["nominal_tokens_cl100k"] / 1000).astype(int).astype(str) + "k"
    fig = px.bar(
        df,
        x="ctx_k",
        y="ttft_ms",
        color="model_label",
        facet_col="cache_state",
        barmode="group",
        title="TTFT (ms) — faceted by cache_state, grouped by model",
    )
    path = output_dir / "chart.html"
    fig.write_html(path)
    return path


# ===== CLI =====

CONTEXT_SIZES_FULL = [2000, 10000, 30000, 100000]
CONTEXT_SIZES_QUICK = [2000, 10000]
CONTEXT_SIZES_DEEP = [2000, 10000, 30000, 100000, 200000]
DECISION_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001",
                   "grok-4-1-fast-non-reasoning", "gpt-5"}
QUICK_MODELS = {"claude-sonnet-4-6", "claude-haiku-4-5-20251001"}


@dataclass
class RunPlan:
    mode: str
    models: list[ModelSpec]
    context_sizes: list[int]
    tasks: tuple[str, ...]


def resolve_run_plan(args: argparse.Namespace) -> RunPlan:
    """Translate CLI args into the effective matrix."""
    all_models = list(MODEL_CATALOG)
    if args.model:
        target = [s for s in all_models if s.primary_id == args.model
                  or args.model in s.fallback_ids]
        if not target:
            raise SystemExit(f"Model '{args.model}' not in catalog.")
        return RunPlan("model", target, CONTEXT_SIZES_FULL, ("simple", "recall", "synthesis"))
    if args.quick:
        models = [s for s in all_models if s.primary_id in QUICK_MODELS]
        return RunPlan("quick", models, CONTEXT_SIZES_QUICK, ("simple", "recall", "synthesis"))
    if args.decision:
        models = [s for s in all_models if s.primary_id in DECISION_MODELS]
        return RunPlan("decision", models, [30000], ("recall",))
    if args.deep:
        return RunPlan("deep", all_models, CONTEXT_SIZES_DEEP, ("simple", "recall", "synthesis"))
    # default / --standard
    return RunPlan("standard", all_models, CONTEXT_SIZES_FULL, ("simple", "recall", "synthesis"))


def check_smoke_gate(mode: str, bench_results_root: Path, force: bool) -> None:
    """For --standard and --deep: refuse if no prior successful --quick recorded."""
    if mode in ("quick", "decision", "model") or force:
        return
    if not bench_results_root.exists():
        raise SystemExit(
            "\n❌ Smoke gate: no prior --quick found. Run:\n"
            "   uv run python scripts/bench_llm_v3.py --quick\n"
            "first, or pass --force-standard to override.\n"
        )
    for meta in bench_results_root.glob("*/run_meta.json"):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            # Threshold 5: 36 calls with 3-4 transient network failures is normal
            if m.get("mode") == "quick" and m.get("errors_total", 99) <= 5:
                return
        except Exception:
            continue
    raise SystemExit(
        "\n❌ Smoke gate: no successful --quick in bench_results/ "
        "(need errors_total ≤ 5).\n"
        "Run `uv run python scripts/bench_llm_v3.py --quick` first,\n"
        "or pass --force-standard to override.\n"
    )


def _estimate_cost(plan: RunPlan, n_active: int) -> float:
    """Rough ballpark: avg 35k tokens/call, 3-segment cache, avg price $2/1M."""
    n_calls = n_active * len(plan.context_sizes) * len(plan.tasks) * 3
    # 1 cold @ ~35k full price + 2 warm @ ~35k × 0.2 (mixed cache behavior)
    avg_cost_per_call = (35_000 * 2.0 / 1e6) * 0.5  # 50% effective price with caching
    return n_calls * avg_cost_per_call


async def _main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    plan = resolve_run_plan(args)
    fixtures_dir = Path(args.fixtures_dir)
    results_root = Path("bench_results")

    if not args.dry_run:
        check_smoke_gate(plan.mode, results_root, args.force_standard)

    # Pre-generate fixtures so --dry-run can verify them
    for c in plan.context_sizes:
        load_notes(c, fixtures_dir)
    print(f"▸ Fixtures ready: {fixtures_dir} ({len(plan.context_sizes)} files)")

    # Detect active providers (by env key)
    active_providers = {s.provider for s in plan.models if _provider_has_key(s.provider)}
    active_specs_candidates = [s for s in plan.models if s.provider in active_providers]

    # In dry-run mode: print plan + skip warmup + skip API calls
    if args.dry_run:
        n_active = len(active_specs_candidates)
        n_calls = n_active * len(plan.context_sizes) * len(plan.tasks) * 3
        print()
        print(f"[DRY RUN] Mode: --{plan.mode}")
        print(f"[DRY RUN] Active providers: "
              f"{sorted(active_providers)}")
        print(f"[DRY RUN] Skipped (no key): "
              f"{sorted({s.provider for s in plan.models} - active_providers)}")
        print(f"[DRY RUN] Context sizes: {plan.context_sizes}")
        print(f"[DRY RUN] Tasks: {plan.tasks}")
        print(f"[DRY RUN] Active MCTs: "
              f"{n_active}×{len(plan.context_sizes)}×{len(plan.tasks)} = "
              f"{n_active * len(plan.context_sizes) * len(plan.tasks)}")
        print(f"[DRY RUN] Total API calls: {n_calls}")
        print(f"[DRY RUN] Estimated cost: ~${_estimate_cost(plan, n_active):.2f}")
        print(f"[DRY RUN] Wall-clock estimate: "
              f"{max(5, n_calls * 3 // 60)}-{max(10, n_calls * 6 // 60)} min")
        print(f"[DRY RUN] cache_window_risk threshold: {CACHE_WINDOW_RISK_SEC:.0f}s per task block")
        return

    # Warmup
    print()
    print("▸ Warmup (isolated payload, zero prefix pollution):")
    active_specs = []
    # Filter MODEL_CATALOG to the plan.models list but only for providers with keys
    specs_to_warmup = [s for s in plan.models if s.provider in active_providers]
    _ensure_google_key_env()
    warmup_results = await asyncio.gather(*(warmup_one(s) for s in specs_to_warmup))
    for spec, result in zip(specs_to_warmup, warmup_results):
        if result is None:
            print(f"  ✗ {spec.provider}/{spec.primary_id} — exhausted fallbacks")
            continue
        mid, is_fb = result
        marker = "↪" if is_fb else "✓"
        print(f"  {marker} {spec.provider}/{mid}" + (" (fallback)" if is_fb else ""))
        active_specs.append(ActiveSpec(spec, mid, is_fb))

    if not active_specs:
        raise SystemExit("No models survived warmup. Aborting.")

    print()
    print(f"▸ Running --{plan.mode}: "
          f"{len(active_specs)} × {len(plan.context_sizes)} × {len(plan.tasks)} "
          f"= {len(active_specs) * len(plan.context_sizes) * len(plan.tasks)} MCTs, "
          f"{len(active_specs) * len(plan.context_sizes) * len(plan.tasks) * 3} total calls")

    from tqdm.asyncio import tqdm as async_tqdm  # local import for --dry-run speed
    # total = specs × contexts × tasks × 3 (cold + 2 warm per task)
    expected_calls = len(active_specs) * len(plan.context_sizes) * len(plan.tasks) * 3
    pbar = async_tqdm(total=expected_calls, desc="calls", unit="call")
    running_cost = [0.0]
    error_count = [0]

    def progress_cb(r: CallResult) -> None:
        pbar.update(1)
        running_cost[0] += r.cost_usd
        if r.error:
            error_count[0] += 1
        pbar.set_postfix(cost=f"${running_cost[0]:.2f}", errors=error_count[0])

    t0 = time.monotonic()
    results = await run_all_mc_blocks(
        active_specs,
        plan.context_sizes,
        fixtures_dir,
        task_names=plan.tasks,
        progress_cb=progress_cb,
    )
    pbar.close()
    elapsed = time.monotonic() - t0

    # Output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_dir = Path(args.output_dir) if args.output_dir else (results_root / timestamp)

    csv_path = write_csv(results, output_dir)
    summary_path = render_summary_md(
        results, output_dir,
        run_args={
            "mode": plan.mode,
            "timestamp": timestamp,
            "elapsed_sec": elapsed,
            "active_models": [a.active_model_id for a in active_specs],
            "raw_args": vars(args),
        },
    )
    meta_path = write_run_meta(
        results, output_dir,
        run_args={
            "mode": plan.mode,
            "timestamp": timestamp,
            "elapsed_sec": elapsed,
            "active_models": [a.active_model_id for a in active_specs],
            "raw_args": {k: v for k, v in vars(args).items() if not callable(v)},
        },
    )
    chart_path = render_chart_html(results, output_dir) if args.with_chart else None

    errors = sum(1 for r in results if r.error)
    print()
    print(f"▸ Done in {elapsed/60:.1f} min "
          f"({len(results)} calls, {errors} errors, "
          f"${sum(r.cost_usd for r in results):.2f})")
    print(f"  CSV:     {csv_path}")
    print(f"  Summary: {summary_path}")
    print(f"  Meta:    {meta_path}")
    if chart_path:
        print(f"  Chart:   {chart_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bench_llm_v3",
        description="Jarvis LLM selection benchmark.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--quick", action="store_true")
    mode_group.add_argument("--standard", action="store_true")
    mode_group.add_argument("--deep", action="store_true")
    mode_group.add_argument("--decision", action="store_true")
    mode_group.add_argument("--model", type=str, help="Run a single model by id")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate fixtures + print plan, no API calls")
    parser.add_argument("--with-chart", action="store_true")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--fixtures-dir", type=str, default="bench_fixtures")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip --deep cost confirmation")
    parser.add_argument("--force-standard", action="store_true",
                        help="Skip smoke gate check (use with care)")
    args = parser.parse_args()

    if args.deep and not args.no_confirm and not args.dry_run:
        print("--deep will cost ~$12-18 and take 2-4 hours. Continue? [y/N] ", end="")
        if input().strip().lower() != "y":
            raise SystemExit("Cancelled.")

    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
