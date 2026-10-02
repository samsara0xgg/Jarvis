"""ADR 0120 — Jev between the Tier 0 regex and the model, against a fake endpoint.

The endpoint is a local HTTP server that answers like OpenRouter's decisions
API (``answers.route.choice`` / ``confidence``, ``usage.cost``) or fails in a
chosen way. What is asserted is what the turn did: the Tier 0 function ran (the
action row says so) or the model was asked, and the call's event.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import decide
from jarvis.decision.stream_envelope import compose_envelope
from jarvis.decision.surrogate_route import OPTIONS, SurrogateRoute, tier0_hit
from jarvis.decision.tier0 import load_tier0_table
from jarvis.state.event_log import emit_event
from tests.canary._helpers import repo_root
from tests.integration.test_conversational_turn_no_gate_downgrade import _build_ctx

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from pathlib import Path

TABLE = load_tier0_table(repo_root() / "config" / "tier0_patterns.yaml")
LLM_ANSWER = "the model's own answer"
KEY = "test-key-not-a-secret"


class _Jev:
    """A fake decisions endpoint: records every request, answers by ``mode``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.mode = "ok"
        self.choice = "time"
        self.confidence = 0.95
        self.delay_s = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(size)))
                outer.headers.append(dict(self.headers))
                time.sleep(outer.delay_s)
                if outer.mode == "trickle":
                    # Every read lands inside the phase timeout; the whole never ends in time.
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    for _ in range(30):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                    return
                if outer.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    return
                body = (
                    b"not json"
                    if outer.mode == "garbage"
                    else json.dumps({
                        "answers": {"route": {
                            "choice": outer.choice, "confidence": outer.confidence,
                        }},
                        "usage": {"cost": 0.0000123},
                    }).encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/decisions"


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    fake = _Jev()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def _route(jev: _Jev, *, timeout_ms: int = 400) -> SurrogateRoute:
    return SurrogateRoute(
        model="typesafe/jev-1.13", min_confidence=0.9, timeout_ms=timeout_ms, url=jev.url,
    )


def _events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM events WHERE type = ? ORDER BY id", (kind,))
    return [json.loads(row[0]) for row in rows]


def _say(
    tmp_path: Path, words: str, route: SurrogateRoute | None, *,
    before: Callable[[sqlite3.Connection], None] | None = None,
) -> tuple[Any, sqlite3.Connection, Any]:
    """One utterance through decide() with the shipped Tier 0 table; (result, log, model)."""
    ctx, conn, llm = _build_ctx(tmp_path, draft_text=LLM_ANSWER)
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


def _ran_tier0(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [p for p in _events(conn, "action.proposed") if p.get("routed_by") == "tier_0"]


def test_a_confident_choice_runs_the_tier0_function(tmp_path: Path, jev: _Jev) -> None:
    """At 0.95 the row's tool runs by the Tier 0 path; the model is never asked."""
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev))
    (ran,) = _ran_tier0(conn)
    assert (ran["pattern_id"], ran["tool_name"], ran["caller_principal"]) == (
        "time_now", "get_current_time", "regex_router",
    )
    assert llm.chat_calls == 0
    assert result.response_plan is not None
    assert result.response_plan.text != LLM_ANSWER
    (event,) = _events(conn, "route.surrogate_decided")
    assert event == {
        "turn_id": "T9", "options_version": "1", "model": "typesafe/jev-1.13",
        "choice": "time", "confidence": 0.95, "accepted": True, "error": None,
        "cost_usd": 0.0000123, "latency_ms": event["latency_ms"],
    }
    assert 0 <= event["latency_ms"] < 400
    assert jev.headers[0]["Authorization"] == f"Bearer {KEY}"


def test_the_request_asks_one_choice_question_over_the_options(tmp_path: Path, jev: _Jev) -> None:
    """The body is the documented shape: model, state, one choice question."""
    _say(tmp_path, "do you have the time", _route(jev))
    (body,) = jev.requests
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == "User: do you have the time"
    question = body["questions"]["route"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {o.id for o in OPTIONS} | {"none"}
    assert question["criteria"]["none"] == "Anything else, including small talk and questions."


def test_repeat_says_the_last_answer_again(tmp_path: Path, jev: _Jev) -> None:
    """The repeat option says the last voice answer, as the regex shortcut does."""
    jev.choice = "repeat"
    result, conn, llm = _say(
        tmp_path, "could you go over that once more", _route(jev),
        before=lambda conn: _exchange(conn, "T1", "tell me a fact", "Water is wet.", ms=None),
    )
    assert result.response_plan is not None
    assert result.response_plan.text == "Water is wet."
    assert llm.chat_calls == 0
    assert _events(conn, "route.surrogate_decided")[0]["accepted"] is True


def test_repeat_with_nothing_said_yet_goes_to_the_model(tmp_path: Path, jev: _Jev) -> None:
    """Nothing to repeat is not a route: the model answers."""
    jev.choice = "repeat"
    result, conn, llm = _say(tmp_path, "could you go over that once more", _route(jev))
    assert llm.chat_calls == 1
    assert result.response_plan is not None
    assert result.response_plan.text == LLM_ANSWER
    assert _events(conn, "route.surrogate_decided")[0]["accepted"] is False


@pytest.mark.parametrize(
    ("setup", "choice", "confidence", "error"),
    [
        ({"confidence": 0.89}, "time", 0.89, None),
        ({"choice": "none", "confidence": 0.99}, "none", 0.99, None),
        ({"mode": "error"}, None, None, "http"),
        ({"mode": "garbage"}, None, None, "bad_json"),
        ({"delay_s": 0.6}, None, None, "timeout"),
    ],
    ids=["below_threshold", "none", "http_error", "bad_json", "timeout"],
)
def test_anything_else_falls_through_to_the_model(  # noqa: PLR0913 — one parameter per expected field.
    tmp_path: Path, jev: _Jev, setup: dict[str, Any],
    choice: str | None, confidence: float | None, error: str | None,
) -> None:
    """Below the bar, none, an HTTP error, bad JSON and a late answer all reach the model."""
    for name, value in setup.items():
        setattr(jev, name, value)
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev, timeout_ms=300))
    assert llm.chat_calls == 1
    assert result.response_plan is not None
    assert result.response_plan.text == LLM_ANSWER
    assert _ran_tier0(conn) == []
    (event,) = _events(conn, "route.surrogate_decided")
    assert (event["choice"], event["confidence"], event["error"], event["accepted"]) == (
        choice, confidence, error, False,
    )


