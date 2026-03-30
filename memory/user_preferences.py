"""Per-user persistent key-value preference store."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class UserPreferenceStore:
    """Persistent key-value preferences scoped per user.

    Stores personal facts, settings, and preferences that Claude can
    read and write via the memory skill.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        memory_config = config.get("memory", {})
        self.persist_dir = Path(memory_config.get("preferences_dir", "data/memory"))
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, Any]] = {}
        self.logger = LOGGER

    def get(self, user_id: str, key: str, default: Any = None) -> Any:
        """Get a single preference value.

        Args:
            user_id: User identifier.
            key: Preference key.
            default: Value to return if key is not found.

        Returns:
            The stored value or the default.
        """
        prefs = self.get_all(user_id)
        return prefs.get(key, default)

    def set(self, user_id: str, key: str, value: Any) -> None:
        """Set a single preference value and persist.

        Args:
            user_id: User identifier.
            key: Preference key.
            value: Value to store.
        """
        prefs = self._load(user_id)
        prefs[key] = value
        self._cache[user_id] = prefs
        self._save(user_id, prefs)
        self.logger.info("Set preference %s=%r for user %s", key, value, user_id)

    def delete(self, user_id: str, key: str) -> bool:
        """Delete a single preference key.

        Args:
            user_id: User identifier.
            key: Preference key to remove.

        Returns:
            True if the key existed and was removed.
        """
        prefs = self._load(user_id)
        if key not in prefs:
            return False
        del prefs[key]
        self._cache[user_id] = prefs
        self._save(user_id, prefs)
        self.logger.info("Deleted preference %s for user %s", key, user_id)
        return True

    def get_all(self, user_id: str) -> dict[str, Any]:
        """Get all preferences for a user.

        Args:
            user_id: User identifier.

        Returns:
            A dict of all stored preferences.
        """
        if user_id not in self._cache:
            self._cache[user_id] = self._load(user_id)
        return dict(self._cache[user_id])

    def _load(self, user_id: str) -> dict[str, Any]:
        """Load preferences from disk."""
        filepath = self._filepath(user_id)
        if not filepath.exists():
            return {}
        try:
            with filepath.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning("Failed to load preferences for %s: %s", user_id, exc)
            return {}

    def _save(self, user_id: str, prefs: dict[str, Any]) -> None:
        """Persist preferences to disk."""
        filepath = self._filepath(user_id)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with filepath.open("w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2, default=str)
        except OSError as exc:
            self.logger.warning("Failed to save preferences for %s: %s", user_id, exc)

    def _filepath(self, user_id: str) -> Path:
        """Resolve the JSON file path for a user."""
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.persist_dir / f"{safe_id}.json"
