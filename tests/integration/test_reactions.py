"""ADR 0256: he and she can put one emoji on a message, and each sees the other's.

Acceptance checks over a real memory.db and event log, the real turn driver and the real routes.
No network and no model: the reply is scripted.

- one emoji is one grapheme cluster with the emoji property, and nothing else is;
- `<react>EMOJI</react>` is cut out of a reply wherever the model puts it, streamed in pieces
  or whole, and the first valid one is the reaction;
- a reaction is an event, the newest per (target, by) is the one now, and a null takes it off;
- history gets one appended line per reaction (his `user`, hers `assistant`), the earlier
  messages stay byte-identical, and the lines are never rows of the conversation window;
- conversation rows carry `reactions` for his and her messages only;
- a typed reply that is only the tag writes no text row and one reaction line and passes the
  answer gates; a spoken one follows the empty-answer behaviour it always had;
- `POST /inherent/reactions` checks the target and the emoji, appends the line and never starts
  a turn; a message that arrives while another is answered gets the eyes from the program;
- the phone socket sends a `reaction` frame to every phone and `said` carries `record_id`.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared.emoji import is_single_emoji
from jarvis.state import reactions
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    append_record,
    conversation_rows,
    message_record,
    render_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

GOOD_EMOJI = (
    "\U0001f44d",
    "\u2764\ufe0f",
    "\u2764",
    "\U0001f44d\U0001f3fd",
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",
    "\U0001f1e8\U0001f1e6",
    "1\ufe0f⃣",
    "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "\U0001f3f3\ufe0f\u200d\U0001f308",
    "\U0001f602",
    "\U0001f525",
)
BAD_EMOJI = (
    "",
    "a",
    "1",
    "hi",
    "\U0001f44d\U0001f44d",
    "\U0001f44d ",
    " \U0001f44d",
    "\U0001f44da",
    "\U0001f3fd",
)


@pytest.mark.parametrize("text", GOOD_EMOJI)
def test_one_emoji_is_accepted(text: str) -> None:
    """Single code points, joined sequences, tones, flags and keycaps are one emoji each."""
    assert is_single_emoji(text)


@pytest.mark.parametrize("text", BAD_EMOJI)
def test_anything_else_is_refused(text: str) -> None:
    """Empty, words, a bare digit, two emoji, spaces and a lone modifier are not one emoji."""
    assert not is_single_emoji(text)


# --- the store ------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    """A memory.db with one exchange and an event log, both fresh."""
    memory = tmp_path / "memory.db"
    append_record(memory, record_id="u1", source="allen", text="明天去哪里吃饭好呢")
    append_record(memory, record_id="a1", source="jarvis", text="去楼下那家面馆吧。")
    append_record(memory, record_id="m1", source="mail", text="a letter")
    append_record(memory, record_id="p1", source="picture_note", text="[Picture note]\na bus")
    conn = open_event_log(tmp_path / "events.db")
    yield memory, conn
    conn.close()


def lines(memory: Path) -> list[tuple[str, str, str]]:
    """(id, source, text) of every record, oldest first."""
    with closing(sqlite3.connect(memory)) as conn:
        return [
            (str(a), str(b), str(c))
            for a, b, c in conn.execute("SELECT id, source, text FROM records ORDER BY rowid")
        ]


def test_the_newest_event_per_target_and_side_is_the_reaction_now(
    store: tuple[Path, sqlite3.Connection],
) -> None:
    """Replacing and removing are events; a side with nothing left is not listed."""
    _, conn = store
    reactions.emit(conn, target="a1", by="allen", emoji="\U0001f44d")
    reactions.emit(conn, target="a1", by="jarvis", emoji="\U0001f440")
    reactions.emit(conn, target="u1", by="allen", emoji="\U0001f602")
    reactions.emit(conn, target="a1", by="allen", emoji="\u2764\ufe0f")
    reactions.emit(conn, target="u1", by="allen", emoji=None)

    assert reactions.current(conn, ["a1", "u1", "nope"]) == {
        "a1": {"allen": "\u2764\ufe0f", "jarvis": "\U0001f440"},
    }
    assert reactions.current(conn, ["u1"]) == {}


def test_his_reaction_appends_a_user_line_and_taking_it_back_a_second(
    store: tuple[Path, sqlite3.Connection],
) -> None:
    """The line quotes the first 60 characters; a repeat changes nothing; his own word."""
    memory, conn = store
    long_answer = "好的" * 60
    append_record(memory, record_id="a2", source="jarvis", text=long_answer)

    assert reactions.allen_reacts(conn, memory, target="a2", emoji="\U0001f44d")
    assert not reactions.allen_reacts(conn, memory, target="a2", emoji="\U0001f44d")
    assert reactions.allen_reacts(conn, memory, target="a2", emoji=None)
    assert not reactions.allen_reacts(conn, memory, target="a2", emoji=None)

    quoted = long_answer[:60]
    assert [(source, text) for _, source, text in lines(memory)[-2:]] == [
        ("allen_reaction", f"[He reacted \U0001f44d to: «{quoted}»]"),
        ("allen_reaction", f"[He took back \U0001f44d from: «{quoted}»]"),
    ]
    assert reactions.current(conn, ["a2"]) == {}


@pytest.mark.parametrize("target", ["m1", "p1", "missing"])
def test_only_his_and_her_messages_can_be_reacted_to(
    store: tuple[Path, sqlite3.Connection], target: str,
) -> None:
    """Mail, a picture note and an unknown id are refused, and nothing is written."""
    memory, conn = store
    before = lines(memory)
    with pytest.raises(LookupError):
        reactions.allen_reacts(conn, memory, target=target, emoji="\U0001f44d")
    assert lines(memory) == before
    assert reactions.current(conn, [target]) == {}
    assert message_record(memory, target) is None
    assert message_record(memory, "u1") == ("allen", "明天去哪里吃饭好呢")


def test_a_reaction_line_is_not_a_message_to_react_to(
    store: tuple[Path, sqlite3.Connection],
) -> None:
    """The record a reaction appended is refused as a target."""
    memory, conn = store
    reactions.allen_reacts(conn, memory, target="u1", emoji="\U0001f44d")
    (line_id, *_), = [row for row in lines(memory) if row[1] == "allen_reaction"]
    with pytest.raises(LookupError):
        reactions.allen_reacts(conn, memory, target=line_id, emoji="\U0001f44d")


def test_history_gets_the_lines_at_its_end_and_its_front_stays_byte_identical(
    store: tuple[Path, sqlite3.Connection],
) -> None:
    """His line is `user`, hers `assistant`; adding them changes no earlier message."""
    memory, conn = store
    before = render_context(memory, exclude_id="live").history

    reactions.allen_reacts(conn, memory, target="a1", emoji="\U0001f44d")
    reactions.jarvis_reacts(
        conn, memory, target="u1", emoji="\u2764\ufe0f", record_id="reaction:r1",
    )
    after = render_context(memory, exclude_id="live").history

    assert after[: len(before) - 1] == before[:-1], "an earlier message changed"
    assert str(after[len(before) - 1]["content"]).startswith(str(before[-1]["content"]))
    assert after[-2]["role"] == "user"
    assert str(after[-2]["content"]).endswith("[He reacted \U0001f44d to: «去楼下那家面馆吧。»]")
    assert after[-1] == {"role": "assistant", "content": "[You reacted \u2764\ufe0f]"}


def test_reaction_lines_are_never_rows_and_messages_carry_what_is_on_them(
    store: tuple[Path, sqlite3.Connection], tmp_path: Path,
) -> None:
    """The window hides the lines; a message row lists both sides, mail and notes do not."""
    memory, conn = store
    reactions.allen_reacts(conn, memory, target="a1", emoji="\U0001f44d")
    reactions.jarvis_reacts(conn, memory, target="u1", emoji="\U0001f440", record_id="reaction:r1")

    rows, more = conversation_rows(memory)
    assert [row["id"] for row in rows] == ["u1", "a1", "m1"]
    assert not more

    inherent_loop._add_reactions(tmp_path / "events.db", rows)  # noqa: SLF001
    assert [row.get("reactions") for row in rows] == [
        {"jarvis": "\U0001f440"}, {"allen": "\U0001f44d"}, None,
    ]
    after, _ = conversation_rows(memory, after=rows[-1]["seq"])  # type: ignore[arg-type]
    assert after == [], "the lines past the cursor are hidden too"
