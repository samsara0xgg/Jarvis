"""Private plugin preferences and credentials, independent of operator YAML."""

from __future__ import annotations

import json
import os
import secrets
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace a private JSON document, including on first write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


class PluginSettings:
    """One process owns writes; callers receive copies, never the secret store."""

    def __init__(self, root: Path) -> None:
        """Recover preferences and create the desktop management credential."""
        self.root = root
        self._lock = threading.RLock()
        self.preferences = self._read("plugin-settings.json")
        self._credentials = self._read("plugin-credentials.json")
        access = self._read("plugin-access.json")
        self._token = str(access.get("token") or secrets.token_urlsafe(32))
        write_private_json(root / "plugin-access.json", {"token": self._token})

    def _read(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = f"Invalid plugin storage: {name}"
            raise TypeError(msg)
        return data

    def matches(self, authorization: str | None) -> bool:
        """Check the management credential without revealing it in responses."""
        return secrets.compare_digest(authorization or "", f"Bearer {self._token}")

    def update(self, plugin_id: str, **values: object) -> None:
        """Persist user choices without altering the YAML configuration."""
        with self._lock:
            next_preferences = {
                **self.preferences,
                plugin_id: {**self.preferences.get(plugin_id, {}), **values},
            }
            write_private_json(self.root / "plugin-settings.json", next_preferences)
            self.preferences = next_preferences

    def credentials(self, plugin_id: str) -> dict[str, str]:
        """Return only the requested plugin's stored input values."""
        with self._lock:
            return dict(self._credentials.get(plugin_id, {}))

    def save_credentials(self, plugin_id: str, values: dict[str, str]) -> None:
        """Store credentials privately; they never enter the conversation log."""
        with self._lock:
            data = {**self._credentials, plugin_id: values}
            write_private_json(self.root / "plugin-credentials.json", data)
            self._credentials = data
