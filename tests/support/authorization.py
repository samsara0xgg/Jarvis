"""A confirmed action seeded as a debt the authorizer can consume."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.shared.realtime import AuthorizedDispatch
from jarvis.state.authorized_dispatch_outbox import (
    authorize_confirmation_dispatch,
    ensure_authorized_dispatch_schema,
)
from tests.support.concurrency import confirmation_candidate, seed_accepted_confirmation

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event


def consume_debt(
    conn: sqlite3.Connection, accepted: Event, snapshot: Mapping[str, object]
) -> AuthorizedDispatch:
    request, lease = confirmation_candidate(accepted.event_uid, snapshot, random_suffix="snapshot")
    result = authorize_confirmation_dispatch(
        conn,
        source_confirmation_event_id=accepted.event_uid,
        action_request=request,
        lease=lease,
        gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
        now_ms=2_010_000,
    )
    assert isinstance(result, AuthorizedDispatch)
    return result


def seed_debt(conn: sqlite3.Connection) -> AuthorizedDispatch:
    ensure_authorized_dispatch_schema(conn)
    accepted, snapshot = seed_accepted_confirmation(conn, suffix="snapshot")
    return consume_debt(conn, accepted, snapshot)
