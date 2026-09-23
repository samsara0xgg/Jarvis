"""ADR 0035: the shipped plugins/ directory, read the way the runtime reads it.

Asserts on what boot would connect to and list in the system prompt, and on the
``read_skill`` boundary: a skill's own files only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.execution.skill_reader import build_read_skill
from jarvis.execution.tools import ToolError
from jarvis.runtime.plugins import load_plugins

PLUGINS = Path(__file__).parent.parent.parent / "plugins"
SPEC = "notion:notion-spec-to-implementation"


def test_enabled_plugins_contribute_their_servers_and_skills() -> None:
    """Notion on with a Codex override, GitHub off: one server, four skills, one catalogue."""
    plugins = load_plugins(
        PLUGINS,
        {
            "notion": {"mcp_servers": {"notion": {"default_tools_approval_mode": "writes"}}},
            "github": {"enabled": False},
        },
    )
    assert plugins.servers == {
        "notion": {
            "type": "http",
            "url": "https://mcp.notion.com/mcp",
            "oauth_resource": "https://mcp.notion.com",
            "default_tools_approval_mode": "writes",
        }
    }
    assert set(plugins.skills) == {
        "notion:notion-knowledge-capture",
        "notion:notion-meeting-intelligence",
        "notion:notion-research-documentation",
        SPEC,
    }
    prompt = plugins.skills_prompt()
    assert prompt.startswith("## Skills")
    assert f"- {SPEC}: Turn Notion specs into implementation plans" in prompt
    assert load_plugins(PLUGINS, {}).skills_prompt() == ""


def test_github_plugin_reads_its_token_from_the_environment() -> None:
    """The GitHub server names an env var, never a token."""
    github = load_plugins(PLUGINS, {"github": {}}).servers["github"]
    assert github["url"] == "https://api.githubcopilot.com/mcp/"
    assert github["bearer_token_env_var"] == "GITHUB_PAT_TOKEN"  # noqa: S105 — a variable name.


def test_read_skill_stays_inside_the_skill() -> None:
    """SKILL.md and its references read; a sibling skill, the plugin root or /etc do not."""
    (reader,) = build_read_skill(load_plugins(PLUGINS, {"notion": {}}).skills)
    body = reader.handler({"name": SPEC}, None)  # type: ignore[arg-type]
    assert body["file"] == "SKILL.md"
    assert "reference/task-creation.md" in body["content"]
    ref = reader.handler({"name": SPEC, "file": "reference/task-creation.md"}, None)  # type: ignore[arg-type]
    assert ref["content"].startswith("#")
    for escape in ("../notion-knowledge-capture/SKILL.md", "../../.mcp.json", "/etc/hosts"):
        with pytest.raises(ToolError):
            reader.handler({"name": SPEC, "file": escape}, None)  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        reader.handler({"name": "notion:missing"}, None)  # type: ignore[arg-type]
