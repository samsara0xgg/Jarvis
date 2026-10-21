"""An Anthropic preset drives every request path the way a gpt-6-luna one does.

No network: the real Anthropic SDK talks to a localhost peer that records the request
body and streams a scripted reply, and the pure conversions are checked input to output.
"""

# ruff: noqa: E501 — scripted frames and expected bodies read best one entry to a line
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import SPOKEN_REPLY_FORMAT, _tool_menu, open_prefix_warm
from jarvis.decision.llm import LLMClient, failure_reason
from jarvis.decision.llm_anthropic import (
    CONTINUE,
    NATIVE_TOOL_SEARCH,
    remember_blocks,
    request_fields,
    to_messages,
)
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.llm_stream import (
    LLMResponseCompleted,
    LLMResponseFailed,
    LLMTextDelta,
    LLMToolCallCompleted,
    LLMUsageCompleted,
)
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.shared.system_prompt import SplitSystem
from jarvis.state.event_log import open_event_log
from tests.canary._helpers import repo_root
from tests.integration.test_typed_llm_stream import _SSE

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

TOOLS = [{"name": "lookup", "description": "d", "input_schema": {"type": "object"}, "deferred": True}]


def _preset(base_url: str, **extra: Any) -> LLMClient:  # noqa: ANN401 — preset keys
    return LLMClient({
        "provider": "openai",
        "presets": {"haiku": {
            "provider": "anthropic", "model": "claude-haiku-5-5", "api_key_env": "HAIKU_FIXTURE_KEY",
            "max_tokens": 4096, "reasoning_effort": "low", "base_url": base_url, **extra,
        }},
        "default_preset": "haiku", "timeout_s": 5, "max_retries": 0,
    })


def _reply(*blocks: dict[str, Any], stop: str, usage: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """An Anthropic event stream over ``blocks``: ``{"type": "thinking", ...}``, text, tool_use."""
    frames: list[dict[str, Any]] = [{
        "type": "message_start",
        "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 1,
                              "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2}},
    }]
    for index, block in enumerate(blocks):
        start = {"thinking": {"type": "thinking", "thinking": "", "signature": ""},
                 "text": {"type": "text", "text": ""},
                 "tool_use": {"type": "tool_use", "id": block.get("id"), "name": block.get("name"),
                              "input": {}},
                 "server_tool_use": {"type": "server_tool_use", "id": block.get("id"),
                                     "name": block.get("name"), "input": {}},
                 "tool_search_tool_result": block}[block["type"]]
        frames.append({"type": "content_block_start", "index": index, "content_block": start})
        if block["type"] == "tool_search_tool_result":
            pass  # the server sends its result whole, with no deltas
        elif block["type"] == "thinking":
            frames.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
            frames.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "signature_delta", "signature": block["signature"]}})
        elif block["type"] == "text":
            frames.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            frames.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "input_json_delta", "partial_json": block["json"]}})
        frames.append({"type": "content_block_stop", "index": index})
    frames += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": usage or {"output_tokens": 6}},
        {"type": "message_stop"},
    ]
    return frames


async def _run(client: LLMClient, frames: list[dict[str, Any]], **call: Any) -> tuple[_SSE, list[Any]]:  # noqa: ANN401
    peer = _SSE(frames)
    async with peer.running() as url:
        client._base_url = url.removesuffix("/v1")  # noqa: SLF001 — the peer's address
        handle = client.stream_events(on_settled=lambda _d: None, **call)
        return peer, [event async for event in handle.events()]


def test_a_spoken_request_carries_structure_thinking_caching_and_no_openai_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text_format -> output_config.format; effort low -> adaptive; the tier is not sent."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    frames = _reply(
        {"type": "thinking", "thinking": "hm", "signature": "sig-1"},
        {"type": "text", "text": '{"spoken": "好的。", "written": ""}'}, stop="end_turn",
    )
    peer, events = asyncio.run(_run(
        _preset(""), frames,
        messages=[{"role": "user", "content": "你好"}], system="SYS", tools=TOOLS,
        responses=True, text_format=SPOKEN_REPLY_FORMAT, service_tier="priority",
    ))
    body = peer.body
    assert peer.path == "/v1/messages"
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 4096
    assert body["system"] == "SYS"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SPOKEN_REPLY_FORMAT["schema"]},
    }
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["tools"] == [{"name": "lookup", "description": "d", "input_schema": {"type": "object"}}]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
    assert not {"service_tier", "temperature", "instructions", "input", "store"} & body.keys()
    texts = [event.text for event in events if isinstance(event, LLMTextDelta)]
    assert json.loads("".join(texts)) == {"spoken": "好的。", "written": ""}
    usage = next(event for event in events if isinstance(event, LLMUsageCompleted))
    # The prompt is the fresh 12 plus the 4 read and 2 written; the cost arithmetic wants it whole.
    assert (usage.input_tokens, usage.output_tokens) == (18, 6)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (4, 2)
    assert usage.usage_status == "provider_final"
    assert usage.service_tier is None
    done = next(event for event in events if isinstance(event, LLMResponseCompleted))
    assert done.finish_reason == "end_turn"


