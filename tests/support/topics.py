"""A store of topics (ADR 0390) over seeded messages, made by the store's own writes."""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jarvis.state import topics
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
"""The zone the seeded stamps are written in and their days are cut in."""

MESSAGES = (
    # 2026-10-11: the pottery class comes up, with her answer, and a line about lunch
    ("a1", "2026-10-11T09:00:00-07:00", "allen", "周六的陶艺课我报名了"),
    ("a2", "2026-10-11T09:00:05-07:00", "jarvis", "好的 周六上午十点"),
    ("a3", "2026-10-11T12:30:00-07:00", "allen", "午饭吃什么"),
    ("n1", "2026-10-11T13:00:00-07:00", "picture_note", "[Picture note]\n一碗面"),
    # 2026-10-13: the class again, and the move
    ("b1", "2026-10-13T19:00:00-07:00", "allen", "陶艺课要带围裙吗"),
    ("b2", "2026-10-13T19:00:04-07:00", "jarvis", "要 最好带一条旧的"),
    ("b3", "2026-10-13T20:00:00-07:00", "allen", "搬家的箱子订好了"),
    # 2026-10-14: he talks inside the class topic, and once in the pottery-tools one
    ("c1", "2026-10-14T08:00:00-07:00", "allen", "陶艺课改到周日了"),
    ("c2", "2026-10-14T08:00:03-07:00", "jarvis", "记下了 周日上午十点"),
    ("c3", "2026-10-14T08:10:00-07:00", "allen", "拉坯工具在哪买"),
)
CLASS_RECAP = "他报了 2026-10-17 周六上午十点的陶艺课 要带一条旧围裙"


def seed_messages(db: Path, rows: Sequence[tuple[str, str, str, str]] = MESSAGES) -> None:
    """``rows`` into ``records`` as the store writes them: ``(id, ts, source, text)``."""
    with closing(open_memory_db(db)) as conn, conn:
        conn.executemany("INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", rows)


def base(db: Path) -> set[str]:
    """The version ids a night's plan is made from."""
    return {one.version_id for one in topics.current(db).values()}


def night(
    db: Path, day: str, *entries: topics.NightTopic, merges: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    """One night's plan applied; what it wrote."""
    written = topics.apply_night(
        db, topics.NightPlan(day, tuple(entries), tuple(merges)), base=base(db), model="test",
    )
    assert written is not None
    return written


def opened(
    name: str, *record_ids: str, line: str = "还在进行", recap: str = "",
    topic_id: str | None = None, state: topics.State = "open",
) -> topics.NightTopic:
    """A night's entry: a new topic, or ``topic_id`` with its new recap and its lines."""
    return topics.NightTopic(
        topic_id, name, "done" if state == "done" else "open", line, recap or f"{name}的前情",
        tuple(record_ids),
    )


def pottery_world(db: Path) -> None:
    """The class (t1, his talk on 10-14 inside it), the move (t2, done) and tools merged into t1.

    t3 "拉坯工具" is opened on 10-14 by a night, then merged into t1 by him.
    """
    seed_messages(db)
    night(
        db, "2026-10-13", opened("陶艺课", "a1", "a2", "b1", "b2", recap=CLASS_RECAP),
        opened("搬家", "b3", line="箱子订好了", state="done"),
    )
    assert topics.file_his(db, "t1", ["c1", "c2"]) == "t1"
    night(db, "2026-10-14", opened("拉坯工具", "c3"))
    assert topics.change(db, "t3", "merge", into="t1") is not None


__all__ = [
    "CLASS_RECAP",
    "MESSAGES",
    "ZONE",
    "base",
    "night",
    "opened",
    "pottery_world",
    "seed_messages",
]
