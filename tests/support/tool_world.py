"""One tool call through the real flat dispatcher, and the event chain it leaves."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, Tool, ToolRegistry
from jarvis.shared import ActionRequest, AuthorizationLease, CallerPrincipal, RawResult
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared.clock import Clock
    from jarvis.shared.here import HereSource


class ToolWorld:
    """One runtime root, registry and lifecycle wired together."""

    def __init__(
        self, tmp_path: Path, *, tools: tuple[Tool, ...], clock: Clock | None = None,
        here: HereSource | None = None,
    ) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.registry = ToolRegistry(clock=clock, here=here)
        for one in tools:
            self.registry.register(one)
        self.lifecycle = ActionLifecycle()

    def dispatch(self, request: ActionRequest) -> RawResult:
        """Authorize and dispatch one request on the caller's thread."""
        self.lifecycle.register(request.action_id)
        self.lifecycle.transition(request.action_id, "authorized")
        return self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()


def tool_request(  # noqa: PLR0913 — one keyword per request field a test sets
    tool_name: str,
    action_id: str,
    *,
    arguments: dict[str, Any] | None = None,
    caller: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
    lease: bool = False,
    lease_expires_at_ms: int | None = None,
) -> ActionRequest:
    """Build one ActionRequest for a fixture tool; ``lease`` attaches Allen's grant for it.

    The grant is good for a minute of wall time unless ``lease_expires_at_ms`` says when it ends.
    """
    return ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=None,
        caller_principal=caller,
        risk_level="L1",
        arguments=arguments if arguments is not None else {},
        authorization_lease=(
            _lease_for(tool_name, action_id, lease_expires_at_ms) if lease else None
        ),
        run_id=None,
        turn_id=None,
    )


def _lease_for(
    tool_name: str, action_id: str, expires_at_ms: int | None = None,
) -> AuthorizationLease:
    """A lease an accepted card would have minted for ``tool_name`` (ADR 0012 D2)."""
    return {
        "lease_id": f"L{action_id}",
        "granted_by": "allen",
        "granted_to": CallerPrincipal.JARVIS_LLM,
        "allowed_tools": frozenset({tool_name}),
        "allowed_targets": frozenset(),
        "expires_at_ms": (
            int(time.time() * 1000) + 60_000 if expires_at_ms is None else expires_at_ms
        ),
        "max_uses": 1,
        "reason": "test",
        "source_confirmation_event_id": f"accepted-{action_id}",
    }


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
