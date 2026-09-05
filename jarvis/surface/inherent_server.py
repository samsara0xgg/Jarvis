"""Inherent FastAPI app factory — ADR-0003 Step 7 + ADR-0005 §5.2.

L5 surface module. Hosts the daemon's HTTP+WS endpoints the
inherent-swift client (legacy ``desktop/inherent-swift/``) speaks. The
ADR-0003 wave shipped text + WS. ADR-0005 §5.2 lights up the
``/inherent/asr-submit`` endpoint with a real handler (multipart WAV in,
normalized transcript out); ``/inherent/image-submit`` remains a 501
stub until ADR-0004 lands.

The runtime wires this app via ``runtime/inherent_loop.serve_inherent``
in Step 8, injecting an :class:`InherentDeps` with:

- ``submit_callable`` — bound to
  ``emit_surface_user_intent(runtime.conn, transcript=text, turn_id=<mint>)``
  (L5 input adapter per spec §3.6.1). The handler wraps the sync call
  in ``asyncio.to_thread`` so the SQLite write does not block the
  event loop.
- ``broadcaster`` — the shared :class:`InherentBroadcaster`. The
  runtime's background ``_response_watcher`` task polls the L2 Event
  Log for ``surface.response_{open,chunk,emitted}`` rows and
  dispatches to the broadcaster's per-type methods; the WS endpoint
  here registers / unregisters connecting clients.

Layer rules (L5): may import from stdlib, ``fastapi`` / ``pydantic`` /
``starlette``, and the L5 siblings ``jarvis.surface.inherent_output``,
``jarvis.surface.inherent_protocol`` (the v2 wire DTOs) and
``jarvis.surface.voice_pipeline`` (intra-layer — the ASR endpoint
catches the pipeline's typed exceptions to map to HTTP status codes).
Never names :mod:`jarvis.runtime`, :mod:`jarvis.decision`,
:mod:`jarvis.execution`, or :mod:`jarvis.deployment`. The
``submit_callable`` and ``voice_pipeline_callable`` are **injected**
so the module never needs to import :mod:`jarvis.state` either —
runtime is the only place wiring across layers.

Wire contract (preserved from legacy ``ui/web/server.py`` so the
inherent-swift client's ``BridgeBackend`` keeps working unchanged):

- ``POST /inherent/submit``      — body ``{"text": str}`` →
  ``{"status": "accepted", "turn_id": str}`` (ADR-0009 D2 added
  ``turn_id``; additive, so the inherent-swift client that reads only
  ``status`` is unaffected)
- ``WS  /inherent/ws``           — outbound-only; client receives ``{"op", "payload"}`` envelopes
- ``WS  /inherent/ws/v2``        — ADR-0014 D5-D7; ``Authorization: Bearer <token>``
  on the upgrade, typed :mod:`jarvis.surface.inherent_protocol` envelopes, and a
  ``client.hello`` / ``server.hello`` handshake. Registered only when
  ``InherentDeps.v2`` is injected, so a v1-only deployment's route table is
  byte-identical to what it was. With ``InherentV2Deps.attach_client``
  injected (ADR-0014 D8/D11), the hello-completed socket is handed to the
  runtime's client hub as a :class:`V2Session` and every vetted post-hello
  frame is routed to the returned :class:`V2ClientHandle`; without it the
  socket says hello and then only listens, as card 1 left it.
- ``GET /api/health``            — liveness; ``{"status": "ok"}``
- ``POST /inherent/image-submit`` — Step 2 / ADR-0004 stub (501)
- ``POST /inherent/asr-submit``   — ADR-0005 §5.2; multipart WAV in, transcript out.
  Falls back to 501 when ``InherentDeps.voice_pipeline_callable`` is unset
  (preserves the ADR-0003 text-only smoke test path).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import io
import logging
import secrets
import time
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

import numpy as np
import soxr
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ValidationError

from jarvis.surface.inherent_protocol import (
    HELLO_TIMEOUT_S,
    INITIAL_MAX_FRAMES_PER_S,
    MAX_CLIENT_FRAME_BYTES,
    REQUIRED_CLIENT_CAPABILITIES,
    AsrSubmitV2Request,
    AsrSubmitV2Response,
    ClientEnvelope,
    ClientHello,
    RuntimeCapabilities,
    ServerHello,
    ServerHelloPayload,
    SubmitV2Request,
    SubmitV2Response,
    hello_is_supported,
)
from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jarvis.shared import Event
    from jarvis.surface.inherent_output import InherentBroadcaster


LOGGER = logging.getLogger("jarvis.surface.inherent_server")

# ADR-0005 §5.2: hard cap on uploaded WAV size for /inherent/asr-submit.
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


class SubmitRequest(BaseModel):
    """Body of ``POST /inherent/submit``.

    Matches the legacy wire shape ``{"text": str}`` exactly so the
    inherent-swift client needs no changes. Pydantic enforces the type;
    a missing field returns 422, a non-string field returns 422, an
    empty string is accepted at the schema layer and rejected at the
    handler layer with 400.
    """

    text: str


class CancelResponseRequest(BaseModel):
    """Body of ``POST /inherent/cancel-response`` (ADR-0008 D10).

    ``scope`` defaults to ``"generation"`` — the only scope implemented in
    Wave 4A. ``"foreground_output"`` needs a playback lease that does not
    exist yet and is answered with ``{"outcome": "unsupported_scope"}``,
    as is any unrecognized string.
    """

    response_id: str
    scope: str = "generation"
    reason: str = "operator_request"


@dataclass(frozen=True)
class V2Session:
    """One hello-completed v2 socket, handed to the runtime's client hub.

    Only the connection id and two callables cross this boundary: the hub
    sends through ``send_text`` and closes through ``close``; the socket
    object itself never leaves this module.
    """

    connection_id: str
    send_text: Callable[[str], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]


class V2ClientHandle(Protocol):
    """What the hub returns for an attached session (ADR-0014 D8/D11)."""

    async def on_frame(self, envelope: ClientEnvelope) -> None:
        """Receive one vetted post-hello client frame."""

    async def detach(self) -> None:
        """Release the connection once its socket is gone."""


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
    """Injectable dependencies for the ADR-0014 ``/inherent/ws/v2`` route.

    Every entry is a value or a callable the runtime binds. That is what
    keeps the v2 route inside L5's import rules: this module mints no
    identity, opens no database, and never reads the token file — it only
    compares, echoes, and asks.

    Attributes:
        token_matches: Constant-time comparison of the token presented on
            the upgrade against the one this boot rotated. The runtime
            binds ``functools.partial(inherent_v2_token_matches, token)``
            so the secret itself never reaches this layer.
        mint_connection_id: One fresh ``connection_id`` per accepted
            socket (``jarvis.shared.realtime.new_connection_id``).
        boot_id: Minted once per daemon process; lets a client tell a
            reconnect to the same process from a restart.
        log_epoch: The Event Log's lineage id, read once at daemon start.
            A client whose stored epoch differs must resnapshot.
        high_water_cursor: ``MAX(events.id)`` at hello time — where the
            durable stream stands when the connection opens.
        runtime_capabilities: What this daemon can actually do right now,
            computed from live wiring rather than declared.
        hello_timeout_s: D7 deadline for the first frame; past it the
            socket closes with ``hello_timeout``.
        max_frames_per_s: D5 initial per-connection frame budget; a
            breach closes with ``protocol_error``.
        attach_client: ADR-0014 D8/D11 — the runtime hub's entry point.
            Called once per hello-completed socket with its
            :class:`V2Session`; the returned handle receives every vetted
            post-hello frame and is detached when the socket ends. ``None``
            (the default) keeps card 1's behavior: hello, then listen.
        submit_text: ADR-0014 D21 — bound to the L2 input submission inbox.
            Takes ``(request_id, client_instance_id, text)`` and returns the
            durable receipt or a refusal. Sync (it owns a ``BEGIN
            IMMEDIATE``) and offloaded via ``asyncio.to_thread`` exactly as
            ``submit_callable`` is. ``None`` makes the route answer 501.
        submit_asr: ADR-0014 D21 — the ASR half, bound to the inbox's
            processing lease around ``voice_pipeline_callable``. Takes
            ``(pcm, request_id, client_instance_id, audio_sha256,
            language)``; the decode and the HTTP bounds stay here, the
            lease/append/resolve sequence stays in the runtime. It raises
            the same :mod:`jarvis.surface.voice_pipeline` exceptions the v1
            handler maps, so 422 and 503 mean what they already mean.
            ``None`` makes the route answer 501.
    """

    token_matches: Callable[[str], bool]
    mint_connection_id: Callable[[], str]
    boot_id: str
    log_epoch: str
    high_water_cursor: Callable[[], int]
    runtime_capabilities: Callable[[], RuntimeCapabilities]
    hello_timeout_s: float = HELLO_TIMEOUT_S
    max_frames_per_s: int = INITIAL_MAX_FRAMES_PER_S
    attach_client: Callable[[V2Session], Awaitable[V2ClientHandle]] | None = None
    submit_text: Callable[[str, str, str], InputSubmissionOutcome] | None = None
    submit_asr: Callable[[bytes, str, str, str, str], InputSubmissionOutcome] | None = None


@dataclass(frozen=True)
class InherentDeps:
    """Injectable dependencies for the FastAPI app.

    Frozen so the runtime's wiring stays explicit — once
    ``serve_inherent`` builds the deps, neither the app factory nor
    the handlers mutate them.

    Attributes:
        submit_callable: Synchronous side effect of the ``/submit``
            handler. The daemon binds this to a partial of
            ``emit_surface_user_intent(runtime.conn, transcript=text,
            turn_id=<mint>)`` per spec §3.6.1. Sync (not async)
            because the underlying SQLite write is sync; the handler
            offloads the call via ``asyncio.to_thread`` so the event
            loop stays unblocked.

            ADR-0009 D2: the return value is the ``turn_id`` the
            callable minted, echoed back on the wire so a forwarding
            client can correlate the response stream (and print the id
            when it gives up waiting). ``None`` is accepted for
            bindings that do not mint one — the response then carries
            an empty ``turn_id`` and the client falls back to matching
            the response header's query text.
        broadcaster: Shared :class:`InherentBroadcaster` instance.
            The runtime's ``_response_watcher`` task pushes envelopes
            into it from the background (one cursor over the three
            ``surface.response_{open,chunk,emitted}`` types, dispatched
            by ``event.type``); the WS endpoint here registers /
            unregisters client sockets on connect / disconnect.
        voice_pipeline_callable: ADR-0005 §5.2 — bound to
            ``voice_pipeline.VoicePipeline.run_turn`` (keyword args
            packed into positional ``(audio_bytes, turn_id, channel,
            language)``). Returns the emitted ``utterance.received``
            :class:`Event` row. Defaults to ``None`` so ADR-0003
            text-only fixtures stay backward compatible — when unset
            the ``/inherent/asr-submit`` handler 501s instead of
            attempting ASR.
        cancel_response_callable: ADR-0008 D10 — bound to the runtime's
            ``make_response_cancel_callable`` when
            ``realtime.response.independent_response_cancel`` is on.
            Takes ``(response_id, scope, reason)`` and returns one of
            ``cancelled`` / ``already_terminal`` / ``unknown_response``
            / ``unsupported_scope`` / ``timeout``. The injected-callable
            shape is what keeps ``jarvis/surface`` free of any
            ``jarvis.decision`` import. ``None`` (the default) means the
            ``/inherent/cancel-response`` route is never registered, so
            the route table and OpenAPI schema stay exactly as they are
            today.
        v2: ADR-0014 D5-D7 — the realtime v2 socket's dependencies.
            ``None`` (the default) leaves ``/inherent/ws/v2`` unregistered
            and the route table exactly as v1 deployments know it.
    """

    submit_callable: Callable[[str], str | None]
    broadcaster: InherentBroadcaster
    voice_pipeline_callable: Callable[[bytes, str, str, str], Event] | None = None
    cancel_response_callable: Callable[[str, str, str], str] | None = None
    v2: InherentV2Deps | None = None


class _FrameRateLimiter:
    """Fixed one-second window admission counter for one v2 socket (D5).

    A fixed window rather than a sliding one: the budget exists to stop a
    runaway client, not to shape traffic, and a client that behaves never
    comes near the edge where the two disagree.
    """

    def __init__(self, max_per_s: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Start the first window now.

        Args:
            max_per_s: Frames admitted per window before the breach.
            clock: Monotonic time source; injectable for tests.
        """
        self._max_per_s = max_per_s
        self._clock = clock
        self._window_start = clock()
        self._count = 0

    def admit(self) -> bool:
        """Count one frame and report whether it stays within the budget."""
        now = self._clock()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._count = 0
        self._count += 1
        return self._count <= self._max_per_s


