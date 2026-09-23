"""L4 — MCP servers as flat tools (ADR 0033, remote auth per ADR 0032).

Every configured or plugin server is entered once at registry build; each
tool it lists becomes a deferred :class:`Tool` named ``mcp__<server>__<tool>``
(ADR 0034), at ``L3`` with ``requires_confirmation`` when Codex's approval
rule for its mode says the call must be confirmed first.
The ``mcp`` SDK is asyncio-only and its client context manager must be
entered and exited from one task, so :class:`McpServers` owns a private loop
thread: one task per server holds the client open, and the synchronous
handlers hop onto that loop for every call.

A remote entry (``url``) authenticates with ``headers`` (``$VAR`` expanded
from the daemon environment) or with ``auth: oauth``, whose token file lives
under ``token_dir``; only a login command passes ``open_url``, so the daemon
can never open a browser.

Layer boundary (`.importlinter`): stdlib + ``mcp``/``httpx2`` plus
``jarvis.shared`` and this layer's own modules. No YAML is read here; the
composition root passes the ``servers`` mapping down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import AsyncExitStack, suppress
from typing import TYPE_CHECKING, Any, Final

import httpx2
from mcp import Client, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from jarvis.execution.mcp_oauth import (
    DEFAULT_OAUTH_CALLBACK_PORT,
    LOGIN_HINT,
    FileTokenStorage,
    build_oauth,
)
from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from mcp.client._transport import Transport

LOGGER = logging.getLogger(__name__)

DEFAULT_MCP_TIMEOUT_S: Final[float] = 30.0
"""``tools.mcp.timeout_s`` when unset: the read timeout of every list and call."""

_NAME_CHARS: Final = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME_CHARS: Final[int] = 64
"""The function-name limit of the OpenAI-compatible chat API every preset speaks."""

_JOIN_GRACE_S: Final[float] = 5.0
"""Slack past the SDK's own read timeout before a hop onto the loop gives up."""

_LOGIN_WAIT_S: Final[float] = 300.0
"""How long a login command waits for the browser before giving up."""

_HTTP_TIMEOUT: Final = httpx2.Timeout(30.0, read=300.0)
"""The SDK's own defaults: a server may hold the response stream open."""


APPROVAL_MODES: Final = frozenset({"auto", "prompt", "writes", "approve"})
"""Codex's per-server / per-tool approval modes (ADR 0033); ``auto`` when unset."""


def mcp_tool_name(server: str, name: str) -> str:
    """``mcp__<server>__<tool>``, squeezed into the chat API's function-name alphabet."""
    return _NAME_CHARS.sub("_", f"mcp__{server}__{name}")[:_MAX_NAME_CHARS]


def is_oauth(spec: Mapping[str, Any]) -> bool:
    """``auth: oauth``, or Codex's ``oauth`` / ``oauth_resource`` keys (ADR 0035)."""
    return str(spec.get("auth") or "").lower() == "oauth" or bool(
        spec.get("oauth") or spec.get("oauth_resource")
    )


def approval_mode(spec: Mapping[str, Any], tool_name: str) -> str:
    """``tools.<tool>.approval_mode``, else ``default_tools_approval_mode``, else ``auto``."""
    tools = spec.get("tools") or {}
    per_tool = tools.get(tool_name) if isinstance(tools, Mapping) else None
    mode = (
        (per_tool or {}).get("approval_mode") or spec.get("default_tools_approval_mode") or "auto"
    )
    if mode not in APPROVAL_MODES:
        msg = f"approval_mode {mode!r} for {tool_name!r}; expected one of {sorted(APPROVAL_MODES)}"
        raise ValueError(msg)
    return str(mode)


