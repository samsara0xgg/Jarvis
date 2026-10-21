"""The Live startup brief (``brief_note``): the same text whatever it costs to read.

The brief is rendered when a Live session starts, from the whole history. Two things are pinned:
the text under each budget on a fixed store (the output the original, quadratic trimming loop
gave), and that the work does not follow the length of the conversation.
"""

# ruff: noqa: E501, RUF001 — Chinese lines and the expected text read best one entry to a line
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from jarvis.state import memory_db
from jarvis.state.memory_db import append_summary, brief_note, open_memory_db

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime.fromisoformat("2026-10-21T12:00:00-07:00")
SAID = [
    ("allen", "早上好"), ("jarvis", "早，今天有三件事。"), ("allen", "先说第一件"),
    ("jarvis", "十点和 Ada 开会。"), ("allen", "move it to eleven"), ("jarvis", "Moved to 11:00."),
    ("allen", "thanks"), ("jarvis_live", "You're welcome."), ("allen", "午饭吃什么"),
    ("jarvis", "附近有一家面馆。"), ("allen", "好，就那家"), ("jarvis", "已经记下了。"),
    ("allen", "再见"), ("jarvis", "再见。"),
]
SUMMARY = (
    "# Earlier\nThe user planned the day.\n### Meetings\nAda at ten.\n### Food\nNoodles nearby."
    "\n### Mood\nCalm."
)


def _store(root: Path, said: list[tuple[str, str]], *, summary: bool = False) -> Path:
    """A memory.db holding ``said`` four minutes apart, the last 2 h 8 min before ``NOW``.

    ``summary`` covers the first six.
    """
    path = root / "memory.db"
    open_memory_db(path).close()
    last = NOW - timedelta(minutes=128)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (
                    f"r{i:05d}", (last - timedelta(minutes=4 * (len(said) - 1 - i))).isoformat(),
                    who, text,
                )
                for i, (who, text) in enumerate(said)
            ],
        )
    if summary:
        append_summary(
            path, base_id=None, upto_record_id="r00005", summary=SUMMARY, model="m",
            input_chars=1, output_chars=1,
        )
    return path


HEAD = ("[About the user]", "Time: 2026-10-21T12:00-07:00 Wednesday · 2 h 8 min since the last exchange")
RECORDS = "[Conversation records, oldest first]"
BY_SUMMARY = "[Conversation summary · up to 2026-10-21T09:20:00-07:00 · ask the backend for exact words]"
LAST_TWO = (
    "[2026-10-21T09:48:00-07:00] user: 再见",
    "[2026-10-21T09:52:00-07:00] jarvis: 再见。",
)
LAST_SIX = (
    "[2026-10-21T09:32:00-07:00] user: 午饭吃什么",
    "[2026-10-21T09:36:00-07:00] jarvis: 附近有一家面馆。",
    "[2026-10-21T09:40:00-07:00] user: 好，就那家",
    "[2026-10-21T09:44:00-07:00] jarvis: 已经记下了。",
    *LAST_TWO,
)
# (a summary, the budget, the brief as the original trimming loop rendered it)
GOLDEN: list[tuple[bool, int, tuple[str, ...]]] = [
    (False, 10_000, (
        *HEAD, RECORDS,
        "[2026-10-21T09:00:00-07:00] user: 早上好",
        "[2026-10-21T09:04:00-07:00] jarvis: 早，今天有三件事。",
        "[2026-10-21T09:08:00-07:00] user: 先说第一件",
        "[2026-10-21T09:12:00-07:00] jarvis: 十点和 Ada 开会。",
        "[2026-10-21T09:16:00-07:00] user: move it to eleven",
        "[2026-10-21T09:20:00-07:00] jarvis: Moved to 11:00.",
        "[2026-10-21T09:24:00-07:00] user: thanks",
        "[2026-10-21T09:28:00-07:00] jarvis_live: You're welcome.",
        *LAST_SIX,
    )),
    (False, 400, (*HEAD, RECORDS, *LAST_SIX)),  # the oldest records go first
    (False, 0, (*HEAD, RECORDS, "(none)")),  # nothing fits: the head alone, over budget
    (True, 10_000, (
        *HEAD, BY_SUMMARY, "# Earlier", "The user planned the day.", "### Meetings", "Ada at ten.",
        "### Food", "Noodles nearby.", "### Mood", "Calm.", RECORDS,
        "[2026-10-21T09:24:00-07:00] user: thanks",
        "[2026-10-21T09:28:00-07:00] jarvis_live: You're welcome.",
        *LAST_SIX,
    )),
    (True, 400, (
        *HEAD, BY_SUMMARY, "# Earlier", "The user planned the day.", "### Meetings", "Ada at ten.",
        "### Food", "Noodles nearby.", "### Mood", "Calm.", RECORDS, *LAST_TWO,
    )),
    (True, 300, (  # no record fits: the summary's sections go from the bottom up
        *HEAD, BY_SUMMARY, "# Earlier", "The user planned the day.", "### Meetings", "Ada at ten.",
        RECORDS, "(none)",
    )),
    (True, 200, (*HEAD, BY_SUMMARY, RECORDS, "(none)")),
]


@pytest.mark.parametrize(("summary", "budget", "expected"), GOLDEN)
def test_the_brief_under_a_budget_is_the_text_the_trimming_always_gave(
    tmp_path: Path, *, summary: bool, budget: int, expected: tuple[str, ...],
) -> None:
    """Oldest records first, then the summary's sections from the bottom up."""
    path = _store(tmp_path, SAID, summary=summary)
    assert brief_note(path, max_chars=budget, now=NOW) == "\n".join(expected)


def test_a_brief_reads_no_more_of_the_conversation_than_it_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten times the history costs the database the same work: SQLite's step count is the measure."""
    steps = {"n": 0}
    opened = memory_db.open_memory_db

    def counted(path: Path) -> sqlite3.Connection:
        conn = opened(path)

        def tick() -> int:
            steps["n"] += 1
            return 0  # carry on

        conn.set_progress_handler(tick, 10)
        return conn

    monkeypatch.setattr(memory_db, "open_memory_db", counted)
    work = {}
    for length in (400, 4000):
        said = [("allen" if i % 2 else "jarvis", f"record number {i}") for i in range(length)]
        root = tmp_path / str(length)
        root.mkdir()
        path = _store(root, said)
        steps["n"] = 0
        brief = brief_note(path, max_chars=1500, now=NOW)
        work[length] = steps["n"]
        assert f"record number {length - 1}" in brief
        assert len(brief) <= 1500
    assert work[400] > 0  # the count is wired
    assert work[4000] <= 2 * work[400]
