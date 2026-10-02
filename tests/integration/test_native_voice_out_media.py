"""A stop while the actor is still writing ends as an interruption, however slow the settle.

The helper acks a discard one render callback after the DISCARD frame, so
``settle_interrupted_generation`` is not instant the way it is with the Python
callback. A response task that wakes inside that window and sees the tombstone
used to end the turn as ``playback_failed`` / ``tts_fallback_unavailable``
(retryable) before the interrupt's own terminal committed (live harness
2026-10-02: three of four stop-word barge-ins).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_media
from jarvis.surface.voice_native_out import NativeAudioStreamPlayer
from tests.integration.test_wave2_streaming_media import (
    _Behavior,
    _CallbackPump,
    _config,
    _emit_response,
    _FakeProvider,
    _playback_rows_for,
    _player,
    _submit_response,
    _terminal_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

needs_swiftc = pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")


def test_a_slow_settle_does_not_let_the_response_task_fail_the_turn_first(tmp_path: Path) -> None:
    """The interrupt's settle is slow (ack not back); a response task reaching its end meanwhile."""
    db_path = tmp_path / "slow.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    provider.behaviors[("RSLOW", 0)] = _Behavior(samples=24_000, final_delay_s=0.0)
    player = _player()
    real_settle = player.settle_interrupted_generation
    callers: list[object] = []
    first_poll: list[float] = []

    def slow_settle(**kwargs: int) -> object:
        """The first caller (the interrupt) waits for the ack; a second one is not slowed."""
        task = asyncio.current_task()
        if not callers:
            first_poll.append(time.monotonic())
        if task not in callers:
            callers.append(task)
        if task is callers[0] and time.monotonic() - first_poll[0] < 0.3:
            return None
        return real_settle(**kwargs)

    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), response_timeout_s=10.0),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            rows = _emit_response(
                conn, response_id="RSLOW", group_id="G", turn_id="T", text="Counting slowly.",
            )
            asyncio.run(_submit_response(pipeline, rows))
            deadline = time.monotonic() + 5
            while player.played_samples < 800:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            with patch.object(player, "settle_interrupted_generation", side_effect=slow_settle):
                assert pipeline.stop_foreground_output("RSLOW") == "applied"
                assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        pipeline.close()
    verdict = open_event_log(db_path)
    try:
        terminals = [(kind, payload.get("reason")) for kind, payload in _terminal_rows(verdict)]
        assert terminals == [("surface.playback_interrupted", "user_stop")]
    finally:
        verdict.close()
        conn.close()


@needs_swiftc
@pytest.mark.parametrize("rate", [48_000, 8_000])
def test_a_stop_while_audio_is_still_being_written_is_an_interruption_not_a_failure(
    tmp_path: Path,
    rate: int,
) -> None:
    """At 8 kHz a callback lasts 64 ms, longer than settle's bounded wait: the None path too."""
    db_path = tmp_path / "stop.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(candidate_count=1)
    provider.behaviors[("RSTOP", 0)] = _Behavior(
        samples=rate * 3, sample_rate_hz=rate, final_delay_s=0.0,
    )
    player = NativeAudioStreamPlayer(
        sample_rate_hz=rate,
        ring_seconds=0.25,
        generation_safe=True,
        estimated_output_latency_s=0.0,
        extra_args=("--null-device",),
    )
    assert player.start().started
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), canonical_sample_rate_hz=rate, response_timeout_s=10.0),
        start_player=False,
    )
    try:
        rows = _emit_response(
            conn, response_id="RSTOP", group_id="G", turn_id="T", text="Counting slowly.",
        )
        asyncio.run(_submit_response(pipeline, rows))
        deadline = time.monotonic() + 5
        while player.played_samples < rate // 4:  # audio is playing and the ring is full
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert pipeline.stop_foreground_output("RSTOP") == "applied"
        assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        pipeline.close()
        player.stop()
    verdict = open_event_log(db_path)
    try:
        terminals = [(kind, payload.get("reason")) for kind, payload in _terminal_rows(verdict)]
        assert terminals == [("surface.playback_interrupted", "user_stop")]
        assert _playback_rows_for(verdict, "RSTOP", "surface.playback_failed") == 0
    finally:
        verdict.close()
        conn.close()
    print(f"A1 stop race @{rate} Hz: terminals={terminals}")  # noqa: T201
