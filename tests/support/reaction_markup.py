"""Decide a batch of replies in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.decision import decide
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.turn_guards import StepModel, guard_context

if TYPE_CHECKING:
    from pathlib import Path


def decide_batch(
    tmp_path: Path,
    model: StepModel,
    channel: str,
    budget: int = 8,
) -> Any:  # noqa: ANN401 - DecideResult
    conn = open_event_log(tmp_path / "events.db")
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "我到家了", "turn_id": "T-batch", "channel": channel},
            correlation={"turn_id": "T-batch"},
        )
        return decide(trigger, guard_context(conn, tmp_path, model, budget))
    finally:
        conn.close()
