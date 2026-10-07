"""L4 — MCP servers as flat tools (ADR 0033, remote auth per ADR 0032).

Every configured or plugin server is entered once at registry build; each
tool it lists becomes a deferred :class:`Tool` named ``mcp__<server>__<tool>``
(ADR 0034), at ``L3`` with ``requires_confirmation`` when Codex's approval
rule for its mode says the call must be confirmed first.
The ``mcp`` SDK is asyncio-only and its client context manager must be
entered and exited from one task, so :class:`McpServers` owns a private loop
thread: one task per server holds the client open, and the synchronous
handlers hop onto that loop for every call. Codex's ``enabled_tools`` narrows
a server's menu; a server that lists ``browser_snapshot`` runs every tool
behind the browser guard (ADR 0059). A server with Gmail's send and draft
tools gets a ``gmail_send`` that replies inside a thread and names the
signed-in address, and keeps ``gmail_createDraft`` off the menu (ADR 0063).

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
import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import AsyncExitStack, suppress
from email.utils import parseaddr
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import httpx2
from mcp import Client, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from jarvis.execution.browser_guard import SNAPSHOT_TOOL, guarded
from jarvis.execution.mcp_oauth import (
    DEFAULT_OAUTH_CALLBACK_PORT,
    LOGIN_HINT,
    FileTokenStorage,
    build_oauth,
)
from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Coroutine
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

_MAIL_TOOLS: Final = frozenset({"gmail_send", "gmail_createDraft", "gmail_sendDraft"})
"""Google's Workspace server lists these; its send has no thread, its draft does (ADR 0063)."""


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


def stdio_env(spec: Mapping[str, Any]) -> dict[str, str]:
    """The entry's ``env`` with ``$VAR`` expanded from the daemon environment."""
    # `$VAR` keeps credentials in ~/.jarvis/env, the same indirection as api_key_env.
    return {str(k): os.path.expandvars(str(v)) for k, v in (spec.get("env") or {}).items()}


