"""Atomic confirmation consumption and authorized-dispatch debt (L2)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from jarvis.shared import CallerPrincipal
from jarvis.shared.realtime import (
    AlreadyConsumed,
    AuthorizedDispatch,
    ConfirmationConsumptionOutcome,
    stable_authorization_identity,
)
from jarvis.state.event_log import (
    append_event_in_transaction,
    get_event,
    iter_events_of_types,
)
from jarvis.state.projections import PendingConfirmations

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import ActionRequest, AuthorizationLease, Event
    from jarvis.state.committed_event_bus import CommittedEventBus

FailureStage = Literal[
    "after_begin",
    "after_revalidation",
    "after_gate_append",
    "after_consumption_claim",
    "after_outbox_insert",
    "before_commit",
]
FailureInjector = Callable[[FailureStage], None]

_CREATE_CONSUMPTION_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS confirmation_consumption_claims (
    source_confirmation_event_id TEXT PRIMARY KEY,
    confirmation_id TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    lease_id TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL UNIQUE,
    gate_event_uid TEXT NOT NULL UNIQUE,
    claimed_at_ms INTEGER NOT NULL
)
"""

_CREATE_OUTBOX_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS authorized_dispatch_outbox (
    gate_event_uid TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE,
    source_confirmation_event_id TEXT NOT NULL UNIQUE,
    confirmation_id TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    lease_id TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    target_entity_ref TEXT,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at_ms INTEGER NOT NULL
)
"""


class AuthorizedDispatchError(RuntimeError):
    """Base class for confirmation-consumption transaction failures."""


class ConfirmationRevalidationError(AuthorizedDispatchError):
    """The accepted confirmation, lease, slot, or frozen scope is invalid."""


class AuthorizedDispatchTransactionStateError(AuthorizedDispatchError):
    """The primitive cannot own its required ``BEGIN IMMEDIATE``."""


class AuthorizedDispatchCorruptionError(AuthorizedDispatchError):
    """Persisted dispatch debt failed recovery integrity validation."""


class AuthorizedDispatchAlreadyStarted(AuthorizedDispatchError):  # noqa: N818 - admission outcome
    """The durable action admission already won; never repeat its effect."""


def answer_confirmation_once(  # noqa: PLR0913 - atomic validation and commit
    conn: sqlite3.Connection,
    *,
    confirmation_id: str,
    accepted: bool,
    utterance_raw: str,
    grammar_rule_id: str,
    correlation: Mapping[str, str] | None = None,
) -> Event:
    """Linearize a pending slot's answer and reuse its canonical Event UID.

    Re-read under the write lock: two stale L3 packets cannot create distinct
    acceptance identities, answer a superseded slot or override a rejection.
    """
    if conn.in_transaction:
        message = "answer requires an idle connection"
        raise AuthorizedDispatchTransactionStateError(message)
    conn.execute("BEGIN IMMEDIATE")
    try:
        events = tuple(iter_events_of_types(conn, (
            "confirmation.requested", "confirmation.accepted",
            "confirmation.rejected", "gate.evaluated",
        )))
        slot = PendingConfirmations.from_events(events).slot
        if slot is None or slot.confirmation_id != confirmation_id:
            message = "confirmation was superseded"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        if accepted and slot.accepted_event_uid is not None and slot.state != "rejected":
            existing = get_event(conn, slot.accepted_event_uid)
            if existing is None:
                message = "canonical acceptance disappeared"
                raise AuthorizedDispatchCorruptionError(message)  # noqa: TRY301 - atomic transaction owns rollback
            conn.commit()
            return existing
        if not slot.is_live(int(time.time() * 1000)):
            message = "confirmation is no longer pending"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        requested = next(event for event in reversed(events) if (
            event.type == "confirmation.requested"
            and event.payload.get("confirmation_id") == confirmation_id
        ))
        event = append_event_in_transaction(
            conn,
            type="confirmation.accepted" if accepted else "confirmation.rejected",
            payload={"confirmation_id": confirmation_id, "utterance_raw": utterance_raw,
                     "grammar_rule_id": grammar_rule_id},
            source_event_id=requested.event_uid,
            correlation=correlation,
            event_uid=uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:answer:{requested.event_uid}").hex,
        )
        conn.commit()
        return event  # noqa: TRY300 - atomic transaction owns rollback
    except BaseException:
        conn.rollback()
        raise


def admit_authorized_dispatch(  # noqa: C901, PLR0915 - atomic fail-closed validation
    conn: sqlite3.Connection,
    action_request: ActionRequest,
    *,
    payload: Mapping[str, object],
    correlation: Mapping[str, str] | None = None,
) -> Event:
    """Consume one outbox row and append its L4 admission in one transaction.

    A crash before commit leaves retryable pending debt. After commit the
    effect may have started: preserve ``dispatched`` and never blindly replay
    it, including when no terminal event survived the crash.
    """
    lease = action_request.authorization_lease
    if lease is None:
        message = "authorized dispatch requires a lease"
        raise ConfirmationRevalidationError(message)
    if (
        lease["granted_by"] != "allen"
        or lease["granted_to"] != action_request.caller_principal
        or action_request.caller_principal != CallerPrincipal.JARVIS_LLM
        or action_request.tool_name not in lease["allowed_tools"]
        or action_request.target_entity_ref not in lease["allowed_targets"]
        or lease["max_uses"] != 1
    ):
        message = "dispatch lease expired or does not permit this action"
        raise ConfirmationRevalidationError(message)
    if conn.in_transaction:
        message = "dispatch requires an idle connection"
        raise AuthorizedDispatchTransactionStateError(message)
    ensure_authorized_dispatch_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if int(time.time() * 1000) >= lease["expires_at_ms"]:
            message = "dispatch lease expired while awaiting admission"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301
        dispatch = _load_dispatch_by_source(conn, lease["source_confirmation_event_id"])
        if dispatch is None:
            message = "confirmation has no authorized dispatch debt"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        identity = dispatch.identity
        if action_request.action_id != identity.action_id or lease["lease_id"] != identity.lease_id:
            message = "dispatch identity differs from authorization"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        accepted = get_event(conn, identity.source_confirmation_event_id)
        requested = None if accepted is None or accepted.source_event_id is None else get_event(
            conn, accepted.source_event_id,
        )
        if requested is None:
            message = "confirmation request disappeared"
            raise AuthorizedDispatchCorruptionError(message)  # noqa: TRY301 - atomic transaction owns rollback
        snapshot = cast("Mapping[str, object]", requested.payload["action_snapshot"])
        actual = _canonical_request_payload(
            action_request, stable_action_id=identity.action_id, frozen_snapshot=snapshot,
        )
        if actual != dispatch.request_payload:
            message = "dispatch request differs from authorized request"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        metadata = cast("Mapping[str, object]", snapshot["args_meta"])
        content = action_request.arguments.get("content")
        if not isinstance(content, str) or (
            hashlib.sha256(content.encode("utf-8")).hexdigest() != metadata["content_sha256"]
            or len(content.encode("utf-8")) != metadata["content_bytes"]
        ):
            message = "dispatch content differs from authorized content"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        expected_arguments = {key: value for key, value in metadata.items() if key not in (
            "content_sha256", "content_bytes", "content_artifact",
        )}
        expected_arguments["content"] = content
        if dict(action_request.arguments) != expected_arguments:
            message = "dispatch arguments differ from authorization"
            raise ConfirmationRevalidationError(message)  # noqa: TRY301 - atomic transaction owns rollback
        cursor = conn.execute(
            "UPDATE authorized_dispatch_outbox SET state = 'dispatched' "
            "WHERE dispatch_id = ? AND state = 'pending'", (identity.dispatch_id,),
        )
        if cursor.rowcount != 1:
            message = "authorized action was already admitted"
            raise AuthorizedDispatchAlreadyStarted(message)  # noqa: TRY301 - atomic transaction owns rollback
        event = append_event_in_transaction(
            conn, type="action.dispatched", payload=payload,
            source_event_id=dispatch.gate_event.event_uid, correlation=correlation,
        )
        conn.commit()
        return event  # noqa: TRY300 - atomic transaction owns rollback
    except BaseException:
        conn.rollback()
        raise


def ensure_authorized_dispatch_schema(conn: sqlite3.Connection) -> None:
    """Install the bounded operational claim/outbox tables idempotently."""
    if conn.in_transaction:
        msg = "authorized-dispatch schema setup requires an idle connection"
        raise AuthorizedDispatchTransactionStateError(msg)
    conn.execute(_CREATE_CONSUMPTION_TABLE_SQL)
    conn.execute(_CREATE_OUTBOX_TABLE_SQL)
    conn.commit()


def authorized_dispatch_schema_matches(conn: sqlite3.Connection) -> bool:
    """Recognize the current owner-defined schema without issuing any DDL.

    Unknown or partial definitions cannot establish absence of operational debt.
    Equivalent but unrecognized custom schemas require explicit migration too.
    """
    expected = {
        "confirmation_consumption_claims": _CREATE_CONSUMPTION_TABLE_SQL,
        "authorized_dispatch_outbox": _CREATE_OUTBOX_TABLE_SQL,
    }
    actual = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN "
        "('confirmation_consumption_claims', 'authorized_dispatch_outbox')",
    ))

    def normalized(sql: str) -> str:
        return " ".join(sql.casefold().split()).replace(" if not exists", "")

    return all(
        isinstance(actual.get(name), str) and normalized(actual[name]) == normalized(sql)
        for name, sql in expected.items()
    )


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    if injector is not None:
        injector(stage)


def _canonical_request_payload(
    action_request: ActionRequest,
    *,
    stable_action_id: str,
    frozen_snapshot: Mapping[str, object],
) -> dict[str, object]:
    caller = action_request.caller_principal
    caller_value = getattr(caller, "value", caller)
    args_meta_raw = frozen_snapshot["args_meta"]
    if not isinstance(args_meta_raw, dict):  # pragma: no cover - revalidated first
        msg = "frozen confirmation args_meta disappeared"
        raise ConfirmationRevalidationError(msg)
    metadata_keys = {"content_sha256", "content_bytes", "content_artifact"}
    bounded_arguments = {
        key: value for key, value in args_meta_raw.items() if key not in metadata_keys
    }
    bounded_arguments["content_ref"] = {
        "artifact": args_meta_raw.get("content_artifact"),
        "sha256": args_meta_raw.get("content_sha256"),
        "bytes": args_meta_raw.get("content_bytes"),
    }
    return {
        "action_id": stable_action_id,
        "tool_name": action_request.tool_name,
        "target_entity_ref": action_request.target_entity_ref,
        "caller_principal": str(caller_value),
        "risk_level": action_request.risk_level,
        "arguments": bounded_arguments,
        "run_id": action_request.run_id,
        "turn_id": action_request.turn_id,
        "payload": None if action_request.payload is None else dict(action_request.payload),
    }


def _request_json(payload: Mapping[str, object]) -> tuple[str, str]:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        msg = "authorized ActionRequest must be bounded canonical JSON"
        raise ConfirmationRevalidationError(msg) from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _PersistedDispatchRow:
    confirmation_id: str
    authorization_id: str
    lease_id: str
    action_id: str
    dispatch_id: str
    gate_event_uid: str
    tool_name: str
    target_entity_ref: str | None
    request_json: str
    request_hash: str


def _fetch_dispatch_row(
    conn: sqlite3.Connection,
    source_confirmation_event_id: str,
) -> _PersistedDispatchRow | None:
    raw = conn.execute(
        "SELECT confirmation_id, authorization_id, lease_id, action_id, "
        "dispatch_id, gate_event_uid, tool_name, target_entity_ref, request_json, "
        "request_hash FROM authorized_dispatch_outbox "
        "WHERE source_confirmation_event_id = ?",
        (source_confirmation_event_id,),
    ).fetchone()
    if raw is None:
        return None
    required_strings = (*raw[:7], *raw[8:10])
    if not all(isinstance(value, str) and value for value in required_strings):
        msg = "authorized-dispatch row contains malformed string fields"
        raise AuthorizedDispatchCorruptionError(msg)
    if raw[7] is not None and not isinstance(raw[7], str):
        msg = "authorized-dispatch target_entity_ref is malformed"
        raise AuthorizedDispatchCorruptionError(msg)
    return _PersistedDispatchRow(
        confirmation_id=cast("str", raw[0]),
        authorization_id=cast("str", raw[1]),
        lease_id=cast("str", raw[2]),
        action_id=cast("str", raw[3]),
        dispatch_id=cast("str", raw[4]),
        gate_event_uid=cast("str", raw[5]),
        tool_name=cast("str", raw[6]),
        target_entity_ref=raw[7],
        request_json=cast("str", raw[8]),
        request_hash=cast("str", raw[9]),
    )


def _validate_stable_rows(
    conn: sqlite3.Connection,
    *,
    source_confirmation_event_id: str,
    row: _PersistedDispatchRow,
) -> None:
    identity = stable_authorization_identity(source_confirmation_event_id)
    persisted_ids = (
        row.authorization_id,
        row.lease_id,
        row.action_id,
        row.dispatch_id,
    )
    expected_ids = (
        identity.authorization_id,
        identity.lease_id,
        identity.action_id,
        identity.dispatch_id,
    )
    if persisted_ids != expected_ids:
        msg = "authorized-dispatch stable identity does not match accepted Event UID"
        raise AuthorizedDispatchCorruptionError(msg)
    claim = conn.execute(
        "SELECT confirmation_id, authorization_id, lease_id, action_id, "
        "dispatch_id, gate_event_uid FROM confirmation_consumption_claims "
        "WHERE source_confirmation_event_id = ?",
        (source_confirmation_event_id,),
    ).fetchone()
    expected_claim = (
        row.confirmation_id,
        row.authorization_id,
        row.lease_id,
        row.action_id,
        row.dispatch_id,
        row.gate_event_uid,
    )
    if claim is None or tuple(claim) != expected_claim:
        msg = "authorized-dispatch claim and outbox rows do not agree"
        raise AuthorizedDispatchCorruptionError(msg)


def _load_frozen_snapshot(
    conn: sqlite3.Connection,
    *,
    source_confirmation_event_id: str,
    confirmation_id: str,
) -> dict[str, object]:
    accepted_event = get_event(conn, source_confirmation_event_id)
    if accepted_event is None or accepted_event.type != "confirmation.accepted":
        msg = "authorized-dispatch source is not confirmation.accepted"
        raise AuthorizedDispatchCorruptionError(msg)
    if accepted_event.payload.get("confirmation_id") != confirmation_id:
        msg = "authorized-dispatch confirmation_id differs from its acceptance"
        raise AuthorizedDispatchCorruptionError(msg)
    requested_event = (
        None
        if accepted_event.source_event_id is None
        else get_event(conn, accepted_event.source_event_id)
    )
    if requested_event is None or requested_event.type != "confirmation.requested":
        msg = "authorized-dispatch acceptance has no confirmation request"
        raise AuthorizedDispatchCorruptionError(msg)
    if requested_event.payload.get("confirmation_id") != confirmation_id:
        msg = "authorized-dispatch confirmation_id differs from its request"
        raise AuthorizedDispatchCorruptionError(msg)
    snapshot = requested_event.payload.get("action_snapshot")
    if not isinstance(snapshot, dict):
        msg = "authorized-dispatch request has no frozen action snapshot"
        raise AuthorizedDispatchCorruptionError(msg)
    return snapshot


def _load_valid_gate(
    conn: sqlite3.Connection,
    *,
    source_confirmation_event_id: str,
    row: _PersistedDispatchRow,
) -> Event:
    gate_event = get_event(conn, row.gate_event_uid)
    if gate_event is None:
        msg = f"outbox gate event {row.gate_event_uid!r} is missing"
        raise AuthorizedDispatchCorruptionError(msg)
    if gate_event.type != "gate.evaluated":
        msg = "authorized-dispatch debt does not reference gate.evaluated"
        raise AuthorizedDispatchCorruptionError(msg)
    if gate_event.source_event_id != source_confirmation_event_id:
        msg = "authorized-dispatch gate is not sourced by its canonical acceptance"
        raise AuthorizedDispatchCorruptionError(msg)
    expected_payload = {
        "gate": "pre_action",
        "outcome": "pass",
        "action_id": row.action_id,
        "lease_id": row.lease_id,
        "authorization_id": row.authorization_id,
        "dispatch_id": row.dispatch_id,
    }
    if any(gate_event.payload.get(key) != value for key, value in expected_payload.items()):
        msg = "authorized-dispatch gate payload does not match stable outbox identity"
        raise AuthorizedDispatchCorruptionError(msg)
    return gate_event


def _load_valid_request(
    row: _PersistedDispatchRow,
    *,
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    recomputed_hash = hashlib.sha256(row.request_json.encode("utf-8")).hexdigest()
    if row.request_hash != recomputed_hash:
        msg = "authorized-dispatch request_hash does not match request_json"
        raise AuthorizedDispatchCorruptionError(msg)
    try:
        request_payload_raw: Any = json.loads(row.request_json)
    except json.JSONDecodeError as exc:
        msg = "outbox request_json is not valid JSON"
        raise AuthorizedDispatchCorruptionError(msg) from exc
    if not isinstance(request_payload_raw, dict):
        msg = "outbox request_json is not an object"
        raise AuthorizedDispatchCorruptionError(msg)
    canonical_json = json.dumps(request_payload_raw, sort_keys=True, separators=(",", ":"))
    if canonical_json != row.request_json:
        msg = "authorized-dispatch request_json is not canonical"
        raise AuthorizedDispatchCorruptionError(msg)
    row_bindings = {
        "action_id": row.action_id,
        "tool_name": row.tool_name,
        "target_entity_ref": row.target_entity_ref,
    }
    if any(request_payload_raw.get(key) != value for key, value in row_bindings.items()):
        msg = "authorized-dispatch request_json does not match its outbox row"
        raise AuthorizedDispatchCorruptionError(msg)

    args_meta = snapshot.get("args_meta")
    if not isinstance(args_meta, dict):
        msg = "authorized-dispatch frozen arguments are malformed"
        raise AuthorizedDispatchCorruptionError(msg)
    metadata_keys = {"content_sha256", "content_bytes", "content_artifact"}
    expected_arguments = {
        key: value for key, value in args_meta.items() if key not in metadata_keys
    }
    expected_arguments["content_ref"] = {
        "artifact": args_meta.get("content_artifact"),
        "sha256": args_meta.get("content_sha256"),
        "bytes": args_meta.get("content_bytes"),
    }
    snapshot_bindings = {
        "tool_name": snapshot.get("tool_name"),
        "target_entity_ref": snapshot.get("target_entity_ref"),
        "caller_principal": snapshot.get("caller"),
        "risk_level": snapshot.get("risk_level"),
        "arguments": expected_arguments,
        "payload": None,
    }
    if any(
        request_payload_raw.get(key) != value for key, value in snapshot_bindings.items()
    ):
        msg = "authorized-dispatch request_json differs from its frozen confirmation"
        raise AuthorizedDispatchCorruptionError(msg)
    return request_payload_raw


def _load_dispatch_by_source(
    conn: sqlite3.Connection,
    source_confirmation_event_id: str,
) -> AuthorizedDispatch | None:
    row = _fetch_dispatch_row(conn, source_confirmation_event_id)
    if row is None:
        return None
    identity = stable_authorization_identity(source_confirmation_event_id)
    _validate_stable_rows(
        conn,
        source_confirmation_event_id=source_confirmation_event_id,
        row=row,
    )
    snapshot = _load_frozen_snapshot(
        conn,
        source_confirmation_event_id=source_confirmation_event_id,
        confirmation_id=row.confirmation_id,
    )
    gate_event = _load_valid_gate(
        conn,
        source_confirmation_event_id=source_confirmation_event_id,
        row=row,
    )
    request_payload = _load_valid_request(row, snapshot=snapshot)
    return AuthorizedDispatch(
        identity=identity,
        confirmation_id=row.confirmation_id,
        gate_event=gate_event,
        tool_name=row.tool_name,
        target_entity_ref=row.target_entity_ref,
        request_payload=request_payload,
    )


def get_authorized_dispatch(
    conn: sqlite3.Connection,
    source_confirmation_event_id: str,
) -> AuthorizedDispatch | None:
    """Read one stable outbox debt by its canonical accepted Event UID."""
    return _load_dispatch_by_source(conn, source_confirmation_event_id)


def _revalidate_confirmation(  # noqa: C901, PLR0912, PLR0915 - fail-closed matrix
    conn: sqlite3.Connection,
    *,
    source_confirmation_event_id: str,
    action_request: ActionRequest,
    lease: AuthorizationLease,
    now_ms: int,
) -> tuple[str, Event, Mapping[str, object]]:
    accepted_event = get_event(conn, source_confirmation_event_id)
    if accepted_event is None or accepted_event.type != "confirmation.accepted":
        msg = "source_confirmation_event_id is not a canonical acceptance"
        raise ConfirmationRevalidationError(msg)
    requested_event = (
        None
        if accepted_event.source_event_id is None
        else get_event(conn, accepted_event.source_event_id)
    )
    if requested_event is None or requested_event.type != "confirmation.requested":
        msg = "accepted confirmation does not reference confirmation.requested"
        raise ConfirmationRevalidationError(msg)

    confirmation_id = accepted_event.payload.get("confirmation_id")
    if not isinstance(confirmation_id, str) or not confirmation_id:
        msg = "accepted confirmation has no canonical confirmation_id"
        raise ConfirmationRevalidationError(msg)
    if requested_event.payload.get("confirmation_id") != confirmation_id:
        msg = "accepted confirmation does not match its requested slot"
        raise ConfirmationRevalidationError(msg)

    projection = PendingConfirmations.from_events(
        iter_events_of_types(
            conn,
            (
                "confirmation.requested",
                "confirmation.accepted",
                "confirmation.rejected",
                "gate.evaluated",
            ),
        ),
    )
    slot = projection.slot
    if (
        slot is None
        or slot.confirmation_id != confirmation_id
        or slot.state != "accepted_unconsumed"
        or slot.accepted_event_uid != source_confirmation_event_id
    ):
        msg = "accepted confirmation is stale, superseded, rejected, or already consumed"
        raise ConfirmationRevalidationError(msg)
    if now_ms >= slot.expires_at_ms:
        msg = "confirmation slot expired before dispatch authorization"
        raise ConfirmationRevalidationError(msg)

    required_lease_fields = {
        "lease_id",
        "granted_by",
        "granted_to",
        "allowed_tools",
        "allowed_targets",
        "expires_at_ms",
        "max_uses",
        "reason",
        "source_confirmation_event_id",
    }
    if not required_lease_fields.issubset(lease):
        msg = "authorization lease is malformed"
        raise ConfirmationRevalidationError(msg)
    if lease["source_confirmation_event_id"] != source_confirmation_event_id:
        msg = "authorization lease names a different acceptance"
        raise ConfirmationRevalidationError(msg)
    max_uses = lease["max_uses"]
    if not isinstance(max_uses, int) or isinstance(max_uses, bool) or max_uses != 1:
        msg = "Wave 1 authorization lease requires max_uses=1"
        raise ConfirmationRevalidationError(msg)
    expires_at_ms = lease["expires_at_ms"]
    if (
        not isinstance(expires_at_ms, int)
        or isinstance(expires_at_ms, bool)
        or expires_at_ms <= now_ms
    ):
        msg = "authorization lease expired before claim"
        raise ConfirmationRevalidationError(msg)
    if lease["granted_by"] != "allen":
        msg = "authorization lease was not granted by Allen"
        raise ConfirmationRevalidationError(msg)
    if lease["reason"] != slot.template_line:
        msg = "authorization lease reason differs from the confirmed template"
        raise ConfirmationRevalidationError(msg)
    if lease["granted_to"] != action_request.caller_principal:
        msg = "authorization lease principal does not match ActionRequest"
        raise ConfirmationRevalidationError(msg)
    allowed_tools = lease["allowed_tools"]
    if not isinstance(allowed_tools, frozenset) or allowed_tools != frozenset(
        {action_request.tool_name},
    ):
        msg = "authorization lease does not permit the requested tool"
        raise ConfirmationRevalidationError(msg)
    target = action_request.target_entity_ref
    allowed_targets = lease["allowed_targets"]
    expected_targets = frozenset() if target is None else frozenset({target})
    if not isinstance(allowed_targets, frozenset) or allowed_targets != expected_targets:
        msg = "authorization lease does not permit the requested target"
        raise ConfirmationRevalidationError(msg)

    snapshot = slot.snapshot
    if snapshot.get("tool_name") != action_request.tool_name:
        msg = "ActionRequest tool differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    if snapshot.get("target_entity_ref") != target:
        msg = "ActionRequest target differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    caller = getattr(action_request.caller_principal, "value", action_request.caller_principal)
    if snapshot.get("caller") != caller:
        msg = "ActionRequest caller differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    if snapshot.get("risk_level") != action_request.risk_level:
        msg = "ActionRequest risk differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    args_meta = snapshot.get("args_meta")
    if not isinstance(args_meta, dict):
        msg = "frozen confirmation snapshot has malformed args_meta"
        raise ConfirmationRevalidationError(msg)
    content = action_request.arguments.get("content")
    expected_content_hash = args_meta.get("content_sha256")
    expected_content_bytes = args_meta.get("content_bytes")
    content_artifact = args_meta.get("content_artifact")
    if (
        not isinstance(content, str)
        or not isinstance(expected_content_hash, str)
        or not isinstance(expected_content_bytes, int)
        or isinstance(expected_content_bytes, bool)
        or not isinstance(content_artifact, str)
        or not content_artifact
    ):
        msg = "ActionRequest content cannot be verified against the frozen snapshot"
        raise ConfirmationRevalidationError(msg)
    content_bytes = content.encode("utf-8")
    if hashlib.sha256(content_bytes).hexdigest() != expected_content_hash:
        msg = "ActionRequest content hash differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    if expected_content_bytes != len(content_bytes):
        msg = "ActionRequest content length differs from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    metadata_keys = {"content_sha256", "content_bytes", "content_artifact"}
    expected_arguments = {
        key: value for key, value in args_meta.items() if key not in metadata_keys
    }
    expected_arguments["content"] = content
    if dict(action_request.arguments) != expected_arguments:
        msg = "ActionRequest arguments differ from the frozen confirmation snapshot"
        raise ConfirmationRevalidationError(msg)
    if action_request.payload is not None:
        msg = "confirmation-backed ActionRequest cannot carry unapproved payload"
        raise ConfirmationRevalidationError(msg)
    return confirmation_id, accepted_event, snapshot


def authorize_confirmation_dispatch(  # noqa: PLR0913 - transaction inputs mirror the ADR claim
    conn: sqlite3.Connection,
    *,
    source_confirmation_event_id: str,
    action_request: ActionRequest,
    lease: AuthorizationLease,
    gate_payload: Mapping[str, object],
    gate_source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    now_ms: int | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> ConfirmationConsumptionOutcome:
    """Claim one acceptance and commit its gate + outbox atomically.

    Candidate ``lease_id`` and ``action_id`` values are intentionally not
    trusted as identities.  The canonical accepted Event UID deterministically
    derives the authorization, lease, action, and dispatch IDs used in both
    durable records.  A duplicate runner therefore returns the same dispatch
    debt even when it arrived with different random candidates.
    """
    if conn.in_transaction:
        msg = "authorized-dispatch primitive requires transaction ownership"
        raise AuthorizedDispatchTransactionStateError(msg)
    ensure_authorized_dispatch_schema(conn)
    identity = stable_authorization_identity(source_confirmation_event_id)
    if gate_payload.get("gate") != "pre_action" or gate_payload.get("outcome") != "pass":
        msg = "only an L3 passing gate may create authorized dispatch debt"
        raise ConfirmationRevalidationError(msg)
    if (
        gate_source_event_id is not None
        and gate_source_event_id != source_confirmation_event_id
    ):
        msg = "passing gate source must be the canonical acceptance Event UID"
        raise ConfirmationRevalidationError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _inject(failure_injector, "after_begin")
        effective_now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        existing = _load_dispatch_by_source(conn, source_confirmation_event_id)
        if existing is not None:
            conn.commit()
            return AlreadyConsumed(dispatch=existing)

        confirmation_id, accepted_event, frozen_snapshot = _revalidate_confirmation(
            conn,
            source_confirmation_event_id=source_confirmation_event_id,
            action_request=action_request,
            lease=lease,
            now_ms=effective_now_ms,
        )
        _inject(failure_injector, "after_revalidation")

        request_payload = _canonical_request_payload(
            action_request,
            stable_action_id=identity.action_id,
            frozen_snapshot=frozen_snapshot,
        )
        request_json, request_hash = _request_json(request_payload)
        effective_gate_payload: dict[str, object] = dict(gate_payload)
        effective_gate_payload.update(
            {
                "action_id": identity.action_id,
                "lease_id": identity.lease_id,
                "authorization_id": identity.authorization_id,
                "dispatch_id": identity.dispatch_id,
            },
        )
        gate_event = append_event_in_transaction(
            conn,
            type="gate.evaluated",
            payload=effective_gate_payload,
            source_event_id=accepted_event.event_uid,
            correlation=correlation,
        )
        _inject(failure_injector, "after_gate_append")

        conn.execute(
            "INSERT INTO confirmation_consumption_claims ("
            "source_confirmation_event_id, confirmation_id, authorization_id, "
            "lease_id, action_id, dispatch_id, gate_event_uid, claimed_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_confirmation_event_id,
                confirmation_id,
                identity.authorization_id,
                identity.lease_id,
                identity.action_id,
                identity.dispatch_id,
                gate_event.event_uid,
                effective_now_ms,
            ),
        )
        _inject(failure_injector, "after_consumption_claim")
        conn.execute(
            "INSERT INTO authorized_dispatch_outbox ("
            "gate_event_uid, dispatch_id, source_confirmation_event_id, confirmation_id, "
            "authorization_id, lease_id, action_id, tool_name, "
            "target_entity_ref, request_json, request_hash, created_at_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gate_event.event_uid,
                identity.dispatch_id,
                source_confirmation_event_id,
                confirmation_id,
                identity.authorization_id,
                identity.lease_id,
                identity.action_id,
                action_request.tool_name,
                action_request.target_entity_ref,
                request_json,
                request_hash,
                effective_now_ms,
            ),
        )
        _inject(failure_injector, "after_outbox_insert")
        _inject(failure_injector, "before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if committed_event_bus is not None:
        committed_event_bus.publish(gate_event)
    return AuthorizedDispatch(
        identity=identity,
        confirmation_id=confirmation_id,
        gate_event=gate_event,
        tool_name=action_request.tool_name,
        target_entity_ref=action_request.target_entity_ref,
        request_payload=request_payload,
    )


__all__ = [
    "AuthorizedDispatchCorruptionError",
    "AuthorizedDispatchError",
    "AuthorizedDispatchTransactionStateError",
    "ConfirmationRevalidationError",
    "FailureInjector",
    "FailureStage",
    "authorize_confirmation_dispatch",
    "ensure_authorized_dispatch_schema",
    "get_authorized_dispatch",
]
