"""ScriptedModel: the one stand-in for the model client of tests that run the decision layer.

The acceptance reads it through the code that uses a client: ``decide()`` for a tool round and
an answer, and the production ``LLMStreamHandle`` for the streamed request, beside the checks
that it records what it was sent, fails when the test asks it to, and has the shape of the real
``LLMClient``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import decide
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMRequestClient, LLMSessionFactory
from jarvis.decision.llm_stream import (
    LLMResponseCompleted,
    LLMResponseFailed,
    LLMTextDelta,
    LLMToolCallCompleted,
    LLMUsageCompleted,
)
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import (
    Call,
    DecisionClient,
    Failure,
    Pause,
    Reply,
    RunClient,
    ScriptedModel,
    ScriptedSessions,
    ScriptExhaustedError,
    Usage,
)
from tests.support.turn_guards import guard_context

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm_stream import LLMStreamEvent, StreamDisposition

_MESSAGES = [{"role": "user", "content": "几点了"}]


def test_a_bare_string_is_a_reply_and_the_request_is_kept_as_it_was_sent() -> None:
    """Text comes back with the usage the script reports; the record is a copy."""
    model = ScriptedModel("八点二十。")
    messages: list[dict[str, Any]] = [{"role": "user", "content": "几点了"}]
    result = model.chat(messages=messages, system="s", tools=[{"name": "t"}], tool_choice="none")
    messages[0]["content"] = "changed after the call"
    assert (result.text, result.tool_calls, result.finish_reason) == ("八点二十。", (), "stop")
    assert (result.tokens_in, result.tokens_out, result.usage_status) == (12, 6, "provider_final")
    assert result.model_used == "scripted-model"
    (request,) = model.requests
    assert (request.kind, request.system, request.tools) == ("chat", "s", [{"name": "t"}])
    assert (request.tool_choice, request.messages) == ("none", _MESSAGES)


def test_a_reply_can_call_tools_and_say_nothing() -> None:
    """Arguments go out as the JSON string the real client returns; ids are filled in."""
    model = ScriptedModel(
        Reply(calls=(Call("lookup", {"q": "ice"}), Call("clock", call_id="mine"))),
    )
    result = model.chat(messages=_MESSAGES, system="s")
    assert result.text is None
    assert result.finish_reason == "tool_calls"
    assert [(c.call_id, c.name, json.loads(c.arguments_json)) for c in result.tool_calls] == [
        ("call-0", "lookup", {"q": "ice"}),
        ("mine", "clock", {}),
    ]


def test_a_reply_without_usage_reports_none_and_a_failure_raises_what_it_holds() -> None:
    """The cost is unknown, not zero, and the error is the test's own."""
    model = ScriptedModel(Reply("ok", usage=None), Failure(TimeoutError("slow")))
    result = model.chat(messages=_MESSAGES, system="s")
    assert (result.input_tokens, result.tokens_in, result.usage_status) == (None, 0, "unavailable")
    assert model.last_input_tokens is None
    with pytest.raises(TimeoutError, match="slow"):
        model.chat(messages=_MESSAGES, system="s")
    assert len(model.requests) == 2


def test_the_last_usage_is_what_the_client_reports_after_a_chat() -> None:
    """The cost guard reads it from the client."""
    model = ScriptedModel(Reply("ok", usage=Usage(input_tokens=100, output_tokens=7)))
    before = (model.last_input_tokens, model.last_output_tokens)
    model.chat(messages=_MESSAGES, system="s")
    assert (before, (model.last_input_tokens, model.last_output_tokens)) == (
        (None, None),
        (100, 7),
    )


def test_asking_past_the_script_fails_the_test_and_says_which_request() -> None:
    """A model that is asked once too often is a test that expected fewer rounds."""
    model = ScriptedModel("one")
    model.chat(messages=_MESSAGES, system="s")
    with pytest.raises(ScriptExhaustedError, match="chat request 2 but the script has 1"):
        model.chat(messages=_MESSAGES, system="s")


_CONFIG = {
    "provider": "openai",
    "default_preset": "luna",
    "presets": {
        "luna": {"model": "m", "base_url": "https://example.invalid", "max_tokens": 8},
        "deep": {"model": "d", "base_url": "https://example.invalid", "max_tokens": 8},
    },
}


def test_the_stand_in_and_the_real_client_both_have_the_shape_decide_reads() -> None:
    """The members exist on both; the type check in model.py holds their signatures."""
    assert isinstance(ScriptedModel(), DecisionClient)
    assert isinstance(LLMClient(_CONFIG), DecisionClient)


def test_a_request_past_the_script_gets_the_standing_answer_when_there_is_one() -> None:
    """``otherwise`` serves any number of turns; ``script`` adds the next answer as a test goes."""
    model = ScriptedModel("first", otherwise="standing")
    model.script("added")
    answers = [model.chat(messages=_MESSAGES, system="s").text for _ in range(4)]
    assert answers == ["first", "added", "standing", "standing"]
    assert len(model.requests) == 4


