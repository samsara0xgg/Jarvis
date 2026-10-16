"""ADR 0276: each answered turn leaves one timing fact, and a read-only route serves them.

Real ``drive_turn``/``decide()`` and a real on-disk Event Log over the localhost peers of the
spoken-route tests: an Anthropic event stream that thinks before it writes, with cache fields in
its usage, and the OpenAI /v1/responses peer. Only the model's output is scripted.
"""

# ruff: noqa: RUF001 — fullwidth stops in the scripted answers
from __future__ import annotations

import asyncio
import functools
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.decision import _ClaimHold, turn_timing
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.runtime.inherent_loop import _serve_turn_timings
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import MemorySettings
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from tests.integration.test_anthropic_adaptation import _preset, _reply
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - autouse fixture of the spoken-route peer
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_typed_llm_stream import _SSE
from tests.integration.test_wire_routine_streaming import _drive, _payloads
from tools import turn_timings

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from jarvis.runtime import JarvisRuntime

_ANSWER = "八点二十有一班。"
_ORDER = (
    "opened_ms", "sent_ms", "first_event_ms", "first_thinking_ms", "first_text_ms", "done_ms",
)


def _timing(runtime: JarvisRuntime) -> dict[str, Any]:
    (payload,) = _payloads(runtime.conn, "turn.timing")
    return payload


def _ascending(values: list[float | None]) -> bool:
    assert None not in values, values
    return values == sorted(values)  # type: ignore[type-var]


def _anthropic_runtime(tmp_path: Path, base_url: str) -> JarvisRuntime:
    """The spoken-route runtime, its model Sonnet 5.5 on the Anthropic peer at ``base_url``."""
    config = {
        "provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 256,
        "base_url": base_url.removesuffix("/v1"), "api_key_env": "ROUTINE_STREAM_FIXTURE_KEY",
        "timeout_s": 5, "max_retries": 0,
    }
    runtime = _spoken_runtime(tmp_path, base_url)
    return replace(
        runtime, llm_client=LLMClient(config), llm_session_factory=LLMSessionFactory(config),
        memory=MemorySettings.from_config({}, runtime_root=tmp_path),
    )


def test_a_stream_says_when_its_thinking_and_its_text_first_came(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sent, then the first event, then thinking (counted in bytes), then the first text."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    frames = _reply(
        {"type": "thinking", "thinking": "hm", "signature": "sig-1"},
        {"type": "text", "text": "好的。"}, stop="end_turn",
    )

    async def run() -> Any:  # noqa: ANN401 - the stream handle
        peer = _SSE(frames)
        async with peer.running() as url:
            client = _preset("")
            client._base_url = url.removesuffix("/v1")  # noqa: SLF001 - the peer's address
            handle = client.stream_events(
                messages=[{"role": "user", "content": "你好"}], system="SYS",
                on_settled=lambda _d: None,
            )
            _ = [event async for event in handle.events()]
            return handle

    seen = asyncio.run(run()).timing
    marks = [seen.sent_ns, seen.first_event_ns, seen.first_thinking_ns, seen.first_text_ns]
    assert None not in marks
    assert marks == sorted(marks)
    assert seen.thinking_bytes == len("hm")


def test_an_answered_turn_writes_one_timing_fact_with_ordered_moments_and_usage(
    tmp_path: Path,
) -> None:
    """A Sonnet turn: thinking before text, cache tokens counted, the first chunk after the text."""
    frames = _reply(
        {"type": "thinking", "thinking": "the bus times are in the state", "signature": "s"},
        {"type": "text", "text": _ANSWER}, stop="end_turn", usage={"output_tokens": 6},
    )

    async def run() -> JarvisRuntime:
        async with _SSE(frames).running() as url:
            runtime = _anthropic_runtime(tmp_path, url)
            intent = emit_event(
                runtime.conn, type="surface.user_intent",
                payload={"transcript": "下一班公交几点", "turn_id": "T-sonnet",
                         "channel": "cli_stdin"},
                correlation={"turn_id": "T-sonnet"},
            )
            await asyncio.to_thread(_drive, runtime, intent)
            return runtime

    runtime = asyncio.run(run())
    fact = _timing(runtime)
    assert (fact["turn_id"], fact["channel"], fact["trigger"]) == (
        "T-sonnet", "cli_stdin", "surface.user_intent",
    )
    assert (fact["provider"], fact["model"]) == ("anthropic", "claude-sonnet-5-5")
    (request,) = fact["requests"]
    assert request["kind"] == "decision"
    assert request["streamed"] is True
    assert _ascending([request[name] for name in _ORDER])
    # The turn's first chunk is after the model's first text, and the turn ends after both.
    assert _ascending([
        fact["decide_start_ms"], fact["context_ms"], request["sent_ms"], fact["first_text_ms"],
        fact["first_chunk_ms"], fact["done_ms"],
    ])
    assert fact["first_text_ms"] == request["first_text_ms"]
    assert fact["context_ms"] == request["opened_ms"]
    # The prompt is the fresh 12 plus the 4 read and 2 written (the stream's own count).
    assert (request["input_tokens"], request["output_tokens"]) == (18, 6)
    assert (request["cache_read_tokens"], request["cache_write_tokens"]) == (4, 2)
    assert request["thinking_bytes"] == len("the bus times are in the state")
    assert request["tool_call"] is False
    assert fact["tool_round_before_text"] is False
    assert fact["holds"] == {}
    assert fact["held_by"] is None
    assert {"memory", "packet", "messages"} <= set(fact["context_parts"])
    # It joins the request's cost row.
    costs = _payloads(runtime.conn, "cost.recorded")
    assert [c["llm_request_id"] for c in costs] == [request["llm_request_id"]]


