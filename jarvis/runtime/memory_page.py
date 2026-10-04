"""Runtime binding of the Dashboard's memory page (ADR 0154).

``jarvis.state.memory_page`` holds the reads and writes; this adds what only the runtime
knows: where ``memory.db`` is, where the prompt's history starts, and the cap the Settings
file keeps for the core memory note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.state import memory_page

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime.settings import Settings

DEFAULT_CAP: Final[int] = 4000


class MemoryPage:
    """The routes' callables, each blocking; the server runs them off the loop."""

    def __init__(self, path: Path, settings: Settings | None, *, history_since: str) -> None:
        """Bind the store, the Settings file and the ISO time the history starts at."""
        self._path = path
        self._settings = settings
        self._since = history_since

    def _cap(self) -> tuple[int, int]:
        return self._settings.core_memory_cap() if self._settings else (DEFAULT_CAP, DEFAULT_CAP)

    def overview(self) -> dict[str, Any]:
        """The 记着的 screen."""
        saved, booted = self._cap()
        return memory_page.overview(self._path, max_chars=saved, booted_max_chars=booted)

    def item(self, item_id: str) -> dict[str, Any]:
        """One item whole."""
        return memory_page.item(self._path, item_id)

    def edit(self, item_id: str, text: str, section: str | None) -> dict[str, Any]:
        """Edit or move an item (pinned)."""
        return memory_page.edit_item(self._path, item_id, text, section, max_chars=self._cap()[0])

    def delete(self, item_id: str) -> dict[str, Any]:
        """Delete an item."""
        return memory_page.delete_item(self._path, item_id, max_chars=self._cap()[0])

    def confirm(self, item_id: str) -> dict[str, Any]:
        """Confirm an item."""
        return memory_page.confirm_item(self._path, item_id, max_chars=self._cap()[0])

    def keep(self, version: str, item_id: str) -> dict[str, Any]:
        """Keep an item the night marked stale."""
        return memory_page.keep_stale(self._path, version, item_id, max_chars=self._cap()[0])

    def versions(self) -> dict[str, Any]:
        """The 改动 screen."""
        return memory_page.versions(self._path)

    def undo(self, version: str) -> dict[str, Any]:
        """Take back one version's changes."""
        return memory_page.undo(self._path, version, max_chars=self._cap()[0])

    def set_cap(self, max_chars: int) -> dict[str, Any]:
        """Save the cap in the Settings file; ValueError names a bad one."""
        low, high = memory_page.CAP_RANGE
        if self._settings is None:
            msg = "settings are not available"
            raise LookupError(msg)
        if not low <= max_chars <= high:
            msg = f"the cap is between {low} and {high} characters"
            raise ValueError(msg)
        self._settings.update({"core_memory_max_chars": max_chars})
        saved, booted = self._cap()
        return {"max_chars": saved, "booted_max_chars": booted}

    def search(self, query: str, who: str) -> dict[str, Any]:
        """Search every record since the history starts."""
        return memory_page.search(self._path, query=query, who=who, since=self._since)

    def days(self) -> dict[str, Any]:
        """The 日摘要 screen."""
        return memory_page.day_summaries(self._path)

    def day(self, day: str) -> dict[str, Any]:
        """One day's summary."""
        return memory_page.day_summary(self._path, day)

    def edit_day(self, day: str, sections: dict[str, list[str]]) -> dict[str, Any]:
        """Store the user's own day summary."""
        return memory_page.edit_day_summary(self._path, day, sections)

    def day_records(
        self,
        day: str,
        around: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """One day's conversation, a page at a time."""
        return memory_page.day_records(self._path, day, around=around, offset=offset, limit=limit)
