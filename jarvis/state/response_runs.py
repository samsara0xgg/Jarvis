"""L2 primitive for opening a durable ResponseRun and folding open ones.

Per ADR-0008 §3 D1: terminal, confirmation-consumption, inbox/outbox and
receipt CAS primitives exclusively own ``BEGIN IMMEDIATE``/``COMMIT``/
``ROLLBACK``, call the no-commit append, and publish to
:class:`~jarvis.state.committed_event_bus.CommittedEventBus` only after the
outer commit succeeds.  ``response.started`` is the opening half of a
CAS-terminated lifecycle whose terminal is publishable in exactly that idiom,
so it belongs here rather than to ``emit_event`` — the compatibility wrapper
for one-event transactions, which commits and never publishes.

No duplicate-claim check is performed on start: ``response_id`` is a fresh
uuid4 per run, so a compare-and-set on the opening event would be dead code.
Exactly-once applies to the *terminal*, which
:mod:`jarvis.state.lifecycle_terminal` already owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import append_event_in_transaction, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.lifecycle_terminal import FailureInjector, FailureStage

_RESPONSE_STARTED: Final[str] = "response.started"
_RESPONSE_TERMINALS: Final[tuple[str, ...]] = (
    "response.completed",
    "response.cancelled",
    "response.failed",
)


class ResponseRunTransactionStateError(RuntimeError):
    """The primitive cannot own ``BEGIN IMMEDIATE`` on this connection."""


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    """Forward one named failure stage to an optional injector."""
    if injector is not None:
        injector(stage)


def append_response_started(  # noqa: PLR0913 — one keyword per Event column; spec §5.1 shape is fixed.
    conn: sqlite3.Connection,
    *,
    payload: Mapping[str, object],
    source_event_id: str,
    correlation: Mapping[str, str] | None = None,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> Event:
    """Append one ``response.started`` inside its own immediate transaction.

    Raises:
        ResponseRunTransactionStateError: The caller already owns a
            transaction on ``conn``; this primitive must own its own.
    """
    if conn.in_transaction:
        msg = "response-run start primitive requires transaction ownership"
        raise ResponseRunTransactionStateError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _inject(failure_injector, "after_begin")
        _inject(failure_injector, "after_terminal_check")
        event = append_event_in_transaction(
            conn,
            type=_RESPONSE_STARTED,
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
    return event


@dataclass(frozen=True)
class OpenResponseRun:
    """One ``response.started`` with no terminal, folded in append order."""

    response_id: str
    response_group_id: str
    turn_id: str
    started_event_uid: str


def open_response_runs(conn: sqlite3.Connection) -> tuple[OpenResponseRun, ...]:
    """Return every started-but-unterminated run in append order.

    Reads the four ``response.*`` types once and folds them: a start is open
    until some terminal names the same ``response_id``.  The boot reconciler
    uses this to close runs a crashed process abandoned.
    """
    started: dict[str, OpenResponseRun] = {}
    terminated: set[str] = set()
    for event in iter_events_of_types(conn, (_RESPONSE_STARTED, *_RESPONSE_TERMINALS)):
        response_id = event.payload.get("response_id")
        if not isinstance(response_id, str) or not response_id:
            continue
        if event.type == _RESPONSE_STARTED:
            started[response_id] = OpenResponseRun(
                response_id=response_id,
                response_group_id=str(event.payload.get("response_group_id", "")),
                turn_id=str(event.payload.get("turn_id", "")),
                started_event_uid=event.event_uid,
            )
        else:
            terminated.add(response_id)
    return tuple(
        run for response_id, run in started.items() if response_id not in terminated
    )


__all__ = [
    "OpenResponseRun",
    "ResponseRunTransactionStateError",
    "append_response_started",
    "open_response_runs",
]