def _v2_presented_token(header: str | None) -> str | None:
    """Extract the Bearer token from an ``Authorization`` header (D5).

    Returns None for anything that is not exactly ``Bearer <token>``. The
    header value is never logged — a malformed one is indistinguishable
    from a wrong one to everything downstream, which is the point.
    """
    if header is None:
        return None
    scheme, separator, token = header.partition(" ")
    if scheme != "Bearer" or not separator or not token:
        return None
    return token


async def _v2_close(ws: WebSocket, code: int, reason: str) -> None:
    """Close the socket once; a second close (route racing the hub) is a no-op."""
    with contextlib.suppress(RuntimeError):
        await ws.close(code=code, reason=reason)


async def _v2_receive_text_frame(ws: WebSocket) -> str | None:
    """Receive one client frame, closing on the two transport-level rejects.

    Both checks are pre-decode by design (D5): a binary frame has no place
    in a JSON protocol, and an oversized one must be refused before it is
    parsed, not after.

    Returns:
        The frame's text, or None when the socket was closed here.

    Raises:
        WebSocketDisconnect: The client went away.
    """
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(int(message.get("code", 1000)))
    text = message.get("text")
    if text is None:
        await _v2_close(ws, 1002, "protocol_error")
        return None
    if len(text.encode("utf-8")) > MAX_CLIENT_FRAME_BYTES:
        await _v2_close(ws, 1009, "frame_too_large")
        return None
    return str(text)


