"""ADR-0014 D14 acceptance: the durable ``confirmation.expired`` terminal.

Covers the L2 primitive (one row, its CAS races) and the two folds that read
it, every one of them driven by an injected ``now_ms``, never by a real clock.
The runtime sweep that wrote the row is retired (ADR 0047); the row type stays
in the schema.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime.inherent_view_sequencer import ClientLane, InherentViewSequencer
from jarvis.shared.realtime import AlreadyTerminal, StaleConfirmation, TerminalCommitted
from jarvis.state.event_log import (
    _REGISTRY_MAP,
    emit_event,
    iter_events_of_types,
    open_event_log,
    read_log_epoch,
)
from jarvis.state.inherent_view import (
    _CLEAR_REASON_OF_TYPE,
    CONFIRMATION_EVENT_TYPES,
    InherentView,
)
from jarvis.state.lifecycle_terminal import terminalize_confirmation
from jarvis.state.projections import PendingConfirmations

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


# --- the PendingConfirmations fold (R12) -------------------------------------


def test_folding_the_expired_row_moves_the_slot_out_of_pending(
    conn: sqlite3.Connection,
) -> None:
    """A matching id expires the slot; is_live is False before the deadline."""
    requested = _request(conn, "CONF-fold")
    _expire(conn, requested)

    events = list(
        conn.execute("SELECT event_uid FROM events ORDER BY id"),
    )
    assert len(events) == 2

    projection = PendingConfirmations.from_events(_all_events(conn))
    slot = projection.slot
    assert slot is not None
    assert slot.confirmation_id == "CONF-fold"
    assert slot.state == "expired"
    # Well before the deadline, so only the new state can make this False.
    assert slot.is_live(_REQUESTED_AT_MS) is False


def test_an_expired_row_naming_a_superseded_id_leaves_the_current_slot_alone(
    conn: sqlite3.Connection,
) -> None:
    """The non-matching branch behaves exactly like accepted/rejected."""
    first = _request(conn, "CONF-old")
    _request(conn, "CONF-new")
    # Force the stale terminal in: the sweep could never write this, but a
    # log carrying one from before the supersession must not move the slot.
    emit_event(
        conn,
        type="confirmation.expired",
        payload={"confirmation_id": "CONF-old", "expired_at_ms": _EXPIRES_AT_MS},
        source_event_id=first.event_uid,
    )

    slot = PendingConfirmations.from_events(_all_events(conn)).slot
    assert slot is not None
    assert slot.confirmation_id == "CONF-new"
    assert slot.state == "pending"
    assert slot.is_live(_REQUESTED_AT_MS) is True


def _all_events(conn: sqlite3.Connection) -> list[Event]:
    return list(
        iter_events_of_types(
            conn,
            (
                "confirmation.requested",
                "confirmation.accepted",
                "confirmation.rejected",
                "confirmation.expired",
                "gate.evaluated",
            ),
        ),
    )


# --- the Inherent view clear (R11) -------------------------------------------


def test_the_expired_row_clears_the_panel_with_reason_expired_at_its_cursor(
    conn: sqlite3.Connection,
) -> None:
    """R11: one confirmation.cleared(reason="expired") at the row's cursor."""
    requested = _request(conn, "CONF-view")
    outcome = _expire(conn, requested)
    assert isinstance(outcome, TerminalCommitted)

    fold = InherentView()
    upsert = fold.fold(
        cursor=_revision_of(conn, requested.event_uid),
        event_uid=requested.event_uid,
        event_type="confirmation.requested",
        ts_epoch_ms=_REQUESTED_AT_MS,
        payload=requested.payload,
        source_event_id=None,
        correlation=None,
    )
    assert upsert is not None
    assert [change.kind for change in upsert.changes] == ["confirmation.upsert"]

    expired_cursor = _revision_of(conn, outcome.event.event_uid)
    cleared = fold.fold(
        cursor=expired_cursor,
        event_uid=outcome.event.event_uid,
        event_type="confirmation.expired",
        # Strictly before the deadline, so the lazy read-time clear cannot
        # fire and only the durable row can produce this change.
        ts_epoch_ms=_REQUESTED_AT_MS + 1,
        payload=outcome.event.payload,
        source_event_id=requested.event_uid,
        correlation=None,
    )
    assert cleared is not None
    assert len(cleared.changes) == 1
    change = cleared.changes[0]
    assert change.kind == "confirmation.cleared"
    assert change.cleared is not None
    assert change.cleared.confirmation_id == "CONF-view"
    assert change.cleared.reason == "expired"
    assert change.cleared.revision == expired_cursor


