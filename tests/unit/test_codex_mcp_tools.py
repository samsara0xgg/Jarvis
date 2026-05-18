"""Subprocess unit tests for the stdio MCP server (ADR-0002 Step 6).

The server is spawned by Codex in production; this test spawns it the
same way (``python -m jarvis.execution.codex_mcp_tools``) and drives
the protocol surface end-to-end so the framing, dispatch, and JSON-RPC
error codes match the contract spec'd in
``docs/adr/0002-real-codex-flagship-scenario.md`` § submit_report tool
injection.

Why subprocess and not in-process: the production lifetime is a
subprocess owned by Codex, so the subprocess test catches stdio framing
bugs (newline handling, flush order, stderr-vs-stdout discipline) that
an in-process ``serve()`` call would miss.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from typing import IO, TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# Module entrypoint we spawn for every test. Keeping it as a constant
# (rather than inlining the string) makes the call sites read like
# "spawn the MCP server" instead of "shell out to python -m".
_SERVER_MODULE = "jarvis.execution.codex_mcp_tools"

# Wall-clock budget for any one test: the server is small and replies
# immediately, so a roundtrip should finish well under a second. The
# generous timeout is for CI/macOS interpreter startup outliers.
_RECV_TIMEOUT_SEC = 5.0


def _send(proc: subprocess.Popen[bytes], msg: dict[str, Any]) -> None:
    """Write one newline-delimited JSON message to the server's stdin."""
    assert proc.stdin is not None
    line = (json.dumps(msg) + "\n").encode("utf-8")
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Read one JSON-RPC reply from the server's stdout."""
    assert proc.stdout is not None
    stdout: IO[bytes] = proc.stdout
    line = stdout.readline()
    if not line:
        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read() or b""
        pytest.fail(
            f"MCP server closed stdout before reply; stderr={stderr.decode(errors='replace')!r}"
        )
    decoded = json.loads(line.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


@contextlib.contextmanager
def _server() -> Iterator[subprocess.Popen[bytes]]:
    """Spawn the MCP server subprocess; tear it down on exit."""
    proc = subprocess.Popen(  # noqa: S603 — sys.executable is trusted, fixed args
        [sys.executable, "-m", _SERVER_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        if proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ValueError):
                proc.stdin.close()
        try:
            proc.wait(timeout=_RECV_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_RECV_TIMEOUT_SEC)


def test_initialize_returns_server_info() -> None:
    """``initialize`` returns the jarvis-tools server identity."""
    with _server() as proc:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        reply = _recv(proc)

    assert reply["id"] == 1
    assert reply["result"]["serverInfo"]["name"] == "jarvis-tools"
    assert reply["result"]["serverInfo"]["version"] == "0.1.0"
    assert "tools" in reply["result"]["capabilities"]
    assert "protocolVersion" in reply["result"]


def test_initialized_notification_no_reply() -> None:
    """``notifications/initialized`` is a notification — no JSON-RPC reply."""
    with _server() as proc:
        # Send the notification, then immediately a tools/list request.
        # If the server (incorrectly) replied to the notification, the
        # very next readline would return that reply instead of the
        # tools/list result. So a clean tools/list reply on the next
        # readline is the assertion.
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
        reply = _recv(proc)

    assert reply["id"] == 99
    assert "result" in reply


def test_tools_list_returns_submit_report() -> None:
    """``tools/list`` returns exactly one tool — ``submit_report``."""
    with _server() as proc:
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        reply = _recv(proc)

    tools = reply["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "submit_report"
    assert "MUST call this exactly once" in tool["description"]

    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["status", "summary"]

    # Every WorkerReport field per spec §3.5.8 / ADR § submit_report
    # tool injection is present in the schema.
    properties = schema["properties"]
    expected_property_names = {
        "status",
        "summary",
        "changed_files",
        "commands_run",
        "tests_run",
        "evidence_submitted",
        "remaining_risks",
        "needs_human_review",
        "next_recommended_action",
    }
    assert set(properties.keys()) == expected_property_names

    # Status enum carries exactly the four spec'd values.
    assert properties["status"]["enum"] == ["ok", "partial", "failed", "blocked"]

    # Boolean / string / array typing must match the contract; the L4
    # take_notification capture reads these fields verbatim.
    assert properties["summary"]["type"] == "string"
    assert properties["needs_human_review"]["type"] == "boolean"
    assert properties["remaining_risks"]["type"] == "string"
    assert properties["next_recommended_action"]["type"] == "string"
    for array_field in (
        "changed_files",
        "commands_run",
        "tests_run",
        "evidence_submitted",
    ):
        assert properties[array_field]["type"] == "array"
        assert properties[array_field]["items"]["type"] == "string"


def test_tools_call_echoes_structured_arguments() -> None:
    """``tools/call`` for ``submit_report`` echoes the args as structured content."""
    args: dict[str, Any] = {
        "status": "partial",
        "summary": "implemented but verify failed on edge case",
        "changed_files": ["jarvis/foo.py", "tests/unit/test_foo.py"],
        "commands_run": ["uv run pytest -x"],
        "tests_run": ["tests/unit/test_foo.py"],
        "evidence_submitted": ["events: action.result_observed"],
        "remaining_risks": "edge case at empty input",
        "needs_human_review": True,
        "next_recommended_action": "guard for empty input",
    }
    with _server() as proc:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "submit_report", "arguments": args},
            },
        )
        reply = _recv(proc)

    assert reply["id"] == 3
    result = reply["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == args
    # ``content`` is the human-visible text echo; the structured payload
    # is the load-bearing surface (L4 reads ``structuredContent`` via the
    # item/tool_call notification, not the text block).
    assert any(
        block.get("type") == "text" and "submit_report accepted" in block.get("text", "")
        for block in result["content"]
    )


def test_unknown_method_returns_method_not_found() -> None:
    """Bogus method names trigger JSON-RPC -32601 without killing the loop."""
    with _server() as proc:
        _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/bogus"})
        reply = _recv(proc)
        # Loop must still be alive — a follow-up request gets a normal reply.
        _send(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        follow_up = _recv(proc)

    assert reply["id"] == 4
    assert reply["error"]["code"] == -32601
    assert reply["error"]["message"] == "Method not found"
    assert "result" in follow_up


def test_unknown_tool_returns_invalid_params() -> None:
    """``tools/call`` with an unregistered tool name returns -32602."""
    with _server() as proc:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "not_a_real_tool", "arguments": {}},
            },
        )
        reply = _recv(proc)

    assert reply["id"] == 6
    assert reply["error"]["code"] == -32602
    assert "not_a_real_tool" in reply["error"]["message"]


def test_eof_exits_cleanly() -> None:
    """Closing stdin makes the server exit with code 0 (no stuck process)."""
    proc = subprocess.Popen(  # noqa: S603 — sys.executable trusted, fixed args
        [sys.executable, "-m", _SERVER_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.close()
        rc = proc.wait(timeout=_RECV_TIMEOUT_SEC)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_RECV_TIMEOUT_SEC)
    assert rc == 0
