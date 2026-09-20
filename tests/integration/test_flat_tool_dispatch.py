"""ADR 0019 — a flat ``Tool`` leaves the rows a ``ToolDefinition`` handler left.

Asserts on the Event Log after a real ``ToolRegistry.dispatch``: the chain
``action.dispatched`` → ``action.running`` → ``action.result_observed``, the
terminal's ``source_event_id`` being the running row, its correlation being
``{action_id}`` alone, and the payload the dispatcher serializes on the
return path, the ``ToolError`` path, the unexpected-exception path and the
result-cap path — once on the runner thread and once inline.
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

    from jarvis.execution.tools import ToolContext

_TIME_KEYS = {"iso", "date", "time", "weekday", "spoken_time", "spoken_date"}
_ARGS_SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
_CAP = 300


@tool(
    description="Raise a ToolError carrying its argument.",
    input_schema=_ARGS_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
)
def failing_tool(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The error path: nothing is returned, the dispatcher serializes the raise."""
    msg = f"bad {args['x']}"
    raise ToolError(msg, code="invalid_argument")


@tool(
    description="Raise something that is not a ToolError.",
    input_schema=_ARGS_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
    max_result_chars=_CAP,
)
def crashing_tool(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The unexpected path: the dispatcher still writes the terminal, message capped."""
    raise RuntimeError("z" * args["x"])


@tool(
    description="Return more text than the cap allows.",
    input_schema=_ARGS_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
    max_result_chars=_CAP,
)
def long_tool(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The cap path: the longest string is windowed head+tail, the rest kept whole."""
    return {"text": "a" * args["x"], "rows": ["b" * 40, "c" * 40], "n": args["x"]}


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


def _chain(fx: _Fixture, action_id: str) -> dict[str, Any]:
    """Assert the three-row chain and return the terminal's payload."""
    rows = _action_rows(fx.conn, action_id)
    assert [r[0] for r in rows] == ["action.dispatched", "action.running", "action.result_observed"]
    _, _, observed, src, corr = rows[2]
    assert src == rows[1][1]
    assert corr == {"action_id": action_id}
    assert fx.lifecycle.state_of(action_id) == "result_observed"
    return observed


@pytest.mark.parametrize("with_runner", [True, False], ids=["runner", "inline"])
def test_flat_tool_writes_the_same_terminal_row(tmp_path: Path, *, with_runner: bool) -> None:
    """Return, ToolError, crash and overflow each end in one ``action.result_observed``."""
    tools = (get_current_time, failing_tool, crashing_tool, long_tool)
    fx = _Fixture(tmp_path, tools=tools, with_runner=with_runner)
    try:
        bundle = fx.dispatch(_request("get_current_time", "A1"))
        observed = _chain(fx, "A1")
        assert set(observed) == {"action_id", "semantics", "tool_output"}
        assert observed["semantics"] == "observation"
        assert set(json.loads(observed["tool_output"])) == _TIME_KEYS
        slot = bundle.slots[0]
        assert slot.error is None
        assert slot.tool_output == observed["tool_output"]
        assert set(slot.payload) == _TIME_KEYS

        bundle = fx.dispatch(_request("failing_tool", "A2", arguments={"x": 7}))
        observed = _chain(fx, "A2")
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

        bundle = fx.dispatch(_request("crashing_tool", "A3", arguments={"x": 5000}))
        observed = _chain(fx, "A3")
        assert observed["semantics"] == "error"
        assert observed["error"] == "unexpected_error"
        assert len(observed["tool_output"]) <= _CAP
        out = json.loads(observed["tool_output"])
        assert out["code"] == "unexpected_error"
        assert out["error"].startswith("crashing_tool: unexpected RuntimeError: zzz")
        assert "…[omitted" in out["error"]
        assert out["truncated"] is True
        assert bundle.slots[0].error == "unexpected_error"

        bundle = fx.dispatch(_request("long_tool", "A4", arguments={"x": 2000}))
        observed = _chain(fx, "A4")
        assert observed["semantics"] == "observation"
        assert len(observed["tool_output"]) <= _CAP
        out = json.loads(observed["tool_output"])
        assert out["rows"] == ["b" * 40, "c" * 40]
        assert out["n"] == 2000
        assert out["truncated"] is True
        head, marker, tail = out["text"].partition("…[omitted ")
        assert head.startswith("aaaa")
        assert head.endswith("\n")
        assert marker
        assert tail.endswith("a" * 10)
        assert bundle.slots[0].payload == out
    finally:
        fx.close()
