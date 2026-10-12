"""ADR 0255: the pictures he sent, as the conversation keeps them.

His record is linked to its files when it is written. Records written before that link existed are
linked here, from the event log, for the files still kept (:func:`link_kept_pictures`).

On a Claude preset a picture in history is sent by its Files API id, not its bytes.
:class:`PictureFiles` uploads a picture the first time a request needs it and keeps the id beside
the file (``Attachments.file_id``); :meth:`PictureFiles.release` deletes a copy when its file is
swept or erased.
Nothing here raises into a turn: a picture that cannot be uploaded rides as words for that request,
and is not tried again for :data:`RETRY_AFTER_S`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

from jarvis.state import picture_notes as note_records
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import link_attachments, linked_attachment_ids

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.state.attachments import Attachments

LOGGER = logging.getLogger(__name__)

SOURCES: Final = ("surface.user_intent", "utterance.received")
"""The rows that open one of his turns; the record carries the row's uid (or its ``record_id``)."""
_DAY_MS: Final = 86_400_000


def link_kept_pictures(
    memory_db: Path, event_log: Path, store: Attachments, *, days: int | None,
) -> int:
    """Link records written before ADR 0255 to the files still kept; how many records got links.

    Reads the opening rows of his turns of the last ``days`` (all when ``None``) that name files,
    and links each record whose files are kept and not linked yet. Safe to repeat.
    """
    since = 0 if days is None else int(time.time() * 1000) - days * _DAY_MS
    marks = ",".join("?" * len(SOURCES))
    conn = open_runtime_event_log(event_log)
    try:
        rows = conn.execute(
            "SELECT event_uid, payload_json FROM events "  # noqa: S608 — marks is "?, ?"
            f"WHERE type IN ({marks}) AND ts_epoch_ms >= ? "
            "AND json_extract(payload_json, '$.attachments') IS NOT NULL ORDER BY id",
            (*SOURCES, since),
        ).fetchall()
    finally:
        conn.close()
    linked = linked_attachment_ids(memory_db)
    done = 0
    for uid, raw in rows:
        payload = json.loads(raw)
        ids = payload.get("attachments")
        if not isinstance(ids, list) or all(str(one) in linked for one in ids):
            continue
        refs = [ref for one in ids if (ref := store.get(str(one))) is not None]
        if not refs:
            continue
        link_attachments(memory_db, str(payload.get("record_id") or uid), refs)
        linked.update(ref.id for ref in refs)
        done += 1
    if done:
        LOGGER.info("pictures: linked %d earlier records to their files", done)
    return done


RETRY_AFTER_S: Final = 600.0
"""A picture whose upload failed rides as words, and is not tried again for this long."""
WAIT_S: Final = 5.0
"""The most one request's history waits, in all, for uploads that are in flight."""
_UPLOAD_WORKERS: Final = 2


