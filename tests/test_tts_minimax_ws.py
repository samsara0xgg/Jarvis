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


class TestAbortRace:
    @pytest.mark.asyncio
    async def test_abort_mid_stream_stops_yielding(self, connect_patch):
        """Abort flag set mid-stream → feed exits without yielding more chunks."""
        from core.tts import TTSEngine
        from core.tts_minimax_ws import MinimaxWSClient
        import threading
        import time as _time

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1,
                                 sample_rate_out=32000)
        await client.open_session(emotion=None)

        # Many chunks — abort fires mid-way (50ms in, mock recv has 1ms sim-delay)
        ws.send_queue = [
            json.dumps({"data": {"audio": (b"\x00\x00" * 20).hex()}, "is_final": False}),
            json.dumps({"data": {"audio": (b"\x00\x01" * 20).hex()}, "is_final": False}),
            json.dumps({"data": {"audio": (b"\x00\x02" * 20).hex()}, "is_final": False}),
            json.dumps({"data": {"audio": (b"\x00\x03" * 20).hex()}, "is_final": False}),
            json.dumps({"data": {"audio": ""}, "is_final": True}),
        ]

        player = MagicMock()
        player.played_samples = 0
        write_count = [0]

        def fake_write(pcm, **kw):
            write_count[0] += 1
            player.played_samples += len(pcm)
        player.write = fake_write
        player.drain = MagicMock(return_value=False)

        abort_ev = threading.Event()

        async def _trigger_abort():
            await asyncio.sleep(0.05)
            abort_ev.set()

        eng = TTSEngine.__new__(TTSEngine)
        eng.logger = MagicMock()
        eng._stream_player_sample_rate = 32000

        asyncio.create_task(_trigger_abort())
        t0 = _time.monotonic()
        result = await eng._stream_to_player_async(
            "long text", None, player, client, abort_ev,
        )
        elapsed_ms = (_time.monotonic() - t0) * 1000

        # Abort should cut at or before all 4 chunks processed (may process all
        # 4 if network is super fast; what we really test is that abort_ev is
        # honored and no hang).
        assert not result.completed
        assert elapsed_ms < 500

        await client.close_session()

    def test_pipeline_abort_closes_ws(self):
        """TTSPipeline.abort invokes ws_client.close_session."""
        import threading
        import queue as _q
        from core.tts import TTSPipeline, TTSEngine

        eng = TTSEngine.__new__(TTSEngine)
        eng.engine_name = "edge-tts"  # non-streaming path for this test
        eng.logger = MagicMock()
        eng.stop = MagicMock()
        eng._stream_player = None
        pipeline = TTSPipeline.__new__(TTSPipeline)
        pipeline._engine = eng
        pipeline._aborted = threading.Event()
        pipeline._text_queue = _q.Queue()
        pipeline._audio_queue = _q.Queue()
        pipeline._progress_lock = threading.Lock()
        pipeline._played_texts = []
        pipeline._currently_playing = None
        pipeline._progress_map = {}
        pipeline.logger = MagicMock()

        ws_client = MagicMock()
        ws_client.close_session = AsyncMock(return_value={})
        pipeline._ws_client = ws_client
        pipeline._ws_loop = asyncio.new_event_loop()
        t = threading.Thread(target=pipeline._ws_loop.run_forever, daemon=True)
        t.start()

        try:
            pipeline.abort()
            assert ws_client.close_session.call_count >= 1
        finally:
            pipeline._ws_loop.call_soon_threadsafe(pipeline._ws_loop.stop)
            t.join(timeout=2)


class TestTurnLevelSession:
    @pytest.mark.asyncio
    async def test_three_feeds_share_one_ws_session(self, connect_patch):
        """Multiple feed() calls on one ws: each sends task_continue, no reconnect."""
        from core.tts_minimax_ws import MinimaxWSClient

        ws = connect_patch
        ws.send_queue = [
            json.dumps({"event": "connected_success", "base_resp": {"status_code": 0}}),
            json.dumps({"event": "task_started", "base_resp": {"status_code": 0}}),
        ]
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1,
                                 sample_rate_out=32000)
        await client.open_session(emotion=None)

        chunk_msgs = [
            json.dumps({"data": {"audio": (b"\x00\x00" * 4).hex()}, "is_final": True}),
            json.dumps({"data": {"audio": (b"\x01\x01" * 4).hex()}, "is_final": True}),
            json.dumps({"data": {"audio": (b"\x02\x02" * 4).hex()}, "is_final": True}),
        ]
        for i, text in enumerate(["句一。", "句二。", "句三。"]):
            ws.send_queue = [chunk_msgs[i]]
            out = [p async for p in client.feed(text)]
            assert len(out) >= 1
        tc_count = sum(1 for m in ws.sent if m.get("event") == "task_continue")
        assert tc_count == 3
        # Only ONE task_start was sent at open_session (sents[0])
        ts_count = sum(1 for m in ws.sent if m.get("event") == "task_start")
        assert ts_count == 1
        await client.close_session()


