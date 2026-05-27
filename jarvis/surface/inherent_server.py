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
``starlette``, and the L5 siblings ``jarvis.surface.inherent_output``
and ``jarvis.surface.voice_pipeline`` (intra-layer — the ASR endpoint
catches the pipeline's typed exceptions to map to HTTP status codes).
Never names :mod:`jarvis.runtime`, :mod:`jarvis.decision`,
:mod:`jarvis.execution`, or :mod:`jarvis.deployment`. The
``submit_callable`` and ``voice_pipeline_callable`` are **injected**
so the module never needs to import :mod:`jarvis.state` either —
runtime is the only place wiring across layers.

Wire contract (preserved from legacy ``ui/web/server.py`` so the
inherent-swift client's ``BridgeBackend`` keeps working unchanged):

- ``POST /inherent/submit``      — body ``{"text": str}`` → ``{"status": "accepted"}``
- ``WS  /inherent/ws``           — outbound-only; client receives ``{"op", "payload"}`` envelopes
- ``GET /api/health``            — liveness; ``{"status": "ok"}``
- ``POST /inherent/image-submit`` — Step 2 / ADR-0004 stub (501)
- ``POST /inherent/asr-submit``   — ADR-0005 §5.2; multipart WAV in, transcript out.
  Falls back to 501 when ``InherentDeps.voice_pipeline_callable`` is unset
  (preserves the ADR-0003 text-only smoke test path).
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

import numpy as np
import soxr
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """

    submit_callable: Callable[[str], None]
    broadcaster: InherentBroadcaster
    voice_pipeline_callable: Callable[[bytes, str, str, str], Event] | None = None


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


def create_app(deps: InherentDeps) -> FastAPI:
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
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        await asyncio.to_thread(deps.submit_callable, text)
        return {"status": "accepted"}

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
    "SubmitRequest",
    "create_app",
]
