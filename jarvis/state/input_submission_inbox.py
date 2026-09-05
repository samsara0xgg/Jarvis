"""Authenticated, idempotent v2 input submission receipts (L2, ADR-0014 D21).

The v1 ``POST /inherent/submit`` has no request identity: a lost HTTP
response leaves the client unable to tell "the daemon never saw it" from
"the daemon accepted it and the reply died on the wire", so the only safe
client behavior is to give up.  Retrying duplicates a turn.

This module is what makes a retry safe.  Every v2 input carries a
client-chosen ``request_id``; the idempotency key
``(authenticated_principal, client_instance_id, request_id)`` addresses one
receipt row, and the receipt and the canonical input event it names commit
in the *same* transaction.  There is therefore no receipt without its input
event and no input event without a stored result:

- text — one ``BEGIN IMMEDIATE`` claims or resolves the payload hash,
  appends ``surface.user_intent`` through
  :func:`~jarvis.state.event_log.append_event_in_transaction` carrying the
  server-minted ``turn_id``, ``source_client_request_id`` and
  ``source_surface``, stores the receipt, and commits;
- ASR — the D21 processing lease, because
  :meth:`jarvis.surface.voice_pipeline.VoicePipeline.run_turn` commits
  ``utterance.received`` on its own connection and audio decode must never
  hold a SQLite write lock.  One short transaction claims
  ``(request_id, audio_sha256)`` as ``processing``, ASR runs outside any
  transaction, and a final ``BEGIN IMMEDIATE`` verifies the same
  request/hash and stores the committed row's ids.  The duplicate window is
  closed by lookup instead: before re-running ASR for a retry whose lease is
  ``processing``, the inbox asks whether an ``utterance.received`` already
  carries that ``turn_id`` and resolves the original result from it.

The ``turn_id`` is minted **here**, before the final transaction, and written
into the canonical payload.  Clients never choose one, and ADR-0008's
:func:`~jarvis.state.input_claim.claim_input_once` reuses this exact id when
it appends ``turn.started`` rather than minting a replacement.

Layer rules (L2): stdlib plus :mod:`jarvis.state.event_log`.  This module
names no other package.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.state.event_log import append_event_in_transaction

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

SOURCE_SURFACE: Final[str] = "inherent_v2"
"""What the authenticated endpoint stamps server-side (D21); never client-supplied."""

ASR_LEASE_TTL_MS: Final[int] = 120_000
"""How long a ``processing`` ASR lease is honored before a retry may resume it."""

FailureStage = Literal["after_input_append", "after_receipt_insert"]
FailureInjector = Callable[[FailureStage], None]

_CREATE_RECEIPTS_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS input_submission_receipts (
    authenticated_principal TEXT NOT NULL,
    client_instance_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    input_event_uid TEXT,
    session_id TEXT,
    utterance_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    leased_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (authenticated_principal, client_instance_id, request_id)
)
"""

_SELECT_RECEIPT_SQL: Final[str] = """
SELECT kind, payload_hash, state, turn_id, input_event_uid, session_id,
       utterance_id, result_json, leased_at_ms
FROM input_submission_receipts
WHERE authenticated_principal = ? AND client_instance_id = ? AND request_id = ?
"""

_UTTERANCE_FOR_TURN_SQL: Final[str] = (
    "SELECT event_uid FROM events WHERE type = 'utterance.received' "
    "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id ASC LIMIT 1"
)


class InputSubmissionError(RuntimeError):
    """Base failure for the v2 input submission inbox."""


class InputSubmissionTransactionStateError(InputSubmissionError):
    """The primitive cannot own ``BEGIN IMMEDIATE`` on this connection."""


class PayloadConflictError(InputSubmissionError):
    """This ``request_id`` was already used for a different payload (HTTP 409).

    D21: "An identical request-ID/payload-hash retry returns the original
    result.  Different payload is rejected."  Accepting it would let one
    request id name two distinct turns, which is exactly the ambiguity the
    receipt exists to remove.
    """


class SubmissionInProgressError(InputSubmissionError):
    """An unexpired ASR lease for this request is still running (HTTP 503)."""


@dataclass(frozen=True)
class SubmissionKey:
    """The D21 idempotency key: one receipt row per triple."""

    authenticated_principal: str
    client_instance_id: str
    request_id: str


