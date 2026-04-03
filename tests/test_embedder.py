"""Tests for memory.embedder — text embedding via FastEmbed."""

from __future__ import annotations

import numpy as np
import pytest

from memory.embedder import Embedder


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
