"""Anthropic Messages request building for the one chat-completions history shape.

Callers keep a single history vocabulary (``role: tool``, assistant ``tool_calls``,
``image_url`` parts, ``image_file`` parts for an uploaded picture, a ``phase`` label); this
module turns it into what
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

from jarvis.shared.system_prompt import SplitSystem

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh", "max"})
_BAD_ID: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_-]")
# What a request that ends on the model's own words is asked next: the API takes
# no assistant prefill, so a turn that continues its own text is sent a nudge.
CONTINUE: Final[str] = "Continue."
_OPENING: Final[str] = "(start of the conversation)"

# A tool-using turn's thinking and server-tool blocks (the server's tool search, ADR 0287), by
# tool call id: the next request of that turn must carry them back unchanged. History never
# holds them (it is one shape for every provider), so the stream or response that produced them
# leaves them here, in the order the response gave them.
KEPT_BLOCKS: Final = frozenset(
    {"thinking", "redacted_thinking", "server_tool_use", "tool_search_tool_result"},
)
_BLOCKS_KEPT: Final[int] = 512
_kept: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_kept_lock = threading.Lock()

NATIVE_TOOL_SEARCH: Final[Mapping[str, str]] = {
    "type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25",
}
"""Anthropic's own tool search (ADR 0287): it finds ``defer_loading`` tools inside the response."""


def remember_blocks(call_ids: Iterable[str], blocks: Sequence[Mapping[str, Any]]) -> None:
    """Keep ``blocks`` for the turn that answers these tool calls."""
    if not blocks:
        return
    with _kept_lock:
        for call_id in call_ids:
            _kept[call_id] = [dict(block) for block in blocks]
            _kept.move_to_end(call_id)
        while len(_kept) > _BLOCKS_KEPT:
            _kept.popitem(last=False)


def _kept_blocks(call_ids: Iterable[str]) -> list[dict[str, Any]]:
    with _kept_lock:
        for call_id in call_ids:
            if call_id in _kept:
                return [dict(block) for block in _kept[call_id]]
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


FILES_BETA: Final[str] = "files-api-2025-04-14"
"""The beta header a request needs to name an uploaded file in an image block (ADR 0255)."""


FAST_BETA: Final[str] = "fast-mode-2026-02-01"
"""The beta header of fast mode: the body's top-level ``speed: "fast"`` (ADR 0295)."""


def files_headers(body: Mapping[str, Any]) -> dict[str, str]:
    """The extra headers ``body`` needs.

    The Files beta when an image block names a file, the fast-mode beta for ``speed: "fast"``.
    """
    betas = []
    for message in body.get("messages") or ():
        content = message.get("content")
        if any(
            block.get("type") == "image" and block.get("source", {}).get("type") == "file"
            for block in (content if isinstance(content, list) else ())
        ):
            betas.append(FILES_BETA)
            break
    if body.get("speed") == "fast":
        betas.append(FAST_BETA)
    return {"anthropic-beta": ",".join(betas)} if betas else {}


def create_args(body: Mapping[str, Any]) -> dict[str, Any]:
    """The kwargs of ``messages.create(**...)`` for ``body``.

    The SDK's ``create`` has no ``speed`` parameter, so it rides ``extra_body``.
    """
    args = {k: v for k, v in body.items() if k != "speed"}
    args["extra_headers"] = files_headers(body) or None
    if "speed" in body:
        args["extra_body"] = {"speed": body["speed"]}
    return args


type StaleFiles = Callable[[Sequence[str]], tuple[Mapping[str, str], Callable[[], None]]]
"""ADR 0255: file ids the API did not accept -> (fallback text for each, ``commit``).

The text stands in for the picture's block (``""``: leave it out) for the one retry; ``commit``
is called once that retry has been answered, and forgets the ids."""

_stale_files: StaleFiles | None = None
_NOT_ACCEPTED: Final[frozenset[tuple[int, str]]] = frozenset(
    {(404, "not_found_error"), (403, "permission_error"), (400, "invalid_request_error")},
)
"""(HTTP status, error type) a request naming a file it cannot read may get. A 400 can have
another cause; retrying it once as words costs at most a re-upload of the ids it forgets."""


def set_stale_files_handler(handler: StaleFiles | None) -> None:
    """Install what answers a request rejected over a file id (the runtime owns the files)."""
    global _stale_files  # noqa: PLW0603 — one handler per process, like the thinking blocks above
    _stale_files = handler


def _file_ids(body: Mapping[str, Any]) -> list[str]:
    found: dict[str, None] = {}
    for message in body.get("messages") or ():
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            source = block.get("source") if block.get("type") == "image" else None
            if isinstance(source, dict) and source.get("type") == "file":
                found[str(source["file_id"])] = None
    return list(found)


