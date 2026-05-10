"""Jarvis Live2D Web Server — FastAPI + SSE."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import mimetypes
import os
import shutil
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import io

from ui.web.browser_ws_player import BrowserWSPlayer
from core.tts_minimax_ws import MinimaxWSClient


# MiniMax WS-collect path writes raw 32kHz mono 16-bit PCM to .pcm files.
# Browsers can't decode raw PCM — wrap with a 44-byte WAV header at serve time
# so the frontend <audio> element plays it natively.
_WAV_SR = 32000
_WAV_CH = 1
_WAV_BITS = 16


def _wrap_pcm_to_wav(pcm_path: Path, wav_path: Path,
                     sample_rate: int = _WAV_SR,
                     channels: int = _WAV_CH,
                     bits: int = _WAV_BITS) -> None:
    """Read raw PCM bytes from pcm_path, write WAV file with minimal header."""
    pcm = pcm_path.read_bytes()
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                      byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    wav_path.write_bytes(header + pcm)

from fastapi import FastAPI, Form, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.inherent_wake_listener import InherentWakeListener, inherent_wake_enabled

LOGGER = logging.getLogger(__name__)
_INHERENT_IMAGE_MAX_BYTES = 15 * 1024 * 1024
_INHERENT_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
_DIRECT_TOOL_ALLOWLIST = {"cc_tell", "cc_slash", "cc_interrupt"}

# --- Browser TTS streaming state (Task 2) ---
_ws_routes: dict[str, Any] = {}          # session_id → WebSocket
_ws_routes_lock = asyncio.Lock()
_active_chats: dict[str, dict] = {}      # session_id → {turn_id, abort_event, ws_client}
_active_chats_lock = asyncio.Lock()

# --- Inherent card bridge: broadcast jarvis response.* events to every
# connected /inherent/ws client. Multiple clients = multiple Electron cards
# all showing the same content (acceptable for single-user setup).
_inherent_clients: set[Any] = set()

# --- cc-mode turn counters for trace.turn_id (process-local, fine to reset
# on restart since trace_id is the canonical key). Keyed by ':cc'-suffixed
# session_id so they live in their own namespace and never collide with
# /api/chat turn ids.
_cc_turn_counters: dict[str, int] = {}
_cc_turn_lock = threading.Lock()


def _next_cc_turn(session_id: str) -> int:
    with _cc_turn_lock:
        n = _cc_turn_counters.get(session_id, 0) + 1
        _cc_turn_counters[session_id] = n
        return n


async def _send_ctrl(ws: Any, payload: dict) -> None:
    """Send a JSON control frame. Silently drops if ws has gone away."""
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        LOGGER.debug("ws send_text failed: %s", exc)


def _apply_heard_response(
    jarvis_app: Any,
    session_id: str,
    played_texts: list[str],
) -> bool:
    """Write the heard-only assistant message back to conversation_store.

    Fetches the current history, calls
    ``JarvisApp._truncate_assistant_for_interrupt`` to shrink the last
    assistant message to what the user actually heard (plus an interrupt
    marker), and replaces the store entry. Returns True on success.
    Logs and returns False on any failure so an unwritable store doesn't
    crash the turn teardown.
    """
    try:
        history = jarvis_app.conversation_store.get_history(session_id) or []
        if not history:
            return False
        truncated = jarvis_app._truncate_assistant_for_interrupt(
            history, played_texts,
        )
        jarvis_app.conversation_store.replace(session_id, truncated)
        LOGGER.info(
            "[chat] WP5 heard_response: %d sentence(s) written to store",
            len(played_texts),
        )
        return True
    except Exception as exc:
        LOGGER.warning(
            "[chat] WP5 conversation_store writeback failed: %s",
            exc, exc_info=True,
        )
        return False


def _compute_played_texts(
    sentence_results: list[dict],
    abort_event: threading.Event,
    sample_rate: int,
) -> list[str]:
    """Reconstruct what the user actually heard when a turn was aborted.

    Mirrors ``TTSPipeline.abort``'s WP5 logic for the Web path. Full-played
    sentences contribute their whole text; the one sentence that was
    mid-playback when abort landed is truncated via ``_wp5_truncate`` using
    the browser-reported playback cursor. Returns [] if the turn completed
    normally (no abort).

    Args:
        sentence_results: Per-sentence entries from ``_stream_one`` finally,
            each ``{"idx", "text", "result": PlaybackResult | None}``.
        abort_event: Turn-level abort flag; [] is returned when unset.
        sample_rate: Player output sample rate (32 kHz for Web).
    """
    if not abort_event.is_set() or not sentence_results:
        return []
    from core.tts import _wp5_truncate

    played: list[str] = []
    for entry in sorted(sentence_results, key=lambda e: e["idx"]):
        result = entry["result"]
        if result is None:
            continue
        if getattr(result, "completed", False):
            played.append(entry["text"])
            continue
        # Mid-playback on abort — truncate to what cursor reports as heard.
        # Only one sentence can be in this state under tts_lock serialization.
        heard = _wp5_truncate(
            text=entry["text"],
            played_samples=getattr(result, "played_samples", 0),
            sentence_start_samples=getattr(result, "sentence_start_samples", 0),
            total_samples=getattr(result, "total_samples", None),
            subtitle_url=getattr(result, "subtitle_url", None),
            sample_rate=sample_rate,
        )
        if heard:
            played.append(heard)
        break
    return played


def _write_cc_trace(
    jarvis_app: Any,
    req: "ToolExecuteRequest",
    result: str | None,
    err: Exception | None,
    elapsed_ms: int,
) -> None:
    """Write a single trace row for a cc-mode tool execution.

    Best-effort caller is responsible for swallowing exceptions; this
    function only validates inputs and delegates to ``trace_log.log_turn``.
    """
    from memory.trace import (
        EndReason, InputDevice, Mode, PathTaken, TriggerSource,
    )

    trace_log = getattr(jarvis_app, "trace_log", None)
    if trace_log is None:
        return

    trace_session = f"{req.session_id}:cc" if req.session_id else "cc:anonymous"
    turn_id = _next_cc_turn(trace_session)

    # tool_calls — single-element list mirroring the LLM trace shape so
    # downstream analytics can union over both code paths.
    tool_call: dict[str, Any] = {
        "name": req.name,
        "args": req.args or {},
        "ms": elapsed_ms,
    }
    if err is None:
        tool_call["result"] = result
    else:
        tool_call["error"] = str(err)

    # user_text fallback: if FE didn't send raw input, synthesize from
    # tool args so the column is never empty for cc rows.
    user_text = req.user_text
    if not user_text:
        if req.name == "cc_tell":
            user_text = str((req.args or {}).get("text", ""))
        elif req.name == "cc_slash":
            cmd = (req.args or {}).get("command", "")
            extra = (req.args or {}).get("args", "")
            user_text = f"/{cmd}" + (f" {extra}" if extra else "")
        else:
            user_text = f"[{req.name}]"

    trace_log.log_turn(
        session_id=trace_session,
        turn_id=turn_id,
        user_text=user_text,
        assistant_text="",
        trigger_source=TriggerSource.WEB_TEXT.value,
        path_taken=PathTaken.LOCAL.value,
        tool_calls=[tool_call],
        latency_ms=elapsed_ms,
        end_reason=(EndReason.SUCCESS if err is None else EndReason.ERROR).value,
        error=(None if err is None else str(err)),
        input_metadata={"hostname": socket.gethostname()},
        mode=Mode.CC.value,
        input_device=InputDevice.PET_APP.value,
    )


class ChatRequest(BaseModel):
    text: str
    session_id: str

class InherentSubmitRequest(BaseModel):
    text: str

class SessionResponse(BaseModel):
    session_id: str
    status: str

class HiddenModeRequest(BaseModel):
    session_id: str = ""
    enabled: bool = False


class LLMSwitchRequest(BaseModel):
    preset: str


class ToolExecuteRequest(BaseModel):
    name: str
    args: dict = {}
    # Trace metadata — optional. session_id ties the row to the panel's
    # FastAPI session (suffixed ':cc' so turn_id space stays separate from
    # /api/chat). user_text is the raw input the user typed in cc mode
    # before this request parsed it into name+args.
    session_id: str = ""
    user_text: str = ""


AUDIO_DIR = Path(__file__).parent / "audio_cache"


def create_app(jarvis_app: Any) -> FastAPI:
    """Create FastAPI app wrapping a JarvisApp instance."""
    app = FastAPI(title="Jarvis Live2D Web")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    sessions: dict[str, dict] = {}

    # --- Task 6: VAD cancel via event_bus ---
    def _on_tts_cancelled(payload: dict | None = None) -> None:
        """EventBus handler for VAD-triggered interrupts. Fan out cancel
        frames to every live browser WS with an active chat."""
        if payload is None:
            payload = {}
        reason = payload.get("reason", "vad")
        loop = getattr(jarvis_app, "_web_loop", None)
        if not isinstance(loop, asyncio.AbstractEventLoop):
            return

        async def _fan_out() -> None:
            for sid, chat in list(_active_chats.items()):
                ws = _ws_routes.get(sid)
                if ws is None:
                    continue
                await _send_ctrl(ws, {
                    "type": "cancel",
                    "turn_id": chat["turn_id"],
                    "reason": reason,
                })
                try:
                    chat["abort_event"].set()
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_fan_out(), loop)

    if hasattr(jarvis_app, "event_bus") and jarvis_app.event_bus is not None:
        jarvis_app.event_bus.on("jarvis.tts_cancelled", _on_tts_cancelled)

        # --- Inherent card bridge ---
        # Forward jarvis response.{start,chunk,final} events as siri:*
        # protocol JSON to every connected /inherent/ws client. Cross-thread
        # safe: event_bus.emit fires on jarvis main thread, we hop to the web
        # asyncio loop via run_coroutine_threadsafe. No-op if no clients
        # connected (loop not yet captured) — events are simply dropped.
        def _broadcast_inherent(op: str, payload: dict) -> None:
            loop = getattr(jarvis_app, "_web_loop", None)
            if not isinstance(loop, asyncio.AbstractEventLoop):
                return
            msg = json.dumps({"op": op, "payload": payload}, ensure_ascii=False)

            async def _fan_out() -> None:
                dead = []
                for ws in list(_inherent_clients):
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _inherent_clients.discard(ws)

            asyncio.run_coroutine_threadsafe(_fan_out(), loop)

        def _on_response_start(_payload: dict | None = None) -> None:
            LOGGER.info("[inherent-bridge] response.start fired clients=%d loop_set=%s",
                        len(_inherent_clients),
                        getattr(jarvis_app, "_web_loop", None) is not None)
            payload = {
                "content": "",
                "streaming": True,
                "kind": "text",
            }
            q = (_payload or {}).get("q")
            if q:
                payload["q"] = q
            _broadcast_inherent("open", payload)

        def _on_response_chunk(payload: dict | None = None) -> None:
            text = (payload or {}).get("text")
            LOGGER.info("[inherent-bridge] response.chunk fired text=%r clients=%d",
                        (text or "")[:40], len(_inherent_clients))
            if not text:
                return
            _broadcast_inherent("append", {"token": text})

        def _on_response_final(_payload: dict | None = None) -> None:
            LOGGER.info("[inherent-bridge] response.final fired clients=%d", len(_inherent_clients))
            _broadcast_inherent("done", {"fadeMs": 5000})

        jarvis_app.event_bus.on("response.start", _on_response_start)
        jarvis_app.event_bus.on("response.chunk", _on_response_chunk)
        jarvis_app.event_bus.on("response.final", _on_response_final)
        LOGGER.info("[inherent-bridge] listeners registered for response.start/chunk/final")

        def _emit_inherent_voice(phase: str, payload: dict[str, Any]) -> None:
            voice_payload = {"phase": phase}
            voice_payload.update(payload)
            _broadcast_inherent("voice", voice_payload)

        if inherent_wake_enabled(getattr(jarvis_app, "config", None)):
            listener = getattr(jarvis_app, "_inherent_wake_listener", None)
            if not isinstance(listener, InherentWakeListener):
                listener = InherentWakeListener(jarvis_app, _emit_inherent_voice)
                setattr(jarvis_app, "_inherent_wake_listener", listener)
            listener.start()
            LOGGER.info("[inherent-wake] listener requested from web backend")

    def _shutdown_inherent_wake_listener() -> None:
        listener = getattr(jarvis_app, "_inherent_wake_listener", None)
        if isinstance(listener, InherentWakeListener):
            listener.stop()
    app.router.add_event_handler("shutdown", _shutdown_inherent_wake_listener)

    AUDIO_DIR.mkdir(exist_ok=True)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/session", response_model=SessionResponse)
    def create_session():
        sid = str(uuid.uuid4())
        sessions[sid] = {"active": True}
        LOGGER.info("Session created: %s", sid)
        return SessionResponse(session_id=sid, status="connected")

    @app.delete("/api/session/{session_id}", response_model=SessionResponse)
    async def delete_session(session_id: str):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
        # Abort any in-flight chat on this session + flush the browser ring
        # so paced residual PCM stops immediately. Without this, the drain
        # task keeps feeding the WS for seconds after the user thinks they
        # hung up.
        loop = asyncio.get_running_loop()
        prev = _active_chats.get(session_id)
        ws = _ws_routes.get(session_id)
        if ws is not None:
            asyncio.run_coroutine_threadsafe(
                _send_ctrl(ws, {
                    "type": "cancel",
                    "turn_id": prev["turn_id"] if prev else None,
                    "reason": "session_ended",
                }),
                loop,
            )
        if prev is not None:
            try:
                prev["abort_event"].set()
            except Exception:
                pass
        del sessions[session_id]
        LOGGER.info("Session deleted: %s", session_id)
        return SessionResponse(session_id=session_id, status="disconnected")

    @app.get("/api/llm/presets")
    def list_llm_presets():
        """Return available cloud LLM presets + currently active one."""
        presets = jarvis_app.llm.get_presets()
        active = jarvis_app.llm.active_preset
        items = [
            {
                "name": name,
                "model": cfg.get("model"),
                "base_url": cfg.get("base_url"),
            }
            for name, cfg in presets.items()
        ]
        return {"presets": items, "active": active}

    @app.post("/api/llm/switch")
    def switch_llm_preset(req: LLMSwitchRequest):
        """Switch cloud LLM to a named preset at runtime."""
        try:
            msg = jarvis_app.llm.switch_model(req.preset)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        LOGGER.info("LLM preset switched to '%s'", req.preset)
        return {"status": "ok", "active": req.preset, "message": msg}

    @app.post("/api/tool/execute")
    def execute_tool(req: ToolExecuteRequest):
        """Direct tool / YAML-skill invocation, bypassing the LLM and TTS.

        Used by the pet overlay's cc mode to forward keystrokes to a zellij
        cc session (cc_tell / cc_slash). Localhost-only by network policy;
        runs at owner role since the panel is the user's own UI.

        Each successful or failed call writes a row to the trace table with
        ``mode='cc'`` and ``input_device='pet_app'`` so cc proxy traffic is
        auditable alongside normal Jarvis turns.
        """
        registry = getattr(jarvis_app, "tool_registry", None)
        if registry is None:
            raise HTTPException(500, "tool_registry not available")
        # Validate the tool name is known so we return 404 instead of an
        # ambiguous "Unknown tool" string from the registry. Validation
        # failures are NOT traced — they're caller mistakes, not turns.
        if req.name not in _DIRECT_TOOL_ALLOWLIST:
            raise HTTPException(403, f"Tool not allowed on this endpoint: {req.name}")

        known = {d["name"] for d in registry.get_tool_definitions("owner")}
        if req.name not in known:
            raise HTTPException(404, f"Unknown tool: {req.name}")

        args = req.args or {}
        t0 = time.monotonic()
        result: str | None = None
        err: Exception | None = None
        try:
            result = registry.execute(req.name, args, user_role="owner")
        except Exception as exc:
            err = exc
            LOGGER.exception("execute_tool %s failed", req.name)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Best-effort trace write: never let a trace failure break the call.
        try:
            _write_cc_trace(jarvis_app, req, result, err, elapsed_ms)
        except Exception:
            LOGGER.warning("cc trace write failed", exc_info=True)

        if err is not None:
            raise HTTPException(500, f"{err}")
        return {"ok": True, "result": result}

    @app.post("/api/hidden-mode")
    async def toggle_hidden_mode(req: HiddenModeRequest):
        from core.personality import set_nsfw_mode
        session_id = req.session_id
        enabled = req.enabled
        set_nsfw_mode(enabled)
        # Clear conversation history on mode switch to prevent context bleed in both directions
        if session_id:
            jarvis_app.conversation_store.clear(session_id)
        LOGGER.info("Hidden mode %s — conversation history cleared for %s",
                     "ON" if enabled else "OFF", session_id)
        return {"status": "ok", "enabled": enabled}

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if req.session_id not in sessions:
            raise HTTPException(404, "Session not found — dial first")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        tts = jarvis_app._get_tts()

        sentence_index = 0

        # --- Cancel previous turn + flush any stale browser playback ---
        # Server-side `prev` may be None (popped in finally after stream_futures
        # drained) while the browser ring buffer still holds 10s+ of PCM from
        # the previous turn. Always flush on new_chat so stale audio doesn't
        # overlap the new turn.
        prev = _active_chats.get(req.session_id)
        ws_prev = _ws_routes.get(req.session_id)
        if ws_prev is not None:
            asyncio.run_coroutine_threadsafe(
                _send_ctrl(ws_prev, {
                    "type": "cancel",
                    "turn_id": prev["turn_id"] if prev else None,
                    "reason": "new_chat",
                }),
                loop,
            )
        if prev is not None:
            try:
                prev["abort_event"].set()
            except Exception:
                pass

        turn_id = uuid.uuid4().hex
        abort_event = threading.Event()
        _active_chats[req.session_id] = {
            "turn_id": turn_id,
            "abort_event": abort_event,
            "ws_client": None,  # filled in below if streaming
        }
        ws_client_for_turn: MinimaxWSClient | None = None
        turn_start_sent = False
        # Serializes per-sentence _stream_to_player_async calls on the FastAPI
        # loop so multiple sentences don't clobber the single MiniMax WS
        # session. chat() is async on the FastAPI loop, so creating the Lock
        # here binds it to the correct loop.
        tts_lock: asyncio.Lock = asyncio.Lock()
        # Track in-flight per-sentence stream futures so _run can wait for
        # all TTS to finish before emitting turn_end.
        stream_futures: list = []
        # WP5: each _stream_one appends {"idx","text","result"} here when
        # it exits. _run finally iterates to compute played_texts on abort
        # (heard_response truncation input for conversation_store writeback).
        sentence_results: list[dict] = []
        prewarm_future = None
        LOGGER.info(
            "[chat] turn_id=%s session=%s text=%r",
            turn_id, req.session_id, req.text[:80],
        )
        if bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False)) \
                and _ws_routes.get(req.session_id) is not None:
            cfg = jarvis_app.config.get("tts", {})
            try:
                ws_client_for_turn = MinimaxWSClient(
                    base_url=cfg.get("minimax_base_url", "https://api-uw.minimax.io"),
                    api_key=cfg.get("minimax_key", "") or os.environ.get("MINIMAX_API_KEY", ""),
                    model=cfg.get("minimax_model", "speech-2.8-turbo"),
                    voice_id=cfg.get("minimax_voice", "Chinese (Mandarin)_ExplorativeGirl"),
                    volume=int(cfg.get("minimax_volume", 1)),
                    sample_rate_out=32000,  # skip resampler
                    sample_rate_in=32000,
                )
                # Prewarm: open WS in background so handshake overlaps LLM tokens.
                prewarm_future = asyncio.run_coroutine_threadsafe(
                    ws_client_for_turn.open_session(emotion=None), loop,
                )
                # Announce the turn on the browser WS.
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(_ws_routes[req.session_id],
                               {"type": "turn_start", "turn_id": turn_id}),
                    loop,
                )
                turn_start_sent = True
                _active_chats[req.session_id]["ws_client"] = ws_client_for_turn
                LOGGER.info(
                    "[chat] streaming ENABLED turn_id=%s ws_client prewarm scheduled",
                    turn_id,
                )
            except Exception as exc:
                LOGGER.warning(
                    "[chat] MinimaxWSClient prewarm failed: %s", exc, exc_info=True,
                )
                ws_client_for_turn = None
        else:
            LOGGER.info(
                "[chat] streaming DISABLED (flag=%s ws_present=%s) — using file path",
                jarvis_app.config.get("tts", {}).get("browser_streaming", False),
                _ws_routes.get(req.session_id) is not None,
            )

        async def _stream_one(
            sentence: str,
            emotion_local: str,
            idx_local: int,
            ws_ref: Any,
        ) -> None:
            """Run one sentence's TTS stream on the FastAPI loop. Serialized
            via tts_lock so multiple sentences don't clobber the single
            MiniMax WS session. Emits sentence_end control frame on exit."""
            LOGGER.info(
                "[stream] idx=%d enter (text=%r emotion=%s)",
                idx_local, sentence[:40], emotion_local,
            )
            result = None
            prewarm_err: Exception | None = None
            try:
                if prewarm_future is not None:
                    try:
                        await asyncio.wrap_future(prewarm_future)
                    except Exception as exc:
                        prewarm_err = exc
                        LOGGER.warning(
                            "[stream] idx=%d prewarm open_session failed: %s",
                            idx_local, exc, exc_info=True,
                        )
                if prewarm_err is not None:
                    return
                async with tts_lock:
                    LOGGER.info("[stream] idx=%d lock acquired", idx_local)
                    if abort_event.is_set():
                        LOGGER.info("[stream] idx=%d aborted before feed", idx_local)
                        return
                    def _get_cursor() -> int:
                        # Browser AudioWorklet reports cumulative played
                        # samples for the turn. Gate on playback_turn_id so
                        # a stale cursor from the prior turn (still in-flight
                        # when abort lands) doesn't leak into this turn's WP5.
                        entry = _active_chats.get(req.session_id)
                        if entry is None:
                            return 0
                        if entry.get("playback_turn_id") != turn_id:
                            return 0
                        return int(entry.get("playback_cursor", 0))
                    player = BrowserWSPlayer(
                        ws=ws_ref, sentence_index=idx_local, loop=loop,
                        abort_event=abort_event,
                        get_cursor=_get_cursor,
                        on_first_chunk=(tts._first_chunk_callback if idx_local == 0 else None),
                    )
                    try:
                        result = await tts._stream_to_player_async(
                            sentence, emotion_local, player,
                            ws_client_for_turn, abort_event,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "[stream] idx=%d _stream_to_player_async raised: %s",
                            idx_local, exc, exc_info=True,
                        )
                        return
                    if result is not None and getattr(result, "raised", None):
                        LOGGER.warning(
                            "[stream] idx=%d inner raised: %s",
                            idx_local, result.raised, exc_info=result.raised,
                        )
                    LOGGER.info(
                        "[stream] idx=%d done: total_samples=%s played=%s",
                        idx_local,
                        getattr(result, "total_samples", None),
                        getattr(result, "played_samples", None),
                    )
            finally:
                sentence_results.append(
                    {"idx": idx_local, "text": sentence, "result": result},
                )
                try:
                    await _send_ctrl(ws_ref, {
                        "type": "sentence_end",
                        "turn_id": turn_id,
                        "sentence_index": idx_local,
                        "subtitle_url":
                            getattr(result, "subtitle_url", None) if result else None,
                    })
                except Exception as exc:
                    LOGGER.debug(
                        "[stream] idx=%d sentence_end send failed: %s",
                        idx_local, exc,
                    )

        def on_sentence(
            sentence: str,
            emotion: str = "",
            voice_text: str | None = None,
        ) -> None:
            nonlocal sentence_index

            from core import tts_preprocessor
            pp_cfg = jarvis_app.config.get("tts", {}).get("tts_preprocessor", {})
            display_text = sentence or ""
            spoken_text = voice_text if voice_text is not None else display_text
            spoken_text = tts_preprocessor.clean(spoken_text, pp_cfg)
            if not display_text.strip() and not spoken_text.strip():
                return

            idx = sentence_index
            sentence_index += 1

            use_stream = (
                bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False))
                and _ws_routes.get(req.session_id) is not None
                and ws_client_for_turn is not None  # prewarm succeeded
            )

            LOGGER.info(
                "[on_sentence] idx=%d use_stream=%s display=%r spoken=%r emotion=%s",
                idx, use_stream, display_text[:40], spoken_text[:40], emotion,
            )

            audio_url = ""
            if use_stream and spoken_text.strip():
                ws = _ws_routes[req.session_id]
                # Fire sentence_start + schedule TTS stream, then return
                # immediately so LLM stream isn't blocked.
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {
                        "type": "sentence_start",
                        "turn_id": turn_id,
                        "sentence_index": idx,
                        "text": spoken_text,
                        "emotion": (emotion or "neutral").lower(),
                    }),
                    loop,
                )
                fut = asyncio.run_coroutine_threadsafe(
                    _stream_one(spoken_text, emotion, idx, ws), loop,
                )
                stream_futures.append(fut)
            else:
                if tts and spoken_text.strip():
                    try:
                        r = tts.synth_to_file(spoken_text, emotion)
                        if r:
                            audio_path, deletable = r
                            if audio_path.endswith(".pcm"):
                                audio_name = f"{uuid.uuid4().hex}.wav"
                                dest = AUDIO_DIR / audio_name
                                _wrap_pcm_to_wav(Path(audio_path), dest)
                            else:
                                ext = Path(audio_path).suffix or ".mp3"
                                audio_name = f"{uuid.uuid4().hex}{ext}"
                                dest = AUDIO_DIR / audio_name
                                shutil.copy2(audio_path, dest)
                            if deletable:
                                Path(audio_path).unlink(missing_ok=True)
                            audio_url = f"/api/audio/{audio_name}"
                    except Exception as exc:
                        LOGGER.warning("TTS synth failed: %s", exc, exc_info=True)

            # Emit SSE event immediately (fast text display in UI)
            event = {
                "turn_id": turn_id,
                "index": idx,
                "text": display_text,
                "emotion": emotion.lower() if emotion else "neutral",
                "audio_url": audio_url,
            }
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        # Inject a logging handler to stream backend logs to SSE
        class SSELogHandler(logging.Handler):
            def emit(self, record):
                try:
                    msg = self.format(record)
                    log_event = {"_log": True, "level": record.levelname, "msg": msg}
                    asyncio.run_coroutine_threadsafe(queue.put(log_event), loop)
                except Exception:
                    pass

        sse_handler = SSELogHandler()
        sse_handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        sse_handler.setLevel(logging.INFO)

        def _run():
            root = logging.getLogger()
            root.addHandler(sse_handler)
            try:
                jarvis_app.handle_text(
                    req.text,
                    session_id=req.session_id,
                    on_sentence=on_sentence,
                )
            except Exception as exc:
                LOGGER.error("handle_text failed: %s", exc, exc_info=True)
            finally:
                root.removeHandler(sse_handler)
                # Wait for all per-sentence TTS streams to finish before
                # emitting turn_end + closing the MiniMax session.
                LOGGER.info(
                    "[chat] handle_text returned, waiting on %d stream futures",
                    len(stream_futures),
                )
                for sfut in stream_futures:
                    try:
                        sfut.result(timeout=120.0)
                    except Exception as exc:
                        LOGGER.warning(
                            "[chat] stream future raised: %s", exc, exc_info=True,
                        )
                LOGGER.info("[chat] all stream futures drained, turn_id=%s", turn_id)
                # WP5: if this turn was aborted mid-playback, shrink the last
                # assistant message in conversation_store so the next LLM
                # turn only sees what the user actually heard. Mirrors the
                # CLI path (JarvisApp._process_turn →
                # _truncate_assistant_for_interrupt).
                played_texts: list[str] = _compute_played_texts(
                    sentence_results, abort_event, sample_rate=32000,
                )
                if abort_event.is_set() and played_texts:
                    _apply_heard_response(
                        jarvis_app, req.session_id, played_texts,
                    )
                if ws_client_for_turn is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            ws_client_for_turn.close_session(), loop,
                        )
                    except Exception as exc:
                        LOGGER.debug("close_session scheduling failed: %s", exc)
                ws = _ws_routes.get(req.session_id)
                if ws is not None and turn_start_sent:
                    asyncio.run_coroutine_threadsafe(
                        _send_ctrl(ws, {"type": "turn_end", "turn_id": turn_id}),
                        loop,
                    )
                _active_chats.pop(req.session_id, None)
                # Expose trace_id in the done event so the frontend can send
                # explicit thumbs feedback via POST /api/outcome.
                _raw_tid = getattr(jarvis_app, "_last_trace_id", None)
                done_trace_id = _raw_tid if isinstance(_raw_tid, int) else None
                asyncio.run_coroutine_threadsafe(
                    queue.put({"_done": True, "trace_id": done_trace_id}), loop
                )

        loop.run_in_executor(None, _run)

        async def event_stream():
            while True:
                event = await queue.get()
                if isinstance(event, dict) and event.get("_done"):
                    done_payload = {"trace_id": event.get("trace_id")}
                    yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                    break
                if isinstance(event, dict) and event.get("_log"):
                    yield f"event: log\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:
                    yield f"event: sentence\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Turn-Id": turn_id,
            },
        )

    @app.post("/api/chat/cancel")
    async def chat_cancel(req: ChatRequest):
        """User-initiated stop. Flushes any in-flight turn on this session
        and aborts the LLM/TTS pipeline. Safe to call when no turn is
        active — sends a flush cancel anyway to drain stale browser PCM."""
        if req.session_id not in sessions:
            raise HTTPException(404, "Session not found")
        loop = asyncio.get_running_loop()
        prev = _active_chats.get(req.session_id)
        ws = _ws_routes.get(req.session_id)
        if ws is not None:
            asyncio.run_coroutine_threadsafe(
                _send_ctrl(ws, {
                    "type": "cancel",
                    "turn_id": prev["turn_id"] if prev else None,
                    "reason": "user_stop",
                }),
                loop,
            )
        if prev is not None:
            try:
                prev["abort_event"].set()
            except Exception:
                pass
        return {"status": "ok", "cancelled": prev is not None}

    # --- Outcome feedback (thumbs) ---
    @app.post("/api/outcome")
    async def post_outcome(payload: dict):
        """Record explicit user feedback (thumbs up / down / skip).

        Payload: {trace_id: int, signal: int}
          signal = 1   thumbs up (positive)
          signal = -1  thumbs down (negative)
          signal = 0   skip (explicit no-opinion)

        Idempotent: later call overwrites earlier (update_outcome is a simple
        SET). trace_id is exposed by the SSE done event for this session.
        """
        from fastapi.responses import JSONResponse
        trace_id = payload.get("trace_id")
        signal = payload.get("signal")
        if signal not in (-1, 0, 1):
            return JSONResponse({"error": "signal must be -1, 0, or 1"}, status_code=400)
        if not isinstance(trace_id, int):
            return JSONResponse({"error": "trace_id must be an integer"}, status_code=400)
        row = jarvis_app.trace_log.query_by_trace_id(trace_id)
        if row is None:
            return JSONResponse({"error": "trace not found"}, status_code=404)
        jarvis_app.trace_log.update_outcome(trace_id, signal=signal, at_turn_id=trace_id)
        LOGGER.info("[outcome] thumbs trace_id=%d signal=%d", trace_id, signal)
        return {"ok": True, "trace_id": trace_id}

    async def _transcribe_audio_upload(audio: UploadFile) -> dict[str, str]:
        import soundfile as sf
        import numpy as np

        audio_bytes = await audio.read()
        if not audio_bytes:
            return {"text": "", "emotion": ""}

        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1).astype(np.float32)
        if len(data) == 0:
            return {"text": "", "emotion": ""}

        if sr != 16000:
            LOGGER.info("ASR resampling %dHz to 16000Hz", sr)
            duration = len(data) / sr
            target_len = int(duration * 16000)
            if target_len <= 0:
                return {"text": "", "emotion": ""}
            indices = np.linspace(0, len(data) - 1, target_len)
            data = np.interp(indices, np.arange(len(data)), data).astype(np.float32)

        # Drop silent / too-short audio before ASR. Whisper hallucinates
        # confidently on near-silent input ("请不吝点赞..." prior pollution and
        # "X X X X" repetition loops). Same gate the wake listener uses.
        quality_ok, qmsg = jarvis_app.audio_recorder.is_quality_ok(data)
        if not quality_ok:
            LOGGER.info("ASR dropped low-quality audio: %s", qmsg)
            return {"text": "", "emotion": ""}

        result = jarvis_app.speech_recognizer.transcribe(data)
        text = result.text or ""
        lang = getattr(result, "language", "") or ""
        # Drop non-Chinese short fragments (likely noise/echo misdetected as ja/en)
        if lang != "zh" and len(text) <= 5:
            LOGGER.info("ASR dropped non-zh fragment: lang=%s text='%s'", lang, text)
            text = ""
        return {
            "text": text,
            "emotion": getattr(result, "emotion", "") or "",
        }

    # --- ASR ---
    @app.post("/api/asr")
    async def asr(session_id: str = Form(...), audio: UploadFile = File(...)):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
        return await _transcribe_audio_upload(audio)

    @app.get("/api/audio/{filename}")
    def get_audio(filename: str):
        path = AUDIO_DIR / filename
        if not path.exists():
            raise HTTPException(404, "Audio file not found")
        # MIME by extension — browsers dispatch their decoder on this.
        if filename.endswith(".wav"):
            media_type = "audio/wav"
        elif filename.endswith(".ogg"):
            media_type = "audio/ogg"
        elif filename.endswith(".flac"):
            media_type = "audio/flac"
        else:
            media_type = "audio/mpeg"  # .mp3 default
        return FileResponse(path, media_type=media_type)

    @app.websocket("/api/tts/stream")
    async def tts_stream(ws: WebSocket, session_id: str):
        if session_id not in sessions:
            await ws.close(code=1008, reason="unknown session")
            return
        await ws.accept()
        # Capture the running loop so event_bus subscribers can cross
        # threads via run_coroutine_threadsafe. Runs here (not in a
        # startup hook) because TestClient doesn't reliably fire startup
        # without a context-manager wrapper.
        if not isinstance(getattr(jarvis_app, "_web_loop", None), asyncio.AbstractEventLoop):
            jarvis_app._web_loop = asyncio.get_running_loop()
        async with _ws_routes_lock:
            old = _ws_routes.get(session_id)
            _ws_routes[session_id] = ws
        if old is not None:
            try:
                await old.close(code=1001, reason="superseded")
            except Exception:
                pass

        # Heartbeat: send a ping every 25s so idle connections aren't culled
        # by NAT/reverse-proxy timeouts (typical ~60s). Client replies with
        # pong in the recv loop below; we don't enforce pong-timeout here —
        # dead sockets surface via send raising or ws.receive_text EOF.
        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(25)
                    await _send_ctrl(ws, {"type": "ping"})
            except asyncio.CancelledError:
                pass

        hb_task = asyncio.create_task(_heartbeat())
        try:
            while True:
                msg = await ws.receive_text()
                # Accept client control frames. "pong" and "user_stop" are
                # the only ones defined today.
                try:
                    payload = json.loads(msg)
                except Exception:
                    continue
                if payload.get("type") == "user_stop":
                    prev = _active_chats.get(session_id)
                    if ws is not None:
                        await _send_ctrl(ws, {
                            "type": "cancel",
                            "turn_id": prev["turn_id"] if prev else None,
                            "reason": "user_stop",
                        })
                    if prev is not None:
                        try:
                            prev["abort_event"].set()
                        except Exception:
                            pass
                elif payload.get("type") == "playback_cursor":
                    # Frontend reports total played samples (~150ms cadence).
                    # Stored per-session so heard_response truncation and
                    # delayed session-drain logic can read it.
                    prev = _active_chats.get(session_id)
                    if prev is not None:
                        try:
                            prev["playback_cursor"] = int(payload.get("samples", 0))
                            prev["playback_turn_id"] = payload.get("turn_id")
                        except Exception:
                            pass
        except WebSocketDisconnect:
            pass
        finally:
            hb_task.cancel()
            async with _ws_routes_lock:
                if _ws_routes.get(session_id) is ws:
                    _ws_routes.pop(session_id, None)

    @app.post("/inherent/submit")
    async def inherent_submit(req: InherentSubmitRequest):
        """Text submit entry for the desktop inherent card.

        The hotkey-driven Electron card POSTs typed text here. We hand off
        to JarvisApp.handle_text on a worker thread; the response surfaces
        to the renderer via event_bus → /inherent/ws → siri:open/append/done
        automatically (the bridge is wired up at create_app() time).
        Returns immediately so the renderer stays responsive while the LLM
        streams.
        """
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: jarvis_app.handle_text(text, session_id="_inherent"),
        )
        LOGGER.info("[inherent] submit text=%r", text[:80])
        return {"status": "accepted"}

    @app.post("/inherent/image-submit")
    async def inherent_image_submit(
        text: str = Form(""),
        image: UploadFile = File(...),
    ):
        """Image submit entry for the desktop inherent card.

        The card sends one staged image plus optional text. We turn the image
        into a data URL for the current OpenAI-compatible LLM request and keep
        only the text/attachment marker in conversation history.
        """
        llm_provider = getattr(getattr(jarvis_app, "llm", None), "provider", None)
        if isinstance(llm_provider, str) and llm_provider != "openai":
            raise HTTPException(400, "image input requires provider=openai")

        raw_mime = (image.content_type or "").split(";", 1)[0].strip().lower()
        guessed_mime = mimetypes.guess_type(image.filename or "")[0] or ""
        mime = raw_mime or guessed_mime.lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in _INHERENT_IMAGE_MIME_TYPES:
            raise HTTPException(400, "unsupported image type")

        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(400, "image is required")
        if len(image_bytes) > _INHERENT_IMAGE_MAX_BYTES:
            raise HTTPException(413, "image too large")

        prompt = (text or "").strip()
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{encoded}"
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: jarvis_app.handle_image(
                prompt,
                data_url,
                session_id="_inherent",
                image_name=image.filename or None,
                image_mime=mime,
                image_bytes=len(image_bytes),
            ),
        )
        LOGGER.info(
            "[inherent] submit image text=%r filename=%r bytes=%d",
            prompt[:80], image.filename, len(image_bytes),
        )
        return {"status": "accepted", "text": prompt}

    @app.post("/inherent/asr-submit")
    async def inherent_asr_submit(audio: UploadFile = File(...)):
        """Voice submit entry for the desktop inherent card.

        The Electron renderer records a push-to-talk WAV clip, main forwards
        it here, and this endpoint transcribes then submits the recognized
        text into the same `_inherent` conversation used by typed card input.
        """
        result = await _transcribe_audio_upload(audio)
        text = (result.get("text") or "").strip()
        emotion = result.get("emotion", "")
        if not text:
            LOGGER.info("[inherent] voice submit produced empty transcript")
            return {"status": "empty", "text": "", "emotion": emotion}

        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            lambda: jarvis_app.handle_text(text, session_id="_inherent"),
        )
        LOGGER.info("[inherent] submit voice text=%r", text[:80])
        return {"status": "accepted", "text": text, "emotion": emotion}

    @app.websocket("/inherent/ws")
    async def inherent_ws(ws: WebSocket):
        """Inherent card bridge endpoint (outbound-only broadcast).

        Server pushes ``{op, payload}`` JSON when jarvis response.* events
        fire on event_bus. Client (Electron inherent-main) does not send
        anything yet — the recv loop is only here so disconnects surface
        as WebSocketDisconnect rather than zombie sockets.

        NB: Must be registered *before* the StaticFiles mount below, or the
        wildcard "/" mount intercepts the upgrade and StarLette asserts on
        scope["type"] == "http".
        """
        await ws.accept()
        # Same _web_loop capture trick as tts_stream — must run inside a real
        # request so TestClient/uvicorn-startup ordering doesn't matter.
        if not isinstance(getattr(jarvis_app, "_web_loop", None), asyncio.AbstractEventLoop):
            jarvis_app._web_loop = asyncio.get_running_loop()
        _inherent_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _inherent_clients.discard(ws)

    web_dir = Path(__file__).parent
    app.mount("/", StaticFiles(directory=str(web_dir), html=True, follow_symlink=True), name="static")

    return app


def create_jarvis_app(config_path: str = "config.yaml") -> Any:
    """Load config and create JarvisApp."""
    import yaml
    from jarvis import JarvisApp
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return JarvisApp(config, config_path=config_path)


def app_factory():
    """Module-level factory for uvicorn --reload mode.

    Reads config.yaml from cwd. JarvisApp is recreated on every reload, so
    the SenseVoice / Silero / TTS model load (~5s) is paid each time the
    watcher fires. Acceptable for dev iteration, NEVER for prod.
    """
    jarvis = create_jarvis_app(os.environ.get("JARVIS_CONFIG_PATH", "config.yaml"))
    return create_app(jarvis)


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Jarvis Live2D Web Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dev", action="store_true",
                        help="Enable uvicorn --reload (watches Python source for changes)")
    args = parser.parse_args()

    # Logging: console + file (same format as jarvis.py)
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    file_handler = logging.FileHandler(log_dir / "web_server.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)
    LOGGER.info("Log file: %s", log_dir / "web_server.log")

    if AUDIO_DIR.exists():
        shutil.rmtree(AUDIO_DIR)
    AUDIO_DIR.mkdir(exist_ok=True)

    if args.dev:
        LOGGER.info("[dev] uvicorn --reload mode; watching ui/web, core, memory")
        os.environ["JARVIS_CONFIG_PATH"] = args.config
        uvicorn.run(
            "ui.web.server:app_factory",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=["ui/web", "core", "memory", "."],
            reload_includes=["jarvis.py", "*.py"],
        )
    else:
        jarvis = create_jarvis_app(args.config)
        app = create_app(jarvis)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
