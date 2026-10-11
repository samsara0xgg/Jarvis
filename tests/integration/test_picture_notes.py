"""ADR 0223: a picture he sends is remembered as words, and can be looked at again.

Acceptance checks against the real turn driver, a real memory.db, event log and attachment store,
with a scripted vision provider (no network, no model):

- a picture's note is written after its turn, from the picture, his words and her answer, and his
  next turn when it arrives inside the ten-minute window (or the window's end without one);
- the note is an appended record: later history carries it, the history before it stays
  byte-identical, and the record names the picture so the model can look at it again;
- a failed read is tried once more and then given up on, a full queue drops, nothing raises;
- ``look_at_picture`` reads the stored file, and after retention says it is gone and gives the note;
- clearing the pictures clears their notes, and the export carries both;
- an attachment-only turn is answered in the language of his last turn with words, and the
  filler sentence the program writes for it is read by no language check;
- the day line has a picture item at its time, with the stay's place and the note.
"""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import DecideContext, LifecycleLike, RuntimePathsLike, ToolRegistryLike, decide
from jarvis.execution.picture_tool import build_picture_tool
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, drive_turn, inherent_loop
from jarvis.runtime.picture_notes import (
    MAX_ATTEMPTS,
    MAX_PENDING,
    RETRY_AFTER_S,
    WINDOW_S,
    PictureNotes,
)
from jarvis.shared import lang
from jarvis.shared.about import clean_about
from jarvis.state import picture_notes as note_records
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.day_line import DaySources, fold_day
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import MemorySettings, render_context
from jarvis.surface.cli import emit_surface_user_intent
from tests.integration.test_phone_attachments import PNG, _CapturingModel, _StubRuntimePaths
from tests.integration.test_wave4a_response_run import (
    _final_result,
    _make_runtime,
    _plan,
    _script_decide,
)

if TYPE_CHECKING:
    from jarvis.decision.llm import LLMClient

WORDS = "这是什么"
ANSWER = "Looks like a plate of noodles."
CORRECTION = "是乌冬面"
FILLER_EN = lang.TEXT["attach.no_words"]["en"]


class _Vision:
    """The vision preset, scripted: writes a note that includes his correction when it was given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures = 0

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        self.calls.append({"path": image_path, "question": question, "instructions": instructions})
        if self.failures:
            self.failures -= 1
            msg = "provider down"
            raise RuntimeError(msg)
        assert question is not None
        return (
            "A plate of noodles. He said it is udon."
            if CORRECTION in question else "A plate of noodles."
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    """A runtime with memory.db, the attachment store and a note writer, driven by real turns."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assemble the runtime under ``tmp_path``; ``monkeypatch`` scripts its decisions."""
        base = _make_runtime(tmp_path)
        self.store = Attachments(base.runtime_paths.artifacts_root / ATTACHMENTS_DIRNAME)
        self.memory = MemorySettings(
            db_path=base.runtime_paths.root / "memory.db",
            audio_dir=base.runtime_paths.root / "audio",
        )
        self.vision = _Vision()
        self.clock = _Clock()
        self.event_log = base.runtime_paths.event_log
        self.notes = self.restart()
        self.runtime: JarvisRuntime = replace(
            base, memory=self.memory, attachments=self.store, picture_notes=self.notes,
        )
        self.seen: list[DecideContext] = []
        self._monkeypatch = monkeypatch
        self._turns = 0

    def restart(self) -> PictureNotes:
        """A fresh note worker over the same stores: what a restarted daemon starts with."""
        return PictureNotes(
            memory_db=self.memory.db_path, store=self.store, vision=self.vision,
            event_log=self.event_log, clock=self.clock, threaded=False,
        )

    def turn(self, said: str, answer: str, pictures: tuple[str, ...] = ()) -> None:
        """Drive one whole turn: his intent in the log, a scripted answer, the records written."""
        self._turns += 1
        plan = _final_result(_plan(text=answer, response_hash=f"h{self._turns}"))

        def decide_stub(trigger: Any, ctx: DecideContext) -> Any:  # noqa: ANN401
            self.seen.append(ctx)
            return plan(trigger, ctx)

        _script_decide(self._monkeypatch, decide_stub)
        intent = emit_surface_user_intent(
            self.runtime.conn, transcript=said, turn_id=f"T{self._turns}", attachments=pictures,
        )
        drive_turn(self.runtime, user_intent_event=intent, available_surfaces=frozenset())

    def picture(self, name: str = "IMG_1.png") -> str:
        """Store one picture the way an upload does; its id."""
        return self.store.save(PNG, name, images_ok=True).id

    def records(self) -> list[tuple[str, str, str]]:
        """(id, source, text) of every memory record, oldest first."""
        with closing(sqlite3.connect(self.memory.db_path)) as conn:
            return [
                (str(a), str(b), str(c))
                for a, b, c in conn.execute("SELECT id, source, text FROM records ORDER BY rowid")
            ]

    def close(self) -> None:
        """Release the log connection."""
        self.runtime.conn.close()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """A fresh runtime per test."""
    one = World(tmp_path, monkeypatch)
    yield one
    one.close()


