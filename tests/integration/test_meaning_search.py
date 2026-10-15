"""ADR 0260: search by meaning, beside words.

A real memory.db is indexed and searched with a fake embedder whose vectors depend only on which
concept words a text holds, so every assertion is about what the index holds and what the
``conversation`` tool returns, never about a model's taste. The one real-model check runs only
when ``JARVIS_MEANING_MODEL_DIR`` points at the model files.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.state import core_memory, meaning
from jarvis.state.embedding import E5Embedder
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from jarvis.execution.tools import ToolContext

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)
# A concept is a set of words in either language; a text points along every concept it holds.
CONCEPTS = (("interview", "面试"), ("weather", "rain", "天气"), ("garden", "花园"))
RECORDS = [
    ("r1", "2026-10-01T15:12:00-07:00", "allen", "remind me about the Shopify interview"),
    ("r2", "2026-10-02T09:00:00-07:00", "allen", "Mostly rain in the morning"),
    ("r3", "2026-10-03T09:00:00-07:00", "allen", "The garden sounds nice"),
    ("r4", "2026-10-04T09:00:00-07:00", "allen", "面试 prep notes, bring the 面试 résumé"),
]


class FakeEmbedder:
    """Concept vectors; counts the texts it was asked to embed."""

    def __init__(self, model_id: str = "fake-1", *, present: bool = True) -> None:
        """A model of this id that is or is not on disk."""
        self.model_id = model_id
        self.available = present
        self.embedded: list[str] = []

    def embed(self, texts: Any, *, query: bool) -> np.ndarray:  # noqa: ANN401
        """One concept vector per text; passages are counted."""
        rows = []
        for text in texts:
            if not query:
                self.embedded.append(text)
            row = np.array(
                [float(any(w in text.lower() for w in group)) for group in CONCEPTS] + [0.01],
                dtype=np.float32,
            )
            rows.append(row / np.linalg.norm(row))
        return np.stack(rows)


def _store(root: Path, records: list[tuple[str, str, str, str]] = RECORDS) -> Path:
    path = root / "memory.db"
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany("INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", records)
    return path


def _ids(path: Path, embedder: FakeEmbedder | None, **args: Any) -> list[str]:  # noqa: ANN401
    (tool,) = build_conversation_tool(path, ZONE, lambda: NOW, embedder)
    answer = tool.handler(args, cast("ToolContext", None))
    return [m["id"] for m in answer["moments"]]


def _drain(path: Path, embedder: FakeEmbedder) -> None:
    while meaning.index_pending(path, embedder, batch=2):
        pass


def test_a_long_text_is_cut_into_pieces_that_overlap() -> None:
    """A text over 400 characters becomes pieces sharing 80 characters; a short one stays whole."""
    text = "".join(chr(0x4E00 + i % 500) for i in range(1000))
    cut = meaning.pieces(text)
    assert [len(p) for p in cut] == [400, 400, 360]
    assert cut[0][-80:] == cut[1][:80]
    assert meaning.pieces("short") == ["short"]
    assert meaning.pieces("   ") == []


def test_a_piece_held_for_this_model_is_not_embedded_again(tmp_path: Path) -> None:
    """A second pass embeds nothing; an edited text is embedded alone."""
    path, model = _store(tmp_path), FakeEmbedder()
    _drain(path, model)
    assert len(model.embedded) == len(RECORDS)
    model.embedded.clear()
    _drain(path, model)
    assert model.embedded == []
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute("UPDATE records SET text = 'The garden is dull' WHERE id = 'r3'")
    _drain(path, model)
    assert model.embedded == ["The garden is dull"]


def test_vectors_of_another_model_are_ignored_and_rebuilt(tmp_path: Path) -> None:
    """A new model id sees no old vectors and replaces them all."""
    path = _store(tmp_path)
    old, new = FakeEmbedder("fake-1"), FakeEmbedder("fake-2")
    _drain(path, old)
    with closing(open_memory_db(path)) as conn:
        assert meaning.nearest(conn, path, new, "interview", "records", limit=5, margin=0.06) == []
    _drain(path, new)
    assert len(new.embedded) == len(RECORDS)
    with closing(open_memory_db(path)) as conn:
        models = {m for (m,) in conn.execute("SELECT model FROM meaning_vectors")}
    assert models == {"fake-2"}


def test_a_removed_text_loses_its_vectors(tmp_path: Path) -> None:
    """A deleted record's vector is dropped on the next pass."""
    path, model = _store(tmp_path), FakeEmbedder()
    _drain(path, model)
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute("DELETE FROM records WHERE id = 'r2'")
    _drain(path, model)
    with closing(open_memory_db(path)) as conn:
        refs = {r for (r,) in conn.execute("SELECT ref FROM meaning_vectors")}
    assert refs == {"r1", "r3", "r4"}


