"""ADR 0122 — Jev between the Tier 0 regex and the model, against a fake endpoint.

The endpoint is a local HTTP server that answers like OpenRouter's decisions
API (``answers.route.choice`` / ``confidence``, ``usage.cost``) or fails in a
chosen way. What is asserted is what the turn did: the Tier 0 function ran (the
action row says so) or the model was asked, and the call's event.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import decide
from jarvis.decision.stream_envelope import compose_envelope
from jarvis.decision.surrogate_route import OPTIONS, JevLog, SurrogateRoute, tier0_hit
from jarvis.state.event_log import emit_event
from tests.canary._helpers import repo_root
from tests.support import surrogate as surrogate_support
from tests.support.clock import FakeClock
from tests.support.runtime_config import runtime_config
from tests.support.surrogate import (
    KEY,
    LLM_ANSWER,
    TABLE,
    SurrogateJev,
    logged_events,
    ran_tier0,
)
from tests.support.turn_context import build_decide_context

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

jev = surrogate_support.jev  # the fixture, found by name in this module


def _route(
    jev: SurrogateJev, *, timeout_ms: int = 400, log: JevLog | None = None
) -> SurrogateRoute:
    return SurrogateRoute(
        model="typesafe/jev-1.13",
        min_confidence=0.9,
        timeout_ms=timeout_ms,
        clock=jev.clock,
        url=jev.url,
        log=log,
    )


def _say(
    tmp_path: Path,
    words: str,
    route: SurrogateRoute | None,
    *,
    before: Callable[[sqlite3.Connection], None] | None = None,
) -> tuple[Any, sqlite3.Connection, Any]:
    """One utterance through decide() with the shipped Tier 0 table; (result, log, model)."""
    ctx, conn, llm = build_decide_context(tmp_path, draft_text=LLM_ANSWER)
    ctx = replace(ctx, tier0_table=TABLE, surrogate_route=route)
    if before is not None:
        before(conn)
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": "T9", "transcript": words, "channel": "inherent_wake"},
        correlation={"turn_id": "T9"},
    )
    return decide(trigger, ctx), conn, llm


def test_a_confident_choice_runs_the_tier0_function(tmp_path: Path, jev: SurrogateJev) -> None:
    """At 0.95 the row's tool runs by the Tier 0 path; the model is never asked."""
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev))
    (ran,) = ran_tier0(conn)
    assert (ran["pattern_id"], ran["tool_name"], ran["caller_principal"]) == (
        "time_now",
        "get_current_time",
        "regex_router",
    )
    assert len(llm.requests) == 0
    assert result.response_plan is not None
    assert result.response_plan.text != LLM_ANSWER
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert event == {
        "turn_id": "T9",
        "options_version": "3",
        "model": "typesafe/jev-1.13",
        "choice": "time",
        "confidence": 0.95,
        "accepted": True,
        "error": None,
        "cost_usd": 0.0000123,
        "latency_ms": event["latency_ms"],
        "parallel": False,
        "aborted": False,
        "aborted_cost_usd": None,
        "aborted_input_tokens": None,
    }
    assert event["latency_ms"] == 0  # the endpoint took none of the clock's time
    assert jev.headers[0]["Authorization"] == f"Bearer {KEY}"


def test_the_batch_path_stays_sequential_even_with_parallel_on(
    tmp_path: Path, jev: SurrogateJev
) -> None:
    """Only the spoken stream path overlaps the model's request; this one asks Jev first."""
    route = SurrogateRoute(
        model="typesafe/jev-1.13",
        min_confidence=0.9,
        timeout_ms=400,
        clock=jev.clock,
        url=jev.url,
        parallel=True,
    )
    _result, conn, llm = _say(tmp_path, "do you have the time", route)
    assert len(llm.requests) == 0
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert (event["accepted"], event["aborted"], event["parallel"]) == (True, False, True)


