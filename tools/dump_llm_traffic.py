"""Dump every LLM round trip in a Day-1 happy-path run.

Run with the worktree-local venv:

    ./.venv/bin/python tools/dump_llm_traffic.py

Wraps LLMClient.chat to intercept (system, messages, tools, result),
runs run_turn() against real OpenRouter + gpt-5.5, prints each call's
inputs and the assistant's response (text or tool_calls) verbatim.
"""

# Printing every call's verbatim payload IS the purpose of this debug tool;
# T201 is disabled file-wide rather than annotated on every print site.
# ruff: noqa: T201

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from jarvis.decision.llm import ChatResult

UTTERANCE = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001 — fullwidth CJK punctuation is authentic Allen-style input we want to exercise the prompt against.


def main() -> int:
    """Run one Day-1 happy-path turn and print every LLM call verbatim."""
    with tempfile.TemporaryDirectory() as tmp:
        runtime_root = Path(tmp)
        runtime = bootstrap_runtime_app(runtime_root=runtime_root)

        # Seed exactly one open task created 26h ago (mirrors scenario fixture).
        yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
        emit_event(
            runtime.conn,
            type="task.created",
            payload={
                "task_id": "task_X",
                "goal": "Implement Day-1 verify pipeline",
                "source": "manual",
            },
            ts_epoch_ms=yesterday_ms,
        )

        # Wrap chat() so we capture every input/output pair.
        original_chat = runtime.llm_client.chat
        call_index = {"n": 0}

        def chat_spy(
            *,
            messages: list[dict[str, Any]],
            system: str,
            tools: list[dict[str, Any]] | None = None,
            tool_choice: str | None = "auto",
        ) -> ChatResult:
            call_index["n"] += 1
            n = call_index["n"]
            sep = "=" * 78
            print(f"\n\n{sep}\nLLM CALL #{n}\n{sep}")
            print(f"\n--- SYSTEM PROMPT (length={len(system)} chars) ---")
            print(system)
            print(f"\n--- MESSAGES ({len(messages)}) ---")
            for i, msg in enumerate(messages):
                print(f"\n[msg {i}] role={msg.get('role')!r}")
                # Pretty-print content / tool_calls / tool_call_id for legibility.
                printable = {k: v for k, v in msg.items() if k != "role"}
                print(json.dumps(printable, indent=2, ensure_ascii=False))
            print(f"\n--- TOOLS ({len(tools) if tools else 0}) ---")
            if tools:
                print(json.dumps(tools, indent=2, ensure_ascii=False))
            print(f"\n--- tool_choice = {tool_choice!r} ---")

            result = original_chat(
                messages=messages, system=system, tools=tools, tool_choice=tool_choice,
            )

            print(f"\n--- RESPONSE #{n} ---")
            print(f"finish_reason: {result.finish_reason!r}")
            print(f"input_tokens: {result.input_tokens} | output_tokens: {result.output_tokens}")
            if result.text is not None:
                print(f"\n[text]\n{result.text}")
            if result.tool_calls:
                print(f"\n[tool_calls] ({len(result.tool_calls)})")
                for tc in result.tool_calls:
                    print(
                        f"  - call_id={tc.call_id!r} name={tc.name!r} "
                        f"arguments_json={tc.arguments_json!r}",
                    )
            return result

        runtime.llm_client.chat = chat_spy  # type: ignore[method-assign]

        result = run_turn(runtime, utterance=UTTERANCE)

        sep = "=" * 78
        print(f"\n\n{sep}\nFINAL RESPONSE TEXT (written to surface)\n{sep}\n")
        print(result.response_text)
        print(f"\n[iterations={result.iterations}, events_emitted={len(result.events_emitted)}]")
        print(f"[turn_id={result.turn_id!r}]")

        runtime.conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
