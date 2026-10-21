"""H3 — Pre-emit Gate token is required before ``render_response``.

Per ADR 0001 § Acceptance criterion H3:

> Runtime check on the surface render; raises if Pre-emit Gate token is
> stale or absent.

Three runtime scenarios are exercised on ``surface.cli_render.render_response``,
the one path the daemon renders an approved plan through:

1. ``last_gate_response_hash=None`` → ``PreEmitTokenError`` raised.
2. mismatching ``last_gate_response_hash`` → ``PreEmitTokenError`` raised.
3. matching hash → ``render_response`` succeeds; returned state has the
   token cleared (``last_gate_response_hash=None``) and the audit event
   is in the log.

The :class:`ResponsePlan` used here is the real
:class:`jarvis.decision.ResponsePlan` dataclass — not a mock. No
``unittest.mock`` imports anywhere in the canary suite.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import ResponsePlan
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.surface.cli import PreEmitTokenError, SurfaceState
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _make_plan(text: str) -> ResponsePlan:
    """Build a real :class:`ResponsePlan` with a SHA-256-stamped hash."""
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=("verified",),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="consequential_claim",
        required_gate_mode="full_text",
    )


def _render(
    state: SurfaceState, plan: ResponsePlan, conn: sqlite3.Connection,
) -> tuple[SurfaceState, object]:
    return render_response(
        state,
        plan,
        conn=conn,
        turn_id="turn-h3",
        attention_channel="silent_log",
        available_surfaces=frozenset(),
    )


def test_render_response_raises_when_no_token_recorded(tmp_path: Path) -> None:
    """Empty state (no token) is rejected, and nothing is written."""
    conn = open_event_log(tmp_path / "events.db")
    with pytest.raises(PreEmitTokenError):
        _render(SurfaceState(last_gate_response_hash=None), _make_plan("ok"), conn)
    assert not list(iter_events_of_types(conn, ("surface.response_emitted",)))


def test_render_response_raises_on_stale_token(tmp_path: Path) -> None:
    """A different (stale) token is rejected, and nothing is written."""
    conn = open_event_log(tmp_path / "events.db")
    stale_hash = hashlib.sha256(b"response-B").hexdigest()
    with pytest.raises(PreEmitTokenError):
        _render(SurfaceState(last_gate_response_hash=stale_hash), _make_plan("response-A"), conn)
    assert not list(iter_events_of_types(conn, ("surface.response_emitted",)))


def test_render_response_succeeds_with_matching_token_and_clears_state(tmp_path: Path) -> None:
    """Matching hash renders the plan; new state has the token cleared."""
    conn = open_event_log(tmp_path / "events.db")
    plan = _make_plan("hello")
    next_state, _ = _render(SurfaceState(last_gate_response_hash=plan.response_hash), plan, conn)
    assert next_state.last_gate_response_hash is None
    emitted = list(iter_events_of_types(conn, ("surface.response_emitted",)))
    assert [event.payload["response_hash"] for event in emitted] == [plan.response_hash]
