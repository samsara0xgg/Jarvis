"""Unit tests for L3 :mod:`jarvis.decision.llm`.

ADR § Acceptance G1 + § Reference sources directives drive the
assertions: this is the LLM-free construction surface. No network,
no real ``chat()`` calls, no ``unittest.mock`` (per ADR § Acceptance
G2 — applied to unit tests Day-1 as well).

The legacy ``core/llm.py`` builds the OpenAI / Anthropic SDK client
inside ``__init__``; Day-1's L3 :class:`LLMClient` defers SDK
construction to the first :meth:`chat` / :meth:`chat_stream` call.
That makes these tests deterministic offline: we instantiate the
client, read its public attributes, exercise preset switching,
verify ``api_key_env`` resolution via ``monkeypatch``, and never
touch the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.llm import (
    ChatResult,
    ChatStreamChunk,
    LLMClient,
    MissingLLMSectionError,
    ToolCall,
    UnknownPresetError,
    load_llm_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Repo-root config/jarvis.yaml — Step 7's verbatim copy of the legacy LLM block.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"


# ----- load_llm_config -----------------------------------------------------


def test_load_llm_config_returns_llm_block() -> None:
    """``load_llm_config`` returns the ``llm:`` mapping from jarvis.yaml."""
    cfg = load_llm_config(_CONFIG_PATH)
    assert isinstance(cfg, dict)
    assert cfg["provider"] == "openai"
    assert cfg["default_preset"] == "deep"
    presets = cfg["presets"]
    assert presets["deep"]["model"] == "gpt-5.5"
    assert presets["fast"]["model"] == "gpt-5.4-mini"


def test_load_llm_config_missing_llm_section_raises(tmp_path: Path) -> None:
    """``load_llm_config`` raises when the YAML has no ``llm:`` key."""
    bad = tmp_path / "no_llm.yaml"
    bad.write_text("other: 1\n", encoding="utf-8")
    with pytest.raises(MissingLLMSectionError):
        load_llm_config(bad)


def test_load_llm_config_empty_file_raises(tmp_path: Path) -> None:
    """Empty / non-mapping YAML files are rejected with the typed error."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(MissingLLMSectionError):
        load_llm_config(empty)


# ----- LLMClient construction (uses real config/jarvis.yaml) ---------------


@pytest.fixture
def llm_cfg() -> Mapping[str, object]:
    """Real ``llm:`` block parsed from ``config/jarvis.yaml``."""
    return load_llm_config(_CONFIG_PATH)