def test_the_request_asks_one_choice_question_over_the_options(
    tmp_path: Path, jev: SurrogateJev
) -> None:
    """The body is the documented shape: model, state, one choice question."""
    _say(tmp_path, "do you have the time", _route(jev))
    (body,) = jev.requests
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == "User: do you have the time"
    question = body["questions"]["route"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {o.id for o in OPTIONS} | {"none"}
    assert question["criteria"]["none"] == "Anything else, including small talk and questions."
    assert body["provider"] == {"zdr": True}


def test_with_zdr_off_the_request_names_no_provider_preference(
    tmp_path: Path, jev: SurrogateJev
) -> None:
    """The switch is the only thing that puts ``provider`` in the body."""
    route = SurrogateRoute(
        model="typesafe/jev-1.13",
        min_confidence=0.9,
        timeout_ms=400,
        clock=jev.clock,
        url=jev.url,
        zdr=False,
    )
    _say(tmp_path, "do you have the time", route)
    assert "provider" not in jev.requests[0]


def test_repeat_says_the_last_answer_again(tmp_path: Path, jev: SurrogateJev) -> None:
    """The repeat option says the last voice answer, as the regex shortcut does."""
    jev.choice = "repeat"
    result, conn, llm = _say(
        tmp_path,
        "could you go over that once more",
        _route(jev),
        before=lambda conn: _exchange(conn, "T1", "tell me a fact", "Water is wet.", ms=None),
    )
    assert result.response_plan is not None
    assert result.response_plan.text == "Water is wet."
    assert len(llm.requests) == 0
    assert logged_events(conn, "route.surrogate_decided")[0]["accepted"] is True


def test_repeat_with_nothing_said_yet_goes_to_the_model(tmp_path: Path, jev: SurrogateJev) -> None:
    """Nothing to repeat is not a route: the model answers."""
    jev.choice = "repeat"
    result, conn, llm = _say(tmp_path, "could you go over that once more", _route(jev))
    assert len(llm.requests) == 1
    assert result.response_plan is not None
    assert result.response_plan.text == LLM_ANSWER
    assert logged_events(conn, "route.surrogate_decided")[0]["accepted"] is False


@pytest.mark.parametrize(
    ("setup", "choice", "confidence", "error"),
    [
        ({"confidence": 0.89}, "time", 0.89, None),
        ({"choice": "none", "confidence": 0.99}, "none", 0.99, None),
        ({"mode": "error"}, None, None, "http"),
        ({"mode": "no_route"}, None, None, "no_zdr_route"),
        ({"mode": "garbage"}, None, None, "bad_json"),
        ({"delay_s": 0.6}, None, None, "timeout"),
    ],
    ids=["below_threshold", "none", "http_error", "no_zdr_route", "bad_json", "timeout"],
)
def test_anything_else_falls_through_to_the_model(  # noqa: PLR0913 — one parameter per expected field.
    tmp_path: Path,
    jev: SurrogateJev,
    setup: dict[str, Any],
    choice: str | None,
    confidence: float | None,
    error: str | None,
) -> None:
    """Below the bar, none, an HTTP error, bad JSON and a late answer all reach the model."""
    for name, value in setup.items():
        setattr(jev, name, value)
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev, timeout_ms=300))
    assert len(llm.requests) == 1
    assert result.response_plan is not None
    assert result.response_plan.text == LLM_ANSWER
    assert ran_tier0(conn) == []
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert (event["choice"], event["confidence"], event["error"], event["accepted"]) == (
        choice,
        confidence,
        error,
        False,
    )
    requests = jev.arrived(1)  # a timed-out call can return before the stand-in reads it
    assert len(requests) == 1  # never repeated without zdr
    assert requests[0]["provider"] == {"zdr": True}


def test_the_deadline_is_for_the_whole_call_not_each_phase(
    tmp_path: Path, jev: SurrogateJev
) -> None:
    """A reply that trickles in, each read within the phase timeout, is cut at the deadline."""
    jev.mode = "trickle"
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev))
    assert result.response_plan is not None
    assert len(llm.requests) == 1
    (event,) = logged_events(conn, "route.surrogate_decided")
    assert (event["error"], event["accepted"]) == ("timeout", False)
    assert event["latency_ms"] == 400  # the deadline, not the 3 s the reply would have taken


