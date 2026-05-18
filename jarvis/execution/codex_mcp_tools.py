"""Standalone stdio MCP server exposing the ``submit_report`` tool.

ADR-0002 Step 6 — spec §3.5.8.

Codex spawns this module as a subprocess at app-server startup via the
``-c mcp_servers.jarvis-tools.*`` flags (Step 7 wires the injection).
Codex owns the subprocess lifetime; jarvis never spawns or directly
manages it. From within a Codex turn, the worker can call
``submit_report`` exactly once before ending the turn to deliver a
structured ``WorkerReport``.

This module speaks the MCP stdio JSON-RPC dialect by hand
(newline-delimited JSON over stdin/stdout) — no third-party ``mcp``
PyPI dependency. Framing pattern mirrors Hermes'
``agent/transports/hermes_tools_mcp_server.py`` (line-oriented stdio).

Protocol surface (everything else returns JSON-RPC error -32601):

* ``initialize`` (request)            -> server info + capabilities
* ``notifications/initialized``       -> notification, no reply
* ``tools/list`` (request)            -> ``[SUBMIT_REPORT_TOOL]``
* ``tools/call`` (request)            -> echo the structured arguments

Hard rules (see ADR §submit_report tool injection):

* stdout is the protocol — never print anything other than valid
  JSON-RPC messages to stdout. All logging / debug output uses stderr.
* No imports from ``jarvis.*``. Codex owns the lifetime; the L4 spawn
  worker handler captures the actual ``submit_report`` arguments via
  ``item/tool_call`` notifications on the Codex app-server channel, not
  by reading anything from this subprocess.
* Enforcement is by prompt pressure + post-turn guard (in L4), not by
  this server. This server merely accepts and echoes the call so Codex
  can record it as an ``item/tool_call`` event.

Run with: ``python -m jarvis.execution.codex_mcp_tools``.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import IO, Any

# JSON-RPC 2.0 §request id MAY be string, number, or null; jarvis never
# generates one — the client picks. Treating it as opaque carries it
# verbatim into the matching response so the client can correlate.
JsonRpcId = str | int | float | None

# Protocol version Codex's MCP client negotiates on ``initialize``. The
# value below mirrors what Codex's reference MCP servers report; clients
# tolerate version skew gracefully.
_PROTOCOL_VERSION = "2024-11-05"

_SERVER_NAME = "jarvis-tools"
_SERVER_VERSION = "0.1.0"

# JSON-RPC standard error codes used by this server.
_PARSE_ERROR = -32700
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

# Single tool surface per ADR-0002 § submit_report tool injection
# (spec §3.5.8). The ``required`` list and the optional-property keys
# are load-bearing — the L4 take_notification capture in Step 10 reads
# the structured arguments verbatim and forms the canonical
# ``WorkerReport`` payload from them. Schema-change here ripples into
# spec §3.5.8 + the post-turn guard.
SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "name": "submit_report",
    "description": (
        "Submit a final structured report. You MUST call this exactly "
        "once before ending the turn. The report is how Jarvis reads "
        "your result; without it the run is marked report_missing."
    ),
    "input_schema": {
        "type": "object",
        "required": ["status", "summary"],
        "properties": {
            "status": {"enum": ["ok", "partial", "failed", "blocked"]},
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "tests_run": {"type": "array", "items": {"type": "string"}},
            "evidence_submitted": {"type": "array", "items": {"type": "string"}},
            "remaining_risks": {"type": "string"},
            "needs_human_review": {"type": "boolean"},
            "next_recommended_action": {"type": "string"},
        },
    },
}


def _read_message(stream: IO[str]) -> dict[str, Any] | None:
    """Read one newline-delimited JSON message from ``stream``.

    Returns the decoded message dict. Returns ``None`` on EOF.
    Raises ``json.JSONDecodeError`` if the line is not valid JSON —
    callers translate this into a JSON-RPC parse error response.
    """
    while True:
        raw = stream.readline()
        if raw == "":
            return None
        line = raw.strip()
        if line == "":
            # Skip blank keepalive lines without treating them as EOF.
            continue
        decoded: object = json.loads(line)
        if not isinstance(decoded, dict):
            # JSON-RPC requires an object at the top level for our methods.
            msg = "expected JSON object at top level"
            raise json.JSONDecodeError(msg, line, 0)
        return decoded


def _write_message(stream: IO[str], msg: dict[str, Any]) -> None:
    """Write one newline-delimited JSON message to ``stream``.

    Flush immediately — the parent process consumes line-by-line and
    blocks on the next read until a complete line lands.
    """
    stream.write(json.dumps(msg) + "\n")
    stream.flush()


def _ok_response(msg_id: JsonRpcId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: JsonRpcId, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(msg_id: JsonRpcId) -> dict[str, Any]:
    return _ok_response(
        msg_id,
        {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
        },
    )


def _handle_tools_list(msg_id: JsonRpcId) -> dict[str, Any]:
    return _ok_response(msg_id, {"tools": [SUBMIT_REPORT_TOOL]})


def _handle_tools_call(msg_id: JsonRpcId, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name != SUBMIT_REPORT_TOOL["name"]:
        return _error_response(
            msg_id, _INVALID_PARAMS, f"Unknown tool: {name!r}",
        )
    return _ok_response(
        msg_id,
        {
            "content": [{"type": "text", "text": "submit_report accepted"}],
            "isError": False,
            "structuredContent": arguments,
        },
    )


def _coerce_id(raw: object) -> JsonRpcId:
    """Coerce the raw ``id`` field to the JSON-RPC id union.

    Decoded JSON only ever yields str/int/float/None for primitive ids;
    anything else (list/dict) violates the JSON-RPC spec and we map it
    to ``None`` for the error reply.
    """
    if isinstance(raw, (str, int, float)) or raw is None:
        return raw
    return None


def _dispatch(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Route one decoded JSON-RPC message. ``None`` means no reply."""
    method = msg.get("method")
    msg_id = _coerce_id(msg.get("id"))
    params_raw = msg.get("params") or {}
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}

    # Notifications carry no ``id`` — JSON-RPC forbids replying to them.
    is_notification = "id" not in msg

    if method == "initialize":
        return _handle_initialize(msg_id)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _handle_tools_list(msg_id)
    if method == "tools/call":
        return _handle_tools_call(msg_id, params)

    if is_notification:
        # Unknown notification — silently ignore per JSON-RPC.
        return None
    return _error_response(msg_id, _METHOD_NOT_FOUND, "Method not found")


def serve(reader: IO[str], writer: IO[str]) -> None:
    """Run the request/response loop until EOF on ``reader``."""
    while True:
        try:
            msg = _read_message(reader)
        except json.JSONDecodeError:
            _write_message(
                writer,
                _error_response(None, _PARSE_ERROR, "Parse error"),
            )
            continue
        if msg is None:
            return
        reply = _dispatch(msg)
        if reply is not None:
            _write_message(writer, reply)


def main() -> int:
    """Entry point for ``python -m jarvis.execution.codex_mcp_tools``."""
    try:
        serve(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — last-resort guard for the stdio loop
        # Never let a stack trace land on stdout (it's the protocol wire).
        # Dump full traceback to stderr so Codex's captured stderr surfaces
        # the failure in the user-facing error report.
        sys.stderr.write(f"jarvis-tools MCP server crashed: {exc!r}\n")
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
