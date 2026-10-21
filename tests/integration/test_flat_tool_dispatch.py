"""ADR 0019 — a flat ``Tool`` leaves its rows through the dispatcher.

Asserts on the Event Log after a real ``ToolRegistry.dispatch``: the chain
``action.dispatched`` → ``action.running`` → ``action.result_observed``, the
terminal's ``source_event_id`` being the running row, its correlation being
``{action_id}`` alone, and the payload the dispatcher serializes on the
return path, the ``ToolError`` path, the unexpected-exception path and the
result-cap path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import (
    ToolError,
    get_current_time,
    tool,
)
from jarvis.shared import CallerPrincipal
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
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
    raise ToolError(msg, code="invalid_arguments")


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


def test_flat_tool_writes_the_same_terminal_row(tmp_path: Path) -> None:
    """Return, ToolError, crash and overflow each end in one ``action.result_observed``."""
    tools = (get_current_time, failing_tool, crashing_tool, long_tool)
    fx = ToolWorld(tmp_path, tools=tools)
    try:
        bundle = fx.dispatch(
            tool_request("get_current_time", "A1", caller=CallerPrincipal.REGEX_ROUTER)
        )
        observed = tool_chain(fx, "A1")
        assert set(observed) == {"action_id", "semantics", "tool_output", "receipt"}
        assert observed["semantics"] == "observation"
        assert set(json.loads(observed["tool_output"])) == _TIME_KEYS
        slot = bundle
        assert slot.error is None
        assert slot.tool_output == observed["tool_output"]
        assert set(slot.payload) == _TIME_KEYS

        bundle = fx.dispatch(tool_request("failing_tool", "A2", arguments={"x": 7}))
        observed = tool_chain(fx, "A2")
        assert observed == {
            "action_id": "A2",
            "semantics": "error",
            "tool_output": (
                '{"error": "bad 7", "code": "invalid_arguments", "kind": "retry_other_args"}'
            ),
            "error": "invalid_arguments",
            "receipt": {
                "status": "failed", "reads_only": True, "derived": True,
                "line": "failing_tool(x=7) gave error invalid_arguments",
                "failure": "retry_other_args",
            },
        }
        slot = bundle
        assert slot.error == "invalid_arguments"
        assert slot.payload == {"error": "invalid_arguments"}
        assert slot.tool_output == observed["tool_output"]

        bundle = fx.dispatch(tool_request("crashing_tool", "A3", arguments={"x": 5000}))
        observed = tool_chain(fx, "A3")
        assert observed["semantics"] == "error"
        assert observed["error"] == "unexpected_error"
        assert len(observed["tool_output"]) <= _CAP
        out = json.loads(observed["tool_output"])
        assert out["code"] == "unexpected_error"
        assert out["error"].startswith("crashing_tool failed unexpectedly (RuntimeError: zzz")
        assert "…[omitted" in out["error"]
        assert out["error"].endswith("do not repeat the same call")  # the way out survives the cut
        assert out["truncated"] is True
        assert bundle.error == "unexpected_error"

        bundle = fx.dispatch(tool_request("long_tool", "A4", arguments={"x": 2000}))
        observed = tool_chain(fx, "A4")
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
        assert bundle.payload == out
    finally:
        fx.close()
