"""Stable prefix builder — assembles the persistent context injected into every Cloud LLM call."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.core.store import MemoryStore

LOGGER = logging.getLogger(__name__)

_MAX_RECENT_TURNS = 10  # 10 turns = 20 messages (user + assistant each)

_PREAMBLE = (
    "The following observations are your memory of past conversations "
    "with the user. Newer observations supersede older ones. "
    "Reference specific details when relevant."
)


class StablePrefixBuilder:
    """Assembles the stable prefix string for Cloud LLM prompts.

    The prefix has a fixed structure (joined by blank lines):
      1. personality_text
      2. Preamble about observations
      3. <observations> block (chronological)
      4. Recent conversation turns (last 10 turns)
      5. Current user input

    Args:
        store: MemoryStore instance to read observations from.
        personality_text: System personality prompt from core/personality.py.
    """

    def __init__(self, store: MemoryStore, personality_text: str) -> None:
        self._store = store
        self._personality_text = personality_text

    def build(self, recent_turns: list[dict], current_input: str) -> str:
        """Assemble the full stable prefix string.

        Args:
            recent_turns: List of message dicts with 'role' and 'content' keys.
                Only messages with non-empty string content are included.
            current_input: The current user input text.

        Returns:
            The assembled prefix string, sections joined by double newlines.
        """
        sections: list[str] = []

        # 1. Personality
        sections.append(self._personality_text)

        # 2. Preamble
        sections.append(_PREAMBLE)

        # 3. Observations
        observations = self._store.get_all_observations()
        obs_lines = [o["content"] for o in observations if o.get("content")]
        sections.append(f"<observations>\n{chr(10).join(obs_lines)}\n</observations>")

        # 4. Recent turns (last _MAX_RECENT_TURNS turns = 2x messages)
        formatted_turns = self._format_recent_turns(recent_turns)
        if formatted_turns:
            sections.append(f"--- 最近对话 ---\n{formatted_turns}")

        # 5. Current input
        sections.append(f"--- 本轮 ---\n[user] {current_input}")

        return "\n\n".join(sections)

    def _format_recent_turns(self, turns: list[dict]) -> str:
        """Format recent turns, keeping only the last _MAX_RECENT_TURNS turns.

        Filters out messages with non-string or empty content.

        Args:
            turns: Full list of message dicts.

        Returns:
            Formatted string of recent conversation, or empty string if none.
        """
        # Filter to messages with non-empty string content
        valid = [
            t for t in turns
            if isinstance(t.get("content"), str) and t["content"]
        ]

        # Keep last N turns (N*2 messages)
        max_messages = _MAX_RECENT_TURNS * 2
        trimmed = valid[-max_messages:]

        if not trimmed:
            return ""

        lines = [f"[{t['role']}] {t['content']}" for t in trimmed]
        return "\n".join(lines)
