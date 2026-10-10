r"""Replay one logged decision request through the real ``LLMClient.stream_events``.

    .venv/bin/python scripts/replay_decision.py LLM_IO_JSONL --tools TOOLS.json \\
        --preset haiku [--line -1] [--runs 3] [--config config/jarvis.yaml]

``LLM_IO_JSONL`` is a ``llm-io.jsonl`` (``diagnostics.log_llm_io``); the record is the
``--line``-th line of ``kind: decision`` (default the last). The log names its tools
only, so ``TOOLS.json`` is ``{name: {name, description, input_schema}}``. The record is
OpenAI-shaped (Responses ``instructions``/``input`` or chat ``messages``); it is turned
back into the chat-completions history the decision loop keeps and sent as the preset's
provider would send it, so an Anthropic preset exercises the adaptation. Keys come from
the environment, then from the runtime root's saved keys as Jarvis loads them; none is
ever printed. Prints JSON: first-token and total seconds, text, tool calls, usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_stream import (
    LLMTextDelta,
    LLMToolCallCompleted,
    LLMToolCallStarted,
    LLMUsageCompleted,
)
from jarvis.deployment import load_env_file

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.decision.llm_stream import StreamDisposition

_OMITTED = "[picture omitted]"


def decision_record(path: Path, line: int) -> dict[str, Any]:
    """The ``line``-th ``kind: decision`` record of the log."""
    records = [
        record
        for text in path.read_text(encoding="utf-8").splitlines()
        if text.strip() and (record := json.loads(text)).get("kind") == "decision"
    ]
    if not records:
        message = f"{path} holds no kind=decision record"
        raise SystemExit(message)
    return dict(records[line])


def _parts(content: Any) -> Any:  # noqa: ANN401 — a Responses content value: text or typed parts
    if not isinstance(content, list):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "input_image":
            url = str(part.get("image_url") or "")
            parts.append(
                {"type": "text", "text": _OMITTED} if _OMITTED in url
                else {"type": "image_url", "image_url": {"url": url}},
            )
    return parts


def history_of(request: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """``(system, messages)`` in the shape the decision loop keeps."""
    if "input" not in request:  # chat/completions
        messages = [dict(one) for one in request["messages"]]
        system = messages.pop(0)["content"] if messages[0]["role"] == "system" else ""
        return str(system), messages
    messages = []
    for item in request["input"]:
        kind = item.get("type")
        if kind == "function_call_output":
            messages.append({
                "role": "tool", "tool_call_id": item["call_id"], "content": item.get("output", ""),
            })
        elif kind == "function_call":
            call = {
                "id": item["call_id"], "type": "function",
                "function": {"name": item["name"], "arguments": item.get("arguments", "{}")},
            }
            last = messages[-1] if messages else None
            if last and last["role"] == "assistant":
                last.setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif kind is None or kind == "message":
            message: dict[str, Any] = {
                "role": item["role"], "content": _parts(item.get("content", "")),
            }
            if item.get("phase"):
                message["phase"] = item["phase"]
            messages.append(message)
    return str(request.get("instructions") or ""), messages


def tools_of(request: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The request's tools, by name, from the catalog."""
    names: Sequence[str] = request.get("tools") or []
    missing = [name for name in names if name not in catalog]
    if missing:
        message = f"TOOLS.json lacks {missing}"
        raise SystemExit(message)
    return [dict(catalog[name]) for name in names]


def _client(config_path: Path, preset: str) -> LLMClient:
    llm = yaml.safe_load(config_path.read_text(encoding="utf-8"))["llm"]
    spec = llm["presets"][preset]
    return LLMClient({
        "provider": llm.get("provider", "openai"),
        "presets": {preset: dict(spec)},
        "default_preset": preset,
        "timeout_s": llm.get("timeout_s", 30),
        "max_retries": llm.get("max_retries", 1),
    })


async def replay_once(
    client: LLMClient, request: Mapping[str, Any], catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """One streamed request; the timings, text, tool calls and usage."""
    system, messages = history_of(request)
    settled: list[StreamDisposition] = []
    text_format = (request.get("text") or {}).get("format")
    started = time.monotonic()
    handle = client.stream_events(
        messages=messages, system=system, tools=tools_of(request, catalog),
        on_settled=settled.append, responses="input" in request,
        text_format=text_format or None,
    )
    first: float | None = None
    text, calls = "", []
    usage: LLMUsageCompleted | None = None
    finish = failure = None
    async for event in handle.events():
        if first is None and isinstance(event, (LLMTextDelta, LLMToolCallStarted)):
            first = time.monotonic() - started
        if isinstance(event, LLMTextDelta):
            text += event.text
        elif isinstance(event, LLMToolCallCompleted):
            calls.append({"name": event.name, "arguments": event.arguments_json})
        elif isinstance(event, LLMUsageCompleted):
            usage = event
        elif event.kind == "response_completed":
            finish = event.finish_reason
        elif event.kind == "response_failed":
            failure = event.error_code
    return {
        "first_token_s": None if first is None else round(first, 3),
        "total_s": round(time.monotonic() - started, 3),
        "text": text, "tool_calls": calls, "finish_reason": finish, "error": failure,
        "usage": None if usage is None else {
            "input": usage.input_tokens, "output": usage.output_tokens,
            "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens,
            "status": usage.usage_status,
        },
    }


def main() -> int:
    """Parse arguments, replay ``--runs`` times, print the JSON."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--line", type=int, default=-1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--config", type=Path, default=Path("config/jarvis.yaml"))
    args = parser.parse_args()
    root = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis")
    load_env_file(root.expanduser())  # fill-only; the values are never read here
    client = _client(args.config, args.preset)
    request = decision_record(args.log, args.line)["request"]
    catalog = json.loads(args.tools.read_text(encoding="utf-8"))
    results = [asyncio.run(replay_once(client, request, catalog)) for _ in range(args.runs)]
    out: Any = {"preset": args.preset, "model": client.model, **results[0]} if args.runs == 1 else {
        "preset": args.preset, "model": client.model, "runs": results,
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
