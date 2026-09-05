"""Single L2 owner for playback, response, action and confirmation terminal CAS writes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal

from jarvis.shared.realtime import (
    AlreadyTerminal,
    LifecycleOwner,
    StaleConfirmation,
    TerminalCommitted,
    TerminalOutcome,
)
from jarvis.state.event_log import append_event_in_transaction, get_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus

FailureStage = Literal[
    "after_begin",
    "after_terminal_check",
    "after_event_append",
    "before_commit",
]
FailureInjector = Callable[[FailureStage], None]

_PLAYBACK_TERMINALS: Final[frozenset[str]] = frozenset(
    {
        "surface.playback_completed",
        "surface.playback_interrupted",
        "surface.playback_failed",
    },
)
_RESPONSE_TERMINALS: Final[frozenset[str]] = frozenset(
    {"response.completed", "response.cancelled", "response.failed"},
)
_ACTION_TERMINALS: Final[frozenset[str]] = frozenset(
    {
        "action.result_observed",
        "action.failed",
        "action.timeout_assumed",
        "action.cancelled",
    },
)
_CONFIRMATION_TERMINALS: Final[frozenset[str]] = frozenset(
    {
        "confirmation.accepted",
        "confirmation.rejected",
        "confirmation.expired",
    },
)


class LifecycleTerminalError(RuntimeError):
    """Base failure for invalid lifecycle-terminal requests."""


class InvalidTerminalEventError(LifecycleTerminalError):
    """The event type is not a terminal owned by the selected lifecycle."""


class LifecycleTransactionStateError(LifecycleTerminalError):
    """The primitive cannot own ``BEGIN IMMEDIATE`` on this connection."""


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    if injector is not None:
        injector(stage)


def _existing_terminal(
    conn: sqlite3.Connection,
    *,
    owner: LifecycleOwner,
    payload: Mapping[str, object],
) -> Event | None:
    """Return the first canonical terminal for one lifecycle identity."""
    if owner == "playback":
        row = conn.execute(
            "SELECT event_uid FROM events WHERE type IN (?, ?, ?) "
            "AND json_extract(payload_json, '$.response_id') = ? "
            "AND json_extract(payload_json, '$.playback_generation_id') = ? "
            "ORDER BY id ASC LIMIT 1",
            (
                "surface.playback_completed",
                "surface.playback_interrupted",
                "surface.playback_failed",
                payload["response_id"],
                payload["playback_generation_id"],
            ),
        ).fetchone()
    elif owner == "response":
        row = conn.execute(
            "SELECT event_uid FROM events WHERE type IN (?, ?, ?) "
            "AND json_extract(payload_json, '$.response_id') = ? "
            "ORDER BY id ASC LIMIT 1",
            (
                "response.completed",
                "response.cancelled",
                "response.failed",
                payload["response_id"],
            ),
        ).fetchone()
    elif owner == "action":
        row = conn.execute(
            "SELECT event_uid FROM events WHERE type IN (?, ?, ?, ?) "
            "AND json_extract(payload_json, '$.action_id') = ? "
            "ORDER BY id ASC LIMIT 1",
            (
                "action.result_observed",
                "action.failed",
                "action.timeout_assumed",
                "action.cancelled",
                payload["action_id"],
            ),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT event_uid FROM events WHERE type IN (?, ?, ?) "
            "AND json_extract(payload_json, '$.confirmation_id') = ? "
            "ORDER BY id ASC LIMIT 1",
            (
                "confirmation.accepted",
                "confirmation.rejected",
                "confirmation.expired",
                payload["confirmation_id"],
            ),
        ).fetchone()
    if row is None:
        return None
    event = get_event(conn, str(row[0]))
    if event is None:  # pragma: no cover - same-transaction row cannot disappear
        msg = f"terminal event {row[0]!r} disappeared from append-only log"
        raise LifecycleTerminalError(msg)
    return event


def _terminalize[Precondition](  # noqa: PLR0913 - mirrors the canonical Event append shape
    conn: sqlite3.Connection,
    *,
    owner: LifecycleOwner,
    identity: str,
    terminal_types: frozenset[str],
    event_type: str,
    payload: Mapping[str, object],
    source_event_id: str | None,
    correlation: Mapping[str, str] | None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
    precondition: Callable[[sqlite3.Connection], Precondition | None] | None = None,
) -> TerminalOutcome | Precondition:
    """Append at most one canonical terminal for one lifecycle identity.

    ``precondition`` is ADR-0014 D14's expected-revision check, and the
    three pre-D14 siblings do not pass it: it runs INSIDE this function's
    ``BEGIN IMMEDIATE``, after the existing-terminal CAS, and whatever
    non-None value it returns is returned verbatim in place of a terminal
    while nothing is appended. Omitted, the type variable is unsolved and
    the return type collapses to :data:`TerminalOutcome`, so the three
    existing siblings keep their exact signature and behavior.
    """
    if event_type not in terminal_types:
        msg = f"{event_type!r} is not a {owner} terminal"
        raise InvalidTerminalEventError(msg)
    if conn.in_transaction:
        msg = "lifecycle terminal primitive requires transaction ownership"
        raise LifecycleTransactionStateError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _inject(failure_injector, "after_begin")
        existing = _existing_terminal(
            conn,
            owner=owner,
            payload=payload,
        )
        if existing is not None:
            conn.commit()
            return AlreadyTerminal(owner=owner, identity=identity, event=existing)
        if precondition is not None:
            refusal = precondition(conn)
            if refusal is not None:
                conn.commit()
                return refusal
        _inject(failure_injector, "after_terminal_check")
        event = append_event_in_transaction(
            conn,
            type=event_type,
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )
        _inject(failure_injector, "after_event_append")
        _inject(failure_injector, "before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if committed_event_bus is not None:
        committed_event_bus.publish(event)
    return TerminalCommitted(owner=owner, identity=identity, event=event)


def terminalize_playback(  # noqa: PLR0913 - explicit Event fields are intentional
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: Mapping[str, object],
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> TerminalOutcome:
    """Atomically append one playback terminal per response/generation."""
    session_id = payload.get("session_id")
    response_id = payload.get("response_id")
    generation = payload.get("playback_generation_id")
    if not isinstance(session_id, str) or not session_id:
        msg = "playback terminal requires non-empty session_id"
        raise LifecycleTerminalError(msg)
    if not isinstance(response_id, str) or not response_id:
        msg = "playback terminal requires non-empty response_id"
        raise LifecycleTerminalError(msg)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        msg = "playback terminal requires non-negative playback_generation_id"
        raise LifecycleTerminalError(msg)
    identity = f"{response_id}:{generation}"
    return _terminalize(
        conn,
        owner="playback",
        identity=identity,
        terminal_types=_PLAYBACK_TERMINALS,
        event_type=event_type,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
    )


def terminalize_response(  # noqa: PLR0913 - explicit Event fields are intentional
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: Mapping[str, object],
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> TerminalOutcome:
    """Atomically append one L3 terminal per ``response_id``."""
    response_id = payload.get("response_id")
    if not isinstance(response_id, str) or not response_id:
        msg = "response terminal requires non-empty response_id"
        raise LifecycleTerminalError(msg)
    return _terminalize(
        conn,
        owner="response",
        identity=response_id,
        terminal_types=_RESPONSE_TERMINALS,
        event_type=event_type,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
    )


def terminalize_action(  # noqa: PLR0913 - explicit Event fields are intentional
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: Mapping[str, object],
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> TerminalOutcome:
    """Atomically append one canonical L4 terminal per ``action_id``."""
    action_id = payload.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        msg = "action terminal requires non-empty action_id"
        raise LifecycleTerminalError(msg)
    return _terminalize(
        conn,
        owner="action",
        identity=action_id,
        terminal_types=_ACTION_TERMINALS,
        event_type=event_type,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
    )


def _newest_requested_revision(conn: sqlite3.Connection) -> int | None:
    """Return ``events.id`` of the newest ``confirmation.requested`` row."""
    row = conn.execute(
        "SELECT id FROM events WHERE type = 'confirmation.requested' "
        "ORDER BY id DESC LIMIT 1",
    ).fetchone()
    return None if row is None else int(row[0])


def terminalize_confirmation(  # noqa: PLR0913 - explicit Event fields are intentional
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: Mapping[str, object],
    expected_revision: int,
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> TerminalOutcome | StaleConfirmation:
    """Atomically append one confirmation terminal per ``confirmation_id``.

    ADR-0014 D14's terminalizer. Beyond its three siblings it evaluates one
    precondition inside the same transaction: the newest
    ``confirmation.requested`` row must still be the one the caller folded.
    That is :func:`jarvis.state.projections._fold_pending_confirmations`'s
    single-slot rule — a new ``requested`` unconditionally replaces the slot
    — checked at CAS time, so a sweep whose fold has been overtaken by a
    fresh ask appends nothing and reports :class:`StaleConfirmation`.
    """
    confirmation_id = payload.get("confirmation_id")
    if not isinstance(confirmation_id, str) or not confirmation_id:
        msg = "confirmation terminal requires non-empty confirmation_id"
        raise LifecycleTerminalError(msg)

    def _still_newest(open_conn: sqlite3.Connection) -> StaleConfirmation | None:
        actual = _newest_requested_revision(open_conn)
        if actual == expected_revision:
            return None
        return StaleConfirmation(
            confirmation_id=confirmation_id,
            expected_revision=expected_revision,
            actual_revision=actual,
        )

    return _terminalize(
        conn,
        owner="confirmation",
        identity=confirmation_id,
        terminal_types=_CONFIRMATION_TERMINALS,
        event_type=event_type,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
        committed_event_bus=committed_event_bus,
        failure_injector=failure_injector,
        precondition=_still_newest,
    )


__all__ = [
    "FailureInjector",
    "FailureStage",
    "InvalidTerminalEventError",
    "LifecycleTerminalError",
    "LifecycleTransactionStateError",
    "terminalize_action",
    "terminalize_confirmation",
    "terminalize_playback",
    "terminalize_response",
]
