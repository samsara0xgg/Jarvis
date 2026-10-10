"""Anthropic Messages request building for the one chat-completions history shape.

Callers keep a single history vocabulary (``role: tool``, assistant ``tool_calls``,
``image_url`` parts, a ``phase`` label); this module turns it into what
/v1/messages takes, and builds the request body: structured output from the
Responses ``text.format``, adaptive thinking with an effort, and prompt caching.

Layer rules: stdlib only; no ``jarvis.*`` import.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh", "max"})
_BAD_ID: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_-]")
# What a request that ends on the model's own words is asked next: the API takes
# no assistant prefill, so a turn that continues its own text is sent a nudge.
CONTINUE: Final[str] = "Continue."
_OPENING: Final[str] = "(start of the conversation)"

# A tool-using turn's thinking blocks, by tool call id: the next request of that
# turn must carry them back unchanged. History never holds them (it is one shape
# for every provider), so the stream or response that produced them leaves them here.
_THINKING_KEPT: Final[int] = 512
_thinking: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_thinking_lock = threading.Lock()


def remember_thinking(call_ids: Iterable[str], blocks: Sequence[Mapping[str, Any]]) -> None:
    """Keep ``blocks`` for the turn that answers these tool calls."""
    if not blocks:
        return
    with _thinking_lock:
        for call_id in call_ids:
            _thinking[call_id] = [dict(block) for block in blocks]
            _thinking.move_to_end(call_id)
        while len(_thinking) > _THINKING_KEPT:
            _thinking.popitem(last=False)


def _kept_thinking(call_ids: Iterable[str]) -> list[dict[str, Any]]:
    with _thinking_lock:
        for call_id in call_ids:
            if call_id in _thinking:
                return [dict(block) for block in _thinking[call_id]]
    return []


def safe_id(value: object) -> str:
    """A tool-use id the API accepts (``^[a-zA-Z0-9_-]+$``)."""
    return _BAD_ID.sub("_", str(value)) or "_"


def _image(url: str) -> dict[str, Any]:
    head, _, data = url.partition(",")
    if head.startswith("data:") and head.endswith(";base64"):
        media = head[5:].split(";", 1)[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _user_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else ():
        if part.get("type") == "text":
            if str(part.get("text") or "").strip():
                blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            blocks.append(_image(part["image_url"]["url"]))
    return blocks


def _arguments(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _push(
    out: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]],
    lead: list[dict[str, Any]] | None = None,
) -> None:
    """Append a message; the API folds neighbours of one role, so this does."""
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        if lead:
            out[-1]["content"][0:0] = lead
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": [*(lead or []), *blocks]})


def to_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Chat-completions history -> /v1/messages ``messages``.

    Tool results ride one user message ahead of any text that follows them; the
    first message is the user's and the last is too (no assistant prefill).
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            result = {
                "type": "tool_result", "tool_use_id": safe_id(message.get("tool_call_id")),
                "content": str(message.get("content") or ""),
            }
            _push(out, "user", [result])
        elif role == "assistant":
            calls = list(message.get("tool_calls") or ())
            blocks = _user_blocks(message.get("content"))
            blocks += [
                {
                    "type": "tool_use", "id": safe_id(call["id"]),
                    "name": call["function"]["name"],
                    "input": _arguments(call["function"].get("arguments")),
                }
                for call in calls
            ]
            _push(out, "assistant", blocks, _kept_thinking(call["id"] for call in calls))
        else:  # user, and the system notes a history carries mid-conversation
            _push(out, "user", _user_blocks(message.get("content")))
    for message in out:
        if message["role"] == "user":
            message["content"].sort(key=lambda block: block["type"] != "tool_result")
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": _OPENING}]})
    if out and out[-1]["role"] == "assistant":
        out.append({"role": "user", "content": [{"type": "text", "text": CONTINUE}]})
    return out


def to_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Tool definitions with only the keys /v1/messages takes."""
    return [
        {
            "name": tool["name"], "description": tool.get("description", ""),
            "input_schema": copy.deepcopy(
                tool.get("input_schema") or {"type": "object", "properties": {}},
            ),
        }
        for tool in tools
    ]


def _tool_choice(tool_choice: str | None) -> dict[str, str] | None:
    if tool_choice is None or tool_choice == "auto":
        return None
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return {"type": "none"}
    return {"type": "tool", "name": tool_choice}


def request_fields(  # noqa: PLR0913 — the request's whole variable part, named
    *, messages: Sequence[Mapping[str, Any]], system: str,
    tools: Sequence[Mapping[str, Any]] | None, max_tokens: int,
    reasoning_effort: str | None, text_format: Mapping[str, Any] | None = None,
    tool_choice: str | None = None, cache: bool = True,
) -> dict[str, Any]:
    """The /v1/messages fields for one request, from the preset's ``reasoning_effort``.

    ``none`` turns thinking off; ``minimal`` is ``low``; any other effort is adaptive
    thinking at that effort. A forced tool call allows no thinking. ``cache`` marks the
    request for automatic prompt caching (the last block, tools -> system -> messages).
    """
    body: dict[str, Any] = {
        "max_tokens": max_tokens, "system": system, "messages": to_messages(messages),
    }
    forced = None
    if tools:
        body["tools"] = to_tools(tools)
        forced = _tool_choice(tool_choice)
        if forced:
            body["tool_choice"] = forced
    effort = (reasoning_effort or "").lower()
    output: dict[str, Any] = {}
    if (forced and forced["type"] in {"any", "tool"}) or effort == "none":
        body["thinking"] = {"type": "disabled"}
    elif effort:
        body["thinking"] = {"type": "adaptive"}
        effort = "low" if effort == "minimal" else effort
        if effort in _EFFORTS:
            output["effort"] = effort
    if text_format:
        output["format"] = {
            "type": "json_schema", "schema": copy.deepcopy(dict(text_format["schema"])),
        }
    if output:
        body["output_config"] = output
    if cache:
        body["cache_control"] = {"type": "ephemeral"}
    return body
