"""L2 durable input claim and consumer adoption watermark (ADR-0008 D8).

Before this module the daemon's user-intent watcher anchored its cursor at
``MAX(events.id)`` on every boot.  That shortcut has two costs D8 names
explicitly: an utterance that landed while the daemon was down is silently
dropped, and a crash between "watcher read the row" and "turn produced any
milestone" loses the turn with no durable trace that it was ever accepted.

Two primitives replace it:

* :func:`adopt_consumer` records **once** where a named consumer started
  reading.  Only rows after that watermark are eligible for recovery, so
  enabling the consumer on an old log never replays years of history (F24).
* :func:`claim_input_once` turns "I am going to drive this trigger" into a
  durable, idempotent ``turn.started`` keyed by the trigger's ``event_uid``.
  Two pumps, or one pump and a restart scan, resolve to the same claim; the
  loser dispatches nothing (F17).

The claim reuses the server-minted ``turn_id`` already required on the
canonical input payload — D8 is explicit that it "does not mint a second ID"
— and refuses a conflicting reuse of that id by a different trigger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import append_event_in_transaction, get_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence

    from jarvis.shared import Event

REALTIME_INTENT_CONSUMER: Final[str] = "realtime_intent"
"""The consumer name D8 gives the runtime's user-intent pump."""

_TURN_MILESTONE_TYPES: Final[tuple[str, ...]] = (
    "response.started",
    "action.proposed",
    "surface.response_emitted",
    "turn.ended",
    "turn.failed",
)
"""Rows that prove a claimed turn already got past "accepted".

D8 lets startup re-enqueue a "claimed turn with no response/action
milestone" and only that.  A turn that reached any of these produced work a
replay would duplicate, so it is left alone and — if it is genuinely stuck —
owned by the supervisor sweep instead.
"""


class InputClaimError(RuntimeError):
    """Base failure for the durable input-claim primitives."""


class ConflictingTurnClaimError(InputClaimError):
    """The trigger's ``turn_id`` is already claimed by a different trigger.

    D8: the claim "reuses the server-minted ``turn_id`` already required on
    the canonical input payload and rejects a conflicting reuse".  Silently
    accepting one would let two distinct utterances share a turn identity.
    """


class InputClaimTransactionStateError(InputClaimError):
    """The primitive cannot own ``BEGIN IMMEDIATE`` on this connection."""


@dataclass(frozen=True)
class InputClaimed:
    """This caller appended the trigger's one durable ``turn.started``."""

    turn_id: str
    trigger_event_uid: str
    event: Event


@dataclass(frozen=True)
class InputAlreadyClaimed:
    """Someone already claimed this trigger; nothing was appended."""

    turn_id: str
    trigger_event_uid: str
    event: Event


InputClaimOutcome = InputClaimed | InputAlreadyClaimed


@dataclass(frozen=True)
class ConsumerAdoption:
    """Where a named consumer began reading, and whether we just set it."""

    name: str
    adoption_row_id: int
    event: Event
    adopted_now: bool


def _require_transaction_ownership(conn: sqlite3.Connection) -> None:
    """Refuse to run when the caller already owns an open transaction."""
    if conn.in_transaction:
        msg = "input-claim primitive requires transaction ownership"
        raise InputClaimTransactionStateError(msg)


def _existing_claim_for_trigger(
    conn: sqlite3.Connection,
    trigger_event_uid: str,
) -> Event | None:
    """Return the ``turn.started`` already claiming this trigger, if any."""
    row = conn.execute(
        "SELECT event_uid FROM events WHERE type = 'turn.started' "
        "AND source_event_id = ? ORDER BY id ASC LIMIT 1",
        (trigger_event_uid,),
    ).fetchone()
    if row is None:
        return None
    return get_event(conn, str(row[0]))


