"""Jarvis Live2D Web Server — FastAPI + SSE."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import io

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)

class ChatRequest(BaseModel):
    text: str
    session_id: str

class SessionResponse(BaseModel):
    session_id: str
    status: str

class HiddenModeRequest(BaseModel):
    session_id: str = ""
    enabled: bool = False

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

        def on_sentence(sentence: str, emotion: str = "") -> None:
            nonlocal sentence_index
            idx = sentence_index
            sentence_index += 1

            audio_url = ""
            if tts:
                try:
                    result = tts.synth_to_file(sentence, emotion)
                    if result:
                        audio_path, deletable = result
                        audio_name = f"{uuid.uuid4().hex}.mp3"
                        dest = AUDIO_DIR / audio_name
                        shutil.copy2(audio_path, dest)
                        if deletable:
                            Path(audio_path).unlink(missing_ok=True)
                        audio_url = f"/api/audio/{audio_name}"
                except Exception as exc:
                    LOGGER.warning("TTS synth failed: %s", exc)

            event = {
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
        return FileResponse(path, media_type="audio/mpeg")

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
