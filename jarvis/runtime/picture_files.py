"""ADR 0255: the pictures he sent, as the conversation keeps them.

His record is linked to its files when it is written. Records written before that link existed are
linked here, from the event log, for the files still kept (:func:`link_kept_pictures`).

Layer rules: ``jarvis.state`` only; the runtime wires it (L5/L2).
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import link_attachments, linked_attachment_ids

if TYPE_CHECKING:
    from pathlib import Path

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
