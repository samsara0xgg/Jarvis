"""Inherent FastAPI app factory — ADR-0003 Step 7 + ADR-0005 §5.2.

L5 surface module. Hosts the daemon's HTTP+WS endpoints the desktop
surface speaks. The ADR-0003 wave shipped text + WS; push-to-talk audio
arrives on ``/inherent/asr-submit/v2``; a phone's pictures, files and shares arrive on
``/inherent/attachments`` and ``/inherent/share`` (ADR 0211). :func:`require_local_key` puts every
route except the liveness probe behind the local key and a local Host.

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
:mod:`jarvis.execution`, or :mod:`jarvis.deployment`. The submit and
ASR callables are **injected** so the module never needs to import
:mod:`jarvis.state` either — runtime is the only place wiring across
layers.

Wire contract (preserved from legacy ``ui/web/server.py`` so the
inherent-swift client's ``BridgeBackend`` keeps working unchanged):

- ``POST /inherent/submit``      — body ``{"text": str}`` →
  ``{"status": "accepted", "turn_id": str}`` (ADR-0009 D2 added
  ``turn_id``; additive, so the inherent-swift client that reads only
  ``status`` is unaffected)
- ``POST /inherent/ask``         — body ``{"text": str}`` → ``{"turn_id", "spoken", "written"}``
  once that turn's final answer is in (504 / 502 otherwise), for a one-shot caller (Siri)
- ``GET  /inherent/waiting``     — everything waiting on Allen in one read, for a paired phone
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
- ``GET /inherent/work-state``   — ADR 0023 saved current-work-state record + data head
- ``POST /inherent/work-state/refresh`` — ADR 0023 on-demand analysis (single-flight)
- ``GET /inherent/projects``     — ADR 0037 seven-day project view (no model call)
- ``POST /inherent/projects/refresh`` — ADR 0037 sort new activities (single-flight)
- ``POST /inherent/attachments`` — ADR 0211; multipart ``file`` → ``{"id", "kind", "name", ...}``;
  ``POST /inherent/submit`` then takes ``"attachments": [id]``
- ``POST /inherent/share``       — ADR 0211; a link, text or files from another app's share sheet
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import io
import ipaddress
import json
import logging
import re
import time
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import numpy as np
import soxr
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import Address, Headers
from starlette.datastructures import UploadFile as FormFile
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketClose

from jarvis.shared.lang import language, t
from jarvis.state.agent_marks import AgentMarks
from jarvis.state.attachments import (
    MAX_UPLOAD_BODY_BYTES,
    AttachmentRef,
    AttachmentRefused,
    Attachments,
    valid_id,
)
from jarvis.state.day_line import parse_day
from jarvis.state.memory_page import Conflict
from jarvis.state.push_tokens import RegistrationError
from jarvis.state.shares import (
    MAX_NOTE_CHARS,
    MAX_SHARE_TEXT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
)
from jarvis.surface.apns import MAX_REGISTER_BYTES, REGISTER_PATH
from jarvis.surface.claude_hooks import ClaudeHooks
from jarvis.surface.claude_sessions import ClaudeSessions
from jarvis.surface.codex_sessions import (
    CodexSession,
    fold_codex_hook,
    prune_codex_sessions,
    settle_codex_sessions,
)
from jarvis.surface.device_pairing import CLAIM_PATH, DevicePairing, register_pairing_routes
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
from jarvis.surface.phone_events import MAX_BATCH_BYTES, MAX_BATCH_FRAMES, PHONE_EVENTS_PATH
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub, serve_phone
from jarvis.surface.terminal_link import TERMINAL_PATH, TerminalHub, serve_terminal
from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from datetime import date
    from datetime import time as clock
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

    from jarvis.surface.terminal_events import BrainEvents
    from jarvis.surface.voice_controls import VoiceControls
    from jarvis.surface.voice_live import LiveVoice

    _CallNext = Callable[[Request], Awaitable[Response]]

    from jarvis.surface.inherent_output import InherentBroadcaster


LOGGER = logging.getLogger("jarvis.surface.inherent_server")

# ADR-0005 §5.2: hard cap on uploaded WAV size for /inherent/asr-submit/v2.
_ASR_MAX_BYTES = 5 * 1024 * 1024
_ASR_ACCEPTED_CONTENT_TYPES = frozenset({"audio/wav", "audio/wave", "audio/x-wav"})
_ASR_TARGET_SAMPLE_RATE_HZ = 16000
# The two authenticated input routes, gated in middleware so the token is the
# first thing checked — exactly where the v2 socket checks it.
_V2_INPUT_PATHS: Final[frozenset[str]] = frozenset(
    {"/inherent/submit/v2", "/inherent/asr-submit/v2"},
)
_PCM16_SAMPLE_WIDTH_BYTES = 2
# A page that rebinds its own domain to 127.0.0.1 still sends that domain as
# Host, so only requests addressed to this machine reach a route.
_LOCAL_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1", "localhost")
# ADR 0070 / 0069: the longest line the island types into a session, the longest mark key.
_REPLY_CHARS: Final = 4000
# ``POST /inherent/ask``: the biggest body and question, how long the answer is waited for, and
# how often a wait re-reads the log without a wake-up (``response.failed`` never reaches the wire).
_ASK_BODY_BYTES: Final = 4096
_ASK_TEXT_CHARS: Final = 2000
_ASK_WAIT_S: Final[float] = 25.0
_ASK_RECHECK_S: Final = 1.0
_SESSION_ID_CHARS: Final = 128
# Open without the local key: the liveness probe, and the v2 routes, which
# check their own per-boot token.
_HEALTH_PATH: Final = "/api/health"
_KEYLESS_PATHS: Final[frozenset[str]] = frozenset(
    {_HEALTH_PATH, "/inherent/ws/v2", *_V2_INPUT_PATHS},
)


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
    # ADR 0211: ids from ``POST /inherent/attachments``; the text may then be empty.
    attachments: list[str] = Field(default_factory=list)


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


class QuestionAnswerRequest(BaseModel):
    """ADR 0066: the ask card's answers by field label, or ``dismiss`` for its close button."""

    clarification_id: str
    answers: dict[str, str] = Field(default_factory=dict)
    dismiss: bool = False


class CardDecisionRequest(BaseModel):
    """ADR 0062: Allen's answer to the card he sees, by its id; edits are the card's text fields."""

    confirmation_id: str
    decision: Literal["accept", "reject"]
    edits: dict[str, str] = Field(default_factory=dict)


class CancelResponseRequest(BaseModel):
    """Body of ``POST /inherent/cancel-response`` (ADR-0008 D10).

    ``scope`` defaults to ``"generation"``, which cancels the run itself.
    ``"foreground_output"`` stops only the speech that is audible now and
    leaves the run to finish on its own; under that scope ``reason`` is
    ignored, because such a stop is always recorded as ``user_stop``. Any
    unrecognized string is answered with ``{"outcome": "unsupported_scope"}``.

    A turn still being thought about has no answer on the wire to name yet
    (its ``open`` leaves once the answer is whole, ADR 0108): ``turn_id`` in
    place of ``response_id`` cancels every run of that turn still open, as
    ``generation``, and answers ``no_open_run`` when none is. A ``user_stop``
    for a turn whose run has not opened yet is kept and applied when it does
    (``stopped_before_start``): the model is never asked.
    """

    response_id: str = ""
    turn_id: str = ""
    scope: str = "generation"
    reason: str = "operator_request"


class ControlsRequest(BaseModel):
    """Body of ``POST /inherent/controls`` (ADR-0015).

    Each field is optional; ``None`` leaves that switch untouched, so ``{}``
    is a pure read the surface uses to sync on connect. The response is
    always the full current state ``{"mic_muted", "speech_muted", "conversation", "quiet"}``.
    """

    mic_muted: bool | None = None
    speech_muted: bool | None = None
    # ADR 0041: the surface's wave mode, listening without a wake word.
    conversation: bool | None = None
    # ADR 0153: the quiet level, the one controls field the daemon keeps across restarts.
    quiet: Literal["off", "quiet", "no-pop", "dnd"] | None = None
    # GPT-Live phase A: ``start`` opens a session (refused with a reason when
    # the ingress or the API key is missing), ``stop`` hangs up.  The response
    # then also carries ``"live": {...}`` (``LiveVoice.status``), on every request.
    live: Literal["start", "stop"] | None = None


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


