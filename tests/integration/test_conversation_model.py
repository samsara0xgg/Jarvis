"""ADR 0297 — the conversation's model and effort are a live setting, and opus-fast is fast mode.

No paid call: the Anthropic SDK talks to a localhost peer that records the request, the 429 is a
scripted stream, and the setting is checked through the page's own read/update.
"""

# ruff: noqa: E501 — scripted bodies and expected values read best one entry to a line
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import llm as llm_module
from jarvis.decision.llm_anthropic import FAST_BETA, FILES_BETA, files_headers, request_fields
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.llm_stream import LLMUsageCompleted
from jarvis.runtime.settings import Settings, apply_settings
from jarvis.runtime.settings_owner import owner_of
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from tests.canary._helpers import repo_root
from tests.integration.test_anthropic_adaptation import _reply, _run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

PRESETS: dict[str, Any] = {
    "luna": {"model": "gpt-6-luna", "reasoning_effort": "none", "base_url": "https://api.openai.com/v1"},
    "luna-think": {"model": "gpt-6-luna", "api": "responses", "reasoning_effort": "medium"},
    "sonnet": {"provider": "anthropic", "model": "claude-sonnet-5-5", "reasoning_effort": "low"},
    "sonnet-high": {"provider": "anthropic", "model": "claude-sonnet-5-5", "reasoning_effort": "high"},
    "haiku": {"provider": "anthropic", "model": "claude-haiku-5-5", "reasoning_effort": "medium"},
    "opus": {"provider": "anthropic", "model": "claude-opus-5-5", "reasoning_effort": "low"},
    "opus-fast": {
        "provider": "anthropic", "model": "claude-opus-5-5", "reasoning_effort": "low",
        "api_key_env": "OPUS_FIXTURE_KEY", "speed": "fast",
    },
}
LLM: dict[str, Any] = {
    "default_preset": "sonnet", "think": {"preset": "sonnet-high", "on_words": "think"},
    "presets": PRESETS,
}


def _page(root: Path, saved: Mapping[str, Any] | None = None) -> tuple[Settings, LLMSessionFactory]:
    """The brain's page and the factory the runtime wires to it (``bootstrap_runtime_app``)."""
    if saved:
        (root / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
    config = apply_settings({"llm": LLM}, root)
    page = Settings(root, config, lambda _kind: [])
    factory = LLMSessionFactory(config["llm"])
    page.on_conversation = factory.choose
    if chosen := page.conversation_saved():
        factory.choose(*chosen)
    return page, factory


# --- the setting ----------------------------------------------------------------------------


def test_nothing_saved_keeps_todays_conversation(tmp_path: Path) -> None:
    """Sonnet at its own effort, the same snapshot as before the setting existed."""
    page, factory = _page(tmp_path)
    body = page.read()
    assert body["values"]["conversation_model"] == "sonnet"
    assert body["values"]["conversation_effort"] == "low"
    assert body["values"]["model_conversation"] == "claude-sonnet-5-5"
    assert body["options"]["conversation_model"] == ["opus-fast", "opus", "sonnet", "haiku", "luna"]
    assert body["options"]["conversation_effort"]["luna"] == ["none", "low", "medium", "high"]
    assert body["options"]["conversation_effort"]["opus"] == ["low", "medium", "high"]
    assert factory.snapshot(None) == factory.snapshot("sonnet")


def test_a_pick_applies_to_the_next_turn_and_never_waits_for_a_restart(tmp_path: Path) -> None:
    """update() saves, calls the factory, shows the new live model id, restart_pending stays false."""
    page, factory = _page(tmp_path)
    body = page.update({"conversation_model": "opus-fast"})
    assert body["restart_pending"] is False
    assert body["values"]["conversation_model"] == "opus-fast"
    assert body["values"]["model_conversation"] == "claude-opus-5-5"
    snapshot = factory.snapshot(None)
    assert (snapshot.preset_name, snapshot.model, snapshot.speed) == ("opus-fast", "claude-opus-5-5", "fast")
    assert snapshot.reasoning_effort == "low"
    body = page.update({"conversation_effort": "high"})
    assert body["restart_pending"] is False
    assert factory.snapshot(None).reasoning_effort == "high"
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "conversation_model": "opus-fast", "conversation_effort": "high",
    }


