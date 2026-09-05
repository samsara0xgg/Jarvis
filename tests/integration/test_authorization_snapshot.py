"""Real SQLite authorization snapshots preserve hidden debt without side effects."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.packet import assemble_packet
from jarvis.shared.realtime import AuthorizedDispatch
from jarvis.state import decision_snapshot
from jarvis.state.authorization_snapshot import read_authorization_snapshot
from jarvis.state.authorized_dispatch_outbox import (
    authorize_confirmation_dispatch,
    ensure_authorized_dispatch_schema,
)
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import append_event_in_transaction, emit_event, open_event_log
from tests.integration.test_wave1_concurrency_safety import (
    _confirmation_candidate,
    _seed_accepted_confirmation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.state.authorization_snapshot import AuthorizationSnapshot


def _consume(
    conn: sqlite3.Connection, accepted: Event, snapshot: Mapping[str, object]
) -> AuthorizedDispatch:
    request, lease = _confirmation_candidate(accepted.event_uid, snapshot, random_suffix="snapshot")
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


def _seed_debt(conn: sqlite3.Connection) -> AuthorizedDispatch:
    ensure_authorized_dispatch_schema(conn)
    accepted, snapshot = _seed_accepted_confirmation(conn, suffix="snapshot")
    return _consume(conn, accepted, snapshot)


def test_empty_snapshot_performs_no_schema_or_data_writes(tmp_path: Path) -> None:
    """SQLite's authorizer blocks every write while the actual L3 packet is built."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": "T", "transcript": "why does ice melt?"},
        )
        denied: list[int] = []
        writes = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE,
        }

        def authorize(action: int, *_args: object) -> int:
            if action in writes:
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        packet = assemble_packet(trigger, conn)
        assert denied == []
        assert packet.event_cursor == 1
        assert packet.authorization_snapshot is not None
        assert packet.authorization_snapshot.complete
        assert packet.authorization_snapshot.tables_present == ()
        assert not conn.in_transaction


@pytest.mark.parametrize("consume", [False, True])
def test_new_confirmation_slot_cannot_hide_prior_accepted_debt(
    tmp_path: Path, *, consume: bool
) -> None:
    """Expired acceptance A stays visible after UI slot B replaces it."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        ensure_authorized_dispatch_schema(conn)
        accepted, snapshot = _seed_accepted_confirmation(conn, suffix="A")
        if consume:
            _consume(conn, accepted, snapshot)
        newer, _snapshot = _seed_accepted_confirmation(conn, suffix="B")
        state = read_decision_snapshot(conn)
        assert state.authorizations.complete
        assert state.projections.pending_confirmations.slot is not None
        assert state.projections.pending_confirmations.slot.confirmation_id == "CONF-B"
        first, second = state.authorizations.confirmations
        assert first.confirmation_id == "CONF-A"
        assert first.answer_event_uid == accepted.event_uid
        assert first.superseded
        assert first.state == ("consumed" if consume else "accepted_unconsumed")
        assert second.answer_event_uid == newer.event_uid
        assert second.state == "accepted_unconsumed"
        if consume:
            assert first.dispatch is not None
            assert first.dispatch.state == "pending"
            assert json.loads(first.dispatch.request_json)["turn_id"] == "T-confirm"


def test_unrelated_leased_gate_cannot_mark_confirmation_consumed(tmp_path: Path) -> None:
    """Legacy slot folding is not the authority for atomic consumption identity."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        ensure_authorized_dispatch_schema(conn)
        accepted, _snapshot = _seed_accepted_confirmation(conn, suffix="A")
        emit_event(
            conn,
            type="gate.evaluated",
            payload={
                "gate": "pre_action",
                "outcome": "pass",
                "reasons": [],
                "lease_id": "unrelated",
            },
        )
        state = read_decision_snapshot(conn)
        assert not state.authorizations.complete
        record = state.authorizations.confirmations[0]
        assert record.answer_event_uid == accepted.event_uid
        assert record.state == "accepted_unconsumed"
        assert record.dispatch is None


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM authorized_dispatch_outbox",
        "DELETE FROM confirmation_consumption_claims",
        "UPDATE authorized_dispatch_outbox SET state = 'unknown'",
        "UPDATE authorized_dispatch_outbox SET state = 'dispatched'",
        "UPDATE authorized_dispatch_outbox SET request_hash = 'wrong'",
        "UPDATE authorized_dispatch_outbox SET action_id = 'foreign'",
        "UPDATE confirmation_consumption_claims SET gate_event_uid = 'foreign'",
        "DROP TABLE confirmation_consumption_claims",
        "DROP TABLE authorized_dispatch_outbox",
    ],
)
def test_corrupt_or_partial_authorization_debt_is_explicitly_incomplete(
    tmp_path: Path, sql: str
) -> None:
    """Every mutation occurs in a disposable DB; the reader never repairs corruption."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _seed_debt(conn)
        conn.execute(sql)
        conn.commit()
        before = conn.total_changes
        state = read_decision_snapshot(conn)
        assert not state.authorizations.complete
        assert state.authorizations.errors
        assert len(state.authorizations.confirmations) == 1
        assert conn.total_changes == before
        assert not conn.in_transaction


@pytest.mark.parametrize(
    ("state_value", "source_valid", "expected"),
    [("pending", True, False), ("dispatched", False, False), ("dispatched", True, True)],
)
def test_dispatch_state_requires_exact_atomic_admission_source(
    tmp_path: Path, state_value: str, *, source_valid: bool, expected: bool
) -> None:
    """Pending cannot have an admission, and dispatched must bind the correct gate."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        dispatch = _seed_debt(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE authorized_dispatch_outbox SET state = ?", (state_value,))
        append_event_in_transaction(
            conn,
            type="action.dispatched",
            payload={"action_id": dispatch.identity.action_id},
            source_event_id=dispatch.gate_event.event_uid if source_valid else None,
        )
        conn.commit()
        result = read_decision_snapshot(conn).authorizations
        assert result.complete is expected
        assert result.confirmations[0].dispatch is not None


