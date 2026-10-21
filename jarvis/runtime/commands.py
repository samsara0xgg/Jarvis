"""The one-shot commands that read the config and nothing of the daemon."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import webbrowser
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jarvis.execution.mcp_oauth import DEFAULT_OAUTH_CALLBACK_PORT
from jarvis.execution.mcp_tools import DEFAULT_MCP_TIMEOUT_S, McpServers, is_oauth, stdio_env
from jarvis.runtime.plugins import Plugins, load_plugins
from jarvis.state import imports
from jarvis.state.memory_db import open_memory_db
from jarvis.state.voiceprint import VoiceprintBook

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.config import (
        RuntimeConfig,
    )



def _has_no_browser() -> bool:
    """True on Linux with no display, where ``webbrowser`` would start lynx or w3m here."""
    return sys.platform != "darwin" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _print_only(_url: str) -> None:
    """The login's URL is already on stderr; open nothing."""


def _mcp_block(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """The `tools.mcp` block, or an empty mapping."""
    tools_block = config.get("tools")
    block = tools_block.get("mcp") if isinstance(tools_block, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _mcp_servers(
    block: Mapping[str, Any], paths: RuntimePaths, *, open_url: Callable[[str], object] | None
) -> McpServers:
    """One :class:`McpServers` per the block's knobs; tokens live under `<root>/mcp/`."""
    return McpServers(
        timeout_s=float(block.get("timeout_s", DEFAULT_MCP_TIMEOUT_S)),
        token_dir=paths.root / "mcp",
        callback_port=int(block.get("oauth_callback_port", DEFAULT_OAUTH_CALLBACK_PORT)),
        redirect_uri=block.get("oauth_redirect_uri") or None,
        open_url=open_url,
    )


def _plugins(config: Mapping[str, Any], repo_root: Path) -> Plugins:
    """ADR 0035: the plugins `tools.plugins` switches on, read from `<repo>/plugins/`."""
    tools_block = config.get("tools")
    block = tools_block.get("plugins") if isinstance(tools_block, Mapping) else None
    return load_plugins(repo_root / "plugins", block if isinstance(block, Mapping) else {})


def _all_mcp_servers(config: Mapping[str, Any], plugins: Plugins) -> dict[str, Any]:
    """Plugin servers, then `tools.mcp.servers`, which replaces a plugin server of its name."""
    servers = _mcp_block(config).get("servers")
    return {**plugins.servers, **(dict(servers) if isinstance(servers, Mapping) else {})}


def mcp_login(
    server: str,
    *,
    config: RuntimeConfig,
    paths: RuntimePaths,
    open_browser: bool = True,
) -> int:
    """ADR 0032: log one `auth: oauth` server in through the browser; the daemon reuses the token.

    ``open_browser`` False, or a machine with no desktop (a brain on a Raspberry Pi), prints the
    authorization URL and opens nothing: open it in a browser on another machine that reaches this
    one's callback port through ``ssh -L``. The token is still stored here (ADR 0170).

    A local server that keeps its own login names its login command in
    ``login_args`` (ADR 0055); that command runs here, in the terminal, with the
    entry's command and environment, so it asks for exactly what the daemon's
    server will use. Returns a process exit code: 0 logged in, 1 the server
    refused or never asked for a login, 2 the entry is missing or has no login;
    a login command's own exit code otherwise.
    """
    block = _mcp_block(config.raw)
    servers = _all_mcp_servers(config.raw, _plugins(config.raw, config.repo_root))
    spec = servers.get(server)
    if not isinstance(spec, Mapping):
        known = ", ".join(sorted(servers)) or "none"
        sys.stderr.write(
            f"mcp-login: no server {server!r} in `tools.mcp.servers` or an enabled plugin"
            f" of {config.config_path} (known: {known})\n"
        )
        return 2
    if spec.get("command") and spec.get("login_args"):
        login = [str(spec["command"]), *(os.path.expandvars(str(a)) for a in spec["login_args"])]
        env = {**os.environ, **stdio_env(spec)}
        return subprocess.run(login, env=env, cwd=spec.get("cwd"), check=False).returncode  # noqa: S603 — Allen's own config names the command.
    if not is_oauth(spec):
        sys.stderr.write(f"mcp-login: {server} does not log in with OAuth; nothing to do\n")
        return 2
    opener: Callable[[str], object] = webbrowser.open
    if not open_browser or _has_no_browser():
        opener = _print_only
        port = int(block.get("oauth_callback_port", DEFAULT_OAUTH_CALLBACK_PORT))
        sys.stderr.write(
            f"mcp-login: no browser is opened here. Open the URL below in a browser that reaches "
            f"this machine's 127.0.0.1:{port}, e.g. from a Mac: "
            f"ssh -L {port}:127.0.0.1:{port} <user>@<this host>\n"
        )

    mcp_servers = _mcp_servers(block, paths, open_url=opener)
    try:
        tools = mcp_servers.connect({server: spec})
    finally:
        mcp_servers.stop()
    token = mcp_servers.token_path(server)
    if not tools:
        sys.stderr.write(f"mcp-login: {server} did not come up; see the warning above\n")
        return 1
    if not mcp_servers.has_login(server):
        # Some servers answer tools/list unauthenticated; that is not a login.
        sys.stderr.write(
            f"mcp-login: {server} answered without asking for a login; no token stored\n"
        )
        return 1
    sys.stdout.write(
        f"mcp-login: {server} logged in, {len(tools)} tools; token at {token}. "
        "Restart the daemon to put them on the menu.\n"
    )
    return 0


def import_exports(
    source: str,
    files: Sequence[Path],
    *,
    config: RuntimeConfig,
    paths: RuntimePaths,
) -> imports.Counts:
    """ADR 0259: keep the files raw under the runtime root and read their entries into memory.db.

    Raises:
        ValueError: ``source`` has no parser yet.
        OSError: a file cannot be read or kept.
    """
    with contextlib.closing(open_memory_db(config.memory.db_path)) as conn:
        return imports.import_files(conn, paths.root / imports.IMPORTS_DIRNAME, source, files)


def open_voiceprints(config: RuntimeConfig) -> VoiceprintBook:
    """ADR 0286: the owner's profiles in this runtime's ``memory.db``, for the CLI."""
    return VoiceprintBook(config.memory.db_path)
