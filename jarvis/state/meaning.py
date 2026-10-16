"""ADR 0260: vectors of what is searchable, kept in memory.db, scanned by cosine.

Each kind of text has one indexer in ``INDEXERS``; adding the imported entries (ADR 0259) later is
one entry there. A text is cut into overlapping pieces; a piece whose digest is held for the
current model is not embedded again, and a piece embedded by another model is rebuilt. The search
side is one matrix product over the vectors of a kind, cached until the table changes.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import statistics
from collections.abc import Callable, Iterable
from contextlib import closing
from typing import TYPE_CHECKING, Final, NamedTuple

import numpy as np

from jarvis.state import core_memory, imports
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from jarvis.state.embedding import Embedder

LOGGER = logging.getLogger(__name__)

PIECE_CHARS: Final = 400
PIECE_OVERLAP: Final = 80
BATCH: Final = 8
"""Pieces embedded at once: 32 of 400 characters held about 1.4 GB at peak on the brain, 8 about
0.8 GB, at the same speed."""
RRF_K: Final = 60
RECORDS: Final = "records"
"""The kind of a conversation record; the ``conversation`` tool searches this one."""
IMPORTS: Final = "imports"
"""The kind of an entry read from an export he handed over (ADR 0259); ``archive`` searches it."""

# One text to index: (ref id, text). ``ref`` is what the kind's own table calls the row.
Text = tuple[str, str]
Indexer = Callable[[sqlite3.Connection], Iterable[Text]]


def pieces(text: str) -> list[str]:
    """``text`` cut into ~400-character pieces overlapping by 80; short text stays whole."""
    text = text.strip()
    if len(text) <= PIECE_CHARS:
        return [text] if text else []
    step = PIECE_CHARS - PIECE_OVERLAP
    out = []
    for start in range(0, len(text), step):
        out.append(text[start : start + PIECE_CHARS])
        if start + PIECE_CHARS >= len(text):
            break
    return out


def _records(conn: sqlite3.Connection) -> Iterator[Text]:
    for rid, text in conn.execute("SELECT id, text FROM records"):
        yield str(rid), str(text)


def _days(conn: sqlite3.Connection) -> Iterator[Text]:
    # A later row of the same day replaces the earlier one.
    rows = conn.execute("SELECT day, summary FROM day_summaries ORDER BY rowid").fetchall()
    for day, summary in dict(rows).items():
        yield str(day), str(summary)


def _imports(conn: sqlite3.Connection) -> Iterator[Text]:
    for one in imports.entries(conn):  # as the tools read them: no export stand-ins
        yield one.id, one.text


def _wordings(item: core_memory.Item) -> Iterator[str]:
    for earlier in item.get("history") or []:
        text = earlier.get("text") if isinstance(earlier, dict) else earlier
        if isinstance(text, str):
            yield text


def _core(conn: sqlite3.Connection) -> Iterator[Text]:
    version = core_memory.latest(conn)
    for items in () if version is None else version.doc.values():
        for item in items:
            if item.get("id") and isinstance(item.get("text"), str):
                yield str(item["id"]), item["text"]


def _core_past(conn: sqlite3.Connection) -> Iterator[Text]:
    version = core_memory.latest(conn)
    for items in () if version is None else version.doc.values():
        for item in items:
            for n, text in enumerate(_wordings(item)):
                if item.get("id"):
                    yield f"{item['id']}:{n}", text


INDEXERS: Final[tuple[tuple[str, Indexer], ...]] = (
    (RECORDS, _records),
    ("day", _days),
    ("core", _core),
    ("core_past", _core_past),
    (IMPORTS, _imports),
)
"""Kind name and the reader of its texts; the vector table is keyed by the kind."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Piece(NamedTuple):
    kind: str
    ref: str
    piece: int
    text: str
    digest: str


def _wanted(conn: sqlite3.Connection) -> list[_Piece]:
    return [
        _Piece(kind, ref, n, part, _digest(part))
        for kind, indexer in INDEXERS
        for ref, text in indexer(conn)
        for n, part in enumerate(pieces(text))
    ]


