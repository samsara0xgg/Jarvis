"""Unit tests for `jarvis.state.event_log` (ADR § Tier 1, Step 4).

Covers: append + iterate determinism, unregistered-type rejection, missing
required-payload rejection, schema_version mismatch rejection, dangling
source_event_id rejection, trigger-level UPDATE/DELETE block, idempotent
open, full round-trip via Event dataclass.

Uses `tmp_path` for the DB file; never touches `~/.jarvis`.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.shared import Event
from jarvis.state.event_log import (
    DanglingSourceEventError,
    EventTypeRegistry,
    MissingPayloadFieldError,
    SchemaVersionMismatchError,
    UnregisteredEventTypeError,
    emit_event,
    get_event,
    iter_events,
    open_event_log,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ----------------------------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh Event Log at `tmp_path/mac_events.db`."""
    return open_event_log(tmp_path / "mac_events.db")


# --- Registry tests ---------------------------------------------------------


def test_registry_contains_canonical_trace_types():
    """All 16 canonical-trace event types must be registered (ADR § A7)."""
    expected = {
        "task.created",
        "turn.started",
        "turn.ended",
        "utterance.received",
        "entity.resolved",
        "action.proposed",
        "gate.evaluated",
        "action.authorized",
        "action.dispatched",
        "action.running",
        "action.result_observed",
        "action.failed",
        "action.timeout_assumed",
        "action.cancelled",
        "run.started",
        "worker.reported",
        "claim.created",
        "evidence.attached",
        "task.verified",
    }
    registered = set(EventTypeRegistry.iter_types())
    missing = expected - registered
    assert not missing, f"missing canonical trace event types: {missing}"


def test_registry_gate_evaluated_schema_matches_adr():
    """`gate.evaluated` schema must match the ADR JSON block verbatim."""
    schema = EventTypeRegistry.get("gate.evaluated")
    assert schema is not None
    assert schema.required_payload == ("gate", "outcome", "reasons")
    # `attempt` is the Pre-emit Gate retry index (0=initial, 1=LLM
    # retry, 2=forced template); see `jarvis.decision._finalize_response`.
    assert schema.optional_payload == (
        "action_id",
        "response_hash",
        "claim_levels",
        "check_results",
        "attempt",
    )
    assert schema.owner_layer == "L3"
    assert schema.schema_version == 1


def test_registry_get_returns_none_for_unregistered():
    """`EventTypeRegistry.get` returns None for an unknown event type."""
    assert EventTypeRegistry.get("totally.fake") is None


def test_registry_requires_and_optional_round_trip():
    """`requires` / `optional` expose the registered tuples verbatim.

    `task.created.optional_payload` was extended in ADR-0002 Step 1 with
    `repo_path` + `verify_command`; required_payload is unchanged.
    """
    assert EventTypeRegistry.requires("task.created") == ("task_id", "goal")
    assert EventTypeRegistry.optional("task.created") == (
        "source",
        "deadline",
        "repo_path",
        "verify_command",
    )


# --- Day-2 registry extensions (ADR-0002 Step 1) ----------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "worker.heartbeat",
        "worker.artifact_observed",
        "worker.report_missing",
        "task.executor_assigned",
        "task.executor_reported",
        "cost.recorded",
        "surface.user_intent",
        "surface.response_emitted",
        "mac.sleeping",
        "mac.awake",
        "worker.suspended_by_sleep",
        "worker.terminated_by_sleep",
    ],
)
def test_day2_event_type_registered(event_type: str) -> None:
    """All 12 ADR-0002 Day-2 event types must be in the registry."""
    schema = EventTypeRegistry.get(event_type)
    assert schema is not None, f"{event_type!r} not in EventTypeRegistry"
    assert schema.event_type == event_type
    assert schema.schema_version == 1


def test_registry_task_created_required_payload_unchanged() -> None:
    """ADR-0002 F7: `task.created.required_payload` stays `(task_id, goal)`."""
    schema = EventTypeRegistry.get("task.created")
    assert schema is not None
    assert schema.required_payload == ("task_id", "goal")