def test_a_tool_round_before_the_text_is_flagged_and_each_request_is_listed(
    tmp_path: Path,
) -> None:
    """Two requests: the first ends in a call, the second writes the answer."""
    with _Peer([[("call", "kept")], [("final_answer", _ANSWER)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        intent = emit_event(
            runtime.conn, type="surface.user_intent",
            payload={"transcript": "备忘里有什么", "turn_id": "T-tool", "channel": "cli_stdin"},
            correlation={"turn_id": "T-tool"},
        )
        _drive(runtime, intent)
    fact = _timing(runtime)
    first, second = fact["requests"]
    assert (first["tool_call"], second["tool_call"]) == (True, False)
    assert fact["tool_round_before_text"] is True
    assert first["first_text_ms"] is None
    assert second["sent_ms"] >= first["done_ms"]
    assert fact["first_text_ms"] == second["first_text_ms"]


def test_a_held_answer_names_the_hold_that_delayed_its_first_chunk(tmp_path: Path) -> None:
    """ADR 0272: the model answers at once, her first sentence leaves 1.5 s after his words."""
    with _Peer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        heard = _spoken(runtime.conn, "T-held", "我想问一下去学校的公交")
        _drive(runtime, heard)
    fact = _timing(runtime)
    (request,) = fact["requests"]
    assert fact["held_by"] == "answer_hold"
    assert set(fact["holds"]) == {"answer_hold"}
    # The wait is most of the time between the model's text and the chunk.
    assert fact["holds"]["answer_hold"] >= 0.5 * (fact["first_chunk_ms"] - request["first_text_ms"])
    assert fact["holds"]["answer_hold"] >= 800
    assert fact["first_chunk_ms"] >= 1400
    assert request["first_text_ms"] < 1000  # the request itself went at once


def test_a_claim_held_until_the_request_ends_is_counted_until_a_chunk_is_out() -> None:
    """ADR 0263: a claim waits for its request; a wait after a chunk is out is no hold."""
    timer = turn_timing.TurnTimer(turn_id="T", channel=None, trigger="x", queue_ms=0)
    token = turn_timing.bind(timer)
    out: list[str] = []
    try:
        held = _ClaimHold(out.append, lambda: None, 0, judged=True)
        held.feed("我查了，没有。")
        time.sleep(0.02)
        assert out == []
        held.release()
        assert out == ["我查了，没有。"]
        assert timer.holds["claim_hold"] >= 15_000_000
        timer.mark("first_chunk")
        turn_timing.hold("surrogate", time.monotonic_ns() - 50_000_000)  # too late to matter
    finally:
        turn_timing.release(token)
    assert set(timer.holds) == {"claim_hold"}
    assert timer.payload()["held_by"] == "claim_hold"


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    """A brain's app with the timing route wired to a log in ``tmp_path``, reached on loopback."""
    log = tmp_path / "events.db"
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1", broadcaster=InherentBroadcaster(),
        turn_timings_read=functools.partial(_serve_turn_timings, log),
    ))
    require_local_key(app, functools.partial(local_key_matches, local_key(tmp_path)))
    return TestClient(app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000)), log


