"""Atomic confirmation consumption and authorized-dispatch debt (L2)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Literal

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


def ensure_authorized_dispatch_schema(conn: sqlite3.Connection) -> None:
    """Install the bounded operational claim/outbox tables idempotently."""
    if conn.in_transaction:
        msg = "authorized-dispatch schema setup requires an idle connection"
        raise AuthorizedDispatchTransactionStateError(msg)
    conn.execute(_CREATE_CONSUMPTION_TABLE_SQL)
    conn.execute(_CREATE_OUTBOX_TABLE_SQL)
    conn.commit()


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


def _load_dispatch_by_source(
    conn: sqlite3.Connection,
    source_confirmation_event_id: str,
) -> AuthorizedDispatch | None:
    row = conn.execute(
        "SELECT confirmation_id, authorization_id, lease_id, action_id, "
        "dispatch_id, gate_event_uid, tool_name, target_entity_ref, request_json "
        "FROM authorized_dispatch_outbox WHERE source_confirmation_event_id = ?",
        (source_confirmation_event_id,),
    ).fetchone()
    if row is None:
        return None
    (
        confirmation_id,
        authorization_id,
        lease_id,
        action_id,
        dispatch_id,
        gate_event_uid,
        tool_name,
        target_entity_ref,
        request_json,
    ) = row
    identity = stable_authorization_identity(source_confirmation_event_id)
    persisted_ids = (authorization_id, lease_id, action_id, dispatch_id)
    expected_ids = (
        identity.authorization_id,
        identity.lease_id,
        identity.action_id,
        identity.dispatch_id,
    )
    if persisted_ids != expected_ids:
        msg = "authorized-dispatch stable identity does not match accepted Event UID"
        raise AuthorizedDispatchError(msg)
    gate_event = get_event(conn, str(gate_event_uid))
    if gate_event is None:
        msg = f"outbox gate event {gate_event_uid!r} is missing"
        raise AuthorizedDispatchError(msg)
    request_payload_raw: Any = json.loads(str(request_json))
    if not isinstance(request_payload_raw, dict):
        msg = "outbox request_json is not an object"
        raise AuthorizedDispatchError(msg)
    return AuthorizedDispatch(
        identity=identity,
        confirmation_id=str(confirmation_id),
        gate_event=gate_event,
        tool_name=str(tool_name),
        target_entity_ref=(None if target_entity_ref is None else str(target_entity_ref)),
        request_payload=request_payload_raw,
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
    effective_now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    identity = stable_authorization_identity(source_confirmation_event_id)
    if gate_payload.get("gate") != "pre_action" or gate_payload.get("outcome") != "pass":
        msg = "only an L3 passing gate may create authorized dispatch debt"
        raise ConfirmationRevalidationError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _inject(failure_injector, "after_begin")
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
            source_event_id=gate_source_event_id or accepted_event.event_uid,
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
    "AuthorizedDispatchError",
    "AuthorizedDispatchTransactionStateError",
    "ConfirmationRevalidationError",
    "FailureInjector",
    "FailureStage",
    "authorize_confirmation_dispatch",
    "ensure_authorized_dispatch_schema",
    "get_authorized_dispatch",
]