async def _v2_read_hello(deps: InherentV2Deps, ws: WebSocket) -> ClientHello | None:
    """Await, decode, and vet the first frame on an accepted v2 socket (D7).

    Returns:
        The decoded hello, or None when the socket was closed here — on
        the deadline (``hello_timeout``), a frame that is not a valid
        ``client.hello`` (``protocol_error``), or a client this daemon
        cannot serve (``upgrade_required``).
    """
    try:
        frame = await asyncio.wait_for(_v2_receive_text_frame(ws), deps.hello_timeout_s)
    except TimeoutError:
        await ws.close(code=1008, reason="hello_timeout")
        return None
    if frame is None:
        return None
    try:
        hello = ClientHello.model_validate_json(frame)
    except ValidationError:
        await ws.close(code=1002, reason="protocol_error")
        return None
    if not hello_is_supported(hello):
        await ws.close(code=1008, reason="upgrade_required")
        return None
    return hello


def _v2_server_hello(
    deps: InherentV2Deps,
    hello: ClientHello,
    connection_id: str,
) -> ServerHello:
    """Build the D7 answer to a supported hello.

    ``resume_mode`` is unconditionally ``snapshot``: this card sends no
    deltas, so there is nothing a client could resume from yet.
    """
    return ServerHello(
        protocol_version=2,
        message_type="server.hello",
        message_id=hello.message_id,
        delivery_class="protocol",
        connection_id=connection_id,
        log_epoch=deps.log_epoch,
        boot_id=deps.boot_id,
        sent_at_ms=int(time.time() * 1000),
        payload=ServerHelloPayload(
            selected_version=2,
            view_schema_version=1,
            resume_mode="snapshot",
            server_high_water_cursor=deps.high_water_cursor(),
            required_client_capabilities=list(REQUIRED_CLIENT_CAPABILITIES),
            runtime_capabilities=deps.runtime_capabilities(),
        ),
    )


