"""Task skills (ADR 0024): instruction bundles Jarvis loads at runtime.

A skill is a directory ``jarvis/shared/skills/<name>/`` holding ``SKILL.md``
(YAML-style frontmatter with ``name`` and ``description``, then the model's
instructions) and optional ``references/*.md`` appended to those instructions.
The frontmatter description is the trigger text a caller shows the decision
model; the body is what the task's own model call receives as its system
prompt. The procedure around the model call is code, never markdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_FENCE = "---"


@dataclass(frozen=True)
class Skill:
    """One loaded skill: its trigger description and its full instructions."""

    name: str
    description: str
    instructions: str


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        message = "SKILL.md must start with a frontmatter fence"
        raise ValueError(message)
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            head = {}
            for line in lines[1:index]:
                key, separator, value = line.partition(":")
                if separator and key.strip():
                    head[key.strip()] = value.strip()
            return head, "\n".join(lines[index + 1 :])
    message = "SKILL.md frontmatter is not closed"
    raise ValueError(message)


def skill_head(skill_md: Path) -> dict[str, str]:
    """The frontmatter of a ``SKILL.md`` outside this package (plugin skills, ADR 0035)."""
    head, _ = _frontmatter(skill_md.read_text(encoding="utf-8"))
    if not head.get("name") or not head.get("description"):
        message = f"{skill_md}: frontmatter needs a name and a description"
        raise ValueError(message)
    return head


def load_skill(name: str) -> Skill:
    """Read one skill directory; a malformed skill raises here, at boot, not at use."""
    root = files("jarvis.shared.skills") / name
    head, body = _frontmatter((root / "SKILL.md").read_text(encoding="utf-8"))
    if head.get("name") != name or not head.get("description"):
        message = f"skill {name}: frontmatter needs name={name} and a description"
        raise ValueError(message)
    parts = [body.strip()]
    references = root / "references"
    if references.is_dir():
        parts.extend(
            entry.read_text(encoding="utf-8").strip()
            for entry in sorted(references.iterdir(), key=lambda item: item.name)
            if entry.name.endswith(".md")
        )
    return Skill(name=name, description=head["description"], instructions="\n\n".join(parts))