def test_a_paused_request_waits_for_the_test_and_a_release_that_never_comes_fails_it() -> None:
    """The test knows when the model is asked, and lets the answer go when it chooses."""
    entered, released = threading.Event(), threading.Event()
    model = ScriptedModel(Pause(entered, released, "late"), Pause(entered, released, "x", 0))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(model.chat, messages=_MESSAGES, system="s")
        assert entered.wait(timeout=5)
        assert not future.done()
        released.set()
        assert future.result(timeout=5).text == "late"
    released.clear()
    with pytest.raises(TimeoutError, match="never released"):
        model.chat(messages=_MESSAGES, system="s")


def test_every_run_of_a_session_factory_reads_one_script_and_keeps_its_own_usage() -> None:
    """A run's client is pinned to its preset; the record and the script are the model's."""
    model = ScriptedModel(Reply("one", usage=Usage(7, 1)), Reply("two", usage=Usage(9, 2)))
    sessions = ScriptedSessions(model, _CONFIG)
    first = sessions.create(sessions.snapshot(None), response_id="R1")
    second = sessions.create(sessions.snapshot("deep"), response_id="R2")
    real = LLMRequestClient(snapshot=sessions.snapshot(None), response_id="R")
    assert isinstance(first, RunClient)
    assert isinstance(real, RunClient)
    assert [(c.response_id, c.model) for c in (first, second)] == [("R1", "m"), ("R2", "d")]
    assert first.chat(messages=_MESSAGES, system="s").text == "one"
    assert second.chat(messages=_MESSAGES, system="s").text == "two"
    assert (first.last_input_tokens, second.last_input_tokens) == (7, 9)
    assert [request.kind for request in model.requests] == ["chat", "chat"]
    assert isinstance(sessions, LLMSessionFactory)


def test_a_claude_preset_keeps_the_files_it_is_given() -> None:
    """The Files API a picture goes up through: ids count the uploads, deletes are kept."""
    model = ScriptedModel(provider="anthropic")
    first = model.upload_file("a.png", b"xx", "image/png")
    second = model.upload_file("b.png", b"yyy", "image/png")
    model.delete_file(first)
    assert (first, second) == ("file_001", "file_002")
    assert model.uploads == [("a.png", 2, "image/png"), ("b.png", 3, "image/png")]
    assert model.deleted_files == ["file_001"]


def test_decide_runs_a_tool_round_and_an_answer_on_a_script(tmp_path: Path) -> None:
    """The real decide() loop, dispatcher and event log; the model is the script."""
    model = ScriptedModel(Reply(calls=(Call("kept"),)), "没有备忘。")
    with open_event_log(tmp_path / "events.db") as conn:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "几点了", "turn_id": "T1"},
            correlation={"turn_id": "T1"},
        )
        result = decide(trigger, guard_context(conn, tmp_path, model.as_llm_client(), 4))
    assert result.response_plan is not None
    assert result.response_plan.text == "没有备忘。"
    first, second = model.requests
    assert first.tools, "the first round offers the registry's tools"
    (tool_message,) = (m for m in second.messages if m["role"] == "tool")
    assert "unknown tool" not in tool_message["content"]  # the dispatcher ran the real `kept`
    assert len(second.messages) > len(first.messages)


def _events(model: ScriptedModel) -> tuple[list[LLMStreamEvent], list[StreamDisposition]]:
    settled: list[StreamDisposition] = []

    async def read() -> list[LLMStreamEvent]:
        handle = model.stream_events(
            messages=_MESSAGES, system="s", tools=[{"name": "t"}], on_settled=settled.append
        )
        return [event async for event in handle.events()]

    return asyncio.run(read()), settled


def test_a_streamed_reply_reaches_the_consumer_as_the_typed_events_of_a_provider() -> None:
    """Text, a proposal, the usage and the finish, through the production normalizer."""
    reply = Reply("八点", calls=(Call("lookup", {"q": "ice"}),), usage=Usage(12, 6, 4))
    model = ScriptedModel(reply)
    events, settled = _events(model)
    assert [event.kind for event in events if event.kind != "tool_arguments_delta"] == [
        "text_delta",
        "tool_started",
        "tool_completed",
        "usage_completed",
        "response_completed",
    ]
    text = next(e for e in events if isinstance(e, LLMTextDelta))
    call = next(e for e in events if isinstance(e, LLMToolCallCompleted))
    usage = next(e for e in events if isinstance(e, LLMUsageCompleted))
    done = next(e for e in events if isinstance(e, LLMResponseCompleted))
    assert text.text == "八点"
    assert (call.name, json.loads(call.arguments_json)) == ("lookup", {"q": "ice"})
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (12, 6, 4)
    assert done.finish_reason == "tool_calls"
    (disposition,) = settled
    assert disposition.outcome == "completed"
    (request,) = model.requests
    assert (request.kind, request.tools) == ("stream", [{"name": "t"}])


def test_a_streamed_failure_is_a_failed_response_with_nothing_to_run() -> None:
    """The consumer sees the failure event and the accounting owner is told."""
    model = ScriptedModel(Failure(ConnectionResetError("reset")))
    events, settled = _events(model)
    failed = [e for e in events if isinstance(e, LLMResponseFailed)]
    assert len(failed) == 1
    assert not [e for e in events if isinstance(e, LLMToolCallCompleted)]
    (disposition,) = settled
    assert disposition.outcome == "error"
