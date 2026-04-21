"""Tests for the conversation store."""

from __future__ import annotations

import json

from memory.hot.conversation import ConversationStore


def _make_config(tmp_path):
    return {"memory": {"conversation_dir": str(tmp_path / "convos"), "max_conversation_turns": 3}}


def test_append_and_get_history(tmp_path):
    store = ConversationStore(_make_config(tmp_path))
    store.append("user1", [{"role": "user", "content": "hello"}])
    store.append("user1", [{"role": "assistant", "content": "hi"}])

    history = store.get_history("user1")
    assert len(history) == 2
    assert history[0]["content"] == "hello"
    assert history[1]["content"] == "hi"


def test_history_persists_to_disk(tmp_path):
    config = _make_config(tmp_path)
    store1 = ConversationStore(config)
    store1.append("user1", [{"role": "user", "content": "test"}])

    store2 = ConversationStore(config)
    history = store2.get_history("user1")
    assert len(history) == 1
    assert history[0]["content"] == "test"


def test_trim_keeps_recent_turns(tmp_path):
    store = ConversationStore(_make_config(tmp_path))
    for i in range(10):
        store.append("user1", [{"role": "user", "content": f"msg{i}"}])

    history = store.get_history("user1")
    # max_turns=3 means 6 messages max
    assert len(history) <= 6


def test_clear_removes_history(tmp_path):
    store = ConversationStore(_make_config(tmp_path))
    store.append("user1", [{"role": "user", "content": "hello"}])
    store.clear("user1")

    assert store.get_history("user1") == []


def test_replace_overwrites_history(tmp_path):
    store = ConversationStore(_make_config(tmp_path))
    store.append("user1", [{"role": "user", "content": "old"}])
    store.replace("user1", [{"role": "user", "content": "new"}])

    history = store.get_history("user1")
    assert len(history) == 1
    assert history[0]["content"] == "new"


def test_separate_users_have_separate_histories(tmp_path):
    store = ConversationStore(_make_config(tmp_path))
    store.append("alice", [{"role": "user", "content": "alice msg"}])
    store.append("bob", [{"role": "user", "content": "bob msg"}])

    assert store.get_history("alice")[0]["content"] == "alice msg"
    assert store.get_history("bob")[0]["content"] == "bob msg"
