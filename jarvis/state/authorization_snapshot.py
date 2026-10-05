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


@dataclass(frozen=True)
class _Request:
    """What the snapshot keeps of one `confirmation.requested`, decoded once."""

    event_uid: str
    confirmation_id: str
    expires_at_ms: int | None
    turn_id: str | None
    frozen_snapshot_json: str
    malformed: bool


@dataclass(frozen=True)
class _Answer:
    event_uid: str
    accepted: bool
    turn_id: str | None


def _request_record(event: Event) -> _Request:
    payload = event.payload
    confirmation_id = payload.get("confirmation_id")
    snapshot = payload.get("action_snapshot")
    expiry = payload.get("expires_at_ms")
    malformed = (
        not isinstance(confirmation_id, str)
        or not confirmation_id
        or not isinstance(snapshot, dict)
        or not isinstance(payload.get("template_line"), str)
        or type(expiry) is not int
    )
    return _Request(
        event_uid=event.event_uid,
        confirmation_id=confirmation_id if isinstance(confirmation_id, str) else "unknown",
        expires_at_ms=expiry if type(expiry) is int else None,
        turn_id=_turn(event),
        frozen_snapshot_json=json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
        malformed=malformed,
    )


class AuthorizationFacts:
    """What the Event Log says about confirmations and dispatches, folded up to some event.

    The event-derived half of :class:`AuthorizationSnapshot`; the operational
    tables are the other half and are read afresh with each snapshot. Folding
    is incremental and a value is never mutated once shared (:meth:`advance`
    copies what it touches), so threads can hold and advance one value.

    An answer binds to a request that precedes it: `emit_event` refuses a
    `source_event_id` that is not already in the log, so a log has no other shape.
    """

    __slots__ = (
        "_accepted",
        "_answers",
        "_by_source",
        "_confirmation_ids",
        "_dispatched",
        "_errors",
        "_known",
        "_leased_gates",
        "_request_count",
        "_requests",
    )

    def __init__(self) -> None:
        """The facts of an empty log."""
        self._known: dict[str, object] = {}  # request uid -> its raw confirmation_id
        self._requests: tuple[_Request, ...] = ()  # the last _MAX_FACTS, oldest first
        self._request_count = 0
        self._answers: dict[str, _Answer] = {}  # request uid -> its first answer
        self._accepted: frozenset[str] = frozenset()  # uids of the accepted answers
        self._confirmation_ids: frozenset[str] = frozenset()
        self._errors: frozenset[str] = frozenset()
        self._dispatched: dict[str, tuple[int, str | None]] = {}  # action id -> count, first source
        self._by_source: dict[str, tuple[tuple[str, object], ...]] = {}  # gate uid -> (uid, action)
        self._leased_gates: tuple[Event, ...] = ()

    def advance(self, events: Sequence[Event]) -> AuthorizationFacts:
        """The facts after ``events``, which must follow this value's last event in log order."""
        if not events:
            return self
        twin = AuthorizationFacts()
        twin._known = dict(self._known)
        twin._answers = dict(self._answers)
        twin._dispatched = dict(self._dispatched)
        twin._by_source = dict(self._by_source)
        twin._requests = self._requests
        twin._request_count = self._request_count
        twin._accepted = self._accepted
        twin._confirmation_ids = self._confirmation_ids
        twin._errors = self._errors
        twin._leased_gates = self._leased_gates
        requests: list[_Request] = []
        errors: list[str] = []
        accepted: list[str] = []
        leased: list[Event] = []
        for event in events:
            if event.type == "confirmation.requested":
                requests.append(_request_record(event))
                twin._known[event.event_uid] = event.payload.get("confirmation_id")
            elif event.type in {"confirmation.accepted", "confirmation.rejected"}:
                twin._bind_answer(event, errors, accepted)
            elif event.type == "action.dispatched":
                twin._note_dispatch(event)
            elif (
                event.type == "gate.evaluated"
                and event.payload.get("gate") == "pre_action"
                and event.payload.get("outcome") == "pass"
                and event.payload.get("lease_id")
            ):
                leased.append(event)
        if requests:
            twin._requests = (*self._requests, *requests)[-_MAX_FACTS:]
            twin._request_count = self._request_count + len(requests)
            twin._confirmation_ids = self._confirmation_ids | {
                str(twin._known[r.event_uid]) for r in requests
            }
        twin._errors = self._errors | frozenset(errors)
        twin._accepted = self._accepted | frozenset(accepted)
        twin._leased_gates = (*self._leased_gates, *leased)
        return twin

    def _bind_answer(self, event: Event, errors: list[str], accepted: list[str]) -> None:
        source = event.source_event_id or ""
        if source not in self._known or self._known[source] != event.payload.get(
            "confirmation_id"
        ):
            errors.append("unbound_confirmation_answer:" + event.event_uid)
            return
        prior = self._answers.get(source)
        if prior is not None and prior.event_uid != event.event_uid:
            errors.append("conflicting_confirmation_answers:" + source)
            return
        is_accepted = event.type == "confirmation.accepted"
        self._answers[source] = _Answer(event.event_uid, is_accepted, _turn(event))
        if is_accepted:
            accepted.append(event.event_uid)

    def _note_dispatch(self, event: Event) -> None:
        action_id = event.payload.get("action_id")
        if isinstance(action_id, str):
            count, first = self._dispatched.get(action_id, (0, event.source_event_id))
            self._dispatched[action_id] = (count + 1, first)
        if event.source_event_id:
            self._by_source[event.source_event_id] = (
                *self._by_source.get(event.source_event_id, ()),
                (event.event_uid, action_id),
            )

    def _validate_dispatch_events(
        self,
        debts: Mapping[str, DispatchDebt],
        errors: list[str],
    ) -> None:
        by_gate = {debt.gate_event_uid: debt for debt in debts.values()}
        for gate_uid, debt in by_gate.items():
            errors.extend(
                "foreign_action_uses_authorized_gate:" + uid
                for uid, action_id in self._by_source.get(gate_uid, ())
                if action_id != debt.action_id
            )
        errors.extend(
            "unbacked_leased_gate:" + event.event_uid
            for event in self._leased_gates
            if not _validate_leased_gate(event, debts.get(event.source_event_id or ""))
        )
        for debt in debts.values():
            count, first = self._dispatched.get(debt.action_id, (0, None))
            if debt.state == "pending":
                if count:
                    errors.append("pending_outbox_already_dispatched:" + debt.dispatch_id)
            elif count != 1 or first != debt.gate_event_uid:
                errors.append("dispatched_outbox_admission_mismatch:" + debt.dispatch_id)


