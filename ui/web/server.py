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

        turn_id = uuid.uuid4().hex
        abort_event = threading.Event()
        ws_client_for_turn: MinimaxWSClient | None = None
        turn_start_sent = False
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
                asyncio.run_coroutine_threadsafe(
                    ws_client_for_turn.open_session(emotion=None), loop,
                )
                # Announce the turn on the browser WS.
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(_ws_routes[req.session_id],
                               {"type": "turn_start", "turn_id": turn_id}),
                    loop,
                )
                turn_start_sent = True
            except Exception as exc:
                LOGGER.warning("MinimaxWSClient prewarm failed, falling back: %s", exc)
                ws_client_for_turn = None

        def on_sentence(sentence: str, emotion: str = "") -> None:
            nonlocal sentence_index
            idx = sentence_index
            sentence_index += 1

            use_stream = (
                bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False))
                and _ws_routes.get(req.session_id) is not None
                and ws_client_for_turn is not None  # prewarm succeeded
            )

            audio_url = ""
            if use_stream:
                ws = _ws_routes[req.session_id]
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
                player = BrowserWSPlayer(ws=ws, sentence_index=idx, loop=loop)
                try:
                    result = tts.stream_to_player(
                        sentence, emotion, player, ws_client_for_turn, abort_event,
                    )
                except Exception as exc:
                    LOGGER.warning("stream_to_player failed: %s", exc)
                    result = None
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {
                        "type": "sentence_end",
                        "turn_id": turn_id,
                        "sentence_index": idx,
                        "subtitle_url":
                            getattr(result, "subtitle_url", None) if result else None,
                    }),
                    loop,
                )
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
                        LOGGER.warning("TTS synth failed: %s", exc)

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
                LOGGER.error("handle_text failed: %s", exc)
            finally:
                root.removeHandler(sse_handler)
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
        async with _ws_routes_lock:
            old = _ws_routes.get(session_id)
            _ws_routes[session_id] = ws
        if old is not None:
            try:
                await old.close(code=1001, reason="superseded")
            except Exception:
                pass
        try:
            while True:
                # Server-push only for now. We still recv to detect client
                # close + to accept future {"type":"user_stop"} frames.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
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
