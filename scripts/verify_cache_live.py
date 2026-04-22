"""Live cache metrics verifier for Anthropic + xAI with the v2 PromptContext.

Builds a real PromptContext via MemoryManager (live SQLite store), fires 2-3
identical chat_stream calls, and prints the input/output/cached token counts
from the LLMClient's `_last_*` properties. Timing drop between round 1 and 2
confirms cache is actually routing requests to a warm replica.

Usage:
    PYTHONPATH=. python scripts/verify_cache_live.py [xai|anthropic|both]

Notes:
- xAI: requires XAI_API_KEY. Uses the same `x-grok-conv-id` sticky routing as
  production. Target: >= 90% cached_tokens / input_tokens after round 1.
- Anthropic: requires ANTHROPIC_API_KEY. In a Claude Code session this env
  var points at the CC internal proxy (returns 401 for direct API calls) —
  run from a normal shell with your personal API key. Target: round 1
  cache_creation > 0, round 2 cache_read > 0.
"""
import os
import sys
import time
import yaml
from memory.manager import MemoryManager
from core.llm import LLMClient


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def build_ctx():
    config = load_config()
    mm = MemoryManager(config)
    return mm.build_prompt_context(
        text="你好",
        user_id="default_user",
        history=[],
        user_name="Allen",
        user_role="admin",
        user_emotion="",
        situation="normal",
    )


def make_client(provider: str):
    config = load_config()
    if provider == "anthropic":
        config["llm"]["provider"] = "anthropic"
        config["llm"]["model"] = "claude-sonnet-4-6"
        config["llm"]["base_url"] = ""
        config["llm"]["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
        config["llm"]["max_tokens"] = 200
    elif provider == "xai":
        config["llm"]["provider"] = "openai"
        config["llm"]["model"] = "grok-4.20-0309-non-reasoning"
        config["llm"]["base_url"] = "https://api.x.ai/v1"
        config["llm"]["api_key"] = os.environ.get("XAI_API_KEY", "")
        config["llm"]["max_tokens"] = 200
    return LLMClient(config)


def probe(provider: str, rounds: int = 2):
    print(f"\n=== {provider.upper()} cache probe ===")
    ctx = build_ctx()
    blocks_total = sum(len(b.content) for b in ctx.blocks)
    print(f"prompt_context: {len(ctx.blocks)} blocks, {blocks_total:,} chars, "
          f"{len(ctx.injected_observation_ids)} obs injected")
    client = make_client(provider)
    for i in range(rounds):
        t0 = time.monotonic()
        collected = []
        try:
            out, _ = client.chat_stream(
                user_message="简单说一句你好。",
                conversation_history=[],
                user_name="Allen",
                user_role="admin",
                on_sentence=lambda s: collected.append(s),
                prompt_context=ctx,
            )
        except Exception as e:
            print(f"  round {i+1}: EXCEPTION {type(e).__name__}: {e}")
            return
        dt = int((time.monotonic() - t0) * 1000)
        meta = getattr(client, "_last_metadata", {}) or {}
        input_tok = getattr(client, "_last_input_tokens", None) or 0
        output_tok = getattr(client, "_last_output_tokens", None) or 0
        cache_read = getattr(client, "_last_cache_read_tokens", None) or 0
        if provider == "anthropic":
            cache_create = meta.get("cache_creation_input_tokens", 0) or 0
            print(f"  round {i+1}: {dt}ms  input={input_tok} output={output_tok}  "
                  f"cache_create={cache_create}  cache_read={cache_read}  "
                  f"reply={out[:30]!r}")
        else:
            pct = f"{100*cache_read/input_tok:.1f}%" if input_tok else "n/a"
            print(f"  round {i+1}: {dt}ms  input={input_tok} output={output_tok}  "
                  f"cached={cache_read} ({pct})  reply={out[:30]!r}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "both"
    if target in ("xai", "both"):
        probe("xai", rounds=3)
    if target in ("anthropic", "both"):
        probe("anthropic", rounds=2)
