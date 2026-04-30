"""Closed-trigger regex pre-filter for jarvis voice commands.

Single-step skill fast path. Matches user_text against ~17 strict
``^...$`` anchored patterns. On hit, returns RegexMatch with intent +
tool spec + TTS template fill. On miss, returns None — caller falls
through to cloud LLM.

Stateful / multi-step skills (cc_approve / cc_show / complete_todo /
delete_todo / cancel_all / sim devices) are intentionally excluded —
they need cloud LLM context.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegexMatch:
    """Result of a regex pattern hit.

    Caller flow: ``tool_registry.execute(tool_name, tool_args)`` →
    ``RegexRouter.render_response(match, tool_result)`` for TTS text.
    """

    pattern_id: str
    intent: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    template_key: str = ""
    template_vars: dict[str, str] = field(default_factory=dict)


class RegexRouter:
    """Fast-path router using a closed regex whitelist."""

    def __init__(self, config: dict) -> None:
        section = config.get("regex_router", {})
        self.device_alias: dict[str, str] = dict(section.get("device_alias", {}))
        self.templates: dict[str, list[str]] = dict(section.get("templates", {}))
        self._patterns: list[
            tuple[re.Pattern[str], Callable[[re.Match[str]], RegexMatch]]
        ] = self._compile()

    def match(self, text: str) -> RegexMatch | None:
        """Return the first pattern hit, or None on miss."""
        text = text.strip()
        for pattern, builder in self._patterns:
            m = pattern.match(text)
            if m is not None:
                return builder(m)
        return None

    def render_response(self, match: RegexMatch, tool_result: str) -> str:
        """Pick a random template for ``match.template_key`` and fill it.

        Variables: ``match.template_vars`` plus ``{tool_result}``. Falls
        back to raw ``tool_result`` if no templates are registered or if
        the chosen template references a missing variable.
        """
        templates = self.templates.get(match.template_key, [])
        if not templates:
            return tool_result
        template = random.choice(templates)
        variables = {**match.template_vars, "tool_result": tool_result}
        try:
            return template.format(**variables)
        except (KeyError, IndexError) as exc:
            LOGGER.warning(
                "Template %r missing variable for match %s: %s",
                match.template_key, match.pattern_id, exc,
            )
            return tool_result

    def _compile(
        self,
    ) -> list[tuple[re.Pattern[str], Callable[[re.Match[str]], RegexMatch]]]:
        return [
            (
                re.compile(r"^现在几点了?[?？]?$"),
                lambda m: RegexMatch(
                    pattern_id="get_current_time",
                    intent="get_current_time",
                    tool_name="get_current_time",
                    template_key="get_current_time",
                ),
            ),
            (
                re.compile(r"^今天几号$"),
                lambda m: RegexMatch(
                    pattern_id="get_date",
                    intent="get_date",
                    tool_name="get_current_time",
                    template_key="get_date",
                ),
            ),
            (
                re.compile(r"^今天天气怎么样$"),
                lambda m: RegexMatch(
                    pattern_id="weather",
                    intent="weather",
                    tool_name="get_weather",
                    template_key="weather",
                ),
            ),
            (
                re.compile(r"^我有什么(待办|todo)$"),
                lambda m: RegexMatch(
                    pattern_id="list_todos",
                    intent="list_todos",
                    tool_name="list_todos",
                    template_key="list_todos",
                ),
            ),
            (
                re.compile(r"^停cc$"),
                lambda m: RegexMatch(
                    pattern_id="cc_interrupt",
                    intent="cc_interrupt",
                    tool_name="cc_interrupt",
                    template_key="cc_interrupt",
                ),
            ),
            (
                re.compile(r"^加个todo[:：\s]+(.+)$"),
                lambda m: RegexMatch(
                    pattern_id="add_todo",
                    intent="add_todo",
                    tool_name="add_todo",
                    tool_args={"content": m.group(1).strip()},
                    template_key="add_todo",
                ),
            ),
            (
                re.compile(r"^记到inbox\s+(.+)$"),
                lambda m: RegexMatch(
                    pattern_id="obsidian_inbox",
                    intent="obsidian_inbox",
                    tool_name="obsidian_inbox",
                    tool_args={"content": m.group(1).strip()},
                    template_key="obsidian_inbox",
                ),
            ),
            (
                re.compile(r"^给cc发\s+(.+)$"),
                lambda m: RegexMatch(
                    pattern_id="cc_tell",
                    intent="cc_tell",
                    tool_name="cc_tell",
                    tool_args={"text": m.group(1).strip()},
                    template_key="cc_tell",
                ),
            ),
            (
                re.compile(r"^帮我输入[:：]\s*(.+)$"),
                lambda m: RegexMatch(
                    pattern_id="type_to_focused",
                    intent="type_to_focused",
                    tool_name="type_to_focused",
                    tool_args={"text": m.group(1)},
                    template_key="type_to_focused",
                ),
            ),
            (
                re.compile(r"^(\d+)\s*分钟(后)?提醒我$"),
                lambda m: RegexMatch(
                    pattern_id="set_timer",
                    intent="set_timer",
                    tool_name="set_timer",
                    tool_args={"seconds": int(m.group(1)) * 60, "label": "timer"},
                    template_key="set_timer",
                    template_vars={"minutes": m.group(1)},
                ),
            ),
            (
                re.compile(r"^(打开|开)(灯带|大灯|电脑灯|灯)$"),
                lambda m: RegexMatch(
                    pattern_id="smart_home_on_v_first",
                    intent="smart_home_control",
                    tool_name="smart_home_control",
                    tool_args={
                        "device_id": self.device_alias[m.group(2)],
                        "action": "turn_on",
                    },
                    template_key="smart_home_turn_on",
                    template_vars={"device": m.group(2)},
                ),
            ),
            (
                re.compile(r"^把(灯带|大灯|电脑灯|灯)打开$"),
                lambda m: RegexMatch(
                    pattern_id="smart_home_on_v_last",
                    intent="smart_home_control",
                    tool_name="smart_home_control",
                    tool_args={
                        "device_id": self.device_alias[m.group(1)],
                        "action": "turn_on",
                    },
                    template_key="smart_home_turn_on",
                    template_vars={"device": m.group(1)},
                ),
            ),
            (
                re.compile(r"^关(灯带|大灯|电脑灯|灯)$"),
                lambda m: RegexMatch(
                    pattern_id="smart_home_off_v_first",
                    intent="smart_home_control",
                    tool_name="smart_home_control",
                    tool_args={
                        "device_id": self.device_alias[m.group(1)],
                        "action": "turn_off",
                    },
                    template_key="smart_home_turn_off",
                    template_vars={"device": m.group(1)},
                ),
            ),
            (
                re.compile(r"^把(灯带|大灯|电脑灯|灯)(关掉|关了)$"),
                lambda m: RegexMatch(
                    pattern_id="smart_home_off_v_last",
                    intent="smart_home_control",
                    tool_name="smart_home_control",
                    tool_args={
                        "device_id": self.device_alias[m.group(1)],
                        "action": "turn_off",
                    },
                    template_key="smart_home_turn_off",
                    template_vars={"device": m.group(1)},
                ),
            ),
            (
                re.compile(r"^把(灯带|大灯|电脑灯|灯)调到百分之(\d+)$"),
                lambda m: RegexMatch(
                    pattern_id="smart_home_set_brightness",
                    intent="smart_home_control",
                    tool_name="smart_home_control",
                    tool_args={
                        "device_id": self.device_alias[m.group(1)],
                        "action": "set_brightness",
                        "value": m.group(2),
                    },
                    template_key="smart_home_set_brightness",
                    template_vars={"device": m.group(1), "value": m.group(2)},
                ),
            ),
            (
                re.compile(r"^切到(opus|sonnet|haiku)$"),
                lambda m: RegexMatch(
                    pattern_id="cc_slash_model",
                    intent="cc_slash",
                    tool_name="cc_slash",
                    tool_args={"command": "model", "args": m.group(1)},
                    template_key="cc_slash_model",
                    template_vars={"arg": m.group(1)},
                ),
            ),
            (
                re.compile(r"^effort\s+(low|medium|high|xhigh|max)$"),
                lambda m: RegexMatch(
                    pattern_id="cc_slash_effort",
                    intent="cc_slash",
                    tool_name="cc_slash",
                    tool_args={"command": "effort", "args": m.group(1)},
                    template_key="cc_slash_effort",
                    template_vars={"arg": m.group(1)},
                ),
            ),
        ]
