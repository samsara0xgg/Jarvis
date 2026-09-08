"""Wave 1 integration acceptance: SQLite races, rollback, and cost protocols."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
import yaml

from jarvis.decision import DecideContext, decide
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import ChatStreamChunk, LLMClient
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import VisionCallError, _build_vision_cost_recorder, _LLMVisionClient
from jarvis.shared import ActionRequest, AuthorizationLease, CallerPrincipal
from jarvis.shared.realtime import (
    AlreadyConsumed,
    AlreadyTerminal,
    AuthorizedDispatch,
    CostAccountingDisposition,
    CostAlreadyRecorded,
    CostRecorded,
    TerminalCommitted,
    Wave1FeatureFlags,
)
from jarvis.state.authorized_dispatch_outbox import (
    AuthorizedDispatchCorruptionError,
    ConfirmationRevalidationError,
    authorize_confirmation_dispatch,
    ensure_authorized_dispatch_schema,
    get_authorized_dispatch,
)
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.cost_accounting import (
    ensure_cost_accounting_schema,
    record_cost_disposition_once,
)
from jarvis.state.event_log import (
    InvalidCorrelationError,
    NestedEventTransactionError,
    append_event_in_transaction,
    emit_event,
    open_event_log,
)
from jarvis.state.lifecycle_terminal import (
    terminalize_action,
    terminalize_playback,
    terminalize_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from jarvis.shared import Event
    from jarvis.shared.realtime import ConfirmationConsumptionOutcome, TerminalOutcome


def _raw_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _event_count(conn: sqlite3.Connection, event_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?",
        (event_type,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    allowed = {
        "confirmation_consumption_claims",
        "authorized_dispatch_outbox",
        "cost_accounting_dispositions",
    }
    assert table in allowed
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def test_transactional_append_rolls_back_and_bus_observes_only_commit(tmp_path: Path) -> None:
    """The no-commit append cannot leak a row or pre-commit notification."""
    path = tmp_path / "events.db"
    conn = open_event_log(path)
    observer = _raw_connection(path)
    bus = CommittedEventBus()
    observed_counts: list[int] = []

    def _on_commit(_event: Event) -> None:
        observed_counts.append(_event_count(observer, "surface.user_intent"))

    bus.subscribe(_on_commit)
    conn.execute("BEGIN IMMEDIATE")
    append_event_in_transaction(
        conn,
        type="surface.user_intent",
        payload={"transcript": "rolled back", "turn_id": "T-rollback"},
    )
    assert _event_count(conn, "surface.user_intent") == 1
    assert _event_count(observer, "surface.user_intent") == 0
    conn.rollback()
    assert _event_count(observer, "surface.user_intent") == 0
    assert observed_counts == []

    committed = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "committed", "turn_id": "T-commit"},
        committed_event_bus=bus,
    )
    assert committed.payload["turn_id"] == "T-commit"
    assert observed_counts == [1]

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(NestedEventTransactionError):
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "must not commit", "turn_id": "T-nested"},
        )
    conn.rollback()
    assert _event_count(observer, "surface.user_intent") == 1

    conn.execute("BEGIN")
    with pytest.raises(InvalidCorrelationError):
        append_event_in_transaction(
            conn,
            type="surface.user_intent",
            payload={"transcript": "invalid", "turn_id": "T-invalid"},
            correlation=cast("Any", {"turn_id": 7}),
        )
    conn.rollback()
    with pytest.raises(InvalidCorrelationError):
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "invalid", "turn_id": "T-invalid"},
            correlation=cast("Any", {"turn_id": 7}),
        )
    assert _event_count(observer, "surface.user_intent") == 1
    observer.close()
    conn.close()


def test_emit_event_with_source_event_id_waits_for_contended_writer_instead_of_failing_fast(
    tmp_path: Path,
) -> None:
    """emit_event's BEGIN IMMEDIATE keeps busy_timeout alive on the source-id path.

    A deferred BEGIN lets the `source_event_id` validation SELECT take a read
    snapshot, so the INSERT becomes a read->write upgrade that SQLite fails
    instantly with SQLITE_BUSY, bypassing the busy handler entirely.
    """
    path = tmp_path / "contended.db"
    conn = open_event_log(path)
    seed = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "seed", "turn_id": "T-seed"},
    )

    hold_seconds = 0.3
    holding = threading.Event()

    def _hold_write_lock() -> None:
        writer = _raw_connection(path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            holding.set()
            time.sleep(hold_seconds)
            writer.execute("COMMIT")
        finally:
            holding.set()
            writer.close()

    holder = threading.Thread(target=_hold_write_lock)
    holder.start()
    assert holding.wait(timeout=10)
    try:
        started = time.perf_counter()
        contended = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "contended", "turn_id": "T-contended"},
            source_event_id=seed.event_uid,
        )
        elapsed = time.perf_counter() - started
    finally:
        holder.join(timeout=10)

    assert contended.source_event_id == seed.event_uid
    assert elapsed >= hold_seconds * 0.6
    assert elapsed < 5.0
    assert _event_count(conn, "surface.user_intent") == 2
    conn.close()


def _race_two_connections(
    path: Path,
    calls: tuple[Callable[[sqlite3.Connection], TerminalOutcome], ...],
) -> tuple[TerminalOutcome, ...]:
    barrier = threading.Barrier(len(calls) + 1)

    def _worker(call: Callable[[sqlite3.Connection], TerminalOutcome]) -> TerminalOutcome:
        conn = _raw_connection(path)
        try:
            barrier.wait()
            return call(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_worker, call) for call in calls]
        barrier.wait()
        return tuple(future.result(timeout=10) for future in futures)


@pytest.mark.parametrize("owner", ["playback", "response", "action"])
def test_lifecycle_terminal_two_connection_race_has_one_winner(
    tmp_path: Path,
    owner: str,
) -> None:
    """BEGIN IMMEDIATE serializes every owner through one shared arbiter."""
    path = tmp_path / f"{owner}.db"
    setup = open_event_log(path)
    setup.close()
    bus = CommittedEventBus()
    published: list[str] = []
    publish_lock = threading.Lock()

    def _published(event: Event) -> None:
        with publish_lock:
            published.append(event.event_uid)

    bus.subscribe(_published)
    if owner == "playback":
        base = {
            "response_id": "R-playback",
            "turn_id": "T1",
            "playback_generation_id": 7,
            "heard_through_sequence": None,
            "submitted_samples": 10,
        }
        calls = (
            lambda conn: terminalize_playback(
                conn,
                event_type="surface.playback_completed",
                payload={**base, "session_id": "S-one", "speech_text_hash": "full"},
                committed_event_bus=bus,
            ),
            lambda conn: terminalize_playback(
                conn,
                event_type="surface.playback_interrupted",
                payload={
                    **base,
                    "session_id": "S-two",
                    "heard_text_hash": "partial",
                    "reason": "race",
                },
                committed_event_bus=bus,
            ),
        )
        terminal_types = _PLAYBACK_TYPES
    elif owner == "response":
        common = {"response_id": "R-response", "response_group_id": "G1", "turn_id": "T1"}
        calls = (
            lambda conn: terminalize_response(
                conn,
                event_type="response.completed",
                payload={**common, "response_hash": "complete"},
                committed_event_bus=bus,
            ),
            lambda conn: terminalize_response(
                conn,
                event_type="response.cancelled",
                payload={**common, "reason": "race"},
                committed_event_bus=bus,
            ),
        )
        terminal_types = _RESPONSE_TYPES
    else:
        calls = (
            lambda conn: terminalize_action(
                conn,
                event_type="action.result_observed",
                payload={"action_id": "A-race", "semantics": "observation"},
                committed_event_bus=bus,
            ),
            lambda conn: terminalize_action(
                conn,
                event_type="action.cancelled",
                payload={"action_id": "A-race", "reason": "race"},
                committed_event_bus=bus,
            ),
        )
        terminal_types = _ACTION_TYPES

    outcomes = _race_two_connections(path, calls)
    assert sum(isinstance(outcome, TerminalCommitted) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, AlreadyTerminal) for outcome in outcomes) == 1
    assert outcomes[0].event.event_uid == outcomes[1].event.event_uid
    assert published == [outcomes[0].event.event_uid]
    if owner == "playback":
        assert {outcome.identity for outcome in outcomes} == {"R-playback:7"}
        assert outcomes[0].event.payload["session_id"] in {"S-one", "S-two"}

    check = _raw_connection(path)
    placeholders = ",".join("?" for _ in terminal_types)
    count = check.execute(
        f"SELECT COUNT(*) FROM events WHERE type IN ({placeholders})",  # noqa: S608
        tuple(sorted(terminal_types)),
    ).fetchone()
    assert count is not None
    assert int(count[0]) == 1
    check.close()


_PLAYBACK_TYPES = frozenset(
    {
        "surface.playback_completed",
        "surface.playback_interrupted",
        "surface.playback_failed",
    },
)
_RESPONSE_TYPES = frozenset({"response.completed", "response.cancelled", "response.failed"})
_ACTION_TYPES = frozenset(
    {"action.result_observed", "action.failed", "action.timeout_assumed", "action.cancelled"},
)


def test_playback_terminal_index_migrates_from_session_scoped_definition(
    tmp_path: Path,
) -> None:
    """Opening a Wave 1 database installs the corrected two-part CAS index."""
    path = tmp_path / "playback-index-migration.db"
    legacy = open_event_log(path)
    legacy.execute("DROP INDEX idx_events_playback_terminal_v2")
    legacy.execute(
        "CREATE INDEX idx_events_playback_terminal "
        "ON events(type, json_extract(payload_json, '$.session_id'), "
        "json_extract(payload_json, '$.response_id'), "
        "json_extract(payload_json, '$.playback_generation_id'))",
    )
    legacy.commit()
    legacy.close()

    migrated = open_event_log(path)
    row = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_events_playback_terminal_v2'",
    ).fetchone()
    assert row is not None
    definition = str(row[0])
    assert "session_id" not in definition
    assert "response_id" in definition
    assert "playback_generation_id" in definition
    migrated.close()


def test_terminal_failure_after_append_rolls_back_without_publish(tmp_path: Path) -> None:
    """A terminal event and its notification are both absent on rollback."""
    conn = open_event_log(tmp_path / "terminal-failure.db")
    bus = CommittedEventBus()
    published: list[str] = []
    bus.subscribe(lambda event: published.append(event.event_uid))

    def _fail(stage: str) -> None:
        if stage == "after_event_append":
            message = "injected"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="injected"):
        terminalize_response(
            conn,
            event_type="response.failed",
            payload={
                "response_id": "R-fail",
                "response_group_id": "G-fail",
                "turn_id": "T-fail",
                "reason": "provider",
            },
            committed_event_bus=bus,
            failure_injector=cast("Any", _fail),
        )
    assert _event_count(conn, "response.failed") == 0
    assert published == []
    conn.close()


def _seed_accepted_confirmation(
    conn: sqlite3.Connection,
    *,
    suffix: str,
) -> tuple[Event, Mapping[str, object]]:
    now_ms = 2_000_000
    content = "safe"
    content_bytes = content.encode("utf-8")
    target = f"file:/tmp/{suffix}.txt"
    snapshot: dict[str, object] = {
        "tool_name": "write_file",
        "caller": "jarvis_llm",
        "canonical_target": f"/tmp/{suffix}.txt",
        "target_entity_ref": target,
        "risk_level": "L3",
        "args_meta": {
            "target": target,
            "mode": "overwrite",
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content_bytes": len(content_bytes),
            "content_artifact": f"/tmp/{suffix}.pending",
        },
    }
    requested = emit_event(
        conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": f"CONF-{suffix}",
            "action_snapshot": snapshot,
            "template_line": "approved",
            "expires_at_ms": now_ms + 60_000,
        },
    )
    accepted = emit_event(
        conn,
        type="confirmation.accepted",
        payload={
            "confirmation_id": f"CONF-{suffix}",
            "utterance_raw": "可以",
            "grammar_rule_id": "yes",
        },
        source_event_id=requested.event_uid,
    )
    return accepted, snapshot


def _confirmation_candidate(
    accepted_event_uid: str,
    snapshot: Mapping[str, object],
    *,
    random_suffix: str,
) -> tuple[ActionRequest, AuthorizationLease]:
    target = cast("str", snapshot["target_entity_ref"])
    request = ActionRequest(
        action_id=f"random-action-{random_suffix}",
        tool_name="write_file",
        target_entity_ref=target,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L3",
        arguments={"target": target, "mode": "overwrite", "content": "safe"},
        authorization_lease=None,
        run_id=None,
        turn_id="T-confirm",
    )
    lease: AuthorizationLease = {
        "lease_id": f"random-lease-{random_suffix}",
        "granted_by": "allen",
        "granted_to": CallerPrincipal.JARVIS_LLM,
        "allowed_tools": frozenset({"write_file"}),
        "allowed_targets": frozenset({target}),
        "expires_at_ms": 2_050_000,
        "max_uses": 1,
        "reason": "approved",
        "source_confirmation_event_id": accepted_event_uid,
    }
    return request, lease


def test_confirmation_acceptance_two_connection_race_has_one_dispatch(tmp_path: Path) -> None:
    """Different random candidates resolve to one stable accepted-event debt."""
    path = tmp_path / "confirmation-race.db"
    setup = open_event_log(path)
    accepted, snapshot = _seed_accepted_confirmation(setup, suffix="race")
    ensure_authorized_dispatch_schema(setup)
    setup.close()
    bus = CommittedEventBus()
    published: list[str] = []
    bus.subscribe(lambda event: published.append(event.event_uid))
    barrier = threading.Barrier(3)

    def _worker(suffix: str) -> ConfirmationConsumptionOutcome:
        conn = _raw_connection(path)
        request, lease = _confirmation_candidate(
            accepted.event_uid,
            snapshot,
            random_suffix=suffix,
        )
        try:
            barrier.wait()
            return authorize_confirmation_dispatch(
                conn,
                source_confirmation_event_id=accepted.event_uid,
                action_request=request,
                lease=lease,
                gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
                now_ms=2_010_000,
                committed_event_bus=bus,
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker, suffix) for suffix in ("one", "two")]
        barrier.wait()
        outcomes = tuple(future.result(timeout=10) for future in futures)

    assert sum(isinstance(outcome, AuthorizedDispatch) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, AlreadyConsumed) for outcome in outcomes) == 1
    dispatches = tuple(
        outcome.dispatch if isinstance(outcome, AlreadyConsumed) else outcome
        for outcome in outcomes
    )
    assert dispatches[0].identity == dispatches[1].identity
    assert dispatches[0].identity.action_id.startswith("A")
    assert dispatches[0].identity.action_id not in {
        "random-action-one",
        "random-action-two",
    }
    assert published == [dispatches[0].gate_event.event_uid]

    check = _raw_connection(path)
    assert _table_count(check, "confirmation_consumption_claims") == 1
    assert _table_count(check, "authorized_dispatch_outbox") == 1
    assert _event_count(check, "gate.evaluated") == 1
    request_row = check.execute(
        "SELECT request_json FROM authorized_dispatch_outbox",
    ).fetchone()
    assert request_row is not None
    bounded_request = json.loads(str(request_row[0]))
    assert "content" not in bounded_request["arguments"]
    assert bounded_request["arguments"]["content_ref"]["sha256"]
    assert "safe" not in str(request_row[0])
    check.close()


@pytest.mark.parametrize(
    "tamper",
    ["request_hash", "request_binding", "claim_binding", "gate_source", "gate_payload"],
)
def test_authorized_dispatch_recovery_rejects_persisted_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    """Recovery never returns dispatchable debt from a mismatched durable chain."""
    conn = open_event_log(tmp_path / f"dispatch-recovery-{tamper}.db")
    accepted, snapshot = _seed_accepted_confirmation(conn, suffix=tamper)
    request, lease = _confirmation_candidate(
        accepted.event_uid,
        snapshot,
        random_suffix=tamper,
    )
    outcome = authorize_confirmation_dispatch(
        conn,
        source_confirmation_event_id=accepted.event_uid,
        action_request=request,
        lease=lease,
        gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
        now_ms=2_010_000,
    )
    assert isinstance(outcome, AuthorizedDispatch)

    if tamper == "request_hash":
        conn.execute(
            "UPDATE authorized_dispatch_outbox SET request_hash = ?",
            ("0" * 64,),
        )
    elif tamper == "request_binding":
        row = conn.execute(
            "SELECT request_json FROM authorized_dispatch_outbox",
        ).fetchone()
        assert row is not None
        request_payload = json.loads(str(row[0]))
        request_payload["tool_name"] = "unapproved_tool"
        encoded = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE authorized_dispatch_outbox SET request_json = ?, request_hash = ?",
            (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()),
        )
    elif tamper == "claim_binding":
        conn.execute(
            "UPDATE confirmation_consumption_claims SET confirmation_id = ?",
            ("CONF-unrelated",),
        )
    else:
        bad_payload = dict(outcome.gate_event.payload)
        source_event_id: str | None = accepted.event_uid
        if tamper == "gate_source":
            source_event_id = accepted.source_event_id
            assert source_event_id is not None
        else:
            bad_payload["dispatch_id"] = "DISP-unrelated"
        bad_gate = emit_event(
            conn,
            type="gate.evaluated",
            payload=bad_payload,
            source_event_id=source_event_id,
        )
        conn.execute(
            "UPDATE confirmation_consumption_claims SET gate_event_uid = ?",
            (bad_gate.event_uid,),
        )
        conn.execute(
            "UPDATE authorized_dispatch_outbox SET gate_event_uid = ?",
            (bad_gate.event_uid,),
        )
    conn.commit()

    with pytest.raises(AuthorizedDispatchCorruptionError):
        get_authorized_dispatch(conn, accepted.event_uid)
    conn.close()


def test_confirmation_gate_source_override_cannot_escape_accepted_uid(
    tmp_path: Path,
) -> None:
    """Callers cannot redirect the passing gate's cause chain."""
    conn = open_event_log(tmp_path / "confirmation-gate-source.db")
    accepted, snapshot = _seed_accepted_confirmation(conn, suffix="gate-source")
    request, lease = _confirmation_candidate(
        accepted.event_uid,
        snapshot,
        random_suffix="gate-source",
    )
    assert accepted.source_event_id is not None
    with pytest.raises(ConfirmationRevalidationError, match="canonical acceptance"):
        authorize_confirmation_dispatch(
            conn,
            source_confirmation_event_id=accepted.event_uid,
            action_request=request,
            lease=lease,
            gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
            gate_source_event_id=accepted.source_event_id,
            now_ms=2_010_000,
        )
    assert _event_count(conn, "gate.evaluated") == 0
    assert _table_count(conn, "confirmation_consumption_claims") == 0
    assert _table_count(conn, "authorized_dispatch_outbox") == 0
    conn.close()