def test_the_choice_survives_a_restart(tmp_path: Path) -> None:
    """The next boot reads settings.json: same pick, nothing pending, the factory already on it."""
    _page(tmp_path)[0].update({"conversation_model": "haiku", "conversation_effort": "medium"})
    page, factory = _page(tmp_path)
    body = page.read()
    assert (body["values"]["conversation_model"], body["values"]["conversation_effort"]) == ("haiku", "medium")
    assert body["restart_pending"] is False
    assert factory.snapshot(None).model == "claude-haiku-5-5"


def test_a_model_alone_resets_an_effort_it_does_not_take(tmp_path: Path) -> None:
    """Luna "none" is not a Claude effort; picking sonnet falls back to its own low."""
    page, factory = _page(tmp_path)
    page.update({"conversation_model": "luna", "conversation_effort": "none"})
    body = page.update({"conversation_model": "sonnet"})
    assert body["values"]["conversation_effort"] == "low"
    assert factory.snapshot(None).reasoning_effort == "low"


@pytest.mark.parametrize(
    "changes",
    [
        {"conversation_model": "gpt-9"},
        {"conversation_effort": "max"},
        {"conversation_effort": "none"},  # sonnet is the current model
        {"conversation_model": "opus", "conversation_effort": "none"},
    ],
)
def test_a_bad_pick_is_refused_and_saves_nothing(tmp_path: Path, changes: dict[str, str]) -> None:
    """ValueError (the route answers 400) and no file."""
    page = _page(tmp_path)[0]
    with pytest.raises(ValueError, match="conversation_"):
        page.update(changes)
    assert not (tmp_path / "settings.json").exists()


def test_the_brain_owns_both_keys() -> None:
    """So a terminal forwards the desktop page's write to the brain (ADR 0227)."""
    assert owner_of("llm.conversation_model") == owner_of("llm.conversation_effort") == "brain"


# --- the factory ----------------------------------------------------------------------------


def test_only_the_conversation_follows_the_choice(tmp_path: Path) -> None:
    """snapshot(None) and the think turn change; a named preset is always itself."""
    page, factory = _page(tmp_path)
    page.update({"conversation_model": "opus", "conversation_effort": "medium"})
    assert factory.snapshot(None).model == "claude-opus-5-5"
    assert factory.snapshot("sonnet").model == "claude-sonnet-5-5"
    assert factory.snapshot("sonnet-high").reasoning_effort == "high"
    think = factory.snapshot("sonnet-high", think=True)
    assert (think.preset_name, think.model, think.reasoning_effort) == ("opus", "claude-opus-5-5", "high")
    assert factory.default_preset == "sonnet"


def test_without_a_choice_the_think_turn_is_the_configured_preset(tmp_path: Path) -> None:
    """No saved pick: ``llm.think.preset`` is the think turn, as ADR 0108 has it."""
    factory = _page(tmp_path)[1]
    assert factory.snapshot("sonnet-high", think=True).preset_name == "sonnet-high"


def test_luna_thinks_on_the_responses_api_only_when_asked_to(tmp_path: Path) -> None:
    """Effort none stays chat/completions; any other effort, and the think turn, use /v1/responses."""
    page, factory = _page(tmp_path)
    page.update({"conversation_model": "luna"})
    assert factory.snapshot(None).api is None
    page.update({"conversation_effort": "medium"})
    snapshot = factory.snapshot(None)
    assert (snapshot.api, snapshot.reasoning_effort, snapshot.model) == ("responses", "medium", "gpt-6-luna")
    page.update({"conversation_effort": "none"})
    think = factory.snapshot("sonnet-high", think=True)
    assert (think.api, think.reasoning_effort) == ("responses", "high")


def test_fast_mode_changes_only_its_own_snapshot_hash() -> None:
    """A preset with no speed hashes as it did; speed is part of the identity of one that has it."""
    factory = LLMSessionFactory(LLM)
    assert factory.snapshot("opus").snapshot_hash != factory.snapshot("opus-fast").snapshot_hash
    assert factory.snapshot("opus").speed is None


# --- fast mode on the wire ------------------------------------------------------------------