def repair_stale_files(
    exc: BaseException, body: Mapping[str, Any],
) -> tuple[dict[str, Any], Callable[[], None]] | None:
    """The request to send once more, and what to call when it is answered, or ``None``.

    Only for a rejection that the status and error type say is about something not found or not
    accessible, on a request that names files. The ids the error names are the stale ones; when it
    names none, every file of the request is treated as stale. The pictures are replaced by their
    fallback text, so the retry carries marker and note, as history did before ADR 0255.
    """
    ids = _file_ids(body)
    detail = getattr(exc, "body", None)
    error = detail.get("error") if isinstance(detail, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    if (
        _stale_files is None or not ids
        or (getattr(exc, "status_code", None), kind) not in _NOT_ACCEPTED
    ):
        return None
    stale = [one for one in ids if one in str(detail)] or ids
    texts, commit = _stale_files(stale)
    messages = copy.deepcopy(list(body["messages"]))
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        blocks: list[dict[str, Any]] = []
        for block in message["content"]:
            source = block.get("source") if block.get("type") == "image" else None
            if isinstance(source, dict) and source.get("file_id") in stale:
                if texts.get(source["file_id"]):
                    blocks.append({"type": "text", "text": texts[source["file_id"]]})
            else:
                blocks.append(block)
        message["content"] = blocks
    return {**body, "messages": messages}, commit


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
        elif part.get("type") == "image_file":  # a picture already uploaded (ADR 0255)
            blocks.append(
                {"type": "image", "source": {"type": "file", "file_id": part["file_id"]}},
            )
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
            _push(out, "assistant", blocks, _kept_blocks(call["id"] for call in calls))
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
    """Tool definitions with only the keys /v1/messages takes.

    A server tool (one with a ``type``) goes as it is, less the ``catalog`` the system takes.
    """
    out: list[dict[str, Any]] = []
    for tool in tools:
        if "type" in tool:
            out.append({k: v for k, v in tool.items() if k != "catalog"})
            continue
        definition = {
            "name": tool["name"], "description": tool.get("description", ""),
            "input_schema": copy.deepcopy(
                tool.get("input_schema") or {"type": "object", "properties": {}},
            ),
        }
        if tool.get("defer_loading"):
            definition["defer_loading"] = True
        out.append(definition)
    return out


def _catalog(tools: Sequence[Mapping[str, Any]] | None) -> str:
    """What the server's tool search can find, said in the system prompt as Anthropic advises."""
    return next((str(t["catalog"]) for t in tools or () if t.get("catalog")), "")


def _tool_choice(tool_choice: str | None) -> dict[str, str] | None:
    if tool_choice is None or tool_choice == "auto":
        return None
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return {"type": "none"}
    return {"type": "tool", "name": tool_choice}


_ADAPTIVE_ONLY = ("claude-sonnet-5", "claude-opus-5")
"""Model id prefixes taking adaptive thinking only: no ``thinking: disabled``, no forced call."""


def _system_blocks(system: str, catalog: str = "") -> str | list[dict[str, Any]]:
    """A split prompt as two text blocks, a cache breakpoint closing the stable one.

    ``catalog`` joins the stable part: it changes only with the tool list ahead of it.
    """
    if not isinstance(system, SplitSystem):
        return f"{system}\n\n{catalog}" if catalog else system
    stable = f"{system.stable}\n\n{catalog}" if catalog else system.stable
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
    ]
    if system.variable:
        blocks.append({"type": "text", "text": "\n\n" + system.variable})
    return blocks


def request_fields(  # noqa: C901, PLR0913 — the request's whole variable part, named
    *, model: str = "", messages: Sequence[Mapping[str, Any]], system: str,
    tools: Sequence[Mapping[str, Any]] | None, max_tokens: int,
    reasoning_effort: str | None, text_format: Mapping[str, Any] | None = None,
    tool_choice: str | None = None, cache: bool = True, fast: bool = False,
) -> dict[str, Any]:
    """The /v1/messages fields for one request, from the preset's ``reasoning_effort``.

    ``none`` turns thinking off; ``minimal`` is ``low``; any other effort is adaptive
    thinking at that effort. A forced tool call allows no thinking, except on a model that
    takes adaptive thinking only: there the call is left to ``auto`` and the caller checks
    that a tool was called. ``cache`` marks the
    request for automatic prompt caching (the last block, tools -> system -> messages).
    ``fast`` asks for fast mode (``speed: "fast"``; :func:`files_headers` adds its beta).
    """
    body: dict[str, Any] = {
        "max_tokens": max_tokens, "system": _system_blocks(system, _catalog(tools)),
        "messages": to_messages(messages),
    }
    forced = None
    if tools:
        body["tools"] = to_tools(tools)
        forced = _tool_choice(tool_choice)
        if forced and forced["type"] in {"any", "tool"} and model.startswith(_ADAPTIVE_ONLY):
            forced = None
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
    if fast:
        body["speed"] = "fast"
    return body
