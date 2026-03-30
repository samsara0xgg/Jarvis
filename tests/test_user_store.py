"""Tests for JSON-backed enrolled user storage."""

from __future__ import annotations

from pathlib import Path

import yaml

from auth.user_store import UserStore


def _load_config() -> dict:
    """Load the project config for user store tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def _build_config(tmp_path: Path) -> dict:
    """Create a config copy with an isolated user store path."""

    config = _load_config()
    config.setdefault("auth", {})
    config["auth"]["user_store_path"] = str(tmp_path / "users.json")
    return config


def _sample_user(user_id: str = "alice") -> dict:
    """Build a valid user record for tests."""

    return {
        "user_id": user_id,
        "name": "Alice",
        "embedding": [0.1, 0.2, 0.3],
        "role": "resident",
        "permissions": ["unlock"],
        "enrolled_at": "2026-03-26T00:00:00+00:00",
    }


def test_user_store_crud_persists_records(tmp_path: Path) -> None:
    """The user store should support add, read, update, list, and delete."""

    store = UserStore(_build_config(tmp_path))
    stored_user = store.add_user(_sample_user())

    assert stored_user["user_id"] == "alice"
    assert store.get_user("alice")["name"] == "Alice"
    assert len(store.get_all_users()) == 1

    updated_user = store.update_user("alice", {"role": "admin", "permissions": ["unlock", "lights"]})

    assert updated_user["role"] == "admin"
    assert updated_user["permissions"] == ["unlock", "lights"]

    reloaded_store = UserStore(_build_config(tmp_path))
    assert reloaded_store.get_user("alice")["role"] == "admin"
    assert reloaded_store.delete_user("alice") is True
    assert reloaded_store.get_user("alice") is None
    assert reloaded_store.get_all_users() == []
