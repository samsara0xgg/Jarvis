"""Read locally available plugin packages without activating their components."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.execution.mcp_tools import is_oauth

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
_ENV = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass(frozen=True)
class PluginPackage:
    """Metadata plus private local paths; only public() reaches the renderer."""

    plugin_id: str
    directory: Path | None
    manifest: dict[str, Any]
    servers: dict[str, dict[str, Any]]
    skill_count: int
    unsupported: str | None = None

    def public(self) -> dict[str, Any]:
        """Return descriptive metadata, never transport configuration or secrets."""
        interface = self.manifest.get("interface") or {}
        auth = {
            "oauth"
            if is_oauth(s)
            else "token"
            if credential_fields(s)
            else "local"
            if s.get("command")
            else "none"
            for s in self.servers.values()
        }
        return {
            "id": self.plugin_id,
            "name": str(
                interface.get("displayName") or self.manifest.get("name") or self.plugin_id
            ),
            "description": str(
                interface.get("shortDescription") or self.manifest.get("description") or ""
            ),
            "capabilities": [str(c) for c in interface.get("capabilities", [])],
            "skill_count": self.skill_count,
            "auth": next(iter(auth)) if len(auth) == 1 else "mixed" if auth else "none",
            "credential_fields": sorted(
                {v for s in self.servers.values() for v in credential_fields(s)}
            ),
            "supported": self.unsupported is None,
            "unavailable_reason": self.unsupported,
        }


def credential_fields(spec: dict[str, Any]) -> set[str]:
    """Find named credentials without exposing any configured values."""
    fields = {str(v) for v in (spec.get("env_http_headers") or {}).values()}
    if spec.get("bearer_token_env_var"):
        fields.add(str(spec["bearer_token_env_var"]))
    # A whole-value `$VAR` in env passes a credential; one inside a longer value
    # (PATH: "$HOME/...") is interpolation the daemon's environment supplies.
    for value in (spec.get("env") or {}).values():
        if whole := _ENV.fullmatch(str(value)):
            fields.add(whole[1] or whole[2])
    for mapping in (spec.get("headers"), spec.get("http_headers")):
        for value in (mapping or {}).values():
            fields.update(a or b for a, b in _ENV.findall(str(value)))
    return fields


def resolve_credentials(spec: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Apply private credential values to one client, never process-global env."""

    def expand(value: object) -> str:
        return _ENV.sub(
            lambda m: values.get(m[1] or m[2], os.environ.get(m[1] or m[2], m[0])), str(value)
        )

    resolved = dict(spec)
    for key in ("env", "headers", "http_headers"):
        if key in spec:
            resolved[key] = {k: expand(v) for k, v in spec[key].items()}
    headers = dict(resolved.get("http_headers") or {})
    if name := spec.get("bearer_token_env_var"):
        headers["Authorization"] = f"Bearer {values.get(str(name), os.environ.get(str(name), ''))}"
        resolved.pop("bearer_token_env_var", None)
    for header, var in (resolved.pop("env_http_headers", {}) or {}).items():
        headers[str(header)] = values.get(str(var), os.environ.get(str(var), ""))
    resolved["http_headers"] = headers
    return resolved


def _read_servers(directory: Path) -> dict[str, dict[str, Any]]:
    mcp = directory / ".mcp.json"
    servers = (
        json.loads(mcp.read_text(encoding="utf-8")).get("mcpServers", {}) if mcp.is_file() else {}
    )
    if not isinstance(servers, dict) or any(not isinstance(s, dict) for s in servers.values()):
        msg = "Invalid plugin server map"
        raise TypeError(msg)
    for spec in servers.values():
        for key in ("env", "headers", "http_headers", "env_http_headers"):
            if key in spec and not isinstance(spec[key], dict):
                msg = "Invalid plugin credential mapping"
                raise TypeError(msg)
    return servers


def read_package(directory: Path) -> PluginPackage:
    """Validate supported components before offering a Connect action."""
    manifest = json.loads((directory / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("interface", {}), dict):
        msg = "Invalid plugin manifest"
        raise TypeError(msg)
    if not isinstance((manifest.get("interface") or {}).get("capabilities", []), list):
        msg = "Invalid plugin capabilities"
        raise TypeError(msg)
    name = str(manifest.get("name") or directory.name)
    if not _NAME.fullmatch(name):
        msg = "Invalid plugin identifier"
        raise ValueError(msg)
    servers = _read_servers(directory)
    skill_count = len(list((directory / "skills").glob("*/SKILL.md")))
    reason = None
    if (directory / ".app.json").exists() and not servers:
        reason = "此插件依赖尚未接入的连接器网关"
    elif not servers and not skill_count:
        reason = "此插件没有 Jarvis 支持的工具或技能"
    elif any(
        not isinstance(s, dict) or not (s.get("url") or s.get("command")) for s in servers.values()
    ):
        reason = "此插件的连接配置暂不受支持"
    elif any(p.is_symlink() for p in directory.rglob("*")):
        reason = "此插件包含需要手动检查的文件链接"
    return PluginPackage(name, directory, manifest, servers, skill_count, reason)


def discover_plugins(repo_root: Path, runtime_root: Path) -> dict[str, PluginPackage]:
    """Installed packages override available local catalogue copies by identity."""
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    explicit = os.environ.get("JARVIS_PLUGIN_CATALOG")
    roots = [Path(explicit)] if explicit else [codex_root / ".tmp/plugins/plugins"]
    directories = [p for root in roots if root.is_dir() for p in sorted(root.iterdir())]
    # Installed Codex caches are optional sources; no network and no writes to them.
    if not explicit:
        directories += [
            p.parent.parent
            for p in sorted((codex_root / "plugins/cache").glob("*/*/*/.codex-plugin/plugin.json"))
        ]
    directories += [
        p
        for root in (runtime_root / "plugins", repo_root / "plugins")
        if root.is_dir()
        for p in sorted(root.iterdir())
    ]
    packages: dict[str, PluginPackage] = {}
    for directory in directories:
        try:
            package = read_package(directory)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        packages[package.plugin_id] = package
    return packages
