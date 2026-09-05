"""L2 exactly-once cost-disposition claim and Event Log append."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal, cast

from jarvis.shared.realtime import (
    CostAccountingDisposition,
    CostAccountingOutcome,
    CostAlreadyRecorded,
    CostRecorded,
    LLMRequestOutcome,
    LLMUsageStatus,
)
from jarvis.state.event_log import append_event_in_transaction, get_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus

FailureStage = Literal[
    "after_begin",
    "after_existing_check",
    "after_event_append",
    "after_claim_insert",
    "before_commit",
]
FailureInjector = Callable[[FailureStage], None]

_CREATE_COST_DISPOSITIONS_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS cost_accounting_dispositions (
    llm_request_id TEXT PRIMARY KEY,
    event_uid TEXT NOT NULL UNIQUE,
    disposition_json TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL
)
"""


class CostAccountingError(RuntimeError):
    """Base class for L2 accounting transaction failures."""


class CostAccountingTransactionStateError(CostAccountingError):
    """The primitive cannot own ``BEGIN IMMEDIATE`` on this connection."""


def ensure_cost_accounting_schema(conn: sqlite3.Connection) -> None:
    """Install the bounded idempotency table without changing Event truth."""
    if conn.in_transaction:
        msg = "cost-accounting schema setup requires an idle connection"
        raise CostAccountingTransactionStateError(msg)
    conn.execute(_CREATE_COST_DISPOSITIONS_SQL)
    conn.commit()


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    if injector is not None:
        injector(stage)


def _disposition_dict(disposition: CostAccountingDisposition) -> dict[str, object]:
    return {
        "llm_request_id": disposition.llm_request_id,
        "kind": disposition.kind,
        "provider": disposition.provider,
        "model": disposition.model,
        "outcome": disposition.outcome,
        "usage_status": disposition.usage_status,
        "provider_response_id": disposition.provider_response_id,
        "input_tokens": disposition.input_tokens,
        "output_tokens": disposition.output_tokens,
        "cache_read_tokens": disposition.cache_read_tokens,
        "cache_write_tokens": disposition.cache_write_tokens,
        "cost_usd": disposition.cost_usd,
        "error_code": disposition.error_code,
    }


def _deserialize_disposition(raw: str) -> CostAccountingDisposition:
    value = json.loads(raw)
    if not isinstance(value, dict):
        msg = "cost disposition record is not a JSON object"
        raise CostAccountingError(msg)
    outcome = cast("LLMRequestOutcome", str(value["outcome"]))
    usage_status = cast("LLMUsageStatus", str(value["usage_status"]))
    return CostAccountingDisposition(
        llm_request_id=str(value["llm_request_id"]),
        kind=str(value["kind"]),
        provider=str(value["provider"]),
        model=str(value["model"]),
        outcome=outcome,
        usage_status=usage_status,
        provider_response_id=_optional_str(value.get("provider_response_id")),
        input_tokens=_optional_int(value.get("input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        cache_read_tokens=_optional_int(value.get("cache_read_tokens")),
        cache_write_tokens=_optional_int(value.get("cache_write_tokens")),
        cost_usd=_optional_float(value.get("cost_usd")),
        error_code=_optional_str(value.get("error_code")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _load_existing(
    conn: sqlite3.Connection,
    llm_request_id: str,
) -> CostAlreadyRecorded | None:
    row = conn.execute(
        "SELECT event_uid, disposition_json FROM cost_accounting_dispositions "
        "WHERE llm_request_id = ?",
        (llm_request_id,),
    ).fetchone()
    if row is None:
        return None
    event = get_event(conn, str(row[0]))
    if event is None:
        msg = f"cost disposition event {row[0]!r} is missing"
        raise CostAccountingError(msg)
    return CostAlreadyRecorded(
        disposition=_deserialize_disposition(str(row[1])),
        event=event,
    )


def _event_payload(disposition: CostAccountingDisposition) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": disposition.kind,
        "model": disposition.model,
        "llm_request_id": disposition.llm_request_id,
        "provider": disposition.provider,
        "usage_status": disposition.usage_status,
        "disposition": disposition.outcome,
        "cost_usd": disposition.cost_usd,
    }
    optional_values: tuple[tuple[str, object | None], ...] = (
        ("provider_response_id", disposition.provider_response_id),
        ("tokens_in", disposition.input_tokens),
        ("tokens_out", disposition.output_tokens),
        ("cache_read_in", disposition.cache_read_tokens),
        ("cache_write_in", disposition.cache_write_tokens),
        ("error_code", disposition.error_code),
    )
    payload.update({key: value for key, value in optional_values if value is not None})
    return payload


def record_cost_disposition_once(
    conn: sqlite3.Connection,
    disposition: CostAccountingDisposition,
    *,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> CostAccountingOutcome:
    """Commit one request's accounting disposition or return its replay."""
    if not disposition.llm_request_id:
        msg = "cost disposition requires a stable llm_request_id"
        raise CostAccountingError(msg)
    if conn.in_transaction:
        msg = "cost-accounting primitive requires transaction ownership"
        raise CostAccountingTransactionStateError(msg)
    ensure_cost_accounting_schema(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _inject(failure_injector, "after_begin")
        existing = _load_existing(conn, disposition.llm_request_id)
        if existing is not None:
            conn.commit()
            return existing
        _inject(failure_injector, "after_existing_check")

        event = append_event_in_transaction(
            conn,
            type="cost.recorded",
            payload=_event_payload(disposition),
            correlation=correlation,
        )
        _inject(failure_injector, "after_event_append")
        disposition_json = json.dumps(
            _disposition_dict(disposition),
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO cost_accounting_dispositions ("
            "llm_request_id, event_uid, disposition_json, recorded_at_ms"
            ") VALUES (?, ?, ?, ?)",
            (
                disposition.llm_request_id,
                event.event_uid,
                disposition_json,
                int(time.time() * 1000),
            ),
        )
        _inject(failure_injector, "after_claim_insert")
        _inject(failure_injector, "before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if committed_event_bus is not None:
        committed_event_bus.publish(event)
    return CostRecorded(disposition=disposition, event=event)


__all__ = [
    "CostAccountingError",
    "CostAccountingTransactionStateError",
    "FailureInjector",
    "FailureStage",
    "ensure_cost_accounting_schema",
    "record_cost_disposition_once",
]


def record_run_cost_once(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    payload: Mapping[str, object],
    correlation: Mapping[str, str] | None = None,
) -> Event | None:
    """Atomically append a worker run's cost, honoring historical cost rows."""
    if conn.in_transaction:
        message = "run cost requires transaction ownership"
        raise CostAccountingTransactionStateError(message)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT event_uid FROM events WHERE type = 'cost.recorded' "
            "AND json_extract(payload_json, '$.run_id') = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return None
        event = append_event_in_transaction(
            conn, type="cost.recorded", payload=payload, correlation=correlation,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return event
