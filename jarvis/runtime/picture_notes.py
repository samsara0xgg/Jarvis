"""ADR 0223: a note in words for each picture he sends, written after its turn.

When a turn that came with pictures ends, :meth:`PictureNotes.submit` queues one job. The job waits
until his next turn arrives or ten minutes have passed, whichever is first, so a correction he
makes lands in the note. Then, for each picture, one request to the vision preset (the same seam
``screen_look`` uses) gets the picture, his words and her answer of that turn, and his next words;
the note it writes is appended to memory.db (:mod:`jarvis.state.picture_notes`). A picture saved
from another app without a question (a ``user.shared`` event) has no turn: its job is ready at once
and carries only his note, if he wrote one.

The memory queue is the scheduler, not the record of what is owed. What is owed is derived from
durable state: a stored image named by one of his turn-opening rows or saved shares of the last
:data:`RECOVER_HOURS` hours that has no note yet. :meth:`PictureNotes.recover` finds those, at start
and every :data:`RESCAN_S` after, so a restart, a full queue or a dropped job loses nothing; the
context of a recovered job (his words, her answer, his next words) is read from the records that
exist. A note is one record under a fixed id, written once whoever queued it.

Nothing here is on a turn's path. The queue holds at most :data:`MAX_PENDING` jobs. A failed
request is tried once more after :data:`RETRY_AFTER_S`; a picture that fails twice is left alone
until the next restart. Nothing is raised into the caller.
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
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.sent_files import rows_naming_files_between

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
RECOVER_HOURS: Final = 24
RESCAN_S: Final = 600.0
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
    saved: bool = False  # saved from another app, no turn: ``words`` is his note


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
        event_log: Path | None = None,
        pinned_language: str = "follow",
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        threaded: bool = True,
    ) -> None:
        """``event_log`` is where owed notes are found (none: nothing is recovered).

        ``clock`` times the waits and ``wall`` dates the rows. ``threaded=False`` leaves the
        worker unstarted; the caller runs :meth:`recover` and :meth:`run_due`.
        """
        self._event_log = event_log
        self._wall = wall
        self._memory_db = memory_db
        self._store = store
        self._vision = vision
        self._pinned = pinned_language
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.Lock()
        self._pending: deque[_Job] = deque()
        self._queued: set[str] = set()  # pictures in a pending job
        self._given_up: set[str] = set()  # pictures that failed twice in this process
        self._worker: threading.Thread | None = None

    def submit(
        self, record_id: str, pictures: Sequence[str], words: str, answer: str,
    ) -> bool:
        """Queue the note jobs of a turn that has ended; ``False`` when the queue is full."""
        return self._queue(
            _Job(record_id, tuple(pictures), len(pictures), words, answer,
                 window_ends=self._clock() + WINDOW_S),
        )

    def submit_saved(self, pictures: Sequence[str], note: str) -> bool:
        """Queue the notes of pictures saved from another app, with his note text if any."""
        return self._queue(
            _Job("", tuple(pictures), len(pictures), note, "", window_ends=0.0, settled=True,
                 saved=True),
        )

    def _queue(self, job: _Job) -> bool:
        """Add ``job`` for the pictures not already waiting; ``False`` when the queue is full."""
        with self._lock:
            fresh = tuple(one for one in job.pictures if one not in self._queued)
            if not fresh:
                return True
            if len(self._pending) >= MAX_PENDING:
                LOGGER.warning("picture notes: queue full; %d pictures wait", len(fresh))
                return False
            self._pending.append(replace(job, pictures=fresh))
            self._queued.update(fresh)
        self.start()
        return True

    def start(self) -> None:
        """Start the worker (once): it recovers what is owed, then runs the queue."""
        with self._lock:
            if not self._threaded or self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._work, name="jarvis-picture-notes", daemon=True,
            )
        self._worker.start()

    def recover(self, *, min_age_s: float = 0.0) -> int:
        """Queue the pictures he sent in the last day that have no note; how many were queued.

        ``min_age_s`` leaves out rows younger than that: a periodic rescan must not race the
        turn that is still being answered, whose own job is queued when it ends.
        """
        if self._event_log is None:
            return 0
        now_ms = int(self._wall() * 1000)
        since = now_ms - RECOVER_HOURS * 3_600_000
        conn = open_runtime_event_log(self._event_log)
        try:
            rows = rows_naming_files_between(
                conn, since_ms=since, until_ms=now_ms - int(min_age_s * 1000),
            )
        finally:
            conn.close()
        queued = 0
        for row in rows:
            payload = row.payload
            owed = self._owed(payload.get("attachments"))
            if not owed:
                continue
            if row.type == "user.shared":
                queued += self.submit_saved(owed, str(payload.get("note") or "")) * len(owed)
                continue
            record_id = str(payload.get("record_id") or row.event_uid)
            words = str(payload.get("transcript") or "")
            age = max(0.0, now_ms / 1000 - row.ts_epoch_ms / 1000)
            queued += len(owed) * self._queue(
                _Job(record_id, tuple(owed), len(payload["attachments"]),
                     "" if lang.is_filler(words) else words,
                     records.answer_after(self._memory_db, record_id),
                     window_ends=self._clock() + max(0.0, WINDOW_S - age)),
            )
        return queued

    def _owed(self, ids: object) -> list[str]:
        """The stored pictures of ``ids`` that have no note and are not waiting or given up."""
        if not isinstance(ids, list):
            return []
        with self._lock:
            skip = self._queued | self._given_up
        wanted = [str(one) for one in ids if str(one) not in skip]
        have = records.bodies(self._memory_db, wanted)
        found = (self._store.get(one) for one in wanted if one not in have)
        return [ref.id for ref in found if ref is not None and ref.kind == "image"]

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
        rescan_at = 0.0
        while True:
            try:
                if self._clock() >= rescan_at:
                    self.recover(min_age_s=0.0 if rescan_at == 0.0 else WINDOW_S)
                    rescan_at = self._clock() + RESCAN_S
                self.run_due()
            except Exception:
                LOGGER.exception("picture notes: a pass failed; the next one runs as usual")
            time.sleep(_POLL_S)

    def _write(self, job: _Job, following: str | None) -> int:
        """One request per picture; the pictures that failed come back as a retry."""
        written, failed = 0, []
        said = job.words or records.last_words(self._memory_db, job.record_id or None)
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
        retry = failed and job.attempts + 1 < MAX_ATTEMPTS
        with self._lock:
            self._queued.difference_update(
                one for one in job.pictures if not (retry and one in failed)
            )
            if failed and not retry:
                self._given_up.update(failed)
            if retry:
                self._pending.append(replace(
                    job, pictures=tuple(failed), attempts=job.attempts + 1,
                    not_before=self._clock() + RETRY_AFTER_S, settled=True, following=following,
                ))
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
        if job.saved:
            lines = ["He saved this picture from another app without asking about it."]
            lines.append(
                f'His note with it: "{_cut(job.words)}"' if job.words else "He wrote no note.",
            )
        else:
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
