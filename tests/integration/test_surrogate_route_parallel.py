"""ADR 0122 — Jev beside the model's request on the spoken stream path.

Real ``drive_turn``/``decide()`` and a real Event Log over the localhost
/v1/responses peer of ``test_spoken_streaming`` and the fake decisions endpoint
of ``test_surrogate_route``. With ``parallel`` on, the model's request is sent
at once and its first output waits for Jev: if Jev chose a function the request
is stopped with nothing of it shown and Tier 0's answer is given; otherwise the
model's turn goes on. With it off, Jev is asked first.

Time here is the fake endpoint's clock, which the route and the runtime share: the endpoint
takes ``delay_s`` of it by moving it, so Jev's deadline passes exactly when a test says and
nothing is timed against the wall. What the model's peer does in real time is only ordered
with the endpoint's answer through events.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import anthropic  # noqa: F401 - the first request of a process imports the SDKs; not in a timing
import openai  # noqa: F401
import pytest

from jarvis.decision.surrogate_route import SurrogateRoute
from tests.support.routine import drive_streaming_turn, logged_payloads, logged_rows
from tests.support.runtime_config import reconfigure
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - autouse fixture, shared with the other spoken-route tests
    spoken_intent,
    spoken_runtime,
)
from tests.support.spoken_structured import structured_reply
from tests.support.surrogate import TABLE, SurrogateJev, jev  # noqa: F401 - the fixture

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

_ANSWER = "我是模型的回答。"
_WORDS = "do you happen to know the time"


def _runtime(  # noqa: PLR0913 - the fixture's knobs, one per test setting
    tmp_path: Path,
    url: str,
    fake: SurrogateJev,
    *,
    parallel: bool = True,
    structured: bool = False,
    timeout_ms: int = 400,
) -> JarvisRuntime:
    runtime = spoken_runtime(tmp_path, url)
    flags = replace(runtime.response_flags, spoken_structured=structured)
    route = SurrogateRoute(
        model="typesafe/jev-1.13",
        min_confidence=0.9,
        timeout_ms=timeout_ms,
        clock=fake.clock,
        url=fake.url,
        parallel=parallel,
    )
    # ADR 0272's hold on a spoken answer waits out its own 1.5 s of real time, which none of
    # these turns is about, so it is off here.
    realtime = runtime.config.raw["realtime"]
    response = {**realtime["response"], "answer_hold": {"seconds": 0}}
    return replace(
        reconfigure(runtime, realtime={**realtime, "response": response}),
        clock=fake.clock,
        response_flags=flags,
        tier0_table=TABLE,
        surrogate_route=route,
    )


def _decided(runtime: JarvisRuntime) -> dict[str, Any]:
    (payload,) = logged_payloads(runtime.conn, "route.surrogate_decided")
    return payload


def _row_id(runtime: JarvisRuntime, kind: str) -> int:
    return logged_rows(runtime.conn, kind)[0][0]


@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured"])
def test_an_accepted_choice_stops_the_held_request_and_shows_nothing_of_it(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811
    *,
    structured: bool,
) -> None:
    """Tier 0's answer comes without the model's first event; none of the model's shows."""
    text = structured_reply(_ANSWER, "details") if structured else _ANSWER
    outputs = [[("call", "kept")], [("final_answer", text)]]
    # The peer holds its first event for good (30 s is only its own guard), so the turn can only
    # end by Jev's answer, which waits until the model's request has reached the peer.
    with SpokenPeer(outputs, first_event_delay_s=30.0) as peer:
        jev.gate = peer.requested
        runtime = _runtime(tmp_path, peer.url, jev, structured=structured, timeout_ms=2000)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-a", _WORDS))
        assert len(peer.requests) == 1  # it was sent, in parallel
        assert peer.completed == 0  # and the model never got to say anything
    conn = runtime.conn
    assert result.response_plan.text != _ANSWER
    shown = "".join(p["text"] for p in logged_payloads(conn, "surface.response_chunk"))
    assert shown == result.response_plan.text  # Tier 0's answer, and only that
    assert "模型" not in shown
    assert [p["tool_name"] for p in logged_payloads(conn, "action.proposed")] == [
        "get_current_time"
    ]
    assert [p["routed_by"] for p in logged_payloads(conn, "action.proposed")] == ["tier_0"]
    assert not any(
        p.get("phase") == "commentary" for p in logged_payloads(conn, "response.started")
    )
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["parallel"]) == (True, True, True)
    # Stopped before any event, the provider reports no usage: the cost is unknown, not zero.
    assert (event["aborted_cost_usd"], event["aborted_input_tokens"]) == (None, None)
    (spent,) = logged_payloads(conn, "cost.recorded")
    assert (spent["disposition"], spent["error_code"], spent["usage_status"]) == (
        "cancelled",
        "interrupted",
        "unavailable",
    )
    assert not logged_payloads(conn, "response.failed")
    assert not logged_payloads(conn, "response.cancelled")