# --- the note -------------------------------------------------------------------------------------


def test_the_note_waits_for_his_next_turn_and_folds_his_correction_in(world: World) -> None:
    """Nothing is written on the turn's path; the next turn releases it, with its words."""
    picture = world.picture()

    world.turn(FILLER_EN, ANSWER, (picture,))

    assert world.notes.pending() == 1
    assert world.notes.run_due() == 0, "no next turn and the window is open: still waiting"
    assert [source for _, source, _ in world.records()] == ["allen", "jarvis"]
    assert world.vision.calls == []

    world.turn(CORRECTION, "原来是乌冬面。")
    assert world.notes.run_due() == 1

    (call,) = world.vision.calls
    assert call["path"] == world.store.path(picture)
    assert FILLER_EN not in call["question"], "the program's filler is not his words"
    assert "He wrote no words." in call["question"]
    assert ANSWER in call["question"]
    assert CORRECTION in call["question"]
    assert "Simplified Chinese" in call["instructions"], "his last words were Chinese"
    assert "outranks your own reading" in call["instructions"]
    assert "Never guess who a person is" in call["instructions"]
    *earlier, (note_id, source, text) = world.records()
    assert [s for _, s, _ in earlier] == ["allen", "jarvis", "allen", "jarvis"]
    assert (note_id, source) == (f"picture-note:{picture}", "picture_note")
    assert text == (
        f"[Picture note · image IMG_1.png · id {picture[:8]}]\n"
        "A plate of noodles. He said it is udon."
    )
    assert world.notes.pending() == 0
    assert world.notes.run_due() == 0, "a picture gets one note"


def test_without_a_next_turn_the_window_ends_the_wait(world: World) -> None:
    """Ten minutes on, the note is written from the picture, his words and her answer alone."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))

    world.clock.now += WINDOW_S - 1
    assert world.notes.run_due() == 0
    world.clock.now += 2
    assert world.notes.run_due() == 1

    (call,) = world.vision.calls
    assert f'His words with it: "{WORDS}"' in call["question"]
    assert "next message" not in call["question"]
    assert world.records()[-1][1] == "picture_note"


def test_a_failed_read_is_retried_once_then_given_up_without_raising(world: World) -> None:
    """One retry after a minute; a second failure drops the note and the loop carries on."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))
    world.clock.now += WINDOW_S
    world.vision.failures = MAX_ATTEMPTS

    assert world.notes.run_due() == 0
    assert world.notes.pending() == 1
    assert world.notes.run_due() == 0, "the retry waits"
    world.clock.now += RETRY_AFTER_S
    assert world.notes.run_due() == 0
    assert world.notes.pending() == 0
    assert len(world.vision.calls) == MAX_ATTEMPTS
    assert [s for _, s, _ in world.records()] == ["allen", "jarvis"]

    again = world.picture("IMG_2.png")
    world.turn(WORDS, ANSWER, (again,))
    world.clock.now += WINDOW_S
    world.vision.failures = 1
    world.notes.run_due()
    world.clock.now += RETRY_AFTER_S
    assert world.notes.run_due() == 1, "the retry can succeed"


