"""Tests for MemoryStore observations table — v2 OM format."""

from __future__ import annotations

import pytest

from memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    """Fresh MemoryStore backed by a temp SQLite DB."""
    db_path = tmp_path / "test_obs.db"
    s = MemoryStore(db_path)
    yield s
    s.close()


class TestObservationStore:
    """CRUD operations for the observations table."""

    def test_add_observation(self, store: MemoryStore):
        row_id = store.add_observation(chunk_id=1, content="* 🔴 用户偏好暖黄灯光")
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_get_all_observations_ordered(self, store: MemoryStore):
        store.add_observation(chunk_id=1, content="first")
        store.add_observation(chunk_id=1, content="second")
        store.add_observation(chunk_id=2, content="third")
        obs = store.get_all_observations()
        assert len(obs) == 3
        assert obs[0]["content"] == "first"
        assert obs[1]["content"] == "second"
        assert obs[2]["content"] == "third"

    def test_get_observations_excludes_superseded(self, store: MemoryStore):
        id1 = store.add_observation(chunk_id=1, content="old fact")
        id2 = store.add_observation(chunk_id=1, content="new fact")
        store.supersede_observation(old_id=id1, new_id=id2)
        obs = store.get_all_observations()
        assert len(obs) == 1
        assert obs[0]["id"] == id2
        assert obs[0]["content"] == "new fact"

    def test_get_observations_char_count(self, store: MemoryStore):
        store.add_observation(chunk_id=1, content="hello")      # 5
        store.add_observation(chunk_id=1, content="world!!")     # 7
        assert store.get_observations_char_count() == 12

    def test_get_next_chunk_id_empty(self, store: MemoryStore):
        assert store.get_next_chunk_id() == 1

    def test_get_next_chunk_id_increments(self, store: MemoryStore):
        store.add_observation(chunk_id=3, content="chunk 3")
        assert store.get_next_chunk_id() == 4

    def test_source_turn_id_stored(self, store: MemoryStore):
        row_id = store.add_observation(
            chunk_id=1, content="with turn", source_turn_id=42,
        )
        obs = store.get_all_observations()
        assert obs[0]["source_turn_id"] == 42