@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured"])
def test_a_declined_choice_lets_the_model_go_on_unchanged(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811
    *,
    structured: bool,
) -> None:
    """None: the model's sentences are spoken as ever, after Jev's event."""
    jev.choice = "none"
    text = structured_reply(_ANSWER, "") if structured else _ANSWER
    with SpokenPeer([[("final_answer", text)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, structured=structured)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-d", _WORDS))
        assert len(peer.requests) == 1
    assert result.response_plan.text == _ANSWER
    chunks = [p["text"] for p in logged_payloads(runtime.conn, "surface.response_chunk")]
    assert "".join(chunks) == _ANSWER
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["choice"]) == (False, False, "none")
    assert _row_id(runtime, "route.surrogate_decided") < _row_id(runtime, "surface.response_chunk")


def test_a_tool_call_waits_for_jevs_decision(tmp_path: Path, jev: SurrogateJev) -> None:  # noqa: F811
    """The model asks for a tool at once; nothing is dispatched until Jev has answered."""
    jev.choice = "none"
    jev.caller_waits = True  # Jev answers only once the turn is held for it
    jev.delay_s = 0.25
    outputs = [[("call", "kept")], [("final_answer", _ANSWER)]]
    with SpokenPeer(outputs) as peer:
        runtime = _runtime(tmp_path, peer.url, jev)
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-t", _WORDS))
    conn = runtime.conn
    assert [p["tool_name"] for p in logged_payloads(conn, "action.proposed")] == ["kept"]
    assert _row_id(runtime, "route.surrogate_decided") < _row_id(runtime, "action.proposed")
    assert _decided(runtime)["latency_ms"] == 250


def test_a_slow_jev_holds_the_first_event_only_to_the_deadline(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811 - the fixture
) -> None:
    """Jev at 1 s against a 0.4 s deadline: the model's output is held 0.4 s, then goes on."""
    jev.delay_s = 1.0
    jev.caller_gives_up = True  # its answer comes after the turn has stopped waiting for it
    with SpokenPeer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-s", _WORDS))
    assert result.response_plan.text == _ANSWER
    event = _decided(runtime)
    assert (event["error"], event["accepted"], event["aborted"]) == ("timeout", False, False)
    assert event["latency_ms"] == 400  # held to the deadline, not to Jev's 1 s


def test_a_model_slower_than_jev_is_never_delayed(tmp_path: Path, jev: SurrogateJev) -> None:  # noqa: F811
    """Jev answers before the model's first event: the turn takes Jev's time and adds none."""
    jev.choice = "none"
    jev.delay_s = 0.25
    with SpokenPeer([[("final_answer", _ANSWER)]], first_event_delay_s=30.0) as peer:
        jev.gate = peer.requested  # the model's request is in...
        jev.on_answered = peer.release_delayed  # ...and its first event follows Jev's answer
        runtime = _runtime(tmp_path, peer.url, jev)
        began = jev.clock.monotonic()
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-m", _WORDS))
    assert result.response_plan.text == _ANSWER
    event = _decided(runtime)
    assert (event["error"], event["latency_ms"]) == (None, 250)
    assert jev.clock.monotonic() - began == 0.25  # no wait for Jev's deadline on top of it
    assert _row_id(runtime, "route.surrogate_decided") < _row_id(runtime, "surface.response_chunk")


def test_with_parallel_off_jev_is_asked_first_and_the_model_never_sent(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811 - the fixture
) -> None:
    """Sequential on the stream path: an accepted choice means no model request at all."""
    with SpokenPeer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, parallel=False)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-p", _WORDS))
        assert peer.requests == []
    assert result.response_plan.text != _ANSWER
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["parallel"]) == (True, False, False)
    assert logged_payloads(runtime.conn, "cost.recorded") == []


def test_with_parallel_off_a_decline_sends_the_model_after_jev(
    tmp_path: Path,
    jev: SurrogateJev,  # noqa: F811 - the fixture
) -> None:
    """Sequential: the model's request waits for Jev's answer, then everything is as before."""
    jev.choice = "none"
    with SpokenPeer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, parallel=False)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-q", _WORDS))
        assert len(peer.requests) == 1
    assert result.response_plan.text == _ANSWER
    assert _decided(runtime)["parallel"] is False