def test_a_missing_key_never_calls_and_warns_once(
    tmp_path: Path,
    jev: SurrogateJev,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No key: no request, an event each turn, one warning in all."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    route = _route(jev)
    with caplog.at_level(logging.WARNING, logger="jarvis.decision.surrogate_route"):
        for turn in ("a", "b"):
            (tmp_path / turn).mkdir()
            result, conn, llm = _say(tmp_path / turn, "do you have the time", route)
            assert len(llm.requests) == 1
            assert result.response_plan is not None
            assert logged_events(conn, "route.surrogate_decided")[0]["error"] == "no_key"
    assert jev.requests == []
    assert len(caplog.records) == 1
    assert KEY not in caplog.text


def _logged(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_each_route_call_is_a_line_in_the_local_dataset(
    tmp_path: Path,
    jev: SurrogateJev,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0128: what Jev saw and said, tagged with the turn id; a failed call names its error."""
    path = tmp_path / "jev" / "decisions.jsonl"
    route = _route(jev, timeout_ms=300, log=JevLog(path))
    _say(tmp_path, "do you have the time", route)
    (line,) = _logged(path)
    stamp = datetime.fromisoformat(line.pop("ts"))
    assert stamp.utcoffset() == timedelta(0)
    assert line.pop("latency_ms") == 0
    assert line == {
        "kind": "call",
        "use": "route",
        "ref": "T9",
        "model": "typesafe/jev-1.13",
        "state": "User: do you have the time",
        "questions": jev.requests[0]["questions"],
        "answers": {"route": {"choice": "time", "confidence": 0.95}},
        "error": None,
        "cost_usd": 0.0000123,
    }
    assert line["questions"]["route"]["type"] == "choice"
    for mode, error in (("error", "http"), ("no_route", "no_zdr_route"), ("garbage", "bad_json")):
        jev.mode = mode
        (tmp_path / mode).mkdir()
        _say(tmp_path / mode, "do you have the time", route)
        failed = _logged(path)[-1]
        assert (failed["use"], failed["ref"], failed["error"]) == ("route", "T9", error)
        assert (failed["answers"], failed["cost_usd"]) == (None, None)
        assert failed["state"] == "User: do you have the time"
    assert len(_logged(path)) == 4
    monkeypatch.delenv("OPENROUTER_API_KEY")  # nothing is sent, so nothing is kept
    (tmp_path / "nokey").mkdir()
    _say(tmp_path / "nokey", "do you have the time", route)
    assert len(_logged(path)) == 4
    text = path.read_text(encoding="utf-8").lower()
    assert KEY not in text
    assert "authorization" not in text
    assert "bearer" not in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_dataset_that_cannot_be_written_changes_no_decision(
    tmp_path: Path,
    jev: SurrogateJev,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable path is one warning in all; the turn runs the function as it would."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    route = _route(jev, log=JevLog(blocker / "decisions.jsonl"))
    with caplog.at_level(logging.WARNING, logger="jarvis.decision.surrogate_route"):
        for turn in ("a", "b"):
            (tmp_path / turn).mkdir()
            _result, conn, llm = _say(tmp_path / turn, "do you have the time", route)
            assert [p["pattern_id"] for p in ran_tier0(conn)] == ["time_now"]
            assert len(llm.requests) == 0
    assert len(caplog.records) == 1
    assert "jev log" in caplog.text
    assert KEY not in caplog.text


def test_the_dataset_is_on_unless_the_config_turns_it_off(tmp_path: Path) -> None:
    """``jev_log`` ships on, under the runtime root; ``enabled: false`` builds no writer."""
    import yaml  # noqa: PLC0415 — only this test reads the file

    from jarvis.runtime.bootstrap.jev import build_jev_log  # noqa: PLC0415

    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    assert config["jev_log"] == {"enabled": True}
    log = build_jev_log(runtime_config(config).jev, tmp_path)
    assert log is not None
    assert not (tmp_path / "jev").exists()  # made at the first line
    log.append("call", "route", "T1")
    assert len(_logged(tmp_path / "jev" / "decisions.jsonl")) == 1
    assert build_jev_log(runtime_config({}).jev, tmp_path) is not None
    assert build_jev_log(runtime_config({"jev_log": {"enabled": False}}).jev, tmp_path) is None


def test_a_tier0_regex_match_never_asks_jev(tmp_path: Path, jev: SurrogateJev) -> None:
    """Jev only hears what Tier 0 did not match."""
    _result, conn, llm = _say(tmp_path, "现在几点了", _route(jev))
    assert jev.requests == []
    assert logged_events(conn, "route.surrogate_decided") == []
    assert [p["pattern_id"] for p in ran_tier0(conn)] == ["time_now"]
    assert len(llm.requests) == 0


def test_with_the_route_off_nothing_is_asked_or_recorded(tmp_path: Path, jev: SurrogateJev) -> None:
    """No route configured: no request, no event, the model answers."""
    _result, conn, llm = _say(tmp_path, "do you have the time", None)
    assert jev.requests == []
    assert logged_events(conn, "route.surrogate_decided") == []
    assert len(llm.requests) == 1


def _exchange(
    conn: sqlite3.Connection,
    turn_id: str,
    said: str,
    answer: str,
    *,
    ms: int | None,
) -> None:
    """Allen spoke and the answer's voice text was written; ``ms`` stamps the words."""
    turn = {"turn_id": turn_id}
    emit_event(
        conn,
        type="utterance.received",
        payload={**turn, "transcript": said, "channel": "inherent_wake"},
        correlation=turn,
        ts_epoch_ms=ms,
    )
    response = {**turn, "response_id": f"R{turn_id}", "response_group_id": f"G{turn_id}"}
    emit_event(
        conn,
        type="surface.response_open",
        payload={**response, "phase": "final", "kind": "text", "query": "q", "channel": "both"},
        correlation=turn,
    )
    emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            **response,
            "phase": "final",
            "channel": "both",
            "text": compose_envelope(answer, "details"),
            "voice_text": answer,
        },
        correlation=turn,
    )


def _state_sent(
    tmp_path: Path,
    jev: SurrogateJev,
    exchanges: list[tuple[str, str, int]],
) -> str:
    """Ask about "what about now" after ``(words, answer, minutes ago)`` exchanges."""
    now = int(time.time() * 1000)

    def history(conn: sqlite3.Connection) -> None:
        for n, (said, answer, minutes) in enumerate(exchanges):
            _exchange(conn, f"T{n}", said, answer, ms=now - int(minutes * 60_000))

    jev.choice = "none"
    _say(tmp_path, "what about now", _route(jev), before=history)
    (body,) = jev.requests
    return str(body["state"])


def test_the_state_carries_the_last_two_exchanges(tmp_path: Path, jev: SurrogateJev) -> None:
    """Her answers are cut to 200 characters; a third exchange back is not sent."""
    state = _state_sent(
        tmp_path,
        jev,
        [
            ("oldest", "oldest answer", 6),
            ("second last", "s" * 250, 2),
            ("last", "last answer", 1),
        ],
    )
    assert state == "\n".join(
        [
            "User: second last",
            "Assistant: " + "s" * 200 + "...",
            "User: last",
            "Assistant: last answer",
            "User: what about now",
        ]
    )


def test_an_exchange_past_ten_minutes_is_left_out(tmp_path: Path, jev: SurrogateJev) -> None:
    """Context older than ten minutes is not context."""
    state = _state_sent(tmp_path, jev, [("long ago", "old answer", 11), ("last", "last answer", 1)])
    assert state == "User: last\nAssistant: last answer\nUser: what about now"


def test_the_options_are_the_tier0_rows_that_take_no_argument() -> None:
    """A row added to or removed from tier0_patterns.yaml must be seen here."""
    plain = [row for row in TABLE if not row.arg_template and row.regex.groups == 0]
    options = [o for o in OPTIONS if o.pattern_id is not None]
    hits = [tier0_hit(o, TABLE) for o in options]
    assert all(hits)
    assert {row.response_template for row in plain} == {h.response_template for h in hits if h}
    assert {row.tool_name for row in plain} == {h.tool_name for h in hits if h}


def test_the_shipped_config_is_off_and_enabling_it_reads_the_block() -> None:
    """The repo ships it off; an enabled block builds the route (a bad one is refused at load)."""
    import yaml  # noqa: PLC0415 — only this test reads the file

    from jarvis.runtime.bootstrap.jev import _surrogate_route  # noqa: PLC0415

    path = repo_root() / "config" / "jarvis.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    clock = FakeClock()
    assert _surrogate_route(runtime_config(config).jev.surrogate_route, clock) is None
    block = config["realtime"]["surrogate_route"]
    assert (block["model"], block["min_confidence"], block["timeout_ms"]) == (
        "typesafe/jev-1.13",
        0.95,
        400,
    )
    block["enabled"] = True
    route = _surrogate_route(runtime_config(config).jev.surrogate_route, clock)
    assert route is not None
    assert (route.model, route.min_confidence, route.timeout_ms) == ("typesafe/jev-1.13", 0.95, 400)
    assert (route.parallel, route.zdr) == (True, True)
    block["zdr"] = False
    off = _surrogate_route(runtime_config(config).jev.surrogate_route, clock)
    assert off is not None
    assert off.zdr is False
