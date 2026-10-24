"""Starting a turn from text: ``POST /inherent/submit`` and ``POST /inherent/share``.

- ``POST /inherent/submit`` — body ``{"text": str}`` → ``{"status": "accepted", "turn_id": str}``
  (ADR-0009 D2 added ``turn_id``; additive, so the inherent-swift client that reads only
  ``status`` is unaffected). With ``"attachments": [id]`` (ADR 0211) the files go with the words.
  With ``"topic": "<topic id>"`` (ADR 0390) the words are sent from that topic.
- ``POST /inherent/share`` — ADR 0211; a link, text or files from another app's share sheet,
  as a turn (``ask``) or kept for later.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from jarvis.shared.about import clean_about
from jarvis.shared.lang import t
from jarvis.state.shares import MAX_NOTE_CHARS, MAX_SHARE_TEXT_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS
from jarvis.state.topics import TOPIC_ID
from jarvis.surface.inherent_server.attachments import checked_attachments
from jarvis.surface.inherent_server.guards import paired_device

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class SubmitRequest(BaseModel):
    """Body of ``POST /inherent/submit``.

    Matches the legacy wire shape ``{"text": str}`` exactly so the
    inherent-swift client needs no changes. Pydantic enforces the type;
    a missing field returns 422, a non-string field returns 422, an
    empty string is accepted at the schema layer and rejected at the
    handler layer with 400.
    """

    text: str
    # ADR 0211: ids from ``POST /inherent/attachments``; the text may then be empty.
    attachments: list[str] = Field(default_factory=list)
    # ADR 0214: the item a paired phone has open (``jarvis.shared.about``); 422 if it is bad.
    about: dict[str, Any] | None = None
    # ADR 0262: the phone's id for this message, the one its ``/phone/ws`` ``say`` carries. The
    # same id sent again, over either route, is the same turn and writes nothing twice.
    utterance_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # ADR 0390: the topic he sent the words from (``t3``); a merged one's id names the topic it
    # went into, and an id that names no listed topic is a 422.
    topic: str | None = Field(default=None, pattern=rf"^{TOPIC_ID.pattern}$")

    @field_validator("about")
    @classmethod
    def _checked_about(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else clean_about(value)


class ShareRequest(BaseModel):
    """Body of ``POST /inherent/share`` (ADR 0211): one thing shared from another app.

    Exactly one of ``url``, ``text`` or ``attachments``; ``title`` goes with a url. With ``ask``
    the share is a turn, without it the share is kept for later.
    """

    url: str = ""
    title: str = ""
    text: str = ""
    note: str = ""
    attachments: list[str] = Field(default_factory=list)
    ask: bool = False


async def topic_of(deps: InherentDeps, topic: str | None) -> str | None:
    """The listed topic ``topic`` names now (ADR 0390), or the 422 that refuses it."""
    if topic is None:
        return None
    if deps.topic_resolve is None or deps.submit_in_topic is None:
        raise HTTPException(status_code=422, detail="this host keeps no topics")
    live = await asyncio.to_thread(deps.topic_resolve, topic)
    if live is None:
        raise HTTPException(status_code=422, detail=f"no topic {topic} is listed")
    return live


async def start_turn(  # noqa: PLR0913 - the phone message's fields, as `say` has them.
    deps: InherentDeps, device: str | None, text: str, ids: Sequence[str] = (),
    about: dict[str, Any] | None = None, utterance_id: str | None = None,
    topic: str | None = None, *, via: str | None = None,
) -> str:
    """Start the turn of ``text`` (with stored files ``ids``), written under ``device`` if any.

    ``about`` (ADR 0214) is the item the paired phone has open; the caller has refused it for
    anyone else. ``utterance_id`` (ADR 0262) names the message: a second call with it starts
    no second turn. ``topic`` (ADR 0390) is the listed topic it was sent from, already resolved
    by :func:`topic_of`. ``via`` (ADR 0401) is the watch the paired phone asked for.
    """
    if topic is not None and deps.submit_in_topic is not None:
        try:
            return await asyncio.to_thread(
                deps.submit_in_topic, device, text, ids, about, utterance_id, topic,
            )
        except ValueError as exc:  # only a named message is checked on the way in
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if device is not None and deps.submit_as_device is not None:
        try:
            return await asyncio.to_thread(
                deps.submit_as_device, device, text, ids, about, utterance_id, via,
            )
        except ValueError as exc:  # only a named message is checked on the way in
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if ids and deps.submit_attachments is not None:
        return await asyncio.to_thread(deps.submit_attachments, text, ids)
    return await asyncio.to_thread(deps.submit_callable, text) or ""


async def _submit_with_attachments(  # noqa: PLR0913 - the same fields.
    deps: InherentDeps, text: str, ids: Sequence[str], device: str | None,
    about: dict[str, Any] | None = None, utterance_id: str | None = None,
    topic: str | None = None,
) -> dict[str, str]:
    """``POST /inherent/submit`` with ``attachments``: the files go with the words (ADR 0211)."""
    unique = list(dict.fromkeys(ids))
    await checked_attachments(deps, unique)
    if deps.submit_attachments is None:  # checked_attachments refused already
        raise HTTPException(status_code=501, detail="attachments are not enabled")
    turn_id = await start_turn(
        deps, device, text or t("attach.no_words"), unique, about, utterance_id, topic,
    )
    return {"status": "accepted", "turn_id": turn_id}


def _share_fields(req: ShareRequest) -> dict[str, Any]:
    """The share's one subject and its limits (ADR 0211), or the 400 / 413 that refuses it."""
    url, title, text, note = req.url.strip(), req.title.strip(), req.text.strip(), req.note.strip()
    if sum(bool(one) for one in (url, text, req.attachments)) != 1:
        raise HTTPException(status_code=400, detail="send exactly one of url, text or attachments")
    if title and not url:
        raise HTTPException(status_code=400, detail="a title goes with a url")
    for value, limit, what in (
        (url, MAX_URL_CHARS, "url"), (title, MAX_TITLE_CHARS, "title"),
        (text, MAX_SHARE_TEXT_CHARS, "text"), (note, MAX_NOTE_CHARS, "note"),
    ):
        if len(value) > limit:
            raise HTTPException(status_code=413, detail=f"{what} is over {limit} characters")
    if url:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise HTTPException(status_code=400, detail="url must be an http(s) address")
    kind = "link" if url else "text" if text else "file"
    return {"kind": kind, "url": url, "title": title, "text": text, "note": note}