def test_the_expired_row_produces_exactly_one_clear_at_its_real_timestamp(
    conn: sqlite3.Connection,
) -> None:
    """The production shape: the row's own ts is necessarily past the deadline.

    A sweep only terminalizes at or after ``expires_at_ms``, so the committed
    row's ``ts_epoch_ms`` always satisfies the fold's lazy read-time check as
    well. Both paths can therefore fire on this one row, and the pin is that
    the client still sees exactly ONE clear, with reason ``expired``, at that
    row's own cursor.
    """
    requested = _request(conn, "CONF-real")
    outcome = _expire(conn, requested)
    assert isinstance(outcome, TerminalCommitted)

    fold = InherentView()
    fold.fold(
        cursor=_revision_of(conn, requested.event_uid),
        event_uid=requested.event_uid,
        event_type="confirmation.requested",
        ts_epoch_ms=_REQUESTED_AT_MS,
        payload=requested.payload,
        source_event_id=None,
        correlation=None,
    )

    expired_cursor = _revision_of(conn, outcome.event.event_uid)
    transition = fold.fold(
        cursor=expired_cursor,
        event_uid=outcome.event.event_uid,
        event_type="confirmation.expired",
        ts_epoch_ms=outcome.event.ts_epoch_ms,
        payload=outcome.event.payload,
        source_event_id=requested.event_uid,
        correlation=None,
    )
    assert outcome.event.ts_epoch_ms >= _EXPIRES_AT_MS
    assert transition is not None
    reasons = [
        change.cleared.reason for change in transition.changes if change.cleared is not None
    ]
    assert [change.kind for change in transition.changes] == ["confirmation.cleared"]
    assert reasons == ["expired"]
    assert transition.changes[0].cleared is not None
    assert transition.changes[0].cleared.revision == expired_cursor


def test_the_sequencer_delivers_the_expired_row_as_one_cleared_delta(
    tmp_path: Path,
) -> None:
    """The static row query feeds the fold, so a v2 panel actually sees it.

    Without ``confirmation.expired`` in ``_SELECT_RESPONSE_ROWS_SQL`` the fold
    is never handed the row and an idle panel keeps the expired ask on screen
    — the exact failure this card exists to remove.
    """
    conn = open_event_log(tmp_path / "events.db")
    sequencer = InherentViewSequencer(
        conn, log_epoch=read_log_epoch(conn), boot_id="Bfixture0001",
    )
    frames: list[tuple[str, int]] = []
    lane = ClientLane(
        connection_id="C1",
        enqueue=lambda frame, cursor: frames.append((frame, cursor)),
    )
    staging = sequencer.begin_snapshot(lane)

    requested = _request(conn, "CONF-seq")
    outcome = _expire(conn, requested)
    assert isinstance(outcome, TerminalCommitted)
    replayed = sequencer.complete_snapshot(lane, staging)
    conn.close()

    payloads = [json.loads(frame)["payload"]["changes"] for frame, _ in frames]
    assert replayed == 2
    assert [[change["kind"] for change in changes] for changes in payloads] == [
        ["confirmation.upsert"], ["confirmation.cleared"],
    ]
    assert payloads[1][0]["reason"] == "expired"
    assert payloads[1][0]["confirmation_id"] == "CONF-seq"


def test_every_confirmation_terminal_type_has_a_clear_reason() -> None:
    """No confirmation row the fold accepts may fall through the reason table.

    Mirrors the SQL-vs-FOLD_EVENT_TYPES pin: the fold now looks the reason up
    rather than branching on two values, so a fifth type added to
    CONFIRMATION_EVENT_TYPES without a table entry would raise at fold time.
    """
    terminals = set(CONFIRMATION_EVENT_TYPES) - {"confirmation.requested"}
    assert terminals == set(_CLEAR_REASON_OF_TYPE)
