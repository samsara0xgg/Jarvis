"""Per-user conversation history with sliding window and JSON persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class ConversationStore:
    """Manage per-user conversation history for Claude API interactions.

    Keeps a sliding window of recent messages in memory and persists
    to disk so conversations survive restarts.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        memory_config = config.get("memory", {})
        self.max_turns = int(memory_config.get("max_conversation_turns", 20))
        self.persist_dir = Path(memory_config.get("conversation_dir", "data/conversations"))
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self.logger = LOGGER

    def get_history(self, user_id: str) -> list[dict[str, Any]]:
        """Return the current conversation for a user.

        Loads from disk on first access, then serves from memory.

        Args:
            user_id: The user whose history to retrieve.

        Returns:
            A list of Claude API message dicts (role + content).
        """
        if user_id not in self._sessions:
            self._sessions[user_id] = self._load_from_disk(user_id)
        return list(self._sessions[user_id])

    def append(self, user_id: str, messages: list[dict[str, Any]]) -> None:
        """Append new messages to a user's conversation and persist.

        Args:
            user_id: The user whose history to update.
            messages: New message dicts to append.
        """
        session = self._sessions.setdefault(user_id, [])
        session.extend(messages)
        self._trim(user_id)
        self._save_to_disk(user_id)

    def replace(self, user_id: str, messages: list[dict[str, Any]]) -> None:
        """Replace a user's entire conversation history.

        Args:
            user_id: The user whose history to replace.
            messages: Full message list.
        """
        self._sessions[user_id] = list(messages)
        self._trim(user_id)
        self._save_to_disk(user_id)

    def clear(self, user_id: str) -> None:
        """Clear a user's conversation history.

        Args:
            user_id: The user whose history to clear.
        """
        self._sessions[user_id] = []
        self._save_to_disk(user_id)

    def _trim(self, user_id: str) -> None:
        """Trim conversation to the most recent turns."""
        session = self._sessions.get(user_id, [])
        max_messages = self.max_turns * 2
        if len(session) > max_messages:
            self._sessions[user_id] = session[-max_messages:]

    def _load_from_disk(self, user_id: str) -> list[dict[str, Any]]:
        """Load conversation from JSON file."""
        filepath = self._filepath(user_id)
        if not filepath.exists():
            return []
        try:
            with filepath.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            return data.get("messages", [])
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning("Failed to load conversation for %s: %s", user_id, exc)
            return []

    def _save_to_disk(self, user_id: str) -> None:
        """Persist conversation to JSON file."""
        filepath = self._filepath(user_id)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with filepath.open("w", encoding="utf-8") as f:
                json.dump(
                    self._sessions.get(user_id, []),
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        except OSError as exc:
            self.logger.warning("Failed to save conversation for %s: %s", user_id, exc)

    def _filepath(self, user_id: str) -> Path:
        """Resolve the JSON file path for a user."""
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.persist_dir / f"{safe_id}.json"
