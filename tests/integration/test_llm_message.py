"""One Anthropic message read from its raw stream (ADR 0359).

The reader is fed the events the API sends for a request that runs its own web search: thinking,
a search call whose input arrives in pieces, its result with each page's age, then text that
cites the pages. What it holds at any moment is what a deep search reads when its minute runs
out, and what it sends back when the API pauses a long turn. The client's own request is made
over a scripted message, so no network is touched.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import LLMClient, MissingAPIKeyError
from jarvis.decision.llm_message import MessageReader
from tests.support.model import Reply, ScriptedModel, Usage

if TYPE_CHECKING:
    from collections.abc import Mapping


def _start(index: int, block: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": dict(block)}


def _delta(index: int, piece: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": dict(piece)}


def _stop(index: int) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srv_1",
    "content": [{
        "type": "web_search_result", "url": "https://one.example/a", "title": "One",
        "page_age": "October 20, 2026",
    }],
}
_CITATION = {
    "type": "web_search_result_location", "url": "https://one.example/a", "title": "One",
    "cited_text": "a sentence",
}


def _search_stream() -> list[dict[str, Any]]:
    """What the API streams for one search, then a cited brief that ends the turn."""
    return [
        {"type": "message_start", "message": {
            "id": "msg_1",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 400, "output_tokens": 1},
        }},
        _start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "plan it"}),
        _delta(0, {"type": "signature_delta", "signature": "sig"}),
        _stop(0),
        _start(1, {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}}),
        _delta(1, {"type": "input_json_delta", "partial_json": '{"query": "rust'}),
        _delta(1, {"type": "input_json_delta", "partial_json": ' release"}'}),
        _stop(1),
        _start(2, _RESULT),
        _stop(2),
        _start(3, {"type": "text", "text": ""}),
        _delta(3, {"type": "text_delta", "text": "Rust 1.9"}),
        _delta(3, {"type": "text_delta", "text": "0 is out."}),
        _delta(3, {"type": "citations_delta", "citation": _CITATION}),
        _stop(3),
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {
            "output_tokens": 80, "server_tool_use": {"web_search_requests": 1},
        }},
        {"type": "message_stop"},
    ]


def _read(events: list[dict[str, Any]]) -> MessageReader:
    reader = MessageReader("LLMtest")
    for event in events:
        reader.feed(event)
    return reader


def test_a_finished_message_keeps_every_block_as_the_api_sent_it() -> None:
    """Thinking with its signature, a search with its whole input, its result and the cited text."""
    reader = _read(_search_stream())
    assert reader.blocks == [
        {"type": "thinking", "thinking": "plan it", "signature": "sig"},
        {"type": "server_tool_use", "id": "srv_1", "name": "web_search",
         "input": {"query": "rust release"}},
        _RESULT,
        {"type": "text", "text": "Rust 1.90 is out.", "citations": [_CITATION]},
    ]
    assert reader.stop_reason == "end_turn"
    assert reader.ended is True
    assert reader.search_requests == 1


def test_a_message_cut_short_holds_what_arrived_and_a_search_not_yet_whole_has_no_input() -> None:
    """At the deadline the reader has the blocks so far; an unfinished call is not guessed at."""
    cut = _search_stream()[:7]  # the call's input is half arrived
    reader = _read(cut)
    assert [b["type"] for b in reader.blocks] == ["thinking", "server_tool_use"]
    assert reader.blocks[1]["input"] == {}
    assert reader.stop_reason is None
    assert reader.ended is False
    assert reader.search_requests is None
    whole = _read(_search_stream()[:8])  # the input is whole before its block closes
    assert whole.blocks[1]["input"] == {"query": "rust release"}


def test_blocks_are_copies_the_caller_may_keep() -> None:
    """A copy taken now does not change as the message goes on."""
    events = _search_stream()
    reader = _read(events[:13])
    kept = reader.blocks
    for event in events[13:]:
        reader.feed(event)
    assert kept[-1] == {"type": "text", "text": "Rust 1.9"}
    assert reader.blocks[-1]["text"] == "Rust 1.90 is out."


def test_what_the_message_cost_is_settled_by_how_far_it_got() -> None:
    """A finished message is final; one cut short is partial, never a free call."""
    done = _read(_search_stream()).disposition("completed")
    assert done.outcome == "completed"
    assert done.provider_response_id == "msg_1"
    assert done.finish_reason == "end_turn"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (500, 80)
    assert done.usage.cache_read_tokens == 400
    assert done.usage.usage_status == "provider_final"
    cut = _read(_search_stream()[:8]).disposition("cancelled")
    assert cut.outcome == "cancelled"
    assert cut.usage.usage_status == "partial"
    assert cut.usage.input_tokens == 500


def test_a_pause_is_the_stop_reason_and_its_blocks_are_ready_to_send_back() -> None:
    """The API pauses a long turn; the blocks so far go back as they came."""
    events = _search_stream()
    events[-2] = {"type": "message_delta", "delta": {"stop_reason": "pause_turn"}, "usage": {
        "output_tokens": 80,
    }}
    reader = _read(events)
    assert reader.stop_reason == "pause_turn"
    json.dumps(reader.blocks)  # plain JSON, as the request body needs


def test_an_event_the_reader_does_not_know_is_left_alone() -> None:
    """A ping, or an event the API adds later, does not break the message."""
    events = _search_stream()
    reader = _read([{"type": "ping"}, *events[:-1], {"type": "unheard_of"}, events[-1]])
    assert reader.blocks == _read(events).blocks
    assert reader.ended is True


def _client() -> LLMClient:
    return LLMClient({
        "provider": "anthropic", "default_preset": "sonnet",
        "presets": {"sonnet": {
            "provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "NOT_SET_HERE",
            "max_tokens": 4096,
        }},
    })


def test_the_client_refuses_a_provider_whose_search_is_not_anthropics() -> None:
    """Only Anthropic's server tools return blocks like these."""
    client = LLMClient({
        "provider": "openai", "default_preset": "gpt", "presets": {"gpt": {
            "provider": "openai", "model": "gpt-6", "api_key_env": "NOT_SET_HERE",
        }},
    })
    with pytest.raises(ValueError, match="message_events"):
        client.message_events(messages=[], system="s")