def test_registry_task_created_optional_payload_extended() -> None:
    """ADR-0002 F7: optional_payload gains `repo_path` + `verify_command`."""
    schema = EventTypeRegistry.get("task.created")
    assert schema is not None
    assert "repo_path" in schema.optional_payload
    assert "verify_command" in schema.optional_payload
    # Day-1 optional fields preserved.
    assert "source" in schema.optional_payload
    assert "deadline" in schema.optional_payload


def test_registry_action_result_observed_optional_payload_has_error_payload() -> None:
    """ADR-0001 F1 tail / ADR-0002 amendment: `error_payload` is optional."""
    schema = EventTypeRegistry.get("action.result_observed")
    assert schema is not None
    assert "error_payload" in schema.optional_payload
    # Day-1 optional fields preserved.
    assert "tool_output" in schema.optional_payload
    assert "error" in schema.optional_payload
    assert "run_id" in schema.optional_payload


def test_registry_evidence_attached_required_payload_day2() -> None:
    """`evidence.attached.required_payload` carries the F8 `relation` field.

    ADR-0002 F8 / Step 12 lifts the Day-1 `(evidence_id, claim_id, level)`
    tuple to the Day-2 `(evidence_id, claim_id, relation, level)` shape now
    that every emit-site in the L3 Result Interpreter derives `relation`
    from the F2 ladder (spec §8.6). Step 1 deferred this amendment to
    keep Day-1 Tier-1 green; Step 12 lands it after the emit-sites are
    updated. See Step 12 commit body.
    """
    schema = EventTypeRegistry.get("evidence.attached")
    assert schema is not None
    assert schema.required_payload == (
        "evidence_id",
        "claim_id",
        "relation",
        "level",
    )


def test_registry_cost_recorded_is_l3_owned() -> None:
    """ADR-0002 lines 1198-1212: cost.recorded.owner_layer is L3.

    L4 returns cost data inside RawResult.metadata["cost"]; L3 is the
    sole emit-site (Step 3 wires this).
    """
    schema = EventTypeRegistry.get("cost.recorded")
    assert schema is not None
    assert schema.owner_layer == "L3"
    assert schema.required_payload == ("kind", "model")


# --- ADR-0003 Inherent Text Surface registry extensions --------------------


def test_registry_turn_failed_registered() -> None:
    """ADR-0003 D7: `turn.failed` is the watcher-level catch-all event.

    Registered Day-2 so emit_event(type="turn.failed", ...) is callable;
    the emit-site lands in Step 8 inside runtime/inherent_loop.py's
    _user_intent_watcher when drive_turn raises uncaught.
    """
    schema = EventTypeRegistry.get("turn.failed")
    assert schema is not None, "turn.failed must be in EventTypeRegistry per ADR-0003 D7"
    assert schema.event_type == "turn.failed"
    assert schema.owner_layer == "L5"
    assert schema.required_payload == ("turn_id", "exception_repr")
    assert schema.optional_payload == ("trigger_event_id",)
    assert schema.schema_version == 1