@pytest.mark.parametrize(
    "failure_stage",
    ["after_gate_append", "after_consumption_claim", "after_outbox_insert"],
)
def test_confirmation_failure_injection_is_all_or_nothing(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    """Gate, accepted-event claim, and outbox share one rollback boundary."""
    conn = open_event_log(tmp_path / f"confirmation-{failure_stage}.db")
    accepted, snapshot = _seed_accepted_confirmation(conn, suffix=failure_stage)
    ensure_authorized_dispatch_schema(conn)
    request, lease = _confirmation_candidate(
        accepted.event_uid,
        snapshot,
        random_suffix="fault",
    )
    bus = CommittedEventBus()
    published: list[str] = []
    bus.subscribe(lambda event: published.append(event.event_uid))

    def _fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=failure_stage):
        authorize_confirmation_dispatch(
            conn,
            source_confirmation_event_id=accepted.event_uid,
            action_request=request,
            lease=lease,
            gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
            now_ms=2_010_000,
            committed_event_bus=bus,
            failure_injector=cast("Any", _fail),
        )
    assert _event_count(conn, "gate.evaluated") == 0
    assert _table_count(conn, "confirmation_consumption_claims") == 0
    assert _table_count(conn, "authorized_dispatch_outbox") == 0
    assert published == []
    conn.close()


