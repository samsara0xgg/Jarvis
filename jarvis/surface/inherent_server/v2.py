"""ADR-0014 D21: the authenticated, idempotent input routes the v2 clients post to.

- ``POST /inherent/submit/v2`` — a text turn behind the boot token
- ``POST /inherent/asr-submit/v2`` — a push-to-talk WAV (at most 5 MB) behind the same token

Both are exempt from the local key (``guards.V2_INPUT_PATHS``) and check their own per-boot token,
in a middleware of their own, before anything reads the body.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

import numpy as np
import soxr
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from jarvis.surface.inherent_protocol import (
    AsrSubmitV2Request,
    AsrSubmitV2Response,
    SubmitV2Request,
    SubmitV2Response,
)
from jarvis.surface.inherent_server.guards import V2_INPUT_PATHS, bearer_token, is_loopback_peer
from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from starlette.datastructures import Address

    from jarvis.surface.inherent_server.deps import InherentDeps

    _CallNext = Callable[[Request], Awaitable[Response]]


LOGGER = logging.getLogger(__name__)


# ADR-0005 §5.2: hard cap on uploaded WAV size for /inherent/asr-submit/v2.
_ASR_MAX_BYTES = 5 * 1024 * 1024
_ASR_ACCEPTED_CONTENT_TYPES = frozenset({"audio/wav", "audio/wave", "audio/x-wav"})
_ASR_TARGET_SAMPLE_RATE_HZ = 16000

_PCM16_SAMPLE_WIDTH_BYTES = 2


def _decode_wav_to_pcm16_mono_16k(wav_bytes: bytes) -> bytes:
    """Decode a WAV upload into raw PCM16 mono little-endian @ 16 kHz.

    The recognizer expects raw PCM16 frames (``np.frombuffer(...,
    dtype=np.int16)``); a multipart upload contains the full WAV
    container (RIFF header + PCM data) and the inherent-swift client
    may record at the device's native sample rate (commonly 44.1 / 48
    kHz). This helper strips the header, mixes multi-channel to mono,
    and resamples to 16 kHz via soxr (HQ quality, with anti-alias filter).

    Raises:
        HTTPException(415): malformed WAV or unsupported sample width.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            n_channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            framerate = wav.getframerate()
            n_frames = wav.getnframes()
            pcm = wav.readframes(n_frames)
    except wave.Error as exc:
        raise HTTPException(status_code=415, detail=f"invalid WAV: {exc}") from None

    if sample_width != _PCM16_SAMPLE_WIDTH_BYTES:
        raise HTTPException(
            status_code=415,
            detail=f"WAV must be PCM16 (got sample_width={sample_width})",
        )

    samples = np.frombuffer(pcm, dtype=np.int16)
    if n_channels > 1:
        # Mix down to mono by averaging channels.
        samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

    if framerate != _ASR_TARGET_SAMPLE_RATE_HZ:
        if framerate <= 0 or samples.size == 0:
            return b""
        pcm_f32 = samples.astype(np.float32) / 32768.0
        resampled_f32 = soxr.resample(pcm_f32, framerate, _ASR_TARGET_SAMPLE_RATE_HZ, quality="HQ")
        resampled_f32 = np.clip(resampled_f32, -1.0, 1.0)
        samples = (resampled_f32 * 32767.0).astype(np.int16)

    return samples.tobytes()


@dataclass(frozen=True)
class InputSubmissionOutcome:
    """What an injected v2 input callable answers with (ADR-0014 D21).

    A plain value rather than an exception because the two refusals are not
    faults: ``payload_conflict`` is the client reusing a ``request_id`` for
    different content, ``in_progress`` is an identical upload still running.
    Mapping them here keeps :mod:`jarvis.state` out of L5 — the runtime
    translates the inbox's typed errors into this shape.
    """

    outcome: Literal["accepted", "payload_conflict", "in_progress"]
    request_id: str = ""
    input_event_uid: str = ""
    turn_id: str = ""
    session_id: str | None = None
    utterance_id: str | None = None
    text: str | None = None
    emotion: str | None = None


