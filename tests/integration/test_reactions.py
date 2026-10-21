"""ADR 0257: he and she can put one emoji on a message, and each sees the other's.

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

import functools
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime import inherent_loop
from jarvis.runtime.daemon import output_watchers
from jarvis.shared.emoji import is_single_emoji
from jarvis.state import reactions
from jarvis.state.device_tokens import device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_record,
    conversation_rows,
    message_record,
    render_context,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from tests.support.phone import StepsHost, frames_of
from tests.support.routine import drive_streaming_turn, logged_rows
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - the autouse fixture the peer needs
    spoken_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx2 import Response  # what the test client answers with

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

    output_watchers.add_reactions(tmp_path / "events.db", rows)
    assert [row.get("reactions") for row in rows] == [
        {"jarvis": "\U0001f440"}, {"allen": "\U0001f44d"}, None,
    ]
    after, _ = conversation_rows(memory, after=rows[-1]["seq"])  # type: ignore[arg-type]
    assert after == [], "the lines past the cursor are hidden too"


# --- her reaction through the turn driver -------------------------------------------------------

THUMB = "\U0001f44d"
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
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
    runtime = spoken_runtime(tmp_path, url)
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
    drive_streaming_turn(runtime, intent)
    return intent


def test_a_typed_reply_that_is_only_her_reaction_leaves_one_line_and_no_text_row(
    tmp_path: Path,
) -> None:
    """History shows she reacted; the conversation shows no empty answer from her."""
    with SpokenPeer([[("final_answer", f"<react>{THUMB}</react>")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        intent = turn(runtime, "我到家了", "cli_stdin", "T-typed")
    memory = tmp_path / "memory.db"

    assert [(rid, source) for rid, source, _ in lines(memory)] == [
        (intent.event_uid, "allen"),
        (
            f"reaction:{logged_rows(runtime.conn, 'surface.response_emitted')[0][4]}",
            "jarvis_reaction",
        ),
    ]
    assert lines(memory)[1][2] == f"[You reacted {THUMB}]"
    assert reaction_events(runtime.conn) == [
        {"target": intent.event_uid, "by": "jarvis", "emoji": THUMB},
    ]
    assert logged_rows(runtime.conn, "turn.failed", "response.failed") == []
    assert len(logged_rows(runtime.conn, "turn.ended", "response.completed")) == 2
    rows, _ = conversation_rows(memory)
    assert [row["source"] for row in rows] == ["allen"]
    assert [turn_["role"] for turn_ in render_context(memory, exclude_id="live").history] == [
        "user",
        "assistant",
    ]


def test_a_typed_reply_with_words_and_a_tag_keeps_the_words_and_adds_the_line(
    tmp_path: Path,
) -> None:
    """The row has the words alone; her reaction comes after it, in her role."""
    with SpokenPeer([[("final_answer", f"到家就好。<react>{THUMB}</react>")]]) as peer:
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
    with SpokenPeer([[("final_answer", f"<react>{THUMB}</react>")]]) as peer:
        runtime = memory_world(tmp_path, peer.url)
        turn(runtime, "我到家了", "inherent_ptt", "T-spoken")

    assert [(source, text) for _, source, text in lines(tmp_path / "memory.db")] == [
        ("allen", "我到家了"),
        ("jarvis", ""),
        ("jarvis_reaction", f"[You reacted {THUMB}]"),
    ]
    assert len(reaction_events(runtime.conn)) == 1


def test_a_reply_without_the_tag_writes_no_reaction(tmp_path: Path) -> None:
    """The ordinary turn writes no event and no line."""
    with SpokenPeer([[("final_answer", "到家就好。")]]) as peer:
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
        emit_event(
            conn,
            type="turn.ended",
            payload={
                "turn_id": turn_id,
                "final_response_hash": "h",
                "consumed_trigger_event_uid": "x",
            },
        )
    late = typed(conn, 4)
    inherent_loop._claim_intent_in_thread(log, late)  # noqa: SLF001
    inherent_loop._mark_seen(log, late)  # noqa: SLF001
    assert len(reaction_events(conn)) == 1, "no turn of his was in flight"
    assert reactions.current(conn, [second.event_uid]) == {second.event_uid: {"jarvis": EYES}}
    conn.close()


# --- POST /inherent/reactions -------------------------------------------------------------------

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"


class Daemon:
    """The app behind the local key and a paired phone's token, on a real log and memory.db."""

    def __init__(self, tmp_path: Path) -> None:
        """Seed one exchange, a mail and a picture note, and wire the real reaction callable."""
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.log = self.root / "events.db"
        open_event_log(self.log).close()
        self.memory = self.root / "memory.db"
        for rid, source, text in (
            ("u1", "allen", "明天去哪里吃饭好呢"),
            ("a1", "jarvis", "去楼下那家面馆吧。"),
            ("m1", "mail", "a letter"),
            ("p1", "picture_note", "[Picture note]\na bus"),
        ):
            append_record(self.memory, record_id=rid, source=source, text=text)
        self.turns: list[str] = []
        deps = InherentDeps(
            submit_callable=self._submit,
            broadcaster=InherentBroadcaster(),
            reaction_set=functools.partial(inherent_loop._set_reaction, self.log, self.memory),  # noqa: SLF001
        )
        app = create_app(deps)
        self.key = local_key(self.root)
        require_local_key(
            app,
            functools.partial(local_key_matches, self.key),
            extra_hosts=[REMOTE],
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.token = pair_device(self.root, "iphone")
        self.remote = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))
        loopback = f"http://{LOOPBACK}:8006"
        self.loopback = TestClient(app, base_url=loopback, client=(LOOPBACK, 50000))

    def _submit(self, text: str) -> str:
        self.turns.append(text)
        return "T1"

    def react(self, body: dict[str, Any]) -> Response:
        """Post a reaction as the paired phone."""
        return self.remote.post(
            "/inherent/reactions",
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def now(self) -> dict[str, dict[str, str]]:
        """The reactions on the seeded messages now."""
        with closing(open_event_log(self.log)) as conn:
            return reactions.current(conn, ["u1", "a1"])


def test_his_reaction_is_kept_replaced_and_taken_off_and_never_starts_a_turn(
    tmp_path: Path,
) -> None:
    """Each change answers what is on the message, writes its line, and runs no turn."""
    d = Daemon(tmp_path)

    first = d.react({"target": "a1", "emoji": THUMB})
    assert (first.status_code, first.json()) == (
        200,
        {"target": "a1", "by": "allen", "emoji": THUMB},
    )
    assert d.react({"target": "a1", "emoji": "\u2764\ufe0f"}).status_code == 200
    assert d.react({"target": "u1", "emoji": FAMILY}).is_success
    assert d.now() == {"a1": {"allen": "\u2764\ufe0f"}, "u1": {"allen": FAMILY}}
    assert d.react({"target": "a1", "emoji": None}).json()["emoji"] is None
    assert d.react({"target": "a1", "emoji": None}).is_success, "nothing to take off is no error"

    assert [text for _, source, text in lines(d.memory) if source == "allen_reaction"] == [
        f"[He reacted {THUMB} to: «去楼下那家面馆吧。»]",
        "[He reacted \u2764\ufe0f to: «去楼下那家面馆吧。»]",
        f"[He reacted {FAMILY} to: «明天去哪里吃饭好呢»]",
        "[He took back \u2764\ufe0f from: «去楼下那家面馆吧。»]",
    ]
    assert d.turns == []
    with closing(open_event_log(d.log)) as conn:
        assert logged_rows(conn, "surface.user_intent", "turn.started") == []


@pytest.mark.parametrize(
    "body",
    [
        {"target": "u1", "emoji": "\U0001f44d\U0001f44d"},
        {"target": "u1", "emoji": "hello"},
        {"target": "u1", "emoji": ""},
        {"target": "u1", "emoji": "1"},
        {"target": "u1", "emoji": f" {THUMB}"},
        {"target": "m1", "emoji": THUMB},
        {"target": "p1", "emoji": THUMB},
        {"target": "nope", "emoji": THUMB},
    ],
)
def test_anything_but_one_emoji_on_a_message_is_a_400_and_writes_nothing(
    tmp_path: Path,
    body: dict[str, Any],
) -> None:
    """Two emoji, words, nothing, a digit, a space, mail, a picture note and an unknown id."""
    d = Daemon(tmp_path)
    before = lines(d.memory)
    assert d.react(body).status_code == 400
    assert lines(d.memory) == before
    assert d.now() == {}


def test_a_reaction_line_cannot_itself_be_reacted_to(tmp_path: Path) -> None:
    """The record a reaction left is not a message."""
    d = Daemon(tmp_path)
    d.react({"target": "u1", "emoji": THUMB})
    ((line_id, *_),) = [row for row in lines(d.memory) if row[1] == "allen_reaction"]
    assert d.react({"target": line_id, "emoji": THUMB}).status_code == 400


def test_the_route_is_behind_the_same_credentials_as_the_rest(tmp_path: Path) -> None:
    """The paired token from the tailnet and the local key on loopback; every other is 401."""
    d = Daemon(tmp_path)
    body = {"target": "u1", "emoji": THUMB}
    local = {"Authorization": f"Bearer {d.key}"}
    wrong = {"Authorization": "Bearer nope"}

    def post(client: TestClient, headers: dict[str, str]) -> int:
        return client.post("/inherent/reactions", json=body, headers=headers).status_code

    assert post(d.remote, {"Authorization": f"Bearer {d.token}"}) == 200
    assert post(d.loopback, local) == 200
    assert [
        post(d.remote, {}),
        post(d.remote, wrong),
        post(d.remote, local),
        post(d.loopback, {}),
        post(d.loopback, wrong),
    ] == [401] * 5
    assert d.react({"target": "u1"}).status_code == 422, "the emoji field is required, even null"


# --- the phone socket ---------------------------------------------------------------------------


def _three(phone: Any) -> bool:  # noqa: ANN401 - the fake phone
    return len(frames_of(phone, "reaction")) == 3


def test_every_phone_gets_every_reaction_and_said_names_the_record(tmp_path: Path) -> None:
    """A reaction frame reaches a phone that opened no turn; `said` carries the record id."""
    with StepsHost(tmp_path) as host:
        mine, other = host.phone(), host.phone(host.other_token)
        try:
            mine.say("u-typed", "我到家了", spoken=False)
            said = mine.wait_text("said")
            with closing(open_event_log(host.log)) as conn:
                (intent_uid,) = [
                    str(uid)
                    for (uid,) in conn.execute(
                        "SELECT event_uid FROM events WHERE type = 'surface.user_intent'",
                    )
                ]
            assert said["record_id"] == intent_uid, "the id his message will have as a row"

            append_record(host.memory, record_id=intent_uid, source="allen", text="我到家了")
            append_record(host.memory, record_id="a1", source="jarvis", text="欢迎回来。")
            with closing(open_event_log(host.log)) as conn:
                reactions.allen_reacts(conn, host.memory, target="a1", emoji=THUMB)
                reactions.emit(conn, target=intent_uid, by="jarvis", emoji=EYES)
                reactions.allen_reacts(conn, host.memory, target="a1", emoji=None)
            for phone in (mine, other):
                phone.wait_until(
                    functools.partial(_three, phone),
                    "three reaction frames",
                )
                assert [
                    (f["target"], f["by"], f["emoji"]) for f in frames_of(phone, "reaction")
                ] == [("a1", "allen", THUMB), (intent_uid, "jarvis", EYES), ("a1", "allen", None)]
            assert not other.rows(), "the other phone opened no turn, so it has no rows"

            late = host.phone()  # a phone that connects later reads the rows, not the past events
            time.sleep(0.3)
            assert frames_of(late, "reaction") == []
            late.close()
        finally:
            mine.close()
            other.close()


def test_the_turn_after_a_reaction_only_answer_is_not_told_that_one_was_interrupted(
    tmp_path: Path,
) -> None:
    """An answer of only a reaction is an answer: the next turn's state says nothing of it."""
    with SpokenPeer(
        [[("final_answer", f"<react>{THUMB}</react>")], [("final_answer", "好。")]]
    ) as peer:
        runtime = memory_world(tmp_path, peer.url)
        turn(runtime, "我到家了", "cli_stdin", "T-first")
        turn(runtime, "晚饭吃什么", "cli_stdin", "T-second")

    sent = json.dumps(peer.requests[1][1]["input"], ensure_ascii=False)
    assert "晚饭吃什么" in sent
    assert "interrupted" not in sent