def test_the_timing_route_needs_the_key_and_serves_the_newest_facts_oldest_first(
    tmp_path: Path,
) -> None:
    """403/401 without the key; with it, the facts after ``since``, at most ``limit`` of them."""
    client, log = _client(tmp_path)
    conn = open_event_log(log)
    for number, ts in enumerate((1_000, 2_000, 3_000)):
        emit_event(
            conn, type="turn.timing", ts_epoch_ms=ts,
            payload={"turn_id": f"T{number}", "first_chunk_ms": 100.0 + number},
            correlation={"turn_id": f"T{number}"},
        )
    emit_event(conn, type="turn.ended", payload={"turn_id": "Tx"}, ts_epoch_ms=4_000)
    conn.close()
    path = "/inherent/turn-timings"
    key = {"Authorization": f"Bearer {local_key(tmp_path)}"}
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401

    body = client.get(path, headers=key).json()
    assert [t["turn_id"] for t in body["turns"]] == ["T0", "T1", "T2"]
    assert body["turns"][2]["first_chunk_ms"] == 102.0
    assert body["turns"][2]["ts_epoch_ms"] == 3_000
    assert body["turns"][0]["event_uid"]
    assert body["now_ms"] > 0
    newest = client.get(path, params={"limit": 2}, headers=key).json()["turns"]
    assert [t["turn_id"] for t in newest] == ["T1", "T2"]
    later = client.get(path, params={"since": 2_000}, headers=key).json()["turns"]
    assert [t["turn_id"] for t in later] == ["T1", "T2"]
    assert client.get(path, params={"limit": 0}, headers=key).status_code == 422
    assert client.get(path, params={"limit": 501}, headers=key).status_code == 422


def test_without_a_reader_there_is_no_timing_route(tmp_path: Path) -> None:
    """A daemon that does not wire it answers 404 to every credential."""
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1", broadcaster=InherentBroadcaster(),
    ))
    require_local_key(app, functools.partial(local_key_matches, local_key(tmp_path)))
    local = TestClient(app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000))
    key = {"Authorization": f"Bearer {local_key(tmp_path)}"}
    assert local.get("/inherent/turn-timings", headers=key).status_code == 404


def _turn(model: str, wire: float, think: float, held: float | None = None) -> dict[str, Any]:
    """A fact shaped as the event carries it: one request that thought, then wrote."""
    sent = 200.0
    text = sent + wire + think
    return {
        "ts_epoch_ms": 1_760_000_000_000, "model": model, "channel": "inherent_ptt",
        "queue_ms": 3, "context_ms": sent, "first_text_ms": text,
        "first_chunk_ms": text + (held or 0) + 5, "tool_round_before_text": False,
        "holds": {} if held is None else {"answer_hold": held},
        "held_by": None if held is None else "answer_hold",
        "requests": [{
            "kind": "decision", "sent_ms": sent, "first_event_ms": sent + wire,
            "first_thinking_ms": sent + wire + 10, "first_text_ms": text,
            "input_tokens": 75_000, "cache_read_tokens": 70_000, "cache_write_tokens": 0,
            "output_tokens": 30,
        }],
    }


def test_the_report_reads_each_segment_and_gives_p50_and_p90_per_model() -> None:
    """Segments are differences of the fact's moments; the percentiles interpolate."""
    one = turn_timings.segments(_turn("sonnet", 800.0, 600.0))
    assert one == {
        "queue": 3, "context": 200.0, "earlier": 0.0, "wire": 800.0, "think": 590.0,
        "gen": 1400.0, "to_chunk": 5.0, "first_chunk": 1605.0, "held": None,
    }
    held = turn_timings.segments(_turn("sonnet", 800.0, 600.0, held=900.0))
    assert held["held"] == 900.0
    turns = [_turn("sonnet", 100.0 * n, 0.0) for n in range(1, 12)] + [_turn("luna", 50.0, 0.0)]
    text = turn_timings.report(turns)
    assert "sonnet: 11 turns" in text
    assert "luna: 1 turns" in text
    sonnet_lines = text.split("sonnet: 11 turns (ms)")[1].splitlines()
    p50 = next(line for line in sonnet_lines if line.startswith("p50"))
    p90 = next(line for line in sonnet_lines if line.startswith("p90"))
    cells = list(turn_timings.SEGMENTS)
    assert dict(zip(cells, p50.split()[1:], strict=True))["wire"] == "600"
    assert dict(zip(cells, p90.split()[1:], strict=True))["wire"] == "1000"
    assert json.dumps(turns)  # the facts stay plain data
