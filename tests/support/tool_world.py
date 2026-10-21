"""One tool call through the real flat dispatcher, and the event chain it leaves."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, Tool, ToolDefinition, ToolRegistry
from jarvis.shared import ActionRequest, CallerPrincipal, RawResultBundle
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


class ToolWorld:
    """One runtime root, registry and lifecycle wired together."""

    def __init__(self, tmp_path: Path, *, tools: tuple[Tool | ToolDefinition, ...]) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.registry = ToolRegistry()
        for one in tools:
            self.registry.register(one)
        self.lifecycle = ActionLifecycle()

    def dispatch(self, request: ActionRequest) -> RawResultBundle:
        """Authorize and dispatch one request on the caller's thread."""
        self.lifecycle.register(request.action_id)
        self.lifecycle.transition(request.action_id, "authorized")
        return self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


def tool_request(
    tool_name: str,
    action_id: str,
    *,
    arguments: dict[str, Any] | None = None,
    caller: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
) -> ActionRequest:
    """Build one ActionRequest for a fixture tool."""
    return ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=None,
        caller_principal=caller,
        risk_level="L1",
        arguments=arguments if arguments is not None else {},
        authorization_lease=None,
        run_id=None,
        turn_id=None,
    )


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


def tool_chain(fx: ToolWorld, action_id: str) -> dict[str, Any]:
    """Assert the three-row chain and return the terminal's payload."""
    rows = _action_rows(fx.conn, action_id)
    assert [r[0] for r in rows] == ["action.dispatched", "action.running", "action.result_observed"]
    _, _, observed, src, corr = rows[2]
    assert src == rows[1][1]
    assert corr == {"action_id": action_id}
    assert fx.lifecycle.state_of(action_id) == "result_observed"
    return observed