class SetupRoutes(Protocol):
    """What first-run setup serves (``jarvis.runtime.setup.Setup``)."""

    def read(self) -> dict[str, Any]:
        """``GET /inherent/setup``."""

    def save_names(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/name``."""

    def check_key(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/key``."""

    def preview(self, body: dict[str, Any]) -> bytes:
        """``POST /inherent/setup/voice-preview``."""

    def done(self) -> dict[str, Any]:
        """``POST /inherent/setup/done``."""


class DictationRoutes(Protocol):
    """ADR 0058 dictation (``jarvis.runtime.dictation.Dictation``)."""

    active: bool

    def begin(self, context: dict[str, str]) -> AsyncIterator[dict[str, Any]]:
        """Start recording; RuntimeError while one is running."""

    def stop(self) -> bool:
        """Finish recording; the running stream goes on to the result."""


class MemoryRoutes(Protocol):
    """ADR 0154: the Dashboard's memory page. Blocking calls (run off the loop thread).

    ``LookupError`` is a 404, ``ValueError`` a 400 and :class:`Conflict` a 409.
    """

    def overview(self) -> dict[str, Any]:
        """The six sections, last night's changes and the counts."""

    def item(self, item_id: str) -> dict[str, Any]:
        """One item whole."""

    def edit(self, item_id: str, text: str, section: str | None) -> dict[str, Any]:
        """Edit or move an item (it is pinned)."""

    def delete(self, item_id: str) -> dict[str, Any]:
        """Delete an item."""

    def confirm(self, item_id: str) -> dict[str, Any]:
        """Confirm an item."""

    def keep(self, version: str, item_id: str) -> dict[str, Any]:
        """Put back an item a nightly version marked stale."""

    def versions(self) -> dict[str, Any]:
        """One entry per core memory version."""

    def undo(self, version: str) -> dict[str, Any]:
        """Take back what a version changed."""

    def set_cap(self, max_chars: int) -> dict[str, Any]:
        """Save the note's character cap."""

    def search(self, query: str, who: str) -> dict[str, Any]:
        """Search the records since the history starts."""

    def days(self) -> dict[str, Any]:
        """One card per day summary."""

    def day(self, day: str) -> dict[str, Any]:
        """One day's summary."""

    def edit_day(self, day: str, sections: dict[str, list[str]]) -> dict[str, Any]:
        """Store the user's day summary."""

    def day_records(
        self, day: str, around: str | None, offset: int, limit: int,
    ) -> dict[str, Any]:
        """One day's conversation, a page at a time."""


class NightRoutes(Protocol):
    """ADR 0093 night run (``jarvis.runtime.night_run.NightRun``); every call may block briefly."""

    def snapshot(self) -> dict[str, Any]:
        """``GET /inherent/night``."""

    def start(
        self, *, hours: float | None, until: clock | None, source: str, action_id: str | None,
    ) -> dict[str, Any]:
        """Start a run, or answer the one already running."""

    def darken_now(self) -> None:
        """Dim, mute and sleep the display now instead of after the bedtime card."""

    def stay(self) -> None:
        """The owner goes to answer a session: dark waits for a quiet minute."""

    def end(self, *, action_id: str | None) -> dict[str, Any]:
        """End the run: put back what it changed."""


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
    device_token_matches: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class AskOutcome:
    """How a turn ended for ``POST /inherent/ask``: its final answer, or the reason it has none."""

    spoken: str = ""
    written: str = ""
    failure: str | None = None


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
        cancel_response_callable: ADR-0008 D10 — bound to the runtime's
            ``make_response_cancel_callable`` when
            ``realtime.response.independent_response_cancel`` is on.
            Takes ``(response_id, scope, reason)`` and returns one of
            ``cancelled`` / ``already_terminal`` / ``unknown_response``
            / ``unsupported_scope`` / ``timeout``, or, for
            ``scope="foreground_output"``, ``applied`` / ``stale`` /
            ``uncertain`` / ``policy_ignore`` /
            ``policy_hash_mismatch`` -- where ``uncertain`` means the
            durable terminal is owed OR the playback actor did not answer
            in time, never that the audio is confirmed stopped. The
            injected-callable
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
    # ADR 0211: the phone's files. ``attachments`` is the store behind
    # ``POST /inherent/attachments`` (``None`` leaves the route unregistered, and a submit
    # carrying ids answers 501); ``images_ok`` is whether the conversation model takes pictures.
    # ``submit_attachments(text, ids)`` starts a turn that carries the files and returns its id;
    # ``share_callable(fields)`` keeps a share that is not a turn and returns its id (``None``
    # leaves ``POST /inherent/share`` unregistered).
    attachments: Attachments | None = None
    images_ok: bool = False
    submit_attachments: Callable[[str, Sequence[str]], str] | None = None
    share_callable: Callable[[dict[str, Any]], str] | None = None
    cancel_response_callable: Callable[[str, str, str], str] | None = None
    # ``(turn_id, reason) -> outcome``: the same route's stop for a turn still
    # being thought about; wired with ``cancel_response_callable``.
    cancel_turn_callable: Callable[[str, str], str] | None = None
    v2: InherentV2Deps | None = None
    # ADR-0015: the mute switches behind ``POST /inherent/controls``. ``None``
    # (the default) leaves the route unregistered.
    controls: VoiceControls | None = None
    # Called once when ``POST /inherent/controls`` turns conversation mode from
    # on to off: the surface's exit, which is a dismissal said without words.
    dismiss_callable: Callable[[], None] | None = None
    # GPT-Live phase A controller; ``None`` means ``live`` requests are refused.
    live: LiveVoice | None = None
    # ADR 0170: the brain's terminals. ``terminals`` holds the connected ones and
    # ``device_name`` maps a device token to the paired name it belongs to; the route
    # ``/terminal/ws`` exists only with both, i.e. only where device tokens are wired.
    # ``POST /inherent/device/events`` (ADR 0197) needs ``device_name`` and ``phone_events``,
    # the log it appends to; a Mac running alone has that without a hub (ADR 0207).
    terminals: TerminalHub | None = None
    device_name: Callable[[str], str | None] | None = None
    phone_events: BrainEvents | None = None
    # ADR 0209: the phone's conversation socket ``/phone/ws``, on every host that has
    # ``device_name``; it needs no terminal hub, so a Mac running alone has it too.
    phone: PhoneHub | None = None
    # ADR 0210: ``(device name, body) -> None`` keeps the push tokens a paired device registers at
    # ``POST /inherent/device/push``; it raises ``RegistrationError`` for a body it refuses.
    # Needs ``device_name``. ``None`` leaves the route unregistered (404).
    push_register: Callable[[str, object], None] | None = None
    # ADR 0210: ``(tool, cwd, request_id) -> None``, called on the loop when a Claude Code
    # permission prompt is held for him, so the host can push that it waits.
    claude_request: Callable[[str, str, str], None] | None = None
    # ADR 0196: the four device-pairing routes. ``None`` leaves them unregistered (404).
    pairing: DevicePairing | None = None
    # ADR-0018: the quota dashboard's read model and its on-demand poll.
    # ``usage_read`` runs a small SQLite fold on the loop thread; ``usage_refresh``
    # awaits one full poll (network off-thread, emit on-thread) and returns
    # the same read model. ``None`` leaves both routes unregistered.
    usage_read: Callable[[], dict[str, Any]] | None = None
    usage_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0048: spend one Codex limit reset, ``request_id -> {code, windows_reset}``
    # (blocking network, run off the loop). Registered only with ``plugin_authorize``:
    # it spends account credit, so only the desktop's private credential may call it.
    usage_codex_reset: Callable[[str], dict[str, Any]] | None = None
    # ADR 0065: record a balance a provider will not report, ``(service, usd)``;
    # raises ``ValueError`` for a bad pair. Loop thread (it emits). Desktop
    # credential only, like the reset.
    usage_record_balance: Callable[[str, float], object] | None = None
    # ADR 0023: the current-work-state record. ``work_state_read`` is a small
    # SQLite fold on the loop thread; ``work_state_refresh`` awaits the
    # runtime's single-flight analysis (off-thread) and answers the same
    # shape plus ``outcome``. ``None`` leaves both routes unregistered.
    work_state_read: Callable[[], dict[str, Any]] | None = None
    work_state_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0037: the project view (never a model call) and the sorting job that
    # answers the same view plus ``outcome``; both run off the loop thread.
    # ``None`` leaves both routes unregistered.
    projects_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    projects_refresh: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # Spec §18.3: the conversation window reads the memory.db rows the
    # backend's own history starts from, oldest first, past a ``seq``
    # cursor. ``(after, limit) -> {"since", "rows"}``; ``None`` leaves the
    # route unregistered.
    conversation_read: Callable[[int, int], dict[str, Any]] | None = None
    # ADR 0062: the card waiting for Allen's button (``{"card": ... | None}``, a
    # small fold off the loop thread) and his answer to it, which starts a turn
    # and returns its id. ``None`` leaves both routes unregistered.
    card_read: Callable[[], dict[str, Any]] | None = None
    card_decide: Callable[[str, str, dict[str, str]], str] | None = None
    # ADR 0066: the ask card waiting to be filled in (``{"card": ... | None}``)
    # and Allen's answers to it, which start a turn (its id) or, dismissed,
    # nothing (``None``). ``None`` leaves both routes unregistered.
    question_read: Callable[[], dict[str, Any]] | None = None
    question_answer: Callable[[str, dict[str, str] | None], str | None] | None = None
    # ADR 0108: whether a turn that thinks is under way, which one, and the
    # words that make one. A small SQLite read on the loop thread; ``None``
    # leaves the route unregistered.
    think_read: Callable[[], dict[str, Any]] | None = None
    # ``POST /inherent/ask``: how the turn ended (its final answer or its failure), ``None`` while
    # it is still running. A small SQLite read, called off the loop thread; ``None`` leaves the
    # route unregistered.
    ask_outcome: Callable[[str], AskOutcome | None] | None = None
    # ADR 0199: one day's timeline, folded from the log when asked: the day asked for, or ``None``
    # for today in the owner's zone, to the response document. Off the loop thread. A ValueError
    # is a 400; ``None`` leaves the route unregistered.
    day_read: Callable[[date | None], Awaitable[dict[str, Any]]] | None = None
    # ADR 0051: the companion home's reads and its one write, all off the loop
    # thread. A LookupError is "not connected" (404, the home's fallback), any
    # other failure 502. ``None`` leaves the routes unregistered.
    today_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    todo_set: Callable[[str, bool], Awaitable[None]] | None = None
    mail_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # ADR 0124: archive junk letters (ids, archive) or put them back (archive False).
    mail_archive: Callable[[list[str], bool], Awaitable[None]] | None = None
    # ADR 0147, the Dashboard's mail page: one letter whole, its read and trash taps and the
    # reply draft under the open letter. ``None`` leaves a route unregistered (404).
    mail_letter: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_summary: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_mark_read: Callable[[list[str], bool], Awaitable[None]] | None = None
    mail_trash: Callable[[list[str], bool], Awaitable[None]] | None = None
    # ADR 0176, ``dashboard.view.enabled``: what the Dashboard shows (page, tab, open item
    # ``(kind, id, title)``, rows ``(id, title, mail_id)``; page None closes). ``None`` = 404.
    view_set: (
        Callable[
            [str | None, str, tuple[str, str, str] | None, list[tuple[str, str, str]]], None,
        ] | None
    ) = None
    mail_draft_read: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    mail_draft_save: Callable[[str, str, str], Awaitable[dict[str, Any]]] | None = None
    mail_draft_send: Callable[[str, str, str], Awaitable[None]] | None = None
    mail_draft_discard: Callable[[str], Awaitable[None]] | None = None
    # ADR 0154: the Dashboard's memory page (core memory, day summaries, search, versions).
    # ``None`` leaves every ``/inherent/memory`` route unregistered (404).
    memory_page: MemoryRoutes | None = None
    # ADR 0155: the job-mail notices the notch shows, Allen's tap on one (id, action, reaction)
    # and the job ledger with its delete. ``None`` leaves the routes unregistered (404): the
    # daemon wires them only while ``job_mail.enabled``. A LookupError is 404, a ValueError 400.
    notices_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    notice_act: Callable[[str, str, str | None], Awaitable[None]] | None = None
    # ADR 0203: the notch's trip cards (pin an offered option, next bus, undo an unpin). ``None``
    # leaves the route unregistered (404); a LookupError is 404.
    departure_act: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # ADR 0160: the same feedback for every other proactive card the client raises, by card id.
    # ``None`` leaves the route unregistered (404); a LookupError is 404, a ValueError 400.
    card_act: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None
    # ADR 0163: ``{hold}`` (``call``, ``away`` or None), for a client whose daemon runs no job mail.
    # ``None`` leaves the route unregistered (404): the daemon wires it while ``moment.enabled``.
    moment_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    jobs_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    job_delete: Callable[[str], Awaitable[None]] | None = None
    job_flag: Callable[[str, str], Awaitable[None]] | None = None
    # ADR 0177: the tracker's own rows: add one (fields, returns its id) and edit one by id
    # (the fields given); a ValueError is 400, an unknown id a LookupError (404).
    application_add: Callable[[dict[str, Any]], Awaitable[str]] | None = None
    application_edit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    # ADR 0186: Allen's undo of an interview's reminders and Outlook event, by application id; an
    # application with none armed is a LookupError (404).
    application_cancel_reminders: Callable[[str], Awaitable[None]] | None = None
    brief_read: Callable[[], dict[str, Any] | None] | None = None
    # ADR 0125: does a finished agent turn's ending ask Allen something? ``asks`` waits for Jev
    # (off the loop thread); ``peek`` never waits, for the terminal sessions' board. None = off.
    turn_end_asks: Callable[[str, str], Awaitable[bool | None]] | None = None
    turn_end_peek: Callable[[str, str, bool], bool | None] | None = None
    # ADR 0128: Allen's messages (epoch seconds) in a session; the first after a scored finish
    # is logged.
    turn_end_answered: Callable[[str, list[float]], None] | None = None
    # ADR 0052: the Settings page's file, read and saved off the loop thread;
    # a ValueError from saving is a 400. ``None`` leaves the routes unregistered.
    settings_read: Callable[[], Awaitable[dict[str, Any]]] | None = None
    settings_update: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # ADR 0147: the reSpeaker board's {present, firmware, direction, speech}, read off the loop.
    board_status: Callable[[], Awaitable[dict[str, Any]]] | None = None
    # Settings > Restart: answer, then TERM this process; registered only when
    # launchd's KeepAlive is there to bring the daemon back.
    restart: Callable[[], None] | None = None
    # ADR 0067, Settings > Privacy & data: the export builds a zip and hands
    # back its temp path (deleted once sent); the clear deletes every
    # recording and screenshot now and answers how many; the erase marks
    # everything for the next boot and restarts, so it is wired only with
    # ``restart``. ``None`` leaves a route unregistered.
    data_export: Callable[[], Awaitable[Path]] | None = None
    data_clear: Callable[[], Awaitable[int]] | None = None
    data_erase: Callable[[], None] | None = None
    # ADR 0046: whether the Agents page may read Claude Code's own session
    # files (``observer.claude_sessions.enabled``); off, it shows only what
    # the user's installed hooks push.
    claude_sessions_read: bool = False
    # ADR 0069: the file Allen's marks on agent sessions live in (unread,
    # parked, archived). ``None`` leaves the marks routes unregistered.
    agent_marks_path: Path | None = None
    # ADR 0038, 0202: desktop management takes the local key or a paired device's
    # token (``manager_authorize``), unlike ordinary text submission. Secrets never
    # travel on the public websocket.
    plugin_read: Callable[[], dict[str, Any]] | None = None
    plugin_action: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    plugin_authorize: Callable[[str | None], bool] | None = None
    plugin_icon: Callable[[str], str | None] | None = None
    # The desktop settings switch: switch the fixed-text language now and keep
    # it in settings.yaml (``code -> language``, raises ValueError on anything
    # but zh / en). Registered only with ``plugin_authorize``.
    language_save: Callable[[str], str] | None = None
    # First-run setup (``jarvis.runtime.setup.Setup``): read, save_names,
    # check_key and preview block (run off the loop thread; ValueError is a
    # 400), done runs on the loop because it schedules the restart.
    setup: SetupRoutes | None = None
    # ADR 0058: dictation from the live mic, heard and polished for the text
    # caret. ``None`` (no voice stack) leaves both routes unregistered.
    dictation: DictationRoutes | None = None
    # ADR 0093: the night run the companion's cards show and its buttons
    # drive. ``None`` leaves both routes unregistered.
    night: NightRoutes | None = None
    # ADR 0019: the Codex session board the hooks fill; the runtime shares it
    # with the night run's watch.
    codex_board: dict[str, CodexSession] = field(default_factory=dict)


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
    complexity cap, exactly as ``_run_asr_submit_v2`` is.

    The authorization check runs BEFORE ``accept``: closing a
    still-connecting socket makes the ASGI server answer the upgrade with
    HTTP 403, so an unauthenticated client never reaches a frame loop and
    the daemon never allocates a ``connection_id`` for it (D5).
    """
    token = _v2_presented_token(ws.headers.get("authorization"))
    if token is None or not _v2_token_ok(deps, ws.client, token):
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


def _v2_token_ok(deps: InherentV2Deps, client: Address | None, token: str) -> bool:
    """Check ``token`` against the boot token, or a device token for a remote peer (ADR 0170)."""
    if deps.device_token_matches is not None and not _is_loopback_peer(client):
        return deps.device_token_matches(token)
    return deps.token_matches(token)


def _v2_authorized(deps: InherentV2Deps, request: Request) -> bool:
    """Report whether this HTTP v2 request presented the boot token (D5)."""
    token = _v2_presented_token(request.headers.get("authorization"))
    return token is not None and _v2_token_ok(deps, request.client, token)


def _v2_input_auth_middleware(
    deps: InherentV2Deps,
) -> Callable[[Request, _CallNext], Awaitable[Response]]:
    """Gate the v2 input routes before anything reads their body (D5).

    ``/inherent/ws/v2`` checks the token before ``accept()``, so an
    unauthenticated client never reaches a frame.  A route-handler check
    cannot match that: FastAPI parses and validates the body *before* the
    handler runs, so an anonymous caller would get 422 for a malformed body
    and 403 only for a well-formed one — which reveals the request schema —
    and a multipart upload would be buffered in full before being refused.
    Refusing in middleware makes the token the first gate on both routes, and
    it touches nothing else: any other path is passed straight through, so v1
    behaves exactly as it did.
    """

    async def gate(request: Request, call_next: _CallNext) -> Response:
        if request.url.path in _V2_INPUT_PATHS and not _v2_authorized(deps, request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    return gate


def _is_loopback_peer(client: Address | None) -> bool:
    """Whether the TCP peer is this machine; no peer address (a unix socket) is too."""
    if client is None:
        return True
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def manager_authorize(
    authorize: Callable[[str | None], bool],
    device_token_matches: Callable[[str], bool] | None,
) -> Callable[[str | None], bool]:
    """The handler-level check of the plugin, language, Codex reset and balance routes (ADR 0202).

    ``authorize`` is the local key; with ``device_token_matches`` a paired device's token passes
    too. The middleware has already refused a remote peer without a device token.
    """
    if device_token_matches is None:
        return authorize

    def allowed(header: str | None) -> bool:
        token = _v2_presented_token(header)
        return authorize(header) or (token is not None and device_token_matches(token))

    return allowed


class _LocalKeyMiddleware:
    """Refuse every HTTP request and socket upgrade that lacks the local key.

    Pure ASGI rather than ``app.middleware("http")`` so ``/inherent/ws`` is
    covered too: a socket without the key is closed before ``accept``, which
    the server answers with HTTP 403.

    With ``device_token_matches`` (ADR 0170), a peer that is not on loopback
    cannot read the key, so it must carry a paired device's token on every
    route but the liveness probe. A peer on loopback is checked as before.
    With ``open_claim`` (ADR 0196), ``POST`` to the claim path is open to any
    peer, loopback or not: a device that has no token yet trades its code there.
    """

    def __init__(
        self,
        app: ASGIApp,
        authorize: Callable[[str | None], bool],
        device_token_matches: Callable[[str], bool] | None = None,
        *,
        open_claim: bool = False,
    ) -> None:
        self.app = app
        self.authorize = authorize
        self.device_token_matches = device_token_matches
        self.open_claim = open_claim

    def _allowed(self, scope: Scope) -> bool:
        if scope["path"] == _HEALTH_PATH:
            return True
        if (
            self.open_claim
            and scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == CLAIM_PATH
        ):
            return True
        header = Headers(scope=scope).get("authorization")
        if self.device_token_matches is not None and not _is_loopback_peer(
            Address(*scope["client"]) if scope.get("client") else None,
        ):
            token = _v2_presented_token(header)
            return token is not None and self.device_token_matches(token)
        return scope["path"] in _KEYLESS_PATHS or self.authorize(header)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and not self._allowed(scope):
            if scope["type"] == "websocket":
                await WebSocketClose(code=1008)(scope, receive, send)
            else:
                await JSONResponse({"detail": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
            return
        await self.app(scope, receive, send)


def require_local_key(
    app: FastAPI,
    authorize: Callable[[str | None], bool],
    *,
    extra_hosts: Sequence[str] = (),
    device_token_matches: Callable[[str], bool] | None = None,
    open_claim: bool = False,
) -> None:
    """Serve only requests addressed to this machine that carry the local key.

    ``authorize`` checks an ``Authorization`` header value (``Bearer <key>``).
    The host check runs first, so a rebound page is refused before anything
    reads its headers. ``extra_hosts`` are the further names a brain answers to
    on its private addresses, and ``device_token_matches`` is what a peer on
    one of those addresses must present (ADR 0170); both default to nothing.
    ``open_claim`` lets any peer ``POST`` the pairing claim route without a
    token (ADR 0196), the only route besides the liveness probe that needs none;
    the app has to register it (``InherentDeps.pairing``).
    """
    app.add_middleware(
        _LocalKeyMiddleware,
        authorize=authorize,
        device_token_matches=device_token_matches,
        open_claim=open_claim,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*_LOCAL_HOSTS, *extra_hosts])


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


class SettingsRequest(BaseModel):
    """Body of ``POST /inherent/settings`` (ADR 0052): the page's changed values by key."""

    changes: dict[str, Any]


class TodoRequest(BaseModel):
    """Body of ``POST /inherent/today/todo`` (ADR 0051): the home's checkbox."""

    id: str
    done: bool


class TurnEndRequest(BaseModel):
    """Body of ``POST /inherent/agents/turn-end`` (ADR 0125): a finished turn's last message."""

    session_id: str = Field(min_length=1, max_length=_SESSION_ID_CHARS)
    text: str = Field(max_length=100_000)


class TurnEndAnswered(BaseModel):
    """Body of ``POST /inherent/agents/turn-end/answered`` (ADR 0128): the session he wrote in."""

    session_id: str = Field(min_length=1, max_length=_SESSION_ID_CHARS)


class MailArchiveRequest(BaseModel):
    """Body of ``POST /inherent/mail/archive`` and ``/unarchive`` (ADR 0124): Gmail message ids."""

    ids: list[str] = Field(max_length=20)


class NoticeActionRequest(BaseModel):
    """Body of ``POST /inherent/notices/{id}`` (ADR 0155): ``seen``, or feedback with a reaction."""

    action: Literal["seen", "feedback", "dismissed"]
    reaction: str | None = Field(default=None, max_length=50)


class DepartureRequest(BaseModel):
    """Body of ``POST /inherent/departure`` (ADR 0203, 0204): the field each action needs."""

    action: Literal["pin", "add", "remove", "next", "undo"]
    offer_id: str = Field(default="", max_length=40)
    index: int = Field(default=0, ge=0, le=9)
    pin_id: str = Field(default="", max_length=40)
    trip_id: str = Field(default="", max_length=60)


class CardActionRequest(BaseModel):
    """Body of ``POST /inherent/cards/{id}`` (ADR 0160): ``seen`` with a snapshot, or feedback."""

    action: Literal["seen", "feedback", "dismissed"]
    reaction: str | None = Field(default=None, max_length=50)
    kind: str | None = Field(default=None, max_length=24)
    level: str | None = Field(default=None, max_length=24)
    facts: dict[str, Any] = Field(default_factory=dict)
    situation: dict[str, Any] = Field(default_factory=dict)


class MailDraftRequest(BaseModel):
    """Body of ``POST /inherent/mail/{id}/draft`` and ``/draft/send`` (ADR 0148): Allen's edit."""

    subject: str = Field(default="", max_length=300)
    body: str = Field(max_length=8000)


class MemoryEditRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/edit`` (ADR 0154): new words, a section to move to."""

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=2000)
    section: str | None = Field(default=None, max_length=40)


class MemoryItemRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/{delete,confirm}``: one item by id."""

    id: str = Field(min_length=1, max_length=64)


class MemoryKeepRequest(BaseModel):
    """Body of ``POST /inherent/memory/item/keep``: an item the nightly ``version`` marked stale."""

    version: str = Field(min_length=1, max_length=80)
    id: str = Field(min_length=1, max_length=64)


class MemoryUndoRequest(BaseModel):
    """Body of ``POST /inherent/memory/undo``: the version whose changes to take back."""

    version: str = Field(min_length=1, max_length=80)


class MemoryCapRequest(BaseModel):
    """Body of ``POST /inherent/memory/cap``: the core memory note's character cap."""

    max_chars: int


class MemoryDayEditRequest(BaseModel):
    """Body of ``POST /inherent/memory/day/edit``: the lines under each heading of one day."""

    day: str = Field(min_length=10, max_length=10)
    sections: dict[str, list[str]]


class ViewRow(BaseModel):
    """One row on the Dashboard's screen: its id and one line of title."""

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=500)
    mail_id: str = Field(default="", max_length=200)  # a Jobs row's newest mail (Gmail id)