def test_the_queue_is_bounded_and_a_gone_picture_costs_no_request(world: World) -> None:
    """A full queue drops the new job; a file already swept is skipped without a model call."""
    picture = world.picture()
    for n in range(MAX_PENDING):
        assert world.notes.submit("r", [f"{n:032x}"], "", "")
    assert world.notes.submit("r", [picture], "", "") is False
    assert world.notes.submit("r", [f"{0:032x}"], "", ""), "a picture already waiting adds nothing"
    for stored in world.store.directory.iterdir():
        stored.unlink()
    world.clock.now += WINDOW_S

    assert world.notes.run_due() == 0
    assert world.vision.calls == []
    assert world.notes.pending() == 0
    assert world.notes.recover() == 0, "a picture that is gone is owed nothing"


# --- history, recall and the second look ---------------------------------------------------------


def test_later_history_carries_the_note_and_the_front_is_byte_stable(world: World) -> None:
    """The note is appended; what history showed before it is a prefix of what it shows after."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))
    world.turn(CORRECTION, "原来是乌冬面。")
    before = render_context(world.memory.db_path, exclude_id="").history

    world.notes.run_due()
    after = render_context(world.memory.db_path, exclude_id="").history

    header = f"[Picture note · image IMG_1.png · id {picture[:8]}]"
    assert after[: len(before)] == before
    assert after[-1]["role"] == "user"
    assert after[-1]["content"].startswith(header)
    assert "He said it is udon." in after[-1]["content"]

    world.turn("谢谢", "不客气。")
    later = render_context(world.memory.db_path, exclude_id="").history
    assert later[: len(before)] == before
    assert all(header not in turn["content"] for turn in later if turn["role"] == "assistant"), (
        "a note is context from his side, never her own words"
    )


def _tool_context(root: Path) -> Any:  # noqa: ANN401
    return SimpleNamespace(runtime_paths=SimpleNamespace(artifacts_root=root))


def test_look_at_picture_reads_the_file_and_degrades_to_the_note_after_retention(
    world: World,
) -> None:
    """While the file is kept the vision preset answers; once swept the note is all there is."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))
    world.notes.submit("x", [picture], "", "")  # a second job is harmless: the note exists
    world.clock.now += WINDOW_S
    world.notes.run_due()
    registry = build_default_registry(
        vision_client=world.vision, memory_db_path=world.memory.db_path,
    )
    assert "look_at_picture" in {t.name for t in registry.get_definitions()}
    assert "look_at_picture" not in {t.name for t in build_default_registry().get_definitions()}
    (tool,) = build_picture_tool(world.vision, world.memory.db_path)
    assert tool.read_only
    ctx = _tool_context(world.store.directory.parent)
    world.vision.calls.clear()

    seen = tool.handler({"picture": picture[:8], "question": "how many noodles?"}, ctx)
    newest = tool.handler({"picture": "latest", "question": "what colour is the plate?"}, ctx)

    assert seen == {"picture": picture[:8], "answer": "A plate of noodles."}
    assert newest["picture"] == picture[:8]
    assert [c["question"] for c in world.vision.calls] == [
        "how many noodles?", "what colour is the plate?",
    ]
    assert world.vision.calls[0]["path"] == world.store.path(picture)

    for stored in world.store.directory.iterdir():
        stored.unlink()
    gone = tool.handler({"picture": picture[:8], "question": "how many noodles?"}, ctx)

    assert gone["status"] == "gone"
    assert gone["note"] == "A plate of noodles."
    assert len(world.vision.calls) == 2, "no model call for a picture that is gone"
    assert tool.handler({"picture": "latest", "question": "?"}, ctx)["status"] == "gone"
    assert tool.handler({"picture": "zz", "question": "?"}, ctx)["status"] == "gone"
    assert build_picture_tool(None, None) == ()


