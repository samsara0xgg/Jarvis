"""What the Event Log says was spent, as L2 answers it (ADR 0349).

The runtime sums the day's dollars and asks whether a kind of model call was recorded. Each test
writes the ``cost.recorded`` rows the accounting writes into a real on-disk Event Log; times are
fixed epoch milliseconds, never the wall clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.state.cost_accounting import call_recorded_since, spent_since
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path


def _cost(log: TurnLog, kind: str, usd: float | None, at: int) -> None:
    log.put("cost.recorded", {"kind": kind, "model": "m", "cost_usd": usd}, at=at)


def test_the_dollars_recorded_since(tmp_path: Path) -> None:
    """Rows at or after the time add up; an unpriced row adds nothing; none make zero."""
    log = TurnLog(tmp_path)
    assert spent_since(log.conn, since_ms=0) == 0.0
    _cost(log, "decision", 5.0, at=900)
    _cost(log, "decision", 0.25, at=1_000)
    _cost(log, "day_summary", 0.5, at=2_000)
    _cost(log, "decision", None, at=2_000)
    log.put("cost.recorded", {"kind": "worker", "model": "m"}, at=2_000)
    log.heard("T1", at=2_000)

    assert spent_since(log.conn, since_ms=1_000) == 0.75
    assert spent_since(log.conn, since_ms=2_001) == 0.0
    assert spent_since(log.conn, since_ms=0) == 5.75


def test_a_call_of_a_kind_recorded_since(tmp_path: Path) -> None:
    """Only a ``cost.recorded`` of that kind at or after the time counts."""
    log = TurnLog(tmp_path)
    _cost(log, "day_summary", 0.1, at=900)
    _cost(log, "decision", 0.1, at=2_000)
    log.put("turn.timing", {"turn_id": "T1", "kind": "day_summary"}, at=2_000)
    assert not call_recorded_since(log.conn, kind="day_summary", since_ms=1_000)
    _cost(log, "day_summary", None, at=1_000)
    assert call_recorded_since(log.conn, kind="day_summary", since_ms=1_000)
    assert not call_recorded_since(log.conn, kind="day_summary", since_ms=1_001)
    assert not call_recorded_since(log.conn, kind="day_prose", since_ms=0)
