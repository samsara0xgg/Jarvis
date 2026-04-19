"""Jarvis Live2D Web Server — FastAPI + SSE."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import struct
import threading
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

LOGGER = logging.getLogger(__name__)

# --- Browser TTS streaming state (Task 2) ---
_ws_routes: dict[str, Any] = {}          # session_id → WebSocket
_ws_routes_lock = asyncio.Lock()
_active_chats: dict[str, dict] = {}      # session_id → {turn_id, abort_event, ws_client}
_active_chats_lock = asyncio.Lock()


async def _send_ctrl(ws: Any, payload: dict) -> None:
    """Send a JSON control frame. Silently drops if ws has gone away."""
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        LOGGER.debug("ws send_text failed: %s", exc)

class ChatRequest(BaseModel):
    text: str
    session_id: str

class SessionResponse(BaseModel):
    session_id: str
    status: str

class HiddenModeRequest(BaseModel):
    session_id: str = ""
    enabled: bool = False


class LLMSwitchRequest(BaseModel):
    preset: str

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
    def delete_session(session_id: str):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
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
                    player = BrowserWSPlayer(
                        ws=ws_ref, sentence_index=idx_local, loop=loop,
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

        def on_sentence(sentence: str, emotion: str = "") -> None:
            nonlocal sentence_index
            idx = sentence_index
            sentence_index += 1

            use_stream = (
                bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False))
                and _ws_routes.get(req.session_id) is not None
                and ws_client_for_turn is not None  # prewarm succeeded
            )

            LOGGER.info(
                "[on_sentence] idx=%d use_stream=%s text=%r emotion=%s",
                idx, use_stream, sentence[:40], emotion,
            )

            audio_url = ""
            if use_stream:
                ws = _ws_routes[req.session_id]
                # Fire sentence_start + schedule TTS stream, then return
                # immediately so LLM stream isn't blocked.
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {
                        "type": "sentence_start",
                        "turn_id": turn_id,
                        "sentence_index": idx,
                        "text": sentence,
                        "emotion": (emotion or "neutral").lower(),
                    }),
                    loop,
                )
                fut = asyncio.run_coroutine_threadsafe(
                    _stream_one(sentence, emotion, idx, ws), loop,
                )
                stream_futures.append(fut)
            else:
                if tts:
                    try:
                        r = tts.synth_to_file(sentence, emotion)
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
                "text": sentence,
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
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        loop.run_in_executor(None, _run)

        async def event_stream():
            while True:
                event = await queue.get()
                if event is None:
                    yield f"event: done\ndata: {{}}\n\n"
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

    # --- ASR ---
    @app.post("/api/asr")
    async def asr(session_id: str = Form(...), audio: UploadFile = File(...)):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
        import soundfile as sf
        import numpy as np
        audio_bytes = await audio.read()
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        if sr != 16000:
            LOGGER.info("ASR resampling %dHz → 16000Hz", sr)
            # Linear interpolation resample
            duration = len(data) / sr
            target_len = int(duration * 16000)
            indices = np.linspace(0, len(data) - 1, target_len)
            data = np.interp(indices, np.arange(len(data)), data).astype(np.float32)
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
        except WebSocketDisconnect:
            pass
        finally:
            hb_task.cancel()
            async with _ws_routes_lock:
                if _ws_routes.get(session_id) is ws:
                    _ws_routes.pop(session_id, None)

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


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Jarvis Live2D Web Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--config", default="config.yaml")
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

    jarvis = create_jarvis_app(args.config)
    app = create_app(jarvis)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
