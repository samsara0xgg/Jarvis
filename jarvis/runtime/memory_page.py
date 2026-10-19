"""Runtime binding of the Dashboard's memory page (ADR 0154).

``jarvis.state.memory_page`` holds the reads and writes; this adds what only the runtime
knows: where ``memory.db`` is, where the prompt's history starts, and the cap the Settings
file keeps for the core memory note.
"""

from __future__ import annotations

import time
from contextlib import closing
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory, memory_page, places
from jarvis.state.daily_report import resolve_zone
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.runtime.settings import Settings

DEFAULT_CAP: Final[int] = 40000


def _written(written: places.Written) -> dict[str, Any]:
    """A place write as the page reads it: the place and the change that holds it."""
    place = places.place_view(written.place, None)
    return {"ok": True, "place": place, "change": written.change_id}


class MemoryPage:
    """The routes' callables, each blocking; the server runs them off the loop."""

    def __init__(  # noqa: PLR0913 — the store, the Settings file, what the page is bound to.
        self, path: Path, settings: Settings | None, *, history_since: str,
        section_caps: Mapping[str, int] = core_memory.SECTION_CAPS,
        event_log: Path | None = None, zone: tzinfo | None = None,
    ) -> None:
        """Bind the store, the Settings file and the ISO time the history starts at.

        ``event_log`` (his phone's stays) and ``zone`` (the one days are cut in) give the places
        their facts (ADR 0234); without a log they have none.
        """
        self._path = path
        self._caps = section_caps
        self._settings = settings
        self._since = history_since
        self._event_log = event_log
        self._zone = zone or resolve_zone(None, None)[1]
        self.clock: Callable[[], float] = time.time

    def _cap(self) -> tuple[int, int]:
        return self._settings.core_memory_cap() if self._settings else (DEFAULT_CAP, DEFAULT_CAP)

    def _limits(self) -> dict[str, Any]:
        """The caps a user write is held to: the whole note's and each section's."""
        return {"max_chars": self._cap()[0], "section_caps": self._caps}

    def overview(self) -> dict[str, Any]:
        """The 记着的 screen."""
        saved, booted = self._cap()
        return memory_page.overview(
            self._path, max_chars=saved, booted_max_chars=booted, section_caps=self._caps,
        )

    def item(self, item_id: str) -> dict[str, Any]:
        """One item whole."""
        return memory_page.item(self._path, item_id)

    def edit(self, item_id: str, text: str, section: str | None) -> dict[str, Any]:
        """Edit or move an item (pinned)."""
        return memory_page.edit_item(self._path, item_id, text, section, **self._limits())

    def delete(self, item_id: str) -> dict[str, Any]:
        """Delete an item."""
        return memory_page.delete_item(self._path, item_id, **self._limits())

    def confirm(self, item_id: str) -> dict[str, Any]:
        """Confirm an item."""
        return memory_page.confirm_item(self._path, item_id, **self._limits())

    def keep(self, version: str, item_id: str) -> dict[str, Any]:
        """Keep an item the night marked stale."""
        return memory_page.keep_stale(self._path, version, item_id, **self._limits())

    def versions(self) -> dict[str, Any]:
        """The 改动 screen."""
        return memory_page.versions(self._path)

    def undo(self, version: str) -> dict[str, Any]:
        """Take back one version's changes."""
        return memory_page.undo(self._path, version, **self._limits())

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

    def places(self) -> dict[str, Any]:
        """The places he keeps with their facts, and the latest changes (ADR 0234)."""
        now = int(self.clock() * 1000)
        seen = []
        if self._event_log is not None:
            with closing(open_runtime_event_log(self._event_log)) as conn:
                seen = places.read_stays(conn, now, self._zone)
        return places.overview(self._path, seen, now, self._zone)

    def place_add(self, name: str, lat: float, lng: float, role: str | None) -> dict[str, Any]:
        """Keep a place at a point he gives (ADR 0296): ``save_place`` with a spot, origin ``page``.

        ``ValueError`` names a refusal; saving a place that is kept already under that name
        changes nothing (``change`` is null).
        """
        return _written(
            places.save(self._path, name=name, lat=lat, lng=lng, role=role, origin="page"),
        )

    def place_edit(
        self, place_id: str, name: str | None, role: str | None, *, forget: bool,
    ) -> dict[str, Any]:
        """Rename a kept place, change its role, or forget it; ValueError names a refusal."""
        if forget:
            if name is not None or role is not None:
                msg = "forget takes nothing else"
                raise ValueError(msg)
            written = places.forget(self._path, place_id, origin="page")
        else:
            written = places.edit(self._path, place_id, name=name, role=role, origin="page")
        return _written(written)

    def place_undo(self, change_id: str) -> dict[str, Any]:
        """Take back one place change; refused when a later one touched the same place."""
        return _written(places.undo(self._path, change_id, origin="page"))