class ViewItem(ViewRow):
    """The item open on the Dashboard: a ``mail``, ``memory``, ``agent``, ``plugin`` or ``job``."""

    kind: str = Field(min_length=1, max_length=24)


class ViewRequest(BaseModel):
    """Body of ``POST /inherent/view`` (ADR 0176): what the Dashboard shows; page null = closed."""

    page: str | None = Field(default=None, max_length=24)
    tab: str = Field(default="", max_length=24)
    item: ViewItem | None = None
    rows: list[ViewRow] = Field(default_factory=list, max_length=50)


def _no_notices() -> dict[str, Any]:
    """The ``GET /inherent/notices`` body of a daemon with nothing to show."""
    return {"notices": [], "audio_private": False, "hold": None, "departure": None}


async def _home_call[T](call: Awaitable[T]) -> T:
    """ADR 0051: not connected is 404 (the home's fallback), a bad id 400, anything else 502."""
    try:
        return await call
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from None
    except Exception as exc:  # noqa: BLE001 — Microsoft or the network failing is the home's 502.
        LOGGER.warning("home route failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None


async def _memory_call[T](call: Callable[[], T]) -> T:
    """ADR 0154: a memory page call off the loop; missing is 404, refused 400, blocked 409."""
    try:
        return await asyncio.to_thread(call)
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from None
    except Exception as exc:  # noqa: BLE001 — a store that cannot be read is the page's 502.
        LOGGER.warning("memory route failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None


def _register_memory_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0154: the Dashboard's memory page, all under ``/inherent/memory``."""
    if deps.memory_page is None:
        return
    page = deps.memory_page

    @app.get("/inherent/memory")
    async def memory_overview() -> dict[str, Any]:
        """The six sections, last night's changes and the counts."""
        return await _memory_call(page.overview)

    @app.get("/inherent/memory/item/{item_id}")
    async def memory_item(item_id: str) -> dict[str, Any]:
        """One item whole, with its sources as words and its history."""
        return await _memory_call(functools.partial(page.item, item_id))

    @app.post("/inherent/memory/item/edit", status_code=200)
    async def memory_edit(req: MemoryEditRequest) -> dict[str, Any]:
        """Edit or move an item: a user version, and the item is pinned."""
        return await _memory_call(functools.partial(page.edit, req.id, req.text, req.section))

    @app.post("/inherent/memory/item/delete", status_code=200)
    async def memory_delete(req: MemoryItemRequest) -> dict[str, Any]:
        """Delete an item: a user version that keeps what was removed."""
        return await _memory_call(functools.partial(page.delete, req.id))

    @app.post("/inherent/memory/item/confirm", status_code=200)
    async def memory_confirm(req: MemoryItemRequest) -> dict[str, Any]:
        """Say an item is right: a user version that changes no text."""
        return await _memory_call(functools.partial(page.confirm, req.id))

    @app.post("/inherent/memory/item/keep", status_code=200)
    async def memory_keep(req: MemoryKeepRequest) -> dict[str, Any]:
        """Put back an item a nightly version marked stale, pinned."""
        return await _memory_call(functools.partial(page.keep, req.version, req.id))

    @app.get("/inherent/memory/versions")
    async def memory_versions() -> dict[str, Any]:
        """One entry per core memory version, newest first."""
        return await _memory_call(page.versions)

    @app.post("/inherent/memory/undo", status_code=200)
    async def memory_undo(req: MemoryUndoRequest) -> dict[str, Any]:
        """Take back what a version changed, as a new version; 409 when a later one touched it."""
        return await _memory_call(functools.partial(page.undo, req.version))

    @app.post("/inherent/memory/cap", status_code=200)
    async def memory_cap(req: MemoryCapRequest) -> dict[str, Any]:
        """Save the note's character cap in settings; the nightly gate reads it at the next boot."""
        return await _memory_call(functools.partial(page.set_cap, req.max_chars))

    @app.get("/inherent/memory/search")
    async def memory_search(
        q: str = "", who: Literal["all", "user", "jarvis"] = "all",
    ) -> dict[str, Any]:
        """Every record with any of the words, grouped by day, newest first."""
        return await _memory_call(functools.partial(page.search, q[:200], who))

    @app.get("/inherent/memory/days")
    async def memory_days() -> dict[str, Any]:
        """One small card per day summary, newest first."""
        return await _memory_call(page.days)

    @app.get("/inherent/memory/day/{day}")
    async def memory_day(day: str) -> dict[str, Any]:
        """One day's summary under its headings."""
        return await _memory_call(functools.partial(page.day, day))

    @app.post("/inherent/memory/day/edit", status_code=200)
    async def memory_day_edit(req: MemoryDayEditRequest) -> dict[str, Any]:
        """Store the user's own version of a day summary; the night never rewrites it."""
        return await _memory_call(functools.partial(page.edit_day, req.day, req.sections))

    @app.get("/inherent/memory/day/{day}/records")
    async def memory_day_records(
        day: str, around: str | None = None, offset: int = 0, limit: int = 120,
    ) -> dict[str, Any]:
        """One day's conversation, oldest first, a page at a time."""
        return await _memory_call(functools.partial(page.day_records, day, around, offset, limit))


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


def _register_day_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0199: ``GET /inherent/day``, one day as timed items; open to every admitted caller."""
    if deps.day_read is None:
        return
    day_read = deps.day_read

    @app.get("/inherent/day")
    async def day(on: Annotated[str | None, Query(alias="date")] = None) -> dict[str, Any]:
        """``{date, tz, start_ms, end_ms, now_ms, items, missing}`` of ``?date=YYYY-MM-DD``."""
        try:
            wanted = None if on is None else parse_day(on)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return await _home_call(day_read(wanted))


def _register_departure_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0203: the notch's trip cards."""
    if deps.departure_act is None:
        return
    departure_act = deps.departure_act

    @app.post("/inherent/departure", status_code=200)
    async def departure(req: DepartureRequest) -> dict[str, Any]:
        """``{departure, reason}``: the pin now served, and why an action changed nothing."""
        return await _home_call(departure_act(req.model_dump()))


def _register_job_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0155: the job-mail notices and the job ledger; each route exists only when wired."""
    if deps.notices_read is not None:
        notices_read = deps.notices_read

        @app.get("/inherent/notices")
        async def notices() -> dict[str, Any]:
            """``{notices, audio_private, hold}``: the job-mail alerts a client may show now."""
            return await _home_call(notices_read())

    if deps.moment_read is not None:
        moment_read = deps.moment_read

        @app.get("/inherent/moment")
        async def moment() -> dict[str, Any]:
            """``{hold}``: ``call``, ``away`` or None; the client's own cards and sounds wait."""
            return await _home_call(moment_read())

    if deps.notice_act is not None:
        notice_act = deps.notice_act

        @app.post("/inherent/notices/{notice_id}", status_code=200)
        async def notice_action(notice_id: str, req: NoticeActionRequest) -> dict[str, bool]:
            """Mark a notice seen or record Allen's reaction; a digest id covers its alerts."""
            await _home_call(notice_act(notice_id, req.action, req.reaction))
            return {"ok": True}

    if deps.card_act is not None:
        card_act = deps.card_act

        @app.post("/inherent/cards/{card_id}", status_code=200)
        async def card_action(card_id: str, req: CardActionRequest) -> dict[str, bool]:
            """Store a shown card's snapshot (``seen``) or Allen's reaction to it."""
            await _home_call(card_act(card_id, req.model_dump()))
            return {"ok": True}

    if deps.jobs_read is not None:
        jobs_read = deps.jobs_read

        @app.get("/inherent/jobs")
        async def jobs() -> dict[str, Any]:
            """``{ledger, applications, skipped}``: job mail by company, the tracker, held back."""
            return await _home_call(jobs_read())

    if deps.application_add is not None:
        application_add = deps.application_add

        @app.post("/inherent/jobs/applications", status_code=200)
        async def application_add_route(req: JobApplicationRequest) -> dict[str, Any]:
            """Add an application Allen made that has no mail; returns its id."""
            return {"ok": True, "id": await _home_call(application_add(req.model_dump()))}

    if deps.application_edit is not None:
        application_edit = deps.application_edit

        @app.post("/inherent/jobs/applications/{app_id}", status_code=200)
        async def application_edit_route(
            app_id: str, req: JobApplicationEditRequest
        ) -> dict[str, bool]:
            """Change an application's status, date, note or hidden; a bad status is a 400."""
            await _home_call(application_edit(app_id, req.model_dump(exclude_unset=True)))
            return {"ok": True}

    if deps.application_cancel_reminders is not None:
        application_cancel_reminders = deps.application_cancel_reminders

        @app.post("/inherent/jobs/applications/{app_id}/cancel-reminders", status_code=200)
        async def application_cancel_reminders_route(app_id: str) -> dict[str, bool]:
            """Cancel an interview's two reminders and delete its Outlook event."""
            await _home_call(application_cancel_reminders(app_id))
            return {"ok": True}

    if deps.job_delete is not None:
        job_delete = deps.job_delete

        @app.post("/inherent/jobs/{message_id}/delete", status_code=200)
        async def job_delete_route(message_id: str) -> dict[str, bool]:
            """Hide one mail from the ledger; nothing is deleted in Gmail."""
            await _home_call(job_delete(message_id))
            return {"ok": True}

    if deps.job_flag is not None:
        job_flag = deps.job_flag

        @app.post("/inherent/jobs/{message_id}/flag", status_code=200)
        async def job_flag_route(message_id: str, req: JobFlagRequest) -> dict[str, bool]:
            """Allen says a held-back mail was job mail: read again, typed and delivered as such."""
            await _home_call(job_flag(message_id, req.reaction))
            return {"ok": True}


def _register_mail_page_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0147: the Dashboard's mail page: a letter whole, read and trash, the draft."""
    if deps.mail_letter is not None:
        mail_letter = deps.mail_letter

        @app.get("/inherent/mail/{message_id}")
        async def mail_letter_route(message_id: str) -> dict[str, Any]:
            """One letter whole: headers and a plain-text body (ADR 0148)."""
            return await _home_call(mail_letter(message_id))

    if deps.mail_summary is not None:
        mail_summary = deps.mail_summary

        @app.get("/inherent/mail/{message_id}/summary")
        async def mail_summary_route(message_id: str) -> dict[str, Any]:
            """``{summary}``: one sentence on the letter, written on demand and kept (ADR 0148)."""
            return await _home_call(mail_summary(message_id))

    if deps.mail_mark_read is not None:
        mail_mark_read = deps.mail_mark_read

        @app.post("/inherent/mail/read", status_code=200)
        async def mail_read_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Mark the letters Allen opened read; an id not listed or open is a 400."""
            await _home_call(mail_mark_read(req.ids, True))  # noqa: FBT003 — the route's body.
            return {"ok": True}

        @app.post("/inherent/mail/unread", status_code=200)
        async def mail_unread_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Mark letters unread again."""
            await _home_call(mail_mark_read(req.ids, False))  # noqa: FBT003 — the route's body.
            return {"ok": True}

    if deps.mail_trash is not None:
        mail_trash = deps.mail_trash

        @app.post("/inherent/mail/trash", status_code=200)
        async def mail_trash_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Move letters to Gmail's Trash (recoverable there); never a permanent delete."""
            await _home_call(mail_trash(req.ids, True))  # noqa: FBT003 — the route's body.
            return {"ok": True}

        @app.post("/inherent/mail/untrash", status_code=200)
        async def mail_untrash_route(req: MailArchiveRequest) -> dict[str, bool]:
            """Undo: take letters out of the Trash and back into the inbox."""
            await _home_call(mail_trash(req.ids, False))  # noqa: FBT003 — the route's body.
            return {"ok": True}

    if deps.view_set is not None:
        view_set = deps.view_set

        @app.post("/inherent/view", status_code=200)
        async def view(req: ViewRequest) -> dict[str, bool]:
            """What the Dashboard shows; repeated every 20 s, a null page closes (ADR 0176)."""
            item = None if req.item is None else (req.item.kind, req.item.id, req.item.title)
            rows = [(row.id, row.title, row.mail_id) for row in req.rows]
            view_set(req.page, req.tab, item, rows)
            return {"ok": True}

    if deps.mail_draft_read is not None:
        draft_read = deps.mail_draft_read

        @app.get("/inherent/mail/{message_id}/draft")
        async def mail_draft_route(message_id: str) -> dict[str, Any]:
            """``{draft: {revision, to, subject, body, by} | null}`` under the letter."""
            return await _home_call(draft_read(message_id))

    if deps.mail_draft_save is not None:
        draft_save = deps.mail_draft_save

        @app.post("/inherent/mail/{message_id}/draft", status_code=200)
        async def mail_draft_save_route(message_id: str, req: MailDraftRequest) -> dict[str, Any]:
            """Save Allen's own edit of the draft; answers like ``GET``."""
            return await _home_call(draft_save(message_id, req.subject, req.body))

    if deps.mail_draft_send is not None:
        draft_send = deps.mail_draft_send

        @app.post("/inherent/mail/{message_id}/draft/send", status_code=200)
        async def mail_draft_send_route(message_id: str, req: MailDraftRequest) -> dict[str, bool]:
            """Save the edit and put the send card up; nothing leaves until he presses it."""
            await _home_call(draft_send(message_id, req.subject, req.body))
            return {"ok": True}

    if deps.mail_draft_discard is not None:
        draft_discard = deps.mail_draft_discard

        @app.post("/inherent/mail/{message_id}/draft/discard", status_code=200)
        async def mail_draft_discard_route(message_id: str) -> dict[str, bool]:
            """Drop the draft."""
            await _home_call(draft_discard(message_id))
            return {"ok": True}


def _register_home_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0051/0052: Today, its to-do checkbox, mail, the morning brief and Settings."""
    if deps.today_read is not None and deps.todo_set is not None:
        today_read, todo_set = deps.today_read, deps.todo_set

        @app.get("/inherent/today")
        async def today() -> dict[str, Any]:
            """Today's calendar, open to-dos and the weather; no model call."""
            return await _home_call(today_read())

        @app.post("/inherent/today/todo", status_code=200)
        async def todo(req: TodoRequest) -> dict[str, bool]:
            """Check a to-do off in Microsoft To Do, or open it again."""
            await _home_call(todo_set(req.id, req.done))
            return {"ok": True}

    if deps.mail_read is not None:
        mail_read = deps.mail_read

        @app.get("/inherent/mail")
        async def mail() -> dict[str, Any]:
            """Unread mail from people, newest first."""
            return await _home_call(mail_read())

        if deps.mail_archive is not None:
            mail_archive = deps.mail_archive

            @app.post("/inherent/mail/archive", status_code=200)
            async def mail_archive_route(req: MailArchiveRequest) -> dict[str, bool]:
                """Archive the junk letters Allen tapped; an id not offered as junk is a 400."""
                await _home_call(mail_archive(req.ids, True))  # noqa: FBT003 — the route's body.
                return {"ok": True}

            @app.post("/inherent/mail/unarchive", status_code=200)
            async def mail_unarchive_route(req: MailArchiveRequest) -> dict[str, bool]:
                """Undo: put letters archived by the route above back in the inbox."""
                await _home_call(mail_archive(req.ids, False))  # noqa: FBT003 — the route's body.
                return {"ok": True}

    if deps.brief_read is not None:
        brief_read = deps.brief_read

        @app.get("/inherent/brief")
        async def brief() -> dict[str, Any]:
            """This morning's brief; 404 until yesterday's report is saved."""
            found = brief_read()
            if found is None:
                raise HTTPException(status_code=404, detail="no brief for today yet")
            return found

    if deps.settings_read is not None and deps.settings_update is not None:
        settings_read, settings_update = deps.settings_read, deps.settings_update

        @app.get("/inherent/settings")
        async def settings() -> dict[str, Any]:
            """``{values, options, restart_pending}`` for the Settings page."""
            return await settings_read()

        @app.post("/inherent/settings", status_code=200)
        async def settings_save(req: SettingsRequest) -> dict[str, Any]:
            """Save the changed values for the next boot; answers like ``GET``."""
            return await _home_call(settings_update(req.changes))

    if deps.board_status is not None:
        board_status = deps.board_status

        @app.get("/inherent/board")
        async def board() -> dict[str, Any]:
            """The reSpeaker board as the Settings page shows it (ADR 0147)."""
            return await board_status()

    if deps.restart is not None:
        restart = deps.restart

        @app.post("/inherent/restart", status_code=202)
        async def restart_now() -> dict[str, bool]:
            """Settings > Restart: the answer leaves first, then launchd brings Jarvis back."""
            restart()
            return {"ok": True}


def _register_data_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0067: take the data out as a zip, clear the recordings, erase everything."""
    if deps.data_export is not None:
        data_export = deps.data_export

        @app.get("/inherent/data/export")
        async def data_export_zip() -> FileResponse:
            """One zip of the user's data, as a download; the temp file goes once it is sent."""
            path = await data_export()
            return FileResponse(
                path, media_type="application/zip",
                filename=f"Jarvis-export-{time.strftime('%Y-%m-%d')}.zip",
                background=BackgroundTask(path.unlink, missing_ok=True),
            )

    if deps.data_clear is not None:
        data_clear = deps.data_clear

        @app.post("/inherent/data/clear-recordings", status_code=200)
        async def data_clear_recordings() -> dict[str, int]:
            """Delete every recording and screenshot now; the conversations stay."""
            return {"deleted": await data_clear()}

    if deps.data_erase is not None:
        data_erase = deps.data_erase

        @app.post("/inherent/data/erase", status_code=202)
        async def data_erase_all() -> dict[str, bool]:
            """Everything but the speech models goes at the restart this starts."""
            data_erase()
            return {"ok": True}


class DictationRequest(BaseModel):
    """Body of ``POST /inherent/dictation``: where the words will land, for the polish."""

    app: str = Field(default="", max_length=200)
    window: str = Field(default="", max_length=500)
    selected: str = Field(default="", max_length=20000)
    before: str = Field(default="", max_length=1000)


def dictation_stream(dictation: DictationRoutes, req: DictationRequest) -> StreamingResponse:
    """Record now; stream ``{level}`` lines, ``{state: thinking}``, then the result (ADR 0058).

    A session already running is a 409. A terminal's dictation (ADR 0183) answers the same way.
    """
    try:
        lines = dictation.begin(req.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:200]) from None

    async def body() -> AsyncIterator[bytes]:
        async for line in lines:
            yield (json.dumps(line, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(body(), media_type="application/x-ndjson")


def _register_dictation_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0058: one dictation at a time, streamed as NDJSON until its result."""
    if deps.dictation is None:
        return
    dictation = deps.dictation

    @app.post("/inherent/dictation")
    async def dictate(req: DictationRequest) -> StreamingResponse:
        """Record now; stream ``{level}`` lines, ``{state: thinking}``, then the result."""
        return dictation_stream(dictation, req)

    @app.post("/inherent/dictation/stop", status_code=200)
    async def dictate_stop() -> dict[str, bool]:
        """Finish recording; the open stream answers with the result."""
        return {"ok": dictation.stop()}


class NightRequest(BaseModel):
    """Body of ``POST /inherent/night``: start one (for ``hours``), go dark now, stay, or end it."""

    action: Literal["start", "dark", "stay", "end"]
    hours: float | None = Field(default=None, ge=0.25, le=12)


def register_night_routes(app: FastAPI, run: NightRoutes | None) -> None:
    """ADR 0093: the night run as the companion reads and drives it; none without a run."""
    if run is None:
        return
    night = run

    @app.get("/inherent/night")
    async def night_read() -> dict[str, Any]:
        """The run now (or null), the last one that ended, the default length."""
        return await asyncio.to_thread(night.snapshot)

    @app.post("/inherent/night", status_code=200)
    async def night_act(req: NightRequest) -> dict[str, Any]:
        """Start, go dark now, stay lit while the owner answers a session, or end; like ``GET``."""
        if req.action == "start":
            await asyncio.to_thread(
                functools.partial(
                    night.start, hours=req.hours, until=None, source="companion", action_id=None,
                ),
            )
        elif req.action == "dark":
            await asyncio.to_thread(night.darken_now)
        elif req.action == "stay":
            await asyncio.to_thread(night.stay)
        else:
            await asyncio.to_thread(functools.partial(night.end, action_id=None))
        return await asyncio.to_thread(night.snapshot)


async def _capped_body(request: Request, limit: int) -> bytes:
    """The request body, or a 413 as soon as it is over ``limit`` bytes (none of it kept)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="body too large")
    return bytes(body)


_MAX_SETUP_BODY_BYTES = 4096


async def _setup_body(request: Request) -> dict[str, Any]:
    """A setup request's JSON object; no echo of it in errors (it can hold an API key)."""
    raw = await request.body()
    if len(raw) > _MAX_SETUP_BODY_BYTES:
        raise HTTPException(status_code=413, detail="setup request too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    return body


def _register_setup_routes(app: FastAPI, deps: InherentDeps) -> None:
    """First-run setup: status, names, a key to test and keep, a voice preview, done."""
    if deps.setup is None:
        return
    setup = deps.setup

    @app.get("/inherent/setup")
    async def setup_status() -> dict[str, Any]:
        """Whether setup has run, what it saved, which keys work, what the user can use."""
        return await asyncio.to_thread(setup.read)

    @app.post("/inherent/setup/name")
    async def setup_names(request: Request) -> dict[str, Any]:
        """``{name?, assistant_name?}``; answers like ``GET``."""
        body = await _setup_body(request)
        return await _home_call(asyncio.to_thread(setup.save_names, body))

    @app.post("/inherent/setup/key")
    async def setup_key(request: Request) -> dict[str, Any]:
        """``{provider, key}``: run its checks, keep the key in the Keychain only when it works."""
        body = await _setup_body(request)
        return await _home_call(asyncio.to_thread(setup.check_key, body))

    @app.post("/inherent/setup/voice-preview")
    async def setup_preview(request: Request) -> Response:
        """``{voice_id, text?}``: that voice saying the line, as MP3."""
        body = await _setup_body(request)
        audio = await _home_call(asyncio.to_thread(setup.preview, body))
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/inherent/setup/done")
    async def setup_done() -> dict[str, Any]:
        """Setup will not show again; the daemon restarts to take in keys and names."""
        return setup.done()


_MAX_PLUGIN_COMMAND_BYTES = 32_768
_REQUEST_ID: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _refusal(exc: AttachmentRefused) -> HTTPException:
    """The HTTP answer for a file or a turn the attachment store will not take."""
    return HTTPException(status_code=exc.status, detail=str(exc))


async def _checked_attachments(deps: InherentDeps, ids: Sequence[str]) -> list[AttachmentRef]:
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
    return {"id": ref.id, "kind": ref.kind, "name": ref.name, "bytes": ref.size, "mime": ref.mime}


async def _submit_with_attachments(
    deps: InherentDeps, text: str, ids: Sequence[str],
) -> dict[str, str]:
    """``POST /inherent/submit`` with ``attachments``: the files go with the words (ADR 0211)."""
    unique = list(dict.fromkeys(ids))
    await _checked_attachments(deps, unique)
    if deps.submit_attachments is None:  # _checked_attachments refused already
        raise HTTPException(status_code=501, detail="attachments are not enabled")
    turn_id = await asyncio.to_thread(
        deps.submit_attachments, text or t("attach.no_words"), unique,
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
    deps: InherentDeps, share_callable: Callable[[dict[str, Any]], str], req: ShareRequest,
) -> dict[str, str]:
    """``POST /inherent/share``: a turn when he said ask her, otherwise one saved event."""
    fields = _share_fields(req)
    ids = list(dict.fromkeys(req.attachments))
    if ids:
        refs = await _checked_attachments(deps, ids)
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
    if ids and deps.submit_attachments is not None:
        return {
            "status": "accepted",
            "turn_id": await asyncio.to_thread(deps.submit_attachments, words, ids),
        }
    minted = await asyncio.to_thread(deps.submit_callable, words)
    return {"status": "accepted", "turn_id": minted or ""}


def create_app(deps: InherentDeps) -> FastAPI:  # noqa: C901, PLR0912, PLR0915 — one closed route table; the cancel and controls routes are registered only when injected.
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
        if req.attachments:
            return await _submit_with_attachments(deps, text, req.attachments)
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        turn_id = await asyncio.to_thread(deps.submit_callable, text)
        return {"status": "accepted", "turn_id": turn_id or ""}

    if deps.ask_outcome is not None:
        ask_outcome = deps.ask_outcome

        @app.post("/inherent/ask", status_code=200)
        async def ask(request: Request) -> Response:
            """A one-shot question: submit it like ``/inherent/submit``, answer with the reply.

            ``{"text"}`` (at most 2000 characters in a body of at most 4 KiB) is typed in, so the
            turn is never spoken on the Mac. Answers 200 ``{turn_id, spoken, written}`` from the
            turn's final answer (a wait line is not it), 502 ``{turn_id, detail}`` when the turn
            fails or is cancelled, and 504 ``{turn_id, detail}`` after 25 s; that turn keeps
            running and is in the conversation.
            """
            try:
                body = json.loads(await _capped_body(request, _ASK_BODY_BYTES) or b"{}")
            except ValueError:  # no echo of the body in an error
                body = None
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=400, detail="text required")
            text = text.strip()
            if len(text) > _ASK_TEXT_CHARS:
                raise HTTPException(
                    status_code=400, detail=f"text is over {_ASK_TEXT_CHARS} characters",
                )
            # Watch before submitting, so an answer sent at once still wakes the wait.
            with deps.broadcaster.watch_settled() as settled:
                turn_id = await asyncio.to_thread(deps.submit_callable, text) or ""
                deadline = time.monotonic() + _ASK_WAIT_S
                while True:
                    settled.clear()  # before the read: an answer written meanwhile sets it again
                    outcome = await asyncio.to_thread(ask_outcome, turn_id)
                    if outcome is not None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return JSONResponse(
                            {"turn_id": turn_id, "detail": "no answer yet; the turn goes on"},
                            status_code=504,
                        )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(settled.wait(), min(remaining, _ASK_RECHECK_S))
            if outcome.failure is not None:
                return JSONResponse(
                    {"turn_id": turn_id, "detail": outcome.failure}, status_code=502,
                )
            return JSONResponse(
                {"turn_id": turn_id, "spoken": outcome.spoken, "written": outcome.written},
            )

    if deps.attachments is not None:
        attachment_store = deps.attachments

        @app.post("/inherent/attachments", status_code=200)
        async def upload_attachment(request: Request) -> dict[str, Any]:
            """ADR 0211: one picture or text file from a phone, kept until it ages out."""
            return await _upload_attachment(deps, attachment_store, request)

    if deps.share_callable is not None:
        share_callable = deps.share_callable

        @app.post("/inherent/share", status_code=200)
        async def share(req: ShareRequest) -> dict[str, str]:
            """ADR 0211: a share from another app, as a turn (``ask``) or kept for later."""
            return await _share(deps, share_callable, req)

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
            if req.response_id:
                outcome = await asyncio.to_thread(
                    cancel_response_callable,
                    req.response_id,
                    req.scope,
                    req.reason,
                )
            elif req.turn_id and deps.cancel_turn_callable is not None:
                outcome = await asyncio.to_thread(
                    deps.cancel_turn_callable, req.turn_id, req.reason,
                )
            else:
                raise HTTPException(status_code=422, detail="response_id or turn_id required")
            return {"outcome": outcome}

    if deps.controls is not None:
        controls = deps.controls

        @app.post("/inherent/controls", status_code=200)
        async def set_controls(req: ControlsRequest) -> dict[str, object]:
            """Flip the mute switches and drive GPT-Live; answer with the full state.

            Conversation going on to off is the surface's exit: she is stopped at
            once and says nothing back (``dismiss_callable``).
            """
            before = controls.conversation
            state: dict[str, object] = dict(
                controls.update(
                    mic_muted=req.mic_muted,
                    speech_muted=req.speech_muted,
                    conversation=req.conversation,
                    quiet=req.quiet,
                ),
            )
            if req.mic_muted is not None or req.speech_muted is not None:
                LOGGER.info(
                    "controls: mic_muted=%s speech_muted=%s",
                    state["mic_muted"], state["speech_muted"],
                )
            if state["conversation"] != before:
                LOGGER.info("controls: conversation=%s", state["conversation"])
                if before and deps.dismiss_callable is not None:
                    try:
                        await asyncio.to_thread(deps.dismiss_callable)
                    except Exception:  # noqa: BLE001 - the switch is already flipped
                        LOGGER.warning("controls: dismissal failed", exc_info=True)
            if deps.live is None:
                if req.live is not None:
                    state["live"] = {"state": "unavailable", "reason": "gpt_live_disabled"}
                return state
            if req.live == "start":
                state["live"] = await deps.live.start()
            elif req.live == "stop":
                state["live"] = await deps.live.stop(reason="user")
            else:
                state["live"] = deps.live.status()
            return state

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
        # The `live` op is the client's only writer of live state (resonance
        # src/runtime.ts), so a fresh client needs one snapshot; sending it
        # through the broadcaster keeps it in the same order as the pushes.
        if deps.live is not None:
            with contextlib.suppress(Exception):
                await deps.broadcaster.broadcast_op("live", phase="state", **deps.live.status())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await deps.broadcaster.unregister(ws)
            # ADR 0041: with no surface left to leave wave mode, stop
            # listening without a wake word.
            if deps.controls is not None and deps.controls.conversation and not (
                deps.broadcaster.has_clients
            ):
                deps.controls.update(conversation=False)
                LOGGER.info("controls: conversation=False (last surface disconnected)")

    if deps.v2 is not None:
        v2_deps = deps.v2
        app.middleware("http")(_v2_input_auth_middleware(v2_deps))

        @app.websocket("/inherent/ws/v2")
        async def ws_v2_endpoint(ws: WebSocket) -> None:
            """ADR-0014 D5-D7 realtime socket. See :func:`_run_v2_session`.

            Deliberately NOT registered with the v1 broadcaster: v1 pushes
            ``{"op", "payload"}`` envelopes, which a v2 client would reject.
            """
            await _run_v2_session(v2_deps, ws)

        @app.post("/inherent/submit/v2", status_code=200)
        async def submit_v2(req: SubmitV2Request) -> SubmitV2Response:
            """ADR-0014 D21 authenticated idempotent text input.

            Registered beside the v2 socket and behind the same token, so a
            v1-only deployment's route table is byte-identical to what it was.
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

    if deps.terminals is not None and deps.device_name is not None:
        terminals, device_name = deps.terminals, deps.device_name

        @app.websocket(TERMINAL_PATH)
        async def ws_terminal(ws: WebSocket) -> None:
            """ADR 0170: a paired terminal's socket; only its own device token opens it.

            The middleware has already required a device token from a remote peer. A peer
            on loopback presents the local key there, which is not a device token, so it
            is refused here: a terminal always names itself by its token.
            """
            token = _v2_presented_token(ws.headers.get("authorization"))
            name = None if token is None else device_name(token)
            if name is None:
                await ws.close(code=1008)
                return
            await serve_terminal(terminals, ws, name)

    if deps.phone is not None and deps.device_name is not None:
        phone_hub, phone_socket_name = deps.phone, deps.device_name

        @app.websocket(PHONE_PATH)
        async def ws_phone(ws: WebSocket) -> None:
            """ADR 0209: a paired phone's conversation; only its own device token opens it.

            As on ``/terminal/ws`` and the phone-events route, the local key is not a device
            token and is refused here: a phone always names itself by its token.
            """
            token = _v2_presented_token(ws.headers.get("authorization"))
            name = None if token is None else phone_socket_name(token)
            if name is None:
                await ws.close(code=1008)
                return
            await serve_phone(phone_hub, ws, name)

    if deps.phone_events is not None and deps.device_name is not None:
        phone_name, brain_events = deps.device_name, deps.phone_events

        @app.post(PHONE_EVENTS_PATH)
        async def post_phone_events(request: Request) -> dict[str, Any]:
            """ADR 0197: a paired phone's batch of sensed events, each appended once.

            Only a paired device's own token opens it; the local key is refused here as it
            is on ``/terminal/ws``. ``async`` on purpose: the append runs on the loop
            thread that owns the runtime's log connection, as the socket's does, and a
            plain ``def`` would run it on a worker thread the connection is closed to.
            """
            token = _v2_presented_token(request.headers.get("authorization"))
            name = None if token is None else phone_name(token)
            if name is None:
                raise HTTPException(
                    status_code=403, detail="a paired device's token is required",
                )
            try:
                body = json.loads(await _capped_body(request, MAX_BATCH_BYTES))
            except (ValueError, RecursionError):
                body = None
            frames = body.get("events") if isinstance(body, dict) else None
            if not isinstance(frames, list):
                raise HTTPException(status_code=400, detail='send {"events": [...]}')
            if len(frames) > MAX_BATCH_FRAMES:
                raise HTTPException(status_code=413, detail="too many events in one batch")
            return {"acks": brain_events.record_phone_batch(name, frames)}

    if deps.push_register is not None and deps.device_name is not None:
        push_name, push_register = deps.device_name, deps.push_register

        @app.post(REGISTER_PATH)
        async def post_push_registration(request: Request) -> dict[str, bool]:
            """ADR 0210: a paired phone registers its APNs device token (and Live Activity token).

            Only a paired device's own token opens it; the local key is refused here as it is on
            ``/inherent/device/events``. A refusal never repeats a value of the body.
            """
            token = _v2_presented_token(request.headers.get("authorization"))
            name = None if token is None else push_name(token)
            if name is None:
                raise HTTPException(
                    status_code=403, detail="a paired device's token is required",
                )
            try:
                body = json.loads(await _capped_body(request, MAX_REGISTER_BYTES))
            except (ValueError, RecursionError):
                body = None
            try:
                await asyncio.to_thread(push_register, name, body)
            except RegistrationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return {"ok": True}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Liveness probe — used by ops scripts to confirm the daemon is up."""
        return {"status": "ok"}

    if deps.plugin_read and deps.plugin_action and deps.plugin_authorize:
        plugin_read, plugin_action, plugin_authorize = (
            deps.plugin_read,
            deps.plugin_action,
            deps.plugin_authorize,
        )

        @app.get("/inherent/plugins")
        async def plugins(request: Request) -> dict[str, Any]:
            if not plugin_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            return await asyncio.to_thread(plugin_read)

        if deps.plugin_icon is not None:
            plugin_icon = deps.plugin_icon

            @app.get("/inherent/plugins/{plugin_id}/icon")
            async def plugin_logo(plugin_id: str, request: Request) -> dict[str, str | None]:
                if not plugin_authorize(request.headers.get("authorization")):
                    raise HTTPException(status_code=401, detail="desktop authorization required")
                return {"icon": await asyncio.to_thread(plugin_icon, plugin_id)}

        @app.post("/inherent/plugins/action")
        async def plugin_command(request: Request) -> dict[str, Any]:
            if not plugin_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            body = await request.body()
            if len(body) > _MAX_PLUGIN_COMMAND_BYTES:
                raise HTTPException(status_code=413, detail="plugin command too large")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("data", {}), dict):
                    raise HTTPException(status_code=400, detail="invalid plugin request")
                return await asyncio.to_thread(
                    plugin_action, str(payload.get("operation", "")), payload.get("data", {})
                )
            except ValueError as exc:
                # No Pydantic error echo: request bodies can contain credentials.
                bad_json = isinstance(exc, json.JSONDecodeError)
                detail = t("plugin.bad_request") if bad_json else str(exc)
                raise HTTPException(status_code=400, detail=detail) from None

    if deps.language_save is not None and deps.plugin_authorize is not None:
        language_save, language_authorize = deps.language_save, deps.plugin_authorize

        @app.get("/inherent/language")
        async def language_read() -> dict[str, str]:
            """The language Jarvis speaks and writes its fixed text in."""
            return {"language": language()}

        @app.post("/inherent/language", status_code=200)
        async def language_write(request: Request) -> dict[str, str]:
            """Switch to ``{"language": "zh" | "en"}`` now and keep it in settings.yaml."""
            if not language_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                code = json.loads(await request.body()).get("language")
                chosen = await asyncio.to_thread(language_save, str(code))
            except (ValueError, AttributeError) as exc:
                raise HTTPException(status_code=400, detail="language must be zh or en") from exc
            return {"language": chosen}

    if deps.usage_read is not None and deps.usage_refresh is not None:
        usage_read, usage_refresh = deps.usage_read, deps.usage_refresh

        @app.get("/inherent/usage")
        async def usage() -> dict[str, Any]:
            """ADR-0018: latest quota / spend / balance snapshot per service."""
            return usage_read()

        @app.post("/inherent/usage/refresh", status_code=200)
        async def usage_refresh_now() -> dict[str, Any]:
            """ADR-0018: poll every source now, then answer like ``GET``."""
            return await usage_refresh()

    if deps.usage_codex_reset is not None and deps.plugin_authorize is not None:
        codex_reset, desktop_authorize = deps.usage_codex_reset, deps.plugin_authorize

        @app.post("/inherent/usage/codex/reset", status_code=200)
        async def usage_codex_reset(request: Request) -> dict[str, Any]:
            """ADR 0048: spend one Codex limit reset; the request id makes a retry a no-op."""
            if not desktop_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                request_id = json.loads(await request.body()).get("request_id")
            except (ValueError, AttributeError):
                request_id = None
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise HTTPException(status_code=400, detail="request_id must be a UUID")
            try:
                return await asyncio.to_thread(codex_reset, request_id)
            except (OSError, ValueError) as exc:  # urllib errors are OSError.
                raise HTTPException(status_code=502, detail=str(exc)[:200]) from None

    if deps.usage_record_balance is not None and deps.plugin_authorize is not None:
        record_balance, balance_authorize = deps.usage_record_balance, deps.plugin_authorize

        @app.post("/inherent/usage/balance", status_code=200)
        async def usage_balance(request: Request) -> dict[str, Any]:
            """ADR 0065: record a balance; the next usage poll subtracts the spend since."""
            if not balance_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                body = json.loads(await request.body())
                service, usd = body.get("service"), body.get("usd")
            except (ValueError, AttributeError):
                service = usd = None
            amount = isinstance(usd, int | float) and not isinstance(usd, bool)
            if not isinstance(service, str) or not amount:
                raise HTTPException(status_code=400, detail="needs a service and a usd amount")
            try:
                record_balance(service, float(usd))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return {"recorded": True}

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

    if deps.conversation_read is not None:
        conversation_read = deps.conversation_read

        @app.get("/inherent/conversation")
        async def conversation(after: int = 0, limit: int = 200) -> dict[str, Any]:
            """Spec §18.3: the conversation of record past ``after`` (0 = the newest rows)."""
            return conversation_read(after, limit)

    if deps.card_read is not None and deps.card_decide is not None:
        card_read, card_decide = deps.card_read, deps.card_decide

        @app.get("/inherent/confirmation")
        async def confirmation_card() -> dict[str, Any]:
            """ADR 0062: the pending card, or ``{"card": null}``."""
            return await asyncio.to_thread(card_read)

        @app.post("/inherent/confirmation", status_code=200)
        async def confirmation_answer(req: CardDecisionRequest) -> dict[str, str]:
            """ADR 0062: send or dismiss the card on screen; 409 once it is not the pending one."""
            card = (await asyncio.to_thread(card_read)).get("card")
            if not card or card.get("id") != req.confirmation_id:
                raise HTTPException(status_code=409, detail="that card is no longer waiting")
            turn_id = await asyncio.to_thread(
                card_decide, req.confirmation_id, req.decision, dict(req.edits),
            )
            return {"status": "accepted", "turn_id": turn_id}
    if deps.question_read is not None and deps.question_answer is not None:
        question_read, question_answer = deps.question_read, deps.question_answer

        @app.get("/inherent/clarification")
        async def clarification_card() -> dict[str, Any]:
            """ADR 0066: the ask card waiting to be filled in, or ``{"card": null}``."""
            return await asyncio.to_thread(question_read)

        @app.post("/inherent/clarification", status_code=200)
        async def clarification_answer(req: QuestionAnswerRequest) -> dict[str, Any]:
            """ADR 0066: fill in or dismiss the ask card; 409 once it is not the one waiting."""
            if not req.dismiss and not any(v.strip() for v in req.answers.values()):
                raise HTTPException(status_code=400, detail="nothing was filled in")
            try:
                turn_id = await asyncio.to_thread(
                    question_answer,
                    req.clarification_id,
                    None if req.dismiss else dict(req.answers),
                )
            except LookupError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"status": "dismissed" if turn_id is None else "accepted", "turn_id": turn_id}
    if deps.think_read is not None:
        think_read = deps.think_read

        @app.get("/inherent/think")
        async def think() -> dict[str, Any]:
            """ADR 0108: ``{on, on_words, turn_id}`` for the companion's deep look."""
            return think_read()

    _register_day_route(app, deps)
    _register_home_routes(app, deps)
    _register_mail_page_routes(app, deps)
    _register_job_routes(app, deps)
    _register_departure_route(app, deps)
    _register_memory_routes(app, deps)
    _register_data_routes(app, deps)
    _register_setup_routes(app, deps)
    _register_dictation_routes(app, deps)
    register_night_routes(app, deps.night)
    register_pairing_routes(app, deps.pairing)

    # ADR 0019 step 4: Allen's own Codex sessions, fed by scripts/codex_hook_log.py.
    codex_board = deps.codex_board

    @app.post("/inherent/codex-hook", status_code=200)
    async def codex_hook(payload: dict[str, Any]) -> dict[str, bool]:
        """Fold one Codex hook payload into the session board; never a decision."""
        fold_codex_hook(codex_board, payload, now_ms=int(time.time() * 1000))
        return {"ok": True}

    @app.get("/inherent/codex-sessions")
    async def codex_sessions() -> dict[str, Any]:
        """Newest-first rows for the Resonance Codex card."""
        prune_codex_sessions(codex_board, now_ms=int(time.time() * 1000))
        settle_codex_sessions(codex_board)
        rows = sorted(codex_board.values(), key=lambda r: int(r["since_ms"]), reverse=True)
        return {"sessions": rows}

    # ADR 0046: Allen's own Claude Code sessions, read from Claude Code's own state;
    # ADR 0049: with the prompts Jarvis holds for them and their compacting / stopped marks.
    # ADR 0170: on a brain (``terminals``) the session files are the terminal's, so the board, a
    # conversation and a reply are asked of it, and the brain's own files are never read.
    claude_board = ClaudeSessions(
        deps.turn_end_peek, deps.turn_end_answered,
        None if deps.terminals is None else deps.terminals.call,
    )
    claude_hooks = ClaudeHooks(
        lambda: deps.controls.quiet if deps.controls is not None else "off",
        deps.claude_request,
    )

    async def read_claude_board() -> dict[str, Any]:
        """The board with its held prompts; reading it is what tells a hook someone listens."""
        if not deps.claude_sessions_read:
            return claude_hooks.merge({"sessions": []})
        return claude_hooks.merge(await asyncio.to_thread(claude_board.read))

    @app.get("/inherent/claude-sessions")
    async def claude_sessions() -> dict[str, Any]:
        """Newest-first Claude Code session rows for the Resonance Agents page."""
        return await read_claude_board()

    @app.get("/inherent/waiting")
    async def waiting(request: Request) -> dict[str, Any]:
        """Everything waiting on Allen, read once; answering stays on each card's own route.

        ``{confirmation, clarification, notices, plugin_request, claude_requests}``: the card of
        ``GET /inherent/confirmation`` and of ``/clarification`` (``card``), the body of
        ``/notices``, ``request`` of ``/plugins``, and each Claude Code session's held prompt
        (the ``request`` of its ``/claude-sessions`` row with the session's id, title and
        project). Nothing waiting is ``null`` or empty, and so is a source this daemon does not
        have. The board is read exactly as ``/claude-sessions`` reads it, so a phone that asks
        for this keeps Claude Code's prompts held.
        """

        async def card(read: Callable[[], dict[str, Any]] | None) -> dict[str, Any] | None:
            found = None if read is None else (await asyncio.to_thread(read)).get("card")
            return found if isinstance(found, dict) else None

        async def notices() -> dict[str, Any]:
            return _no_notices() if deps.notices_read is None else await deps.notices_read()

        async def plugin_request() -> dict[str, Any] | None:
            if deps.plugin_read is None or deps.plugin_authorize is None:
                return None
            if not deps.plugin_authorize(request.headers.get("authorization")):
                return None
            found = (await asyncio.to_thread(deps.plugin_read)).get("request")
            return found if isinstance(found, dict) else None

        async def claude_requests() -> list[dict[str, Any]]:
            rows = (await read_claude_board())["sessions"]
            return [
                {
                    **row["request"],
                    "session_id": row["session_id"],
                    "title": row.get("title", ""),
                    "project": row.get("project", ""),
                }
                for row in rows
                if row.get("request")
            ]

        async def source[T](name: str, read: Awaitable[T], empty: T) -> T:
            # One source down (a mail or Microsoft call) leaves the rest of what waits readable.
            try:
                return await read
            except Exception as exc:  # noqa: BLE001 — a source failing is an empty one, logged.
                LOGGER.warning("waiting: %s unreadable: %s: %s", name, type(exc).__name__, exc)
                return empty

        confirmation, clarification, notice_body, plugin, claude = await asyncio.gather(
            source("confirmation", card(deps.card_read), None),
            source("clarification", card(deps.question_read), None),
            source("notices", notices(), _no_notices()),
            source("plugin request", plugin_request(), None),
            claude_requests(),
        )
        return {
            "confirmation": confirmation,
            "clarification": clarification,
            "notices": notice_body,
            "plugin_request": plugin,
            "claude_requests": claude,
        }

    @app.get("/inherent/claude-sessions/{session_id}/conversation")
    async def claude_conversation(session_id: str) -> dict[str, Any]:
        """ADR 0070: what Allen said and each turn's final answer, for the island's page."""
        if not deps.claude_sessions_read:
            raise HTTPException(status_code=404, detail="reading Claude Code's files is off")
        try:
            return await asyncio.to_thread(claude_board.conversation, session_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such session") from None
        except OSError as exc:  # the terminal is not there, or reads no Claude Code files
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None

    @app.post("/inherent/claude-sessions/{session_id}/reply", status_code=200)
    async def claude_reply(session_id: str, body: dict[str, Any]) -> dict[str, bool]:
        """ADR 0070: type Allen's line into an idle background session; 409 if it cannot."""
        text = body.get("text")
        if not deps.claude_sessions_read or not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="a reply needs text")
        try:
            await asyncio.to_thread(claude_board.reply, session_id, text[:_REPLY_CHARS])
        except LookupError:
            raise HTTPException(status_code=409, detail="it cannot take a reply now") from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
        return {"ok": True}

    @app.post("/inherent/agents/turn-end", status_code=200)
    async def turn_end(req: TurnEndRequest) -> dict[str, bool | None]:
        """ADR 0125: does the end of a finished agent turn ask Allen something; null = unknown."""
        if deps.turn_end_asks is None:
            return {"asks": None}
        return {"asks": await deps.turn_end_asks(req.session_id, req.text)}

    @app.post("/inherent/agents/turn-end/answered", status_code=200)
    async def turn_end_answered(req: TurnEndAnswered) -> dict[str, bool]:
        """ADR 0128: Allen just sent a message in a Startrail session; fire and forget."""
        if deps.turn_end_answered is not None:
            deps.turn_end_answered(req.session_id, [time.time()])
        return {"ok": True}

    if deps.agent_marks_path is not None:
        marks = AgentMarks(deps.agent_marks_path)

        @app.get("/inherent/agent-marks")
        async def agent_marks() -> dict[str, Any]:
            """ADR 0069: unread, parked and archived, per session, for every surface."""
            return marks.read()

        @app.post("/inherent/agent-marks/{session_id}", status_code=200)
        async def agent_mark(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
            """ADR 0069: ``seen`` / ``unread`` / ``park`` / ``archive`` on one session."""
            if not 0 < len(session_id) <= _SESSION_ID_CHARS:
                raise HTTPException(status_code=400, detail="bad session id")
            return await asyncio.to_thread(marks.update, session_id, body)

    @app.post("/inherent/claude-hook", status_code=200)
    async def claude_hook(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """ADR 0049: the body is the hook's stdout; ``{}`` is no decision."""
        if payload.get("hook_event_name") == "PermissionRequest":
            return await claude_hooks.permission(payload, request.is_disconnected)
        claude_hooks.event(payload)
        return {}

    @app.post("/inherent/claude-requests/{request_id}", status_code=200)
    async def claude_request_answer(request_id: str, body: dict[str, Any]) -> dict[str, bool]:
        """ADR 0049: Allen's answer from the notice card; 404 once the prompt is gone."""
        if not claude_hooks.answer(request_id, body):
            raise HTTPException(status_code=404, detail="that prompt is no longer waiting")
        return {"ok": True}

    return app


__all__ = [
    "AskOutcome",
    "ControlsRequest",
    "DictationRequest",
    "DictationRoutes",
    "InherentDeps",
    "InherentV2Deps",
    "InputSubmissionOutcome",
    "MemoryRoutes",
    "NightRoutes",
    "SettingsRequest",
    "SubmitRequest",
    "V2ClientHandle",
    "V2Session",
    "create_app",
    "dictation_stream",
    "manager_authorize",
    "register_night_routes",
    "require_local_key",
]
