"""ADR 0260: the local multilingual-e5-small model that turns a text into 384 numbers.

int8 ONNX through onnxruntime plus the ``tokenizers`` package, two threads, loaded on first use
and once per process. A model directory without its files is not an error: ``available`` says so
(logged once) and search stays on words.

Every load and batch runs on one thread of the embedder's own, and glibc is asked to hand the freed
memory back after each: glibc keeps a heap per thread that touched ``malloc``, so batches landing
on whichever ``to_thread`` worker was free left the brain about 400 MB larger for good.
"""

from __future__ import annotations

import ctypes
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

MODEL_ID: Final = "multilingual-e5-small-int8"
TOKENIZER_FILE: Final = "tokenizer.json"
MODEL_FILE: Final = "model_quantized.onnx"
DIMENSIONS: Final = 384
_MAX_TOKENS: Final = 512
_THREADS: Final = 2
_QUERY_PREFIX: Final = "query: "
_PASSAGE_PREFIX: Final = "passage: "


class Embedder(Protocol):
    """What the index and the search need of a model: its id, whether it can run, the vectors."""

    model_id: str

    @property
    def available(self) -> bool:
        """Whether the model's files are on disk."""
        ...

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        """One L2-normalised float32 row per text; ``query`` picks the e5 prefix."""
        ...


class E5Embedder:
    """multilingual-e5-small read from ``directory``; the session loads on the first ``embed``."""

    model_id = MODEL_ID

    def __init__(self, directory: Path) -> None:
        """Remember where the files are; nothing is read yet."""
        self._dir = directory
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meaning-embed")
        self._loaded: tuple[Any, Any, bool] | None = None
        self._warned = False

    @property
    def available(self) -> bool:
        """True when both model files exist; logs once when they do not."""
        have = (self._dir / TOKENIZER_FILE).is_file() and (self._dir / MODEL_FILE).is_file()
        if not have and not self._warned:
            self._warned = True
            LOGGER.warning("meaning search: no model in %s; searching by words only", self._dir)
        return have

    def _load(self) -> tuple[Any, Any, bool]:
        if self._loaded is None:
            import onnxruntime as ort  # noqa: PLC0415 — heavy; only a search by meaning pays
            from tokenizers import Tokenizer  # noqa: PLC0415

            tokenizer = Tokenizer.from_file(str(self._dir / TOKENIZER_FILE))
            tokenizer.enable_truncation(_MAX_TOKENS)
            options = ort.SessionOptions()
            options.intra_op_num_threads = _THREADS
            # The arena keeps the peak of every batch shape for good; without it the brain
            # holds the model at about +450 MB instead of +580 MB, at the same speed.
            options.enable_cpu_mem_arena = False
            session = ort.InferenceSession(
                str(self._dir / MODEL_FILE), options, providers=["CPUExecutionProvider"],
            )
            wants_types = any(i.name == "token_type_ids" for i in session.get_inputs())
            self._loaded = (tokenizer, session, wants_types)
        return self._loaded

    def embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        """Mean-pool over the attention mask, then L2-normalise; on the embedder's own thread."""
        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        return self._worker.submit(self._embed, texts, query=query).result()

    def _embed(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        try:
            return self._pooled(texts, query=query)
        finally:
            _trim()

    def _pooled(self, texts: Sequence[str], *, query: bool) -> np.ndarray:
        tokenizer, session, wants_types = self._load()
        prefix = _QUERY_PREFIX if query else _PASSAGE_PREFIX
        encoded = tokenizer.encode_batch([prefix + text for text in texts])
        width = max(len(e.ids) for e in encoded)
        ids = np.zeros((len(encoded), width), dtype=np.int64)
        mask = np.zeros((len(encoded), width), dtype=np.int64)
        for row, e in enumerate(encoded):
            ids[row, : len(e.ids)] = e.ids
            mask[row, : len(e.ids)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if wants_types:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, feed)[0]
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / weights.sum(axis=1)
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        return np.asarray(pooled / np.maximum(norm, 1e-12), dtype=np.float32)


@functools.cache
def _malloc_trim() -> Callable[[int], int] | None:
    try:
        return ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):  # not glibc (macOS): its allocator returns memory itself
        return None


def _trim() -> None:
    """Return the free pages glibc holds in every thread's heap to the system."""
    trim = _malloc_trim()
    if trim is not None:
        trim(0)


@functools.cache
def shared(directory: Path) -> E5Embedder:
    """The one embedder of this process for ``directory``: the index task and the tool share it."""
    return E5Embedder(directory)