def test_a_preset_note_follows_the_system_prompt_only_on_that_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """system_note rides after every system prompt of its preset, and leaves on a switch."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    client = _preset("", system_note="# Working on this model\n- Call the tool.")
    frames = _reply({"type": "text", "text": '{"spoken": "好", "written": ""}'}, stop="end_turn")
    peer, _ = asyncio.run(_run(client, frames, messages=[{"role": "user", "content": "x"}], system="SYS"))
    assert peer.body["system"] == "SYS\n\n# Working on this model\n- Call the tool."
    client._presets["plain"] = {"provider": "anthropic", "model": "claude-haiku-5-5"}  # noqa: SLF001 — a second preset
    factory = LLMSessionFactory({"presets": {"haiku": {"provider": "anthropic", "model": "claude-haiku-5-5", "system_note": "N"}}})
    run_client = factory.create(factory.snapshot("haiku"), response_id="r")
    assert run_client._system_note == "\n\nN"  # noqa: SLF001 — a run client keeps the note
    client.switch_model("plain")
    peer, _ = asyncio.run(_run(client, frames, messages=[{"role": "user", "content": "x"}], system="SYS"))
    assert peer.body["system"] == "SYS"


def test_a_tool_turn_sends_its_thinking_blocks_back_unchanged_with_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream's thinking (and signature) leads the assistant turn of the next request."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    first = _reply(
        {"type": "thinking", "thinking": "need lookup", "signature": "sig-9"},
        {"type": "tool_use", "id": "toolu_A", "name": "lookup", "json": '{"q": "ice"}'},
        stop="tool_use",
    )
    client = _preset("")
    _, events = asyncio.run(_run(client, first, messages=[{"role": "user", "content": "q"}],
                                 system="S", tools=TOOLS))
    call = next(event for event in events if isinstance(event, LLMToolCallCompleted))
    assert (call.call_id, call.arguments_json) == ("toolu_A", '{"q": "ice"}')
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_A", "type": "function",
            "function": {"name": "lookup", "arguments": '{"q": "ice"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "cold"},
        {"role": "user", "content": "state note"},
    ]
    peer, _ = asyncio.run(_run(
        client, _reply({"type": "text", "text": "{}"}, stop="end_turn"),
        messages=history, system="S", tools=TOOLS,
    ))
    assert peer.body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "need lookup", "signature": "sig-9"},
            {"type": "tool_use", "id": "toolu_A", "name": "lookup", "input": {"q": "ice"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_A", "content": "cold"},
            {"type": "text", "text": "state note"}]},
    ]


def _tool(name: str, *, deferred: bool = False, supersedes: dict[str, str] | None = None) -> Any:  # noqa: ANN401 — a ToolDefinitionLike
    return SimpleNamespace(
        name=name, description=f"{name} d", input_schema={"type": "object"}, deferred=deferred,
        requires_confirmation=False, supersedes=supersedes or {},
    )


SEARCH_DESCRIPTION = (
    "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools "
    "for the next model call.\n\nYou have access to tools from the following sources:\n- gmail: Mail.\n"
    "Some of the tools may not have been provided to you upfront, and you should use this tool "
    "(`tool_search`) to search for the required tools. Tool metadata is English, so search with "
    "English keywords."
)


def test_on_anthropics_host_the_deferred_tools_wait_for_its_own_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0287: every tool goes, deferred ones marked; its search stands where tool_search stood."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    search = _tool("tool_search")
    search.description = SEARCH_DESCRIPTION
    surface = [
        _tool("mail", supersedes={"mcp__gmail__gmail_search": "mail(query=...)"}), search,
        _tool("mcp__gmail__gmail_search", deferred=True), _tool("mcp__gmail__gmail_send", deferred=True),
    ]
    own = _tool_menu(surface, {"mcp__gmail__gmail_send"})
    assert [t["name"] for t in own] == ["mail", "tool_search", "mcp__gmail__gmail_send"]
    menu = _tool_menu(surface, (), native=True)
    catalog = (
        "# Tool discovery\n\nYou have access to tools from the following sources:\n- gmail: Mail.\n"
        "Some of the tools may not have been provided to you upfront, and you should search for the "
        "required tools with the tool search tool. Tool metadata is English, so search with English "
        "keywords."
    )
    assert menu == [
        {"name": "mail", "description": "mail d", "input_schema": {"type": "object"}},
        {**NATIVE_TOOL_SEARCH, "catalog": catalog},
        {"name": "mcp__gmail__gmail_send", "description": "mcp__gmail__gmail_send d",
         "input_schema": {"type": "object"}, "defer_loading": True},
    ]
    peer, _ = asyncio.run(_run(
        _preset(""), _reply({"type": "text", "text": "ok"}, stop="end_turn"),
        messages=[{"role": "user", "content": "x"}], system=SplitSystem("STABLE", "VAR"), tools=menu,
    ))
    body = peer.body
    assert body["tools"] == [
        {"name": "mail", "description": "mail d", "input_schema": {"type": "object"}},
        {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
        {"name": "mcp__gmail__gmail_send", "description": "mcp__gmail__gmail_send d",
         "input_schema": {"type": "object"}, "defer_loading": True},
    ]
    assert body["system"] == [
        {"type": "text", "text": f"STABLE\n\n{catalog}", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nVAR"},
    ]


def test_only_anthropics_own_host_runs_its_tool_search() -> None:
    """Another server speaking the Messages API, and every OpenAI preset, keep tool_search."""
    assert _preset("").native_tool_search
    assert _preset("https://api.anthropic.com").native_tool_search
    assert not _preset("https://api.example.cn/anthropic").native_tool_search
    assert not LLMClient({"provider": "openai", "model": "m"}).native_tool_search


def test_a_found_tools_search_rides_back_in_order_with_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's search call and result go back on the turn's next request, where they came."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    found = {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_S", "content": {
        "type": "tool_search_tool_search_result",
        "tool_references": [{"type": "tool_reference", "tool_name": "lookup"}]}}
    first = _reply(
        {"type": "thinking", "thinking": "find it", "signature": "sig-3"},
        {"type": "server_tool_use", "id": "srvtoolu_S", "name": "tool_search_tool_bm25",
         "json": '{"query": "look up"}'},
        found,
        {"type": "tool_use", "id": "toolu_B", "name": "lookup", "json": '{"q": "ice"}'},
        stop="tool_use",
    )
    client = _preset("")
    menu = [{**NATIVE_TOOL_SEARCH, "catalog": "C"}, {**TOOLS[0], "defer_loading": True}]
    _, events = asyncio.run(_run(client, first, messages=[{"role": "user", "content": "q"}],
                                 system="S", tools=menu))
    calls = [event for event in events if isinstance(event, LLMToolCallCompleted)]
    assert [(c.call_id, c.name) for c in calls] == [("toolu_B", "lookup")]
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_B", "type": "function",
            "function": {"name": "lookup", "arguments": '{"q": "ice"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_B", "content": "cold"},
    ]
    peer, _ = asyncio.run(_run(
        client, _reply({"type": "text", "text": "{}"}, stop="end_turn"),
        messages=history, system="S", tools=menu,
    ))
    assert peer.body["system"] == "S\n\nC"
    assert peer.body["messages"][1] == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "find it", "signature": "sig-3"},
        {"type": "server_tool_use", "id": "srvtoolu_S", "name": "tool_search_tool_bm25",
         "input": {"query": "look up"}},
        found,
        {"type": "tool_use", "id": "toolu_B", "name": "lookup", "input": {"q": "ice"}}]}


def test_a_refusal_fails_the_stream_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop_reason refusal is a protocol failure, as OpenAI's refusal is."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    _, events = asyncio.run(_run(
        _preset(""), _reply({"type": "text", "text": "no"}, stop="refusal"),
        messages=[{"role": "user", "content": "x"}], system="S",
    ))
    failed = next(event for event in events if isinstance(event, LLMResponseFailed))
    assert failed.error_code == "provider_refusal"


def test_the_prefix_warm_asks_for_no_output_and_ends_on_the_users_last_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """max_tokens 0, the next turn's thinking, tools and format, and no assistant prefill."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    frames = [
        _reply(stop="max_tokens", usage={"output_tokens": 0})[0],
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]
    client = _preset("")
    registry = SimpleNamespace(for_caller=lambda _principal: [])

    async def go() -> _SSE:
        peer = _SSE(frames)
        async with peer.running() as url:
            client._base_url = url.removesuffix("/v1")  # noqa: SLF001
            conn = open_event_log(tmp_path / "e.db")
            try:
                handle = open_prefix_warm(
                    conn, llm_client=client, system_prompt="S",
                    history=[{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                             {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
                             {"role": "user", "content": "unanswered"}],
                    tool_registry=registry, responses=True, text_format=SPOKEN_REPLY_FORMAT,
                )
                assert handle is not None
                _ = [event async for event in handle.events()]
            finally:
                conn.close()
        return peer

    peer = asyncio.run(go())
    assert peer.body["max_tokens"] == 0
    assert [m["role"] for m in peer.body["messages"]] == ["user", "assistant", "user"]
    assert peer.body["messages"][-1]["content"] == [{"type": "text", "text": "q2"}]
    assert peer.body["output_config"]["format"]["type"] == "json_schema"
    assert peer.body["thinking"] == {"type": "adaptive"}


def test_chat_maps_tool_choice_images_and_the_whole_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-stream chat: forced call -> tool_choice any and no thinking; image_url -> image block."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    sent: list[dict[str, Any]] = []

    class _Messages:
        def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
            sent.append(kwargs)
            return SimpleNamespace(
                id="msg_9", stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=3,
                                      cache_read_input_tokens=5, cache_creation_input_tokens=1),
                content=[
                    SimpleNamespace(type="thinking", model_dump=lambda: {
                        "type": "thinking", "thinking": "t", "signature": "s"}),
                    SimpleNamespace(type="tool_use", id="toolu_B", name="report", input={"a": 1}),
                ],
                model_dump=dict,
            )

    client = _preset("")
    client._anthropic_client = SimpleNamespace(messages=_Messages())  # noqa: SLF001
    result = client.chat(
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "what"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}],
        system="S", tools=TOOLS, tool_choice="required", service_tier="priority",
    )
    body = sent[0]
    assert body["tool_choice"] == {"type": "any"}
    assert body["thinking"] == {"type": "disabled"}
    assert "output_config" not in body
    assert "service_tier" not in body
    assert body["messages"][0]["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert (result.input_tokens, result.cache_read_in, result.cache_write_in) == (16, 5, 1)
    assert result.tool_calls[0].arguments_json == '{"a": 1}'


def test_sonnet_forced_call_stays_auto_with_thinking_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sonnet 5.5 rejects thinking disabled: a required call goes out as auto, thinking adaptive."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    sent: list[dict[str, Any]] = []

    class _Messages:
        def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
            sent.append(kwargs)
            return SimpleNamespace(
                id="msg_s", stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0),
                content=[SimpleNamespace(type="text", text="no call")], model_dump=dict,
            )

    client = LLMClient({
        "provider": "openai",
        "presets": {"sonnet": {
            "provider": "anthropic", "model": "claude-sonnet-5-5",
            "api_key_env": "HAIKU_FIXTURE_KEY", "max_tokens": 64,
            "reasoning_effort": "medium", "base_url": "",
        }},
        "default_preset": "sonnet", "timeout_s": 5, "max_retries": 0,
    })
    client._anthropic_client = SimpleNamespace(messages=_Messages())  # noqa: SLF001
    result = client.chat(
        messages=[{"role": "user", "content": "x"}], system="S", tools=TOOLS,
        tool_choice="report",
    )
    assert "tool_choice" not in sent[0]
    assert sent[0]["thinking"] == {"type": "adaptive"}
    assert sent[0]["output_config"] == {"effort": "medium"}
    assert not result.tool_calls