def test_emit_turn_failed_happy_path(tmp_path: Path) -> None:
    """emit_event with the minimum required payload succeeds and round-trips."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="turn.failed",
            payload={
                "turn_id": "T1",
                "exception_repr": "RuntimeError('boom')",
            },
            ts_epoch_ms=0,
        )
    assert evt.type == "turn.failed"
    assert evt.schema_version == 1
    assert evt.payload == {
        "turn_id": "T1",
        "exception_repr": "RuntimeError('boom')",
    }


def test_emit_turn_failed_accepts_optional_trigger_event_id(tmp_path: Path) -> None:
    """`trigger_event_id` is optional; supplying it round-trips through payload."""
    with closing(_open(tmp_path)) as conn:
        parent = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "hi", "turn_id": "T1"},
            ts_epoch_ms=0,
        )
        evt = emit_event(
            conn,
            type="turn.failed",
            payload={
                "turn_id": "T1",
                "exception_repr": "RuntimeError('boom')",
                "trigger_event_id": parent.event_uid,
            },
            ts_epoch_ms=1,
        )
    assert evt.payload["trigger_event_id"] == parent.event_uid


def test_emit_turn_failed_rejects_missing_turn_id(tmp_path: Path) -> None:
    """Missing `turn_id` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="turn.failed",
                payload={"exception_repr": "RuntimeError('boom')"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_turn_failed_rejects_missing_exception_repr(tmp_path: Path) -> None:
    """Missing `exception_repr` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="turn.failed",
                payload={"turn_id": "T1"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


# --- ADR-0003 Step 2 chunked-response registry extensions ------------------


def test_registry_surface_response_open_registered() -> None:
    """ADR-0003 Step 2 D10: `surface.response_open` is the turn-level header.

    Emitted once per turn by render_response BEFORE any chunk; carries the
    user transcript (`query`) and the surface kind (always `"text"` in
    Step 2). Triggers the WS `op:open` envelope at the broadcaster.
    """
    schema = EventTypeRegistry.get("surface.response_open")
    assert schema is not None, (
        "surface.response_open must be in EventTypeRegistry per ADR-0003 Step 2 D10"
    )
    assert schema.event_type == "surface.response_open"
    assert schema.owner_layer == "L5"
    assert schema.required_payload == ("turn_id", "query", "kind")
    assert schema.optional_payload == ()
    assert schema.schema_version == 1


def test_registry_surface_response_chunk_registered() -> None:
    """ADR-0003 Step 2 D10: `surface.response_chunk` is the per-chunk event.

    Emitted by render_response once per sentence (`sentence` gate mode) or
    once total (`full_text` / `structured` mode). Triggers the WS
    `op:append` envelope at the broadcaster.
    """
    schema = EventTypeRegistry.get("surface.response_chunk")
    assert schema is not None, (
        "surface.response_chunk must be in EventTypeRegistry per ADR-0003 Step 2 D10"
    )
    assert schema.event_type == "surface.response_chunk"
    assert schema.owner_layer == "L5"
    assert schema.required_payload == ("turn_id", "text")
    assert schema.optional_payload == ()
    assert schema.schema_version == 1


def test_emit_surface_response_open_happy_path(tmp_path: Path) -> None:
    """emit_event with the minimum required payload succeeds and round-trips."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T1",
                "query": "what's on my plate today?",
                "kind": "text",
            },
            ts_epoch_ms=0,
        )
        fetched = list(iter_events(conn))
    assert evt.type == "surface.response_open"
    assert evt.schema_version == 1
    assert evt.payload == {
        "turn_id": "T1",
        "query": "what's on my plate today?",
        "kind": "text",
    }
    assert fetched == [evt]


def test_emit_surface_response_chunk_happy_path(tmp_path: Path) -> None:
    """emit_event with the minimum required payload succeeds and round-trips."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="surface.response_chunk",
            payload={"turn_id": "T1", "text": "First sentence."},
            ts_epoch_ms=0,
        )
        fetched = list(iter_events(conn))
    assert evt.type == "surface.response_chunk"
    assert evt.schema_version == 1
    assert evt.payload == {"turn_id": "T1", "text": "First sentence."}
    assert fetched == [evt]


def test_emit_surface_response_open_rejects_missing_turn_id(tmp_path: Path) -> None:
    """Missing `turn_id` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="surface.response_open",
                payload={"query": "hi", "kind": "text"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_surface_response_open_rejects_missing_query(tmp_path: Path) -> None:
    """Missing `query` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="surface.response_open",
                payload={"turn_id": "T1", "kind": "text"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_surface_response_open_rejects_missing_kind(tmp_path: Path) -> None:
    """Missing `kind` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="surface.response_open",
                payload={"turn_id": "T1", "query": "hi"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_surface_response_chunk_rejects_missing_turn_id(tmp_path: Path) -> None:
    """Missing `turn_id` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={"text": "hello"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_surface_response_chunk_rejects_missing_text(tmp_path: Path) -> None:
    """Missing `text` raises MissingPayloadFieldError; nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={"turn_id": "T1"},
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


# --- emit_event happy path --------------------------------------------------


def test_emit_event_round_trip_through_iter_events(tmp_path: Path) -> None:
    """emit_event return value == iter_events row for the same event."""
    with closing(_open(tmp_path)) as conn:
        emitted = emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "ship Day-1"},
            correlation={"turn_id": "T1"},
            ts_epoch_ms=1_000,
        )
        events = list(iter_events(conn))

    assert len(events) == 1
    persisted = events[0]
    assert persisted == emitted
    # Dataclass equality is structural; sanity-check the load-bearing fields.
    assert persisted.type == "task.created"
    assert persisted.schema_version == 1
    assert persisted.ts_epoch_ms == 1_000
    assert persisted.payload == {"task_id": "task_X", "goal": "ship Day-1"}
    assert persisted.correlation == {"turn_id": "T1"}
    assert persisted.source_event_id is None