def test_packet_event_cursor_and_operational_tables_share_one_read_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second connection consumes after the event read; the packet stays pre-commit."""
    path = tmp_path / "events.db"
    with (
        contextlib.closing(open_event_log(path)) as reader,
        contextlib.closing(open_event_log(path)) as writer,
    ):
        ensure_authorized_dispatch_schema(reader)
        accepted, frozen = _seed_accepted_confirmation(reader, suffix="race")
        original = read_authorization_snapshot
        consumed = False

        def consume_between_reads(
            conn: sqlite3.Connection, events: Sequence[Event]
        ) -> AuthorizationSnapshot:
            nonlocal consumed
            if not consumed:
                _consume(writer, accepted, frozen)
                consumed = True
            return original(conn, events)

        monkeypatch.setattr(decision_snapshot, "read_authorization_snapshot", consume_between_reads)
        before = read_decision_snapshot(reader)
        after = read_decision_snapshot(reader)
        assert before.authorizations.complete
        assert before.authorizations.confirmations[0].state == "accepted_unconsumed"
        assert before.authorizations.confirmations[0].dispatch is None
        assert after.authorizations.complete
        assert after.authorizations.confirmations[0].state == "consumed"
        assert after.event_cursor > before.event_cursor
        assert all(
            event.type != "gate.evaluated" for event in before.projections.recent_trace.events
        )


def test_snapshot_borrows_and_never_commits_caller_transaction(tmp_path: Path) -> None:
    """A caller-owned transaction remains open and can still roll back its own work."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        conn.execute("BEGIN")
        append_event_in_transaction(
            conn, type="utterance.received", payload={"turn_id": "T", "transcript": "uncommitted"}
        )
        state = read_decision_snapshot(conn)
        assert state.event_cursor == 1
        assert conn.in_transaction
        conn.rollback()
        assert read_decision_snapshot(conn).event_cursor == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"expires_at_ms": "invalid"},
        {"expires_at_ms": True},
        {"expires_at_ms": None},
        {"action_snapshot": "invalid"},
        {"confirmation_id": []},
        {"template_line": 7},
    ],
)
def test_malformed_confirmation_successor_cannot_crash_or_reactivate_old_slot(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    """Validly appended malformed fields become explicit unknown packet evidence."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        ensure_authorized_dispatch_schema(conn)
        accepted, frozen = _seed_accepted_confirmation(conn, suffix="old")
        emit_event(
            conn,
            type="confirmation.requested",
            payload={
                "confirmation_id": "malformed",
                "action_snapshot": dict(frozen),
                "expires_at_ms": 2_060_000,
                "template_line": "synthetic request",
                **fields,
            },
        )
        trigger = emit_event(
            conn, type="utterance.received", payload={"turn_id": "next", "transcript": "synthetic"}
        )
        packet = assemble_packet(trigger, conn)
        assert packet.pending_confirmation.slot is None
        assert packet.authorization_snapshot is not None
        assert not packet.authorization_snapshot.complete
        assert packet.authorization_snapshot.confirmations[0].answer_event_uid == accepted.event_uid


def test_empty_partial_schema_cannot_establish_no_authorization_debt(tmp_path: Path) -> None:
    """Even zero rows require the actual supported table definitions."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        conn.execute(
            "CREATE TABLE confirmation_consumption_claims (source_confirmation_event_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE authorized_dispatch_outbox (source_confirmation_event_id TEXT, "
            "state TEXT, created_at_ms INTEGER, gate_event_uid TEXT)"
        )
        conn.commit()
        state = read_decision_snapshot(conn)
        assert not state.authorizations.complete
        assert "unsupported_authorization_schema" in state.authorizations.errors


@pytest.mark.parametrize("kind", ["gate", "admission"])
def test_foreign_identity_cannot_borrow_an_existing_acceptance_or_gate(
    tmp_path: Path, kind: str
) -> None:
    """Reverse source joins validate identity, not merely source UID membership."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        dispatch = _seed_debt(conn)
        if kind == "gate":
            emit_event(
                conn,
                type="gate.evaluated",
                payload={
                    "gate": "pre_action",
                    "outcome": "pass",
                    "reasons": [],
                    "action_id": "foreign",
                    "lease_id": "foreign",
                    "authorization_id": "foreign",
                    "dispatch_id": "foreign",
                },
                source_event_id=dispatch.identity.source_confirmation_event_id,
            )
        else:
            emit_event(
                conn,
                type="action.dispatched",
                payload={"action_id": "foreign"},
                source_event_id=dispatch.gate_event.event_uid,
            )
        state = read_decision_snapshot(conn)
        assert not state.authorizations.complete
        assert state.authorizations.errors