def test_the_deadline_is_for_the_whole_call_not_each_phase(tmp_path: Path, jev: _Jev) -> None:
    """A reply that trickles in, each read within the phase timeout, is cut at 0.4 s."""
    jev.mode = "trickle"
    started = time.monotonic()
    result, conn, llm = _say(tmp_path, "do you have the time", _route(jev))
    assert result.response_plan is not None
    assert llm.chat_calls == 1
    (event,) = _events(conn, "route.surrogate_decided")
    assert (event["error"], event["accepted"]) == ("timeout", False)
    assert 400 <= event["latency_ms"] <= 450
    assert time.monotonic() - started < 2.0


def test_a_missing_key_never_calls_and_warns_once(
    tmp_path: Path, jev: _Jev, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """No key: no request, an event each turn, one warning in all."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    route = _route(jev)
    with caplog.at_level(logging.WARNING, logger="jarvis.decision.surrogate_route"):
        for turn in ("a", "b"):
            (tmp_path / turn).mkdir()
            result, conn, llm = _say(tmp_path / turn, "do you have the time", route)
            assert llm.chat_calls == 1
            assert result.response_plan is not None
            assert _events(conn, "route.surrogate_decided")[0]["error"] == "no_key"
    assert jev.requests == []
    assert len(caplog.records) == 1
    assert KEY not in caplog.text


def test_a_tier0_regex_match_never_asks_jev(tmp_path: Path, jev: _Jev) -> None:
    """Jev only hears what Tier 0 did not match."""
    _result, conn, llm = _say(tmp_path, "现在几点了", _route(jev))
    assert jev.requests == []
    assert _events(conn, "route.surrogate_decided") == []
    assert [p["pattern_id"] for p in _ran_tier0(conn)] == ["time_now"]
    assert llm.chat_calls == 0


def test_with_the_route_off_nothing_is_asked_or_recorded(tmp_path: Path, jev: _Jev) -> None:
    """No route configured: no request, no event, the model answers."""
    _result, conn, llm = _say(tmp_path, "do you have the time", None)
    assert jev.requests == []
    assert _events(conn, "route.surrogate_decided") == []
    assert llm.chat_calls == 1


def _exchange(
    conn: sqlite3.Connection, turn_id: str, said: str, answer: str, *, ms: int | None,
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
            **response, "phase": "final", "channel": "both",
            "text": compose_envelope(answer, "details"), "voice_text": answer,
        },
        correlation=turn,
    )


def _state_sent(
    tmp_path: Path, jev: _Jev, exchanges: list[tuple[str, str, int]],
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


def test_the_state_carries_the_last_two_exchanges(tmp_path: Path, jev: _Jev) -> None:
    """Her answers are cut to 200 characters; a third exchange back is not sent."""
    state = _state_sent(tmp_path, jev, [
        ("oldest", "oldest answer", 6),
        ("second last", "s" * 250, 2),
        ("last", "last answer", 1),
    ])
    assert state == "\n".join([
        "User: second last",
        "Assistant: " + "s" * 200 + "...",
        "User: last",
        "Assistant: last answer",
        "User: what about now",
    ])


def test_an_exchange_past_ten_minutes_is_left_out(tmp_path: Path, jev: _Jev) -> None:
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
    """The repo ships it off; an enabled block builds the route, a bad one stops boot."""
    import yaml  # noqa: PLC0415 — only this test reads the file

    from jarvis.runtime import RuntimeBootstrapError, _surrogate_route  # noqa: PLC0415

    path = repo_root() / "config" / "jarvis.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert _surrogate_route(config, path) is None
    block = config["realtime"]["surrogate_route"]
    assert (block["model"], block["min_confidence"], block["timeout_ms"]) == (
        "typesafe/jev-1.13", 0.9, 400,
    )
    block["enabled"] = True
    route = _surrogate_route(config, path)
    assert route is not None
    assert (route.model, route.min_confidence, route.timeout_ms) == ("typesafe/jev-1.13", 0.9, 400)
    block["timeout_ms"] = 0
    with pytest.raises(RuntimeBootstrapError):
        _surrogate_route(config, path)
