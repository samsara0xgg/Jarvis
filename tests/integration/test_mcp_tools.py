"""MCP acceptance: real servers as subprocesses, the real dispatcher and the SQLite log.

ADR 0031 (stdio server, tool naming, risk mapping, result rows) and ADR 0032
(a bearer header, an OAuth login through a loopback redirect, a daemon that
reuses and refreshes the stored token and never opens a browser).
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.mcp_tools import McpServers
from tests.integration.test_flat_tool_dispatch import _chain, _Fixture, _request

if TYPE_CHECKING:
    from collections.abc import Iterator

HERE = Path(__file__).parent
ECHO = {"echo": {"command": sys.executable, "args": [str(HERE / "mcp_echo_server.py")]}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_listening(port: int, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    msg = f"port {port} never came up"
    raise TimeoutError(msg)


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread; stopped after the test so the subprocess exits."""
    mcp = McpServers(timeout_s=20)
    yield mcp
    mcp.stop()


@pytest.fixture
def oauth_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """The OAuth-protected Streamable HTTP server, alive for one test."""
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 — our own test server script.
        [
            sys.executable,
            str(HERE / "mcp_oauth_server.py"),
            str(port),
            getattr(request, "param", "client_secret_post"),
        ]
    )
    try:
        _wait_listening(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _call(tmp_path: Path, tools: tuple[Any, ...], name: str, action_id: str) -> dict[str, Any]:
    """Dispatch through the real registry and return the decoded terminal payload."""
    fx = _Fixture(tmp_path / action_id, tools=tools)
    try:
        fx.dispatch(_request(name, action_id))
        observed = _chain(fx, action_id)
        assert observed["semantics"] == "observation"
        return dict(json.loads(observed["tool_output"]))
    finally:
        fx.close()


def test_listed_tools_dispatch_through_the_real_log(tmp_path: Path, servers: McpServers) -> None:
    """echo/add/boom arrive named, deferred and approval-mapped; each call ends in one row."""
    tools = servers.connect(ECHO)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__boom"}
    echo, add = by_name["mcp__echo__echo"], by_name["mcp__echo__add"]
    # ADR 0033 auto mode: readOnlyHint runs unasked; a tool with no hints asks first.
    assert (echo.read_only, echo.risk_level, echo.requires_confirmation) == (True, "L0", False)
    assert (add.read_only, add.risk_level, add.requires_confirmation) == (False, "L3", True)
    assert all(t.deferred for t in tools)  # ADR 0034
    assert add.input_schema["required"] == ["a", "b"]

    fx = _Fixture(tmp_path, tools=tools)
    try:
        fx.dispatch(_request("mcp__echo__echo", "M1", arguments={"text": "你好 MCP"}))
        observed = _chain(fx, "M1")
        assert observed["semantics"] == "observation"
        # mcp 2.x wraps a bare `str` return as structured content; that wins over the text block.
        assert json.loads(observed["tool_output"]) == {"result": "你好 MCP"}

        fx.dispatch(_request("mcp__echo__add", "M2", arguments={"a": 1, "b": 2}))
        observed = _chain(fx, "M2")
        assert json.loads(observed["tool_output"]) == {"sum": 3}

        fx.dispatch(_request("mcp__echo__boom", "M3"))
        observed = _chain(fx, "M3")
        assert observed["semantics"] == "error"
        assert observed["error"] == "mcp_tool_error"
        # mcp 2.x masks the server-side exception; the model sees the SDK's generic text.
        assert json.loads(observed["tool_output"])["error"] == "Error executing tool boom"
    finally:
        fx.close()


def test_codex_approval_modes_move_the_risk(servers: McpServers) -> None:
    """Server default and per-tool override, Codex's keys; an unknown mode keeps the server off."""
    spec = ECHO["echo"]
    approve_all = {
        "echo": {
            **spec,
            "default_tools_approval_mode": "approve",
            "tools": {"echo": {"approval_mode": "prompt"}},
        }
    }
    risks = {t.name: (t.risk_level, t.requires_confirmation) for t in servers.connect(approve_all)}
    assert risks == {
        "mcp__echo__echo": ("L3", True),
        "mcp__echo__add": ("L1", False),
        "mcp__echo__boom": ("L1", False),
    }
    assert servers.connect({"echo": {**spec, "default_tools_approval_mode": "sometimes"}}) == ()


def test_unreachable_server_contributes_nothing(servers: McpServers) -> None:
    """A missing binary is a warning, not a boot failure; the reachable server still lists."""
    tools = servers.connect({"ghost": {"command": "/nonexistent/mcp-server"}, **ECHO})
    assert {t.name.split("__")[1] for t in tools} == {"echo"}


def test_bearer_header_from_the_environment(
    tmp_path: Path, servers: McpServers, oauth_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No header: the 401 keeps the server off the menu. `$VAR` in headers: it lists and answers."""
    monkeypatch.setenv("MCP_TEST_TOKEN", "static-secret")
    assert servers.connect({"anon": {"url": oauth_url}}) == ()
    tools = servers.connect(
        {"tok": {"url": oauth_url, "headers": {"Authorization": "Bearer $MCP_TEST_TOKEN"}}}
    )
    assert {t.name for t in tools} == {"mcp__tok__whoami"}
    assert _call(tmp_path, tools, "mcp__tok__whoami", "H1") == {"tokens_issued": 0, "refreshes": 0}
    # Codex's .mcp.json spelling of the same header (ADR 0035).
    codex = servers.connect({"cdx": {"url": oauth_url, "bearer_token_env_var": "MCP_TEST_TOKEN"}})
    assert {t.name for t in codex} == {"mcp__cdx__whoami"}


def test_daemon_without_a_login_skips_the_oauth_server(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No token set: skipped before any connection; a registration alone is not a login."""
    token_dir = tmp_path / "mcp"
    spec = {"url": "http://127.0.0.1:9/mcp", "auth": "oauth"}
    daemon = McpServers(timeout_s=20, token_dir=token_dir, callback_port=_free_port())
    try:
        assert daemon.connect({"svc": spec}) == ()
        assert not token_dir.exists()
        # A timed-out login leaves the dynamic registration behind; still not a login.
        token_dir.mkdir()
        (token_dir / "svc.json").write_text(json.dumps({"client_info": {"client_id": "x"}}))
        assert daemon.connect({"svc": spec}) == ()
    finally:
        daemon.stop()
    reasons = [str(r.exc_info[1]) for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(reasons) == 2
    assert all("not logged in" in reason for reason in reasons)


@pytest.mark.parametrize(
    "oauth_url", ["client_secret_basic", "client_secret_post", "none"], indirect=True
)
def test_oauth_login_then_the_daemon_reuses_and_refreshes(tmp_path: Path, oauth_url: str) -> None:
    """Login through the loopback redirect; the daemon reuses the file, refreshes on expiry."""
    token_dir, port = tmp_path / "mcp", _free_port()
    spec = {"url": oauth_url, "auth": "oauth"}
    opened: list[str] = []

    def browser(url: str) -> None:
        opened.append(url)
        # A browser follows /authorize's redirect back to the loopback listener.
        threading.Thread(
            target=lambda: urllib.request.urlopen(url, timeout=10).read(),  # noqa: S310 — loopback test server.
            daemon=True,
        ).start()

    def daemon() -> McpServers:
        return McpServers(timeout_s=20, token_dir=token_dir, callback_port=port)

    login = McpServers(timeout_s=20, token_dir=token_dir, callback_port=port, open_url=browser)
    try:
        tools = login.connect({"svc": spec})
        assert {t.name for t in tools} == {"mcp__svc__whoami"}
        assert _call(tmp_path, tools, "mcp__svc__whoami", "O1") == {
            "tokens_issued": 1,
            "refreshes": 0,
        }
    finally:
        login.stop()
    assert len(opened) == 1
    path = token_dir / "svc.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(token_dir.stat().st_mode & 0o777) == "0o700"
    stored = json.loads(path.read_text())
    assert stored["tokens"]["access_token"] == "at-1"  # noqa: S105 — the test server's counter.
    assert stored["tokens"]["refresh_token"] == "rt-1"  # noqa: S105
    assert stored["client_info"]["client_id"]
    assert stored["expires_at"] > time.time()

    # Logged in: the daemon reuses the token without a browser.
    warm = daemon()
    try:
        tools = warm.connect({"svc": spec})
        assert _call(tmp_path, tools, "mcp__svc__whoami", "O2") == {
            "tokens_issued": 1,
            "refreshes": 0,
        }
    finally:
        warm.stop()

    # Expired: the daemon refreshes before its first request and rewrites the file.
    stored["expires_at"] = time.time() - 60
    path.write_text(json.dumps(stored))
    stale = daemon()
    try:
        tools = stale.connect({"svc": spec})
        assert _call(tmp_path, tools, "mcp__svc__whoami", "O3") == {
            "tokens_issued": 2,
            "refreshes": 1,
        }
    finally:
        stale.stop()
    refreshed = json.loads(path.read_text())
    assert refreshed["tokens"]["access_token"] == "at-2"  # noqa: S105
    assert refreshed["expires_at"] > time.time()
    assert len(opened) == 1
