"""Tests for memory.embedder — text embedding via FastEmbed."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from memory.core.embedder import Embedder


@pytest.fixture(scope="module")
def embedder():
    """Shared embedder instance (model loads once per test module)."""
    return Embedder()


class TestEmbedder:
    """Embedding generation and similarity tests."""

    def test_encode_returns_correct_shape(self, embedder: Embedder):
        vec = embedder.encode("你好世界")
        assert isinstance(vec, np.ndarray)
        assert vec.ndim == 1
        assert vec.shape[0] == 512  # bge-small-zh-v1.5 is 512-dim

    def test_encode_returns_unit_norm(self, embedder: Embedder):
        vec = embedder.encode("测试文本")
        norm = np.linalg.norm(vec)
        assert abs(norm - 1.0) < 1e-5

    def test_encode_batch_shape(self, embedder: Embedder):
        texts = ["你好", "世界", "测试"]
        mat = embedder.encode_batch(texts)
        assert mat.shape == (3, 512)

    def test_encode_batch_empty(self, embedder: Embedder):
        mat = embedder.encode_batch([])
        assert mat.shape[0] == 0

    def test_encode_batch_unit_norms(self, embedder: Embedder):
        mat = embedder.encode_batch(["你好", "世界"])
        norms = np.linalg.norm(mat, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-5)

    def test_similar_texts_high_cosine(self, embedder: Embedder):
        """Semantically similar Chinese texts should have high cosine similarity."""
        v1 = embedder.encode("我喜欢喝咖啡")
        v2 = embedder.encode("拿铁是我最爱的饮品")
        cosine = float(v1 @ v2)
        assert cosine > 0.5, f"Expected > 0.5, got {cosine}"

    def test_dissimilar_texts_low_cosine(self, embedder: Embedder):
        """Unrelated texts should have low cosine similarity."""
        v1 = embedder.encode("我喜欢喝咖啡")
        v2 = embedder.encode("今天的股票涨了三个点")
        cosine = float(v1 @ v2)
        assert cosine < 0.5, f"Expected < 0.5, got {cosine}"

    def test_same_text_cosine_one(self, embedder: Embedder):
        """Same text should have cosine ~1.0."""
        v1 = embedder.encode("Allen住在温哥华")
        v2 = embedder.encode("Allen住在温哥华")
        cosine = float(v1 @ v2)
        assert cosine > 0.99

    def test_dimension_property(self, embedder: Embedder):
        assert embedder.dimension == 512


@pytest.fixture
def mock_embedder():
    """Bare Embedder with a mock model, for cache-behavior tests."""
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


class TestCache:
    """Single-entry cache behavior."""

    def test_same_text_hits_cache(self, mock_embedder):
        """Encoding the same text twice should only call model.embed once."""
        mock_embedder.encode("开灯")
        mock_embedder._model.embed = MagicMock(
            return_value=iter([np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        )
        mock_embedder.encode("开灯")
        mock_embedder._model.embed.assert_not_called()

    def test_different_text_misses_cache(self, mock_embedder):
        """Encoding different texts should call model.embed twice."""
        mock_embedder.encode("开灯")
        mock_embedder._model.embed = MagicMock(
            return_value=iter([np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)])
        )
        mock_embedder.encode("关灯")
        mock_embedder._model.embed.assert_called_once()

    def test_cache_returns_copy(self, mock_embedder):
        """Two calls with the same text should return different objects."""
        v1 = mock_embedder.encode("开灯")
        v2 = mock_embedder.encode("开灯")
        assert v1 is not v2

    def test_cache_does_not_break_normalization(self, mock_embedder):
        """Cached vector should still be unit-norm."""
        mock_embedder._model.embed = MagicMock(
            return_value=iter([np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32)])
        )
        mock_embedder._last_text = None
        mock_embedder._last_vec = None
        v = mock_embedder.encode("测试文本")
        assert abs(np.linalg.norm(v) - 1.0) < 1e-6

        v2 = mock_embedder.encode("测试文本")
        assert abs(np.linalg.norm(v2) - 1.0) < 1e-6
