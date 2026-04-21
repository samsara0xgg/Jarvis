"""Dump an Assembler-built PromptContext to /tmp for the 8-item verification.

Not run in CI. Use it to eyeball the four-block prompt structure the LLM
will receive on any given turn. Intentionally stand-alone (no network, no
LLM call) — reads from the live SQLite memory DB configured in config.yaml.

Usage:
    python scripts/dump_prompt_context.py [--user-id allen] [--text 'query']
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without PYTHONPATH gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from memory.manager import MemoryManager


DEFAULT_OUT = Path("/tmp/jarvis_last_prompt.txt")


def _load_config(config_path: Path) -> dict:
    with config_path.open("r") as fh:
        return yaml.safe_load(fh)


def _render(ctx, out: Path) -> None:
    anth = ctx.to_anthropic_system()
    openai = ctx.to_openai_system_str()

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("PromptContext dump — memory v2 finish verification")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"blocks: {len(ctx.blocks)}")
    lines.append(f"injected_observation_ids: {ctx.injected_observation_ids}")
    lines.append(f"messages: {len(ctx.messages)}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("Anthropic serialization (list[dict])")
    lines.append("-" * 78)
    for i, entry in enumerate(anth):
        cache = "cache=ephemeral" if "cache_control" in entry else "cache=off"
        preview = entry["text"].replace("\n", " / ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        lines.append(f"[{i}] {cache}")
        lines.append(f"    {preview}")

    lines.append("")
    lines.append("-" * 78)
    lines.append("Per-block full content")
    lines.append("-" * 78)
    for b in ctx.blocks:
        lines.append(f"### block: name={b.name!r} cache={b.cache}")
        lines.append(b.content)
        lines.append("")

    lines.append("-" * 78)
    lines.append("Messages")
    lines.append("-" * 78)
    for m in ctx.messages:
        lines.append(f"[{m.get('role')}] {m.get('content', '')[:200]}")

    lines.append("")
    lines.append("-" * 78)
    lines.append("OpenAI serialization (single string preview, first 400 chars)")
    lines.append("-" * 78)
    lines.append(openai[:400])
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")

    # Also emit a machine-readable JSON summary for scripted checks.
    summary = {
        "block_names": [b.name for b in ctx.blocks],
        "block_cache_flags": [b.cache for b in ctx.blocks],
        "injected_observation_ids": ctx.injected_observation_ids,
        "messages_roles": [m.get("role") for m in ctx.messages],
        "anthropic_entries": len(anth),
        "anthropic_cached_count": sum(1 for e in anth if "cache_control" in e),
    }
    json_out = out.with_suffix(".json")
    json_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Wrote prompt dump to {out}")
    print(f"Wrote JSON summary to {json_out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--user-id", default="default_user")
    parser.add_argument("--text", default="你还记得我住哪吗？")
    parser.add_argument("--user-name", default="Allen")
    parser.add_argument("--user-role", default="owner")
    parser.add_argument("--emotion", default="")
    parser.add_argument("--situation", default="normal")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    config = _load_config(args.config)
    mgr = MemoryManager(config)

    history = [
        {"role": "user", "content": "之前聊过温哥华"},
        {"role": "assistant", "content": "嗯，你提过。"},
    ]

    ctx = mgr.build_prompt_context(
        text=args.text,
        user_id=args.user_id,
        history=history,
        user_name=args.user_name,
        user_role=args.user_role,
        user_emotion=args.emotion,
        situation=args.situation,
    )
    _render(ctx, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
