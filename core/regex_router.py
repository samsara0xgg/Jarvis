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
                    tool_name="weather",
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
        ]