def test_emit_event_default_uid_is_unique(tmp_path: Path) -> None:
    """Default `event_uid` (uuid4 hex) is unique across two appends."""
    with closing(_open(tmp_path)) as conn:
        first = emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T1"},
            ts_epoch_ms=10,
        )
        second = emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T2"},
            ts_epoch_ms=11,
        )
    assert first.event_uid != second.event_uid
    assert len(first.event_uid) == 32  # uuid4 hex is 32 lowercase hex chars.
    assert all(ch in "0123456789abcdef" for ch in first.event_uid)


def test_emit_event_get_event_returns_persisted_event(tmp_path: Path) -> None:
    """`get_event(uid)` returns the same Event that `emit_event` returned."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T1"},
            ts_epoch_ms=5,
            event_uid="evt-fixed-1",
        )
        fetched = get_event(conn, "evt-fixed-1")
    assert fetched == evt
    assert fetched is not None
    assert fetched.event_uid == "evt-fixed-1"


def test_get_event_returns_none_for_unknown_uid(tmp_path: Path) -> None:
    """`get_event` returns None when the uid is absent (no exception)."""
    with closing(_open(tmp_path)) as conn:
        assert get_event(conn, "no-such-uid") is None


def test_iter_events_is_deterministic_across_appends(tmp_path: Path) -> None:
    """Append three events; iter_events yields them in append order."""
    with closing(_open(tmp_path)) as conn:
        e1 = emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T1"},
            ts_epoch_ms=1,
            event_uid="u1",
        )
        e2 = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "hi", "turn_id": "T1"},
            ts_epoch_ms=2,
            event_uid="u2",
            source_event_id="u1",
        )
        e3 = emit_event(
            conn,
            type="turn.ended",
            payload={"turn_id": "T1"},
            ts_epoch_ms=3,
            event_uid="u3",
            source_event_id="u2",
        )
        seen = list(iter_events(conn))

    assert seen == [e1, e2, e3]
    # Source-chain integrity preserved.
    assert seen[1].source_event_id == "u1"
    assert seen[2].source_event_id == "u2"


def test_emit_event_accepts_optional_payload_absent(tmp_path: Path) -> None:
    """Optional-payload keys may be absent (only required keys must be present)."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},  # no source, no deadline.
            ts_epoch_ms=0,
        )
    assert evt.payload == {"task_id": "task_X", "goal": "G"}


def test_emit_event_serializes_nested_payload(tmp_path: Path) -> None:
    """Nested lists / dicts in payload survive the JSON round-trip."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="entity.resolved",
            payload={
                "entity_type": "task",
                "natural_ref": "yesterday's task",
                "resolved_to": "task_X",
                "confidence": "high",
                "candidates": ["task_X", "task_Y"],
                "match_basis": "single open task",
                "outcome": "resolved",
            },
            ts_epoch_ms=0,
            event_uid="e1",
        )
        fetched = get_event(conn, "e1")
    assert fetched == evt
    assert fetched is not None
    assert fetched.payload["candidates"] == ["task_X", "task_Y"]


# --- emit_event validation errors -------------------------------------------


def test_emit_event_rejects_unregistered_type(tmp_path: Path) -> None:
    """Unregistered `type` raises `UnregisteredEventTypeError`, nothing written."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(UnregisteredEventTypeError):
            emit_event(
                conn,
                type="totally.fake",
                payload={"x": 1},
                ts_epoch_ms=0,
            )
        # Nothing was written.
        assert list(iter_events(conn)) == []