# --- his data ------------------------------------------------------------------------------------


def test_clearing_the_pictures_clears_their_notes_and_the_export_carries_both(
    world: World,
) -> None:
    """Export has the files and the notes; clear takes the notes with the pictures, not the rest."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))
    world.clock.now += WINDOW_S
    world.notes.run_due()

    archive = inherent_loop._export_data(world.runtime)  # noqa: SLF001
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            extracted = Path(bundle.extract("memory.db", archive.parent / "x"))
    finally:
        archive.unlink()
    assert any(n.startswith("artifacts/attachments/") and picture in n for n in names)
    with closing(sqlite3.connect(extracted)) as exported:
        assert exported.execute(
            "SELECT count(*) FROM records WHERE source = 'picture_note'",
        ).fetchone() == (1,)

    assert inherent_loop._clear_media(world.runtime) >= 1  # noqa: SLF001

    assert world.store.get(picture) is None
    assert [s for _, s, _ in world.records()] == ["allen", "jarvis"]
    assert note_records.bodies(world.memory.db_path, [picture]) == {}


# --- the reply language of an attachment-only turn -----------------------------------------------


def test_the_filler_is_no_words_to_any_language_check() -> None:
    """Both languages of both filler sentences are filler; his words never are."""
    fillers = [
        lang.TEXT[key][one] for key in ("attach.no_words", "share.default_note")
        for one in lang.LANGUAGES
    ]

    assert all(lang.is_filler(f) and lang.is_filler(f" {f}\n") for f in fillers)
    assert not any(lang.is_filler(x) for x in (WORDS, "", "(Sent with no words.) and more", None))


def test_an_attachment_only_turn_takes_the_language_of_his_last_words(world: World) -> None:
    """The driver hands the decision his last words, past marker lines and fillers."""
    first = world.picture()
    world.turn(WORDS, ANSWER, (first,))
    world.turn(FILLER_EN, ANSWER, (world.picture("IMG_2.png"),))
    world.turn(FILLER_EN, ANSWER, (world.picture("IMG_3.png"),))

    assert [ctx.last_words for ctx in world.seen] == ["", WORDS, WORDS]
    first_record = world.records()[0][0]
    assert note_records.next_words(world.memory.db_path, first_record) == "", "a turn, no words"
    assert note_records.next_words(world.memory.db_path, world.records()[-1][0]) is None
    assert note_records.last_words(world.memory.db_path, "no-such-record") == ""


def _decide_request(tmp_path: Path, transcript: str, last_words: str) -> str:
    """The user message ``decide`` sends for an attachment-only turn, as one string."""
    log = tmp_path / "events.db"
    store = Attachments(tmp_path / ATTACHMENTS_DIRNAME)
    picture = store.save(PNG, "IMG_1.png", images_ok=True).id
    model = _CapturingModel()
    with closing(open_event_log(log)) as conn:
        trigger = emit_surface_user_intent(
            conn, transcript=transcript, turn_id="T-lang", attachments=[picture],
        )
        decide(trigger, DecideContext(
            conn=conn,
            runtime_paths=cast("RuntimePathsLike", _StubRuntimePaths(log, tmp_path)),
            tool_registry=cast("ToolRegistryLike", build_default_registry()),
            lifecycle=cast("LifecycleLike", ActionLifecycle()),
            llm_client=cast("LLMClient", model), system_prompt="s",
            read_attachments=store.load_many, last_words=last_words,
        ))
    (live,) = [m for m in model.requests[0] if m["role"] == "user"]
    return str(live["content"][0]["text"])


def test_the_reply_follows_his_last_words_not_the_filler(tmp_path: Path) -> None:
    """On an English system the filler is English; it no longer pins the reply to English."""
    text = _decide_request(tmp_path / "a", FILLER_EN, WORDS)
    assert text.endswith("[Reply language for this turn: Chinese]")

    english = _decide_request(tmp_path / "b", FILLER_EN, "what is this")
    assert english.endswith("[Reply language for this turn: English]")

    nothing_yet = _decide_request(tmp_path / "c", FILLER_EN, "")
    assert "Reply language" not in nothing_yet, "no words ever said: nothing is pinned"

    own_words = _decide_request(tmp_path / "d", WORDS, "what is this")
    assert own_words.endswith("[Reply language for this turn: Chinese]"), "his own words win"


# --- the day line --------------------------------------------------------------------------------


def test_the_day_line_has_a_picture_item_at_its_time_place_and_note(world: World) -> None:
    """Folded from the log: the turn's time, the stay that covers it, the note once written."""
    picture = world.picture()
    pending_only = world.picture("IMG_2.png")
    text_file = world.store.save(b"just notes\n", "n.txt", images_ok=True).id
    hour = 3_600_000
    t0 = 1_800_000_000_000
    log = world.runtime.conn
    emit_event(
        log, type="phone.visit_observed", ingestion_node="phone", ts_epoch_ms=t0,
        payload={
            "lat": 10.0, "lng": 20.0, "accuracy_m": 30.0, "arrived_at_ms": t0 - hour,
            "departed_at_ms": t0 + hour, "place": "Example Place",
        },
    )
    for at, ids in ((t0 + 1000, [picture]), (t0 + 2 * hour, [pending_only, text_file])):
        emit_event(
            log, type="surface.user_intent", ts_epoch_ms=at, correlation={"turn_id": f"T{at}"},
            payload={"transcript": "x", "turn_id": f"T{at}", "attachments": ids},
        )
    note_records.append(world.memory.db_path, world.store.get(picture), "A plate of noodles.")  # type: ignore[arg-type]
    sources = DaySources(
        UTC, None, world.memory.db_path, None, world.store.directory,
    )

    line = fold_day(log, t0 - 5 * hour, t0 + 5 * hour, t0 + 3 * hour, sources)

    pictures = [item for item in line.items if item["kind"] == "picture"]
    assert pictures == [
        {
            "id": f"picture-{picture}", "lane": "you", "kind": "picture", "start_ms": t0 + 1000,
            "end_ms": t0 + 1000, "state": "done", "attachment": picture,
            "note": "A plate of noodles.", "lat": 10.0, "lng": 20.0, "accuracy_m": 30.0,
            "place": "Example Place",
        },
        {
            "id": f"picture-{pending_only}", "lane": "you", "kind": "picture",
            "start_ms": t0 + 2 * hour, "end_ms": t0 + 2 * hour, "state": "done",
            "attachment": pending_only,
        },
    ]
    assert "pictures" not in line.missing
    assert json.dumps(pictures)  # the payload is plain JSON


