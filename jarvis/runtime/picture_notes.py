"""ADR 0223: a note in words for each picture he sends, written after its turn.

When a turn that came with pictures ends, :meth:`PictureNotes.submit` queues one job. The job waits
until his next turn arrives or ten minutes have passed, whichever is first, so a correction he
makes lands in the note. Then, for each picture, one request to the vision preset (the same seam
``screen_look`` uses) gets the picture, his words and her answer of that turn, and his next words;
the note it writes is appended to memory.db (:mod:`jarvis.state.picture_notes`).

Nothing here is on a turn's path. The queue holds at most :data:`MAX_PENDING` jobs and a full
queue drops the new one with a log line (its picture goes without a note). A failed request is
tried once more after :data:`RETRY_AFTER_S`, then given up on; nothing is raised into the caller.
The queue lives in memory: a restart inside the window loses the pending notes. The look-again
tool still reads the picture while it is kept.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from jarvis.shared import lang
from jarvis.state import picture_notes as records

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from jarvis.execution.tools import VisionClient
    from jarvis.state.attachments import Attachments

LOGGER = logging.getLogger(__name__)

WINDOW_S: Final = 600.0
"""His next turn goes into the note if it comes within this long after the picture's turn ends."""
MAX_PENDING: Final = 16
RETRY_AFTER_S: Final = 60.0
MAX_ATTEMPTS: Final = 2
"""One try and one retry."""
_POLL_S: Final = 2.0
_CONTEXT_CHARS: Final = 1500
"""Each of his words and her answer is cut to this many characters in the request."""

_INSTRUCTIONS: Final = (
    "You write a short note about one picture a user sent to his assistant, so the assistant can "
    "remember it later without seeing it. Write in {language}. Say what the picture shows, quote "
    "any text visible in it, and say what he said about it. What he said about the picture "
    "outranks your own reading of it: when he named or corrected something, state his version "
    "as his (he said it is X) and do not contradict it. Never guess who a person is and never "
    "name anyone from their face. Plain text, at most {limit} characters, no preface."
)


@dataclass(frozen=True)
class _Job:
    record_id: str  # memory.db id of his words of the turn
    pictures: tuple[str, ...]
    total: int  # how many pictures the message had, whatever is left to retry
    words: str
    answer: str
    window_ends: float
    not_before: float = 0.0
    attempts: int = 0
    settled: bool = False  # the window is over; ``following`` is what it held
    following: str | None = None


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _CONTEXT_CHARS else text[: _CONTEXT_CHARS - 1] + "…"


class PictureNotes:
    """The queue and the worker that write picture notes; see the module docstring."""

    def __init__(  # noqa: PLR0913 — the stores and the model seam, plus a clock for the checks.
        self,
        *,
        memory_db: Path,
        store: Attachments,
        vision: VisionClient,
        pinned_language: str = "follow",
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        """``threaded=False`` leaves the worker unstarted; the caller runs :meth:`run_due`."""
        self._memory_db = memory_db
        self._store = store
        self._vision = vision
        self._pinned = pinned_language
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.Lock()
        self._pending: deque[_Job] = deque()
        self._worker: threading.Thread | None = None

    def submit(
        self, record_id: str, pictures: Sequence[str], words: str, answer: str,
    ) -> bool:
        """Queue the note jobs of a turn that has ended; ``False`` when the queue is full."""
        job = _Job(
            record_id, tuple(pictures), len(pictures), words, answer,
            window_ends=self._clock() + WINDOW_S,
        )
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                LOGGER.warning("picture notes: queue full; %d pictures get no note", len(pictures))
                return False
            self._pending.append(job)
            if self._threaded and self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="jarvis-picture-notes", daemon=True,
                )
                self._worker.start()
        return True

    def pending(self) -> int:
        """How many jobs are waiting."""
        with self._lock:
            return len(self._pending)

    def run_due(self) -> int:
        """Run every job whose wait is over; the number of notes written."""
        written = 0
        with self._lock:
            jobs = list(self._pending)
        for job in jobs:
            following = (
                job.following if job.settled
                else records.next_words(self._memory_db, job.record_id)
            )
            now = self._clock()
            if now < job.not_before or (following is None and now < job.window_ends):
                continue
            with self._lock:
                self._pending.remove(job)
            written += self._write(job, following)
        return written

    def _work(self) -> None:
        while True:
            time.sleep(_POLL_S)
            try:
                self.run_due()
            except Exception:
                LOGGER.exception("picture notes: a pass failed; the next one runs as usual")

    def _write(self, job: _Job, following: str | None) -> int:
        """One request per picture; the pictures that failed come back as a retry."""
        written, failed = 0, []
        said = job.words or records.last_words(self._memory_db, job.record_id)
        language = lang.language_name(lang.reply_language(said, self._pinned))
        for number, attachment_id in enumerate(job.pictures, start=1):
            try:
                written += self._note(job, attachment_id, number, following, language)
            except Exception as exc:  # noqa: BLE001 — a vision or disk failure costs one note.
                LOGGER.warning(
                    "picture notes: %s failed for picture %s (attempt %d of %d)",
                    type(exc).__name__, attachment_id[:8], job.attempts + 1, MAX_ATTEMPTS,
                )
                failed.append(attachment_id)
        if failed and job.attempts + 1 < MAX_ATTEMPTS:
            retry = replace(
                job, pictures=tuple(failed), attempts=job.attempts + 1,
                not_before=self._clock() + RETRY_AFTER_S, settled=True, following=following,
            )
            with self._lock:
                self._pending.append(retry)
        return written

    def _note(
        self, job: _Job, attachment_id: str, number: int, following: str | None, language: str,
    ) -> int:
        """Write one picture's note; 0 when it is gone, not a picture, or already has one."""
        ref = self._store.get(attachment_id)
        path = self._store.path(attachment_id)
        if ref is None or path is None or ref.kind != "image":
            return 0
        if attachment_id in records.bodies(self._memory_db, [attachment_id]):
            return 0
        lines = [f"He sent {job.total} picture(s) in one message; this is number {number}."]
        lines.append(
            f'His words with it: "{_cut(job.words)}"' if job.words else "He wrote no words.",
        )
        if job.answer:
            lines.append(f'Her answer: "{_cut(job.answer)}"')
        if following:
            lines.append(
                f'His next message, which may or may not be about the picture: "{_cut(following)}"',
            )
        body = self._vision.describe_image(
            path,
            question="\n".join(lines),
            instructions=_INSTRUCTIONS.format(language=language, limit=records.NOTE_MAX_CHARS),
        ).strip()
        if not body:
            msg = "the vision model wrote no note"
            raise ValueError(msg)
        records.append(self._memory_db, ref, body[: records.NOTE_MAX_CHARS])
        return 1
