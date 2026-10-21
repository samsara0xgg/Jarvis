"""A phone's pictures and files: ``POST`` and ``GET /inherent/attachments``.

ADR 0211; multipart ``file`` → ``{"id", "kind", "name", ...}``, after which ``POST
/inherent/submit`` takes ``"attachments": [id]``. ADR 0255 serves a stored file back while it is
kept.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import UploadFile as FormFile

from jarvis.state.attachments import MAX_UPLOAD_BODY_BYTES, AttachmentRefused, valid_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI

    from jarvis.state.attachments import AttachmentRef, Attachments
    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def _refusal(exc: AttachmentRefused) -> HTTPException:
    """The HTTP answer for a file or a turn the attachment store will not take."""
    return HTTPException(status_code=exc.status, detail=str(exc))


async def checked_attachments(deps: InherentDeps, ids: Sequence[str]) -> list[AttachmentRef]:
    """The refs of a turn's attachments, or the 404 / 422 / 501 that says why not (ADR 0211)."""
    if deps.attachments is None or deps.submit_attachments is None:
        raise HTTPException(status_code=501, detail="attachments are not enabled on this daemon")
    if not all(valid_id(one) for one in ids):
        raise HTTPException(status_code=422, detail="an attachment id is malformed")
    try:
        return await asyncio.to_thread(deps.attachments.check_for_turn, ids)
    except AttachmentRefused as exc:
        raise _refusal(exc) from None


async def _upload_attachment(
    deps: InherentDeps, attachments: Attachments, request: Request,
) -> dict[str, Any]:
    """``POST /inherent/attachments``: one multipart ``file``, stored, answered with its id.

    The size is judged from ``Content-Length`` before a byte is read, which the server holds the
    body to, so a request without one (chunked) is refused outright.
    """
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        raise HTTPException(status_code=411, detail="send a Content-Length")
    if int(declared) > MAX_UPLOAD_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    async with request.form() as form:
        upload = form.get("file")
        if not isinstance(upload, FormFile):
            raise HTTPException(status_code=400, detail='send one file in the "file" field')
        data = await upload.read(MAX_UPLOAD_BODY_BYTES)
        name = upload.filename or ""
    try:
        ref = await asyncio.to_thread(attachments.save, data, name, images_ok=deps.images_ok)
    except AttachmentRefused as exc:
        raise _refusal(exc) from None
    if deps.attachment_saved is not None and ref.kind == "image":
        try:
            deps.attachment_saved(ref)
        except Exception:
            LOGGER.exception("attachment %s: could not start its upload", ref.id[:8])
    return {"id": ref.id, "kind": ref.kind, "name": ref.name, "bytes": ref.size, "mime": ref.mime}


def register_attachment_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0211, 0255: upload a file and read it back, when ``deps.attachments`` is wired."""
    if deps.attachments is None:
        return
    attachment_store = deps.attachments

    @app.post("/inherent/attachments", status_code=200)
    async def upload_attachment(request: Request) -> dict[str, Any]:
        """ADR 0211: one picture or text file from a phone, kept until it ages out."""
        return await _upload_attachment(deps, attachment_store, request)

    @app.get("/inherent/attachments/{attachment_id}")
    async def read_attachment(attachment_id: str) -> Response:
        """ADR 0255: a stored file's bytes, while it is kept; 404 once swept or never there."""
        ref = attachment_store.get(attachment_id)
        path = attachment_store.path(attachment_id)
        if ref is None or path is None:
            raise HTTPException(status_code=404, detail="the attachment is gone or unknown")
        return FileResponse(
            path, media_type=ref.mime, headers={"X-Content-Type-Options": "nosniff"},
        )
