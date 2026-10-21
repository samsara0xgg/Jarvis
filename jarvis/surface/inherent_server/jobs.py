"""The job ledger and the application tracker (ADR 0155, 0177, 0186)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class JobFlagRequest(BaseModel):
    """Body of ``POST /inherent/jobs/{message_id}/flag``."""

    reaction: str = Field(max_length=50)


class JobApplicationRequest(BaseModel):
    """Body of ``POST /inherent/jobs/applications`` (ADR 0177); ``applied_at`` is YYYY-MM-DD."""

    company: str = Field(max_length=200)
    role: str = Field(default="", max_length=200)
    applied_at: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, max_length=20)
    note: str = Field(default="", max_length=2000)


class JobApplicationEditRequest(BaseModel):
    """Body of ``POST /inherent/jobs/applications/{id}``: only the fields sent are changed."""

    status: str | None = Field(default=None, max_length=20)
    applied_at: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=2000)
    hidden: bool | None = None


def register_job_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0155, 0177, 0186: the ledger and the tracker, each route only when wired."""
    if deps.jobs_read is not None:
        jobs_read = deps.jobs_read

        @app.get("/inherent/jobs")
        async def jobs() -> dict[str, Any]:
            """``{ledger, applications, skipped}``: job mail by company, the tracker, held back."""
            return await home_call(jobs_read())

    if deps.application_add is not None:
        application_add = deps.application_add

        @app.post("/inherent/jobs/applications", status_code=200)
        async def application_add_route(req: JobApplicationRequest) -> dict[str, Any]:
            """Add an application Allen made that has no mail; returns its id."""
            return {"ok": True, "id": await home_call(application_add(req.model_dump()))}

    if deps.application_edit is not None:
        application_edit = deps.application_edit

        @app.post("/inherent/jobs/applications/{app_id}", status_code=200)
        async def application_edit_route(
            app_id: str, req: JobApplicationEditRequest
        ) -> dict[str, bool]:
            """Change an application's status, date, note or hidden; a bad status is a 400."""
            await home_call(application_edit(app_id, req.model_dump(exclude_unset=True)))
            return {"ok": True}

    if deps.application_cancel_reminders is not None:
        application_cancel_reminders = deps.application_cancel_reminders

        @app.post("/inherent/jobs/applications/{app_id}/cancel-reminders", status_code=200)
        async def application_cancel_reminders_route(app_id: str) -> dict[str, bool]:
            """Cancel an interview's two reminders and delete its Outlook event."""
            await home_call(application_cancel_reminders(app_id))
            return {"ok": True}

    if deps.job_delete is not None:
        job_delete = deps.job_delete

        @app.post("/inherent/jobs/{message_id}/delete", status_code=200)
        async def job_delete_route(message_id: str) -> dict[str, bool]:
            """Hide one mail from the ledger; nothing is deleted in Gmail."""
            await home_call(job_delete(message_id))
            return {"ok": True}

    if deps.job_flag is not None:
        job_flag = deps.job_flag

        @app.post("/inherent/jobs/{message_id}/flag", status_code=200)
        async def job_flag_route(message_id: str, req: JobFlagRequest) -> dict[str, bool]:
            """Allen says a held-back mail was job mail: read again, typed and delivered as such."""
            await home_call(job_flag(message_id, req.reaction))
            return {"ok": True}