def _confirmation_fact(
    requested: _Request,
    *,
    answer: _Answer | None,
    dispatch: DispatchDebt | None,
    latest_request_uid: str,
    errors: list[str],
) -> ConfirmationFact:
    if requested.malformed:
        errors.append("malformed_confirmation_request:" + requested.event_uid)
    state: Literal["pending", "superseded", "rejected", "accepted_unconsumed", "consumed"]
    if answer is None:
        state = "pending" if requested.event_uid == latest_request_uid else "superseded"
    elif not answer.accepted:
        state = "rejected"
    else:
        state = "accepted_unconsumed" if dispatch is None else "consumed"
    return ConfirmationFact(
        confirmation_id=requested.confirmation_id,
        request_event_uid=requested.event_uid,
        answer_event_uid=None if answer is None else answer.event_uid,
        state=state,
        superseded=requested.event_uid != latest_request_uid,
        expires_at_ms=requested.expires_at_ms,
        request_turn_id=requested.turn_id,
        answer_turn_id=None if answer is None else answer.turn_id,
        frozen_snapshot_json=requested.frozen_snapshot_json,
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


def read_authorization_snapshot(
    conn: sqlite3.Connection,
    facts: AuthorizationFacts,
) -> AuthorizationSnapshot:
    """Read operational tables under the transaction that produced ``facts``."""
    if not conn.in_transaction:
        message = "authorization snapshot requires a pinned read transaction"
        raise ValueError(message)
    errors: list[str] = []
    try:
        debts, names, truncated = _read_dispatches(conn, errors)
    except sqlite3.DatabaseError:
        debts, names, truncated = {}, (), False
        errors.append("unreadable_authorization_schema")
    errors.extend(facts._errors)  # noqa: SLF001 - the event-derived half of this snapshot
    if not names and facts._accepted:  # noqa: SLF001
        errors.append("accepted_confirmation_without_authorization_schema")
    facts._validate_dispatch_events(debts, errors)  # noqa: SLF001
    if any(source not in facts._accepted for source in debts):  # noqa: SLF001
        errors.append("outbox_acceptance_outside_event_snapshot")
    if len(facts._confirmation_ids) != facts._request_count:  # noqa: SLF001
        errors.append("duplicate_confirmation_identity")
    latest_uid = facts._requests[-1].event_uid if facts._requests else ""  # noqa: SLF001
    # Omitted facts always make the view incomplete.
    records = tuple(
        _confirmation_fact(
            requested,
            answer=facts._answers.get(requested.event_uid),  # noqa: SLF001
            dispatch=debts.get(facts._answers[requested.event_uid].event_uid)  # noqa: SLF001
            if requested.event_uid in facts._answers  # noqa: SLF001
            else None,
            latest_request_uid=latest_uid,
            errors=errors,
        )
        for requested in facts._requests  # noqa: SLF001
    )
    return AuthorizationSnapshot(
        confirmations=records,
        errors=tuple(sorted(set(errors))),
        tables_present=names,
        truncated=truncated or facts._request_count > _MAX_FACTS,  # noqa: SLF001
    )