class PictureFiles:
    """The Files API copies of the history pictures: uploaded in the background, read by id."""

    def __init__(
        self, store: Attachments, *, memory_db: Path | None = None,
        clock: Callable[[], float] = time.monotonic, wait_s: float = WAIT_S,
    ) -> None:
        """``store`` keeps the file ids; ``memory_db`` holds the notes; ``clock`` is monotonic.

        ``wait_s`` is how long a request waits, in all, for uploads that are still running.
        """
        self._store = store
        self._memory_db = memory_db
        self._clock = clock
        self._wait_s = wait_s
        self._lock = threading.Lock()
        self._failed: dict[str, float] = {}
        self._inflight: dict[str, Future[str | None]] = {}
        self._pool: ThreadPoolExecutor | None = None

    def start(self, client: LLMClient, attachment_id: str) -> Future[str | None] | None:
        """Begin uploading a stored picture in the background; never blocks, never raises.

        ``None`` when there is nothing to do: it has an id, was tried and failed a moment ago,
        is gone, is not a picture, or ``client`` is not on an Anthropic preset. A picture already
        uploading returns its running upload.
        """
        if client.provider != "anthropic" or self._store.file_id(attachment_id) is not None:
            return None
        with self._lock:
            if running := self._inflight.get(attachment_id):
                return running
            if self._store.file_id(attachment_id) is not None:  # finished since the check above
                return None
            if self._clock() < self._failed.get(attachment_id, 0.0):
                return None
            ref = self._store.get(attachment_id)
            if ref is None or ref.kind != "image":
                return None
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    _UPLOAD_WORKERS, thread_name_prefix="jarvis-picture-upload",
                )
            future = self._pool.submit(self._upload, client, attachment_id)
            self._inflight[attachment_id] = future
            return future

    def _upload(self, client: LLMClient, attachment_id: str) -> str | None:
        try:
            ref = self._store.get(attachment_id)
            path = self._store.path(attachment_id)
            if ref is None or path is None:
                return None
            file_id = client.upload_file(ref.name, path.read_bytes(), ref.mime)
            self._store.set_file_id(attachment_id, file_id)
        except Exception:  # noqa: BLE001 — a failed upload costs the picture, not the turn
            LOGGER.warning(
                "pictures: could not upload %s; it rides as words", attachment_id[:8],
                exc_info=True,
            )
            with self._lock:
                self._failed[attachment_id] = self._clock() + RETRY_AFTER_S
            return None
        finally:
            with self._lock:
                self._inflight.pop(attachment_id, None)
        return file_id

    def resolver(self, client: LLMClient) -> Callable[[str], str | None] | None:
        """Attachment id -> file id for one request on ``client``; ``None`` off Anthropic.

        Only a Claude preset takes a file id; any other provider keeps the marker and the note.
        A picture with an id rides at once. One still uploading (or started now) is waited for,
        for at most ``wait_s`` in all across the request's pictures; whatever is not there by
        then rides as its marker and note for this request and is found ready by a later one.
        """
        if client.provider != "anthropic":
            return None
        deadline = time.monotonic() + self._wait_s

        def file_id(attachment_id: str) -> str | None:
            if (known := self._store.file_id(attachment_id)) is not None:
                return known
            if (running := self.start(client, attachment_id)) is None:
                return self._store.file_id(attachment_id)
            try:
                return running.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                return None

        return file_id

    def stale(self, file_ids: Sequence[str]) -> tuple[dict[str, str], Callable[[], None]]:
        """File ids the API would not read: their pictures' notes, and what forgets the ids.

        The note stands in for the picture in the one request that is retried (marker and note,
        as before ADR 0255). The ids are forgotten, with a pause before the picture is uploaded
        again, only once that retry has been answered: a rejection that was about something
        else must not cost valid ids, and their uploaded copies with them.
        """
        by_file = {file_id: one for one, file_id in self._store.file_ids().items()}
        pictures = {file_id: by_file[file_id] for file_id in file_ids if file_id in by_file}
        notes = {
            file_id: text for file_id, one in pictures.items()
            if self._memory_db is not None
            and (text := note_records.note_text(self._memory_db, one)) is not None
        }

        def forget() -> None:
            with self._lock:
                for one in pictures.values():
                    self._store.forget_file_id(one)
                    self._failed[one] = self._clock() + RETRY_AFTER_S
            LOGGER.warning(
                "pictures: dropped %d file ids the API could not read; those pictures ride as "
                "words and are uploaded again later", len(pictures),
            )

        LOGGER.warning(
            "pictures: the API rejected %d file ids; retrying the request with words",
            len(file_ids),
        )
        return notes, forget

    def release(self, client: LLMClient | None, *, everything: bool = False) -> int:
        """Delete the Files API copies of the pictures whose file is gone; how many went.

        ``everything`` deletes every copy (the data is being erased). A copy that cannot be
        deleted now stays on record and is tried at the next sweep.
        """
        copies = self._store.file_ids(orphaned=not everything)
        if not copies:
            return 0
        if client is None:
            LOGGER.warning(
                "pictures: %d uploaded copies to delete, but no Anthropic preset", len(copies),
            )
            return 0
        done = 0
        for attachment_id, file_id in copies.items():
            try:
                client.delete_file(file_id)
            except Exception:  # noqa: BLE001 — best effort; the id stays for the next sweep
                LOGGER.warning(
                    "pictures: could not delete the uploaded copy of %s", attachment_id[:8],
                    exc_info=True,
                )
                continue
            self._store.forget_file_id(attachment_id)
            done += 1
        if done:
            LOGGER.info("pictures: deleted %d uploaded copies", done)
        return done