@dataclass(frozen=True)
class InherentV2Deps:
    """Injectable dependencies for the ADR-0014 D21 input routes.

    Every entry is a callable the runtime binds. That is what keeps the v2
    routes inside L5's import rules: this module opens no database and never
    reads the token file — it only compares and asks.

    Attributes:
        token_matches: Constant-time comparison of the token presented on
            a request against the one this boot rotated. The runtime
            binds ``functools.partial(inherent_v2_token_matches, token)``
            so the secret itself never reaches this layer.
        submit_text: ADR-0014 D21 — bound to the L2 input submission inbox.
            Takes ``(request_id, client_instance_id, text)`` and returns the
            durable receipt or a refusal. Sync (it owns a ``BEGIN
            IMMEDIATE``) and offloaded via ``asyncio.to_thread`` exactly as
            ``submit_callable`` is. ``None`` makes the route answer 501.
        submit_asr: ADR-0014 D21 — the ASR half, bound to the inbox's
            processing lease around the voice pipeline. Takes
            ``(pcm, request_id, client_instance_id, audio_sha256,
            language)``; the decode and the HTTP bounds stay here, the
            lease/append/resolve sequence stays in the runtime. It raises
            the :mod:`jarvis.surface.voice_pipeline` exceptions the route
            maps to 422 (empty) and 503 (busy).
            ``None`` makes the route answer 501.
        device_token_matches: ADR 0170 — the check a peer that is not on loopback
            must pass instead of ``token_matches``, which only a process on this
            machine can read. ``None`` (the default) leaves such a peer refused.
    """

    token_matches: Callable[[str], bool]
    submit_text: Callable[[str, str, str], InputSubmissionOutcome] | None = None
    submit_asr: Callable[[bytes, str, str, str, str], InputSubmissionOutcome] | None = None
    device_token_matches: Callable[[str], bool] | None = None


def _v2_token_ok(deps: InherentV2Deps, client: Address | None, token: str) -> bool:
    """Check ``token`` against the boot token, or a device token for a remote peer (ADR 0170)."""
    if deps.device_token_matches is not None and not is_loopback_peer(client):
        return deps.device_token_matches(token)
    return deps.token_matches(token)


def _v2_authorized(deps: InherentV2Deps, request: Request) -> bool:
    """Report whether this HTTP v2 request presented the boot token (D5)."""
    token = bearer_token(request.headers.get("authorization"))
    return token is not None and _v2_token_ok(deps, request.client, token)


def _v2_input_auth_middleware(
    deps: InherentV2Deps,
) -> Callable[[Request, _CallNext], Awaitable[Response]]:
    """Gate the v2 input routes before anything reads their body (D5).

    A route-handler check would come too late: FastAPI parses and validates
    the body *before* the handler runs, so an anonymous caller would get 422
    for a malformed body and 403 only for a well-formed one — which reveals the
    request schema — and a multipart upload would be buffered in full before
    being refused. Refusing in middleware makes the token the first gate on both
    routes, and it touches nothing else: any other path is passed straight through.
    """

    async def gate(request: Request, call_next: _CallNext) -> Response:
        if request.url.path in V2_INPUT_PATHS and not _v2_authorized(deps, request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    return gate


def _v2_refusal(outcome: InputSubmissionOutcome) -> None:
    """Map the inbox's two refusals to their status codes (D21)."""
    if outcome.outcome == "payload_conflict":
        raise HTTPException(
            status_code=409,
            detail="request_id already submitted with a different payload",
        )
    if outcome.outcome == "in_progress":
        raise HTTPException(status_code=503, detail="request already being processed")


async def _run_submit_v2(deps: InherentV2Deps, req: SubmitV2Request) -> SubmitV2Response:
    """ADR-0014 D21 — body of ``POST /inherent/submit/v2``.

    ``client_created_at_ms`` is decoded and then deliberately dropped: D21
    calls it untrusted telemetry, so nothing downstream may order or identify
    by it.
    """
    if deps.submit_text is None:
        raise HTTPException(status_code=501, detail="v2 input inbox not wired (ADR-0014 D21)")
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    outcome = await asyncio.to_thread(
        deps.submit_text,
        req.request_id,
        req.client_instance_id,
        text,
    )
    _v2_refusal(outcome)
    return SubmitV2Response(
        request_id=outcome.request_id,
        input_event_uid=outcome.input_event_uid,
        turn_id=outcome.turn_id,
        session_id=outcome.session_id,
    )


def _asr_v2_fields(
    request_id: str,
    client_instance_id: str,
    client_created_at_ms: int,
    audio_sha256: str,
    language: str,
) -> AsrSubmitV2Request:
    """Validate the non-file half of the multipart body, or 400."""
    try:
        return AsrSubmitV2Request(
            request_id=request_id,
            client_instance_id=client_instance_id,
            client_created_at_ms=client_created_at_ms,
            audio_sha256=audio_sha256,
            language=language,
        )
    except ValidationError:
        raise HTTPException(status_code=400, detail="invalid submission fields") from None


async def _asr_v2_audio(audio: UploadFile | None, expected_sha256: str) -> tuple[bytes, str]:
    """Check the upload's type, size and digest, then decode it.

    Returns the decoded PCM16 mono 16 kHz frames and the digest of the exact
    uploaded bytes, which is the receipt's payload hash.
    """
    if audio is None:
        raise HTTPException(status_code=400, detail="audio file required")
    if audio.content_type not in _ASR_ACCEPTED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content type: {audio.content_type}",
        )
    body = await audio.read()
    if len(body) > _ASR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="audio too large (max 5MB)")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    digest = hashlib.sha256(body).hexdigest()
    if digest != expected_sha256:
        raise HTTPException(status_code=400, detail="audio_sha256 does not match the upload")
    pcm = _decode_wav_to_pcm16_mono_16k(body)
    if not pcm:
        raise HTTPException(status_code=400, detail="empty audio after decode")
    return pcm, digest


