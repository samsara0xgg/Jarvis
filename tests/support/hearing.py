"""Qwen's streaming recognizer, replaced, and a terminal's voice link wired to a brain's hearing."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.surface import voice_asr
from jarvis.surface.terminal_hearing import BrainHearing, QwenEars
from jarvis.surface.terminal_speaker import Journal, VoiceLink

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class QwenTask:
    """One recognition task as Qwen received it."""

    parameters: dict[str, Any]
    audio: bytearray = field(default_factory=bytearray)
    finished: bool = False


class FakeQwen:
    """Qwen's duplex protocol on fake connections: a task's words are its audio, read as text.

    ``fails`` makes every task end in ``task-failed``.
    """

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.tasks: list[QwenTask] = []
        self.connections = 0

    async def connect(self) -> _Connection:
        self.connections += 1
        return _Connection(self)


class _Connection:
    def __init__(self, qwen: FakeQwen) -> None:
        self._qwen = qwen
        self._out: asyncio.Queue[str] = asyncio.Queue()
        self._task: QwenTask | None = None

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            assert self._task is not None
            self._task.audio += message
            return
        header = json.loads(message)["header"]
        if header["action"] == "run-task":
            self._task = QwenTask(json.loads(message)["payload"]["parameters"])
            self._qwen.tasks.append(self._task)
            self._event("task-started")
        elif header["action"] == "finish-task":
            assert self._task is not None
            self._task.finished = True
            if self._qwen.fails:
                self._event("task-failed", error_code="InvalidParameter", error_message="refused")
                return
            words = self._task.audio.decode("ascii")
            self._event("result-generated", sentence={"sentence_id": 1, "text": words})
            self._event("task-finished")

    def _event(self, event: str, sentence: dict[str, Any] | None = None, **header: str) -> None:
        output = {} if sentence is None else {"sentence": sentence}
        self._out.put_nowait(json.dumps({
            "header": {"event": event, **header}, "payload": {"output": output},
        }))

    async def recv(self) -> str:
        return await self._out.get()

    async def close(self) -> None:
        return None


class SenseVoiceStandIn:
    """SenseVoice, replaced: hears every clip as the same words."""

    def recognize(self, audio: bytes) -> voice_asr.TranscriptionResult:
        del audio
        return voice_asr.TranscriptionResult("森", 0.9, "zh", None)

    def partial_text(self, audio: bytes) -> str:
        del audio
        return "森"

    def prewarm(self) -> None:
        return None


class HearingBench:
    """A terminal's VoiceLink on a loop of its own, its frames handed to a brain's hearing."""

    def __init__(self, tmp_path: Path, qwen: FakeQwen, *, hears: bool = True) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.brain = BrainHearing(QwenEars("key", connect=qwen.connect))
        self.link = VoiceLink(Journal(tmp_path / "journal.db"))
        ready = {"type": "ready", "voice": True, "listen": True, "hear": hears}
        self.link.up(self._to_brain, self.loop, ready)

    async def _to_brain(self, text: str) -> None:
        self.brain.on_frame(self, self._to_terminal, json.loads(text))

    async def _to_terminal(self, text: str) -> None:
        self.link.on_frame(json.loads(text))

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
