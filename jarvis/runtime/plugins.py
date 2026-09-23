"""Plugins in Codex's directory layout (ADR 0035).

A plugin is ``plugins/<name>/`` with ``.codex-plugin/plugin.json``, an
optional ``.mcp.json`` (``mcpServers``: name -> server entry) and optional
``skills/<skill>/SKILL.md``. ``tools.plugins.<name>`` switches one on and may
carry Codex's per-server overrides under ``mcp_servers.<server>``
(``default_tools_approval_mode``, ``tools.<tool>.approval_mode``). A server
Allen defines under ``tools.mcp.servers`` replaces a plugin server of the same
name, as Codex's user config outranks a plugin.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared.skills import skill_head

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Codex's skills catalogue prompt (ext/skills/src/catalog_prompt.rs), cut to what applies
# without a shell or subagents.
_SKILLS_HEADER: Final = (
    "## Skills\n"
    "A skill is a set of instructions stored in a `SKILL.md` file. Below is the list of skills "
    "that can be used. Each entry includes a name and a description.\n"
    "### Available skills\n"
    "{entries}\n"
    "### How to use skills\n"
    "- Trigger rules: If the user names a skill OR the task clearly matches a skill's "
    "description shown above, you must use that skill for that turn. Multiple mentions mean "
    "use them all. Do not carry skills across turns unless re-mentioned.\n"
    "- Missing/blocked: If a named skill isn't in the list or can't be read, say so briefly "
    "and continue with the best fallback.\n"
    "- How to use a skill: after deciding to use it, call `read_skill` with its name and read "
    "its `SKILL.md` completely before taking task actions. When `SKILL.md` points to other "
    "files such as `reference/…`, read the ones the task needs with `read_skill`'s `file` "
    "argument before acting on them.\n"
    "- Context hygiene: do not load unrelated references; prefer files directly linked from "
    "`SKILL.md`."
)


@dataclass(frozen=True)
class Plugins:
    """What the enabled plugins contribute: MCP servers and skills."""

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Server name -> entry, the plugin's ``.mcp.json`` with its config overrides applied."""
    sources: dict[str, str] = field(default_factory=dict)
    """Server name -> its plugin's description, for ``tool_search``'s source list."""
    skills: dict[str, Path] = field(default_factory=dict)
    """``<plugin>:<skill>`` -> the skill directory."""
    skill_descriptions: dict[str, str] = field(default_factory=dict)

    def skills_prompt(self) -> str:
        """Codex's skills catalogue for the system prompt, or "" when there is none."""
        if not self.skills:
            return ""
        entries = "\n".join(f"- {n}: {self.skill_descriptions[n]}" for n in sorted(self.skills))
        return _SKILLS_HEADER.format(entries=entries)


def load_plugins(root: Path, block: Mapping[str, Any]) -> Plugins:
    """Read every plugin ``block`` switches on; a broken one is skipped with a warning."""
    plugins = Plugins()
    for name, raw in block.items():
        entry = raw if isinstance(raw, dict) else {}
        if entry.get("enabled", True) is False:
            continue
        directory = root / str(name)
        try:
            manifest = json.loads(
                (directory / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            LOGGER.warning("plugin %r: missing or invalid plugin.json under %s", name, directory)
            continue
        description = str(manifest.get("description") or name)
        _add_servers(plugins, str(name), directory, entry.get("mcp_servers") or {}, description)
        for skill_md in sorted((directory / "skills").glob("*/SKILL.md")):
            head = skill_head(skill_md)
            key = f"{name}:{head['name']}"
            plugins.skills[key] = skill_md.parent
            plugins.skill_descriptions[key] = head["description"]
    return plugins


def _add_servers(
    plugins: Plugins,
    name: str,
    directory: Path,
    overrides: Mapping[str, Any],
    description: str,
) -> None:
    mcp_json = directory / ".mcp.json"
    if not mcp_json.is_file():
        return
    try:
        servers = json.loads(mcp_json.read_text(encoding="utf-8")).get("mcpServers") or {}
    except ValueError:
        LOGGER.warning("plugin %r: invalid .mcp.json; its servers are skipped", name)
        return
    for server, spec in servers.items():
        if server in plugins.servers:
            LOGGER.warning("plugin %r: server %r already comes from another plugin", name, server)
            continue
        plugins.servers[server] = {**spec, **(overrides.get(server) or {})}
        plugins.sources[server] = description