def _stdio(spec: Mapping[str, Any]) -> StdioServerParameters:
    return StdioServerParameters(
        command=str(spec["command"]),
        # `$HOME` in a path keeps a machine's install location out of the shipped config.
        args=[os.path.expandvars(str(a)) for a in spec.get("args") or []],
        env=stdio_env(spec),
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
    if _error_text(payload["text"]):
        # Google's Workspace server answers a failure as {"error": ...} in a normal block.
        raise ToolError(payload["text"], code="mcp_tool_error")
    omitted = [b.type for b in result.content if not isinstance(b, mcp_types.TextContent)]
    if omitted:
        # ponytail: images/audio/resources are named, never carried; the model reads text.
        payload["omitted_blocks"] = omitted
    return payload


def _error_text(text: str) -> bool:
    """Whether a text block is exactly ``{"error": ...}``, a server's failure in a normal answer."""
    if not text.lstrip().startswith("{"):
        return False
    try:
        body = json.loads(text)
    except ValueError:
        return False
    return isinstance(body, dict) and body.keys() == {"error"} and bool(body["error"])


def _json_text(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON object a server answered in its text block."""
    body = json.loads(str(payload.get("text", "")))
    if not isinstance(body, dict):
        msg = f"answer is not a JSON object: {body!r}"
        raise ToolError(msg, code="mcp_tool_error")
    return body


def _own_address(call: Callable[[str, Mapping[str, Any]], dict[str, Any]]) -> str:
    """The signed-in Gmail address, read off the newest sent message; empty when unknown."""
    try:
        hits = _json_text(call("gmail_search", {"query": "in:sent", "maxResults": 1}))
        first = (hits.get("messages") or [{}])[0]
        if not first.get("id"):
            return ""
        sent = _json_text(call("gmail_get", {"messageId": first["id"], "format": "metadata"}))
    except (ToolError, ValueError, TypeError, AttributeError):
        return ""
    return parseaddr(str(sent.get("from") or ""))[1]


def _send_in_thread(
    send: Callable[[Mapping[str, Any]], dict[str, Any]],
    call: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    args: Mapping[str, Any],
) -> dict[str, Any]:
    """``gmail_send``; given a ``threadId``, a reply drafted into that thread, then sent."""
    if not args.get("threadId"):
        return send(args)
    draft = _json_text(call("gmail_createDraft", args))
    return call("gmail_sendDraft", {"draftId": str(draft.get("id", ""))})


def _mail_send(listed: mcp_types.Tool, address: str) -> tuple[str, dict[str, Any]]:
    """``gmail_send``'s description and schema with the reply thread and the sender (ADR 0063)."""
    schema = dict(listed.input_schema)
    schema["properties"] = {
        **(schema.get("properties") or {}),
        "threadId": {
            "type": "string",
            "description": "To reply, the threadId of the message you answer; the reply joins it.",
        },
    }
    own = f" The signed-in account, the user's own address, is {address}." if address else ""
    head = listed.description or listed.name
    description = f"{head} To reply to a message, pass its threadId.{own}"
    return description, schema


# Broad mail questions read ~10 full bodies (8 KB each) per turn; the search answer is ids only.
_MAIL_READ_HINTS: Final = {
    "gmail_search": (
        " It returns only message ids: to choose among them, gmail_get each with"
        ' format "metadata" (subject, sender, date, snippet), not "full".'
    ),
    "gmail_get": (
        ' Default format is "full", a whole body per message. To scan or pick among'
        ' messages use format "metadata"; open "full" only for the few the answer needs.'
    ),
}


class McpServers:
    """Every entered MCP client and the loop thread that keeps them open."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_MCP_TIMEOUT_S,
        token_dir: Path | None = None,
        callback_port: int = DEFAULT_OAUTH_CALLBACK_PORT,
        open_url: Callable[[str], object] | None = None,
        always_loaded: Collection[str] = (),
    ) -> None:
        """Start the loop thread; nothing connects until :meth:`connect`.

        ``token_dir`` holds one OAuth token file per server. ``open_url`` is the
        login command's browser; without it an OAuth server is only reused,
        never logged in. ``always_loaded`` names tools (``mcp__<server>__<tool>``)
        that stay on the model's menu without a ``tool_search`` (ADR 0127).
        """
        self._always_loaded = frozenset(always_loaded)
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
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop_lock = threading.Lock()
        self._stopped = False
        self.connected_servers: set[str] = set()
        self._listed: list[tuple[str, Client, mcp_types.Tool]] = []
        self._clients: dict[str, Client] = {}
        self._browsers: set[str] = set()
        self._mail: dict[str, str] = {}  # ADR 0063: mail server -> its signed-in address

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
        with self._stop_lock:
            if self._stopped:
                coro.close()
                msg = "MCP client has stopped"
                raise RuntimeError(msg)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(self._timeout_s + _JOIN_GRACE_S)

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

    def _open(  # noqa: C901 — transport selection and cancellation-aware client lifetime
        self, server: str, spec: Mapping[str, Any]
    ) -> Client:
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
            task = asyncio.current_task()
            if task is not None:
                self._tasks.add(task)
            try:
                async with AsyncExitStack() as stack:
                    if http_client is not None:
                        await stack.enter_async_context(http_client)
                    client = await stack.enter_async_context(
                        Client(target, read_timeout_seconds=read_timeout)
                    )
                    ready.set_result(client)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                elif not isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                if task is not None:
                    self._tasks.discard(task)

        with self._stop_lock:
            if self._stopped:
                msg = "MCP connection cancelled"
                raise RuntimeError(msg)
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
                every = self._run(self._list(client))
                # Codex's `enabled_tools`: only the named tools reach the menu.
                allowed = spec.get("enabled_tools")
                listed = [t for t in every if allowed is None or t.name in allowed]
                mail = {t.name for t in every} >= _MAIL_TOOLS
                if mail:
                    # ADR 0063: the card holds the draft; a reply goes out through gmail_send.
                    listed = [t for t in listed if t.name != "gmail_createDraft"]
                modes = [approval_mode(spec, one.name) for one in listed]
            except Exception:  # noqa: BLE001 — a missing binary, a refused URL, a hung handshake, a bad approval mode: warn, never fail boot.
                LOGGER.warning(
                    "mcp server %r unavailable; its tools stay off the menu", server, exc_info=True
                )
                continue
            self._clients[server] = client
            if any(t.name == SNAPSHOT_TOOL for t in every):
                self._browsers.add(server)  # ADR 0059
            if mail:
                self._mail[server] = _own_address(partial(self._call, server, client))
            pairs = zip(listed, modes, strict=True)
            tools.extend(self._wrap(server, client, one, mode) for one, mode in pairs)
            self.connected_servers.add(server)
            self._listed.extend((server, client, one) for one in listed)
            LOGGER.info("mcp server %r: %d tools", server, len(listed))
        return tuple(tools)

    def configured_tools(self, servers: Mapping[str, Mapping[str, Any]]) -> tuple[Tool, ...]:
        """Reapply approval preferences to live tools without another login."""
        return tuple(
            self._wrap(server, client, tool, approval_mode(servers[server], tool.name))
            for server, client, tool in self._listed
        )

    def _wrap(self, server: str, client: Client, listed: mcp_types.Tool, mode: str) -> Tool:
        read_only = bool(listed.annotations and listed.annotations.read_only_hint)
        # ADR 0033: a call that needs approval sits at the confirmation threshold,
        # so the Pre-action Gate asks Allen before it runs.
        ask = needs_approval(listed.annotations, mode)
        run: Callable[[Mapping[str, Any]], dict[str, Any]] = partial(
            self._call, server, client, listed.name
        )
        if server in self._browsers:
            run = guarded(listed.name, run, partial(self._snapshot, server, client))
        description = listed.description or listed.title or listed.name
        schema: Mapping[str, Any] = listed.input_schema
        if server in self._mail and listed.name == "gmail_send":
            run = partial(_send_in_thread, run, partial(self._call, server, client))
            description, schema = _mail_send(listed, self._mail[server])
        if server in self._mail and listed.name in _MAIL_READ_HINTS:
            description += _MAIL_READ_HINTS[listed.name]

        def call(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
            return run(args)

        name = mcp_tool_name(server, listed.name)
        return Tool(
            name=name,
            description=description,
            input_schema=schema,
            handler=call,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L3" if ask else ("L0" if read_only else "L1"),
            read_only=read_only,
            requires_confirmation=ask,
            deferred=name not in self._always_loaded,
        )

    def _call(
        self, server: str, client: Client, tool: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            result = self._run(client.call_tool(tool, dict(args)))
        except Exception as exc:
            # A dead or refusing server is a tool error, not a crash.
            msg = f"{server}: {type(exc).__name__}: {exc}"
            raise ToolError(msg, code="mcp_server") from exc
        return _payload(result)

    def _snapshot(self, server: str, client: Client) -> str:
        """The page as the browser guard sees it; a refused snapshot's text names a dialog."""
        try:
            payload = self._call(server, client, SNAPSHOT_TOOL, {})
            return "\n".join(v for v in payload.values() if isinstance(v, str))
        except ToolError as exc:
            return str(exc)

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Call one tool of a connected server outside a model turn (ADR 0036 and 0051 readers).

        No gate stands in front of this: callers pass read-only tools, and the one write is the
        to-do status Allen clicked on the home (ADR 0051).
        """
        client = self._clients.get(server)
        if client is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return self._call(server, client, tool, args)

    def stop(self) -> None:
        """Cancel pending authorization as well as entered clients, once."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

            async def shutdown() -> None:
                tasks = list(self._tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(_JOIN_GRACE_S)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(_JOIN_GRACE_S)
            if not self._thread.is_alive():
                self._loop.close()