def _existing_claim_for_turn(conn: sqlite3.Connection, turn_id: str) -> Event | None:
    """Return the ``turn.started`` already using this ``turn_id``, if any."""
    row = conn.execute(
        "SELECT event_uid FROM events WHERE type = 'turn.started' "
        "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id ASC LIMIT 1",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    return get_event(conn, str(row[0]))


def claim_input_once(
    conn: sqlite3.Connection,
    *,
    trigger_event: Event,
) -> InputClaimOutcome:
    """Idempotently claim one input trigger with a durable ``turn.started``.

    Args:
        conn: Event Log connection this call owns a transaction on.
        trigger_event: The committed ``surface.user_intent`` /
            ``utterance.received`` row being adopted.  Its
            ``payload["turn_id"]`` is the claim identity and its
            ``event_uid`` is the claim key.

    Returns:
        :class:`InputClaimed` when this call appended the claim,
        :class:`InputAlreadyClaimed` when one already existed.

    Raises:
        InputClaimError: The trigger carries no usable ``turn_id``.
        ConflictingTurnClaimError: That ``turn_id`` is already claimed by a
            different trigger.
    """
    raw_turn_id = trigger_event.payload.get("turn_id")
    if not isinstance(raw_turn_id, str) or not raw_turn_id:
        msg = (
            f"input trigger {trigger_event.event_uid!r} carries no string "
            "turn_id; it cannot be claimed"
        )
        raise InputClaimError(msg)
    _require_transaction_ownership(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _existing_claim_for_trigger(conn, trigger_event.event_uid)
        if existing is not None:
            conn.commit()
            return InputAlreadyClaimed(
                turn_id=raw_turn_id,
                trigger_event_uid=trigger_event.event_uid,
                event=existing,
            )
        conflicting = _existing_claim_for_turn(conn, raw_turn_id)
        if conflicting is not None:
            msg = (
                f"turn_id {raw_turn_id!r} is already claimed by trigger "
                f"{conflicting.source_event_id!r}; trigger "
                f"{trigger_event.event_uid!r} may not reuse it"
            )
            raise ConflictingTurnClaimError(msg)  # noqa: TRY301 — rollback below is the point.
        claim = append_event_in_transaction(
            conn,
            type="turn.started",
            payload={"turn_id": raw_turn_id, "trigger": trigger_event.event_uid},
            source_event_id=trigger_event.event_uid,
            correlation={"turn_id": raw_turn_id},
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return InputClaimed(
        turn_id=raw_turn_id,
        trigger_event_uid=trigger_event.event_uid,
        event=claim,
    )


def adopt_consumer(conn: sqlite3.Connection, *, name: str) -> ConsumerAdoption:
    """Record, once, the row id after which ``name`` is responsible for input.

    The first call appends ``consumer.adopted(name, adoption_row_id=MAX(id))``
    inside ``BEGIN IMMEDIATE``; every later call returns that same milestone.
    D8's F24: rows at or before the watermark are pre-adoption history and are
    never eligible for recovery.
    """
    _require_transaction_ownership(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT event_uid FROM events WHERE type = 'consumer.adopted' "
            "AND json_extract(payload_json, '$.name') = ? ORDER BY id ASC LIMIT 1",
            (name,),
        ).fetchone()
        if row is not None:
            existing = get_event(conn, str(row[0]))
            if existing is None:  # pragma: no cover - append-only row cannot vanish
                msg = f"consumer.adopted event {row[0]!r} disappeared from the log"
                raise InputClaimError(msg)  # noqa: TRY301 — rollback below is the point.
            conn.commit()
            return ConsumerAdoption(
                name=name,
                adoption_row_id=int(existing.payload["adoption_row_id"]),
                event=existing,
                adopted_now=False,
            )
        max_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        adoption_row_id = 0 if max_row is None else int(max_row[0])
        event = append_event_in_transaction(
            conn,
            type="consumer.adopted",
            payload={"name": name, "adoption_row_id": adoption_row_id},
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ConsumerAdoption(
        name=name,
        adoption_row_id=adoption_row_id,
        event=event,
        adopted_now=True,
    )


def recoverable_inputs(
    conn: sqlite3.Connection,
    *,
    adoption_row_id: int,
    trigger_types: Iterable[str],
) -> Sequence[tuple[int, Event]]:
    """Return post-watermark input rows a restart may still safely drive.

    Two shapes qualify, and only two (D8's startup reconciliation table):

    * an input row with **no** ``turn.started`` claim — nobody adopted it;
    * a claimed turn with **no** response/action milestone — it was claimed
      and then lost, so nothing it could duplicate exists.

    A claimed turn that already reached a milestone is deliberately absent:
    re-enqueueing it would redispatch its action, which D8 forbids outright.
    Rows at or before ``adoption_row_id`` are pre-adoption history and are
    never returned.
    """
    selected = tuple(dict.fromkeys(trigger_types))
    if not selected:
        return ()
    placeholders = ",".join("?" * len(selected))
    rows = conn.execute(
        f"SELECT id, event_uid FROM events "  # noqa: S608 — placeholders only, values stay bound.
        f"WHERE type IN ({placeholders}) AND id > ? ORDER BY id ASC",
        (*selected, adoption_row_id),
    ).fetchall()

    recoverable: list[tuple[int, Event]] = []
    for row_id, event_uid in rows:
        event = get_event(conn, str(event_uid))
        if event is None:  # pragma: no cover - append-only row cannot vanish
            continue
        claim = _existing_claim_for_trigger(conn, event.event_uid)
        if claim is None:
            recoverable.append((int(row_id), event))
            continue
        turn_id = claim.payload.get("turn_id")
        if isinstance(turn_id, str) and not _turn_has_milestone(conn, turn_id):
            recoverable.append((int(row_id), event))
    return tuple(recoverable)


def _turn_has_milestone(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Return whether this turn already produced response/action work."""
    placeholders = ",".join("?" * len(_TURN_MILESTONE_TYPES))
    row = conn.execute(
        f"SELECT 1 FROM events WHERE type IN ({placeholders}) "  # noqa: S608 — placeholders only.
        "AND json_extract(correlation_json, '$.turn_id') = ? LIMIT 1",
        (*_TURN_MILESTONE_TYPES, turn_id),
    ).fetchone()
    return row is not None


__all__ = [
    "REALTIME_INTENT_CONSUMER",
    "ConflictingTurnClaimError",
    "ConsumerAdoption",
    "InputAlreadyClaimed",
    "InputClaimError",
    "InputClaimOutcome",
    "InputClaimTransactionStateError",
    "InputClaimed",
    "adopt_consumer",
    "claim_input_once",
    "recoverable_inputs",
]
