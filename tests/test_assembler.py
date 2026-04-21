"""Tests for memory.hot.assembler — PromptContext + 4-block prompt assembly."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory.hot.assembler import Assembler, PromptBlock, PromptContext


# ---------------------------------------------------------------------------
# PromptBlock / PromptContext data classes
# ---------------------------------------------------------------------------

class TestPromptBlock:
    def test_defaults(self):
        b = PromptBlock(content="hello")
        assert b.content == "hello"
        assert b.cache is False
        assert b.name == ""

    def test_cache_flag(self):
        b = PromptBlock(content="x", cache=True, name="identity")
        assert b.cache is True
        assert b.name == "identity"


class TestPromptContextSerialization:
    def test_to_anthropic_system_shape(self):
        ctx = PromptContext(
            blocks=[
                PromptBlock(content="A", cache=True, name="identity"),
                PromptBlock(content="B", cache=True, name="profile"),
                PromptBlock(content="C", cache=True, name="observations"),
                PromptBlock(content="D", cache=False, name="situation"),
            ],
            messages=[],
        )
        out = ctx.to_anthropic_system()
        assert isinstance(out, list)
        assert len(out) == 4
        for entry in out:
            assert entry["type"] == "text"
        # First three cached, last not
        assert out[0]["cache_control"] == {"type": "ephemeral"}
        assert out[1]["cache_control"] == {"type": "ephemeral"}
        assert out[2]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in out[3]
        assert [e["text"] for e in out] == ["A", "B", "C", "D"]

    def test_to_openai_system_str_joins_with_blank_line(self):
        ctx = PromptContext(
            blocks=[PromptBlock(content="A"), PromptBlock(content="B")],
            messages=[],
        )
        assert ctx.to_openai_system_str() == "A\n\nB"

    def test_injected_ids_default_empty(self):
        ctx = PromptContext(blocks=[], messages=[])
        assert ctx.injected_observation_ids == []

    def test_anthropic_cache_breakpoint_budget(self):
        """Plan caps cache_control at 3 blocks (identity/profile/observations)."""
        ctx = PromptContext(
            blocks=[
                PromptBlock(content="A", cache=True),
                PromptBlock(content="B", cache=True),
                PromptBlock(content="C", cache=True),
                PromptBlock(content="D", cache=False),
            ],
            messages=[],
        )
        out = ctx.to_anthropic_system()
        cached = [e for e in out if "cache_control" in e]
        assert len(cached) <= 3


# ---------------------------------------------------------------------------
# Assembler behaviour
# ---------------------------------------------------------------------------

def _make_store(profile=None, observations=None):
    store = MagicMock()
    store.get_profile = MagicMock(return_value=profile)
    store.get_all_observations = MagicMock(return_value=observations or [])
    return store


def _profile_fn(profile: dict | None) -> str:
    if not profile:
        return ""
    return f"name={profile.get('name', '')}"


class TestAssembler:
    def test_full_four_blocks_present(self):
        store = _make_store(
            profile={"name": "Allen"},
            observations=[
                {"id": 1, "content": "obs one"},
                {"id": 2, "content": "obs two"},
            ],
        )
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble(
            text="你好",
            user_id="allen",
            history=[{"role": "user", "content": "ping"}],
            user_name="Allen",
            user_role="owner",
            user_emotion="",
            situation="normal",
        )
        names = [b.name for b in ctx.blocks]
        assert names == ["identity", "profile", "observations", "situation"]

    def test_block1_identity_is_cache_true(self):
        store = _make_store()
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        identity = next(b for b in ctx.blocks if b.name == "identity")
        assert identity.cache is True
        assert "<personality>" in identity.content
        assert "<output_rules>" in identity.content

    def test_block2_profile_skipped_when_profile_absent(self):
        store = _make_store(profile=None, observations=[{"id": 1, "content": "o"}])
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        names = [b.name for b in ctx.blocks]
        assert "profile" not in names

    def test_block2_profile_skipped_when_profile_text_empty(self):
        """profile_fn returning empty string means nothing useful to render."""
        store = _make_store(profile={"name": ""})
        asm = Assembler(store=store, profile_fn=lambda p: "")
        ctx = asm.assemble("x", "u", history=[])
        names = [b.name for b in ctx.blocks]
        assert "profile" not in names

    def test_block2_profile_content_uses_profile_fn(self):
        store = _make_store(profile={"name": "Allen"})
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        profile = next(b for b in ctx.blocks if b.name == "profile")
        assert profile.cache is True
        assert "[关于用户]" in profile.content
        assert "name=Allen" in profile.content

    def test_block3_observations_cache_true_and_full(self):
        observations = [
            {"id": 10, "content": "obs A"},
            {"id": 20, "content": "obs B"},
        ]
        store = _make_store(observations=observations)
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        obs = next(b for b in ctx.blocks if b.name == "observations")
        assert obs.cache is True
        assert "<observations>" in obs.content
        assert "10. obs A" in obs.content
        assert "20. obs B" in obs.content

    def test_block3_injected_ids_equal_all_observation_ids(self):
        observations = [
            {"id": 10, "content": "obs A"},
            {"id": 20, "content": "obs B"},
            {"id": 30, "content": "obs C"},
        ]
        store = _make_store(observations=observations)
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        assert ctx.injected_observation_ids == [10, 20, 30]

    def test_block3_skips_observations_without_id_or_content(self):
        observations = [
            {"id": 10, "content": "good"},
            {"id": None, "content": "no id"},
            {"id": 20, "content": ""},
            {"id": 30, "content": "also good"},
        ]
        store = _make_store(observations=observations)
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        assert ctx.injected_observation_ids == [10, 30]
        obs = next(b for b in ctx.blocks if b.name == "observations")
        assert "10. good" in obs.content
        assert "30. also good" in obs.content
        assert "no id" not in obs.content

    def test_block3_empty_observations_yield_empty_injected_ids(self):
        store = _make_store(observations=[])
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[])
        assert ctx.injected_observation_ids == []

    def test_block4_situation_is_cache_false(self):
        store = _make_store()
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble(
            "x", "u", history=[],
            user_name="Allen", user_role="owner",
        )
        situation = next(b for b in ctx.blocks if b.name == "situation")
        assert situation.cache is False
        assert situation.content.startswith("<situation>")
        assert situation.content.endswith("</situation>")
        assert "Allen" in situation.content

    def test_messages_is_history_plus_current_user_turn(self):
        store = _make_store()
        asm = Assembler(store=store, profile_fn=_profile_fn)
        history = [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "ans"},
        ]
        ctx = asm.assemble("current", "u", history=history)
        assert ctx.messages == history + [
            {"role": "user", "content": "current"}
        ]

    def test_history_is_copied_not_aliased(self):
        """Mutating ctx.messages must not corrupt the caller's history list."""
        store = _make_store()
        asm = Assembler(store=store, profile_fn=_profile_fn)
        history = [{"role": "user", "content": "a"}]
        ctx = asm.assemble("b", "u", history=history)
        ctx.messages.append({"role": "assistant", "content": "c"})
        assert len(history) == 1

    def test_pure_function_stateless_between_calls(self):
        """Two identical assembles with same inputs produce independent contexts."""
        observations = [{"id": 1, "content": "obs"}]
        store = _make_store(observations=observations)
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx1 = asm.assemble("x", "u", history=[])
        ctx2 = asm.assemble("x", "u", history=[])
        assert ctx1 is not ctx2
        assert ctx1.injected_observation_ids == ctx2.injected_observation_ids
        # Mutation of one must not affect the other
        ctx1.injected_observation_ids.append(999)
        assert ctx2.injected_observation_ids == [1]

    def test_to_anthropic_system_end_to_end(self):
        """Full assembly → Anthropic shape: 4 entries, 3 cached, 1 not."""
        store = _make_store(
            profile={"name": "Allen"},
            observations=[{"id": 1, "content": "o"}],
        )
        asm = Assembler(store=store, profile_fn=_profile_fn)
        ctx = asm.assemble("x", "u", history=[], user_name="Allen")
        sys_list = ctx.to_anthropic_system()
        assert len(sys_list) == 4
        cached = [e for e in sys_list if "cache_control" in e]
        assert len(cached) == 3  # identity + profile + observations
