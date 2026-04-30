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
        ] = []