def test_the_client_names_a_missing_key_when_the_stream_is_read_not_when_it_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request has its identity before any I/O; a missing key fails on the first read."""
    monkeypatch.delenv("NOT_SET_HERE", raising=False)
    stream = _client().message_events(
        messages=[{"role": "user", "content": "q"}], system="s",
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
    )
    assert stream.request_id.startswith("LLM")

    async def first() -> None:
        async for _ in stream.events:
            break

    with pytest.raises(MissingAPIKeyError):
        asyncio.run(first())


def test_the_scripted_model_streams_a_message_the_reader_reads_back() -> None:
    """The shared fake's raw stream is the reader's input; a pause sends the blocks back."""
    blocks = [
        {"type": "text", "text": "notes"},
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
        {"type": "text", "text": "brief", "citations": [_CITATION]},
    ]
    model = ScriptedModel(
        Reply(raw={"content": blocks, "stop_reason": "pause_turn"}, usage=Usage(7, 3)),
        otherwise=Reply(raw={"content": [], "stop_reason": "end_turn"}),
        provider="anthropic",
    )
    stream = model.message_events(messages=[{"role": "user", "content": "q"}], system="s")
    reader = MessageReader(stream.request_id)

    async def drain() -> None:
        async for event in stream.events:
            reader.feed(event)

    asyncio.run(drain())
    assert reader.blocks == blocks
    assert reader.stop_reason == "pause_turn"
    model.message_events(
        messages=[{"role": "user", "content": "q"}], system="s", paused=reader.blocks,
    )
    assert model.requests[1].messages == [
        {"role": "user", "content": "q"}, {"role": "assistant", "content": blocks},
    ]
