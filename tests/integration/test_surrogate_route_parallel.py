"""ADR 0120 — Jev beside the model's request on the spoken stream path.

Real ``drive_turn``/``decide()`` and a real Event Log over the localhost
/v1/responses peer of ``test_spoken_streaming`` and the fake decisions endpoint
of ``test_surrogate_route``. With ``parallel`` on, the model's request is sent
at once and its first output waits for Jev: if Jev chose a function the request
is stopped with nothing of it shown and Tier 0's answer is given; otherwise the
model's turn goes on. With it off, Jev is asked first.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.surrogate_route import SurrogateRoute
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - autouse fixture, shared with the other spoken-route tests
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_spoken_structured import _reply
from tests.integration.test_surrogate_route import TABLE, _Jev, jev  # noqa: F401 - the fixture
from tests.integration.test_wire_routine_streaming import _drive, _payloads, _rows

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

_ANSWER = "我是模型的回答。"
_WORDS = "do you happen to know the time"


def _runtime(  # noqa: PLR0913 - the fixture's knobs, one per test setting
    tmp_path: Path, url: str, fake: _Jev, *, parallel: bool = True, structured: bool = False,
    timeout_ms: int = 400,
) -> JarvisRuntime:
    runtime = _spoken_runtime(tmp_path, url)
    flags = replace(runtime.response_flags, spoken_structured=structured)
    route = SurrogateRoute(
        model="typesafe/jev-1.13", min_confidence=0.9, timeout_ms=timeout_ms, url=fake.url,
        parallel=parallel,
    )
    return replace(runtime, response_flags=flags, tier0_table=TABLE, surrogate_route=route)


def _decided(runtime: JarvisRuntime) -> dict[str, Any]:
    (payload,) = _payloads(runtime.conn, "route.surrogate_decided")
    return payload


def _row_id(runtime: JarvisRuntime, kind: str) -> int:
    return _rows(runtime.conn, kind)[0][0]


@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured"])
def test_an_accepted_choice_stops_the_held_request_and_shows_nothing_of_it(
    tmp_path: Path, jev: _Jev, *, structured: bool,  # noqa: F811 - the fixture
) -> None:
    """Tier 0's answer comes long before the model's first event; none of the model's shows."""
    jev.delay_s = 0.8  # the client needs a moment to reach the peer; the deadline is longer here
    text = _reply(_ANSWER, "details") if structured else _ANSWER
    outputs = [[("call", "list_memos")], [("final_answer", text)]]
    with _Peer(outputs, first_event_delay_s=4.0) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, structured=structured, timeout_ms=2000)
        started = time.monotonic()
        result = _drive(runtime, _spoken(runtime.conn, "turn-a", _WORDS))
        elapsed = time.monotonic() - started
        assert len(peer.requests) == 1  # it was sent, in parallel
    conn = runtime.conn
    assert elapsed < 2.0  # not 4 s: the model's first event is never waited for
    assert result.response_plan.text != _ANSWER
    shown = "".join(p["text"] for p in _payloads(conn, "surface.response_chunk"))
    assert shown == result.response_plan.text  # Tier 0's answer, and only that
    assert "模型" not in shown
    assert [p["tool_name"] for p in _payloads(conn, "action.proposed")] == ["get_current_time"]
    assert [p["routed_by"] for p in _payloads(conn, "action.proposed")] == ["tier_0"]
    assert not any(p.get("phase") == "commentary" for p in _payloads(conn, "response.started"))
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["parallel"]) == (True, True, True)
    # Stopped before any event, the provider reports no usage: the cost is unknown, not zero.
    assert (event["aborted_cost_usd"], event["aborted_input_tokens"]) == (None, None)
    (spent,) = _payloads(conn, "cost.recorded")
    assert (spent["disposition"], spent["error_code"], spent["usage_status"]) == (
        "cancelled", "interrupted", "unavailable",
    )
    assert not _payloads(conn, "response.failed")
    assert not _payloads(conn, "response.cancelled")


