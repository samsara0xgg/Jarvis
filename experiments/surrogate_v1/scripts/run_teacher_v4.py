"""Run v4 teacher prompt against bench. v3 -> v4 changes:
  - TEMPLATE = teacher_prompt_v4.md (2-branch anyOf, 5 RULES, 5 EXAMPLES)
  - SCHEMA imported from schema_v4 (drops greeting branch, drops 3 fields)
  - SMOKE set updated to cover all 5 defer reasons + 3 tool branches

Usage:
    uv run python experiments/surrogate_v1/scripts/run_teacher_v4.py <model> [--dry|--smoke]

Modes:
    --dry    1 sample only, smoke test the API call works
    --smoke  8 cases covering all 5 defer reasons + tool branches
    (no flag) full 80 sample bench
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import yaml
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.personality import build_identity_block, build_situation_block
from memory.core.store import MemoryStore
from memory.manager import MemoryManager
from experiments.surrogate_v1.scripts.schema_v4 import SCHEMA

TEMPLATE = ROOT / "experiments/surrogate_v1/teacher_prompt_v4.md"
SAMPLES_FILE = ROOT / "experiments/router-bench/samples.jsonl"
PRICING_FILE = ROOT / "data/pricing.json"

USER_ID = "default_user"
USER_NAME = "Allen"
USER_ROLE = "owner"

REASONING_MODELS_V5 = {"gpt-5-mini", "gpt-5-nano"}
REASONING_MODELS_V5_4 = {"gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4"}
REASONING_MODELS = REASONING_MODELS_V5 | REASONING_MODELS_V5_4
NON_REASONING_MODELS = {"gpt-4o"}
ALLOWED_MODELS = REASONING_MODELS | NON_REASONING_MODELS

REASONING_MAX_TOKENS = 4000
NONREASONING_MAX_TOKENS = 1500
NONREASONING_TEMPERATURE = 0.0

# 8 cases covering all 5 defer reasons + 3 tool branches
SMOKE_IDS = [798, 1078, 1046, 1058, 824, 1064, 1059, 893]
SMOKE_EXPECTED = {
    798:  ("tool",  None),                # control_device.set sanity
    1078: ("tool",  None),                # cc_message strict trigger "发一句"
    1046: ("defer", "ambiguous"),         # "让cc写X" - no strict cc_message trigger
    1058: ("tool",  None),                # cc_slash Chinese verb still allowed (intent yes, spans=[])
    824:  ("defer", "out_of_scope"),      # capability meta-question
    1064: ("defer", "needs_history"),     # bare correction (was context_continuation in v3)
    1059: ("defer", "multi_intent"),      # cc /model + /effort compound
    893:  ("defer", "tool_chaining"),     # "根据时间讲故事"
}


def reasoning_effort_for(model: str) -> str:
    return "minimal" if model in REASONING_MODELS_V5 else "none"


MAX_RETRIES = 1
RETRY_DELAY_S = 2.0

OBS_HEADER = (
    "The following observations are your memory of past conversations "
    "with the user. Newer observations supersede older ones. Reference "
    "specific details when relevant."
)

RESPONSE_FORMAT = {"type": "json_schema", "json_schema": SCHEMA}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("teacher_v4")


def render_blocks(cfg: dict, db_path: Path) -> tuple[str, str, str, str]:
    store = MemoryStore(str(db_path))
    mgr = MemoryManager(config=cfg)

    identity = build_identity_block(user_role=USER_ROLE)
    profile_dict = store.get_profile(USER_ID)
    profile_text = mgr._profile_to_text(profile_dict)
    profile = (f"[关于用户]\n{profile_text}" if profile_text
               else "[关于用户]\n（无 profile）")

    con = sqlite3.connect(str(db_path))
    rows = con.execute(
        "SELECT id, content FROM observations "
        "WHERE superseded_by IS NULL ORDER BY id ASC"
    ).fetchall()
    con.close()
    body = "\n".join(f"{rid}. {content}" for rid, content in rows)
    observations = (f"{OBS_HEADER}\n\n<observations>\n{body}\n</observations>")

    situation = build_situation_block(
        user_name=USER_NAME, user_role=USER_ROLE,
        user_emotion="", situation="normal",
    )
    return identity, profile, observations, situation


def build_system_prompt(cfg: dict, db_path: Path) -> str:
    template = TEMPLATE.read_text()
    identity, profile, observations, situation = render_blocks(cfg, db_path)
    rendered = (template
                .replace("{{IDENTITY_BLOCK}}", identity)
                .replace("{{PROFILE_BLOCK}}", profile)
                .replace("{{OBSERVATIONS_BLOCK}}", observations)
                .replace("{{SITUATION_BLOCK}}", situation))
    sys_idx = rendered.find("## SYSTEM")
    user_idx = rendered.find("## USER")
    if sys_idx < 0 or user_idx < 0:
        raise RuntimeError("v4 template missing ## SYSTEM or ## USER marker")
    return rendered[sys_idx:user_idx].strip()


def call_teacher(client: OpenAI, model: str, system: str, user_text: str) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            t0 = time.time()
            kwargs: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                "response_format": RESPONSE_FORMAT,
            }
            if model in REASONING_MODELS:
                kwargs["max_completion_tokens"] = REASONING_MAX_TOKENS
                kwargs["reasoning_effort"] = reasoning_effort_for(model)
            else:
                kwargs["max_tokens"] = NONREASONING_MAX_TOKENS
                kwargs["temperature"] = NONREASONING_TEMPERATURE
            resp = client.chat.completions.create(**kwargs)
            elapsed_ms = int((time.time() - t0) * 1000)
            cached = 0
            details = getattr(resp.usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
            return {
                "ok": True,
                "raw_text": resp.choices[0].message.content,
                "input_tokens": resp.usage.prompt_tokens,
                "cached_tokens": cached,
                "output_tokens": resp.usage.completion_tokens,
                "elapsed_ms": elapsed_ms,
                "finish_reason": resp.choices[0].finish_reason,
            }
        except Exception as e:
            last_err = repr(e)
            logger.warning("attempt %d failed: %s", attempt, last_err)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_S)
    return {"ok": False, "error": last_err}


def cost_usd(model: str, in_tok: int, out_tok: int, cached: int, pricing: dict) -> float:
    p = pricing["llm"][model]
    fresh = max(in_tok - cached, 0)
    cache_rate = p.get("cache_read_per_1m") or p["input_per_1m"]
    return (fresh * p["input_per_1m"] / 1_000_000
            + cached * cache_rate / 1_000_000
            + out_tok * p["output_per_1m"] / 1_000_000)


def parse_label(raw: str) -> dict | None:
    """v4 output is wrapped: {"label": {...}}. Unwrap and return inner dict."""
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        try:
            outer, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(outer, dict) and "label" in outer:
        return outer["label"]
    return outer if isinstance(outer, dict) else None


def smoke_check(samples: list[dict], outputs: list[dict]) -> tuple[int, list[dict]]:
    """For smoke mode: assert label_kind matches SMOKE_EXPECTED."""
    passes = 0
    failures = []
    for s, out in zip(samples, outputs):
        if not out.get("ok"):
            failures.append({"id": s["id"], "issue": "api_fail",
                             "error": out.get("error", "")[:200]})
            continue
        label = parse_label(out.get("raw_text", ""))
        if label is None:
            failures.append({"id": s["id"], "issue": "parse_fail",
                             "raw": out.get("raw_text", "")[:200]})
            continue
        actual_kind = label.get("label_kind")
        actual_defer = label.get("defer_reason")
        exp_kind, exp_defer = SMOKE_EXPECTED[s["id"]]
        kind_ok = actual_kind == exp_kind
        defer_ok = (exp_defer is None) or (actual_defer == exp_defer)
        if kind_ok and defer_ok:
            passes += 1
            print(f"  [OK ] id={s['id']:4d} {s['user_text'][:40]:42s} → {actual_kind}/{actual_defer}")
        else:
            print(f"  [FAIL] id={s['id']:4d} {s['user_text'][:40]:42s} → {actual_kind}/{actual_defer} (expected {exp_kind}/{exp_defer})")
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "expected": (exp_kind, exp_defer),
                             "actual": (actual_kind, actual_defer),
                             "label": label})
    return passes, failures


def main(model: str, mode: str) -> int:
    if model not in ALLOWED_MODELS:
        logger.error("model %s not allowed; pick %s", model, sorted(ALLOWED_MODELS))
        return 2
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return 1

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    db_path = Path(cfg.get("memory", {}).get("db_path", "data/memory/jarvis_memory.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    logger.info("rendering v4 system prompt (model=%s, mode=%s)", model, mode)
    system = build_system_prompt(cfg, db_path)
    logger.info("system prompt: %d chars", len(system))

    samples = [json.loads(l) for l in SAMPLES_FILE.open()]
    if mode == "dry":
        samples = samples[:1]
    elif mode == "smoke":
        samples = [s for s in samples if s["id"] in SMOKE_IDS]
        # Keep SMOKE_IDS order
        samples.sort(key=lambda s: SMOKE_IDS.index(s["id"]))
    logger.info("loaded %d bench samples (mode=%s)", len(samples), mode)

    pricing = json.loads(PRICING_FILE.read_text())
    client = OpenAI(api_key=api_key)

    suffix = "_dry" if mode == "dry" else ("_smoke" if mode == "smoke" else "")
    out_file = ROOT / f"experiments/surrogate_v1/bench80_v4_{model}{suffix}.jsonl"
    total_cost = 0.0
    total_in = total_cached = total_out = 0
    outputs = []
    with out_file.open("w") as f:
        for i, s in enumerate(samples, 1):
            logger.info("[%d/%d] id=%s text=%r",
                        i, len(samples), s["id"], s["user_text"][:50])
            meta = call_teacher(client, model, system, s["user_text"])
            rec = {
                "id": s["id"],
                "user_text": s["user_text"],
                "router_intent": s.get("router_intent"),
                "model": model,
                "prompt_version": "v4",
                "schema_strict": True,
                **meta,
            }
            if meta.get("ok"):
                c = cost_usd(model, meta["input_tokens"], meta["output_tokens"],
                             meta.get("cached_tokens", 0), pricing)
                rec["cost_usd"] = round(c, 6)
                total_cost += c
                total_in += meta["input_tokens"]
                total_cached += meta.get("cached_tokens", 0)
                total_out += meta["output_tokens"]
            outputs.append(meta)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()

    logger.info("wrote %s", out_file)
    logger.info("totals: input=%d cached=%d output=%d cost=$%.4f",
                total_in, total_cached, total_out, total_cost)
    if total_in:
        logger.info("cache hit rate: %.1f%%", 100 * total_cached / total_in)

    if mode == "smoke":
        print(f"\n=== SMOKE RESULTS ===")
        passes, failures = smoke_check(samples, outputs)
        print(f"\nResult: {passes}/{len(samples)} cases routed as expected")
        if failures:
            print("\nFailures detail (saved to file):")
            for fail in failures:
                if "actual" in fail:
                    print(f"  id={fail['id']}: {fail['user_text'][:50]!r}")
                    print(f"    expected={fail['expected']} actual={fail['actual']}")
        return 0 if passes == len(samples) else 3
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in ALLOWED_MODELS:
        print(f"Usage: run_teacher_v4.py <{'|'.join(sorted(ALLOWED_MODELS))}> [--dry|--smoke]",
              file=sys.stderr)
        sys.exit(2)
    if "--dry" in args:
        mode = "dry"
    elif "--smoke" in args:
        mode = "smoke"
    else:
        mode = "full"
    sys.exit(main(args[0], mode))