@pytest.mark.parametrize(
    "case",
    ["slot_expired", "lease_expired", "wrong_tool_scope", "max_uses", "content_drift"],
)
def test_confirmation_revalidation_refuses_without_partial_debt(
    tmp_path: Path,
    case: str,
) -> None:
    """Slot, expiry, scope, use-count, and snapshot drift all fail closed."""
    conn = open_event_log(tmp_path / f"confirmation-revalidate-{case}.db")
    accepted, snapshot = _seed_accepted_confirmation(conn, suffix=case)
    request, lease = _confirmation_candidate(
        accepted.event_uid,
        snapshot,
        random_suffix=case,
    )
    now_ms = 2_010_000
    if case == "slot_expired":
        now_ms = 2_070_000
    elif case == "lease_expired":
        lease["expires_at_ms"] = 2_005_000
    elif case == "wrong_tool_scope":
        lease["allowed_tools"] = frozenset({"other_tool"})
    elif case == "max_uses":
        lease["max_uses"] = 2
    else:
        request = replace(
            request,
            arguments={**request.arguments, "content": "tampered"},
        )

    with pytest.raises(ConfirmationRevalidationError):
        authorize_confirmation_dispatch(
            conn,
            source_confirmation_event_id=accepted.event_uid,
            action_request=request,
            lease=lease,
            gate_payload={"gate": "pre_action", "outcome": "pass", "reasons": []},
            now_ms=now_ms,
        )
    assert _event_count(conn, "gate.evaluated") == 0
    assert _table_count(conn, "confirmation_consumption_claims") == 0
    assert _table_count(conn, "authorized_dispatch_outbox") == 0
    conn.close()