@pytest.mark.parametrize("structured", [False, True], ids=["plain", "structured"])
def test_a_declined_choice_lets_the_model_go_on_unchanged(
    tmp_path: Path, jev: _Jev, *, structured: bool,  # noqa: F811 - the fixture
) -> None:
    """None: the model's sentences are spoken as ever, after Jev's event."""
    jev.choice = "none"
    text = _reply(_ANSWER, "") if structured else _ANSWER
    with _Peer([[("final_answer", text)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, structured=structured)
        result = _drive(runtime, _spoken(runtime.conn, "turn-d", _WORDS))
        assert len(peer.requests) == 1
    assert result.response_plan.text == _ANSWER
    chunks = [p["text"] for p in _payloads(runtime.conn, "surface.response_chunk")]
    assert "".join(chunks) == _ANSWER
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["choice"]) == (False, False, "none")
    assert _row_id(runtime, "route.surrogate_decided") < _row_id(runtime, "surface.response_chunk")


def test_a_tool_call_waits_for_jevs_decision(tmp_path: Path, jev: _Jev) -> None:  # noqa: F811
    """The model asks for a tool at once; nothing is dispatched until Jev has answered."""
    jev.choice = "none"
    jev.delay_s = 0.3
    outputs = [[("call", "list_memos")], [("final_answer", _ANSWER)]]
    with _Peer(outputs) as peer:
        runtime = _runtime(tmp_path, peer.url, jev)
        _drive(runtime, _spoken(runtime.conn, "turn-t", _WORDS))
    conn = runtime.conn
    assert [p["tool_name"] for p in _payloads(conn, "action.proposed")] == ["list_memos"]
    assert _row_id(runtime, "route.surrogate_decided") < _row_id(runtime, "action.proposed")
    assert _decided(runtime)["latency_ms"] >= 300


def test_a_slow_jev_holds_the_first_event_only_to_the_deadline(
    tmp_path: Path, jev: _Jev,  # noqa: F811 - the fixture
) -> None:
    """Jev at 1 s against a 0.4 s deadline: the model's output is held 0.4 s, then goes on."""
    jev.delay_s = 1.0
    with _Peer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev)
        started = time.monotonic()
        result = _drive(runtime, _spoken(runtime.conn, "turn-s", _WORDS))
        elapsed = time.monotonic() - started
    assert result.response_plan.text == _ANSWER
    event = _decided(runtime)
    assert (event["error"], event["accepted"], event["aborted"]) == ("timeout", False, False)
    assert 400 <= event["latency_ms"] <= 500
    assert elapsed < 0.9  # held to the deadline, not to Jev's 1 s


def test_a_model_slower_than_jev_is_never_delayed(tmp_path: Path, jev: _Jev) -> None:  # noqa: F811
    """Jev at 0.4 s, the model's first event at 1 s: the answer arrives at the model's pace."""
    jev.choice = "none"
    jev.delay_s = 0.3
    with _Peer([[("final_answer", _ANSWER)]], first_event_delay_s=1.0) as peer:
        runtime = _runtime(tmp_path, peer.url, jev)
        started = time.monotonic()
        _drive(runtime, _spoken(runtime.conn, "turn-m", _WORDS))
        elapsed = time.monotonic() - started
    assert 1.0 <= elapsed < 1.6


def test_with_parallel_off_jev_is_asked_first_and_the_model_never_sent(
    tmp_path: Path, jev: _Jev,  # noqa: F811 - the fixture
) -> None:
    """Sequential on the stream path: an accepted choice means no model request at all."""
    with _Peer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, parallel=False)
        result = _drive(runtime, _spoken(runtime.conn, "turn-p", _WORDS))
        assert peer.requests == []
    assert result.response_plan.text != _ANSWER
    event = _decided(runtime)
    assert (event["accepted"], event["aborted"], event["parallel"]) == (True, False, False)
    assert _payloads(runtime.conn, "cost.recorded") == []


def test_with_parallel_off_a_decline_sends_the_model_after_jev(
    tmp_path: Path, jev: _Jev,  # noqa: F811 - the fixture
) -> None:
    """Sequential: the model's request waits for Jev's answer, then everything is as before."""
    jev.choice = "none"
    with _Peer([[("final_answer", _ANSWER)]]) as peer:
        runtime = _runtime(tmp_path, peer.url, jev, parallel=False)
        result = _drive(runtime, _spoken(runtime.conn, "turn-q", _WORDS))
        assert len(peer.requests) == 1
    assert result.response_plan.text == _ANSWER
    assert _decided(runtime)["parallel"] is False
