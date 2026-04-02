"""Tests for realtime_data cache."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from realtime_data.cache import Cache


class TestCache:
    def test_set_and_get(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache.set("test_key", {"foo": "bar"})

        data, is_stale = cache.get("test_key", ttl_seconds=60)
        assert data == {"foo": "bar"}
        assert is_stale is False

    def test_get_missing_key(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        data, is_stale = cache.get("nonexistent", ttl_seconds=60)
        assert data is None
        assert is_stale is False

    def test_stale_detection(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache.set("stale_key", {"value": 1})

        # TTL of 0 means immediately stale
        data, is_stale = cache.get("stale_key", ttl_seconds=0)
        assert data == {"value": 1}
        assert is_stale is True

    def test_fresh_within_ttl(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache.set("fresh", "hello")

        data, is_stale = cache.get("fresh", ttl_seconds=3600)
        assert data == "hello"
        assert is_stale is False

    def test_overwrite_key(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache.set("key", "v1")
        cache.set("key", "v2")

        data, _ = cache.get("key", ttl_seconds=60)
        assert data == "v2"

    def test_corrupted_cache_file(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache_file = tmp_path / "cache" / "bad.json"
        cache_file.write_text("NOT JSON {{{")

        data, is_stale = cache.get("bad", ttl_seconds=60)
        assert data is None
        assert is_stale is False

    def test_cache_dir_created(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "new" / "nested" / "cache"
        cache = Cache(cache_dir)
        assert cache_dir.exists()

    def test_multiple_keys(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        cache.set("a", [1, 2])
        cache.set("b", {"x": "y"})

        data_a, _ = cache.get("a", ttl_seconds=60)
        data_b, _ = cache.get("b", ttl_seconds=60)
        assert data_a == [1, 2]
        assert data_b == {"x": "y"}