def needs_approval(annotations: mcp_types.ToolAnnotations | None, mode: str) -> bool:
    """Codex ``requires_mcp_tool_approval_for_mode``: must Allen confirm the call first?

    ``auto`` asks unless the server marks the tool read-only; a destructive
    tool always asks; a missing hint counts as the risky value, so only a
    tool marked both non-destructive and closed-world runs unasked.
    """
    read_only = bool(annotations and annotations.read_only_hint)
    if mode == "approve":
        return False
    if mode == "prompt":
        return True
    if mode == "writes":
        return not read_only
    destructive = annotations.destructive_hint if annotations else None
    if destructive is True:
        return True
    if read_only:
        return False
    open_world = annotations.open_world_hint if annotations else None
    return destructive is not False or open_world is not False


def _stdio(spec: Mapping[str, Any]) -> StdioServerParameters:
    env = spec.get("env") or {}
    return StdioServerParameters(
        command=str(spec["command"]),
        args=[str(a) for a in spec.get("args") or []],
        # `$VAR` keeps credentials in ~/.jarvis/env, the same indirection as api_key_env.
        env={str(k): os.path.expandvars(str(v)) for k, v in env.items()},
        cwd=spec.get("cwd"),
    )


def _payload(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """The dict the dispatcher serializes: structured content first, else the text blocks."""
    texts = [b.text for b in result.content if isinstance(b, mcp_types.TextContent)]
    if result.is_error:
        msg = "\n".join(texts) or "tool reported an error"
        raise ToolError(msg, code="mcp_tool_error")
    structured = result.structured_content
    if isinstance(structured, dict):
        return dict(structured)
    payload: dict[str, Any] = {"text": "\n".join(texts)}
    omitted = [b.type for b in result.content if not isinstance(b, mcp_types.TextContent)]
    if omitted:
        # ponytail: images/audio/resources are named, never carried; the model reads text.
        payload["omitted_blocks"] = omitted
    return payload


class McpServers:
    """Every entered MCP client and the loop thread that keeps them open."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_MCP_TIMEOUT_S,
        token_dir: Path | None = None,
        callback_port: int = DEFAULT_OAUTH_CALLBACK_PORT,
        open_url: Callable[[str], object] | None = None,
    ) -> None:
        """Start the loop thread; nothing connects until :meth:`connect`.

        ``token_dir`` holds one OAuth token file per server. ``open_url`` is the
        login command's browser; without it an OAuth server is only reused,
        never logged in.
        """
        self._timeout_s = timeout_s
        self._token_dir = token_dir
        self._callback_port = callback_port
        self._open_url = open_url
        self._wait_s = _LOGIN_WAIT_S if open_url else timeout_s + _JOIN_GRACE_S
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-loop", daemon=True)
        self._thread.start()
        self._stops: list[asyncio.Event] = []
        self._serving: list[Future[None]] = []

    def token_path(self, server: str) -> Path:
        """Where ``server``'s OAuth login lives; the login command reports it."""
        if self._token_dir is None:
            msg = "no token_dir: OAuth servers need one"
            raise ValueError(msg)
        return self._token_dir / f"{re.sub(r'[^\w-]', '_', server)[:128]}.json"

    def has_login(self, server: str) -> bool:
        """Whether ``server``'s file holds a token set; a timed-out login leaves a registration."""
        return FileTokenStorage(self.token_path(server)).has_tokens()

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the loop thread and wait for it here."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            self._timeout_s + _JOIN_GRACE_S
        )

    def _remote(self, server: str, spec: Mapping[str, Any]) -> tuple[Transport, httpx2.AsyncClient]:
        """Streamable HTTP with the entry's headers and, for an OAuth entry, its login."""
        url = os.path.expandvars(str(spec["url"]))
        headers = {
            str(k): os.path.expandvars(str(v))
            for k, v in {**(spec.get("http_headers") or {}), **(spec.get("headers") or {})}.items()
        }
        # Codex's .mcp.json spellings (ADR 0035): a token or header value named by an env var.
        if spec.get("bearer_token_env_var"):
            headers["Authorization"] = f"Bearer {os.environ.get(spec['bearer_token_env_var'], '')}"
        for header, var in (spec.get("env_http_headers") or {}).items():
            headers[str(header)] = os.environ.get(str(var), "")
        auth: httpx2.Auth | None = None
        if is_oauth(spec):
            path = self.token_path(server)
            if self._open_url is None and not self.has_login(server):
                msg = f"{server}: not logged in; {LOGIN_HINT.format(server=server)}"
                raise RuntimeError(msg)
            auth = build_oauth(
                server, url, path, callback_port=self._callback_port, open_url=self._open_url
            )
        http_client = httpx2.AsyncClient(headers=headers, auth=auth, timeout=_HTTP_TIMEOUT)
        return streamable_http_client(url, http_client=http_client), http_client

    def _open(self, server: str, spec: Mapping[str, Any]) -> Client:
        """Hold one client open in its own task until :meth:`stop`; return it once entered."""
        target: Transport | StdioServerParameters
        http_client: httpx2.AsyncClient | None = None
        if spec.get("url"):
            target, http_client = self._remote(server, spec)
        elif spec.get("command"):
            target = _stdio(spec)
        else:
            msg = "an MCP server entry needs `url` or `command`"
            raise ValueError(msg)
        ready: Future[Client] = Future()
        stop = asyncio.Event()
        read_timeout = _LOGIN_WAIT_S if self._open_url else self._timeout_s

        async def serve() -> None:
            try:
                async with AsyncExitStack() as stack:
                    if http_client is not None:
                        await stack.enter_async_context(http_client)
                    client = await stack.enter_async_context(
                        Client(target, read_timeout_seconds=read_timeout)
                    )
                    ready.set_result(client)
                    await stop.wait()
            except Exception as exc:
                if ready.done():
                    raise
                ready.set_exception(exc)

        self._stops.append(stop)
        self._serving.append(asyncio.run_coroutine_threadsafe(serve(), self._loop))
        return ready.result(self._wait_s)

    async def _list(self, client: Client) -> list[mcp_types.Tool]:
        tools: list[mcp_types.Tool] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    def connect(self, servers: Mapping[str, Mapping[str, Any]]) -> tuple[Tool, ...]:
        """Enter every server and return its tools; one that cannot be entered contributes none."""
        tools: list[Tool] = []
        for server, spec in servers.items():
            try:
                client = self._open(server, spec)
                listed = self._run(self._list(client))
                modes = [approval_mode(spec, one.name) for one in listed]
            except Exception:  # noqa: BLE001 — a missing binary, a refused URL, a hung handshake, a bad approval mode: warn, never fail boot.
                LOGGER.warning(
                    "mcp server %r unavailable; its tools stay off the menu", server, exc_info=True
                )
                continue
            pairs = zip(listed, modes, strict=True)
            tools.extend(self._wrap(server, client, one, mode) for one, mode in pairs)
            LOGGER.info("mcp server %r: %d tools", server, len(listed))
        return tuple(tools)

    def _wrap(self, server: str, client: Client, listed: mcp_types.Tool, mode: str) -> Tool:
        read_only = bool(listed.annotations and listed.annotations.read_only_hint)
        # ADR 0033: a call that needs approval sits at the confirmation threshold,
        # so the Pre-action Gate asks Allen before it runs.
        ask = needs_approval(listed.annotations, mode)

        def call(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
            try:
                result = self._run(client.call_tool(listed.name, dict(args)))
            except Exception as exc:
                # A dead or refusing server is a tool error, not a crash.
                msg = f"{server}: {type(exc).__name__}: {exc}"
                raise ToolError(msg, code="mcp_server") from exc
            return _payload(result)

        return Tool(
            name=mcp_tool_name(server, listed.name),
            description=listed.description or listed.title or listed.name,
            input_schema=listed.input_schema,
            handler=call,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L3" if ask else ("L0" if read_only else "L1"),
            read_only=read_only,
            requires_confirmation=ask,
            deferred=True,
        )

    def stop(self) -> None:
        """Exit every client on its own task, then stop the loop thread."""
        for stop in self._stops:
            self._loop.call_soon_threadsafe(stop.set)
        for serving in self._serving:
            with suppress(Exception):
                serving.result(_JOIN_GRACE_S)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(_JOIN_GRACE_S)
