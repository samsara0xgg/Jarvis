"""Tests for MinimaxWSClient — turn-level WebSocket TTS client."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest


@pytest.fixture
def fake_ws():
    """An AsyncMock that impersonates websockets.WebSocketClientProtocol.

    Test bodies enqueue server-side messages via `ws.send_queue.append(...)`;
    `ws.recv` pops them in order. Client-side sends land in `ws.sent`.
    Small await between queue pops simulates network, letting abort/cancel
    tasks get scheduled.
    """
    ws = AsyncMock()
    ws.send_queue = []
    ws.sent = []

    async def recv():
        while not ws.send_queue:
            await asyncio.sleep(0.005)
        # Small sim-network delay so concurrent abort triggers can fire.
        await asyncio.sleep(0.001)
        return ws.send_queue.pop(0)

    async def send(payload):
        ws.sent.append(json.loads(payload))

    async def close():
        pass

    ws.recv = recv
    ws.send = send
    ws.close = close
    return ws


@pytest.fixture
def connect_patch(monkeypatch, fake_ws):
    """Patch websockets.connect to return our fake_ws."""
    import websockets

    async def fake_connect(*a, **kw):
        return fake_ws

    monkeypatch.setattr(websockets, "connect", fake_connect)
    return fake_ws


class TestMinimaxWSClientOpenSession:
    @pytest.mark.asyncio
    async def test_open_session_sends_task_start_and_receives_started(self, connect_patch):
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success",
                        "base_resp": {"status_code": 0, "status_msg": "ok"}}),
            json.dumps({"event": "task_started",
                        "base_resp": {"status_code": 0, "status_msg": "ok"}}),
        ]

        client = MinimaxWSClient(
            base_url="https://api-uw.minimax.io",
            api_key="sk-api-test",
            model="speech-2.8-turbo",
            voice_id="V",
            volume=1,
        )
        await client.open_session(emotion="happy")
        assert client.is_open()
        ts = ws.sent[0]
        assert ts["event"] == "task_start"
        assert ts["voice_setting"]["voice_id"] == "V"
        assert ts["voice_setting"]["emotion"] == "happy"
        assert ts["audio_setting"]["format"] == "pcm"
        await client.close_session()

    @pytest.mark.asyncio
    async def test_open_session_skips_emotion_when_none(self, connect_patch):
        """emotion=None means DON'T SEND the field (saves 500ms server-side)."""
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success",
                        "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started",
                        "base_resp": {"status_code": 0}}),
        ]

        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "speech-2.8-turbo", "V", 1)
        await client.open_session(emotion=None)
        ts = ws.sent[0]
        assert "emotion" not in ts["voice_setting"]
        await client.close_session()

    @pytest.mark.asyncio
    async def test_open_session_raises_on_task_start_failure(self, connect_patch):
        from core.tts_minimax_ws import MinimaxWSClient, WSProtocolError

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"base_resp": {"status_code": 2049, "status_msg": "invalid api key"}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1)
        with pytest.raises(WSProtocolError):
            await client.open_session(emotion="happy")


class TestMinimaxWSClientFeed:
    @pytest.mark.asyncio
    async def test_feed_yields_chunks_then_stops_on_is_final(self, connect_patch):
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0},
                        "session_id": "S", "trace_id": "T"}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "speech-2.8-turbo",
                                 "V", 1, sample_rate_out=32000)
        await client.open_session(emotion=None)

        hex_chunk_1 = (b"\x00\x00\x00\x00").hex()
        hex_chunk_2 = (b"\x00\x01\x00\x02").hex()
        ws.send_queue = [
            json.dumps({"data": {"audio": hex_chunk_1}, "is_final": False}),
            json.dumps({"data": {"audio": hex_chunk_2}, "is_final": False}),
            json.dumps({"data": {"audio": ""}, "is_final": True,
                        "subtitle_file": "https://subs.example/xyz.json"}),
        ]
        out = []
        async for pcm_f32 in client.feed("你好"):
            out.append(pcm_f32)

        assert ws.sent[-1]["event"] == "task_continue"
        assert ws.sent[-1]["text"] == "你好"
        assert len(out) == 2
        assert all(p.dtype == np.float32 for p in out)
        assert client.last_subtitle_url == "https://subs.example/xyz.json"
        await client.close_session()

    @pytest.mark.asyncio
    async def test_feed_handles_odd_byte_chunks_via_carry(self, connect_patch):
        """Odd-length hex (ends on nibble) → carry last byte to next chunk."""
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1,
                                 sample_rate_out=32000)
        await client.open_session(emotion=None)

        # chunk1: 3 bytes (odd → carry 1); chunk2: 3 bytes + 1 carry = 4 aligned = 2 samples
        ws.send_queue = [
            json.dumps({"data": {"audio": "aabbcc"}, "is_final": False}),
            json.dumps({"data": {"audio": "ddeeff"}, "is_final": False}),
            json.dumps({"data": {"audio": ""}, "is_final": True}),
        ]
        out = []
        async for pcm in client.feed("x"):
            out.append(pcm)
        total_samples = sum(len(p) for p in out)
        # 6 bytes total → 3 int16 samples (trailing nibble from odd alignment dropped)
        assert total_samples == 3
        await client.close_session()


