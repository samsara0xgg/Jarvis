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

from fastapi import FastAPI, HTTPException
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

AUDIO_DIR = Path(__file__).parent / "audio_cache"


def create_app(jarvis_app: Any) -> FastAPI:
    """Create FastAPI app wrapping a JarvisApp instance."""
    app = FastAPI(title="Jarvis Live2D Web")
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

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if req.session_id not in sessions:
            raise HTTPException(404, "Session not found — dial first")

        loop = asyncio.get_event_loop()
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
                    audio_path = tts.synth_to_file(sentence, emotion)
                    if audio_path:
                        audio_name = f"{uuid.uuid4().hex}.mp3"
                        dest = AUDIO_DIR / audio_name
                        shutil.copy2(audio_path, dest)
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

        def _run():
            try:
                jarvis_app.handle_text(
                    req.text,
                    session_id=req.session_id,
                    on_sentence=on_sentence,
                )
            except Exception as exc:
                LOGGER.error("handle_text failed: %s", exc)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        loop.run_in_executor(None, _run)

        async def event_stream():
            while True:
                event = await queue.get()
                if event is None:
                    yield f"event: done\ndata: {{}}\n\n"
                    break
                yield f"event: sentence\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/audio/{filename}")
    def get_audio(filename: str):
        path = AUDIO_DIR / filename
        if not path.exists():
            raise HTTPException(404, "Audio file not found")
        return FileResponse(path, media_type="audio/mpeg")

    web_dir = Path(__file__).parent
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

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

    logging.basicConfig(level=logging.INFO)

    if AUDIO_DIR.exists():
        shutil.rmtree(AUDIO_DIR)
    AUDIO_DIR.mkdir(exist_ok=True)

    jarvis = create_jarvis_app(args.config)
    app = create_app(jarvis)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
