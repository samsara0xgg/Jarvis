"""The Dashboard's memory page (ADR 0154), all under ``/inherent/memory``.

- ``POST /inherent/memory/places`` — ADR 0234, 0296; body ``{"name", "lat", "lng", "role"?}``
  keeps a place at a point, as ``save_place`` does; ``{"ok", "place", "change"}``
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from fastapi import HTTPException
from pydantic import BaseModel, Field

from jarvis.state.memory_page import Conflict

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


class MemoryRoutes(Protocol):
    """ADR 0154: the Dashboard's memory page. Blocking calls (run off the loop thread).

    ``LookupError`` is a 404, ``ValueError`` a 400 and :class:`Conflict` a 409.
    """

    def overview(self) -> dict[str, Any]:
        """The six sections, last night's changes and the counts."""

    def item(self, item_id: str) -> dict[str, Any]:
        """One item whole."""

    def edit(self, item_id: str, text: str, section: str | None) -> dict[str, Any]:
        """Edit or move an item (it is pinned)."""

    def delete(self, item_id: str) -> dict[str, Any]:
        """Delete an item."""

    def confirm(self, item_id: str) -> dict[str, Any]:
        """Confirm an item."""

    def keep(self, version: str, item_id: str) -> dict[str, Any]:
        """Put back an item a nightly version marked stale."""

    def versions(self) -> dict[str, Any]:
        """One entry per core memory version."""

    def undo(self, version: str) -> dict[str, Any]:
        """Take back what a version changed."""

    def set_cap(self, max_chars: int) -> dict[str, Any]:
        """Save the note's character cap."""

    def search(self, query: str, who: str) -> dict[str, Any]:
        """Search the records since the history starts."""

    def days(self) -> dict[str, Any]:
        """One card per day summary."""

    def day(self, day: str) -> dict[str, Any]:
        """One day's summary."""

    def edit_day(self, day: str, sections: dict[str, list[str]]) -> dict[str, Any]:
        """Store the user's day summary."""

    def day_records(
        self, day: str, around: str | None, offset: int, limit: int,
    ) -> dict[str, Any]:
        """One day's conversation, a page at a time."""

    def places(self) -> dict[str, Any]:
        """The places he keeps with their facts, and the latest changes (ADR 0234)."""

    def place_add(self, name: str, lat: float, lng: float, role: str | None) -> dict[str, Any]:
        """Keep a place at a point (ADR 0296)."""

    def place_edit(
        self, place_id: str, name: str | None, role: str | None, *, forget: bool,
    ) -> dict[str, Any]:
        """Rename a kept place, change its role, or forget it."""

    def place_undo(self, change_id: str) -> dict[str, Any]:
        """Take back one place change."""


class MemoryEditRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/edit`` (ADR 0154): new words, a section to move to."""

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=2000)
    section: str | None = Field(default=None, max_length=40)


class MemoryItemRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/{delete,confirm}``: one item by id."""

    id: str = Field(min_length=1, max_length=64)


class MemoryKeepRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/keep``: an item the nightly ``version`` marked stale."""

    version: str = Field(min_length=1, max_length=80)
    id: str = Field(min_length=1, max_length=64)


class MemoryUndoRequest(BaseModel):
    """Body of ``POST /inherent/memory/undo``: the version whose changes to take back."""

    version: str = Field(min_length=1, max_length=80)


class MemoryCapRequest(BaseModel):
    """Body of ``POST /inherent/memory/cap``: the core memory note's character cap."""

    max_chars: int


class MemoryPlaceRequest(BaseModel):
    """Body of ``POST /inherent/memory/places/{id}`` (ADR 0234): a new name, role, or ``forget``."""

    name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=20)
    forget: bool = False


class MemoryPlaceAddRequest(BaseModel):
    """Body of ``POST /inherent/memory/places`` (ADR 0296): keep a place at a point."""

    name: str = Field(min_length=1, max_length=80)
    lat: float
    lng: float
    role: str | None = Field(default=None, max_length=20)


class MemoryPlaceUndoRequest(BaseModel):
    """Body of ``POST /inherent/memory/places/undo``: the change to take back."""

    change_id: str = Field(min_length=1, max_length=80)


class MemoryDayEditRequest(BaseModel):
    """Body of ``POST /inherent/memory/day/edit``: the lines under each heading of one day."""

    day: str = Field(min_length=10, max_length=10)
    sections: dict[str, list[str]]