async def _v2_drain_after_hello(
    deps: InherentV2Deps,
    ws: WebSocket,
    on_frame: Callable[[ClientEnvelope], Awaitable[None]] | None,
) -> None:
    """Vet every further client frame and route it to the hub, if any (D5/D7/D11).

    A client that goes wrong is closed on the same terms whether or not a
    hub is attached: size, then budget, then shape.  With ``on_frame`` None
    nothing is routed — the card 1 contract of hello, then listening.
    """
    limiter = _FrameRateLimiter(deps.max_frames_per_s)
    while True:
        frame = await _v2_receive_text_frame(ws)
        if frame is None:
            return
        if not limiter.admit():
            await _v2_close(ws, 1002, "protocol_error")
            return
        try:
            envelope = ClientEnvelope.model_validate_json(frame)
        except ValidationError:
            await _v2_close(ws, 1002, "protocol_error")
            return
        if envelope.message_type == "client.hello":
            await _v2_close(ws, 1002, "protocol_error")
            return
        if on_frame is not None:
            await on_frame(envelope)


async def _run_v2_session(deps: InherentV2Deps, ws: WebSocket) -> None:
    """Serve one ``/inherent/ws/v2`` connection end to end (ADR-0014 D5-D7).

    Split out of ``create_app`` so the factory stays under ruff's
    complexity cap, exactly as ``_run_asr_submit`` is.

    The authorization check runs BEFORE ``accept``: closing a
    still-connecting socket makes the ASGI server answer the upgrade with
    HTTP 403, so an unauthenticated client never reaches a frame loop and
    the daemon never allocates a ``connection_id`` for it (D5).
    """
    token = _v2_presented_token(ws.headers.get("authorization"))
    if token is None or not deps.token_matches(token):
        await ws.close(code=1008)
        return
    await ws.accept()
    connection_id = deps.mint_connection_id()
    try:
        hello = await _v2_read_hello(deps, ws)
        if hello is None:
            return
        await ws.send_text(_v2_server_hello(deps, hello, connection_id).model_dump_json())
        if deps.attach_client is None:
            await _v2_drain_after_hello(deps, ws, None)
            return
        handle = await deps.attach_client(
            V2Session(
                connection_id=connection_id,
                send_text=ws.send_text,
                close=functools.partial(_v2_close, ws),
            ),
        )
        try:
            await _v2_drain_after_hello(deps, ws, handle.on_frame)
        finally:
            await handle.detach()
    except WebSocketDisconnect:
        return


