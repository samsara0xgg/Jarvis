"""L4 — ``read_skill``: the model opens a plugin skill it chose (ADR 0035).

Codex lists each plugin skill's name and description in the prompt and lets
the model read ``SKILL.md`` itself when a task matches. Jarvis's model has no
file tool, so this one reads a skill's ``SKILL.md``, or one file the skill
points at, and nothing outside that skill's directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

READ_SKILL_NAME: Final = "read_skill"
_MAX_RESULT_CHARS: Final[int] = 32_000

_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Skill name as listed under ## Skills."},
        "file": {
            "type": "string",
            "description": "A file the skill refers to, relative to its folder; omit for SKILL.md.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


def build_read_skill(skills: Mapping[str, Path]) -> tuple[Tool, ...]:
    """``read_skill`` over ``skills`` (name -> skill directory); none when there are none."""
    if not skills:
        return ()

    def handle(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("name") or "")
        root = skills.get(name)
        if root is None:
            msg = f"unknown skill {name!r}; known: {', '.join(sorted(skills))}"
            raise ToolError(msg, code="not_found")
        target = (root / str(args.get("file") or "SKILL.md")).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            msg = f"{args.get('file')!r} is not a file inside skill {name!r}"
            raise ToolError(msg, code="not_found")
        return {
            "name": name,
            "file": str(target.relative_to(root.resolve())),
            "content": target.read_text(encoding="utf-8"),
        }

    return (
        Tool(
            name=READ_SKILL_NAME,
            description=(
                "Read a skill listed under ## Skills: its SKILL.md, or a file it refers to."
            ),
            input_schema=_SCHEMA,
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            max_result_chars=_MAX_RESULT_CHARS,
        ),
    )
