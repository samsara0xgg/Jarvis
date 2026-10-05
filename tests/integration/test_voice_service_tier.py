"""ADR 0166: ``realtime.response.voice_service_tier`` and what it bills.

Only the provider socket is faked (a captured SDK body, a scripted /v1/responses peer,
a scripted chat-completions client); the client, the cost guard, ``decide()`` and the
Event Log are real, and prices come from the committed ``data/pricing.json``.
"""

# ruff: noqa: RUF001 — the scripted answers carry a fullwidth comma.
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis import runtime as runtime_module
from jarvis.decision import llm as llm_module
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.state.event_log import emit_event, open_event_log
from tests.canary._helpers import repo_root
from tests.integration.test_spoken_streaming import _frames, _Peer, _spoken, _spoken_runtime
from tests.integration.test_wire_routine_streaming import _drive, _intent, _payloads, _runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

_OPENAI = "https://api.openai.com/v1"
_TABLE = load_pricing_table(repo_root() / "data" / "pricing.json")


@pytest.fixture(autouse=True)
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setenv("TIER_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(runtime_module, "_labels_phases", lambda _base_url: True)


def _client(base_url: str, *, provider: str = "openai", api: str | None = None) -> LLMClient:
    return LLMClient({
        "provider": provider,
        "model": "gpt-6-luna",
        "base_url": base_url,
        "api_key_env": "TIER_FIXTURE_KEY",
        "max_tokens": 64,
        **({"api": api} if api else {}),
    })


def _captured_body(
    monkeypatch: pytest.MonkeyPatch, client: LLMClient, *, responses: bool, **knobs: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """The body ``stream_events`` captured at prepare time, without any network."""
    seen: list[dict[str, Any]] = []

    async def fake(
        _options: Mapping[str, Any], body: Mapping[str, Any], *, responses: bool,  # noqa: ARG001
    ) -> AsyncIterator[Mapping[str, Any]]:
        seen.append(dict(body))
        for frame in _completed_stream(None):
            yield frame

    monkeypatch.setattr(llm_module, "stream_openai", fake)
    handle = client.stream_events(
        messages=[{"role": "user", "content": "hi"}], system="s",
        on_settled=lambda _disposition: None, responses=responses, **knobs,
    )

    async def run() -> None:
        async for _event in handle.events():
            pass

    asyncio.run(run())
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("responses", [True, False])
def test_the_stream_body_carries_the_tier_only_on_openai_and_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, *, responses: bool,
) -> None:
    """Both stream bodies get the field on OpenAI's host; off or elsewhere they are unchanged."""
    openai = _client(_OPENAI)
    off = _captured_body(monkeypatch, openai, responses=responses)
    assert "service_tier" not in off
    # Switch off is the body as it was: asking for nothing changes no byte of it.
    assert json.dumps(off, sort_keys=True) == json.dumps(
        _captured_body(monkeypatch, openai, responses=responses, service_tier=None),
        sort_keys=True,
    )
    on = _captured_body(monkeypatch, openai, responses=responses, service_tier="priority")
    assert on == {**off, "service_tier": "priority"}
    for base_url in ("https://openrouter.ai/api/v1", "https://api.x.ai/v1"):
        other = _captured_body(
            monkeypatch, _client(base_url), responses=responses, service_tier="priority",
        )
        assert "service_tier" not in other


def test_batch_chat_sends_the_tier_to_openai_only() -> None:
    """The non-stream request carries it the same way, and reports back the served tier."""
    sent: list[dict[str, Any]] = []

    class _Responses:
        def create(self, **kwargs: Any) -> object:  # noqa: ANN401
            sent.append(kwargs)
            return SimpleNamespace(
                id="r", status="completed", output=[], output_text="ok", service_tier="fast",
                usage=SimpleNamespace(
                    input_tokens=10, output_tokens=2,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
            )

    for base_url, expected in ((_OPENAI, "priority"), ("https://openrouter.ai/api/v1", None)):
        client = _client(base_url, api="responses")
        client._openai_client = SimpleNamespace(responses=_Responses())  # noqa: SLF001
        result = client.chat(messages=[], system="s", service_tier="priority")
        assert sent[-1].get("service_tier") == expected
        assert result.service_tier == "fast"  # what the response reports wins
    client = _client(_OPENAI, api="responses")
    client._openai_client = SimpleNamespace(responses=_Responses())  # noqa: SLF001
    client.chat(messages=[], system="s")
    assert "service_tier" not in sent[-1]


def _completed_stream(tier: str | None) -> list[dict[str, Any]]:
    frames = _frames([("final_answer", "Hi.")])
    if tier is not None:
        frames[-1]["response"]["service_tier"] = tier
    return frames


def _cost_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reported: str | None, asked: str | None,
) -> dict[str, Any]:
    async def fake(
        _options: Mapping[str, Any], _body: Mapping[str, Any], *, responses: bool,  # noqa: ARG001
    ) -> AsyncIterator[Mapping[str, Any]]:
        for frame in _completed_stream(reported):
            yield frame

    monkeypatch.setattr(llm_module, "stream_openai", fake)
    tmp_path.mkdir()
    conn = open_event_log(tmp_path / "events.db")
    try:
        handle = CostRecorder(conn, pricing_table=_TABLE).stream_events(
            _client(_OPENAI), messages=[], system="s", kind="decision", turn_id="T",
            responses=True, service_tier=asked,
        )

        async def run() -> None:
            async for _event in handle.events():
                pass

        asyncio.run(run())
        (row,) = conn.execute(
            "SELECT payload_json FROM events WHERE type = 'cost.recorded'",
        ).fetchall()
    finally:
        conn.close()
    payload: dict[str, Any] = json.loads(row[0])
    return payload


def test_cost_uses_the_tier_the_response_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast prices at 2x; the reported tier beats the asked one, which is the fallback."""
    standard = _TABLE["gpt-6-luna"]
    fast = _TABLE["gpt-6-luna:fast"]
    # 12 input, 6 output, none cached (the scripted usage): the price of those tokens.
    assert fast["input"] == 2 * standard["input"]
    assert fast["output"] == 2 * standard["output"]
    at_fast = round(12 * fast["input"] / 1e6 + 6 * fast["output"] / 1e6, 6)
    at_standard = round(12 * standard["input"] / 1e6 + 6 * standard["output"] / 1e6, 6)

    reported = _cost_rows(tmp_path / "a", monkeypatch, reported="fast", asked="priority")
    assert (reported["service_tier"], reported["cost_usd"]) == ("fast", at_fast)
    assert at_fast == 2 * at_standard
    # Nothing reported: the tier that was asked for ("priority" is "fast") prices it.
    asked = _cost_rows(tmp_path / "b", monkeypatch, reported=None, asked="priority")
    assert (asked["service_tier"], asked["cost_usd"]) == ("priority", at_fast)
    # The provider says it served the default tier though priority was asked: standard price.
    served = _cost_rows(tmp_path / "c", monkeypatch, reported="default", asked="priority")
    assert (served["service_tier"], served["cost_usd"]) == ("default", at_standard)
    # Never asked, nothing reported: no field, standard price.
    plain = _cost_rows(tmp_path / "d", monkeypatch, reported=None, asked=None)
    assert "service_tier" not in plain
    assert plain["cost_usd"] == at_standard


def test_a_tier_with_no_row_bills_the_standard_rate() -> None:
    """Unknown tier or model: the standard row prices it, never a guess."""
    args = ("gpt-6-sol", 1000, 500, 0, 0, _TABLE)
    assert compute_cost_usd(*args, "fast") == compute_cost_usd(*args)


def _tiered(runtime: Any, tier: str | None) -> Any:  # noqa: ANN401
    realtime = {**runtime.config["realtime"]}
    response = {**realtime["response"], **({"voice_service_tier": tier} if tier else {})}
    realtime = {**realtime, "response": response}
    return replace(runtime, config={**runtime.config, "realtime": realtime})


def test_only_a_spoken_turn_gets_the_configured_tier(tmp_path: Path) -> None:
    """Spoken channels get the key's value; typed, GPT-Live and unset or odd values get none."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        def trigger(channel: str) -> Any:  # noqa: ANN401
            return emit_event(
                conn, type="surface.user_intent",
                payload={"transcript": "hi", "turn_id": "t", "channel": channel},
            )

        config = {"realtime": {"response": {"voice_service_tier": " priority "}}}
        pick = runtime_module._voice_service_tier  # noqa: SLF001
        assert pick(config, trigger("inherent_ptt")) == "priority"
        assert pick(config, trigger("cli_stdin")) is None
        assert pick(config, trigger("gpt_live")) is None
        unsets: list[dict[str, Any]] = [
            {},
            {"realtime": {"response": {}}},
            {"realtime": {"response": {"voice_service_tier": ""}}},
            {"realtime": {"response": {"voice_service_tier": 1}}},
        ]
        for unset in unsets:
            assert pick(unset, trigger("inherent_ptt")) is None
    finally:
        conn.close()


def test_every_request_of_a_spoken_stream_turn_carries_the_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first request and the follow-up after the tool call both send it, and are billed so."""
    monkeypatch.setattr(llm_module, "_is_openai_host", lambda _url: True)  # peer is localhost
    answer = "你还没有记过备忘录。"
    outputs = [[("commentary", "我看一下。"), ("call", "list_memos")], [("final_answer", answer)]]
    with _Peer(outputs) as peer:
        runtime = _tiered(_spoken_runtime(tmp_path, peer.url), "priority")
        _drive(runtime, _spoken(runtime.conn, "turn-tier", "我记过什么"))
    assert [body.get("service_tier") for _path, body in peer.requests] == ["priority", "priority"]
    tiers = [p["service_tier"] for p in _payloads(runtime.conn, "cost.recorded")]
    assert tiers == ["priority", "priority"]


def test_a_spoken_turn_with_the_switch_off_sends_no_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key set: the request body has no ``service_tier``."""
    monkeypatch.setattr(llm_module, "_is_openai_host", lambda _url: True)
    with _Peer([[("final_answer", "好的。")]]) as peer:
        runtime = _tiered(_spoken_runtime(tmp_path, peer.url), None)
        _drive(runtime, _spoken(runtime.conn, "turn-off", "你好"))
    ((_path, body),) = peer.requests
    assert "service_tier" not in body


def _batch_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, channel: str, answers: list[str],
) -> list[dict[str, Any]]:
    """A legacy batch turn on a scripted chat-completions client; returns each request's kwargs."""
    monkeypatch.setattr(llm_module, "_is_openai_host", lambda _url: True)
    sent: list[dict[str, Any]] = []

    class _Completions:
        def create(self, **kwargs: Any) -> object:  # noqa: ANN401
            sent.append(kwargs)
            text = answers[len(sent) - 1]
            return SimpleNamespace(
                id=f"r{len(sent)}", service_tier="fast",
                choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=text, tool_calls=None),
                )],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=2,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
            )

    monkeypatch.setattr(
        LLMClient, "_get_openai_client",
        lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=_Completions())),
    )
    runtime = _tiered(_runtime(tmp_path, "http://127.0.0.1:1/v1", routine=False), "priority")
    intent = (
        _intent(runtime.conn, "turn-batch", "你好")
        if channel == "cli_stdin"
        else emit_event(
            runtime.conn, type="surface.user_intent",
            payload={"transcript": "你好", "turn_id": "turn-batch", "channel": channel},
            correlation={"turn_id": "turn-batch"},
        )
    )
    _drive(runtime, intent)
    return sent


def test_the_batch_path_and_the_spoken_form_rewrite_follow_the_voice_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voice turn's batch request and its spoken-form rewrite carry it; a typed turn's do not."""
    # A written answer ("**" markup) draws the spoken-form rewrite: a second request.
    answers = ["**好的**，完成了。", "好的。"]
    sent = _batch_turn(tmp_path / "voice", monkeypatch, channel="inherent_ptt", answers=answers)
    assert len(sent) == 2
    assert [kwargs.get("service_tier") for kwargs in sent] == ["priority", "priority"]
    typed = _batch_turn(tmp_path / "typed", monkeypatch, channel="cli_stdin", answers=answers)
    assert typed
    assert all("service_tier" not in kwargs for kwargs in typed)
