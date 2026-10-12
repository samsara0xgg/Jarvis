"""ADR 0255: a picture he sends stays in the conversation as a picture.

Acceptance checks against the real turn driver, a real memory.db, event log and attachment store.
No network: the Files API is a recording fake.

- his record is linked to its files when it is written, and records from before the link are
  linked from the event log for the files still kept;
- on a Claude preset the history message of his turn carries its pictures as ``image_file`` parts
  after his words, the 20 newest only; a picture's note is left out only while the picture rides;
- a gone file, a failed upload and a preset that is not Anthropic's keep the marker and the note,
  and the parts are byte-identical from one render to the next;
- the request the Anthropic adapter builds names the file and sends the Files API beta header;
- the rows of ``GET /inherent/conversation`` list his files and carry his words alone, and
  ``GET /inherent/attachments/{id}`` returns the stored bytes to a paired device, 404 once gone;
- the uploaded copy goes with its file: at the retention sweep, on clearing the recordings and on
  erasing everything, and a delete that fails is tried again at the next sweep.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_anthropic import FILES_BETA, files_headers, request_fields
from jarvis.runtime import inherent_loop, picture_files
from jarvis.runtime.picture_files import RETRY_AFTER_S, PictureFiles
from jarvis.state import picture_notes as note_records
from jarvis.state.device_tokens import unpair_device
from jarvis.state.memory_db import (
    HISTORY_PICTURES,
    append_record,
    conversation_rows,
    link_attachments,
    render_context,
)
from tests.integration.test_anthropic_adaptation import _reply, _run
from tests.integration.test_phone_attachments import NOTES, PNG, _daemon, _turn
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
        self.deleted: list[str] = []
        self.failing = False
        self.failing_delete = False

    def upload_file(self, name: str, data: bytes, mime: str) -> str:
        """Record the upload; the id is the count so far, or fail when told to."""
        if self.failing:
            msg = "files api down"
            raise RuntimeError(msg)
        self.uploads.append((name, len(data), mime))
        return f"file_{len(self.uploads):03d}"

    def delete_file(self, file_id: str) -> None:
        """Record the delete, or fail when told to."""
        if self.failing_delete:
            msg = "files api down"
            raise RuntimeError(msg)
        self.deleted.append(file_id)


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


# --- the copy goes with the file -----------------------------------------------------------------


def uploaded(world: World, *names: str) -> tuple[FilesClient, list[str]]:
    """Store, link and upload the pictures; the client and their attachment ids."""
    client = FilesClient()
    world.runtime = replace(
        world.runtime, llm_client=client, picture_files=PictureFiles(world.store),
    )
    ids = [send(world, name) for name in names]
    render(world, world.runtime.picture_files, client)  # type: ignore[arg-type]
    return client, ids


def age(world: World, attachment_id: str, days: int) -> None:
    """Make a stored picture ``days`` old."""
    then = time.time() - days * 86400
    os.utime(world.store.path(attachment_id), (then, then))  # type: ignore[arg-type]


def sweep(world: World) -> None:
    """One pass of the hourly data sweep, as the daemon runs it."""
    inherent_loop._sweep_data(  # noqa: SLF001
        inherent_loop._media_dirs(world.runtime), world.runtime.runtime_paths.root / "logs",  # noqa: SLF001
    )
    inherent_loop._release_pictures(world.runtime)  # noqa: SLF001


def test_the_retention_sweep_deletes_the_uploaded_copy_of_the_files_it_removes(
    world: World,
) -> None:
    """Only the aged picture's copy goes, and its file id record with it."""
    client, (old, fresh) = uploaded(world, "IMG_1.png", "IMG_2.png")
    old_copy, fresh_copy = world.store.file_id(old), world.store.file_id(fresh)
    age(world, old, 31)

    sweep(world)

    assert client.deleted == [old_copy]
    assert world.store.file_id(old) is None
    assert world.store.file_id(fresh) == fresh_copy
    sweep(world)
    assert client.deleted == [old_copy], "nothing is deleted twice"


