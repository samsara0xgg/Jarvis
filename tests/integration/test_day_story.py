"""Her account of a day, as the memory store gives it back (ADR 0349; ADR 0235).

The day plan's draft reads the last days as he lived them, with her account of each when she wrote
one. Real memory.db in ``tmp_path``, the rows written by the same functions the night and the
drafts use.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from jarvis.state.memory_db import append_day_draft, append_day_prose, day_story

if TYPE_CHECKING:
    from pathlib import Path


def _prose(db: Path, day: str, text: str) -> str:
    return append_day_prose(
        db, day=day, text=text, model="m", input_chars=1, output_chars=len(text),
    )


def _written_at(db: Path, prose_id: str, ts: str) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE day_prose SET ts = ? WHERE id = ?", (ts, prose_id))


def test_her_account_of_a_day_is_the_newest_text_written_for_it(tmp_path: Path) -> None:
    """The newest "The day" text of that day only; another day's text is not it."""
    db = tmp_path / "memory.db"
    older = _prose(db, "2026-10-20", "She stayed in.")
    newer = _prose(db, "2026-10-20", "She went out after all.")
    _prose(db, "2026-10-21", "A different day.")
    _written_at(db, older, "2026-10-21T04:00:00+00:00")
    _written_at(db, newer, "2026-10-21T05:00:00+00:00")

    assert day_story(db, "2026-10-20") == "She went out after all."
    assert day_story(db, "2026-10-21") == "A different day."
    assert day_story(db, "2026-10-19") is None


def test_a_draft_or_an_empty_text_is_no_story(tmp_path: Path) -> None:
    """A draft of a day under way has no text of its own; a text of nothing is not one."""
    db = tmp_path / "memory.db"
    append_day_draft(
        db, day="2026-10-20", line="So far", highlights=[], model="m", input_chars=1,
        output_chars=1,
    )
    assert day_story(db, "2026-10-20") is None
    _prose(db, "2026-10-21", "")
    assert day_story(db, "2026-10-21") is None


def test_asking_for_a_story_never_makes_the_store(tmp_path: Path) -> None:
    """A store that is not there, or is not a database, has no story and is left as it was."""
    missing = tmp_path / "memory.db"
    assert day_story(missing, "2026-10-20") is None
    assert not missing.exists()
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    assert day_story(garbage, "2026-10-20") is None
