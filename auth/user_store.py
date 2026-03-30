"""Persistent JSON-backed storage for enrolled speaker user profiles."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)


class UserStore:
    """Store enrolled user records in a JSON file on disk.

    Args:
        config: Application configuration dictionary. The store reads the `auth`
            section for the `user_store_path` setting.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the user store with the configured JSON path.

        Args:
            config: Parsed application configuration.
        """

        auth_config = config.get("auth", config)
        self.filepath = Path(auth_config.get("user_store_path", "data/users.json"))
        self.logger = LOGGER

    def add_user(self, user: dict[str, Any]) -> dict[str, Any]:
        """Add a new enrolled user record to the JSON store.

        Args:
            user: Full user record containing the required enrollment fields.

        Returns:
            The normalized record that was stored.

        Raises:
            ValueError: If the record is invalid or the user already exists.
        """

        normalized_user = self._validate_user_record(user, require_all_fields=True)
        data = self._read_data()
        if any(existing["user_id"] == normalized_user["user_id"] for existing in data["users"]):
            raise ValueError(f"User already exists: {normalized_user['user_id']}")

        data["users"].append(normalized_user)
        self._write_data(data)
        self.logger.info("Added enrolled user: %s", normalized_user["user_id"])
        return deepcopy(normalized_user)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """Get an enrolled user record by `user_id`.

        Args:
            user_id: The identifier to look up.

        Returns:
            The stored user record, or `None` if it does not exist.
        """

        normalized_user_id = user_id.strip()
        for user in self._read_data()["users"]:
            if user["user_id"] == normalized_user_id:
                return deepcopy(user)
        return None

    def get_all_users(self) -> list[dict[str, Any]]:
        """Return all enrolled user records."""

        return [deepcopy(user) for user in self._read_data()["users"]]

    def delete_user(self, user_id: str) -> bool:
        """Delete a user record by `user_id`.

        Args:
            user_id: The identifier of the user to delete.

        Returns:
            `True` when a user was deleted, else `False`.
        """

        data = self._read_data()
        normalized_user_id = user_id.strip()
        remaining_users = [user for user in data["users"] if user["user_id"] != normalized_user_id]
        if len(remaining_users) == len(data["users"]):
            return False

        data["users"] = remaining_users
        self._write_data(data)
        self.logger.info("Deleted enrolled user: %s", normalized_user_id)
        return True

    def update_user(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update a stored user record.

        Args:
            user_id: The identifier of the record to update.
            updates: Partial field updates. The `user_id` field cannot be changed.

        Returns:
            The updated normalized user record.

        Raises:
            KeyError: If the user does not exist.
            ValueError: If the update payload is invalid.
        """

        if "user_id" in updates and str(updates["user_id"]).strip() != user_id.strip():
            raise ValueError("Changing user_id is not supported.")

        data = self._read_data()
        normalized_user_id = user_id.strip()

        for index, user in enumerate(data["users"]):
            if user["user_id"] != normalized_user_id:
                continue

            merged_user = deepcopy(user)
            merged_user.update(deepcopy(updates))
            merged_user["user_id"] = normalized_user_id
            normalized_user = self._validate_user_record(merged_user, require_all_fields=True)
            data["users"][index] = normalized_user
            self._write_data(data)
            self.logger.info("Updated enrolled user: %s", normalized_user_id)
            return deepcopy(normalized_user)

        raise KeyError(f"User not found: {normalized_user_id}")

    def _read_data(self) -> dict[str, list[dict[str, Any]]]:
        """Read the JSON database from disk or return an empty structure."""

        if not self.filepath.exists():
            return {"users": []}

        try:
            with self.filepath.open("r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid user store JSON: {self.filepath}") from exc

        users = data.get("users", [])
        if not isinstance(users, list):
            raise RuntimeError(f"Invalid user store format in {self.filepath}")
        return {"users": users}

    def _write_data(self, data: dict[str, list[dict[str, Any]]]) -> None:
        """Persist the JSON database to disk with a stable file format."""

        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        with self.filepath.open("w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, ensure_ascii=False, indent=2)

    def _validate_user_record(
        self,
        user: dict[str, Any],
        require_all_fields: bool,
    ) -> dict[str, Any]:
        """Validate and normalize a user record before writing it to disk."""

        required_fields = {
            "user_id",
            "name",
            "embedding",
            "role",
            "permissions",
            "enrolled_at",
        }
        normalized_user = deepcopy(user)

        missing_fields = sorted(required_fields.difference(normalized_user))
        if require_all_fields and missing_fields:
            raise ValueError(f"Missing required user fields: {', '.join(missing_fields)}")

        normalized_user["user_id"] = str(normalized_user["user_id"]).strip()
        normalized_user["name"] = str(normalized_user["name"]).strip()
        normalized_user["role"] = str(normalized_user["role"]).strip()
        normalized_user["enrolled_at"] = str(normalized_user["enrolled_at"]).strip()
        normalized_user["permissions"] = [
            str(permission) for permission in normalized_user.get("permissions", [])
        ]

        if not normalized_user["user_id"]:
            raise ValueError("user_id must not be empty.")
        if not normalized_user["name"]:
            raise ValueError("name must not be empty.")
        if not normalized_user["role"]:
            raise ValueError("role must not be empty.")
        if not normalized_user["enrolled_at"]:
            raise ValueError("enrolled_at must not be empty.")

        embedding_array = np.asarray(normalized_user["embedding"], dtype=np.float32).reshape(-1)
        if embedding_array.size == 0:
            raise ValueError("embedding must not be empty.")
        normalized_user["embedding"] = embedding_array.astype(float).tolist()
        return normalized_user
