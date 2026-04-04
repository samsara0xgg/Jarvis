"""Tests for Embedder single-entry cache."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from memory.embedder import Embedder


@pytest.fixture
def embedder():
    e = Embedder.__new__(Embedder)
    e._model_name = "test"
    e._model = MagicMock()
    e._lock = threading.Lock()
    e._last_text = None
    e._last_vec = None
    e._model.embed = MagicMock(
        return_value=iter([np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
    )
    return e


def test_same_text_hits_cache(embedder):
    """Encoding the same text twice should only call model.embed once."""
    embedder.encode("开灯")
    # Reset mock to return fresh iterator for potential second call
    embedder._model.embed = MagicMock(
        return_value=iter([np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
    )
    embedder.encode("开灯")
    embedder._model.embed.assert_not_called()


def test_different_text_misses_cache(embedder):
    """Encoding different texts should call model.embed twice."""
    embedder.encode("开灯")
    embedder._model.embed = MagicMock(
        return_value=iter([np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)])
    )
    embedder.encode("关灯")
    embedder._model.embed.assert_called_once()


def test_cache_returns_copy(embedder):
    """Two calls with the same text should return different objects (not aliased)."""
    v1 = embedder.encode("开灯")
    v2 = embedder.encode("开灯")
    assert v1 is not v2


def test_cache_does_not_break_normalization(embedder):
    """Cached vector should still be unit-norm."""
    # Use a non-unit vector so we can verify normalization happened
    embedder._model.embed = MagicMock(
        return_value=iter([np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32)])
    )
    embedder._last_text = None  # ensure cache miss
    embedder._last_vec = None
    v = embedder.encode("测试文本")
    norm = np.linalg.norm(v)
    assert abs(norm - 1.0) < 1e-6

    # Second call (cache hit) should also be unit-norm
    v2 = embedder.encode("测试文本")
    norm2 = np.linalg.norm(v2)
    assert abs(norm2 - 1.0) < 1e-6
