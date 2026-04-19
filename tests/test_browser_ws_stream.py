# tests/test_browser_ws_stream.py
"""Tests for browser-side WebSocket TTS streaming (BrowserWSPlayer + server routes)."""
import asyncio
import struct
from unittest.mock import MagicMock

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Task 1: BrowserWSPlayer
# ---------------------------------------------------------------------------

class TestBrowserWSPlayer:
    def test_write_forwards_header_plus_int16_bytes(self):
        """BrowserWSPlayer.write packs uint16 header + int16LE samples
        and schedules ws.send_bytes via run_coroutine_threadsafe."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        import ui.web.browser_ws_player as mod

        captured_payloads: list[bytes] = []

        async def fake_send_bytes(data: bytes) -> None:
            captured_payloads.append(data)

        ws = MagicMock()
        ws.send_bytes = fake_send_bytes

        # Replace asyncio.run_coroutine_threadsafe with a sync driver so the
        # coroutine runs inline in the test. The production path needs
        # cross-thread scheduling; the test doesn't have two threads.
        loop = asyncio.new_event_loop()
        original = mod.asyncio.run_coroutine_threadsafe
        def fake_run_cot(coro, _loop):
            loop.run_until_complete(coro)
            return MagicMock()
        mod.asyncio.run_coroutine_threadsafe = fake_run_cot
        try:
            player = BrowserWSPlayer(ws=ws, sentence_index=3, loop=loop)
            pcm = np.array([1.0, -1.0, 0.5, 0.0], dtype=np.float32)
            player.write(pcm)

            assert len(captured_payloads) == 1
            payload = captured_payloads[0]
            # Header: uint16 LE = 3 → b"\x03\x00"
            assert payload[:2] == b"\x03\x00"
            # Body: 4 * int16 LE. 1.0 → 32767, -1.0 → -32767, 0.5 → 16383, 0.0 → 0
            import struct as _s
            samples = _s.unpack("<4h", payload[2:])
            assert samples[0] == 32767
            assert samples[1] == -32767
            assert 16000 <= samples[2] <= 16383  # round/clip leeway
            assert samples[3] == 0
        finally:
            mod.asyncio.run_coroutine_threadsafe = original
            loop.close()

    def test_played_samples_monotonic(self):
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            ws = MagicMock()
            player = BrowserWSPlayer(ws=ws, sentence_index=0, loop=loop)
            assert player.played_samples == 0
            player.write(np.zeros(100, dtype=np.float32))
            assert player.played_samples == 100
            player.write(np.zeros(50, dtype=np.float32))
            assert player.played_samples == 150
        finally:
            loop.close()

    def test_drain_returns_true(self):
        """D11: drain() must return True so PlaybackResult.completed=True
        and WP5 records this sentence in played_texts."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            player = BrowserWSPlayer(ws=MagicMock(), sentence_index=0, loop=loop)
            assert player.drain(5.0) is True
            assert player.drain() is True  # default timeout
        finally:
            loop.close()