def test_emit_event_rejects_missing_required_payload(tmp_path: Path) -> None:
    """Missing required payload key raises `MissingPayloadFieldError`."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(MissingPayloadFieldError):
            emit_event(
                conn,
                type="task.created",
                payload={"task_id": "task_X"},  # missing "goal".
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_event_rejects_schema_version_mismatch(tmp_path: Path) -> None:
    """Explicit `schema_version` that differs from registry raises."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(SchemaVersionMismatchError):
            emit_event(
                conn,
                type="task.created",
                payload={"task_id": "task_X", "goal": "G"},
                schema_version=2,  # registry value is 1.
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_event_accepts_explicit_matching_schema_version(tmp_path: Path) -> None:
    """Passing the same schema_version as the registry is allowed."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},
            schema_version=1,
            ts_epoch_ms=0,
        )
    assert evt.schema_version == 1


def test_emit_event_rejects_dangling_source_event_id(tmp_path: Path) -> None:
    """`source_event_id` referencing a missing uid raises `DanglingSourceEventError`."""
    with closing(_open(tmp_path)) as conn:
        with pytest.raises(DanglingSourceEventError):
            emit_event(
                conn,
                type="turn.started",
                payload={"turn_id": "T1"},
                source_event_id="does-not-exist",
                ts_epoch_ms=0,
            )
        assert list(iter_events(conn)) == []


def test_emit_event_accepts_valid_source_event_id(tmp_path: Path) -> None:
    """A `source_event_id` pointing to an existing event passes validation."""
    with closing(_open(tmp_path)) as conn:
        parent = emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T1"},
            ts_epoch_ms=0,
        )
        child = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "hi", "turn_id": "T1"},
            source_event_id=parent.event_uid,
            ts_epoch_ms=1,
        )
    assert child.source_event_id == parent.event_uid


# --- Append-only trigger enforcement (acceptance A6) ------------------------


def test_update_events_raises(tmp_path: Path) -> None:
    """Direct UPDATE on `events` must raise per append-only trigger."""
    with closing(_open(tmp_path)) as conn:
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},
            ts_epoch_ms=0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE events SET payload_json = '{}' WHERE id = 1")


def test_delete_events_raises(tmp_path: Path) -> None:
    """Direct DELETE on `events` must raise per append-only trigger."""
    with closing(_open(tmp_path)) as conn:
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},
            ts_epoch_ms=0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM events WHERE id = 1")


# --- Idempotent open --------------------------------------------------------


def test_open_event_log_is_idempotent(tmp_path: Path) -> None:
    """Opening the same path twice must not raise and must not double triggers.

    A double-registered trigger would raise `OperationalError: trigger
    events_no_update already exists` — we open twice and assert the
    triggers still fire correctly on UPDATE/DELETE.
    """
    db_path = tmp_path / "mac_events.db"
    with closing(open_event_log(db_path)) as conn1:
        emit_event(
            conn1,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},
            ts_epoch_ms=0,
        )

    with closing(open_event_log(db_path)) as conn2:
        # Schema is reused; existing row is still readable.
        events = list(iter_events(conn2))
        assert len(events) == 1
        # Triggers still fire.
        with pytest.raises(sqlite3.IntegrityError):
            conn2.execute("UPDATE events SET payload_json = '{}' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn2.execute("DELETE FROM events WHERE id = 1")


def test_open_event_log_does_not_duplicate_triggers(tmp_path: Path) -> None:
    """sqlite_master should list each trigger exactly once after two opens."""
    db_path = tmp_path / "mac_events.db"
    with closing(open_event_log(db_path)):
        pass
    with closing(open_event_log(db_path)) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")
        names = [row[0] for row in cursor]
    assert names == ["events_no_delete", "events_no_update"]


# --- Round-trip via shared.Event --------------------------------------------


def test_emit_event_returns_shared_event_dataclass(tmp_path: Path) -> None:
    """The return value of `emit_event` is a `jarvis.shared.Event`."""
    with closing(_open(tmp_path)) as conn:
        evt = emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "G"},
            ts_epoch_ms=0,
        )
    assert isinstance(evt, Event)
