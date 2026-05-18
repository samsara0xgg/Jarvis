"""Unit tests for ``jarvis.decision.packet.assemble_packet``.

Validates that ``assemble_packet`` returns a frozen SituationPacket
with the trigger event, the recent trace, the Task Ledger snapshot,
and the trigger's correlation fields lifted into top-level slots.
"""

from __future__ import annotations

import dataclasses
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.packet import SituationPacket, assemble_packet
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path


def test_assemble_packet_returns_frozen_situation_packet(tmp_path: Path) -> None:
    """assemble_packet returns a frozen SituationPacket containing the trigger."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "ship spec"},
        )
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={"transcript": "hello", "turn_id": "T1"},
            correlation={"turn_id": "T1"},
        )

        packet = assemble_packet(trigger, conn)

        assert isinstance(packet, SituationPacket)
        assert packet.trigger_event == trigger
        assert packet.current_turn_id == "T1"
        assert packet.current_run_id is None
        assert any(record.task_id == "task_X" for record in packet.open_tasks)
        assert packet.recent_trace  # non-empty (at least the two events we just emitted)


def test_assemble_packet_is_frozen(tmp_path: Path) -> None:
    """SituationPacket is a frozen dataclass — mutation raises."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={"transcript": "hi", "turn_id": "T1"},
            correlation={"turn_id": "T1"},
        )
        packet = assemble_packet(trigger, conn)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(packet, "current_turn_id", "T999")  # noqa: B010 — frozen-dataclass FrozenInstanceError requires the descriptor path.


def test_assemble_packet_extracts_run_id_from_correlation(tmp_path: Path) -> None:
    """current_run_id comes from trigger.correlation when present."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "ship spec"},
        )
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R1", "task_id": "task_X", "runner": "stub"},
            correlation={"run_id": "R1"},
        )
        trigger = emit_event(
            conn,
            type="worker.reported",
            payload={
                "run_id": "R1",
                "action_id": "A1",
                "status": "reported_complete",
            },
            correlation={"run_id": "R1", "action_id": "A1", "turn_id": "T1"},
        )

        packet = assemble_packet(trigger, conn)
        assert packet.current_run_id == "R1"
        assert packet.current_turn_id == "T1"
