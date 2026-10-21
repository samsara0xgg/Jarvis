"""The companion's home (ADR 0051): Today with its to-do checkbox, and the morning brief."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import BaseModel

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class TodoRequest(BaseModel):
    """Body of ``POST /inherent/today/todo`` (ADR 0051): the home's checkbox."""

    id: str
    done: bool


def register_today_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0051: Today and its to-do checkbox, when the home's reads are wired."""
    if deps.today_read is None or deps.todo_set is None:
        return
    today_read, todo_set = deps.today_read, deps.todo_set

    @app.get("/inherent/today")
    async def today() -> dict[str, Any]:
        """Today's calendar, open to-dos and the weather; no model call."""
        return await home_call(today_read())

    @app.post("/inherent/today/todo", status_code=200)
    async def todo(req: TodoRequest) -> dict[str, bool]:
        """Check a to-do off in Microsoft To Do, or open it again; its thing follows."""
        await home_call(todo_set(req.id, req.done))
        return {"ok": True}


def register_brief_route(app: FastAPI, deps: InherentDeps) -> None:
    """``GET /inherent/brief``, when ``deps.brief_read`` is wired."""
    if deps.brief_read is None:
        return
    brief_read = deps.brief_read

    @app.get("/inherent/brief")
    async def brief() -> dict[str, Any]:
        """This morning's brief; 404 until yesterday's report is saved."""
        found = brief_read()
        if found is None:
            raise HTTPException(status_code=404, detail="no brief for today yet")
        return found
