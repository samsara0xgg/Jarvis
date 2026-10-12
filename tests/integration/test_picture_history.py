"""ADR 0255: a picture he sends stays in the conversation as a picture.

Acceptance checks against the real turn driver, a real memory.db, event log and attachment store.
No network: the Files API is a recording fake.

- his record is linked to its files when it is written, and records from before the link are
  linked from the event log for the files still kept;
- on a Claude preset the history message of his turn carries its pictures as ``image_file`` parts
  after his words, the 20 newest only; a picture's note is left out only while the picture rides;
- a gone file, a failed upload and a preset that is not Anthropic's keep the marker and the note,
  and the parts are byte-identical from one render to the next;
- the request the Anthropic adapter builds names the file and sends the Files API beta header.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_anthropic import FILES_BETA, files_headers, request_fields
from jarvis.runtime import picture_files
from jarvis.runtime.picture_files import RETRY_AFTER_S, PictureFiles
from jarvis.state import picture_notes as note_records
from jarvis.state.memory_db import (
    HISTORY_PICTURES,
    append_record,
    link_attachments,
    render_context,
)
from tests.integration.test_anthropic_adaptation import _reply, _run
from tests.integration.test_phone_attachments import PNG, _daemon, _turn
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


# --- history pictures ----------------------------------------------------------------------------


class FilesClient(LLMClient):
    """An Anthropic preset whose Files API is a recording fake: no network."""

    def __init__(self, provider: str = "anthropic") -> None:
        """``provider`` is the preset's; ``openai`` stands for any other preset."""
        super().__init__({
            "provider": provider, "default_preset": "p",
            "presets": {"p": {"provider": provider, "model": "m", "max_tokens": 64}},
        })
        self.uploads: list[tuple[str, int, str]] = []
        self.failing = False

    def upload_file(self, name: str, data: bytes, mime: str) -> str:
        """Record the upload; the id is the count so far, or fail when told to."""
        if self.failing:
            msg = "files api down"
            raise RuntimeError(msg)
        self.uploads.append((name, len(data), mime))
        return f"file_{len(self.uploads):03d}"


class Clock:
    """Monotonic seconds the test moves."""

    def __init__(self) -> None:
        """Start at an arbitrary second."""
        self.now = 100.0

    def __call__(self) -> float:
        """The time now."""
        return self.now


def send(world: World, name: str, words: str = WORDS, *, note: bool = True) -> str:
    """One turn of his with a stored picture, her answer, and (by default) the picture's note."""
    picture = world.store.save(PNG, name, images_ok=True)
    marker = f"[attached: image {name}]"
    stamp = picture.id[:8]
    append_record(
        world.memory.db_path, record_id=f"u{stamp}", source="allen", text=f"{words}\n{marker}",
        attachments=[picture],
    )
    append_record(world.memory.db_path, record_id=f"a{stamp}", source="jarvis", text=ANSWER)
    if note:
        note_records.append(world.memory.db_path, picture, f"note of {name}")
    return picture.id


def render(world: World, files: PictureFiles, client: LLMClient) -> list[dict[str, Any]]:
    """History as the next request would carry it."""
    ctx = render_context(
        world.memory.db_path, exclude_id="live", pictures=files.resolver(client),
    )
    return list(ctx.history)


def texts(history: list[dict[str, Any]]) -> str:
    """All the words of a history, parts joined."""
    return "\n".join(
        turn["content"] if isinstance(turn["content"], str)
        else "\n".join(p["text"] for p in turn["content"] if p["type"] == "text")
        for turn in history
    )


def test_a_picture_rides_the_history_after_his_words_and_its_note_stays_out(world: World) -> None:
    """His message is [words + marker, image_file]; the note would only say what the image shows."""
    files, client = PictureFiles(world.store), FilesClient()
    send(world, "IMG_1.png")

    history = render(world, files, client)

    assert [turn["role"] for turn in history] == ["user", "assistant"]
    words, image = history[0]["content"]
    assert words["type"] == "text"
    assert words["text"].endswith(f"{WORDS}\n[attached: image IMG_1.png]")
    assert image == {"type": "image_file", "file_id": "file_001"}
    assert "note of IMG_1.png" not in texts(history)
    assert client.uploads == [("IMG_1.png", len(PNG), "image/png")]


