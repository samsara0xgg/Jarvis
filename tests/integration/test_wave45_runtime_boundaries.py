"""Takeover acceptance for runtime connection and request isolation boundaries."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.runtime import _build_vision_client, _wave5_input_flags
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_flat_request_client_retains_default_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """The immutable request client matches legacy flat-config credentials."""
    monkeypatch.setenv(provider.upper() + "_API_KEY", "test-placeholder")
    config = {"provider": provider, "model": "fixture-model"}
    legacy = LLMClient(config)
    factory = LLMSessionFactory(config)
    client = factory.create(factory.snapshot(), response_id="flat-compat")
    assert client._api_key == legacy._api_key == "test-placeholder"  # noqa: SLF001


def test_boot_vision_requests_are_thread_local_and_accounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent production vision adapters use fresh SDK and SQLite state."""
    barrier = threading.Barrier(2)
    clients: list[object] = []

    class Completions:
        def create(self, **_kwargs: object) -> object:
            barrier.wait(timeout=5)
            return SimpleNamespace(
                id="fixture-vision",
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="screen", tool_calls=None),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
            )

    def sdk(**_kwargs: object) -> object:
        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        clients.append(client)
        return client

    monkeypatch.setenv("VISION_FIXTURE_KEY", "placeholder")
    monkeypatch.setattr("openai.OpenAI", sdk)
    path = tmp_path / "vision.db"
    conn = open_event_log(path)
    config = {
        "llm": {
            "presets": {
                "vision": {
                    "provider": "openai",
                    "model": "gpt-4",
                    "api_key_env": "VISION_FIXTURE_KEY",
                }
            }
        }
    }
    client = _build_vision_client(
        config,
        "vision",
        event_log_path=path,
        pricing_path=tmp_path / "missing.json",
        account_cost=True,
    )
    assert client is not None
    image = tmp_path / "fixture.png"
    image.write_bytes(b"fixture-image")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.describe_image(image, question=None), range(2)))
    assert results == ["screen", "screen"]
    assert len(clients) == 2
    rows = conn.execute("SELECT payload_json FROM events WHERE type='cost.recorded'").fetchall()
    payloads = [json.loads(row[0]) for row in rows]
    assert len(payloads) == 2
    assert len({p["llm_request_id"] for p in payloads}) == 2
    assert all(p["kind"] == "vision" and p["disposition"] == "completed" for p in payloads)
    conn.close()


@pytest.mark.parametrize(
    "missing",
    [
        "transactional_event_append",
        "lifecycle_terminal_cas",
        "confirmation_dispatch_outbox",
        "exactly_once_cost_accounting",
        "response_run_lifecycle",
        "enabled",
    ],
)
def test_pump_requires_every_concurrency_dependency(missing: str) -> None:
    """Each missing safety dependency disables concurrent production decisions."""
    realtime: dict[str, Any] = {
        "enabled": True,
        "concurrency_safety": dict.fromkeys(
            [
                "transactional_event_append",
                "lifecycle_terminal_cas",
                "confirmation_dispatch_outbox",
                "exactly_once_cost_accounting",
            ],
            True,
        ),
        "response": {"response_run_lifecycle": True},
        "input": {"intent_pump": True},
    }
    assert _wave5_input_flags({"realtime": realtime}).intent_pump
    for block in (
        realtime,
        realtime["concurrency_safety"],
        realtime["response"],
    ):
        if missing in block:
            block[missing] = False
    assert not _wave5_input_flags({"realtime": realtime}).intent_pump
