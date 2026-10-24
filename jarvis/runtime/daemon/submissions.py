"""What the daemon's input routes write: typed words, files, a device's message, a PTT clip."""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.turn.start import new_turn_id
from jarvis.state import reactions
from jarvis.state.event_log import MAC_NODE, emit_event, open_runtime_event_log
from jarvis.state.input_submission_inbox import (
    InputReceipt,
    PayloadConflictError,
    SubmissionInProgressError,
    SubmissionKey,
    claim_asr_request,
    release_asr_request,
    resolve_asr_request,
    submit_text_once,
)
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_server.v2 import InputSubmissionOutcome
from jarvis.surface.terminal_events import BrainEvents, phone_turn_id

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from jarvis.runtime.picture_notes import PictureNotes
    from jarvis.shared import Event


V2_PRINCIPAL: Final[str] = "inherent_v2"
"""The ADR-0014 D21 ``authenticated_principal``.

The v2 input routes authenticate one shared per-boot bearer token, not a user, so
this constant names that credential rather than a person.  The idempotency key
still separates clients through ``client_instance_id``; a per-user principal
arrives with a real identity mechanism.
"""


def submit_text(event_log_path: Path, text: str) -> str:
    """``POST /inherent/submit``: Allen's typed words as a turn. Mints a fresh ``turn_id`` per call.

    Runs on whatever thread :func:`asyncio.to_thread` dispatches
    it onto (see ``/inherent/submit`` handler in Step 7). The
    handler thread is NOT the event-loop thread, so to honor
    ``check_same_thread`` we open a fresh ``sqlite3.Connection``
    to the same DB file for the SQLite write.

    **Returns the minted id** (ADR-0009 D2): it is what
    ``POST /inherent/submit`` hands back, and what the one-shot
    CLI filters the WS stream on. Discarding it here — the Step-12
    state — left the response's ``turn_id`` an empty string in
    production, with the client falling back to matching the
    ``open`` envelope's ``q`` against its own utterance.
    """
    turn_id = new_turn_id()
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        emit_surface_user_intent(
            inner_conn,
            transcript=text,
            turn_id=turn_id,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
    return turn_id


def _v2_accepted(receipt: InputReceipt) -> InputSubmissionOutcome:
    """Carry a durable receipt across the L2 -> L5 boundary as a plain value."""
    return InputSubmissionOutcome(
        outcome="accepted",
        request_id=receipt.request_id,
        input_event_uid=receipt.input_event_uid,
        turn_id=receipt.turn_id,
        session_id=receipt.session_id,
        utterance_id=receipt.utterance_id,
        text=receipt.text,
        emotion=receipt.emotion,
    )


def submit_text_v2(
    event_log_path: Path,
    request_id: str,
    client_instance_id: str,
    text: str,
) -> InputSubmissionOutcome:
    """ADR-0014 D21 — the inbox behind ``POST /inherent/submit/v2``.

    Bound at daemon start and run on an ``asyncio.to_thread`` worker, so it
    opens its own connection exactly as ``submit_callable`` does.
    """
    key = SubmissionKey(V2_PRINCIPAL, client_instance_id, request_id)
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        return _v2_accepted(submit_text_once(inner_conn, key=key, transcript=text))
    except PayloadConflictError:
        return InputSubmissionOutcome(outcome="payload_conflict")
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


def submit_with_attachments(  # noqa: PLR0913 - the phone message's fields, as `say` has them.
    event_log_path: Path, text: str, attachment_ids: Sequence[str], *, device: str = MAC_NODE,
    about: dict[str, Any] | None = None, via: str | None = None,
) -> str:
    """ADR 0211: the turn of ``POST /inherent/submit`` whose words came with stored files.

    ``device``: the paired device that sent them (ADR 0212); the turn's opening row is written
    under its name, as the ones ``/phone/ws`` writes are. ``about``: the item its phone has open
    (ADR 0214), already checked, kept on that row. ``via``: the watch the phone asked for
    (ADR 0401), kept on that row too.

    Bound at daemon start and run on an ``asyncio.to_thread`` worker, so it opens its own
    connection exactly as ``submit_callable`` does. Returns the minted ``turn_id``.
    """
    turn_id = new_turn_id()
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        emit_surface_user_intent(
            inner_conn, transcript=text, turn_id=turn_id, attachments=attachment_ids,
            ingestion_node=device, about=about, via=via,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
    return turn_id


def submit_as_device(  # noqa: PLR0913 - the phone message's fields, as `say` has them.
    event_log_path: Path, device: str, text: str, attachment_ids: Sequence[str],
    about: dict[str, Any] | None, utterance_id: str | None = None, via: str | None = None,
) -> str:
    """ADR 0212: the turn of words a paired ``device`` sent over HTTP, with or without files.

    ADR 0262: with the phone's ``utterance_id`` the words are written as its ``/phone/ws``
    ``say`` is, under the same uid and turn id, so the same message sent again by either route
    returns its first turn and writes nothing.

    Raises:
        ValueError: ``utterance_id`` is given and the words are not an utterance this host takes.
    """
    if utterance_id is None:
        return submit_with_attachments(
            event_log_path, text, attachment_ids, device=device, about=about, via=via,
        )
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        return BrainEvents(inner_conn).record_phone_say(
            device, utterance_id, text, spoken=False, about=about, attachments=attachment_ids,
        )
    except sqlite3.IntegrityError:  # the same message's other route wrote it just now
        return phone_turn_id(device, utterance_id)
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


def set_reaction(
    event_log_path: Path, memory_db: Path, target: str, emoji: str | None,
) -> bool:
    """ADR 0257: his reaction on a message, or taken off; whether anything changed.

    The line the model reads and the event are written here and no turn is started.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        return reactions.allen_reacts(conn, memory_db, target=target, emoji=emoji)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def keep_share(
    event_log_path: Path, fields: dict[str, Any], *, picture_notes: PictureNotes | None = None,
) -> str:
    """ADR 0211: keep a share he did not ask about as one ``user.shared`` event; its id.

    A saved picture is queued for its note at once (ADR 0223); if that is lost, the note is
    found again from the event.
    """
    share_id = uuid.uuid4().hex
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        emit_event(
            inner_conn,
            type="user.shared",
            payload={"share_id": share_id, **{k: v for k, v in fields.items() if v}},
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
    if picture_notes is not None and fields.get("attachments"):
        picture_notes.submit_saved(fields["attachments"], str(fields.get("note") or ""))
    return share_id


def submit_asr_v2(  # noqa: PLR0913 — the bound path and pipeline plus the four request fields.
    event_log_path: Path,
    voice_pipeline_callable: Callable[[bytes, str, str, str], Event],
    pcm: bytes,
    request_id: str,
    client_instance_id: str,
    audio_sha256: str,
    language: str,
) -> InputSubmissionOutcome:
    """ADR-0014 D21 — the lease around ``POST /inherent/asr-submit/v2``.

    The lease is claimed and released on this connection while ASR itself runs
    outside any transaction, which is the whole point of the two-phase shape:
    a SenseVoice pass may take seconds and must never hold the write lock.
    A pipeline failure releases the lease so the same ``request_id`` can be
    retried at once rather than waiting out the TTL.
    """
    key = SubmissionKey(V2_PRINCIPAL, client_instance_id, request_id)
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        try:
            claim = claim_asr_request(
                inner_conn, key=key, audio_sha256=audio_sha256, language=language,
            )
        except PayloadConflictError:
            return InputSubmissionOutcome(outcome="payload_conflict")
        except SubmissionInProgressError:
            return InputSubmissionOutcome(outcome="in_progress")
        if isinstance(claim, InputReceipt):
            return _v2_accepted(claim)
        try:
            event = voice_pipeline_callable(pcm, claim.turn_id, "inherent_ptt", language)
        except BaseException:
            release_asr_request(inner_conn, key=key)
            raise
        utterance_id = event.payload.get("utterance_id")
        return _v2_accepted(
            resolve_asr_request(
                inner_conn,
                key=key,
                audio_sha256=audio_sha256,
                language=language,
                turn_id=claim.turn_id,
                input_event_uid=event.event_uid,
                utterance_id=utterance_id if isinstance(utterance_id, str) else None,
                text=str(event.payload.get("transcript", "")),
                emotion=str(event.payload.get("emotion", "") or ""),
            ),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
