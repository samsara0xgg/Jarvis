"""Benchmark intent router latency across LLM providers.

Usage:
    source ~/.secrets
    python tests/benchmark_router_latency.py
"""

from __future__ import annotations

import os
import time
import statistics
import requests

# Simple classification prompt (mimics intent router)
SYSTEM = "你是意图分类器。返回JSON: {\"intent\":\"smart_home|info_query|complex\",\"confidence\":0.9}"
TEST_QUERIES = [
    "开灯",
    "今天天气怎么样",
    "帮我写一首诗",
    "关闭客厅的灯和空调",
    "现在几点了",
]

PROVIDERS = {
    "groq-8b": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.1-8b-instant",
    },
    "groq-70b": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    "openai-4o-mini": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
}

ROUNDS = 3  # each query repeated N times


def bench_one(url: str, api_key: str, model: str, query: str) -> float | None:
    """Single request, returns latency in ms or None on failure."""
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": query},
                ],
                "temperature": 0,
                "max_tokens": 100,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return None


def main() -> None:
    print(f"Testing {len(PROVIDERS)} providers × {len(TEST_QUERIES)} queries × {ROUNDS} rounds\n")

    for name, cfg in PROVIDERS.items():
        api_key = os.environ.get(cfg["key_env"], "")
        if not api_key:
            print(f"  {name:20s}  SKIPPED (no {cfg['key_env']})")
            continue

        latencies: list[float] = []
        failures = 0

        for _ in range(ROUNDS):
            for q in TEST_QUERIES:
                ms = bench_one(cfg["url"], api_key, cfg["model"], q)
                if ms is not None:
                    latencies.append(ms)
                else:
                    failures += 1
                time.sleep(0.3)  # avoid rate limit

        if latencies:
            p50 = statistics.median(latencies)
            p90 = sorted(latencies)[int(len(latencies) * 0.9)]
            avg = statistics.mean(latencies)
            mn = min(latencies)
            mx = max(latencies)
            print(
                f"  {name:20s}  avg={avg:6.0f}ms  p50={p50:6.0f}ms  p90={p90:6.0f}ms  "
                f"min={mn:5.0f}ms  max={mx:5.0f}ms  fail={failures}/{ROUNDS * len(TEST_QUERIES)}"
            )
        else:
            print(f"  {name:20s}  ALL FAILED ({failures} requests)")

    print("\nDone.")


if __name__ == "__main__":
    main()