def test_the_upload_happens_once_and_the_parts_are_byte_identical_from_turn_to_turn(
    world: World,
) -> None:
    """The id is kept beside the file; a later render, another process, and a new turn reuse it."""
    files, client = PictureFiles(world.store), FilesClient()
    send(world, "IMG_1.png")
    first = render(world, files, client)

    again = render(world, PictureFiles(world.store), client)  # a restarted daemon
    send(world, "IMG_2.png")
    later = render(world, files, client)

    assert json.dumps(again) == json.dumps(first)
    assert len(client.uploads) == 2, "one upload per picture, ever"
    assert json.dumps(later[:2]) == json.dumps(first), "what came before is a byte-stable prefix"


def test_only_the_newest_twenty_ride_and_an_older_one_keeps_its_marker_and_note(
    world: World,
) -> None:
    """Past the cap the picture is words again, as in ADR 0223."""
    files, client = PictureFiles(world.store), FilesClient()
    for n in range(HISTORY_PICTURES + 2):
        send(world, f"IMG_{n:02d}.png")

    history = render(world, files, client)

    user_turns = [turn for turn in history if turn["role"] == "user"]
    riding = [turn for turn in user_turns if isinstance(turn["content"], list)]
    assert len(riding) == HISTORY_PICTURES
    assert len(client.uploads) == HISTORY_PICTURES, "the two older ones are never uploaded"
    flat = texts(history)
    for n in (0, 1):
        assert f"[attached: image IMG_{n:02d}.png]" in flat
        assert f"note of IMG_{n:02d}.png" in flat
    assert "note of IMG_21.png" not in flat
    first = history[0]["content"]
    assert isinstance(first, str), "the oldest is words"


def test_a_gone_file_keeps_the_marker_and_the_note_and_costs_no_upload(world: World) -> None:
    """Swept after 30 days: nothing to send."""
    files, client = PictureFiles(world.store), FilesClient()
    gone = send(world, "IMG_1.png")
    send(world, "IMG_2.png")
    world.store.path(gone).unlink()  # type: ignore[union-attr]

    history = render(world, files, client)

    flat = texts(history)
    assert "note of IMG_1.png" in flat
    assert "note of IMG_2.png" not in flat
    assert [name for name, *_ in client.uploads] == ["IMG_2.png"]


def test_a_failed_upload_falls_back_to_words_without_raising_and_is_retried_later(
    world: World, caplog: pytest.LogCaptureFixture,
) -> None:
    """The request still goes; the picture is not hammered, and comes back when the API does."""
    clock = Clock()
    files, client = PictureFiles(world.store, clock=clock), FilesClient()
    send(world, "IMG_1.png")
    client.failing = True

    history = render(world, files, client)

    assert isinstance(history[0]["content"], str)
    assert "note of IMG_1.png" in texts(history)
    assert "could not upload" in caplog.text
    client.failing = False
    render(world, files, client)
    assert client.uploads == [], "not retried inside the window"
    clock.now += RETRY_AFTER_S + 1
    history = render(world, files, client)
    assert history[0]["content"][1] == {"type": "image_file", "file_id": "file_001"}
    assert "note of IMG_1.png" not in texts(history)


def test_another_provider_keeps_the_marker_and_the_note_and_uploads_nothing(world: World) -> None:
    """ADR 0223's history, unchanged, on a preset that is not Anthropic's."""
    files, client = PictureFiles(world.store), FilesClient("openai")
    send(world, "IMG_1.png")

    history = render(world, files, client)
    plain = render_context(world.memory.db_path, exclude_id="live").history

    assert tuple(history) == plain
    assert all(isinstance(turn["content"], str) for turn in history)
    assert "note of IMG_1.png" in texts(history)
    assert client.uploads == []


def test_a_picture_without_a_link_is_words_and_a_text_file_never_rides(world: World) -> None:
    """A record from before the link (not backfilled, file gone) and a text attachment."""
    files, client = PictureFiles(world.store), FilesClient()
    append_record(world.memory.db_path, record_id="old", source="allen",
                  text="look\n[attached: image old.jpg]")
    notes = world.store.save(b"some text", "notes.txt", images_ok=True)
    append_record(world.memory.db_path, record_id="t", source="allen",
                  text="read\n[attached: text notes.txt]", attachments=[notes])

    history = render(world, files, client)

    assert all(isinstance(turn["content"], str) for turn in history)
    assert client.uploads == []