async def _memory_call[T](call: Callable[[], T]) -> T:
    """ADR 0154: a memory page call off the loop; missing is 404, refused 400, blocked 409."""
    try:
        return await asyncio.to_thread(call)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from None
    except Exception as exc:  # noqa: BLE001 — a store that cannot be read is the page's 502.
        LOGGER.warning("memory route failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None


def register_memory_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0154: the Dashboard's memory page, all under ``/inherent/memory``."""
    if deps.memory_page is None:
        return
    page = deps.memory_page

    @app.get("/inherent/memory")
    async def memory_overview() -> dict[str, Any]:
        """The six sections, last night's changes and the counts."""
        return await _memory_call(page.overview)

    @app.get("/inherent/memory/item/{item_id}")
    async def memory_item(item_id: str) -> dict[str, Any]:
        """One item whole, with its sources as words and its history."""
        return await _memory_call(functools.partial(page.item, item_id))

    @app.post("/inherent/memory/item/edit", status_code=200)
    async def memory_edit(req: MemoryEditRequest) -> dict[str, Any]:
        """Edit or move an item: a user version, and the item is pinned."""
        return await _memory_call(functools.partial(page.edit, req.id, req.text, req.section))

    @app.post("/inherent/memory/item/delete", status_code=200)
    async def memory_delete(req: MemoryItemRequest) -> dict[str, Any]:
        """Delete an item: a user version that keeps what was removed."""
        return await _memory_call(functools.partial(page.delete, req.id))

    @app.post("/inherent/memory/item/confirm", status_code=200)
    async def memory_confirm(req: MemoryItemRequest) -> dict[str, Any]:
        """Say an item is right: a user version that changes no text."""
        return await _memory_call(functools.partial(page.confirm, req.id))

    @app.post("/inherent/memory/item/keep", status_code=200)
    async def memory_keep(req: MemoryKeepRequest) -> dict[str, Any]:
        """Put back an item a nightly version marked stale, pinned."""
        return await _memory_call(functools.partial(page.keep, req.version, req.id))

    @app.get("/inherent/memory/versions")
    async def memory_versions() -> dict[str, Any]:
        """One entry per core memory version, newest first."""
        return await _memory_call(page.versions)

    @app.post("/inherent/memory/undo", status_code=200)
    async def memory_undo(req: MemoryUndoRequest) -> dict[str, Any]:
        """Take back what a version changed, as a new version; 409 when a later one touched it."""
        return await _memory_call(functools.partial(page.undo, req.version))

    @app.post("/inherent/memory/cap", status_code=200)
    async def memory_cap(req: MemoryCapRequest) -> dict[str, Any]:
        """Save the note's character cap in settings; the nightly gate reads it at the next boot."""
        return await _memory_call(functools.partial(page.set_cap, req.max_chars))

    @app.get("/inherent/memory/places")
    async def memory_places() -> dict[str, Any]:
        """The places he keeps with their facts, and the latest changes (ADR 0234)."""
        return await _memory_call(page.places)

    @app.post("/inherent/memory/places", status_code=200)
    async def memory_place_add(req: MemoryPlaceAddRequest) -> dict[str, Any]:
        """Keep a place at a point under his name for it, as ``save_place`` does (ADR 0296)."""
        return await _memory_call(
            functools.partial(page.place_add, req.name, req.lat, req.lng, req.role),
        )

    @app.post("/inherent/memory/places/undo", status_code=200)
    async def memory_places_undo(req: MemoryPlaceUndoRequest) -> dict[str, Any]:
        """Take back a place change; 409 when a later one touched the same place."""
        return await _memory_call(functools.partial(page.place_undo, req.change_id))

    @app.post("/inherent/memory/places/{place_id}", status_code=200)
    async def memory_place(place_id: str, req: MemoryPlaceRequest) -> dict[str, Any]:
        """Rename a kept place, change its role, or forget it."""
        return await _memory_call(
            functools.partial(page.place_edit, place_id, req.name, req.role, forget=req.forget),
        )

    @app.get("/inherent/memory/search")
    async def memory_search(
        q: str = "", who: Literal["all", "user", "jarvis"] = "all",
    ) -> dict[str, Any]:
        """Every record with any of the words, grouped by day, newest first."""
        return await _memory_call(functools.partial(page.search, q[:200], who))

    @app.get("/inherent/memory/days")
    async def memory_days() -> dict[str, Any]:
        """One small card per day summary, newest first."""
        return await _memory_call(page.days)

    @app.get("/inherent/memory/day/{day}")
    async def memory_day(day: str) -> dict[str, Any]:
        """One day's summary under its headings."""
        return await _memory_call(functools.partial(page.day, day))

    @app.post("/inherent/memory/day/edit", status_code=200)
    async def memory_day_edit(req: MemoryDayEditRequest) -> dict[str, Any]:
        """Store the user's own version of a day summary; the night never rewrites it."""
        return await _memory_call(functools.partial(page.edit_day, req.day, req.sections))

    @app.get("/inherent/memory/day/{day}/records")
    async def memory_day_records(
        day: str, around: str | None = None, offset: int = 0, limit: int = 120,
    ) -> dict[str, Any]:
        """One day's conversation, oldest first, a page at a time."""
        return await _memory_call(functools.partial(page.day_records, day, around, offset, limit))