def _v2_authorize(deps: InherentV2Deps, request: Request) -> None:
    """Refuse an unauthenticated HTTP v2 request exactly as the socket does (D5).

    ``/inherent/ws/v2`` closes a still-connecting socket, which the ASGI
    server answers with HTTP 403; these routes raise the same status from the
    same token check, so one credential gates the whole v2 surface.
    """
    token = _v2_presented_token(request.headers.get("authorization"))
    if token is None or not deps.token_matches(token):
        raise HTTPException(status_code=403, detail="forbidden")


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
    """Re-check ``_run_asr_submit``'s bounds and decode, in the same order.

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

    The bounds and status codes are ``_run_asr_submit``'s, re-checked in the
    same order against the same module-level constants and decoder.  They are
    duplicated rather than factored out on purpose: v1 is a byte-compatible
    contract with a shipped Swift client, and rewriting its body to share a
    validator would put that contract at risk for no behavior gained.

    The one addition is the advertised ``audio_sha256`` — the receipt's
    payload hash.  The server recomputes it and refuses a mismatch, so a
    truncated upload cannot resolve a receipt against audio nobody heard.
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


async def _run_asr_submit(
    deps: InherentDeps,
    audio: UploadFile | None,
    language: str,
) -> dict[str, str]:
    """ADR-0005 §5.2 — body of ``POST /inherent/asr-submit``.

    Split out of ``create_app`` so the closure stays under ruff's
    cyclomatic-complexity cap. The handler validates the multipart
    upload, mints the ``turn_id``, and offloads
    ``voice_pipeline_callable`` to a worker thread.

    Status codes (per ADR §5.2):

    - 200 — ``{"status": "accepted", "transcript": <normalized>,
      "turn_id": "T<hex>"}``.
    - 400 — empty body / missing audio field.
    - 413 — audio above the 5 MB cap.
    - 415 — unsupported content type.
    - 422 — ASR returned empty after the unified filter
      (``VoicePipelineEmptyError``).
    - 500 — internal error (logged with ``turn_id``).
    - 501 — ``deps.voice_pipeline_callable`` not wired (text-only
      deployments such as the ADR-0003 smoke tests).
    - 503 — ``VOICE_INPUT_LOCK`` busy (wake listener mid-turn).
    """
    if deps.voice_pipeline_callable is None:
        raise HTTPException(
            status_code=501,
            detail="asr voice pipeline not wired (ADR-0005)",
        )
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

    # WAV container → raw PCM16 mono 16 kHz (the recognizer's expected
    # input format). Without this the entire WAV (RIFF header + PCM)
    # is treated as raw int16 samples, which either raises ValueError
    # on odd-length files or recognizes garbage on even-length ones.
    pcm = _decode_wav_to_pcm16_mono_16k(body)
    if not pcm:
        raise HTTPException(status_code=400, detail="empty audio after decode")

    # ADR §5.2: server-mint turn_id, ignore any client-supplied value (Day-1 trust posture).
    turn_id = "T" + secrets.token_hex(4)
    try:
        ev = await asyncio.to_thread(
            deps.voice_pipeline_callable,
            pcm,
            turn_id,
            "inherent_ptt",
            language,
        )
    except VoicePipelineEmptyError:
        raise HTTPException(status_code=422, detail="empty") from None
    except VoiceInputBusyError:
        raise HTTPException(status_code=503, detail="busy") from None
    except Exception:
        LOGGER.exception("asr_submit failed for turn_id=%s", turn_id)
        raise HTTPException(status_code=500, detail="internal") from None

    # Wire shape matches legacy ui/web/server.py:1268 — the inherent-swift
    # client (BridgeBackend.swift:345) reads ``text`` and ``emotion`` fields.
    # ADR-0005 §5.2 listed ``transcript`` but that diverged from the existing
    # Swift contract; the Day-1 add ``turn_id`` rides alongside.
    return {
        "status": "accepted",
        "text": str(ev.payload.get("transcript", "")),
        "emotion": str(ev.payload.get("emotion", "") or ""),
        "turn_id": turn_id,
    }


def create_app(deps: InherentDeps) -> FastAPI:  # noqa: C901 — one closed route table; the cancel route is registered only when injected.
    """Build the FastAPI app with all 5 endpoints registered.

    The factory takes the injected deps once and closes over them in
    the route handlers — Step 8's ``serve_inherent`` owns the uvicorn
    lifecycle, so this module does not register startup / shutdown
    hooks.

    Args:
        deps: Bound runtime dependencies (submit callback + shared
            broadcaster). Must outlive every request the app serves.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance ready
        for uvicorn to serve.
    """
    app = FastAPI(
        title="Jarvis Inherent (ADR-0003 Step 1, text-only)",
        version="0.1.0",
    )

    @app.post("/inherent/submit", status_code=200)
    async def submit(req: SubmitRequest) -> dict[str, str]:
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
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        turn_id = await asyncio.to_thread(deps.submit_callable, text)
        return {"status": "accepted", "turn_id": turn_id or ""}

    if deps.cancel_response_callable is not None:
        cancel_response_callable = deps.cancel_response_callable

        @app.post("/inherent/cancel-response", status_code=200)
        async def cancel_response(req: CancelResponseRequest) -> dict[str, str]:
            """Request generation cancellation for one ResponseRun.

            Always 200 with an outcome body — an unknown id is
            ``{"outcome": "unknown_response"}``, never a 500, because the
            caller cannot distinguish "already finished" from "never
            existed" and neither is a server fault. The callable is sync
            (it owns a SQLite CAS) and is offloaded via
            ``asyncio.to_thread`` exactly like ``/inherent/submit``, which
            is what keeps the ``BEGIN IMMEDIATE`` off the event loop.
            """
            outcome = await asyncio.to_thread(
                cancel_response_callable,
                req.response_id,
                req.scope,
                req.reason,
            )
            return {"outcome": outcome}

    @app.websocket("/inherent/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        """Outbound-only push channel for Inherent wire envelopes.

        Lifecycle:

        1. ``ws.accept()`` — completes the WS handshake BEFORE the
           registry mutation so the broadcaster never holds a half-
           connected client.
        2. ``broadcaster.register(ws)`` — add the client to the in-
           memory registry under the broadcaster's lock.
        3. ``await ws.receive_text()`` in a loop — the legacy
           inherent-swift client is push-only and never sends frames,
           so this just blocks until the client disconnects. The loop
           also absorbs any future-proofing keep-alive frames without
           interpreting them.
        4. ``WebSocketDisconnect`` ends the loop normally.
        5. ``finally:`` ``broadcaster.unregister(ws)`` — runs whether
           the loop ended via disconnect, cancellation, or an
           unexpected exception, so the registry never leaks dead
           sockets.
        """
        await ws.accept()
        await deps.broadcaster.register(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await deps.broadcaster.unregister(ws)

    if deps.v2 is not None:
        v2_deps = deps.v2

        @app.websocket("/inherent/ws/v2")
        async def ws_v2_endpoint(ws: WebSocket) -> None:
            """ADR-0014 D5-D7 realtime socket. See :func:`_run_v2_session`.

            Deliberately NOT registered with the v1 broadcaster: v1 pushes
            ``{"op", "payload"}`` envelopes, which a v2 client would reject.
            """
            await _run_v2_session(v2_deps, ws)

        @app.post("/inherent/submit/v2", status_code=200)
        async def submit_v2(request: Request, req: SubmitV2Request) -> SubmitV2Response:
            """ADR-0014 D21 authenticated idempotent text input.

            Registered beside the v2 socket and behind the same token, so a
            v1-only deployment's route table is byte-identical to what it was.
            """
            _v2_authorize(v2_deps, request)
            return await _run_submit_v2(v2_deps, req)

        @app.post("/inherent/asr-submit/v2", status_code=200)
        async def asr_submit_v2(  # noqa: PLR0913 — one argument per multipart form field.
            request: Request,
            audio: Annotated[UploadFile | None, File()] = None,
            request_id: Annotated[str, Form()] = "",
            client_instance_id: Annotated[str, Form()] = "",
            client_created_at_ms: Annotated[int, Form()] = 0,
            audio_sha256: Annotated[str, Form()] = "",
            language: Annotated[str, Form()] = "zh-CN",
        ) -> AsrSubmitV2Response:
            """ADR-0014 D21 authenticated idempotent PTT upload."""
            _v2_authorize(v2_deps, request)
            fields = _asr_v2_fields(
                request_id,
                client_instance_id,
                client_created_at_ms,
                audio_sha256,
                language,
            )
            return await _run_asr_submit_v2(v2_deps, audio, fields)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Liveness probe — used by ops scripts to confirm the daemon is up."""
        return {"status": "ok"}

    @app.post("/inherent/image-submit", status_code=501)
    async def image_submit() -> None:
        """Step 1 stub — image input lands in Step 2 (ADR-0004)."""
        raise HTTPException(
            status_code=501,
            detail="image not implemented in step 1 (ADR-0004)",
        )

    @app.post("/inherent/asr-submit", status_code=200)
    async def asr_submit(
        audio: Annotated[UploadFile | None, File()] = None,
        language: Annotated[str, Form()] = "zh-CN",
    ) -> dict[str, str]:
        """ADR-0005 §5.2 — PTT WAV in, normalized transcript out.

        See :func:`_run_asr_submit` for the full status-code contract.
        Split out so ``create_app`` stays under the cyclomatic-complexity
        cap; the route handler only forwards to the helper.
        """
        return await _run_asr_submit(deps, audio, language)

    return app


__all__ = [
    "InherentDeps",
    "InherentV2Deps",
    "InputSubmissionOutcome",
    "SubmitRequest",
    "V2ClientHandle",
    "V2Session",
    "create_app",
]