def index_pending(path: Path, embedder: Embedder, *, batch: int = BATCH) -> int:
    """Embed up to ``batch`` pieces not yet held for this model; returns how many are left.

    Also drops vectors whose text is gone. The embedding runs outside any transaction.
    """
    with closing(open_memory_db(path)) as conn:
        held = {
            (k, r, n): (m, d)
            for k, r, n, m, d in conn.execute(
                "SELECT kind, ref, piece, model, digest FROM meaning_vectors",
            )
        }
        wanted = _wanted(conn)
        todo = [
            p for p in wanted
            if held.get((p.kind, p.ref, p.piece)) != (embedder.model_id, p.digest)
        ]
        keep = {(p.kind, p.ref, p.piece) for p in wanted}
        gone = [key for key in held if key not in keep]
        if gone:
            with conn:
                conn.executemany(
                    "DELETE FROM meaning_vectors WHERE kind = ? AND ref = ? AND piece = ?", gone,
                )
        chunk = todo[:batch]
        if chunk:
            vectors = embedder.embed([p.text for p in chunk], query=False).astype(np.float16)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO meaning_vectors (kind, ref, piece, model, digest, vec) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (p.kind, p.ref, p.piece, embedder.model_id, p.digest, v.tobytes())
                        for p, v in zip(chunk, vectors, strict=True)
                    ],
                )
        return len(todo) - len(chunk)


# (path, kind, model) -> ((row count, newest rowid), refs, one matrix row per ref)
_CACHE: dict[tuple[str, str, str], tuple[tuple[int, int], list[str], np.ndarray]] = {}


def _matrix(
    conn: sqlite3.Connection, path: Path, kind: str, model: str,
) -> tuple[list[str], np.ndarray]:
    stamp = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM meaning_vectors",
    ).fetchone()
    key = (str(path), kind, model)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == tuple(stamp):
        return cached[1], cached[2]
    rows = conn.execute(
        "SELECT ref, vec FROM meaning_vectors WHERE kind = ? AND model = ? ORDER BY ref, piece",
        (kind, model),
    ).fetchall()
    refs = [str(r) for r, _ in rows]
    matrix = (
        np.stack([np.frombuffer(v, dtype=np.float16) for _, v in rows]).astype(np.float32)
        if rows else np.zeros((0, 0), dtype=np.float32)
    )
    _CACHE[key] = (tuple(stamp), refs, matrix)
    return refs, matrix


def nearest(  # noqa: PLR0913 — the question and its bounds, all keyword-only after the model.
    conn: sqlite3.Connection, path: Path, embedder: Embedder, query: str | Sequence[str],
    kind: str, *, among: set[str] | None = None, limit: int, margin: float,
) -> list[str]:
    """Ref ids of ``kind`` closest to ``query``, best first, each ``margin`` above the median.

    Several phrasings (the same question in each language) are ranked one by one and merged.

    A text of several pieces counts once, at its best piece. ``among`` limits the candidates.
    e5 scores all text 0.8 or more, and how high depends on the query, so a hit is measured
    against this query's median over every text of ``kind``, not against a fixed score.
    """
    try:
        refs, matrix = _matrix(conn, path, kind, embedder.model_id)
    except sqlite3.OperationalError:  # a store from before the table
        return []
    if not refs:
        return []
    asked = [query] if isinstance(query, str) else list(query)
    rankings = []
    for scores in embedder.embed(asked, query=True) @ matrix.T:
        best: dict[str, float] = {}
        for ref, score in zip(refs, scores.tolist(), strict=True):
            best[ref] = max(score, best.get(ref, -1.0))
        floor = statistics.median(best.values()) + margin
        near = [ref for ref, s in best.items() if s >= floor and (among is None or ref in among)]
        rankings.append(sorted(near, key=lambda ref: -best[ref]))
    return fuse(*rankings)[:limit]


def fuse(*rankings: Sequence[str]) -> list[str]:
    """Reciprocal rank fusion (k=60) of best-first lists; ties keep the earlier list's order."""
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, ref in enumerate(ranking, start=1):
            score[ref] = score.get(ref, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(score, key=lambda ref: -score[ref])
