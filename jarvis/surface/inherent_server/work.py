"""The Dashboard's work views: the current work state (ADR 0023) and the project view (ADR 0037).

- ``GET /inherent/work-state`` — ADR 0023 saved current-work-state record + data head
- ``POST /inherent/work-state/refresh`` — ADR 0023 on-demand analysis (single-flight)
- ``GET /inherent/projects`` — ADR 0037 seven-day project view (no model call)
- ``POST /inherent/projects/refresh`` — ADR 0037 sort new activities (single-flight)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


def register_work_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0023, 0037: the work-state record and the project view, each with its refresh."""
    if deps.work_state_read is not None and deps.work_state_refresh is not None:
        work_state_read, work_state_refresh = deps.work_state_read, deps.work_state_refresh

        @app.get("/inherent/work-state")
        async def work_state() -> dict[str, Any]:
            """ADR 0023: the saved record, the latest TimeSink head and whether a refresh runs."""
            return work_state_read()

        @app.post("/inherent/work-state/refresh", status_code=200)
        async def work_state_refresh_now() -> dict[str, Any]:
            """ADR 0023: analyse now (or join the running analysis), then answer like ``GET``."""
            return await work_state_refresh()

    if deps.projects_read is not None and deps.projects_refresh is not None:
        projects_read, projects_refresh = deps.projects_read, deps.projects_refresh

        @app.get("/inherent/projects")
        async def projects() -> dict[str, Any]:
            """ADR 0037: seven days per project, commits and recent activities; no model call."""
            return await projects_read()

        @app.post("/inherent/projects/refresh", status_code=200)
        async def projects_refresh_now() -> dict[str, Any]:
            """ADR 0037: sort the activities that have no answer yet, then answer like ``GET``."""
            return await projects_refresh()