def test_core_items_and_their_earlier_wordings_are_indexed_by_kind(tmp_path: Path) -> None:
    """A core item is kind core; each earlier wording is kind core_past."""
    path = _store(tmp_path)
    item = {"id": "c1", "text": "Likes the garden", "history": ["Likes the weather"]}
    with closing(open_memory_db(path)) as conn, conn:
        doc = core_memory.empty_doc()
        doc[core_memory.DEFAULT_SECTION].append(item)
        core_memory.append_version(
            conn, base=None, doc=doc, origin="test", upto_day=None, changes=[],
            now="2026-10-05T00:00:00-07:00",
        )
    _drain(path, FakeEmbedder())
    with closing(open_memory_db(path)) as conn:
        kinds = dict(conn.execute("SELECT ref, kind FROM meaning_vectors WHERE ref LIKE 'c1%'"))
    assert kinds == {"c1": "core", "c1:0": "core_past"}


def test_fusion_keeps_an_exact_hit_first_and_adds_a_semantic_only_hit() -> None:
    """Reciprocal rank fusion puts a hit both lists share first."""
    assert meaning.fuse(["exact", "other"], ["close", "exact"]) == ["exact", "close", "other"]
    assert meaning.fuse(["a"], []) == ["a"]


def test_the_tool_finds_a_line_by_meaning_when_no_word_matches(tmp_path: Path) -> None:
    """The tool returns the word hit first and the meaning-only hit after it."""
    path, model = _store(tmp_path), FakeEmbedder()
    _drain(path, model)
    # "面试" shares no word with r1, which says "interview"; r4 holds the word itself.
    assert _ids(path, None, query="面试") == ["r4"]
    assert _ids(path, model, query="面试") == ["r4", "r1"]


def test_the_question_in_both_languages_finds_what_either_finds(tmp_path: Path) -> None:
    """A list of phrasings: words of each count, and each is ranked by meaning, then merged."""
    path, model = _store(tmp_path), FakeEmbedder()
    _drain(path, model)
    assert sorted(_ids(path, None, query=["面试", "garden"])) == ["r3", "r4"]
    # "天气" holds no word of any line; by meaning it reaches r2 ("rain"), "garden" reaches r3.
    assert sorted(_ids(path, model, query=["天气", "garden"])) == ["r2", "r3"]


def test_without_the_model_the_tool_searches_by_words_only(tmp_path: Path) -> None:
    """A missing model leaves the word results unchanged and embeds nothing."""
    path = _store(tmp_path)
    gone = FakeEmbedder(present=False)
    assert _ids(path, gone, query="面试") == _ids(path, None, query="面试") == ["r4"]
    assert gone.embedded == []


def test_a_store_with_no_vectors_yet_still_answers_by_words(tmp_path: Path) -> None:
    """Before the first indexing pass the tool answers by words."""
    path = _store(tmp_path)
    assert _ids(path, FakeEmbedder(), query="面试") == ["r4"]


def test_a_store_from_before_the_vector_table_still_answers(tmp_path: Path) -> None:
    """A store without the vector table answers by words."""
    path = _store(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE meaning_vectors")
    assert _ids(path, FakeEmbedder(), query="面试") == ["r4"]


@pytest.mark.skipif(
    not os.environ.get("JARVIS_MEANING_MODEL_DIR"), reason="JARVIS_MEANING_MODEL_DIR not set",
)
def test_the_real_model_finds_an_english_passage_by_a_chinese_question() -> None:
    """A Chinese question scores the English passage of the same meaning highest."""
    model = E5Embedder(Path(os.environ["JARVIS_MEANING_MODEL_DIR"]))
    if not model.available:
        pytest.skip("model files absent")
    passages = ["Remind me about the Shopify interview on Friday", "Mostly rain in the morning"]
    scores = model.embed(passages, query=False) @ model.embed(["我什么时候面试"], query=True)[0]
    assert int(np.argmax(scores)) == 0