class TestMinimaxWSClientIdle:
    @pytest.mark.asyncio
    async def test_idle_auto_close_after_timeout(self, connect_patch, monkeypatch):
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1)
        client._IDLE_CLOSE_SECONDS = 0.05  # fast for test
        await client.open_session(emotion=None)
        client.start_idle_watchdog()
        await asyncio.sleep(0.2)
        assert not client.is_open()


class TestStreamToPlayer:
    """TTSEngine.stream_to_player: chunk-by-chunk push to player, returns result."""

    @pytest.mark.asyncio
    async def test_stream_pushes_chunks_and_reports_complete(self, connect_patch):
        from core.tts import TTSEngine, PlaybackResult
        from core.tts_minimax_ws import MinimaxWSClient
        import threading

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1,
                                 sample_rate_out=32000)
        await client.open_session(emotion=None)

        ws.send_queue = [
            json.dumps({"data": {"audio": (b"\x00\x00" * 10).hex()}, "is_final": False}),
            json.dumps({"data": {"audio": ""}, "is_final": True}),
        ]

        player = MagicMock()
        player.played_samples = 0

        def fake_write(pcm, **kw):
            player.played_samples += len(pcm)
        player.write = fake_write
        player.drain = MagicMock(return_value=True)

        eng = TTSEngine.__new__(TTSEngine)
        eng.logger = MagicMock()
        eng._stream_player_sample_rate = 32000

        result = await eng._stream_to_player_async(
            "你好", emotion=None, player=player, ws_client=client,
            abort_event=threading.Event(),
        )
        assert isinstance(result, PlaybackResult)
        assert result.completed is True
        assert result.total_samples == 10
        assert player.played_samples == 10
        await client.close_session()


class TestWP5Truncation:
    """WP5 three-level degradation: L1 subtitle / L2 ring-buffer / L3 strict."""

    def test_l2_truncates_by_ring_buffer_fraction(self):
        """With only total_samples + played_samples, truncate by ratio + snap to punctuation."""
        from core.tts import _wp5_truncate

        text = "今天天气真好，我们出去散步吧。"  # 15 chars incl. punct
        # Played 60% of 30 total samples = 18 samples. Fraction 0.6 → k=int(15*0.6)=9
        # text[:9] = "今天天气真好，我们", last char is 们. Window 9..9+3 (20% of 15 ~= 3)
        # text[9:12] = "出去散". No 。！？，、 in window → return text[:9].
        out = _wp5_truncate(
            text=text,
            played_samples=18,
            sentence_start_samples=0,
            total_samples=30,
            subtitle_url=None,
            sample_rate=32000,
        )
        # With fraction 0.6 of 15 chars = 9 → "今天天气真好，我们" (9 chars, ends at 们)
        assert out.startswith("今天天气真好")
        assert len(out) >= 7

    def test_l3_returns_empty_when_no_progress(self):
        """No chunks received + nothing played → empty string (L3 strict)."""
        from core.tts import _wp5_truncate
        out = _wp5_truncate(
            text="abc",
            played_samples=0,
            sentence_start_samples=0,
            total_samples=None,
            subtitle_url=None,
            sample_rate=32000,
        )
        assert out == ""

    def test_snap_to_chinese_comma_within_window(self):
        """Char index inside a word; when punctuation lands in snap window, snap forward."""
        from core.tts import _wp5_truncate
        text = "甲乙丙丁，戊己庚辛壬癸"  # has 1 comma at index 4
        # 40% of 10 samples → k=int(11*0.4)=4. Window text[4:4+2]="，戊". snap to "，" at 4 → text[:5]="甲乙丙丁，"
        out = _wp5_truncate(
            text=text,
            played_samples=4,
            sentence_start_samples=0,
            total_samples=10,
            subtitle_url=None,
            sample_rate=32000,
        )
        assert out == "甲乙丙丁，"

    def test_no_punctuation_returns_raw_cut(self):
        """No punctuation in window → return raw char cut."""
        from core.tts import _wp5_truncate
        text = "甲乙丙丁戊己庚辛壬癸"  # no punctuation → no snap
        out = _wp5_truncate(
            text=text,
            played_samples=4,
            sentence_start_samples=0,
            total_samples=10,
            subtitle_url=None,
            sample_rate=32000,
        )
        assert out == "甲乙丙丁"

    def test_snap_to_space_in_english(self):
        """English: snap truncation to space boundary when inside window."""
        from core.tts import _wp5_truncate
        text = "hello world this is jarvis"
        # 40% of 10 → k=int(26*0.4)=10. text[:10]="hello worl". Window=int(26*0.2)=5.
        # text[10:15]="d this". Space at index 11 → snap → text[:12]="hello world "
        out = _wp5_truncate(
            text=text,
            played_samples=4,
            sentence_start_samples=0,
            total_samples=10,
            subtitle_url=None,
            sample_rate=32000,
        )
        assert out.startswith("hello world")

    def test_full_played_returns_full_text(self):
        """played_samples >= total_samples → fraction 1.0 → full text."""
        from core.tts import _wp5_truncate
        out = _wp5_truncate(
            text="完全播完了。",
            played_samples=100,
            sentence_start_samples=0,
            total_samples=100,
            subtitle_url=None,
            sample_rate=32000,
        )
        assert out == "完全播完了。"