def test_the_day_line_without_the_pictures_source_stores_is_what_it_was(world: World) -> None:
    """No memory store and no attachment store: no picture items, and nothing reported missing."""
    emit_event(
        world.runtime.conn, type="surface.user_intent", correlation={"turn_id": "T"},
        payload={"transcript": "x", "turn_id": "T", "attachments": ["a" * 32]},
    )

    line = fold_day(world.runtime.conn, 0, 2**50, 2**50, DaySources(UTC))

    assert [i for i in line.items if i["kind"] == "picture"] == []
    assert "pictures" not in line.missing


# --- a restart loses nothing ---------------------------------------------------------------


def test_a_restart_between_the_turn_and_the_note_loses_nothing_and_writes_once(
    world: World,
) -> None:
    """What is owed is derived from the log and memory.db, so a fresh queue finds it."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))
    world.turn(CORRECTION, "原来是乌冬面。")
    assert world.notes.pending() == 1

    after = world.restart()
    assert after.pending() == 0
    assert after.recover() == 1
    assert after.run_due() == 1

    (call,) = world.vision.calls
    assert f'"{WORDS}"' in call["question"]
    assert ANSWER in call["question"], "her answer is read back from the records"
    assert CORRECTION in call["question"]
    assert after.recover() == 0, "owed once, then never"
    assert world.restart().recover() == 0
    assert world.notes.run_due() == 0, "the old process wakes up: it finds the note and stops"
    assert len(world.vision.calls) == 1
    assert [s for _, s, _ in world.records()].count("picture_note") == 1


def test_a_recovered_job_keeps_the_rest_of_its_window(world: World) -> None:
    """With no next turn the restarted queue waits out the window, not less and not more."""
    picture = world.picture()
    world.turn(WORDS, ANSWER, (picture,))

    after = world.restart()
    assert after.recover(min_age_s=WINDOW_S) == 0, "a turn still being answered is left alone"
    assert after.recover() == 1
    world.clock.now += WINDOW_S - 5
    assert after.run_due() == 0
    world.clock.now += 6
    assert after.run_due() == 1


def test_only_the_last_day_is_owed(world: World) -> None:
    """A picture from a turn older than a day is not recovered (the tool still reads it)."""
    world.turn(WORDS, ANSWER, (world.picture(),))
    late = PictureNotes(
        memory_db=world.memory.db_path, store=world.store, vision=world.vision,
        event_log=world.event_log, clock=world.clock, threaded=False,
        wall=lambda: time.time() + 25 * 3600,
    )

    assert late.recover() == 0
    assert world.restart().recover() == 1


# --- a saved picture ---------------------------------------------------------------------------


def test_a_saved_picture_gets_a_note_with_no_turn_and_the_day_line_shows_it(
    world: World,
) -> None:
    """Queued at once when saved, found again after a restart; the note carries his words."""
    live, lost = world.picture(), world.picture("IMG_2.png")
    inherent_loop._keep_share(  # noqa: SLF001
        world.event_log, {"kind": "image", "attachments": [live], "note": "for later"},
        picture_notes=world.notes,
    )
    inherent_loop._keep_share(  # noqa: SLF001
        world.event_log, {"kind": "image", "attachments": [lost]},
    )

    assert world.notes.run_due() == 1
    (call,) = world.vision.calls
    assert "saved this picture from another app" in call["question"]
    assert 'His note with it: "for later"' in call["question"]
    assert "Her answer" not in call["question"]
    assert call["path"] == world.store.path(live)

    after = world.restart()
    assert after.recover() == 1, "the one that was never queued is found from its event"
    assert after.run_due() == 1
    assert "He wrote no note." in world.vision.calls[1]["question"]
    assert world.restart().recover() == 0
    assert [s for _, s, _ in world.records()] == ["picture_note", "picture_note"]

    log = world.runtime.conn
    now = int(time.time() * 1000)
    items = fold_day(
        log, now - 3_600_000, now + 3_600_000, now,
        DaySources(UTC, None, world.memory.db_path, None, world.store.directory),
    ).items
    shown = {i["attachment"]: i for i in items if i["kind"] == "picture"}
    assert set(shown) == {live, lost}
    assert shown[live]["note"] == "A plate of noodles."


def test_the_phone_can_say_this_one_about_a_picture(world: World) -> None:
    """``picture`` is an about kind, its day-line id is a valid about id, and the tool takes it."""
    picture = world.picture()

    assert clean_about({"kind": "picture", "id": f"picture-{picture}"}) == {
        "kind": "picture", "id": f"picture-{picture}",
    }
    with pytest.raises(ValueError, match=r"about\.kind"):
        clean_about({"kind": "photo", "id": "x"})
    ref = world.store.find(f"picture-{picture}")
    assert ref is not None
    assert ref.id == picture
