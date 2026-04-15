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

## USER ASSERTIONS ARE AUTHORITATIVE
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
# ===== §10 OUTPUT (Task 13) =====
# ===== §11 CLI (Task 14) =====


def main() -> None:
    raise NotImplementedError("Built in Task 14")


if __name__ == "__main__":
    main()
