"""ADR-0005 voice_tts.MiniMaxWSClient — WS provider (mocked, no network).

Frames the legacy `core/tts_minimax_ws.py` protocol:

    client                       server
       --- connect          --->
       <--- connected_success ---
       --- task_start       --->
       <--- task_started      ---
       --- task_continue    --->
       <--- {data.audio: hex} ---
       <--- {data.audio: hex, is_final: true} ---
       --- task_finish      --->
       <--- close             ---

Audio is hex-encoded int16 little-endian PCM at 32 kHz. Client resamples to
float32 48 kHz and returns the concatenated bytes (the format
``AudioStreamPlayer.write()`` consumes).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed

from jarvis.surface import voice_tts

# ---------------------------------------------------------------------------
# Fake WS plumbing
# ---------------------------------------------------------------------------


def _hex_pcm(int16_value: int, n_samples: int) -> str:
    """Build a hex-encoded int16-LE PCM payload of length ``n_samples``."""
    arr = np.full(n_samples, int16_value, dtype=np.int16)
    return arr.tobytes().hex()


def _make_fake_ws(frames: list[str]) -> AsyncMock:
    """Return an AsyncMock that yields ``frames`` then raises ConnectionClosed."""
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    fake_ws.close = AsyncMock()
    frame_iter = iter(frames)

    async def _recv() -> str:
        try:
            return next(frame_iter)
        except StopIteration as exc:
            # websockets >= 13 ConnectionClosed signature: (rcvd, sent)
            raise ConnectionClosed(None, None) from exc

    fake_ws.recv = _recv
    return fake_ws


def _default_session_frames() -> list[str]:
    """Frames for a complete successful session: 2 audio chunks, the second final."""
    return [
        # 1. connected_success
        json.dumps({
            "event": "connected_success",
            "session_id": "sess-1",
            "trace_id": "trace-1",
        }),
        # 2. task_started — base_resp.status_code == 0 means OK
        json.dumps({
            "event": "task_started",
            "base_resp": {"status_code": 0, "status_msg": "ok"},
        }),
        # 3. audio chunk (non-final) — 480 int16 samples ~= 15 ms @ 32 kHz
        json.dumps({
            "data": {"audio": _hex_pcm(1000, 480)},
            "is_final": False,
        }),
        # 4. audio chunk (final)
        json.dumps({
            "data": {"audio": _hex_pcm(2000, 480)},
            "is_final": True,
        }),
    ]


class _AsyncCM:
    """`async with` wrapper that returns the inner fake WS unchanged."""

    def __init__(self, ws: object) -> None:
        self._ws = ws

    async def __aenter__(self) -> object:
        return self._ws

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_minimax_client_synthesize_returns_pcm_bytes() -> None:
    """Mocked WS yields connected/task_started + 2 audio frames; result is float32 PCM bytes."""

    async def _go() -> None:
        fake_ws = _make_fake_ws(_default_session_frames())

        async def _connect(_url: str, **_kwargs: object) -> object:
            return fake_ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            client = voice_tts.MiniMaxWSClient(
                api_key="test-key",
                voice="Chinese (Mandarin)_ExplorativeGirl",
                primary_endpoint="https://api-uw.minimax.io",
                fallback_endpoint="https://api.minimax.chat",
            )
            pcm = await client.synthesize("你好")

        assert isinstance(pcm, bytes)
        # float32 mono PCM; legacy resamples 32 kHz -> 48 kHz so output is non-empty
        assert len(pcm) > 0
        assert len(pcm) % 4 == 0, "float32 PCM bytes must be a multiple of 4"

    asyncio.run(_go())


def test_minimax_client_sends_task_start_and_task_continue() -> None:
    """The client sends connected→task_start→task_continue→task_finish JSON envelopes."""

    async def _go() -> None:
        fake_ws = _make_fake_ws(_default_session_frames())

        async def _connect(_url: str, **_kwargs: object) -> object:
            return fake_ws

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            client = voice_tts.MiniMaxWSClient(
                api_key="test-key",
                voice="Chinese (Mandarin)_ExplorativeGirl",
                primary_endpoint="https://api-uw.minimax.io",
                fallback_endpoint="https://api.minimax.chat",
            )
            await client.synthesize("你好")

        sent_events = [json.loads(call.args[0])["event"] for call in fake_ws.send.call_args_list]
        assert "task_start" in sent_events
        assert "task_continue" in sent_events
        assert "task_finish" in sent_events

    asyncio.run(_go())


def test_minimax_client_falls_back_when_primary_fails() -> None:
    """Primary endpoint raises; fallback succeeds; synthesize() returns PCM."""
    call_log: list[str] = []

    async def _connect(url: str, **_kwargs: object) -> object:
        call_log.append(url)
        if len(call_log) == 1:
            msg = "primary endpoint unreachable"
            raise OSError(msg)
        return _make_fake_ws(_default_session_frames())

    async def _go() -> None:
        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            client = voice_tts.MiniMaxWSClient(
                api_key="test-key",
                voice="Chinese (Mandarin)_ExplorativeGirl",
                primary_endpoint="https://api-uw.minimax.io",
                fallback_endpoint="https://api.minimax.chat",
            )
            pcm = await client.synthesize("你好")

        assert isinstance(pcm, bytes)
        assert len(pcm) > 0
        assert len(call_log) == 2
        # primary tried first, fallback second
        assert "api-uw.minimax.io" in call_log[0]
        assert "api.minimax.chat" in call_log[1]

    asyncio.run(_go())


def test_minimax_unavailable_raised_when_both_endpoints_fail() -> None:
    """Both endpoints unreachable → MiniMaxUnavailableError."""

    async def _connect(_url: str, **_kwargs: object) -> object:
        msg = "dead"
        raise OSError(msg)

    async def _go() -> None:
        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            client = voice_tts.MiniMaxWSClient(
                api_key="test-key",
                voice="Chinese (Mandarin)_ExplorativeGirl",
                primary_endpoint="https://api-uw.minimax.io",
                fallback_endpoint="https://api.minimax.chat",
            )
            with pytest.raises(voice_tts.MiniMaxUnavailableError):
                await client.synthesize("你好")

    asyncio.run(_go())
