"""Read-only confirmation and dispatch facts for a transaction-pinned L3 packet.

This collector never installs schemas, consumes confirmation, admits dispatch,
expires permission or classifies semantic risk. An old superseded UI slot cannot
hide an accepted-but-unconsumed authorization from the snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jarvis.state.authorized_dispatch_outbox import (
    AuthorizedDispatchError,
    authorized_dispatch_schema_matches,
    get_authorized_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.shared import Event

_MAX_FACTS = 1024
_AUTHORIZATION_TABLE_COUNT = 2


@dataclass(frozen=True)
class DispatchDebt:
    """Integrity-checked durable dispatch identity plus its operational state."""

    accepted_event_uid: str
    confirmation_id: str
    action_id: str
    dispatch_id: str
    authorization_id: str
    lease_id: str
    gate_event_uid: str
    state: Literal["pending", "dispatched"]
    request_json: str


@dataclass(frozen=True)
class ConfirmationFact:
    """Every retained request/answer, independent of the current UI slot."""

    confirmation_id: str
    request_event_uid: str
    answer_event_uid: str | None
    state: Literal["pending", "superseded", "rejected", "accepted_unconsumed", "consumed"]
    superseded: bool
    expires_at_ms: int | None
    request_turn_id: str | None
    answer_turn_id: str | None
    frozen_snapshot_json: str
    dispatch: DispatchDebt | None


@dataclass(frozen=True)
class AuthorizationSnapshot:
    """Collected facts and explicit errors; absence is never a schema side effect."""

    confirmations: tuple[ConfirmationFact, ...] = ()
    errors: tuple[str, ...] = ()
    tables_present: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def complete(self) -> bool:
        """Collection/integrity completeness, not permission or resolved semantics."""
        return not self.errors and not self.truncated


def _turn(event: Event | None) -> str | None:
    if event is None:
        return None
    value = (event.correlation or {}).get("turn_id", event.payload.get("turn_id"))
    return value if isinstance(value, str) and value else None


def _read_dispatches(
    conn: sqlite3.Connection,
    errors: list[str],
) -> tuple[dict[str, DispatchDebt], tuple[str, ...], bool]:
    names = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('authorized_dispatch_outbox', 'confirmation_consumption_claims') ORDER BY name",
        )
    )
    if not names:
        return {}, (), False
    if len(names) != _AUTHORIZATION_TABLE_COUNT:
        errors.append("partial_authorization_schema")
        return {}, names, False
    if not authorized_dispatch_schema_matches(conn):
        errors.append("unsupported_authorization_schema")
        return {}, names, False
    rows = conn.execute(
        "SELECT source_confirmation_event_id, state FROM authorized_dispatch_outbox "
        "ORDER BY created_at_ms, gate_event_uid LIMIT ?",
        (_MAX_FACTS + 1,),
    ).fetchall()
    claims = tuple(
        conn.execute(
            "SELECT source_confirmation_event_id FROM confirmation_consumption_claims "
            "ORDER BY source_confirmation_event_id LIMIT ?",
            (_MAX_FACTS + 1,),
        )
    )
    truncated = len(rows) > _MAX_FACTS or len(claims) > _MAX_FACTS
    sources = {row[0] for row in rows}
    if any(claim[0] not in sources for claim in claims) and not truncated:
        errors.append("orphan_confirmation_consumption_claim")
    debts: dict[str, DispatchDebt] = {}
    for source, state in rows[:_MAX_FACTS]:
        if not isinstance(source, str) or not source or state not in {"pending", "dispatched"}:
            errors.append("malformed_outbox_source_or_state")
            continue
        try:
            dispatch = get_authorized_dispatch(conn, source)
            if dispatch is None:
                errors.append("outbox_disappeared_within_snapshot")
                continue
            identity = dispatch.identity
            debts[source] = DispatchDebt(
                accepted_event_uid=source,
                confirmation_id=dispatch.confirmation_id,
                action_id=identity.action_id,
                dispatch_id=identity.dispatch_id,
                authorization_id=identity.authorization_id,
                lease_id=identity.lease_id,
                gate_event_uid=dispatch.gate_event.event_uid,
                state="pending" if state == "pending" else "dispatched",
                request_json=json.dumps(
                    dispatch.request_payload, sort_keys=True, separators=(",", ":")
                ),
            )
        except (AuthorizedDispatchError, ValueError, TypeError, KeyError, UnicodeError):
            errors.append("outbox_integrity:" + source)
    return debts, names, truncated


def _answers(
    events: Sequence[Event],
    requests: Mapping[str, Event],
    errors: list[str],
) -> dict[str, Event]:
    answers: dict[str, Event] = {}
    for event in events:
        if event.type not in {"confirmation.accepted", "confirmation.rejected"}:
            continue
        requested = requests.get(event.source_event_id or "")
        if requested is None or requested.payload.get("confirmation_id") != event.payload.get(
            "confirmation_id"
        ):
            errors.append("unbound_confirmation_answer:" + event.event_uid)
            continue
        prior = answers.get(requested.event_uid)
        if prior is not None and prior.event_uid != event.event_uid:
            errors.append("conflicting_confirmation_answers:" + requested.event_uid)
        else:
            answers[requested.event_uid] = event
    return answers


def _confirmation_fact(
    requested: Event,
    *,
    answer: Event | None,
    dispatch: DispatchDebt | None,
    latest_request_uid: str,
    errors: list[str],
) -> ConfirmationFact:
    payload = requested.payload
    confirmation_id = payload.get("confirmation_id")
    snapshot = payload.get("action_snapshot")
    expiry = payload.get("expires_at_ms")
    if (
        not isinstance(confirmation_id, str)
        or not confirmation_id
        or not isinstance(snapshot, dict)
        or not isinstance(payload.get("template_line"), str)
        or type(expiry) is not int
    ):
        errors.append("malformed_confirmation_request:" + requested.event_uid)
    state: Literal["pending", "superseded", "rejected", "accepted_unconsumed", "consumed"]
    if answer is None:
        state = "pending" if requested.event_uid == latest_request_uid else "superseded"
    elif answer.type == "confirmation.rejected":
        state = "rejected"
    else:
        state = "accepted_unconsumed" if dispatch is None else "consumed"
    return ConfirmationFact(
        confirmation_id=confirmation_id if isinstance(confirmation_id, str) else "unknown",
        request_event_uid=requested.event_uid,
        answer_event_uid=None if answer is None else answer.event_uid,
        state=state,
        superseded=requested.event_uid != latest_request_uid,
        expires_at_ms=expiry if type(expiry) is int else None,
        request_turn_id=_turn(requested),
        answer_turn_id=_turn(answer),
        frozen_snapshot_json=json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
        dispatch=dispatch,
    )


def _validate_leased_gate(event: Event, debt: DispatchDebt | None) -> bool:
    if debt is None or event.event_uid != debt.gate_event_uid:
        return False
    expected = {
        "action_id": debt.action_id,
        "lease_id": debt.lease_id,
        "dispatch_id": debt.dispatch_id,
        "authorization_id": debt.authorization_id,
    }
    return all(event.payload.get(key) == value for key, value in expected.items())


def _validate_dispatch_events(
    debts: Mapping[str, DispatchDebt],
    events: Sequence[Event],
    errors: list[str],
) -> None:
    by_action: dict[str, list[Event]] = {}
    by_gate = {debt.gate_event_uid: debt for debt in debts.values()}
    for event in events:
        if event.type == "action.dispatched":
            action_id = event.payload.get("action_id")
            if isinstance(action_id, str):
                by_action.setdefault(action_id, []).append(event)
            source_debt = by_gate.get(event.source_event_id or "")
            if source_debt is not None and action_id != source_debt.action_id:
                errors.append("foreign_action_uses_authorized_gate:" + event.event_uid)
        if (
            event.type == "gate.evaluated"
            and event.payload.get("gate") == "pre_action"
            and event.payload.get("outcome") == "pass"
            and event.payload.get("lease_id")
            and not _validate_leased_gate(event, debts.get(event.source_event_id or ""))
        ):
            errors.append("unbacked_leased_gate:" + event.event_uid)
    for debt in debts.values():
        admissions = by_action.get(debt.action_id, [])
        if debt.state == "pending":
            if admissions:
                errors.append("pending_outbox_already_dispatched:" + debt.dispatch_id)
        elif len(admissions) != 1 or admissions[0].source_event_id != debt.gate_event_uid:
            errors.append("dispatched_outbox_admission_mismatch:" + debt.dispatch_id)


def read_authorization_snapshot(
    conn: sqlite3.Connection,
    events: Sequence[Event],
) -> AuthorizationSnapshot:
    """Read operational tables under the transaction that produced ``events``."""
    if not conn.in_transaction:
        message = "authorization snapshot requires a pinned read transaction"
        raise ValueError(message)
    errors: list[str] = []
    try:
        debts, names, truncated = _read_dispatches(conn, errors)
    except sqlite3.DatabaseError:
        debts, names, truncated = {}, (), False
        errors.append("unreadable_authorization_schema")
    requests = {
        event.event_uid: event for event in events if event.type == "confirmation.requested"
    }
    answers = _answers(events, requests, errors)
    if not names and any(answer.type == "confirmation.accepted" for answer in answers.values()):
        errors.append("accepted_confirmation_without_authorization_schema")
    _validate_dispatch_events(debts, events, errors)
    accepted_sources = {
        answer.event_uid for answer in answers.values() if answer.type == "confirmation.accepted"
    }
    if any(source not in accepted_sources for source in debts):
        errors.append("outbox_acceptance_outside_event_snapshot")
    confirmation_ids = [event.payload.get("confirmation_id") for event in requests.values()]
    if len({str(value) for value in confirmation_ids}) != len(confirmation_ids):
        errors.append("duplicate_confirmation_identity")
    latest_uid = next(reversed(requests), "")
    # Scan all answers before bounding. Omitted facts always make the view incomplete.
    records = tuple(
        _confirmation_fact(
            requested,
            answer=answers.get(requested.event_uid),
            dispatch=debts.get(answers[requested.event_uid].event_uid)
            if requested.event_uid in answers
            else None,
            latest_request_uid=latest_uid,
            errors=errors,
        )
        for requested in tuple(requests.values())[-_MAX_FACTS:]
    )
    return AuthorizationSnapshot(
        confirmations=records,
        errors=tuple(sorted(set(errors))),
        tables_present=names,
        truncated=truncated or len(requests) > _MAX_FACTS,
    )
