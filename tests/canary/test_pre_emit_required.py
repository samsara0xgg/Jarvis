"""H3 — Pre-emit Gate token is required before ``write_output``.

Per ADR 0001 § Acceptance criterion H3:

> Runtime check on ``surface.cli.write_output(...)``; raises if Pre-emit
> Gate token is stale or absent.

Three runtime scenarios are exercised:

1. ``last_gate_response_hash=None`` → ``PreEmitTokenError`` raised.
2. mismatching ``last_gate_response_hash`` → ``PreEmitTokenError`` raised.
3. matching hash → ``write_output`` succeeds; returned state has the
   token cleared (``last_gate_response_hash=None``).

The :class:`ResponsePlan` used here is the real
:class:`jarvis.decision.ResponsePlan` dataclass — not a mock. No
``unittest.mock`` imports anywhere in the canary suite.
"""

from __future__ import annotations

import hashlib
import io

import pytest

from jarvis.decision import ResponsePlan
from jarvis.surface.cli import PreEmitTokenError, SurfaceState, write_output


def _make_plan(text: str) -> ResponsePlan:
    """Build a real :class:`ResponsePlan` with a SHA-256-stamped hash."""
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=("verified",),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_write_output_raises_when_no_token_recorded() -> None:
    """Empty state (no token) is rejected."""
    plan = _make_plan("ok")
    state = SurfaceState(last_gate_response_hash=None)
    stream = io.StringIO()
    with pytest.raises(PreEmitTokenError):
        write_output(state, plan, stream=stream)
    assert stream.getvalue() == ""


def test_write_output_raises_on_stale_token() -> None:
    """A different (stale) token is rejected."""
    plan = _make_plan("response-A")
    stale_hash = hashlib.sha256(b"response-B").hexdigest()
    state = SurfaceState(last_gate_response_hash=stale_hash)
    stream = io.StringIO()
    with pytest.raises(PreEmitTokenError):
        write_output(state, plan, stream=stream)
    assert stream.getvalue() == ""


def test_write_output_succeeds_with_matching_token_and_clears_state() -> None:
    """Matching hash renders the plan; new state has token cleared."""
    plan = _make_plan("hello")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)
    stream = io.StringIO()
    next_state = write_output(state, plan, stream=stream)
    assert next_state.last_gate_response_hash is None
    output = stream.getvalue()
    assert "hello" in output
