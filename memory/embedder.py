"""Text embedding via FastEmbed (ONNX) for memory retrieval."""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

# Default model: Chinese-optimized, 512-dim, ~90MB
_DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class Embedder:
    """Generate text embeddings using FastEmbed (ONNX runtime).

    Lazy-loads the model on first use to avoid blocking startup.

    Args:
        model_name: FastEmbed model identifier.
    """

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock = threading.Lock()
        self._last_text: str | None = None
        self._last_vec: np.ndarray | None = None

    def _load(self) -> None:
        """Load the embedding model (first call only, thread-safe)."""
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return  # another thread loaded while we waited
            from fastembed import TextEmbedding

            LOGGER.info("Loading embedding model: %s", self._model_name)
            self._model = TextEmbedding(model_name=self._model_name)
            LOGGER.info("Embedding model loaded")

    def encode(self, text: str) -> np.ndarray:
        """Encode a single text into a unit-norm embedding vector.

        Args:
            text: Input text.

        Returns:
            1-D float32 numpy array (e.g. 512-dim for bge-small-zh).
        """
        if text == self._last_text and self._last_vec is not None:
            return self._last_vec.copy()
        self._load()
        embeddings = list(self._model.embed([text]))
        vec = np.array(embeddings[0], dtype=np.float32)
        # Normalize to unit length for cosine similarity via dot product
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        self._last_text = text
        self._last_vec = vec
        return vec.copy()

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """Encode multiple texts into a matrix of unit-norm embeddings.

        Args:
            texts: List of input texts.

        Returns:
            2-D float32 numpy array of shape (len(texts), dim).
        """
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        self._load()
        embeddings = list(self._model.embed(texts))
        mat = np.array(embeddings, dtype=np.float32)
        # Row-wise normalization
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1.0)
        mat /= norms
        return mat

    @property
    def dimension(self) -> int:
        """Return the embedding dimension (loads model if needed)."""
        self._load()
        # Probe with a dummy text
        vec = self.encode("test")
        return vec.shape[0]