@pytest.mark.parametrize(("history", "expected"), [
    # first message is the user's; a trailing assistant text is nudged on (no prefill)
    ([{"role": "assistant", "content": "hi"}],
     [{"role": "user", "content": [{"type": "text", "text": "(start of the conversation)"}]},
      {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
      {"role": "user", "content": [{"type": "text", "text": CONTINUE}]}]),
    # parallel results ride one user message, ahead of the text after them; ids are made valid
    ([{"role": "user", "content": "q"},
      {"role": "assistant", "content": "", "phase": "commentary", "tool_calls": [
          {"id": "call.1", "type": "function", "function": {"name": "a", "arguments": "bad"}},
          {"id": "call.2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "call.1", "content": "r1"},
      {"role": "tool", "tool_call_id": "call.2", "content": "r2"},
      {"role": "user", "content": "note"}],
     [{"role": "user", "content": [{"type": "text", "text": "q"}]},
      {"role": "assistant", "content": [
          {"type": "tool_use", "id": "call_1", "name": "a", "input": {}},
          {"type": "tool_use", "id": "call_2", "name": "b", "input": {}}]},
      {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "call_1", "content": "r1"},
          {"type": "tool_result", "tool_use_id": "call_2", "content": "r2"},
          {"type": "text", "text": "note"}]}]),
    # consecutive same-role messages fold; a commentary line and its call are one turn
    ([{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
      {"role": "assistant", "content": "line", "phase": "commentary"},
      {"role": "assistant", "content": "", "tool_calls": [
          {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "t1", "content": ""}],
     [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
      {"role": "assistant", "content": [
          {"type": "text", "text": "line"},
          {"type": "tool_use", "id": "t1", "name": "a", "input": {}}]},
      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}]}]),
])
def test_history_converts_to_messages(history: Sequence[dict[str, Any]], expected: list[Any]) -> None:
    """Chat-completions history -> /v1/messages, case by case."""
    assert to_messages(history) == expected


@pytest.mark.parametrize(("effort", "forced", "thinking", "output"), [
    (None, None, None, None),
    ("none", None, {"type": "disabled"}, None),
    ("minimal", None, {"type": "adaptive"}, {"effort": "low"}),
    ("medium", None, {"type": "adaptive"}, {"effort": "medium"}),
    ("medium", "required", {"type": "disabled"}, None),
    ("high", "report", {"type": "disabled"}, None),
])
def test_effort_and_forced_calls_map_to_thinking(
    effort: str | None, forced: str | None, thinking: Any, output: Any,  # noqa: ANN401
) -> None:
    """reasoning_effort -> thinking + output_config.effort; a forced call allows no thinking."""
    body = request_fields(
        messages=[{"role": "user", "content": "x"}], system="S", tools=TOOLS, max_tokens=9,
        reasoning_effort=effort, tool_choice=forced,
    )
    assert body.get("thinking") == thinking
    assert body.get("output_config") == output


def test_the_stash_survives_a_new_client_and_prices_the_haiku_card() -> None:
    """A tool loop's later round may run on another client; cost uses the >100K card past 100K."""
    remember_blocks(["toolu_Z"], [{"type": "thinking", "thinking": "x", "signature": "y"}])
    out = to_messages([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_Z", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "toolu_Z", "content": "r"},
    ])
    assert out[1]["content"][0]["type"] == "thinking"
    table = load_pricing_table(repo_root() / "data" / "pricing.json")
    model = "claude-haiku-5-5"
    # 90K prompt: 30K fresh at 0.10, 40K read at 0.01, 20K written at 0.125 per MTok
    assert compute_cost_usd(model, 90_000, 0, 40_000, 20_000, table) == pytest.approx(0.0059)
    small = compute_cost_usd(model, 100_000, 1_000_000, 0, 0, table)
    large = compute_cost_usd(model, 100_001, 1_000_000, 0, 0, table)
    assert small == pytest.approx(0.1 * 0.1 + 0.5)
    assert large == pytest.approx(100_001 * 0.5 / 1e6 + 2.5)


def test_anthropic_errors_get_the_same_reasons() -> None:
    """The ledger and the desktop name a failed call by reason, whichever SDK raised."""
    import anthropic  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    request = httpx.Request("POST", "https://x.invalid/v1/messages")
    response = httpx.Response(429, request=request)
    assert failure_reason(anthropic.RateLimitError("r", response=response, body=None)) == "rate_limited"
    assert failure_reason(anthropic.APITimeoutError(request=request)) == "timeout"
    assert failure_reason(anthropic.APIConnectionError(request=request)) == "network"


def test_the_replay_script_rebuilds_the_history_and_streams_it_through_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged Responses-shaped request -> chat history -> the preset's own request -> timings."""
    from scripts.replay_decision import history_of, replay_once  # noqa: PLC0415

    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    request = {
        "instructions": "SYS", "tools": ["lookup"],
        "text": {"format": SPOKEN_REPLY_FORMAT},
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "q"}]},
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "cold"},
        ],
    }
    system, history = history_of(request)
    assert system == "SYS"
    assert history == [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "cold"},
    ]

    async def go() -> tuple[_SSE, dict[str, Any]]:
        peer = _SSE(_reply({"type": "text", "text": '{"spoken": "x", "written": ""}'}, stop="end_turn"))
        async with peer.running() as url:
            client = _preset(url.removesuffix("/v1"))
            return peer, await replay_once(client, request, {"lookup": TOOLS[0]})

    peer, out = asyncio.run(go())
    assert peer.body["output_config"]["format"]["type"] == "json_schema"
    assert out["text"] == '{"spoken": "x", "written": ""}'
    assert out["finish_reason"] == "end_turn"
    assert out["usage"] == {"input": 18, "output": 6, "cache_read": 4, "cache_write": 2, "status": "provider_final"}
    assert out["first_token_s"] is not None


