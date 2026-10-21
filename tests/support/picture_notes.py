"""A runtime with the attachment store and a note writer, driven by real turns."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jarvis.runtime import JarvisRuntime, drive_turn
from jarvis.runtime.picture_notes import PictureNotes
from jarvis.shared import lang
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.memory_db import MemorySettings
from jarvis.surface.cli import emit_surface_user_intent
from tests.support.model import ScriptedModel
from tests.support.phone import PNG
from tests.support.response_run import make_runtime

if TYPE_CHECKING:
    from pathlib import Path

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
        self,
        image_path: Path,
        *,
        question: str | None,
        instructions: str | None = None,
    ) -> str:
        self.calls.append({"path": image_path, "question": question, "instructions": instructions})
        if self.failures:
            self.failures -= 1
            msg = "provider down"
            raise RuntimeError(msg)
        assert question is not None
        return (
            "A plate of noodles. He said it is udon."
            if CORRECTION in question
            else "A plate of noodles."
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    """A runtime with memory.db, the attachment store and a note writer, driven by real turns."""

    def __init__(self, tmp_path: Path) -> None:
        """Assemble the runtime under ``tmp_path``; ``model`` answers each turn it is driven."""
        self.model = ScriptedModel()
        base = make_runtime(tmp_path, model=self.model)
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
            base,
            memory=self.memory,
            attachments=self.store,
            picture_notes=self.notes,
        )
        self._turns = 0

    def restart(self) -> PictureNotes:
        """A fresh note worker over the same stores: what a restarted daemon starts with."""
        return PictureNotes(
            memory_db=self.memory.db_path,
            store=self.store,
            vision=self.vision,
            event_log=self.event_log,
            clock=self.clock,
            threaded=False,
        )

    def turn(self, said: str, answer: str, pictures: tuple[str, ...] = ()) -> None:
        """Drive one whole turn: his intent in the log, a scripted answer, the records written."""
        self._turns += 1
        self.model.script(answer)
        intent = emit_surface_user_intent(
            self.runtime.conn,
            transcript=said,
            turn_id=f"T{self._turns}",
            attachments=pictures,
        )
        drive_turn(self.runtime, user_intent_event=intent, available_surfaces=frozenset())

    def history_sent(self, request: int = -1) -> list[dict[str, Any]]:
        """The earlier turns a request showed the model: every message before the live one."""
        return self.model.requests[request].messages[:-1]

    def live_text(self, request: int) -> str:
        """The text of the live user message a request carried, state lines and all."""
        content = self.model.requests[request].messages[-1]["content"]
        if isinstance(content, str):
            return content
        return "\n".join(part["text"] for part in content if part["type"] == "text")

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
