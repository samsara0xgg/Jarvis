"""Allen's marks on his coding-agent sessions, one file every surface shares.

Which finished sessions he has not seen, which he parked for later and
which he archived (done with, kept to find again). The companion's notch
writes them; the Agents window reads and writes the same routes.
``GET /inherent/agent-marks`` serves :meth:`AgentMarks.read`,
``POST /inherent/agent-marks/{session_id}`` applies :meth:`AgentMarks.update`.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from pathlib import Path

# A mark nobody has touched for this long belongs to a session long gone.
KEEP_MS = 30 * 86_400_000


class AgentMarks:
    """Marks keyed by session id (Claude's session id, Codex's thread id)."""

    def __init__(self, path: Path) -> None:
        """Read the file once; a missing or broken one starts empty."""
        self._path = path
        self._lock = threading.Lock()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        marks = data.get("marks") if isinstance(data, dict) else None
        items = marks.items() if isinstance(marks, dict) else ()
        self._marks: dict[str, dict[str, Any]] = {k: v for k, v in items if isinstance(v, dict)}

    def read(self) -> dict[str, Any]:
        """``{"marks": {id: {unread, parked_ms, archived_ms, seen_ms, at}}}``."""
        with self._lock:
            return {"marks": {k: dict(v) for k, v in self._marks.items()}}

    def update(self, session_id: str, change: dict[str, Any]) -> dict[str, Any]:
        """Apply ``seen`` / ``unread`` / ``park`` / ``archive`` and return the session's marks.

        Each key sets only its own mark (``seen`` also ends unread); a
        surface that changes several at once sends them together.
        """
        now = int(time.time() * 1000)
        with self._lock:
            mark = self._marks.setdefault(session_id, {})
            if change.get("seen") is True:
                mark["unread"], mark["seen_ms"] = False, now
            if isinstance(change.get("unread"), bool):
                mark["unread"] = change["unread"]
            for key, field in (("park", "parked_ms"), ("archive", "archived_ms")):
                if isinstance(change.get(key), bool) and change[key] != bool(mark.get(field)):
                    mark[field] = now if change[key] else None
            mark["at"] = now
            self._marks = {
                k: v for k, v in self._marks.items() if now - int(v.get("at") or 0) < KEEP_MS
            }
            write_private_json(self._path, {"marks": self._marks})
            return dict(mark)
