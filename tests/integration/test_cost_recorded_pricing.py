"""Acceptance: a shipped preset's turn commits a real cost_usd, not null.

The observable is the committed artifact, not a dict lookup: this drives a
scripted provider socket through the real ``CostRecorder`` with the real
committed ``data/pricing.json``, then SELECTs the ``cost.recorded`` row back
out of the on-disk Event Log and checks the amount against the token counts
the fake reported.

Only the provider socket is faked. ``CostRecorder._known_cost`` calls the
same ``compute_cost_usd`` the daemon calls, and the pricing table is loaded
from the same committed file ``jarvis.decision._pricing_table`` and
``JarvisRuntime`` load.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import open_event_log
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from pathlib import Path

# Token counts the scripted provider reports; the expected cost is derived
# from these and the committed deepseek-v4-flash rates, never hardcoded.
PROMPT_TOKENS = 1000
COMPLETION_TOKENS = 500
CACHED_TOKENS = 200

_LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "fast",
    "presets": {
        "fast": {
            "provider": "openai",
            "model": "deepseek-v4-flash",
            "base_url": "https://example.invalid/fast",
            "max_tokens": 64,
        },
    },
}


class _FakeCompletions:
    """Chat-completions stand-in reporting fixed usage for one turn."""

    def create(self, **_kwargs: object) -> object:
        """Return one batch completion with the scripted token counts."""
        return SimpleNamespace(
            id="resp-deepseek",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=PROMPT_TOKENS,
                completion_tokens=COMPLETION_TOKENS,
                prompt_tokens_details=SimpleNamespace(cached_tokens=CACHED_TOKENS),
            ),
        )


def test_cost_recorded_carries_a_real_amount_for_a_shipped_preset_model(tmp_path: Path) -> None:
    """A deepseek-v4-flash turn commits a numeric cost_usd matching its tokens."""
    pricing_table = load_pricing_table(repo_root() / "data" / "pricing.json")
    rates = pricing_table["deepseek-v4-flash"]

    factory = LLMSessionFactory(_LLM_CONFIG)
    snapshot = factory.snapshot("fast")
    client = factory.create(snapshot, response_id=new_response_id())
    client._openai_client = SimpleNamespace(  # noqa: SLF001 - provider fixture seam
        chat=SimpleNamespace(completions=_FakeCompletions()),
    )

    conn = open_event_log(tmp_path / "cost.db")
    try:
        result = CostRecorder(conn, pricing_table=pricing_table).chat(
            client,
            messages=[{"role": "user", "content": "hi"}],
            system="",
            kind="decision",
            turn_id="T-cost",
        )
        assert result.model_used == "deepseek-v4-flash"

        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = 'cost.recorded'",
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["tokens_in"] == PROMPT_TOKENS
    assert payload["tokens_out"] == COMPLETION_TOKENS
    assert payload["cache_read_in"] == CACHED_TOKENS

    cost = payload["cost_usd"]
    assert cost is not None
    assert isinstance(cost, float)
    assert cost > 0

    expected = round(
        (PROMPT_TOKENS - CACHED_TOKENS) * rates["input"] / 1_000_000
        + COMPLETION_TOKENS * rates["output"] / 1_000_000
        + CACHED_TOKENS * rates["cache_read"] / 1_000_000,
        6,
    )
    assert cost == expected