_NOTE = "# Working on this model\n- Call the tool."
_SYSTEM_TEXT = "base\n\n[About the user]\n- Allen"


def _sent_by(entry: str, system: str, note: str) -> dict[str, Any]:
    """The JSON body of the one HTTP request ``entry`` puts on the wire for ``system``.

    The real Anthropic SDK runs over an ``httpx`` transport that records the request and answers
    with a scripted reply; nothing goes further than that transport.
    """
    import anthropic  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    sent: list[dict[str, Any]] = []
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
               "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
               "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    frames = _reply({"type": "text", "text": "ok"}, stop="end_turn")

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if sent[-1].get("stream"):
            sse = "".join(f"event: {f['type']}\ndata: {json.dumps(f)}\n\n" for f in frames)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
        return httpx.Response(200, json=message)

    client = _preset("", **({"system_note": note} if note else {}))
    messages = [{"role": "user", "content": "x"}]
    if entry == "stream_events":
        peer, _ = asyncio.run(_run(client, frames, messages=messages, system=system))
        return peer.body
    client._anthropic_client = anthropic.Anthropic(  # noqa: SLF001 — the transport under the real SDK
        api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    if entry == "chat":
        client.chat(messages=messages, system=system)
    else:
        list(client.chat_stream(messages=messages, system=system))
    return sent[0]


@pytest.mark.parametrize("entry", ["chat", "chat_stream", "stream_events"])
@pytest.mark.parametrize("note", ["", _NOTE])
def test_a_split_system_prompt_caches_its_stable_prefix_as_a_block_of_its_own(
    entry: str, note: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any entry, with or without a preset note: the stable block alone carries the breakpoint.

    The route's rules follow it without one, and the note joins the rules, not the prefix.
    """
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    system = SplitSystem(_SYSTEM_TEXT, "spoken rules")
    body = _sent_by(entry, system, note)
    assert body["system"] == [
        {"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\nspoken rules" + (f"\n\n{_NOTE}" if note else "")},
    ]
    # What the model reads is what a plain-string client reads.
    assert "".join(b["text"] for b in body["system"]) == f"{system}" + (f"\n\n{_NOTE}" if note else "")


@pytest.mark.parametrize("entry", ["chat", "chat_stream", "stream_events"])
def test_a_preset_note_alone_is_the_rules_after_a_stable_prefix_and_a_plain_prompt_stays_plain(
    entry: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A split prompt with no rules of its own still keeps its prefix block; a plain string is untouched."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    assert _sent_by(entry, SplitSystem(_SYSTEM_TEXT), _NOTE)["system"] == [
        {"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"\n\n{_NOTE}"},
    ]
    assert _sent_by(entry, SplitSystem(_SYSTEM_TEXT), "")["system"] == [
        {"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}},
    ]
    assert _sent_by(entry, "S", "")["system"] == "S"
    assert _sent_by(entry, "S", _NOTE)["system"] == f"S\n\n{_NOTE}"