class _FakeOpenAICompletions:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def create(self, **kwargs: object) -> object:
        if self.mode == "error":
            message = "provider exploded"
            raise RuntimeError(message)
        if self.mode == "vision_error":
            encoded = "A" * 96
            message = f"provider echoed data:image/png;base64,{encoded}"
            raise RuntimeError(message)
        if kwargs.get("stream") is not True:
            return SimpleNamespace(
                id=f"resp-{self.mode}",
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="batch", tool_calls=None),
                    ),
                ],
                usage=SimpleNamespace(
                    prompt_tokens=11,
                    completion_tokens=3,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=2),
                ),
            )

        text = SimpleNamespace(
            id=f"resp-{self.mode}",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="hello"),
                    finish_reason=None,
                ),
            ],
            usage=None,
        )
        if self.mode == "cancel":
            return iter((text,))
        if self.mode == "stream_error":
            return self._stream_error(text)
        terminal = SimpleNamespace(
            id=f"resp-{self.mode}",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None),
                    finish_reason="stop",
                ),
            ],
            usage=None,
        )
        if self.mode == "usage_only":
            usage_only = SimpleNamespace(
                id=f"resp-{self.mode}",
                choices=[],
                usage=SimpleNamespace(
                    prompt_tokens=17,
                    completion_tokens=5,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=4),
                ),
            )
            return iter((text, terminal, usage_only))
        return iter((text, terminal))

    def _stream_error(self, text: object) -> Iterator[object]:
        yield text
        message = "stream provider exploded"
        raise RuntimeError(message)