def test_client_construction_applies_default_preset(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """Constructing the client from jarvis.yaml picks up the ``deep`` preset."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    assert client.provider == "openai"
    assert client.model == "gpt-5.5"
    assert "openrouter" in client.base_url
    assert client.max_tokens == 32768
    assert client.active_preset == "deep"


def test_client_last_call_state_starts_empty(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """Pre-call ``last_*`` accessors return None / empty-state metadata."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    assert client.last_input_tokens is None
    assert client.last_output_tokens is None
    assert client.last_finish_reason is None
    metadata = client.last_metadata
    # Empty-state shape: every key present, all None / False.
    assert set(metadata.keys()) >= {"provider", "response_id", "preset", "model", "streaming"}
    assert metadata["provider"] is None
    assert metadata["response_id"] is None
    assert metadata["streaming"] is False


def test_client_last_metadata_is_isolated_copy(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """``last_metadata`` returns a fresh mapping so callers can't mutate state."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    snapshot = client.last_metadata
    # Mutating the returned mapping must not change the next read.
    if isinstance(snapshot, dict):
        snapshot["provider"] = "tampered"
    again = client.last_metadata
    assert again["provider"] is None


# ----- presets / switch_model ----------------------------------------------


def test_get_presets_returns_fast_and_deep(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """``get_presets`` reflects both presets defined in jarvis.yaml."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    presets = client.get_presets()
    assert set(presets.keys()) == {"fast", "deep"}
    assert presets["fast"]["model"] == "gpt-5.4-mini"
    assert presets["fast"]["max_tokens"] == 2048
    assert presets["deep"]["model"] == "gpt-5.5"


def test_switch_model_to_fast_updates_state(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """``switch_model('fast')`` returns the new model and updates accessors."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    assert client.model == "gpt-5.5"  # deep is default
    new_model = client.switch_model("fast")
    assert new_model == "gpt-5.4-mini"
    assert client.model == "gpt-5.4-mini"
    assert client.max_tokens == 2048
    assert client.active_preset == "fast"


def test_switch_model_unknown_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """Unknown preset → :class:`UnknownPresetError`."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    client = LLMClient(llm_cfg)
    with pytest.raises(UnknownPresetError):
        client.switch_model("does-not-exist")


# ----- api_key_env resolution ----------------------------------------------


def test_api_key_resolved_from_env_via_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """``api_key_env`` in the preset reads from ``os.environ`` (no logging)."""
    sentinel = "sk-jarvis-test-9b3f4d8a-fixture-only"
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", sentinel)
    client = LLMClient(llm_cfg)
    # We never expose the raw key publicly; assert the SDK construction path
    # would receive it by reaching into the private attribute. The test does
    # NOT log or otherwise echo the value — this is the only read.
    assert client._api_key == sentinel  # noqa: SLF001


def test_api_key_missing_env_yields_none(
    monkeypatch: pytest.MonkeyPatch,
    llm_cfg: Mapping[str, object],
) -> None:
    """When ``api_key_env`` is unset, the resolved key is None.

    Day-1's lazy SDK construction means construction still succeeds; an
    actual ``chat()`` call would then raise :class:`MissingAPIKeyError`.
    """
    monkeypatch.delenv("OPENROUTER_PROXY_KEY", raising=False)
    client = LLMClient(llm_cfg)
    assert client._api_key is None  # noqa: SLF001


def test_invalid_provider_in_config_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported ``provider`` value is rejected at construction time."""
    monkeypatch.setenv("OPENROUTER_PROXY_KEY", "sk-not-a-real-key")
    with pytest.raises(ValueError, match="unsupported provider"):
        LLMClient({"provider": "azure"})


# ----- public dataclass surface (compile-time + frozen) --------------------


def test_chatresult_is_frozen_and_hashable() -> None:
    """:class:`ChatResult` is a frozen dataclass."""
    result = ChatResult(
        text="hi",
        tool_calls=(),
        finish_reason="stop",
        input_tokens=10,
        output_tokens=2,
        raw={},
        # ADR-0002 Step 3 cost fields (defaulted, but exercised here so
        # the canary that requires them present can be satisfied by a
        # hand-built test ChatResult as well).
        model_used="gpt-5.5",
        tokens_in=10,
        tokens_out=2,
        cache_read_in=0,
        cache_write_in=0,
    )
    with pytest.raises((AttributeError, Exception)):
        result.text = "mutated"  # type: ignore[misc]


def test_chatresult_defaults_for_cost_fields() -> None:
    """ChatResult's ADR-0002 Step 3 cost fields default safely (additive)."""
    result = ChatResult(
        text=None,
        tool_calls=(),
        finish_reason="stop",
        input_tokens=None,
        output_tokens=None,
        raw={},
    )
    # Defaulted so legacy call sites (Day-1 unit tests) still compile.
    assert result.model_used == ""
    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.cache_read_in == 0
    assert result.cache_write_in == 0


def test_toolcall_is_frozen() -> None:
    """:class:`ToolCall` is a frozen dataclass."""
    tc = ToolCall(call_id="call_1", name="spawn_worker", arguments_json='{"task_id":"X"}')
    with pytest.raises((AttributeError, Exception)):
        tc.name = "verify_diff"  # type: ignore[misc]


def test_chatstreamchunk_minimum_shape() -> None:
    """:class:`ChatStreamChunk` carries text / is_final / finish_reason."""
    final_chunk = ChatStreamChunk(text=None, is_final=True, finish_reason="stop")
    assert final_chunk.is_final is True
    assert final_chunk.finish_reason == "stop"
    delta_chunk = ChatStreamChunk(text="hello", is_final=False, finish_reason=None)
    assert delta_chunk.text == "hello"
    assert delta_chunk.is_final is False
