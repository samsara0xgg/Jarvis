"""``POST /inherent/view`` (ADR 0176): what the Dashboard shows, for the brain to know."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class ViewRow(BaseModel):
    """One row on the Dashboard's screen: its id and one line of title."""

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=500)
    mail_id: str = Field(default="", max_length=200)  # a Jobs row's newest mail (Gmail id)


class ViewItem(ViewRow):
    """The item open on the Dashboard: a ``mail``, ``memory``, ``agent``, ``plugin`` or ``job``."""

    kind: str = Field(min_length=1, max_length=24)


class ViewRequest(BaseModel):
    """Body of ``POST /inherent/view`` (ADR 0176): what the Dashboard shows; page null = closed."""

    page: str | None = Field(default=None, max_length=24)
    tab: str = Field(default="", max_length=24)
    item: ViewItem | None = None
    rows: list[ViewRow] = Field(default_factory=list, max_length=50)


def register_view_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/view``, when ``deps.view_set`` is wired."""
    if deps.view_set is None:
        return
    view_set = deps.view_set

    @app.post("/inherent/view", status_code=200)
    async def view(req: ViewRequest) -> dict[str, bool]:
        """What the Dashboard shows; repeated every 20 s, a null page closes (ADR 0176)."""
        item = None if req.item is None else (req.item.kind, req.item.id, req.item.title)
        rows = [(row.id, row.title, row.mail_id) for row in req.rows]
        view_set(req.page, req.tab, item, rows)
        return {"ok": True}
