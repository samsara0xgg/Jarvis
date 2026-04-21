"""Tests for memory.stable_prefix — StablePrefixBuilder."""

from __future__ import annotations

import pytest

from memory.stable_prefix import StablePrefixBuilder, _PREAMBLE
from memory.core.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    """Fresh MemoryStore backed by a temp SQLite DB."""
    db_path = tmp_path / "test_memory.db"
    s = MemoryStore(db_path)
    yield s
    s.close()


PERSONALITY = "你是 Jarvis，Allen 的私人语音管家。"


class TestStablePrefixBuilder:
    """StablePrefixBuilder assembles the stable prefix for LLM prompts."""

    def test_build_empty(self, store: MemoryStore):
        """No observations — personality + preamble + empty obs + current input."""
        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build([], "你好")

        assert result.startswith(PERSONALITY)
        assert _PREAMBLE in result
        assert "<observations>" in result
        assert "</observations>" in result
        assert "--- 本轮 ---" in result
        assert "[user] 你好" in result
        # No recent turns section when empty
        assert "--- 最近对话 ---" not in result

    def test_build_with_observations(self, store: MemoryStore):
        """Two observations appear inside <observations> tags."""
        store.add_observation(chunk_id=1, content="* 用户喜欢拿铁")
        store.add_observation(chunk_id=1, content="* 用户住在加拿大")

        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build([], "今天天气怎么样")

        assert "<observations>" in result
        assert "* 用户喜欢拿铁" in result
        assert "* 用户住在加拿大" in result
        assert "</observations>" in result

    def test_build_with_recent_turns(self, store: MemoryStore):
        """Recent turns formatted with [user] and [assistant] labels."""
        turns = [
            {"role": "user", "content": "打开客厅灯"},
            {"role": "assistant", "content": "好的，已打开客厅灯。"},
        ]
        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build(turns, "再调暗一点")

        assert "--- 最近对话 ---" in result
        assert "[user] 打开客厅灯" in result
        assert "[assistant] 好的，已打开客厅灯。" in result
        assert "--- 本轮 ---" in result
        assert "[user] 再调暗一点" in result

    def test_build_observation_order(self, store: MemoryStore):
        """First observation appears before second (chronological)."""
        store.add_observation(chunk_id=1, content="* 第一条")
        store.add_observation(chunk_id=2, content="* 第二条")

        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build([], "test")

        idx_first = result.index("* 第一条")
        idx_second = result.index("* 第二条")
        assert idx_first < idx_second

    def test_build_max_recent_turns(self, store: MemoryStore):
        """Only the last 10 turns (20 messages) are included."""
        turns = []
        for i in range(15):
            turns.append({"role": "user", "content": f"user msg {i}"})
            turns.append({"role": "assistant", "content": f"assistant msg {i}"})

        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build(turns, "current")

        # First 5 turns should be trimmed (indices 0-4)
        assert "user msg 0" not in result
        assert "user msg 4" not in result
        # Last 10 turns should be present (indices 5-14)
        assert "user msg 5" in result
        assert "user msg 14" in result
        assert "assistant msg 14" in result

    def test_build_non_string_content_skipped(self, store: MemoryStore):
        """Messages with non-string content (e.g. tool_use blocks) are skipped."""
        turns = [
            {"role": "user", "content": "帮我开灯"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "hue", "input": {}}
            ]},
            {"role": "user", "content": "谢谢"},
            {"role": "assistant", "content": "不客气！"},
        ]
        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build(turns, "再见")

        assert "[user] 帮我开灯" in result
        assert "[user] 谢谢" in result
        assert "[assistant] 不客气！" in result
        # tool_use block should not appear
        assert "tool_use" not in result

    def test_build_empty_string_content_skipped(self, store: MemoryStore):
        """Messages with empty string content are skipped."""
        turns = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "你好"},
        ]
        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build(turns, "test")

        assert "[assistant] 你好" in result
        # Empty user message should not produce a line
        lines = [l for l in result.split("\n") if l.startswith("[user]")]
        # Only the current_input [user] line
        assert len(lines) == 1
        assert lines[0] == "[user] test"

    def test_build_section_order(self, store: MemoryStore):
        """Sections appear in correct order: personality, preamble, observations, recent, current."""
        store.add_observation(chunk_id=1, content="* obs")
        turns = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        builder = StablePrefixBuilder(store, PERSONALITY)
        result = builder.build(turns, "now")

        idx_personality = result.index(PERSONALITY)
        idx_preamble = result.index(_PREAMBLE)
        idx_obs = result.index("<observations>")
        idx_recent = result.index("--- 最近对话 ---")
        idx_current = result.index("--- 本轮 ---")

        assert idx_personality < idx_preamble < idx_obs < idx_recent < idx_current
