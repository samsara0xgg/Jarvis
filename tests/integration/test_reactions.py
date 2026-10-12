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

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared.emoji import is_single_emoji
from jarvis.state import reactions
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_record,
    conversation_rows,
    message_record,
    render_context,
)
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - the autouse fixture the peer needs
    _Peer,
    _spoken_runtime,
)
from tests.integration.test_wire_routine_streaming import _drive, _rows

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import Event

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


# --- her reaction through the turn driver -------------------------------------------------------

THUMB = "\U0001f44d"
EYES = reactions.EYES


def reaction_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The payload of every conversation.reaction, oldest first."""
    return [
        json.loads(str(payload))
        for (payload,) in conn.execute(
            "SELECT payload_json FROM events WHERE type = 'conversation.reaction' ORDER BY id",
        )
    ]


def memory_world(tmp_path: Path, url: str) -> Any:  # noqa: ANN401 - JarvisRuntime
    """A spoken-route runtime on a localhost peer, with a memory.db."""
    runtime = _spoken_runtime(tmp_path, url)
    return replace(
        runtime,
        memory=MemorySettings(
            db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio",
        ),
    )


def turn(runtime: Any, said: str, channel: str, turn_id: str) -> Event:  # noqa: ANN401
    """Drive one whole turn of words on ``channel``; the opening event."""
    intent = emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={"transcript": said, "turn_id": turn_id, "channel": channel},
        correlation={"turn_id": turn_id},
    )
    _drive(runtime, intent)
    return intent


def test_a_typed_reply_that_is_only_her_reaction_leaves_one_line_and_no_text_row(
    tmp_path: Path,
) -> None:
    """History shows she reacted; the conversation shows no empty answer from her."""
    with _Peer([[("final_answer", f"<react>{THUMB}</react>")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        intent = turn(runtime, "我到家了", "cli_stdin", "T-typed")
    memory = tmp_path / "memory.db"

    assert [(rid, source) for rid, source, _ in lines(memory)] == [
        (intent.event_uid, "allen"),
        (f"reaction:{_rows(runtime.conn, 'surface.response_emitted')[0][4]}", "jarvis_reaction"),
    ]
    assert lines(memory)[1][2] == f"[You reacted {THUMB}]"
    assert reaction_events(runtime.conn) == [
        {"target": intent.event_uid, "by": "jarvis", "emoji": THUMB},
    ]
    assert _rows(runtime.conn, "turn.failed", "response.failed") == []
    assert len(_rows(runtime.conn, "turn.ended", "response.completed")) == 2
    rows, _ = conversation_rows(memory)
    assert [row["source"] for row in rows] == ["allen"]
    assert [turn_["role"] for turn_ in render_context(memory, exclude_id="live").history] == [
        "user", "assistant",
    ]


def test_a_typed_reply_with_words_and_a_tag_keeps_the_words_and_adds_the_line(
    tmp_path: Path,
) -> None:
    """The row has the words alone; her reaction comes after it, in her role."""
    with _Peer([[("final_answer", f"到家就好。<react>{THUMB}</react>")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        intent = turn(runtime, "我到家了", "cli_stdin", "T-both")

    got = lines(tmp_path / "memory.db")
    assert [(source, text) for _, source, text in got] == [
        ("allen", "我到家了"),
        ("jarvis", "到家就好。"),
        ("jarvis_reaction", f"[You reacted {THUMB}]"),
    ]
    assert reaction_events(runtime.conn)[0]["target"] == intent.event_uid


def test_a_spoken_reply_that_is_only_a_tag_keeps_the_empty_answer_it_always_had(
    tmp_path: Path,
) -> None:
    """Nothing new is invented for a spoken turn: an empty answer row, and the reaction."""
    with _Peer([[("final_answer", f"<react>{THUMB}</react>")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        turn(runtime, "我到家了", "inherent_ptt", "T-spoken")

    assert [(source, text) for _, source, text in lines(tmp_path / "memory.db")] == [
        ("allen", "我到家了"), ("jarvis", ""), ("jarvis_reaction", f"[You reacted {THUMB}]"),
    ]
    assert len(reaction_events(runtime.conn)) == 1


def test_a_reply_without_the_tag_writes_no_reaction(tmp_path: Path) -> None:
    """The ordinary turn writes no event and no line."""
    with _Peer([[("final_answer", "到家就好。")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        turn(runtime, "我到家了", "cli_stdin", "T-plain")

    assert [source for _, source, _ in lines(tmp_path / "memory.db")] == ["allen", "jarvis"]
    assert reaction_events(runtime.conn) == []


# --- the program's eyes -------------------------------------------------------------------------


def typed(conn: sqlite3.Connection, n: int, channel: str = "cli_stdin") -> Event:
    """One message of his, arrived."""
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": f"m{n}", "turn_id": f"T{n}", "channel": channel},
        correlation={"turn_id": f"T{n}"},
    )


def test_a_typed_message_that_arrives_mid_turn_gets_the_eyes_and_nothing_else_does(
    tmp_path: Path,
) -> None:
    """Only a typed message with an unfinished turn of his before it; nothing removes them."""
    log = tmp_path / "events.db"
    conn = open_event_log(log)
    first, second, spoken = typed(conn, 1), typed(conn, 2), typed(conn, 3, "inherent_ptt")
    for event in (first, second, spoken):
        assert inherent_loop._claim_intent_in_thread(log, event)  # noqa: SLF001
        inherent_loop._mark_seen(log, event)  # noqa: SLF001
    assert reaction_events(conn) == [{"target": second.event_uid, "by": "jarvis", "emoji": EYES}]

    for turn_id in ("T1", "T2", "T3"):  # all answered
        emit_event(conn, type="turn.ended", payload={
            "turn_id": turn_id, "final_response_hash": "h", "consumed_trigger_event_uid": "x",
        })
    late = typed(conn, 4)
    inherent_loop._claim_intent_in_thread(log, late)  # noqa: SLF001
    inherent_loop._mark_seen(log, late)  # noqa: SLF001
    assert len(reaction_events(conn)) == 1, "no turn of his was in flight"
    assert reactions.current(conn, [second.event_uid]) == {second.event_uid: {"jarvis": EYES}}
    conn.close()