def test_fast_mode_is_a_body_field_and_a_beta_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming request of the conversation carries speed and the beta; usage.speed prices it."""
    monkeypatch.setenv("OPUS_FIXTURE_KEY", "synthetic")
    factory = LLMSessionFactory(LLM)
    client = factory.create(factory.snapshot("opus-fast"), response_id="r1")
    frames = _reply({"type": "text", "text": "好。"}, stop="end_turn")
    frames[0]["message"]["usage"]["speed"] = "fast"
    peer, events = asyncio.run(_run(
        client, frames, messages=[{"role": "user", "content": "hi"}], system="S", tools=None,
    ))
    assert peer.body["speed"] == "fast"
    assert peer.headers["anthropic-beta"] == FAST_BETA
    assert peer.body["model"] == "claude-opus-5-5"
    usage = next(event for event in events if isinstance(event, LLMUsageCompleted))
    assert usage.service_tier == "fast"


def test_a_standard_request_sends_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    """No speed field, no beta header, no tier on a request that did not ask."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    factory = LLMSessionFactory(LLM)
    client = factory.create(factory.snapshot("opus"), response_id="r2")
    frames = _reply({"type": "text", "text": "好。"}, stop="end_turn")
    peer, events = asyncio.run(_run(
        client, frames, messages=[{"role": "user", "content": "hi"}], system="S", tools=None,
    ))
    assert "speed" not in peer.body
    assert "anthropic-beta" not in peer.headers
    assert next(e for e in events if isinstance(e, LLMUsageCompleted)).service_tier is None


def test_the_fast_beta_joins_the_files_beta() -> None:
    """One anthropic-beta header holds both, comma separated."""
    picture = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "file", "file_id": "file_1"}},
    ]}]
    body = request_fields(messages=[], system="S", tools=None, max_tokens=8, reasoning_effort="low", fast=True)
    assert files_headers(body) == {"anthropic-beta": FAST_BETA}
    assert files_headers({**body, "messages": picture}) == {
        "anthropic-beta": f"{FILES_BETA},{FAST_BETA}",
    }


def test_opus_55_takes_adaptive_thinking_only() -> None:
    """No thinking disabled, no forced tool call: the same rule as Sonnet 5.5."""
    body = request_fields(
        model="claude-opus-5-5", messages=[{"role": "user", "content": "x"}], system="S",
        tools=[{"name": "t", "input_schema": {"type": "object"}}], max_tokens=8,
        reasoning_effort="low", tool_choice="t",
    )
    assert "tool_choice" not in body
    assert body["thinking"] == {"type": "adaptive"}


class _RateLimited(Exception):  # noqa: N818 — stands in for the SDK's 429
    status_code = 429


def test_a_fast_request_rejected_with_429_is_sent_again_at_standard_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before its first chunk only, once; the second body has no speed."""
    sent: list[dict[str, Any]] = []

    async def stream(_options: Any, body: dict[str, Any], **_kw: Any) -> AsyncIterator[dict[str, Any]]:  # noqa: ANN401
        sent.append(body)
        if "speed" in body or body.get("always"):
            raise _RateLimited
        yield {"type": "message_start"}

    monkeypatch.setattr(llm_module, "stream_openai", stream)

    async def run(body: dict[str, Any]) -> list[Any]:
        return [chunk async for chunk in llm_module._anthropic_events({}, body)]  # noqa: SLF001

    assert asyncio.run(run({"model": "m", "speed": "fast"})) == [{"type": "message_start"}]
    assert ["speed" in body for body in sent] == [True, False]
    sent.clear()
    with pytest.raises(_RateLimited):  # a standard-speed 429 is not ours to hide
        asyncio.run(run({"model": "m", "always": True}))
    assert len(sent) == 1


# --- pricing --------------------------------------------------------------------------------


def test_fast_usage_bills_on_the_fast_row() -> None:
    """$8/$40 in and out, cache read $0.40, cache write $10 per MTok."""
    table = load_pricing_table(repo_root() / "data" / "pricing.json")
    standard = compute_cost_usd("claude-opus-5-5", 1_000_000, 1_000_000, 0, 0, table)
    fast = compute_cost_usd("claude-opus-5-5", 1_000_000, 1_000_000, 0, 0, table, "fast")
    assert (standard, fast) == (24.0, 48.0)
    cached = compute_cost_usd("claude-opus-5-5", 3_000_000, 0, 1_000_000, 1_000_000, table, "fast")
    assert cached == 8.0 + 0.4 + 10.0
