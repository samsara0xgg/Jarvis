"""L3 Situation Packet assembler — frozen view of "what's true right now".

Per ADR 0001 § Stub strategy L3 row ("real, reads projection snapshots")
and spec §3.4.x.

The Situation Packet bundles the trigger event, the most recent trace
window, and (ADR-0009 D6) the Status Board for the L3 decision pipeline
(Intent Router / Gates). Reading projections from disk on every L3
invocation is fine Day-1 — the trace is short, and rebuilding per
invocation is exactly what makes the Status Board's freshness automatic:
there is no cache to invalidate.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``) and no import of ``jarvis.decision.llm`` so this
module can be re-used by the gates without circular pulls.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.decision_snapshot import read_decision_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.authorization_snapshot import AuthorizationSnapshot
    from jarvis.state.conversation import ConversationHistory
    from jarvis.state.projections import (
        ActionAdmissions,
        PendingConfirmations,
        StatusBoard,
    )


# --- SituationPacket --------------------------------------------------------


@dataclass(frozen=True)
class SituationPacket:
    """Bundle of "what's true at the moment of this L3 invocation".

    Frozen so a single packet can be passed to multiple downstream
    callers (Gates / LLM bridge) without anyone mutating shared state
    mid-evaluation.

    Attributes:
        trigger_event: The single event that re-entered L3 (e.g.
            ``surface.user_intent``, ``action.result_observed``).
        recent_trace: Frozen tuple of the most recent events (oldest
            first; same order as Recent Trace projection iteration).
        current_turn_id: Turn correlation for the trigger (when known
            from ``trigger_event.correlation`` or
            ``trigger_event.payload``).
        status_board: Folded Status Board (ADR-0009 D6) — watched-repo
            state, last Mac power transition, open actions. Carried on
            the packet so L3 can answer "repo X 现在什么状态" from the
            log instead of shelling out to git mid-turn.
        pending_confirmation: Folded PendingConfirmations (ADR-0012 §3
            D4, packet block 8) — the single live-or-recent
            confirmation ask, plus every `lease_id` a passing gate
            evaluation has consumed. The answer-path grammar hook
            (Step 6) and `format_pending_confirmation_note` both read
            this field.
        action_admissions: Folded ActionAdmissions (ADR-0008 D10) — the
            admitting `gate.evaluated` uid and dispatched uid of every
            non-terminal action.
    """

    trigger_event: Event
    recent_trace: tuple[Event, ...]
    current_turn_id: str | None
    status_board: StatusBoard
    pending_confirmation: PendingConfirmations
    action_admissions: ActionAdmissions
    conversation_history: ConversationHistory | None = None
    authorization_snapshot: AuthorizationSnapshot | None = None
    event_cursor: int | None = None


# --- assemble_packet --------------------------------------------------------


def _extract_correlation(trigger: Event, key: str) -> str | None:
    """Return ``trigger.correlation[key]`` or ``trigger.payload[key]`` if str."""
    if trigger.correlation is not None and key in trigger.correlation:
        value = trigger.correlation[key]
        if isinstance(value, str):
            return value
    payload_value = trigger.payload.get(key)
    if isinstance(payload_value, str):
        return payload_value
    return None


def assemble_packet(trigger: Event, conn: sqlite3.Connection) -> SituationPacket:
    """Build a :class:`SituationPacket` from the live event log.

    Reads the Event Log and confirmation/dispatch operational tables under one
    SQLite read transaction, folds the materialized events into projections,
    then bundles the exact cursor, trigger and correlations into a frozen packet.

    Args:
        trigger: The event that re-entered L3 (already appended to
            the log before this call). The trigger is included in the
            packet so downstream callers can inspect its payload
            without re-querying.
        conn: Open Event Log connection.

    Returns:
        Frozen :class:`SituationPacket` ready for the Intent Router,
        Effective Policy resolver, and Gates.
    """
    state = read_decision_snapshot(conn)
    projections = state.projections

    return SituationPacket(
        trigger_event=trigger,
        recent_trace=projections.recent_trace.events,
        current_turn_id=_extract_correlation(trigger, "turn_id"),
        status_board=projections.status_board,
        pending_confirmation=projections.pending_confirmations,
        action_admissions=projections.action_admissions,
        conversation_history=projections.conversation_history,
        authorization_snapshot=state.authorizations,
        event_cursor=state.event_cursor,
    )


# --- Pending confirmation system note (ADR-0012 §3 D4) --------------------

_MS_PER_SECOND: Final[int] = 1000
_NOTE_ARGUMENTS_CHARS: Final[int] = 2000
"""The card's arguments the note carries, so the model can revise a letter it no longer sees."""


def format_pending_confirmation_note(
    packet: SituationPacket,
    *,
    now_ms: int | None = None,
) -> str | None:
    """Render the live pending confirmation ask as an LLM system note.

    Id-free by design (ADR-0012 §3 D4): `confirmation_id` never appears
    in the rendered text, so an LLM reading this note — on an unrelated
    turn (C4), or one that only paraphrases consent (C6) — has no
    handle it could try to use to act on the pending ask itself. Only
    the answer-path grammar hook (Step 6, exact-sentence match against
    `confirm_grammar.yaml`) or the card's button (ADR 0061) can move the
    slot; this note exists so the
    LLM can *talk about* the ask without being structurally able to
    authorize it.

    Returns None when there is no pending slot: no `confirmation.requested`
    has fired, or the slot has moved past `pending` (accepted / rejected /
    consumed / superseded). ADR 0061: the card waits past its TTL, so the
    note shows it, with its arguments, until it is answered or replaced.

    Args:
        packet: The packet whose ``pending_confirmation`` is rendered.
        now_ms: Reference "now" in epoch milliseconds. Defaults to the
            wall clock; injected by callers that need a fixed reference
            (mirrors ``format_status_board_note``).
    """
    slot = packet.pending_confirmation.slot
    resolved_now_ms = int(time.time() * _MS_PER_SECOND) if now_ms is None else now_ms
    if slot is None or not slot.is_live(resolved_now_ms):
        return None

    tool_name = slot.snapshot.get("tool_name", "?")
    arguments = json.dumps(slot.snapshot.get("args_meta", {}), ensure_ascii=False)
    if len(arguments) > _NOTE_ARGUMENTS_CHARS:
        arguments = f"{arguments[:_NOTE_ARGUMENTS_CHARS]}…"
    return (
        f"A card is waiting for the user's button: {tool_name} {arguments}. "
        "You cannot run or authorize it yourself; only the user's button or direct answer "
        "counts. To change it, or when the user asks you to go ahead with it, call the same "
        "tool again with the arguments it should have: that replaces the card and asks once more."
    )


__all__ = [
    "SituationPacket",
    "assemble_packet",
    "format_pending_confirmation_note",
]
