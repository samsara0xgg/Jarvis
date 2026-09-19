"""ADR 0019 — a flat ``Tool`` leaves the rows a ``ToolDefinition`` handler left.

Asserts on the Event Log after a real ``ToolRegistry.dispatch``: the chain
``action.dispatched`` → ``action.running`` → ``action.result_observed``, the
terminal's ``source_event_id`` being the running row, its correlation being
``{action_id}`` alone, and the payload the dispatcher serializes on the
return path and on the ``ToolError`` path — once on the runner thread and
once inline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.tools import ToolError, get_current_time, tool
from jarvis.shared import CallerPrincipal
from tests.integration.test_wave4b_action_runner import _Fixture, _request

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

_TIME_KEYS = {"iso", "date", "time", "weekday", "spoken_time", "spoken_date"}


@tool(
    description="Raise a ToolError carrying its argument.",
    input_schema={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
)
def failing_tool(args: Mapping[str, Any]) -> dict[str, Any]:
    """The error path: nothing is returned, the dispatcher serializes the raise."""
    msg = f"bad {args['x']}"
    raise ToolError(msg, code="invalid_argument")


type _Row = tuple[str, str, dict[str, Any], str | None, dict[str, Any] | None]


def _action_rows(conn: sqlite3.Connection, action_id: str) -> list[_Row]:
    """Every ``action.*`` row of one action, in log order."""
    out: list[_Row] = []
    for etype, uid, payload, src, corr in conn.execute(
        "SELECT type, event_uid, payload_json, source_event_id, correlation_json "
        "FROM events WHERE type LIKE 'action.%' ORDER BY id ASC",
    ):
        decoded = json.loads(payload)
        if decoded.get("action_id") == action_id:
            out.append((etype, uid, decoded, src, json.loads(corr) if corr else None))
    return out


@pytest.mark.parametrize("with_runner", [True, False], ids=["runner", "inline"])
def test_flat_tool_writes_the_same_terminal_row(tmp_path: Path, *, with_runner: bool) -> None:
    """A return is an ``observation`` row, a ``ToolError`` an ``error`` row, both from running."""
    fx = _Fixture(tmp_path, tools=(get_current_time, failing_tool), with_runner=with_runner)
    try:
        bundle = fx.dispatch(_request("get_current_time", "A1"))
        rows = _action_rows(fx.conn, "A1")
        assert [r[0] for r in rows] == [
            "action.dispatched", "action.running", "action.result_observed",
        ]
        running_uid = rows[1][1]
        _, _, observed, src, corr = rows[2]
        assert src == running_uid
        assert corr == {"action_id": "A1"}
        assert set(observed) == {"action_id", "semantics", "tool_output"}
        assert observed["semantics"] == "observation"
        assert set(json.loads(observed["tool_output"])) == _TIME_KEYS
        slot = bundle.slots[0]
        assert slot.error is None
        assert slot.tool_output == observed["tool_output"]
        assert set(slot.payload) == _TIME_KEYS

        bundle = fx.dispatch(_request("failing_tool", "A2", arguments={"x": 7}))
        rows = _action_rows(fx.conn, "A2")
        assert [r[0] for r in rows] == [
            "action.dispatched", "action.running", "action.result_observed",
        ]
        _, _, observed, src, corr = rows[2]
        assert src == rows[1][1]
        assert corr == {"action_id": "A2"}
        assert observed == {
            "action_id": "A2",
            "semantics": "error",
            "tool_output": '{"error": "bad 7", "code": "invalid_argument"}',
            "error": "invalid_argument",
        }
        slot = bundle.slots[0]
        assert slot.error == "invalid_argument"
        assert slot.payload == {"error": "invalid_argument"}
        assert slot.tool_output == observed["tool_output"]

        assert fx.lifecycle.state_of("A1") == "result_observed"
        assert fx.lifecycle.state_of("A2") == "result_observed"
    finally:
        fx.close()