class TestPrewarm:
    def test_prewarm_opens_ws_in_background(self, monkeypatch):
        """prewarm() starts ws loop + opens session; is_open() true after."""
        import threading
        from core.tts import TTSEngine, TTSPipeline

        eng = TTSEngine.__new__(TTSEngine)
        eng.engine_name = "minimax"
        eng.logger = MagicMock()
        eng.minimax_key = "sk-api"
        eng.minimax_model = "speech-2.8-turbo"
        eng.minimax_voice = "V"
        eng.minimax_volume = 1
        eng._minimax_base_url = "https://api-uw.minimax.io"
        eng._minimax_ws_enabled = True
        eng._stream_player_sample_rate = 32000
        eng._ensure_stream_player = MagicMock(return_value=MagicMock())

        fake_client = MagicMock()
        fake_client.is_open = MagicMock(return_value=True)

        async def fake_open(emotion):
            return None
        fake_client.open_session = fake_open
        fake_client.start_idle_watchdog = MagicMock()

        import core.tts_minimax_ws as ws_mod
        monkeypatch.setattr(ws_mod, "MinimaxWSClient", lambda **kw: fake_client)

        pipeline = TTSPipeline.__new__(TTSPipeline)
        pipeline._engine = eng
        pipeline._ws_client = None
        pipeline._ws_loop = None
        pipeline._ws_thread = None

        pipeline.prewarm("HAPPY")
        assert pipeline._ws_client is fake_client
        assert pipeline._ws_client.is_open()

        if pipeline._ws_loop is not None:
            pipeline._ws_loop.call_soon_threadsafe(pipeline._ws_loop.stop)
            if pipeline._ws_thread:
                pipeline._ws_thread.join(timeout=2)


class TestFallbackChain:
    def test_ws_connect_failure_raises_for_engine_fallback(self, monkeypatch):
        """ws connect raises → MinimaxWSClient.open_session raises WSConnectError."""
        from core.tts_minimax_ws import MinimaxWSClient, WSConnectError
        import websockets as ws_mod

        async def boom(*a, **kw):
            raise ConnectionRefusedError("simulated")

        monkeypatch.setattr(ws_mod, "connect", boom)
        client = MinimaxWSClient("https://api-uw.minimax.io", "sk", "m", "V", 1)

        with pytest.raises(WSConnectError):
            asyncio.run(client.open_session(emotion=None))


class TestSubtitleFailure:
    def test_subtitle_fetch_exception_falls_to_l2(self, monkeypatch):
        """Subtitle URL set but fetch raises → L2 by ring fraction still works."""
        from core.tts import _wp5_truncate

        import urllib.request

        def boom(*a, **kw):
            raise OSError("sim network")
        monkeypatch.setattr(urllib.request, "urlopen", boom)

        out = _wp5_truncate(
            text="今天天气真好，我们出去散步吧。",
            played_samples=15,
            sentence_start_samples=0,
            total_samples=30,
            subtitle_url="https://subs.example/x.json",
            sample_rate=32000,
        )
        assert len(out) > 0  # L2 kicked in
        assert len(out) < len("今天天气真好，我们出去散步吧。")


class TestCacheBypass:
    """Short text in cache → don't open ws."""

    def test_short_text_cache_hit_skips_ws(self, tmp_path, monkeypatch):
        from core.tts import TTSEngine, _minimax_emotion_effective
        from unittest.mock import patch

        with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
            eng = TTSEngine.__new__(TTSEngine)
            eng.engine_name = "minimax"
            eng.minimax_key = "sk"
            eng.minimax_model = "speech-2.8-turbo"
            eng.minimax_voice = "V"
            eng.minimax_volume = 1
            eng._minimax_base_url = "https://api-uw.minimax.io"
            eng._minimax_url = f"{eng._minimax_base_url}/v1/t2a_v2"
            eng._tts_cache_dir = tmp_path
            eng._tts_cache_max = 5
            eng.speed = 1.0
            eng.logger = MagicMock()
            eng._preprocessor_config = {}

        emo_eff = _minimax_emotion_effective("calm") or ""
        key = eng._tts_cache_key("好的", emo_eff)
        cache_path = tmp_path / f"{key}.pcm"
        cache_path.write_bytes(b"\x00" * 64)

        from core import tts as tts_mod

        async def should_not_be_called(*a, **kw):
            raise AssertionError("ws should not be called on cache hit")
        monkeypatch.setattr(tts_mod, "_ws_collect_audio", should_not_be_called)

        path, deletable = eng._synth_minimax("好的", "calm")
        assert path == str(cache_path)
        assert deletable is False
