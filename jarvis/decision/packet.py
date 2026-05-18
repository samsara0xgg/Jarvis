"""L3 Situation Packet assembler — frozen view of "what's true right now".

Per ADR 0001 § Stub strategy L3 row ("real, reads projection snapshots")
and spec §3.4.x.

The Situation Packet bundles the trigger event, the most recent trace
window, and the live Task Ledger snapshot for the L3 decision pipeline
(Intent Router / Resolver / Gates). Reading projections from disk on
every L3 invocation is fine Day-1 — the trace is short.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
of sibling layers (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``) and no import of ``jarvis.decision.llm`` so this
module can be re-used by the resolver / gates without circular pulls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.projections import TaskLedgerRecord, TaskLedgerSnapshot


# --- SituationPacket --------------------------------------------------------


@dataclass(frozen=True)
class SituationPacket:
    """Bundle of "what's true at the moment of this L3 invocation".

    Frozen so a single packet can be passed to multiple downstream
    callers (Resolver / Gates / LLM bridge) without anyone mutating
    shared state mid-evaluation.

    Attributes:
        trigger_event: The single event that re-entered L3 (e.g.
            ``utterance.received``, ``worker.reported``,
            ``action.result_observed``).
        recent_trace: Frozen tuple of the most recent events (oldest
            first; same order as Recent Trace projection iteration).
        task_ledger_snapshot: Snapshot of the Task Ledger projection
            used by the Resolver and Pre-action Gate.
        open_tasks: Convenience tuple of currently-open task records
            (derived from the snapshot; cached here so downstream
            callers do not need to recompute).
        current_turn_id: Turn correlation for the trigger (when known
            from ``trigger_event.correlation`` or
            ``trigger_event.payload``).
        current_run_id: Run correlation for the trigger (similar).
    """

    trigger_event: Event
    recent_trace: tuple[Event, ...]
    task_ledger_snapshot: TaskLedgerSnapshot
    open_tasks: tuple[TaskLedgerRecord, ...]
    current_turn_id: str | None
    current_run_id: str | None


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

    Reads the event log via ``jarvis.state.projections.make_snapshot``
    (which folds Task Ledger + Recent Trace + Claim/Evidence in one
    pass), then bundles the trigger + correlations into a frozen
    packet.

    Args:
        trigger: The event that re-entered L3 (already appended to
            the log before this call). The trigger is included in the
            packet so downstream callers can inspect its payload
            without re-querying.
        conn: Open Event Log connection.

    Returns:
        Frozen :class:`SituationPacket` ready for the Intent Router,
        Resolver, Effective Policy resolver, and Gates.
    """
    projections = make_snapshot(conn)
    snapshot = projections.task_ledger.snapshot()

    return SituationPacket(
        trigger_event=trigger,
        recent_trace=projections.recent_trace.events,
        task_ledger_snapshot=snapshot,
        open_tasks=snapshot.open_tasks(),
        current_turn_id=_extract_correlation(trigger, "turn_id"),
        current_run_id=_extract_correlation(trigger, "run_id"),
    )


__all__ = [
    "SituationPacket",
    "assemble_packet",
]