def _openai_client(mode: str) -> LLMClient:
    client = LLMClient(
        {"provider": "openai", "model": "gpt-4", "max_tokens": 32},
    )
    client._openai_client = SimpleNamespace(  # noqa: SLF001 - provider fixture seam
        chat=SimpleNamespace(completions=_FakeOpenAICompletions(mode)),
    )
    return client


class _FakeAnthropicStream:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> Iterator[object]:
        return iter(
            (
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(
                        id="anthropic-response",
                        usage=SimpleNamespace(
                            input_tokens=23,
                            cache_read_input_tokens=3,
                            cache_creation_input_tokens=2,
                        ),
                    ),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(text="anthropic"),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                    usage=SimpleNamespace(output_tokens=7),
                ),
            ),
        )

    def get_final_message(self) -> object:
        return SimpleNamespace(
            id="anthropic-response",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=23,
                output_tokens=7,
                cache_read_input_tokens=3,
                cache_creation_input_tokens=2,
            ),
        )


def _anthropic_client() -> LLMClient:
    client = LLMClient(
        {"provider": "anthropic", "model": "claude-test", "max_tokens": 32},
    )
    client._anthropic_client = SimpleNamespace(  # noqa: SLF001 - provider fixture seam
        messages=SimpleNamespace(stream=lambda **_kwargs: _FakeAnthropicStream()),
    )
    return client


