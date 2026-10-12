"""ADR 0255: a picture he sends stays in the conversation as a picture.

Acceptance checks against the real turn driver, a real memory.db, event log and attachment store.
No network: the Files API is a recording fake.

- his record is linked to its files when it is written, and records from before the link are
  linked from the event log for the files still kept.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import picture_files
from jarvis.state.memory_db import append_record, link_attachments
from tests.integration.test_picture_notes import ANSWER, FILLER_EN, WORDS, World

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """A fresh runtime per test."""
    one = World(tmp_path, monkeypatch)
    yield one
    one.close()


def links(world: World) -> list[tuple[str, int, str, str, str]]:
    """Every (record, pos, attachment, kind, name) link, oldest first."""
    with closing(sqlite3.connect(world.memory.db_path)) as conn:
        return [
            (str(a), int(b), str(c), str(d), str(e)) for a, b, c, d, e in conn.execute(
                "SELECT record_id, pos, attachment_id, kind, name FROM record_attachments "
                "ORDER BY record_id, pos",
            )
        ]


def test_his_record_is_linked_to_its_files_in_the_order_he_sent_them(world: World) -> None:
    """The link table names each file by position, kind and name."""
    first, second = world.picture("IMG_1.png"), world.picture("IMG_2.png")

    world.turn(WORDS, ANSWER, (first, second))
    world.turn("谢谢", "不客气。")

    (record_id, *_), *_ = world.records()
    assert links(world) == [
        (record_id, 0, first, "image", "IMG_1.png"),
        (record_id, 1, second, "image", "IMG_2.png"),
    ]


def test_earlier_records_are_linked_from_the_event_log_for_the_files_still_kept(
    world: World,
) -> None:
    """The backfill reads the log, skips a file that is gone, and does nothing the second time."""
    kept, swept = world.picture("IMG_1.png"), world.picture("IMG_2.png")
    world.turn(FILLER_EN, ANSWER, (kept,))
    world.turn(WORDS, ANSWER, (swept,))
    with closing(sqlite3.connect(world.memory.db_path)) as conn, conn:
        conn.execute("DELETE FROM record_attachments")  # what a record from before ADR 0255 lacks
    world.store.path(swept).unlink()  # type: ignore[union-attr]
    (first, *_), *_ = world.records()

    done = picture_files.link_kept_pictures(
        world.memory.db_path, world.event_log, world.store, days=30,
    )

    assert done == 1
    assert links(world) == [(first, 0, kept, "image", "IMG_1.png")]
    assert picture_files.link_kept_pictures(
        world.memory.db_path, world.event_log, world.store, days=30,
    ) == 0, "linked once"


def test_a_record_that_is_not_there_gets_no_link(world: World) -> None:
    """A link to a record that was never written is dropped, not stored dangling."""
    picture = world.store.save(b"\x89PNG\r\n\x1a\n", "x.png", images_ok=True)
    append_record(world.memory.db_path, record_id="r1", source="allen", text="hi")
    link_attachments(world.memory.db_path, "missing", [picture])
    assert links(world) == []
