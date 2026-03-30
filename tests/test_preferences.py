"""Tests for the user preference store."""

from __future__ import annotations

from memory.user_preferences import UserPreferenceStore


def _make_config(tmp_path):
    return {"memory": {"preferences_dir": str(tmp_path / "prefs")}}


def test_set_and_get(tmp_path):
    store = UserPreferenceStore(_make_config(tmp_path))
    store.set("user1", "color", "blue")
    assert store.get("user1", "color") == "blue"


def test_get_default_for_missing_key(tmp_path):
    store = UserPreferenceStore(_make_config(tmp_path))
    assert store.get("user1", "missing") is None
    assert store.get("user1", "missing", "default") == "default"


def test_get_all(tmp_path):
    store = UserPreferenceStore(_make_config(tmp_path))
    store.set("user1", "a", "1")
    store.set("user1", "b", "2")
    all_prefs = store.get_all("user1")
    assert all_prefs == {"a": "1", "b": "2"}


def test_delete(tmp_path):
    store = UserPreferenceStore(_make_config(tmp_path))
    store.set("user1", "key", "val")
    assert store.delete("user1", "key") is True
    assert store.get("user1", "key") is None
    assert store.delete("user1", "key") is False


def test_persistence(tmp_path):
    config = _make_config(tmp_path)
    store1 = UserPreferenceStore(config)
    store1.set("user1", "name", "Jarvis")

    store2 = UserPreferenceStore(config)
    assert store2.get("user1", "name") == "Jarvis"


def test_separate_users(tmp_path):
    store = UserPreferenceStore(_make_config(tmp_path))
    store.set("alice", "drink", "coffee")
    store.set("bob", "drink", "tea")
    assert store.get("alice", "drink") == "coffee"
    assert store.get("bob", "drink") == "tea"
