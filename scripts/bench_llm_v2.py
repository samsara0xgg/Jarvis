#!/usr/bin/env -S uv run --
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "anthropic>=0.50",
#     "openai>=1.50",
#     "tiktoken>=0.8",
#     "tqdm>=4.66",
#     "plotly>=5.24",
# ]
# ///
"""bench_llm_v2.py — Jarvis 模型选型基准测试 (TTFT + 质量 + 成本).

矩阵 (full):
    8 model × 4 context × 3 cache_state × 3 task × 3 runs  (median)
    ≈ 864 measured calls (+ warmup) → ~45 min 含 6min stale 等待

SDK 策略:
    Anthropic  → anthropic
    OpenAI     → openai
    Gemini     → openai (走 `/v1beta/openai` 兼容层, 与 v1 一致)
    xAI Grok   → openai (base_url 切到 api.x.ai)
    Groq       → openai (base_url 切到 api.groq.com)

用法:
    uv run python scripts/bench_llm_v2.py --quick        # claude+gpt, 2k+10k
    uv run python scripts/bench_llm_v2.py --full         # 全量
    uv run python scripts/bench_llm_v2.py --model sonnet # 单模型
    uv run python scripts/bench_llm_v2.py --full --no-stale  # 跳过 6min stale

注意:
    1. Gemini 未使用显式 context caching (32k 起步); warm/stale 与 cold 等价.
    2. Anthropic 用 system block + cache_control=ephemeral.
    3. OpenAI-compat 由后端自动缓存, 读 usage.prompt_tokens_details.cached_tokens.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tiktoken
from tqdm import tqdm

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LOG = logging.getLogger("bench")

MODELS: list[tuple[str, str]] = [
    ("anthropic", "claude-opus-4-6"),
    ("anthropic", "claude-sonnet-4-6"),
    ("anthropic", "claude-haiku-4-5"),
    ("openai", "gpt-5"),
    ("openai", "gpt-5-mini"),
    ("google", "gemini-3-pro-preview"),
    ("xai", "grok-4-1-fast-non-reasoning"),
    ("groq", "llama-3.3-70b-versatile"),
]

CONTEXT_SIZES = [2_000, 10_000, 30_000, 100_000]
CACHE_STATES = ["cold", "warm", "stale"]

TASKS: dict[str, str] = {
    "simple": "今天温哥华天气怎么样? 一句话回答.",
    "recall": "根据以上观察记录, Allen 最喜欢喝的咖啡是什么? 只回答咖啡名称, 一个词.",
    "synthesis": "根据以上观察记录, Allen 最近一个月状态如何? 给出总结和 3 条具体建议.",
}

CANARY_FACT = "Allen 最喜欢喝拿铁咖啡"
CANARY_KEYWORDS = ["拿铁", "latte"]

# USD per 1M tokens (input, output). 部分定价需用户核实 (标 TODO).
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5": (10.00, 30.00),            # TODO verify
    "gpt-5-mini": (0.25, 2.00),         # TODO verify
    "gemini-3-pro-preview": (1.25, 10.00),  # TODO verify
    "grok-4-1-fast-non-reasoning": (0.30, 1.50),  # TODO verify
    "llama-3.3-70b-versatile": (0.59, 0.79),
}

STALE_WAIT_SECONDS = 360
MAX_OUTPUT_TOKENS = 300
WARMUP_CALLS = 1

OUTPUT_DIR = Path(__file__).parent / "bench_results"

# --------------------------------------------------------------------------
# Utils
# --------------------------------------------------------------------------

_ENC = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


def now_ms() -> float:
    return time.perf_counter() * 1000


# --------------------------------------------------------------------------
# Fake OM notes generator
# --------------------------------------------------------------------------

_TOPICS = [
    "明天的天气", "最近的工作进度", "咖啡豆选择", "RPi5 的温度",
    "音频延迟问题", "中文 ASR 的准确率", "今天的会议", "系统测试结果",
    "新买的耳机", "Jarvis 的语音克隆", "英语口语练习", "本周的跑步计划",
    "日本料理", "周末去哪玩", "Claude Code 的新功能", "MCP 的使用场景",
    "LLM 成本优化", "MiniMax TTS 的情绪", "唤醒词识别率", "voiceprint 绑定",
]
_ACTIONS = [
    "在喝咖啡", "在看代码", "在测试 TTS 延迟", "在调 VAD",
    "在打电话", "在准备晚饭", "在跑步", "在冥想",
    "在看《三体》", "在写日志", "在编辑 yaml", "在重启 Pi",
]
_ENV = [
    "客厅安静, 光线柔和", "书房稍暗, 空调 22°C", "厨房有咖啡香",
    "阳台下雨", "桌面有点乱", "音响放轻音乐", "RPi 有点热, 风扇在转",
]
_WORK = [
    "优化 ASR pipeline", "重构 personality", "写 system test",
    "debug 全双工打断", "review PR", "写周报", "配置 nginx",
]
_QUERY = ["今天天气如何", "播放下一首", "把灯调暗", "提醒我喝水",
          "打开降噪", "记一下这个想法", "读 README", "跑测试"]
_ANS = ["给出温湿度", "切换到下一首", "调到 30%", "设置了 1h 提醒",
        "开启降噪", "存到 memory", "读出首段", "跑完测试"]

_TEMPLATES = [
    "* 🟡 ({h:02d}:{m:02d}) Allen 提到 {topic}. (meaning {date})",
    "* 🟢 ({h:02d}:{m:02d}) 观察: Allen {action}. (meaning {date})",
    "* 🔵 ({h:02d}:{m:02d}) 环境: {env}. (meaning {date})",
    "* ⚪ ({h:02d}:{m:02d}) 工作: {work}. (meaning {date})",
    "* 🟣 ({h:02d}:{m:02d}) Allen 问 Jarvis {query}. Jarvis {ans}. (meaning {date})",
]


def generate_fake_notes(target_tokens: int, seed: int) -> str:
    """生成中文 OM 观察笔记, token 数逼近 target, 中部插入 canary fact."""
    rnd = random.Random(seed)
    lines: list[str] = []
    canary_inserted = False
    # 粗预估: 每行约 40-60 tokens; 先估算行数目标用于插入位置
    target_lines_estimate = max(10, target_tokens // 45)
    canary_line_idx = target_lines_estimate // 2

    while True:
        if not canary_inserted and len(lines) >= canary_line_idx:
            lines.append(
                f"* 🟡 (14:22) Allen 跟 Jarvis 说: 「{CANARY_FACT}, "
                f"下午 3 点后换成茶.」 (meaning 2026-04-07)"
            )
            canary_inserted = True
            continue

        tpl = rnd.choice(_TEMPLATES)
        line = tpl.format(
            h=rnd.randint(7, 23),
            m=rnd.randint(0, 59),
            date=f"2026-04-{rnd.randint(1, 14):02d}",
            topic=rnd.choice(_TOPICS),
            action=rnd.choice(_ACTIONS),
            env=rnd.choice(_ENV),
            work=rnd.choice(_WORK),
            query=rnd.choice(_QUERY),
            ans=rnd.choice(_ANS),
        )
        lines.append(line)

        if len(lines) % 5 == 0:
            cur = count_tokens("\n".join(lines))
            if cur >= target_tokens:
                break
        if len(lines) > target_lines_estimate * 3:
            break

    if not canary_inserted:
        lines.insert(
            len(lines) // 2,
            f"* 🟡 (14:22) Allen 跟 Jarvis 说: 「{CANARY_FACT}.」 (meaning 2026-04-07)",
        )

    return "\n".join(lines)


def check_recall_correct(answer: str) -> bool:
    low = answer.lower()
    return any(kw.lower() in low for kw in CANARY_KEYWORDS)


# --------------------------------------------------------------------------
# Provider adapters
# --------------------------------------------------------------------------

@dataclass
class Call:
    system: str
    user: str
    enable_cache: bool = False
    max_output_tokens: int = MAX_OUTPUT_TOKENS


@dataclass
class CallResult:
    ttft_ms: float | None
    total_ms: float
    input_tokens: int
    output_tokens: int
    answer: str
    cache_read_tokens: int = 0
    error: str | None = None


class BaseAdapter:
    provider: str = ""
    model: str = ""

    def call(self, c: Call) -> CallResult:
        raise NotImplementedError


class AnthropicAdapter(BaseAdapter):
    provider = "anthropic"

    def __init__(self, model: str) -> None:
        import anthropic

        self.model = model
        self.client = anthropic.Anthropic()

    def call(self, c: Call) -> CallResult:
        system_block: dict[str, Any] = {"type": "text", "text": c.system}
        if c.enable_cache:
            system_block["cache_control"] = {"type": "ephemeral"}

        t0 = now_ms()
        ttft = None
        parts: list[str] = []

        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=c.max_output_tokens,
                system=[system_block],
                messages=[{"role": "user", "content": c.user}],
            ) as stream:
                for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        if ttft is None:
                            ttft = now_ms() - t0
                        parts.append(event.delta.text)
                final = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            return CallResult(
                ttft_ms=None,
                total_ms=now_ms() - t0,
                input_tokens=0,
                output_tokens=0,
                answer="",
                error=f"{type(e).__name__}: {e}",
            )

        u = final.usage
        return CallResult(
            ttft_ms=ttft,
            total_ms=now_ms() - t0,
            input_tokens=(u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            output_tokens=u.output_tokens,
            answer="".join(parts),
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )


class OpenAICompatAdapter(BaseAdapter):
    """OpenAI / xAI Grok / Groq / Gemini (via /v1beta/openai)."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
    ) -> None:
        import openai

        self.provider = provider
        self.model = model
        kwargs: dict[str, Any] = {"api_key": os.getenv(api_key_env, "")}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = openai.OpenAI(**kwargs)

    def call(self, c: Call) -> CallResult:
        # Use max_tokens for broad compat; gpt-5 may need max_completion_tokens.
        max_param = "max_completion_tokens" if self.model.startswith("gpt-5") else "max_tokens"
        kwargs: dict[str, Any] = {
            "model": self.model,
            max_param: c.max_output_tokens,
            "messages": [
                {"role": "system", "content": c.system},
                {"role": "user", "content": c.user},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        t0 = now_ms()
        ttft = None
        parts: list[str] = []
        usage = None

        try:
            stream = self.client.chat.completions.create(**kwargs)
            for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        if ttft is None:
                            ttft = now_ms() - t0
                        parts.append(content)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
        except Exception as e:  # noqa: BLE001
            return CallResult(
                ttft_ms=None,
                total_ms=now_ms() - t0,
                input_tokens=0,
                output_tokens=0,
                answer="",
                error=f"{type(e).__name__}: {e}",
            )

        total_ms = now_ms() - t0
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cached = 0
        if usage and getattr(usage, "prompt_tokens_details", None):
            cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

        return CallResult(
            ttft_ms=ttft,
            total_ms=total_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            answer="".join(parts),
            cache_read_tokens=cached,
        )


def make_adapter(provider: str, model: str) -> BaseAdapter | None:
    try:
        if provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                return None
            return AnthropicAdapter(model)
        if provider == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                return None
            return OpenAICompatAdapter(provider, model, api_key_env="OPENAI_API_KEY")
        if provider == "google":
            if not os.getenv("GEMINI_API_KEY"):
                return None
            return OpenAICompatAdapter(
                provider, model,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key_env="GEMINI_API_KEY",
            )
        if provider == "xai":
            if not os.getenv("XAI_API_KEY"):
                return None
            return OpenAICompatAdapter(
                provider, model,
                base_url="https://api.x.ai/v1",
                api_key_env="XAI_API_KEY",
            )
        if provider == "groq":
            if not os.getenv("GROQ_API_KEY"):
                return None
            return OpenAICompatAdapter(
                provider, model,
                base_url="https://api.groq.com/openai/v1",
                api_key_env="GROQ_API_KEY",
            )
    except Exception as e:  # noqa: BLE001
        LOG.warning("adapter init failed (%s/%s): %s", provider, model, e)
        return None
    return None


# --------------------------------------------------------------------------
# Result record
# --------------------------------------------------------------------------

@dataclass
class Result:
    provider: str
    model: str
    context_size: int
    cache_state: str
    task: str
    run_idx: int
    input_tokens: int
    ttft_ms: float | None
    total_ms: float | None
    output_tokens: int
    tokens_per_second: float | None
    answer_preview: str
    answer_correct: bool | None
    cost_usd: float | None
    cache_read_tokens: int
    error: str | None


def estimate_cost(model: str, input_tok: int, output_tok: int, cached_tok: int = 0) -> float | None:
    if model not in PRICING:
        return None
    in_p, out_p = PRICING[model]
    # 简化: 缓存命中按 ~0.1x 估 (Anthropic), OpenAI 自动 ~0.5x. 这里统一 0.1x
    non_cached_in = max(0, input_tok - cached_tok)
    cost = (non_cached_in * in_p + cached_tok * in_p * 0.1 + output_tok * out_p) / 1_000_000
    return round(cost, 6)


# --------------------------------------------------------------------------
# Benchmarker
# --------------------------------------------------------------------------

@dataclass
class Config:
    models: list[tuple[str, str]]
    context_sizes: list[int]
    tasks: list[str]
    runs_per_point: int = 3
    include_stale: bool = True
    dry_run: bool = False


def plan_combos(cfg: Config) -> list[tuple[str, str, int, str]]:
    return [
        (p, m, ctx, task)
        for (p, m) in cfg.models
        for ctx in cfg.context_sizes
        for task in cfg.tasks
    ]


def run_benchmark(cfg: Config) -> list[Result]:
    results: list[Result] = []
    combos = plan_combos(cfg)

    # 构建 adapter 缓存, 预筛选可用
    adapters: dict[tuple[str, str], BaseAdapter] = {}
    for p, m in cfg.models:
        a = make_adapter(p, m)
        if a is None:
            LOG.warning("跳过 %s/%s (缺 API key 或 SDK 初始化失败)", p, m)
            continue
        adapters[(p, m)] = a
    if not adapters:
        LOG.error("没有可用的 adapter, 终止.")
        return results

    # warmup: 每个 adapter 一次小调用, 避免 cold-start 污染首个样本
    LOG.info("预热 %d 个 adapter...", len(adapters))
    for (p, m), adapter in adapters.items():
        try:
            _ = adapter.call(Call(system="ready?", user="hi"))
        except Exception as e:  # noqa: BLE001
            LOG.warning("warmup failed %s/%s: %s", p, m, e)

    # 准备 stale queue (phase 2 用)
    stale_queue: list[tuple[tuple[str, str], BaseAdapter, Call, dict[str, Any]]] = []

    # Phase 1: cold + warm
    total_calls = sum(
        1
        for p, m, _, _ in combos
        if (p, m) in adapters
    ) * len(CACHE_STATES if cfg.include_stale else CACHE_STATES[:2]) * cfg.runs_per_point

    pbar = tqdm(total=len(combos) * cfg.runs_per_point * (2 if cfg.include_stale else 2),
                desc="phase1 cold+warm", unit="combo")

    for p, m, ctx, task in combos:
        if (p, m) not in adapters:
            continue
        adapter = adapters[(p, m)]
        task_prompt = TASKS[task]

        for run_idx in range(cfg.runs_per_point):
            seed = hash((p, m, ctx, task, run_idx)) & 0xFFFFFFFF
            notes = generate_fake_notes(ctx, seed=seed)
            actual_tokens = count_tokens(notes)

            # cold: no cache marker
            cold_call = Call(system=notes, user=task_prompt, enable_cache=False)
            r_cold = adapter.call(cold_call) if not cfg.dry_run else _mock_result(ctx)
            results.append(_mk_result(p, m, ctx, "cold", task, run_idx, r_cold, actual_tokens))
            pbar.update(1)

            # warm: warmup + measured
            warm_call = Call(system=notes, user=task_prompt, enable_cache=True)
            if not cfg.dry_run:
                for _ in range(WARMUP_CALLS):
                    adapter.call(warm_call)
                r_warm = adapter.call(warm_call)
            else:
                r_warm = _mock_result(ctx)
            results.append(_mk_result(p, m, ctx, "warm", task, run_idx, r_warm, actual_tokens))
            pbar.update(1)

            if cfg.include_stale:
                stale_queue.append(
                    ((p, m), adapter, warm_call,
                     {"ctx": ctx, "task": task, "run_idx": run_idx, "tokens": actual_tokens})
                )
    pbar.close()

    # Phase 2: stale
    if cfg.include_stale and stale_queue and not cfg.dry_run:
        LOG.info("phase1 完成. 等待 %d 秒让缓存过期...", STALE_WAIT_SECONDS)
        for remaining in tqdm(range(STALE_WAIT_SECONDS, 0, -10),
                              desc="等 stale TTL", unit="s"):
            time.sleep(10)

        LOG.info("phase2 stale: %d 次测试", len(stale_queue))
        for (p, m), adapter, call, meta in tqdm(stale_queue, desc="phase2 stale"):
            r = adapter.call(call)
            results.append(
                _mk_result(p, m, meta["ctx"], "stale", meta["task"], meta["run_idx"],
                           r, meta["tokens"])
            )
    elif cfg.include_stale and cfg.dry_run:
        for _, _, _, meta in stale_queue:
            pass  # dry-run: skip stale

    return results


def _mock_result(ctx: int) -> CallResult:
    return CallResult(
        ttft_ms=100.0 + ctx * 0.01,
        total_ms=500.0 + ctx * 0.05,
        input_tokens=ctx,
        output_tokens=50,
        answer="mock answer 拿铁",
        cache_read_tokens=0,
    )


def _mk_result(
    p: str, m: str, ctx: int, state: str, task: str, run: int,
    r: CallResult, actual_tokens: int,
) -> Result:
    tps: float | None = None
    if r.total_ms and r.output_tokens and r.ttft_ms and r.total_ms > r.ttft_ms:
        gen_sec = (r.total_ms - r.ttft_ms) / 1000
        if gen_sec > 0:
            tps = round(r.output_tokens / gen_sec, 2)

    correct = check_recall_correct(r.answer) if task == "recall" and not r.error else None
    preview = r.answer[:120].replace("\n", " ") if r.answer else ""
    cost = estimate_cost(m, actual_tokens, r.output_tokens, r.cache_read_tokens) if not r.error else None

    return Result(
        provider=p, model=m, context_size=ctx, cache_state=state,
        task=task, run_idx=run,
        input_tokens=actual_tokens,
        ttft_ms=round(r.ttft_ms, 1) if r.ttft_ms else None,
        total_ms=round(r.total_ms, 1) if r.total_ms else None,
        output_tokens=r.output_tokens,
        tokens_per_second=tps,
        answer_preview=preview,
        answer_correct=correct,
        cost_usd=cost,
        cache_read_tokens=r.cache_read_tokens,
        error=r.error,
    )


# --------------------------------------------------------------------------
# Output: CSV, Markdown, HTML
# --------------------------------------------------------------------------

def save_csv(results: list[Result], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not results:
        LOG.warning("无结果可写 %s", path)
        return
    fields = list(asdict(results[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))
    LOG.info("CSV 已写入: %s (%d 行)", path, len(results))


def _median(xs: list[float]) -> float | None:
    vals = [x for x in xs if x is not None]
    return round(statistics.median(vals), 1) if vals else None


def build_summary(results: list[Result]) -> str:
    """生成 Markdown summary."""
    lines: list[str] = ["# LLM Benchmark Summary\n"]
    lines.append(f"共 {len(results)} 条样本.\n")

    # 过滤掉 errored 样本用于聚合
    ok = [r for r in results if r.error is None]
    if not ok:
        lines.append("所有调用都失败了. 请检查 API key 或日志.\n")
        return "\n".join(lines)

    # 按 (model, context_size, cache_state, task) 聚合, 取 median
    key = lambda r: (r.model, r.context_size, r.cache_state, r.task)
    groups: dict[tuple[str, int, str, str], list[Result]] = {}
    for r in ok:
        groups.setdefault(key(r), []).append(r)

    agg_rows: list[dict[str, Any]] = []
    for (m, ctx, state, task), rs in groups.items():
        agg_rows.append({
            "model": m,
            "context": ctx,
            "state": state,
            "task": task,
            "ttft_med": _median([r.ttft_ms for r in rs if r.ttft_ms]),
            "total_med": _median([r.total_ms for r in rs if r.total_ms]),
            "tps_med": _median([r.tokens_per_second for r in rs if r.tokens_per_second]),
            "cost_med": _median([r.cost_usd for r in rs if r.cost_usd]),
            "correct_ratio": _ratio([r.answer_correct for r in rs if r.answer_correct is not None]),
            "n": len(rs),
        })

    # Table 1: TTFT by model × context (warm cache, task=simple)
    lines.append("\n## TTFT by model × context (warm, task=simple, ms)\n")
    lines.extend(_pivot_table(agg_rows, "ttft_med",
                              filter_fn=lambda r: r["state"] == "warm" and r["task"] == "simple"))

    # Table 2: TTFT cold
    lines.append("\n## TTFT by model × context (cold, task=simple, ms)\n")
    lines.extend(_pivot_table(agg_rows, "ttft_med",
                              filter_fn=lambda r: r["state"] == "cold" and r["task"] == "simple"))

    # Table 3: TTFT stale (if present)
    if any(r["state"] == "stale" for r in agg_rows):
        lines.append("\n## TTFT by model × context (stale, task=simple, ms)\n")
        lines.extend(_pivot_table(agg_rows, "ttft_med",
                                  filter_fn=lambda r: r["state"] == "stale" and r["task"] == "simple"))

    # Table 4: recall 准确率
    lines.append("\n## Recall 准确率 by model × context\n")
    lines.extend(_pivot_table(agg_rows, "correct_ratio",
                              filter_fn=lambda r: r["task"] == "recall" and r["state"] == "warm",
                              format_fn=lambda v: f"{v*100:.0f}%" if v is not None else "—"))

    # Table 5: total 响应时间 (warm, synthesis)
    lines.append("\n## 完整响应时间 by model × context (warm, task=synthesis, ms)\n")
    lines.extend(_pivot_table(agg_rows, "total_med",
                              filter_fn=lambda r: r["state"] == "warm" and r["task"] == "synthesis"))

    # Table 6: cost (warm, synthesis)
    lines.append("\n## 每次调用成本 (warm, task=synthesis, USD)\n")
    lines.extend(_pivot_table(agg_rows, "cost_med",
                              filter_fn=lambda r: r["state"] == "warm" and r["task"] == "synthesis",
                              format_fn=lambda v: f"${v:.5f}" if v is not None else "—"))

    # 错误摘要
    errs = [r for r in results if r.error]
    if errs:
        lines.append(f"\n## 错误 ({len(errs)} 条)\n")
        seen: dict[str, int] = {}
        for r in errs:
            k = f"{r.model} / {r.error[:80]}"
            seen[k] = seen.get(k, 0) + 1
        for k, n in sorted(seen.items(), key=lambda x: -x[1]):
            lines.append(f"- ×{n}  {k}")

    return "\n".join(lines)


def _ratio(xs: list[bool]) -> float | None:
    if not xs:
        return None
    return sum(1 for x in xs if x) / len(xs)


def _pivot_table(rows: list[dict[str, Any]], value_key: str,
                 filter_fn, format_fn=None) -> list[str]:
    filtered = [r for r in rows if filter_fn(r)]
    if not filtered:
        return ["(无数据)"]
    models = sorted({r["model"] for r in filtered})
    ctxs = sorted({r["context"] for r in filtered})
    lookup = {(r["model"], r["context"]): r[value_key] for r in filtered}
    fmt = format_fn or (lambda v: f"{v:.0f}" if v is not None else "—")

    header = "| model \\ context | " + " | ".join(f"{c:,}" for c in ctxs) + " |"
    sep = "|" + "---|" * (len(ctxs) + 1)
    out = [header, sep]
    for m in models:
        cells = [fmt(lookup.get((m, c))) for c in ctxs]
        out.append(f"| `{m}` | " + " | ".join(cells) + " |")
    return out


def save_html_chart(results: list[Result], path: Path) -> None:
    try:
        import plotly.express as px
    except ImportError:
        LOG.warning("plotly 未安装, 跳过 HTML 输出")
        return

    ok = [r for r in results if r.error is None and r.ttft_ms]
    if not ok:
        return
    data = [
        {
            "model": r.model,
            "context_size": r.context_size,
            "cache_state": r.cache_state,
            "ttft_ms": r.ttft_ms,
            "task": r.task,
        }
        for r in ok
    ]
    fig = px.scatter(
        data,
        x="context_size",
        y="ttft_ms",
        color="model",
        symbol="cache_state",
        facet_col="task",
        log_x=True,
        log_y=True,
        title="TTFT vs Context Size",
        hover_data=["cache_state", "model"],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path))
    LOG.info("HTML 图表已写入: %s", path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def filter_models(models: list[tuple[str, str]], pattern: str | None) -> list[tuple[str, str]]:
    if not pattern:
        return models
    pat = pattern.lower()
    return [(p, m) for p, m in models if pat in m.lower() or pat in p.lower()]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true", help="claude+gpt, 2k+10k, 跳过 stale")
    g.add_argument("--full", action="store_true", help="全矩阵, 含 stale")

    ap.add_argument("--model", help="只测名字包含该字符串的模型 (e.g. sonnet, gpt-5)")
    ap.add_argument("--contexts", help="逗号分隔 context sizes, e.g. 2000,10000")
    ap.add_argument("--tasks", help="逗号分隔 task names (simple/recall/synthesis)")
    ap.add_argument("--runs", type=int, default=3, help="每个测试点重复次数 (默认 3)")
    ap.add_argument("--no-stale", action="store_true", help="跳过 6 分钟 stale 测试")
    ap.add_argument("--dry-run", action="store_true", help="不真的调用 API, 跑 mock 数据验证流程")
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="结果目录")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.quick:
        models = [("anthropic", "claude-sonnet-4-6"), ("openai", "gpt-5-mini")]
        contexts = [2_000, 10_000]
        tasks = list(TASKS.keys())
        include_stale = False
    elif args.full:
        models = MODELS
        contexts = CONTEXT_SIZES
        tasks = list(TASKS.keys())
        include_stale = not args.no_stale
    else:
        models = MODELS
        contexts = CONTEXT_SIZES
        tasks = list(TASKS.keys())
        include_stale = not args.no_stale

    if args.model:
        models = filter_models(models, args.model)
    if args.contexts:
        contexts = [int(x) for x in args.contexts.split(",")]
    if args.tasks:
        tasks = [t.strip() for t in args.tasks.split(",") if t.strip() in TASKS]

    cfg = Config(
        models=models,
        context_sizes=contexts,
        tasks=tasks,
        runs_per_point=args.runs,
        include_stale=include_stale,
        dry_run=args.dry_run,
    )

    LOG.info("配置: %d models × %d contexts × %d tasks × %d runs, stale=%s, dry=%s",
             len(models), len(contexts), len(tasks), args.runs, include_stale, args.dry_run)
    for p, m in models:
        LOG.info("  %s / %s", p, m)

    t_start = time.time()
    results = run_benchmark(cfg)
    elapsed = time.time() - t_start
    LOG.info("基准测试完成, 耗时 %.1f 分钟, %d 条结果", elapsed / 60, len(results))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = args.output_dir / f"results_{ts}.csv"
    md_path = args.output_dir / f"summary_{ts}.md"
    html_path = args.output_dir / f"chart_{ts}.html"

    save_csv(results, csv_path)
    md_path.write_text(build_summary(results), encoding="utf-8")
    LOG.info("Summary 已写入: %s", md_path)
    save_html_chart(results, html_path)

    # 在控制台打印 summary 前 60 行
    print("\n" + "=" * 70)
    print(md_path.read_text(encoding="utf-8")[:4000])
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
