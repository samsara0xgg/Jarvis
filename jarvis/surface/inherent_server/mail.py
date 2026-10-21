"""The mail routes: the unread list, the Dashboard's mail page and the reply draft.

- ADR 0051, 0124: unread mail from people, and archiving the junk letters
- ADR 0147: the mail page, a letter whole with its read and trash taps
- ADR 0148: the reply draft under the open letter
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class MailArchiveRequest(BaseModel):
    """Body of ``POST /inherent/mail/archive`` and ``/unarchive`` (ADR 0124): Gmail message ids."""

    ids: list[str] = Field(max_length=20)


class MailDraftRequest(BaseModel):
    """Body of ``POST /inherent/mail/{id}/draft`` and ``/draft/send`` (ADR 0148): Allen's edit."""

    subject: str = Field(default="", max_length=300)
    body: str = Field(max_length=8000)


def register_mail_list_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0051, 0124: unread mail from people, and archiving junk letters."""
    if deps.mail_read is None:
        return
    mail_read = deps.mail_read

    @app.get("/inherent/mail")
    async def mail() -> dict[str, Any]:
        """Unread mail from people, newest first."""
        return await home_call(mail_read())

    if deps.mail_archive is not None:
        mail_archive = deps.mail_archive

        @app.post("/inherent/mail/archive", status_code=200)
        async def mail_archive_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Archive the junk letters Allen tapped; an id not offered as junk is a 400."""
            await home_call(mail_archive(req.ids, True))  # noqa: FBT003 — the route's body.
            return {"ok": True}

        @app.post("/inherent/mail/unarchive", status_code=200)
        async def mail_unarchive_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Undo: put letters archived by the route above back in the inbox."""
            await home_call(mail_archive(req.ids, False))  # noqa: FBT003 — the route's body.
            return {"ok": True}


def register_mail_page_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0147: a letter whole, its summary, read and trash, each only when wired."""
    if deps.mail_letter is not None:
        mail_letter = deps.mail_letter

        @app.get("/inherent/mail/{message_id}")
        async def mail_letter_route(message_id: str) -> dict[str, Any]:
            """One letter whole: headers and a plain-text body (ADR 0148)."""
            return await home_call(mail_letter(message_id))

    if deps.mail_summary is not None:
        mail_summary = deps.mail_summary

        @app.get("/inherent/mail/{message_id}/summary")
        async def mail_summary_route(message_id: str) -> dict[str, Any]:
            """``{summary}``: one sentence on the letter, written on demand and kept (ADR 0148)."""
            return await home_call(mail_summary(message_id))

    if deps.mail_mark_read is not None:
        mail_mark_read = deps.mail_mark_read

        @app.post("/inherent/mail/read", status_code=200)
        async def mail_read_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Mark the letters Allen opened read; an id not listed or open is a 400."""
            await home_call(mail_mark_read(req.ids, True))  # noqa: FBT003 — the route's body.
            return {"ok": True}

        @app.post("/inherent/mail/unread", status_code=200)
        async def mail_unread_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Mark letters unread again."""
            await home_call(mail_mark_read(req.ids, False))  # noqa: FBT003 — the route's body.
            return {"ok": True}

    if deps.mail_trash is not None:
        mail_trash = deps.mail_trash

        @app.post("/inherent/mail/trash", status_code=200)
        async def mail_trash_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Move letters to Gmail's Trash (recoverable there); never a permanent delete."""
            await home_call(mail_trash(req.ids, True))  # noqa: FBT003 — the route's body.
            return {"ok": True}

        @app.post("/inherent/mail/untrash", status_code=200)
        async def mail_untrash_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Undo: take letters out of the Trash and back into the inbox."""
            await home_call(mail_trash(req.ids, False))  # noqa: FBT003 — the route's body.
            return {"ok": True}


def register_mail_draft_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0148: the draft under the open letter, each route only when wired."""
    if deps.mail_draft_read is not None:
        draft_read = deps.mail_draft_read

        @app.get("/inherent/mail/{message_id}/draft")
        async def mail_draft_route(message_id: str) -> dict[str, Any]:
            """``{draft: {revision, to, subject, body, by} | null}`` under the letter."""
            return await home_call(draft_read(message_id))

    if deps.mail_draft_save is not None:
        draft_save = deps.mail_draft_save

        @app.post("/inherent/mail/{message_id}/draft", status_code=200)
        async def mail_draft_save_route(message_id: str, req: MailDraftRequest) -> dict[str, Any]:
            """Save Allen's own edit of the draft; answers like ``GET``."""
            return await home_call(draft_save(message_id, req.subject, req.body))

    if deps.mail_draft_send is not None:
        draft_send = deps.mail_draft_send

        @app.post("/inherent/mail/{message_id}/draft/send", status_code=200)
        async def mail_draft_send_route(message_id: str, req: MailDraftRequest) -> dict[str, bool]:
            """Save the edit and put the send card up; nothing leaves until he presses it."""
            await home_call(draft_send(message_id, req.subject, req.body))
            return {"ok": True}

    if deps.mail_draft_discard is not None:
        draft_discard = deps.mail_draft_discard

        @app.post("/inherent/mail/{message_id}/draft/discard", status_code=200)
        async def mail_draft_discard_route(message_id: str) -> dict[str, bool]:
            """Drop the draft."""
            await home_call(draft_discard(message_id))
            return {"ok": True}