def _stream_all(recorder: CostRecorder, client: LLMClient) -> list[ChatStreamChunk]:
    return list(
        recorder.chat_stream(
            client,
            messages=[{"role": "user", "content": "hi"}],
            system="system",
            kind="decision",
            turn_id="T-cost",
        ),
    )


def test_cost_guard_covers_batch_stream_cancel_error_and_terminal_usage(tmp_path: Path) -> None:
    """Every provider exit gets one non-zero-or-explicitly-unavailable row."""
    conn = open_event_log(tmp_path / "cost-guard.db")
    recorder = CostRecorder(conn)

    batch_client = _openai_client("batch")
    recorder.chat(
        batch_client,
        messages=[{"role": "user", "content": "batch"}],
        system="system",
        kind="decision",
        turn_id="T-cost",
    )

    stream_client = _openai_client("stream")
    stream_chunks = _stream_all(recorder, stream_client)
    assert stream_chunks[-1].is_final is True

    cancel_client = _openai_client("cancel")
    cancelled = recorder.chat_stream(
        cancel_client,
        messages=[{"role": "user", "content": "cancel"}],
        system="system",
        kind="decision",
        turn_id="T-cost",
    )
    assert next(cancelled).text == "hello"
    cast("Any", cancelled).close()

    error_client = _openai_client("error")
    with pytest.raises(RuntimeError, match="provider exploded"):
        recorder.chat(
            error_client,
            messages=[{"role": "user", "content": "error"}],
            system="system",
            kind="decision",
            turn_id="T-cost",
        )

    stream_error_client = _openai_client("stream_error")
    with pytest.raises(RuntimeError, match="stream provider exploded"):
        _stream_all(recorder, stream_error_client)

    usage_client = _openai_client("usage_only")
    usage_chunks = _stream_all(recorder, usage_client)
    assert usage_chunks[-1].input_tokens == 17
    assert usage_chunks[-1].output_tokens == 5
    assert usage_chunks[-1].usage_status == "provider_final"

    anthropic_client = _anthropic_client()
    anthropic_chunks = _stream_all(recorder, anthropic_client)
    assert anthropic_chunks[-1].input_tokens == 23
    assert anthropic_chunks[-1].output_tokens == 7
    assert anthropic_chunks[-1].usage_status == "provider_final"

    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'cost.recorded' ORDER BY id",
    ).fetchall()
    payloads = [json.loads(str(row[0])) for row in rows]
    assert len(payloads) == 7
    assert len({payload["llm_request_id"] for payload in payloads}) == 7
    by_outcome = {payload["disposition"] for payload in payloads}
    assert by_outcome == {"completed", "cancelled", "error"}
    unavailable = [payload for payload in payloads if payload["usage_status"] == "unavailable"]
    assert unavailable
    assert all(
        "tokens_in" not in payload and "tokens_out" not in payload
        for payload in unavailable
    )
    usage_only = next(
        payload
        for payload in payloads
        if payload.get("provider_response_id") == "resp-usage_only"
    )
    assert usage_only["tokens_in"] == 17
    assert usage_only["tokens_out"] == 5

    before = len(payloads)
    existing = record_cost_disposition_once(
        conn,
        CostAccountingDisposition(
            llm_request_id=usage_client.last_llm_request_id or "",
            kind="decision",
            provider="openai",
            model="gpt-4",
            outcome="completed",
            usage_status="provider_final",
            input_tokens=17,
            output_tokens=5,
        ),
    )
    assert isinstance(existing, CostAlreadyRecorded)
    assert _event_count(conn, "cost.recorded") == before
    conn.close()


