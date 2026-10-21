"""Helpers for the plugin tool approval flow: say, press a card, read rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.decision import DecideContext, decide
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


@dataclass(frozen=True)
class Paths:
    event_log: Path
    artifacts_root: Path


def say(ctx: DecideContext, text: str, turn_id: str) -> str:
    trigger = emit_event(
        ctx.conn,
        type="surface.user_intent",
        payload={"transcript": text, "turn_id": turn_id},
        correlation={"turn_id": turn_id},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return result.response_plan.text


def press(ctx: DecideContext, decision: dict[str, Any], turn_id: str) -> str:
    """A card's button: an empty-transcript intent naming the confirmation (ADR 0062)."""
    trigger = emit_event(
        ctx.conn,
        type="surface.user_intent",
        payload={"transcript": "", "turn_id": turn_id, "confirmation_decision": decision},
        correlation={"turn_id": turn_id},
    )
    result = decide(trigger, ctx)
    assert result.response_plan is not None
    return result.response_plan.text


def logged_dicts(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type=? ORDER BY id", (event_type,)
        )
    ]
