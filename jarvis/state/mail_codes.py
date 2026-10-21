"""Which mails a verification code was already copied from (ADR 0369).

A copied code is one ``mail.code_copied`` event: the mail's id and its sender's domain, never the
code (ADR 0266). Before the poller reads any mail it asks which of the mails it has just listed
have one, so a restart inside ``mail_codes.window_min`` does not copy a mail's code a second time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

TYPE: Final[str] = "mail.code_copied"

# One row per copied code: the scan over this type's rows is as small as the codes ever copied.
_COPIED_SQL: Final[str] = (
    "SELECT json_extract(payload_json, '$.message_id') FROM events WHERE type = ? "
    "AND json_extract(payload_json, '$.message_id') IN (SELECT value FROM json_each(?))"
)


def record_copy(conn: sqlite3.Connection, message_id: str, sender_domain: str) -> None:
    """Append ``mail.code_copied``: a code was copied from this mail (never the code itself)."""
    emit_event(
        conn,
        type=TYPE,
        payload={"message_id": message_id, "sender_domain": sender_domain.lower()},
    )


def copied_among(conn: sqlite3.Connection, message_ids: Iterable[str]) -> set[str]:
    """The ids among ``message_ids`` of the mails a code was already copied from."""
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return set()
    return {str(row[0]) for row in conn.execute(_COPIED_SQL, (TYPE, json.dumps(ids)))}