def test_stream_completion_accounting_failure_is_not_reclassified_as_provider_error(
    tmp_path: Path,
) -> None:
    """A failed completion commit propagates without a second disposition attempt."""
    conn = open_event_log(tmp_path / "stream-accounting-failure.db")
    attempts = 0

    def _fail_once(stage: str) -> None:
        nonlocal attempts
        if stage == "after_begin":
            attempts += 1
            if attempts == 1:
                message = "completion accounting failed"
                raise RuntimeError(message)

    recorder = CostRecorder(conn, failure_injector=cast("Any", _fail_once))
    with pytest.raises(RuntimeError, match="completion accounting failed"):
        _stream_all(recorder, _openai_client("stream"))

    assert attempts == 1
    assert _event_count(conn, "cost.recorded") == 0
    assert _table_count(conn, "cost_accounting_dispositions") == 0
    conn.close()


def test_cost_disposition_two_connection_race_preserves_first_committed_truth(
    tmp_path: Path,
) -> None:
    """Conflicting replays share one event and return the winner's disposition."""
    path = tmp_path / "cost-race.db"
    setup = open_event_log(path)
    ensure_cost_accounting_schema(setup)
    setup.close()
    barrier = threading.Barrier(3)
    bus = CommittedEventBus()
    published: list[str] = []
    publish_lock = threading.Lock()
    dispositions = (
        CostAccountingDisposition(
            llm_request_id="LLM-shared-race",
            kind="decision",
            provider="openai",
            model="gpt-race",
            outcome="completed",
            usage_status="provider_final",
            input_tokens=10,
            output_tokens=4,
        ),
        CostAccountingDisposition(
            llm_request_id="LLM-shared-race",
            kind="decision",
            provider="openai",
            model="gpt-race",
            outcome="error",
            usage_status="unavailable",
            error_code="ConflictingReplay",
        ),
    )

    def _publish(event: Event) -> None:
        with publish_lock:
            published.append(event.event_uid)

    def _worker(disposition: CostAccountingDisposition) -> CostRecorded | CostAlreadyRecorded:
        conn = _raw_connection(path)
        try:
            barrier.wait()
            return record_cost_disposition_once(
                conn,
                disposition,
                committed_event_bus=bus,
            )
        finally:
            conn.close()

    bus.subscribe(_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker, disposition) for disposition in dispositions]
        barrier.wait()
        outcomes = tuple(future.result(timeout=10) for future in futures)

    assert sum(isinstance(outcome, CostRecorded) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, CostAlreadyRecorded) for outcome in outcomes) == 1
    winner = next(outcome for outcome in outcomes if isinstance(outcome, CostRecorded))
    loser = next(outcome for outcome in outcomes if isinstance(outcome, CostAlreadyRecorded))
    assert loser.disposition == winner.disposition
    assert loser.event.event_uid == winner.event.event_uid
    assert published == [winner.event.event_uid]

    check = _raw_connection(path)
    assert _event_count(check, "cost.recorded") == 1
    assert _table_count(check, "cost_accounting_dispositions") == 1
    payload_row = check.execute(
        "SELECT payload_json FROM events WHERE type = 'cost.recorded'",
    ).fetchone()
    assert payload_row is not None
    assert json.loads(str(payload_row[0]))["disposition"] == winner.disposition.outcome
    check.close()


