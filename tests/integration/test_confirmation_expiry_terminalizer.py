"""ADR-0014 D14 acceptance: the durable ``confirmation.expired`` terminal.

Covers the L2 primitive: one row per confirmation_id, and the two CAS races
that keep it that way.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.shared.realtime import AlreadyTerminal, StaleConfirmation, TerminalCommitted
from jarvis.state.event_log import _REGISTRY_MAP, emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_confirmation

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event

_TTL_MS = 600_000
_REQUESTED_AT_MS = 1_700_000_000_000
_EXPIRES_AT_MS = _REQUESTED_AT_MS + _TTL_MS

_TERMINAL_TYPES = ("confirmation.accepted", "confirmation.rejected", "confirmation.expired")


def _snapshot() -> dict[str, Any]:
    return {
        "tool_name": "write_file",
        "caller": "jarvis_llm",
        "canonical_target": "/var/folders/expiry.md",
        "target_entity_ref": "file:/var/folders/expiry.md",
        "risk_level": "L3",
        "args_meta": {"content_sha256": "0" * 64, "content_bytes": 4, "content_artifact": "a"},
    }


def _request(
    conn: sqlite3.Connection,
    confirmation_id: str,
    *,
    expires_at_ms: int = _EXPIRES_AT_MS,
) -> Event:
    return emit_event(
        conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": confirmation_id,
            "action_snapshot": _snapshot(),
            "template_line": f"pending write_file for {confirmation_id}",
            "expires_at_ms": expires_at_ms,
        },
        ts_epoch_ms=_REQUESTED_AT_MS,
    )


def _terminal_count(conn: sqlite3.Connection, confirmation_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type IN (?, ?, ?) "
        "AND json_extract(payload_json, '$.confirmation_id') = ?",
        (*_TERMINAL_TYPES, confirmation_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _revision_of(conn: sqlite3.Connection, event_uid: str) -> int:
    row = conn.execute("SELECT id FROM events WHERE event_uid = ?", (event_uid,)).fetchone()
    assert row is not None
    return int(row[0])


def _expire(
    conn: sqlite3.Connection,
    requested: Event,
    *,
    expected_revision: int | None = None,
    expired_at_ms: int = _EXPIRES_AT_MS,
) -> TerminalCommitted | AlreadyTerminal | StaleConfirmation:
    confirmation_id = str(requested.payload["confirmation_id"])
    revision = (
        _revision_of(conn, requested.event_uid) if expected_revision is None else expected_revision
    )
    return terminalize_confirmation(
        conn,
        event_type="confirmation.expired",
        payload={"confirmation_id": confirmation_id, "expired_at_ms": expired_at_ms},
        expected_revision=revision,
        source_event_id=requested.event_uid,
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """One open Event Log per test, on its own temporary file."""
    return open_event_log(tmp_path / "events.db")


# --- the durable row ---------------------------------------------------------


def test_the_expired_terminal_carries_only_its_two_keys_and_names_its_request(
    conn: sqlite3.Connection,
) -> None:
    """One row, payload exactly {confirmation_id, expired_at_ms}, L3-owned."""
    requested = _request(conn, "CONF-row")
    outcome = _expire(conn, requested)

    assert isinstance(outcome, TerminalCommitted)
    assert outcome.owner == "confirmation"
    assert outcome.identity == "CONF-row"

    row = conn.execute(
        "SELECT type, payload_json, source_event_id, actor FROM events WHERE event_uid = ?",
        (outcome.event.event_uid,),
    ).fetchone()
    assert row is not None
    event_type, payload_json, source_event_id, actor = row
    assert event_type == "confirmation.expired"
    assert json.loads(payload_json) == {
        "confirmation_id": "CONF-row",
        "expired_at_ms": _EXPIRES_AT_MS,
    }
    assert source_event_id == requested.event_uid
    assert actor == "jarvis_runtime"

    schema = _REGISTRY_MAP["confirmation.expired"]
    assert schema.owner_layer == "L3"
    assert schema.actor == "jarvis_runtime"
    assert schema.required_payload == ("confirmation_id", "expired_at_ms")
    assert schema.optional_payload == ()
    assert _terminal_count(conn, "CONF-row") == 1


# --- the two CAS races -------------------------------------------------------


def test_an_accepted_confirmation_expires_to_already_terminal_and_appends_nothing(
    conn: sqlite3.Connection,
) -> None:
    """R7 AlreadyTerminal: the accept won, the sweep appends nothing."""
    requested = _request(conn, "CONF-accepted")
    accepted = emit_event(
        conn,
        type="confirmation.accepted",
        payload={
            "confirmation_id": "CONF-accepted",
            "utterance_raw": "可以",
            "grammar_rule_id": "yes.plain",
        },
        source_event_id=requested.event_uid,
    )

    outcome = _expire(conn, requested)

    assert isinstance(outcome, AlreadyTerminal)
    assert outcome.event.event_uid == accepted.event_uid
    assert outcome.event.type == "confirmation.accepted"
    count = _terminal_count(conn, "CONF-accepted")
    print(f"terminal rows for CONF-accepted: {count}")  # noqa: T201 - acceptance evidence
    assert count == 1


def test_a_newer_request_between_fold_and_cas_returns_stale_and_appends_nothing(
    conn: sqlite3.Connection,
) -> None:
    """R7 Stale: the slot the sweep folded has been superseded."""
    first = _request(conn, "CONF-first")
    folded_revision = _revision_of(conn, first.event_uid)
    # The window the sweep cannot hold a lock across: a fresh ask lands.
    second = _request(conn, "CONF-second")

    outcome = _expire(conn, first, expected_revision=folded_revision)

    assert isinstance(outcome, StaleConfirmation)
    assert outcome.confirmation_id == "CONF-first"
    assert outcome.expected_revision == folded_revision
    assert outcome.actual_revision == _revision_of(conn, second.event_uid)
    assert _terminal_count(conn, "CONF-first") == 0
    assert _terminal_count(conn, "CONF-second") == 0


def test_a_second_expiry_call_returns_already_terminal_with_the_row_that_won(
    conn: sqlite3.Connection,
) -> None:
    """Exactly one terminal row per confirmation_id under repeated calls."""
    requested = _request(conn, "CONF-twice")
    first = _expire(conn, requested)
    second = _expire(conn, requested)

    assert isinstance(first, TerminalCommitted)
    assert isinstance(second, AlreadyTerminal)
    assert second.event.event_uid == first.event.event_uid
    assert _terminal_count(conn, "CONF-twice") == 1

