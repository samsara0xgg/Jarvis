"""The rows of the Event Log that name files he sent, as L2 answers it (ADR 0349).

The runtime links the records of his picture turns to their files and writes a note for each
picture. Each test writes the rows a phone's turn and a share leave in a real on-disk Event Log
and asserts which come back; times are fixed epoch milliseconds, never the wall clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.state.sent_files import rows_naming_files_between, turn_rows_naming_files_since
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path

PICTURE = "a" * 32
OTHER = "b" * 32


def _shared(log: TurnLog, share_id: str, files: list[str] | None, *, at: int) -> None:
    payload: dict[str, object] = {"share_id": share_id, "kind": "image", "note": "saved"}
    if files is not None:
        payload["attachments"] = files
    log.put("user.shared", payload, at=at)


def test_the_turns_that_came_with_files_since(tmp_path: Path) -> None:
    """Typed and heard opening rows that name files, oldest first; shares and others do not."""
    log = TurnLog(tmp_path)
    log.heard("T-old", at=900, attachments=[PICTURE])
    typed = log.put(
        "surface.user_intent",
        {"transcript": "look", "turn_id": "T1", "channel": "gpt_live", "attachments": [PICTURE]},
        turn_id="T1", at=1_000,
    )
    heard = log.heard("T2", "and this", at=2_000, attachments=[OTHER, PICTURE])
    log.typed("T3", "gpt_live", at=2_000)  # no files
    log.heard("T4", at=2_000, attachments=None)  # a null is no files either
    _shared(log, "S1", [OTHER], at=2_000)
    log.put("turn.timing", {"turn_id": "T5", "attachments": [PICTURE]}, turn_id="T5", at=2_000)

    found = turn_rows_naming_files_since(log.conn, since_ms=1_000)
    assert [row.event_uid for row in found] == [typed.event_uid, heard.event_uid]
    assert found[1].payload["attachments"] == [OTHER, PICTURE]
    assert found[0].type == "surface.user_intent"
    assert turn_rows_naming_files_since(log.conn, since_ms=2_001) == []
    assert len(turn_rows_naming_files_since(log.conn, since_ms=0)) == 3


def test_the_rows_that_named_files_in_a_window(tmp_path: Path) -> None:
    """Both ends are inside; shares come back with the turns, in the order they were written."""
    log = TurnLog(tmp_path)
    log.heard("T-old", at=900, attachments=[PICTURE])
    first = log.heard("T1", "look", at=1_000, attachments=[PICTURE])
    _shared(log, "S1", [OTHER], at=1_500)
    _shared(log, "S2", None, at=1_600)  # a link share names no files
    last = log.typed("T2", "clarify", at=2_000)
    log.put(
        "surface.user_intent",
        {"transcript": "x", "turn_id": "T3", "attachments": [PICTURE]}, turn_id="T3", at=2_000,
    )
    log.heard("T-late", at=2_001, attachments=[PICTURE])

    found = rows_naming_files_between(log.conn, since_ms=1_000, until_ms=2_000)
    assert [(row.type, row.ts_epoch_ms) for row in found] == [
        ("utterance.received", 1_000),
        ("user.shared", 1_500),
        ("surface.user_intent", 2_000),
    ]
    assert found[0].event_uid == first.event_uid
    assert found[1].payload["note"] == "saved"
    assert last.event_uid not in {row.event_uid for row in found}
    assert rows_naming_files_between(log.conn, since_ms=1_001, until_ms=1_499) == []