@dataclass(frozen=True)
class InputReceipt:
    """What an accepted v2 submission durably resolved to.

    ``replayed`` is True when this call appended nothing and answered from a
    receipt an earlier call committed — the property a retry depends on.
    """

    request_id: str
    input_event_uid: str
    turn_id: str
    replayed: bool
    session_id: str | None = None
    utterance_id: str | None = None
    text: str | None = None
    emotion: str | None = None


@dataclass(frozen=True)
class AsrProcessingLease:
    """The D21 lease: run decode/ASR with this ``turn_id``, then resolve it."""

    turn_id: str


AsrClaim = InputReceipt | AsrProcessingLease


def ensure_input_submission_schema(conn: sqlite3.Connection) -> None:
    """Install the receipt table idempotently on an idle connection."""
    if conn.in_transaction:
        msg = "input-submission schema setup requires an idle connection"
        raise InputSubmissionTransactionStateError(msg)
    conn.execute(_CREATE_RECEIPTS_TABLE_SQL)
    conn.commit()


def payload_hash(kind: str, fields: Mapping[str, Any]) -> str:
    """Hash the request body a receipt is bound to (D21 payload hash).

    Canonical JSON with sorted keys so the same request produces the same
    digest regardless of field order on the wire.
    """
    canonical = json.dumps(
        {"kind": kind, **dict(fields)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_turn_id() -> str:
    """Mint the server-owned ``turn_id`` D21 forbids the client from choosing."""
    return "T" + uuid.uuid4().hex[:8]


def submit_text_once(  # noqa: PLR0913 — one keyword per canonical payload field.
    conn: sqlite3.Connection,
    *,
    key: SubmissionKey,
    transcript: str,
    channel: str = SOURCE_SURFACE,
    language: str = "zh-CN",
    turn_id: str | None = None,
    now_ms: int | None = None,
    failure_injector: FailureInjector | None = None,
) -> InputReceipt:
    """Append one canonical ``surface.user_intent`` and its receipt atomically.

    Args:
        conn: Event Log connection this call owns a transaction on.
        key: The D21 idempotency triple.
        transcript: The normalized user text.
        channel: Canonical payload ``channel``; the v2 surface label.
        language: BCP-47 tag carried on the canonical payload.
        turn_id: Server-minted id; a fresh one when omitted. Never client-chosen.
        now_ms: Receipt timestamp; wall clock when omitted.
        failure_injector: Test hook fired between the append and the commit,
            used to prove no receipt survives without its input event.

    Returns:
        The accepted receipt, ``replayed=True`` when an identical earlier
        request already committed one.

    Raises:
        PayloadConflictError: Same ``request_id``, different payload hash.
    """
    ensure_input_submission_schema(conn)
    digest = payload_hash(
        "text",
        {"transcript": transcript, "channel": channel, "language": language},
    )
    minted = turn_id or new_turn_id()
    stamp = int(time.time() * 1000) if now_ms is None else now_ms

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _read_receipt(conn, key)
        if existing is not None:
            _require_same_payload(key, existing, digest)
            receipt = _receipt_from_row(key, existing, replayed=True)
            conn.commit()
            return receipt
        event = append_event_in_transaction(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": transcript,
                "turn_id": minted,
                "channel": channel,
                "language": language,
                "source_client_request_id": key.request_id,
                "source_surface": SOURCE_SURFACE,
            },
            correlation={"turn_id": minted},
        )
        _inject(failure_injector, "after_input_append")
        _insert_receipt(
            conn,
            key,
            kind="text",
            payload_digest=digest,
            state="accepted",
            turn_id=minted,
            input_event_uid=event.event_uid,
            now_ms=stamp,
        )
        _inject(failure_injector, "after_receipt_insert")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return InputReceipt(
        request_id=key.request_id,
        input_event_uid=event.event_uid,
        turn_id=minted,
        replayed=False,
    )


def claim_asr_request(  # noqa: PLR0913 — one keyword per lease field and its clock.
    conn: sqlite3.Connection,
    *,
    key: SubmissionKey,
    audio_sha256: str,
    turn_id: str | None = None,
    now_ms: int | None = None,
    lease_ttl_ms: int = ASR_LEASE_TTL_MS,
) -> AsrClaim:
    """Claim ``(request_id, audio_sha256)`` as ``processing``, or replay its result.

    The short transaction D21 requires: it never holds the write lock across
    decode or ASR.  A retry that finds a live lease is refused; one that finds
    an expired lease resumes the *same* ``turn_id``, and if the crashed run
    had actually committed its ``utterance.received`` the lookup resolves the
    original result rather than recognizing the audio twice.

    Returns:
        :class:`InputReceipt` when the request already resolved,
        :class:`AsrProcessingLease` when the caller must run ASR.

    Raises:
        PayloadConflictError: Same ``request_id``, different audio hash.
        SubmissionInProgressError: An unexpired lease is still running.
    """
    ensure_input_submission_schema(conn)
    digest = payload_hash("asr", {"audio_sha256": audio_sha256})
    stamp = int(time.time() * 1000) if now_ms is None else now_ms

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _read_receipt(conn, key)
        if existing is None:
            minted = turn_id or new_turn_id()
            _insert_receipt(
                conn,
                key,
                kind="asr",
                payload_digest=digest,
                state="processing",
                turn_id=minted,
                input_event_uid=None,
                now_ms=stamp,
            )
            conn.commit()
            return AsrProcessingLease(turn_id=minted)
        _require_same_payload(key, existing, digest)
        if existing.state == "accepted":
            receipt = _receipt_from_row(key, existing, replayed=True)
            conn.commit()
            return receipt
        recovered = _resolve_processing_lease(
            conn, key, existing, now_ms=stamp, lease_ttl_ms=lease_ttl_ms,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return recovered


def resolve_asr_request(  # noqa: PLR0913 — one keyword per stored receipt column.
    conn: sqlite3.Connection,
    *,
    key: SubmissionKey,
    audio_sha256: str,
    turn_id: str,
    input_event_uid: str,
    session_id: str | None = None,
    utterance_id: str | None = None,
    text: str | None = None,
    emotion: str | None = None,
    now_ms: int | None = None,
) -> InputReceipt:
    """Store the committed ``utterance.received`` ids against the lease (D21).

    The final ``BEGIN IMMEDIATE``: it verifies the same request and hash still
    own the row before writing the result, so a lease that was stolen by a
    resumed retry cannot be overwritten by the run that lost it.

    Raises:
        PayloadConflictError: The row no longer matches this request/hash.
        InputSubmissionError: The lease disappeared.
    """
    digest = payload_hash("asr", {"audio_sha256": audio_sha256})
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _read_receipt(conn, key)
        if existing is None:
            msg = f"ASR lease for request {key.request_id!r} disappeared"
            raise InputSubmissionError(msg)  # noqa: TRY301 — rollback below is the point.
        _require_same_payload(key, existing, digest)
        if existing.state == "accepted":
            receipt = _receipt_from_row(key, existing, replayed=True)
            conn.commit()
            return receipt
        if existing.turn_id != turn_id:
            msg = (
                f"ASR lease for request {key.request_id!r} was resumed under "
                f"turn {existing.turn_id!r}; this run holds {turn_id!r}"
            )
            raise InputSubmissionError(msg)  # noqa: TRY301 — rollback below is the point.
        conn.execute(
            "UPDATE input_submission_receipts SET state = 'accepted', "
            "input_event_uid = ?, session_id = ?, utterance_id = ?, result_json = ?, "
            "leased_at_ms = ? WHERE authenticated_principal = ? "
            "AND client_instance_id = ? AND request_id = ?",
            (
                input_event_uid,
                session_id,
                utterance_id,
                json.dumps({"text": text, "emotion": emotion}, ensure_ascii=False),
                stamp,
                key.authenticated_principal,
                key.client_instance_id,
                key.request_id,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return InputReceipt(
        request_id=key.request_id,
        input_event_uid=input_event_uid,
        turn_id=turn_id,
        replayed=False,
        session_id=session_id,
        utterance_id=utterance_id,
        text=text,
        emotion=emotion,
    )


def release_asr_request(conn: sqlite3.Connection, *, key: SubmissionKey) -> None:
    """Drop an unresolved lease so a failed upload can be retried immediately.

    Only a ``processing`` row is removed: an accepted receipt is durable
    truth and a later identical retry must still replay it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM input_submission_receipts WHERE authenticated_principal = ? "
            "AND client_instance_id = ? AND request_id = ? AND state = 'processing'",
            (key.authenticated_principal, key.client_instance_id, key.request_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# --- internals -------------------------------------------------------------


@dataclass(frozen=True)
class _ReceiptRow:
    kind: str
    payload_hash: str
    state: str
    turn_id: str
    input_event_uid: str | None
    session_id: str | None
    utterance_id: str | None
    result_json: str
    leased_at_ms: int


def _read_receipt(conn: sqlite3.Connection, key: SubmissionKey) -> _ReceiptRow | None:
    row = conn.execute(
        _SELECT_RECEIPT_SQL,
        (key.authenticated_principal, key.client_instance_id, key.request_id),
    ).fetchone()
    if row is None:
        return None
    return _ReceiptRow(
        kind=str(row[0]),
        payload_hash=str(row[1]),
        state=str(row[2]),
        turn_id=str(row[3]),
        input_event_uid=None if row[4] is None else str(row[4]),
        session_id=None if row[5] is None else str(row[5]),
        utterance_id=None if row[6] is None else str(row[6]),
        result_json=str(row[7]),
        leased_at_ms=int(row[8]),
    )


def _insert_receipt(  # noqa: PLR0913 — one keyword per receipt column.
    conn: sqlite3.Connection,
    key: SubmissionKey,
    *,
    kind: str,
    payload_digest: str,
    state: str,
    turn_id: str,
    input_event_uid: str | None,
    now_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO input_submission_receipts (authenticated_principal, "
        "client_instance_id, request_id, kind, payload_hash, state, turn_id, "
        "input_event_uid, leased_at_ms, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key.authenticated_principal,
            key.client_instance_id,
            key.request_id,
            kind,
            payload_digest,
            state,
            turn_id,
            input_event_uid,
            now_ms,
            now_ms,
        ),
    )


def _require_same_payload(key: SubmissionKey, row: _ReceiptRow, digest: str) -> None:
    if row.payload_hash != digest:
        msg = (
            f"request {key.request_id!r} from client {key.client_instance_id!r} "
            "was already submitted with a different payload"
        )
        raise PayloadConflictError(msg)


def _receipt_from_row(key: SubmissionKey, row: _ReceiptRow, *, replayed: bool) -> InputReceipt:
    result: dict[str, Any] = json.loads(row.result_json)
    text = result.get("text")
    emotion = result.get("emotion")
    return InputReceipt(
        request_id=key.request_id,
        input_event_uid=row.input_event_uid or "",
        turn_id=row.turn_id,
        replayed=replayed,
        session_id=row.session_id,
        utterance_id=row.utterance_id,
        text=text if isinstance(text, str) else None,
        emotion=emotion if isinstance(emotion, str) else None,
    )


def _resolve_processing_lease(
    conn: sqlite3.Connection,
    key: SubmissionKey,
    row: _ReceiptRow,
    *,
    now_ms: int,
    lease_ttl_ms: int,
) -> AsrClaim:
    """Answer a retry that found a ``processing`` lease (D21 recovery)."""
    committed = conn.execute(_UTTERANCE_FOR_TURN_SQL, (row.turn_id,)).fetchone()
    if committed is not None:
        # The crashed run had already committed its utterance; the receipt is
        # what was lost.  Resolve from the durable row instead of recognizing
        # the same audio twice.
        conn.execute(
            "UPDATE input_submission_receipts SET state = 'accepted', "
            "input_event_uid = ?, leased_at_ms = ? WHERE authenticated_principal = ? "
            "AND client_instance_id = ? AND request_id = ?",
            (
                str(committed[0]),
                now_ms,
                key.authenticated_principal,
                key.client_instance_id,
                key.request_id,
            ),
        )
        return InputReceipt(
            request_id=key.request_id,
            input_event_uid=str(committed[0]),
            turn_id=row.turn_id,
            replayed=True,
        )
    if now_ms - row.leased_at_ms < lease_ttl_ms:
        msg = f"request {key.request_id!r} is still being processed"
        raise SubmissionInProgressError(msg)
    conn.execute(
        "UPDATE input_submission_receipts SET leased_at_ms = ? "
        "WHERE authenticated_principal = ? AND client_instance_id = ? AND request_id = ?",
        (now_ms, key.authenticated_principal, key.client_instance_id, key.request_id),
    )
    return AsrProcessingLease(turn_id=row.turn_id)


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    if injector is not None:
        injector(stage)


__all__ = [
    "ASR_LEASE_TTL_MS",
    "SOURCE_SURFACE",
    "AsrClaim",
    "AsrProcessingLease",
    "InputReceipt",
    "InputSubmissionError",
    "InputSubmissionTransactionStateError",
    "PayloadConflictError",
    "SubmissionInProgressError",
    "SubmissionKey",
    "claim_asr_request",
    "ensure_input_submission_schema",
    "new_turn_id",
    "payload_hash",
    "release_asr_request",
    "resolve_asr_request",
    "submit_text_once",
]
