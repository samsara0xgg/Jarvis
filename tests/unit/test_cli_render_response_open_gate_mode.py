"""ADR-0005 §7: surface.response_open carries required_gate_mode in payload.

Spec basis: §3.4.13 (gate-mode semantics) + §3.6.6 (TTS streaming gate
behavior). The Inherent voice surface needs to route between
sentence-streaming and full-text TTS playback based on the same
required_gate_mode the renderer already uses for chunk-splitting; the
audit fact must therefore land on the response_open header per
ADR-0005 §5.3 (plan vs event).
"""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface.cli_render import _emit_response_open

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class _PlanStub:
    """Minimal ResponsePlanLike — only ``required_gate_mode`` is exercised."""

    text: str = ""
    response_hash: str = "h" * 64
    required_gate_mode: str = "sentence"
    output_risk_class: str = "routine"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a fresh Event Log at tmp_path/events.db and close on teardown."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        yield conn


def test_response_open_payload_carries_gate_mode(conn: sqlite3.Connection) -> None:
    """``full_text`` plans land their gate mode on the open payload verbatim."""
    plan = _PlanStub(required_gate_mode="full_text")
    _emit_response_open(conn, turn_id="T1", query="hi", response_plan=plan)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type='surface.response_open' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["required_gate_mode"] == "full_text"


def test_response_open_payload_defaults_when_plan_is_sentence(
    conn: sqlite3.Connection,
) -> None:
    """``sentence`` plans land ``"sentence"`` on the open payload (no rewrite)."""
    plan = _PlanStub(required_gate_mode="sentence")
    _emit_response_open(conn, turn_id="T2", query="", response_plan=plan)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type='surface.response_open' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["required_gate_mode"] == "sentence"