async def _run_asr_submit_v2(
    deps: InherentV2Deps,
    audio: UploadFile | None,
    fields: AsrSubmitV2Request,
) -> AsrSubmitV2Response:
    """ADR-0014 D21 — body of ``POST /inherent/asr-submit/v2``.

    Status codes (ADR-0005 §5.2): 400 missing / empty audio, 413 above the
    5 MB cap, 415 not a PCM16 WAV, 422 nothing recognized, 503 the voice
    input is busy, 500 anything else.

    The advertised ``audio_sha256`` is the receipt's payload hash.  The
    server recomputes it and refuses a mismatch, so a truncated upload
    cannot resolve a receipt against audio nobody heard.
    """
    if deps.submit_asr is None:
        raise HTTPException(status_code=501, detail="asr voice pipeline not wired (ADR-0005)")
    pcm, digest = await _asr_v2_audio(audio, fields.audio_sha256)

    try:
        outcome = await asyncio.to_thread(
            deps.submit_asr,
            pcm,
            fields.request_id,
            fields.client_instance_id,
            digest,
            fields.language,
        )
    except VoicePipelineEmptyError:
        raise HTTPException(status_code=422, detail="empty") from None
    except VoiceInputBusyError:
        raise HTTPException(status_code=503, detail="busy") from None
    except Exception:
        LOGGER.exception("asr_submit_v2 failed for request_id=%s", fields.request_id)
        raise HTTPException(status_code=500, detail="internal") from None

    _v2_refusal(outcome)
    return AsrSubmitV2Response(
        request_id=outcome.request_id,
        input_event_uid=outcome.input_event_uid,
        turn_id=outcome.turn_id,
        session_id=outcome.session_id,
        utterance_id=outcome.utterance_id,
        text=outcome.text or "",
        emotion=outcome.emotion or "",
    )


def register_v2_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR-0014 D21: the two input routes and their token middleware, when ``deps.v2`` is set."""
    if deps.v2 is None:
        return
    v2_deps = deps.v2
    app.middleware("http")(_v2_input_auth_middleware(v2_deps))

    @app.post("/inherent/submit/v2", status_code=200)
    async def submit_v2(req: SubmitV2Request) -> SubmitV2Response:
        """ADR-0014 D21 authenticated idempotent text input.

        Registered only when ``InherentDeps.v2`` is injected, behind the boot token.
        """
        return await _run_submit_v2(v2_deps, req)

    @app.post("/inherent/asr-submit/v2", status_code=200)
    async def asr_submit_v2(  # noqa: PLR0913 — one argument per multipart form field.
        audio: Annotated[UploadFile | None, File()] = None,
        request_id: Annotated[str, Form()] = "",
        client_instance_id: Annotated[str, Form()] = "",
        client_created_at_ms: Annotated[int, Form()] = 0,
        audio_sha256: Annotated[str, Form()] = "",
        language: Annotated[str, Form()] = "zh-CN",
    ) -> AsrSubmitV2Response:
        """ADR-0014 D21 authenticated idempotent PTT upload."""
        fields = _asr_v2_fields(
            request_id,
            client_instance_id,
            client_created_at_ms,
            audio_sha256,
            language,
        )
        return await _run_asr_submit_v2(v2_deps, audio, fields)
