"""Helpers for the previous-answer line: a turn asked, the next turn's line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.decision import _previous_answer_line
from jarvis.decision.packet import assemble_packet
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3


def record_asked(conn: sqlite3.Connection, turn_id: str) -> None:
    emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": turn_id, "transcript": "synthetic", "channel": "inherent_wake"},
    )


def next_turn_line(conn: sqlite3.Connection) -> str | None:
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"turn_id": "next", "transcript": "and?", "channel": "inherent_wake"},
    )
    packet = assemble_packet(trigger, conn)
    return _previous_answer_line(packet)
