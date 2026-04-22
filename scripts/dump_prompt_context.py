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
    """Write the raw prompt that would be sent to the LLM.

    Format matches the historical `/tmp/jarvis_last_prompt.txt` layout so
    the new Assembler output is directly comparable to pre-v2 dumps:
      === SYSTEM (N chars) ===
      <raw system text, exactly what xAI/OpenAI receives>
      === MESSAGES (N entries) ===
      <JSON messages array, exactly what the API receives>
      === ANTHROPIC SYSTEM (N entries) ===
      <list[dict] form, what Anthropic receives with cache_control>
    """
    openai_system = ctx.to_openai_system_str()
    anthropic_system = ctx.to_anthropic_system()

    lines: list[str] = []

    # --- SYSTEM (OpenAI/xAI single string — raw concatenation of 4 blocks) ---
    lines.append(f"=== SYSTEM ({len(openai_system)} chars) ===")
    lines.append(openai_system)
    lines.append("")

    # --- MESSAGES (JSON array exactly as sent to the API) ---
    lines.append(f"=== MESSAGES ({len(ctx.messages)} entries) ===")
    lines.append(json.dumps(ctx.messages, ensure_ascii=False, indent=2))
    lines.append("")

    # --- ANTHROPIC SYSTEM (list[dict] with cache_control — what Claude sees) ---
    lines.append(f"=== ANTHROPIC SYSTEM ({len(anthropic_system)} entries, "
                 f"{sum(1 for e in anthropic_system if 'cache_control' in e)} cached) ===")
    lines.append(json.dumps(anthropic_system, ensure_ascii=False, indent=2))
    lines.append("")

    # --- METADATA (injected observation ids — for trace v3 verification) ---
    lines.append(f"=== INJECTED OBSERVATION IDS ({len(ctx.injected_observation_ids)}) ===")
    lines.append(json.dumps(ctx.injected_observation_ids))
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")

    # Also emit a machine-readable JSON summary for scripted checks.
    summary = {
        "system_chars": len(openai_system),
        "messages_count": len(ctx.messages),
        "messages_roles": [m.get("role") for m in ctx.messages],
        "block_names": [b.name for b in ctx.blocks],
        "block_cache_flags": [b.cache for b in ctx.blocks],
        "injected_observation_ids": ctx.injected_observation_ids,
        "anthropic_entries": len(anthropic_system),
        "anthropic_cached_count": sum(1 for e in anthropic_system if "cache_control" in e),
    }
    json_out = out.with_suffix(".json")
    json_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Wrote prompt dump to {out} ({len(openai_system)} system chars, "
          f"{len(ctx.messages)} messages)")
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

    # Load real conversation history from disk so the dump reflects what
    # jarvis.py would actually send on the next turn.
    conv_path = Path(f"data/conversations/{args.user_id}.json")
    history: list[dict] = []
    if conv_path.exists():
        try:
            history = json.loads(conv_path.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = history.get("messages", []) if isinstance(history, dict) else []
        except Exception as exc:
            print(f"[warn] failed to load {conv_path}: {exc}", file=sys.stderr)
    if not history:
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