def test_cost_failure_injection_rolls_back_event_and_claim(tmp_path: Path) -> None:
    """A failed accounting transaction is absent and retryable."""
    conn = open_event_log(tmp_path / "cost-failure.db")
    ensure_cost_accounting_schema(conn)
    bus = CommittedEventBus()
    published: list[str] = []
    bus.subscribe(lambda event: published.append(event.event_uid))
    disposition = CostAccountingDisposition(
        llm_request_id="LLM-failure",
        kind="decision",
        provider="openai",
        model="gpt-test",
        outcome="error",
        usage_status="unavailable",
        error_code="Injected",
    )

    def _fail(stage: str) -> None:
        if stage == "after_claim_insert":
            message = "cost rollback"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="cost rollback"):
        record_cost_disposition_once(
            conn,
            disposition,
            committed_event_bus=bus,
            failure_injector=cast("Any", _fail),
        )
    assert _event_count(conn, "cost.recorded") == 0
    assert _table_count(conn, "cost_accounting_dispositions") == 0
    assert published == []
    conn.close()


def test_flag_on_decide_path_records_one_production_cost_disposition(
    tmp_path: Path,
) -> None:
    """The real decide orchestration uses the guard, not only its L2 primitive."""
    path = tmp_path / "flag-on-decide.db"
    conn = open_event_log(path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    client = _openai_client("batch")
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast(
            "Any",
            SimpleNamespace(event_log=path, artifacts_root=artifacts),
        ),
        tool_registry=cast("Any", build_default_registry()),
        lifecycle=cast("Any", ActionLifecycle()),
        llm_client=client,
        system_prompt="integration system",
        wave1_features=Wave1FeatureFlags(exactly_once_cost_accounting=True),
    )
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "hello", "turn_id": "T-wave1-wiring"},
        correlation={"turn_id": "T-wave1-wiring"},
    )

    result = decide(trigger, ctx)

    assert result.response_plan is not None
    assert result.response_plan.text == "batch"
    assert _event_count(conn, "cost.recorded") == 1
    assert _table_count(conn, "cost_accounting_dispositions") == 1
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'cost.recorded'",
    ).fetchone()
    assert row is not None
    payload = json.loads(str(row[0]))
    assert payload["kind"] == "decision"
    assert payload["disposition"] == "completed"
    assert payload["llm_request_id"] == client.last_llm_request_id
    conn.close()


def test_vision_cost_guard_flag_on_success_error_and_flag_off_compatibility(
    tmp_path: Path,
) -> None:
    """Composition-root vision calls share L3 accounting and redaction."""
    conn = open_event_log(tmp_path / "vision-cost.db")
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"not-a-real-png")
    guarded_recorder = _build_vision_cost_recorder(
        conn,
        Wave1FeatureFlags(exactly_once_cost_accounting=True),
        pricing_path=tmp_path / "missing-pricing.json",
    )
    assert guarded_recorder is not None

    guarded_success = _LLMVisionClient(
        _openai_client("batch"),
        cost_recorder=guarded_recorder,
    )
    assert guarded_success.describe_image(image_path, question="what?") == "batch"

    guarded_error = _LLMVisionClient(
        _openai_client("vision_error"),
        cost_recorder=guarded_recorder,
    )
    with pytest.raises(VisionCallError) as captured:
        guarded_error.describe_image(image_path, question=None)
    assert "[redacted-base64]" in str(captured.value)
    assert "base64," not in str(captured.value)

    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'cost.recorded' ORDER BY id",
    ).fetchall()
    payloads = [json.loads(str(row[0])) for row in rows]
    assert len(payloads) == 2
    assert {payload["kind"] for payload in payloads} == {"vision"}
    assert {payload["disposition"] for payload in payloads} == {"completed", "error"}
    assert len({payload["llm_request_id"] for payload in payloads}) == 2

    legacy_recorder = _build_vision_cost_recorder(
        conn,
        Wave1FeatureFlags(),
        pricing_path=tmp_path / "missing-pricing.json",
    )
    assert legacy_recorder is None
    legacy = _LLMVisionClient(_openai_client("batch"), cost_recorder=legacy_recorder)
    assert legacy.describe_image(image_path, question=None) == "batch"
    assert _event_count(conn, "cost.recorded") == 2
    assert _table_count(conn, "cost_accounting_dispositions") == 2
    conn.close()


def test_wave1_feature_flags_ship_enabled() -> None:
    """Shipped config opts into all four Wave-1 safety primitives (tier A)."""
    raw = yaml.safe_load(Path("config/jarvis.yaml").read_text(encoding="utf-8"))
    realtime = raw["realtime"]
    flags = Wave1FeatureFlags.from_mapping(realtime["concurrency_safety"])
    assert realtime["enabled"] is True
    assert flags.all_disabled is False
    assert flags.transactional_event_append
    assert flags.lifecycle_terminal_cas
    assert flags.confirmation_dispatch_outbox
    assert flags.exactly_once_cost_accounting
