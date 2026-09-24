"""Runtime-owned plugin connection workflows for conversation and Resonance."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.execution.mcp_oauth import DEFAULT_OAUTH_CALLBACK_PORT
from jarvis.execution.mcp_tools import McpServers, is_oauth
from jarvis.execution.plugin_panel import plugin_panel_tools
from jarvis.execution.skill_reader import build_read_skill
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import DuplicateToolError, Tool, ToolContext, ToolError, ToolRegistry
from jarvis.runtime.plugin_catalog import (
    PluginPackage,
    credential_fields,
    discover_plugins,
    read_package,
    resolve_credentials,
)
from jarvis.runtime.plugins import Plugins, load_plugins
from jarvis.state.event_log import append_event_in_transaction, open_event_log
from jarvis.state.plugin_settings import PluginSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_BUSY = frozenset({"connecting", "authorizing"})


@dataclass
class _Active:
    client: McpServers
    tools: tuple[Tool, ...]
    plugins: Plugins


_MAX_CREDENTIAL_LENGTH = 8192
_LATEST_INPUT_SQL = (
    "SELECT MAX(id) FROM events WHERE type IN ('surface.user_intent', 'utterance.received') "
    "AND COALESCE(json_extract(payload_json, '$.channel'), '') != 'plugin_resume'"
)
_ORIGIN_SQL = (
    "SELECT id, event_uid, payload_json FROM events "
    "WHERE type IN ('surface.user_intent', 'utterance.received') "
    "AND correlation_id = ? ORDER BY id DESC LIMIT 1"
)


class PluginConnections:
    """One serialized user-started connection, with an atomically replaced tool group."""

    def __init__(  # noqa: PLR0913 — explicit composition-root dependencies
        self,
        *,
        repo_root: Path,
        runtime_root: Path,
        event_log: Path,
        registry: ToolRegistry,
        config: Mapping[str, Any],
        open_url: Callable[[str], object] = webbrowser.open,
    ) -> None:
        """Discover packages without authorizing; initialize() loads enabled ones."""
        self.root = runtime_root
        self._repo_root = repo_root
        self.event_log = event_log
        self.registry = registry
        self.settings = PluginSettings(runtime_root)
        self.packages = discover_plugins(repo_root, runtime_root)
        tools = config.get("tools") or {}
        self._config = dict(tools.get("plugins") or {})
        self._mcp = dict(tools.get("mcp") or {})
        self._direct = dict(self._mcp.get("servers") or {})
        claimed = {server: p.plugin_id for p in self.packages.values() for server in p.servers}
        for server, spec in self._direct.items():
            if server in claimed:
                owner = claimed[server]
                defaults = self._config.get(owner)
                self._config[owner] = {
                    **(defaults if isinstance(defaults, dict) else {}),
                    "enabled": True,
                }
            else:
                key = f"mcp-{server}"
                self.packages[key] = PluginPackage(key, None, {"name": server}, {server: spec}, 0)
                self._config[key] = {"enabled": True}
        self._open_url = open_url
        self._lock = threading.RLock()
        self._active: dict[str, _Active] = {}
        self._status: dict[str, tuple[str, str | None]] = {}
        self._owned: frozenset[str] = frozenset()
        self._request: dict[str, Any] | None = None
        self._opening: McpServers | None = None
        self._auth_url: str | None = None
        self._closed = False
        self._presentation = 0
        self.publish_event: Callable[[Any], None] | None = None

    def initialize(self) -> None:
        """Load enabled packages at boot without ever opening an authorization page."""
        for tool in plugin_panel_tools(self.catalog, self.request_from_tool):
            self.registry.register(tool)
        for plugin_id, package in list(self.packages.items()):
            if not self._enabled(plugin_id) or package.unsupported:
                continue
            active = None
            try:
                active = self._load(plugin_id, interactive=False)
                self._active[plugin_id] = active
                self._publish()
                self._status[plugin_id] = ("ready", None)
            except DuplicateToolError:
                self._active.pop(plugin_id, None)
                if active:
                    active.client.stop()
                self._status[plugin_id] = ("error", "插件工具名称冲突，请检查连接配置")
            except (OSError, ValueError, TypeError, RuntimeError):
                self._status[plugin_id] = ("needs_auth", "尚未接入，请连接或重新授权")

    def _enabled(self, plugin_id: str) -> bool:
        defaults = self._config.get(plugin_id)
        enabled = (
            (defaults.get("enabled", True) if isinstance(defaults, dict) else True)
            if plugin_id in self._config
            else False
        )
        return bool(self.settings.preferences.get(plugin_id, {}).get("enabled", enabled))

    def _specs(self, plugin_id: str) -> dict[str, dict[str, Any]]:
        package = self.packages[plugin_id]
        defaults = self._config.get(plugin_id) or {}
        overrides = defaults.get("mcp_servers") or {} if isinstance(defaults, dict) else {}
        mode = self.settings.preferences.get(plugin_id, {}).get("approval_mode")
        specs = {n: {**s, **overrides.get(n, {})} for n, s in package.servers.items()}
        for name, spec in list(specs.items()):
            if name in self._direct:
                specs[name] = dict(self._direct[name])
            if mode:
                specs[name] = {
                    **(self._direct.get(name) or spec),
                    "default_tools_approval_mode": mode,
                }
        return specs

    def _public(self, plugin_id: str) -> dict[str, Any]:
        package = self.packages[plugin_id]
        active = self._active.get(plugin_id)
        status, error = self._status.get(plugin_id, ("disconnected", None))
        if (
            self._request
            and self._request["plugin_id"] == plugin_id
            and self._request["state"] in _BUSY
        ):
            status = self._request["state"]
        specs = self._specs(plugin_id)
        modes = {s.get("default_tools_approval_mode", "auto") for s in specs.values()}
        configured_mode = (
            next(iter(modes)) if len(modes) == 1 else "configured" if modes else "auto"
        )
        metadata = PluginPackage(
            package.plugin_id,
            package.directory,
            package.manifest,
            specs,
            package.skill_count,
            package.unsupported,
        ).public()
        return {
            **metadata,
            "enabled": self._enabled(plugin_id),
            "status": status,
            "error": error,
            "approval_mode": self.settings.preferences.get(plugin_id, {}).get(
                "approval_mode", configured_mode
            ),
            "credentials_saved": bool(self.settings.credentials(plugin_id)),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "read_only": t.read_only,
                    "requires_confirmation": t.requires_confirmation,
                }
                for t in active.tools
            ]
            if active
            else [],
        }

    def read(self) -> dict[str, Any]:
        """Snapshot for the authenticated desktop; excludes every secret and auth URL."""
        with self._lock:
            request = (
                None
                if self._request is None
                else {k: v for k, v in self._request.items() if not k.startswith("_")}
            )
            return {"plugins": [self._public(n) for n in sorted(self.packages)], "request": request}

    def catalog(self) -> dict[str, Any]:
        """Small model-facing catalogue, including unavailable packages."""
        return {
            "plugins": [
                {
                    k: p[k]
                    for k in (
                        "id",
                        "name",
                        "description",
                        "status",
                        "supported",
                        "unavailable_reason",
                    )
                }
                for p in self.read()["plugins"]
            ]
        }

    def skills_prompt(self) -> str:
        """Current skill catalogue, sampled when a new decision turn begins."""
        with self._lock:
            return self._skills().skills_prompt()

    def _skills(self) -> Plugins:
        combined = Plugins()
        for active in self._active.values():
            combined.skills.update(active.plugins.skills)
            combined.skill_descriptions.update(active.plugins.skill_descriptions)
            combined.sources.update(active.plugins.sources)
        return combined

    def _publish(self) -> None:
        all_tools = tuple(t for a in self._active.values() for t in a.tools)
        skills = self._skills()
        group = (
            *all_tools,
            *build_tool_search(all_tools, skills.sources),
            *build_read_skill(skills.skills),
        )
        self.registry.replace_group(self._owned, group)
        self._owned = frozenset(t.name for t in group)

    def request_from_tool(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Bind a panel request to the real originating input, not generated task prose."""
        action = ctx.conn.execute(
            "SELECT correlation_id FROM events WHERE json_extract(payload_json, '$.action_id') = ? "
            "AND type = 'action.running' ORDER BY id DESC LIMIT 1",
            (ctx.action_id,),
        ).fetchone()
        origin = ctx.conn.execute(
            _ORIGIN_SQL,
            (action[0] if action else "",),
        ).fetchone()
        latest = ctx.conn.execute(_LATEST_INPUT_SQL).fetchone()[0]
        if origin is None or (latest is not None and latest > origin[0]):
            msg = "This request is no longer the current conversation"
            raise ToolError(msg, code="stale_request")
        data = json.loads(origin[2])
        with self._lock:
            self._present(str(args.get("plugin_id") or ""), str(args.get("purpose") or "")[:300])
            if self._request is None:
                msg = "Plugin panel request was not created"
                raise RuntimeError(msg)
            # Reopening an active request never changes the task the user already accepted.
            if self._request["state"] not in _BUSY:
                self._request.update(
                    {
                        "continue_task": args.get("continue_task") is True,
                        "_origin": origin[1],
                        "_latest_input": latest,
                        "_transcript": str(data.get("transcript", "")),
                        "_record_id": str(data.get("record_id") or origin[1]),
                        "_origin_channel": data.get("plugin_origin_channel", data.get("channel")),
                    }
                )
            return {
                "panel_opened": True,
                "plugin_id": self._request["plugin_id"],
                "state": self._request["state"],
                "instruction": (
                    "The panel is open. Wait for the user's connection action; "
                    "do not repeat this tool. If continue_task is true, the runtime resumes "
                    "automatically after connection. Do not ask the user to report when done."
                ),
            }

    def _present(self, plugin_id: str, purpose: str = "") -> None:
        if plugin_id not in self.packages:
            msg = "找不到这个插件，请从插件列表选择"
            raise ValueError(msg)
        if self._request and self._request["state"] in _BUSY:
            if self._request["plugin_id"] != plugin_id:
                msg = "请先完成或取消当前连接"
                raise ValueError(msg)
        else:
            self._request = {
                "id": uuid.uuid4().hex,
                "plugin_id": plugin_id,
                "purpose": purpose,
                "state": "ready" if plugin_id in self._active else "offered",
                "continue_task": False,
                "resume_status": "not_requested",
                "error": None,
            }
        self._presentation += 1
        self._request["presentation"] = self._presentation

    def action(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        """Authenticated UI commands; network setup runs outside the state lock."""
        with self._lock:
            if self._closed:
                msg = "Jarvis 正在关闭"
                raise ValueError(msg)
            if operation == "open":
                self._present(str(data.get("plugin_id") or ""))
                return self.read()
            request = self._request
            if not request or data.get("request_id") != request["id"]:
                msg = "连接请求已更新，请重新打开插件"
                raise ValueError(msg)
            stop = self._command(operation, request, data)
        if stop:
            stop.stop()
        return self.read()

    def _command(
        self, operation: str, request: dict[str, Any], data: dict[str, Any]
    ) -> McpServers | None:
        plugin_id = str(request["plugin_id"])
        if operation == "connect":
            self._start(request, data)
        elif operation == "cancel":
            request.update(state="cancelled", continue_task=False, resume_status="cancelled")
            self._auth_url = None
            stop, self._opening = self._opening, None
            return stop
        elif operation == "reopen":
            if request["state"] != "authorizing" or not self._auth_url:
                msg = "当前没有等待授权的页面"
                raise ValueError(msg)
            self._open_url(self._auth_url)
        elif operation == "disable":
            self._require_idle(request)
            self.settings.update(plugin_id, enabled=False)
            active = self._active.pop(plugin_id, None)
            self._publish()
            self._status[plugin_id] = ("disabled", None)
            request.update(state="offered", continue_task=False)
            return active.client if active else None
        elif operation == "approval":
            self._require_idle(request)
            self._approval(plugin_id, data.get("mode"))
        else:
            msg = "未知的插件操作"
            raise ValueError(msg)
        return None

    @staticmethod
    def _require_idle(request: dict[str, Any]) -> None:
        if request["state"] in _BUSY:
            msg = "请先完成或取消当前连接"
            raise ValueError(msg)

    def _start(self, request: dict[str, Any], data: dict[str, Any]) -> None:
        if request["state"] in _BUSY or request["state"] == "ready":
            return
        plugin_id = str(request["plugin_id"])
        if self.packages[plugin_id].unsupported:
            raise ValueError(self.packages[plugin_id].unsupported)
        credentials = data.get("credentials") or {}
        expected = {v for spec in self._specs(plugin_id).values() for v in credential_fields(spec)}
        if not isinstance(credentials, dict) or any(
            k not in expected or not isinstance(v, str) or len(v) > _MAX_CREDENTIAL_LENGTH
            for k, v in credentials.items()
        ):
            msg = "凭证输入无效"
            raise ValueError(msg)
        request.update(
            state="connecting",
            error=None,
            resume_status="pending" if request["continue_task"] else "not_requested",
        )
        self._auth_url = None
        threading.Thread(
            target=self._connect, args=(request, credentials), name="plugin-connect", daemon=True
        ).start()

    def _approval(self, plugin_id: str, mode: object) -> None:
        if mode not in ("auto", "prompt", "writes", "approve"):
            msg = "无效的操作审批设置"
            raise ValueError(msg)
        active = self._active.get(plugin_id)
        if active is None:
            self.settings.update(plugin_id, approval_mode=mode)
            return
        previous = active.tools
        active.tools = active.client.configured_tools(
            {
                name: {**spec, "default_tools_approval_mode": mode}
                for name, spec in self._specs(plugin_id).items()
            }
        )
        try:
            self._publish()
            self.settings.update(plugin_id, approval_mode=mode)
        except BaseException:
            active.tools = previous
            self._publish()
            raise

    def _install(self, plugin_id: str) -> PluginPackage:
        package = self.packages[plugin_id]
        if package.directory is None:
            return package
        destination = self.root / "plugins" / plugin_id
        if (
            package.directory.parent.name == "plugins"
            and package.directory.name == plugin_id
            and (
                package.directory.is_relative_to(self.root)
                or package.directory.parent.parent == self._repo_root
            )
        ):
            return package
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_name(f".{plugin_id}-{uuid.uuid4().hex}")
            try:
                shutil.copytree(package.directory, temp)
                temp.rename(destination)
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
        return read_package(destination)

    def _load(
        self,
        plugin_id: str,
        *,
        interactive: bool,
        request: dict[str, Any] | None = None,
        credentials: dict[str, str] | None = None,
    ) -> _Active:
        package = self._install(plugin_id)
        with self._lock:
            self.packages[plugin_id] = package
        values = {**self.settings.credentials(plugin_id), **(credentials or {})}
        specs = self._specs(plugin_id)
        for spec in specs.values():
            if any(
                not values.get(name) and not os.environ.get(name)
                for name in credential_fields(spec)
            ):
                msg = "请填写连接所需的凭证"
                raise ValueError(msg)
        resolved = {n: resolve_credentials(s, values) for n, s in specs.items()}

        def browser(url: str) -> object:
            with self._lock:
                if (
                    request is not self._request
                    or request is None
                    or request["state"] not in _BUSY
                    or self._closed
                ):
                    msg = "Connection cancelled"
                    raise RuntimeError(msg)
                self._auth_url = url
                request["state"] = "authorizing"
                return self._open_url(url)

        client = McpServers(
            timeout_s=float(self._mcp.get("timeout_s", 30)),
            token_dir=self.root / "mcp",
            callback_port=int(self._mcp.get("oauth_callback_port", DEFAULT_OAUTH_CALLBACK_PORT)),
            open_url=browser if interactive else None,
        )
        with self._lock:
            if interactive:
                if request is not self._request or request is None or request["state"] not in _BUSY:
                    client.stop()
                    msg = "Connection cancelled"
                    raise RuntimeError(msg)
                self._opening = client
        try:
            tools = client.connect(resolved)
            if set(resolved) != client.connected_servers:
                msg = "无法连接服务，请检查网络、凭证或重新授权"
                raise RuntimeError(msg)  # noqa: TRY301 — close the partially connected client below
            if any(is_oauth(s) and not client.has_login(n) for n, s in resolved.items()):
                msg = "授权尚未完成，请重新连接"
                raise RuntimeError(msg)  # noqa: TRY301 — close unauthenticated client below
            plugins = (
                load_plugins(package.directory.parent, {package.directory.name: {}})
                if package.directory
                else Plugins()
            )
            plugins.sources.update(
                {n: str(package.manifest.get("description") or plugin_id) for n in specs}
            )
            return _Active(client, tools, plugins)
        except BaseException:
            client.stop()
            raise

    def _connect(self, request: dict[str, Any], credentials: dict[str, str]) -> None:
        plugin_id = str(request["plugin_id"])
        active: _Active | None = None
        old: _Active | None = None
        try:
            active = self._load(
                plugin_id, interactive=True, request=request, credentials=credentials
            )
            with self._lock:
                if self._closed or request is not self._request or request["state"] not in _BUSY:
                    active.client.stop()
                    return
                old = self._active.get(plugin_id)
                self._active[plugin_id] = active
                try:
                    self._publish()
                    if credentials:
                        self.settings.save_credentials(
                            plugin_id, {**self.settings.credentials(plugin_id), **credentials}
                        )
                    self.settings.update(plugin_id, enabled=True)
                except BaseException:
                    if old:
                        self._active[plugin_id] = old
                    else:
                        self._active.pop(plugin_id, None)
                    self._publish()
                    raise
                self._opening = None
                self._auth_url = None
                self._status[plugin_id] = ("ready", None)
                request["state"] = "ready"
                if request["continue_task"]:
                    request["resume_status"] = self._resume(request)
        except (
            Exception,  # noqa: BLE001 — a worker failure becomes a sanitized UI result
            asyncio.CancelledError,
        ) as exc:  # worker failures become sanitized UI state
            if active and self._active.get(plugin_id) is not active:
                active.client.stop()
            with self._lock:
                if request is self._request and request["state"] in _BUSY:
                    # Transport errors can contain credentials; expose only our fixed messages.
                    error = (
                        str(exc)
                        if isinstance(exc, ValueError) and str(exc) == "请填写连接所需的凭证"
                        else "连接未完成，请检查网络、凭证或重新授权后重试"
                    )
                    request.update(state="error", error=error)
                    self._status[plugin_id] = ("error", error)
                    self._opening = None
                    self._auth_url = None
        finally:
            if old and self._active.get(plugin_id) is not old:
                old.client.stop()

    def _resume(self, request: dict[str, Any]) -> str:
        if not request.get("_origin") or not request.get("_transcript"):
            return "not_requested"
        conn = open_event_log(self.event_log)
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(_LATEST_INPUT_SQL).fetchone()[0]
            if latest != request.get("_latest_input"):
                conn.rollback()
                return "superseded"
            turn_id = f"T{uuid.uuid4().hex[:12]}"
            event = append_event_in_transaction(
                conn,
                type="surface.user_intent",
                event_uid=f"plugin-resume-{request['id']}",
                payload={
                    "transcript": request["_transcript"],
                    "turn_id": turn_id,
                    "channel": "plugin_resume",
                    "plugin_origin_channel": request.get("_origin_channel"),
                    "record_id": request["_record_id"],
                },
                source_event_id=request["_origin"],
                correlation={"turn_id": turn_id},
            )
            conn.commit()
            if self.publish_event:
                self.publish_event(event)
        except (sqlite3.Error, ValueError):
            conn.rollback()
            return "failed"
        finally:
            conn.close()
        return "continued"

    def stop(self) -> None:
        """Invalidate completion before closing clients during daemon shutdown."""
        with self._lock:
            self._closed = True
            clients = [a.client for a in self._active.values()]
            if self._opening:
                clients.append(self._opening)
        for client in clients:
            client.stop()