def test_a_delete_that_fails_is_logged_kept_and_tried_again_at_the_next_sweep(
    world: World, caplog: pytest.LogCaptureFixture,
) -> None:
    """Best effort: the sweep goes on, and the copy is not forgotten."""
    client, (old,) = uploaded(world, "IMG_1.png")
    age(world, old, 31)
    client.failing_delete = True

    sweep(world)

    assert "could not delete the uploaded copy" in caplog.text
    assert world.store.file_id(old) == "file_001"
    client.failing_delete = False
    sweep(world)
    assert client.deleted == ["file_001"]
    assert world.store.file_id(old) is None


def test_clearing_the_recordings_deletes_every_copy_with_the_files(world: World) -> None:
    """The pictures go at once, and so do their uploaded copies."""
    client, ids = uploaded(world, "IMG_1.png", "IMG_2.png")

    inherent_loop._clear_media(world.runtime)  # noqa: SLF001

    assert sorted(client.deleted) == ["file_001", "file_002"]
    assert all(world.store.file_id(one) is None for one in ids)


def test_erasing_everything_deletes_the_copies_before_the_files_go_at_the_next_boot(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The files are still there when the erase is asked for, and the copies are not."""
    client, ids = uploaded(world, "IMG_1.png")
    monkeypatch.setattr(inherent_loop, "_restart_soon", lambda: None)

    inherent_loop._erase_data(world.runtime)  # noqa: SLF001

    assert client.deleted == ["file_001"]
    assert world.store.path(ids[0]) is not None
    assert world.store.file_id(ids[0]) is None


def test_with_no_anthropic_preset_the_copies_wait_and_nothing_raises(world: World) -> None:
    """The deletes need a key of that provider; without a preset the ids are kept."""
    client, (old,) = uploaded(world, "IMG_1.png")
    world.runtime = replace(world.runtime, llm_client=FilesClient("openai"))
    age(world, old, 31)

    sweep(world)

    assert client.deleted == []
    assert world.store.file_id(old) == "file_001"


# --- the Files API calls themselves ------------------------------------------------------------


class _FilesPeer(http.server.BaseHTTPRequestHandler):
    """A local stand-in for api.anthropic.com's /v1/files: records what the SDK sends."""

    seen: list[tuple[str, str, str, bytes]] = []  # noqa: RUF012 — shared by the test below

    def _answer(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size)
        self.seen.append((
            self.command, self.path, self.headers.get("anthropic-beta") or "", body,
        ))
        status = 404 if self.path.endswith("file_gone") else 200
        payload = (
            {"type": "error", "error": {"type": "not_found_error", "message": "gone"}}
            if status == 404 else {"id": "file_011abc", "type": "file", "deleted": True}
        )
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_DELETE = _answer  # noqa: N815

    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


def test_upload_and_delete_use_the_sdks_files_api_with_the_beta_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real SDK against a local peer: a multipart upload, a delete, a missing file is fine."""
    monkeypatch.setenv("HAIKU_FIXTURE_KEY", "synthetic")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FilesPeer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FilesPeer.seen.clear()
    try:
        from tests.integration.test_anthropic_adaptation import _preset  # noqa: PLC0415

        client = _preset("")
        client._base_url = f"http://127.0.0.1:{server.server_port}"  # noqa: SLF001
        assert client.upload_file("IMG_1.png", PNG, "image/png") == "file_011abc"
        client.delete_file("file_011abc")
        client.delete_file("file_gone")  # already deleted: not an error
    finally:
        server.shutdown()
    (up, down, gone) = _FilesPeer.seen
    assert up[:3] == ("POST", "/v1/files?beta=true", FILES_BETA)
    assert b'filename="IMG_1.png"' in up[3]
    assert b"Content-Type: image/png" in up[3]
    assert PNG in up[3]
    assert down[:3] == ("DELETE", "/v1/files/file_011abc?beta=true", FILES_BETA)
    assert gone[1] == "/v1/files/file_gone?beta=true"


class RealClient(LLMClient):
    """A real client, for the calls the fake above replaces."""

    def __init__(self, provider: str) -> None:
        """One preset of ``provider``."""
        super().__init__({
            "provider": provider, "default_preset": "p",
            "presets": {"p": {"provider": provider, "model": "m", "max_tokens": 64}},
        })


def test_the_files_api_is_refused_on_a_client_that_is_not_anthropics() -> None:
    """An OpenAI preset has no such API; asking raises, and the caller falls back to words."""
    with pytest.raises(ValueError, match="Files API"):
        RealClient("openai").upload_file("x.png", PNG, "image/png")



# --- the device API ------------------------------------------------------------------------------


def test_his_rows_list_his_files_and_carry_his_words_alone(world: World) -> None:
    """No marker line, no filler; a swept file is listed as gone; her rows have no files."""
    kept = send(world, "IMG_1.png", "什么菜?")
    swept = send(world, "IMG_2.png", FILLER_EN)
    world.store.path(swept).unlink()  # type: ignore[union-attr]
    append_record(world.memory.db_path, record_id="old", source="allen",
                  text="before the link\n[attached: image old.jpg]")
    append_record(world.memory.db_path, record_id="plain", source="allen", text="谢谢")

    rows, _ = conversation_rows(world.memory.db_path, attachments=world.store)

    by_id = {str(row["id"]): row for row in rows}
    pictured = [row for row in rows if row["source"] == "allen" and row["attachments"]]
    assert [(r["text"], r["attachments"]) for r in pictured] == [
        ("什么菜?", [{"id": kept, "kind": "image", "name": "IMG_1.png", "gone": False}]),
        ("", [{"id": swept, "kind": "image", "name": "IMG_2.png", "gone": True}]),
    ]
    assert by_id["old"]["text"] == "before the link\n[attached: image old.jpg]"
    assert by_id["old"]["attachments"] == []
    assert by_id["plain"]["attachments"] == []
    assert all("attachments" not in row for row in rows if row["source"] == "jarvis")
    assert "picture_note" not in {row["source"] for row in rows}


def test_what_the_model_sees_is_unchanged_by_the_rows_stripping(world: World) -> None:
    """History keeps the marker line and the filler as the record holds them."""
    send(world, "IMG_1.png", FILLER_EN)

    turn = render_context(world.memory.db_path, exclude_id="live").history[0]

    assert FILLER_EN in str(turn["content"])
    assert "[attached: image IMG_1.png]" in str(turn["content"])


def test_a_paired_device_opens_a_stored_file_and_a_gone_one_is_a_404(tmp_path: Path) -> None:
    """Bytes and type as stored; the same credentials as every other route."""
    d = _daemon(tmp_path)
    picture = d.upload(PNG, "IMG_1.png").json()["id"]
    notes = d.upload(NOTES, "notes.txt").json()["id"]

    image = d.remote.get(f"/inherent/attachments/{picture}", headers=d.auth())
    text = d.remote.get(f"/inherent/attachments/{notes}", headers=d.auth())
    local = d.loopback.get(
        f"/inherent/attachments/{picture}", headers={"Authorization": f"Bearer {d.key}"},
    )

    assert (image.status_code, image.content, image.headers["content-type"]) == (
        200, PNG, "image/png",
    )
    assert image.headers["x-content-type-options"] == "nosniff"
    assert (text.status_code, text.content) == (200, NOTES)
    assert text.headers["content-type"].startswith("text/plain")
    assert local.status_code == 200
    d.store.path(picture).unlink()  # type: ignore[union-attr]
    for wanted in (picture, "f" * 32, "not-an-id", "../events.db"):
        assert d.remote.get(
            f"/inherent/attachments/{wanted}", headers=d.auth(),
        ).status_code == 404, wanted


def test_the_file_route_takes_the_credential_that_fits_the_peer_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Loopback: the local key. The tailnet: a paired device's token. Anything else is 401."""
    d = _daemon(tmp_path)
    picture = d.upload(PNG).json()["id"]
    path = f"/inherent/attachments/{picture}"
    wrong = {"Authorization": "Bearer nope"}
    local = {"Authorization": f"Bearer {d.key}"}

    assert [
        d.remote.get(path).status_code, d.remote.get(path, headers=wrong).status_code,
        d.remote.get(path, headers=local).status_code, d.loopback.get(path).status_code,
        d.loopback.get(path, headers=wrong).status_code,
        d.loopback.get(path, headers=d.auth()).status_code,
    ] == [401] * 6
    unpair_device(d.root, "iphone")
    assert d.remote.get(path, headers=d.auth()).status_code == 401


def test_without_an_attachment_store_the_file_route_does_not_exist(tmp_path: Path) -> None:
    """Off like the upload route: 404 for a peer that is allowed in."""
    d = _daemon(tmp_path, wired=False)

    assert d.remote.get(f"/inherent/attachments/{'a' * 32}", headers=d.auth()).status_code == 404