def test_a_message_of_his_with_pictures_joins_the_user_row_that_follows_it(world: World) -> None:
    """Two rows of his in a row (no answer between) stay one message, parts after parts."""
    files, client = PictureFiles(world.store), FilesClient()
    picture = world.store.save(PNG, "IMG_1.png", images_ok=True)
    append_record(world.memory.db_path, record_id="u1", source="allen",
                  text="look\n[attached: image IMG_1.png]", attachments=[picture])
    append_record(world.memory.db_path, record_id="u2", source="allen", text="and then")

    (turn,) = render(world, files, client)

    assert [part["type"] for part in turn["content"]] == ["text", "image_file", "text"]
    assert turn["content"][2]["text"] == "and then"


# --- the request ---------------------------------------------------------------------------------


def test_the_anthropic_request_names_the_file_and_sends_the_files_beta_header(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact shape of a history picture, through the real SDK to a local peer (no network)."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    files, client = PictureFiles(world.store), FilesClient()
    send(world, "IMG_1.png")
    history = render(world, files, client)
    messages = [*history, {"role": "user", "content": "and now?"}]

    body = request_fields(messages=messages, system="S", tools=None, max_tokens=64,
                          reasoning_effort=None)
    assert body["messages"][0]["content"][1] == {
        "type": "image", "source": {"type": "file", "file_id": "file_001"},
    }
    assert files_headers(body) == {"anthropic-beta": FILES_BETA}
    plain = request_fields(
        messages=[{"role": "user", "content": "hi"}], system="S", tools=None, max_tokens=64,
        reasoning_effort=None,
    )
    assert files_headers(plain) == {}

    from tests.integration.test_anthropic_adaptation import _preset  # noqa: PLC0415

    peer, _ = asyncio.run(_run(
        _preset(""), _reply({"type": "text", "text": "ok"}, stop="end_turn"),
        messages=messages, system="S",
    ))
    assert peer.body["messages"][0]["content"][1]["source"] == {
        "type": "file", "file_id": "file_001",
    }
    assert peer.headers["anthropic-beta"] == FILES_BETA


def test_a_real_turn_after_a_picture_turn_sees_the_picture_by_file_id(world: World) -> None:
    """Through drive_turn: the picture's own turn is excluded from history, the next one has it."""
    client = FilesClient()
    world.runtime = replace(
        world.runtime, llm_client=client, picture_files=PictureFiles(world.store),
    )
    picture = world.picture("IMG_1.png")

    world.turn(WORDS, ANSWER, (picture,))
    first = world.seen[-1].history
    world.turn("谢谢", "不客气。")
    second = world.seen[-1].history

    assert first == (), "the live turn carries the picture inline, not by history"
    assert client.uploads == [("IMG_1.png", len(PNG), "image/png")]
    assert second[0]["content"][1] == {"type": "image_file", "file_id": "file_001"}
    assert [turn["role"] for turn in second] == ["user", "assistant"]


def test_a_history_ending_on_his_picture_folds_into_the_live_message_with_both_pictures(
    tmp_path: Path,
) -> None:
    """An unanswered picture turn, then a new one: one message, the file id then the bytes."""
    d = _daemon(tmp_path)
    live = d.upload(PNG, "IMG_2.png").json()["id"]
    history = [{"role": "user", "content": [
        {"type": "text", "text": "look\n[attached: image IMG_1.png]"},
        {"type": "image_file", "file_id": "file_001"},
    ]}]

    model, _ = _turn(d, "and this?", [live], history=history)

    (message,) = [m for m in model.requests[0] if m["role"] == "user"]
    kinds = [part["type"] for part in message["content"]]
    assert kinds == ["text", "image_file", "text", "image_url"]
    assert message["content"][1] == {"type": "image_file", "file_id": "file_001"}
    assert message["content"][3]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "and this?" in message["content"][2]["text"]