async def _share(
    deps: InherentDeps,
    share_callable: Callable[[dict[str, Any]], str],
    req: ShareRequest,
    device: str | None,
) -> dict[str, str]:
    """``POST /inherent/share``: a turn when he said ask her, otherwise one saved event."""
    fields = _share_fields(req)
    ids = list(dict.fromkeys(req.attachments))
    if ids:
        refs = await checked_attachments(deps, ids)
        fields["kind"] = "image" if any(r.kind == "image" for r in refs) else "file"
        fields["attachments"] = ids
    if not req.ask:
        share_id = await asyncio.to_thread(share_callable, fields)
        return {"status": "saved", "share_id": share_id}
    words = "\n".join(
        part
        for part in (
            fields["note"] or t("share.default_note"), fields["title"], fields["url"],
            fields["text"],
        )
        if part
    )
    return {"status": "accepted", "turn_id": await start_turn(deps, device, words, ids)}


def register_submit_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/submit`` (ADR-0003, ADR-0009 D2)."""

    @app.post("/inherent/submit", status_code=200)
    async def submit(req: SubmitRequest, request: Request) -> dict[str, str]:
        """Accept user text and hand off to the runtime via ``submit_callable``.

        ``req.text`` is stripped; an empty post-strip string returns
        400 so the inherent-swift client surfaces the "empty input"
        case explicitly rather than silently no-op-ing. The
        ``submit_callable`` is sync (SQLite write) — offloaded via
        ``asyncio.to_thread`` so the event loop stays free.

        ADR-0009 D2 wire change: the response echoes the minted
        ``turn_id`` so the one-shot CLI can correlate the WS stream it
        subscribed to BEFORE this POST. Additive — existing clients
        that read only ``status`` keep working.
        """
        text = req.text.strip()
        device = paired_device(deps, request)
        if req.about is not None and device is None:
            raise HTTPException(status_code=400, detail="about is for a paired device")
        if req.utterance_id is not None and device is None:
            raise HTTPException(status_code=400, detail="utterance_id is for a paired device")
        if not (text or req.attachments):
            raise HTTPException(status_code=400, detail="text required")
        topic = await topic_of(deps, req.topic)
        if req.attachments:
            return await _submit_with_attachments(
                deps, text, req.attachments, device, req.about, req.utterance_id, topic,
            )
        turn_id = await start_turn(deps, device, text, (), req.about, req.utterance_id, topic)
        return {"status": "accepted", "turn_id": turn_id}


def register_share_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/share`` (ADR 0211), when ``deps.share_callable`` is wired."""
    if deps.share_callable is None:
        return
    share_callable = deps.share_callable

    @app.post("/inherent/share", status_code=200)
    async def share(req: ShareRequest, request: Request) -> dict[str, str]:
        """ADR 0211: a share from another app, as a turn (``ask``) or kept for later."""
        return await _share(deps, share_callable, req, paired_device(deps, request))
