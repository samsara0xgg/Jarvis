"""The native_player switch (ADR 0129) never costs her voice."""

from __future__ import annotations

import shutil
import time
from types import SimpleNamespace

import pytest

from jarvis.surface.voice_native_out import NativeAudioStreamPlayer

RATE = 48_000
COMMON = {"sample_rate_hz": RATE, "ring_seconds": 0.25, "volume": 1.0}
pytestmark = pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")


def test_the_switch_keeps_the_python_player_when_the_helper_cannot_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """native_player on, but an unknown device: one warning, no helper."""
    from jarvis.runtime import inherent_loop  # noqa: PLC0415

    with caplog.at_level("WARNING"):
        unknown = inherent_loop._native_streaming_player(  # noqa: SLF001
            echo_canceller=None, device="No Such Output Device", **COMMON,
        )
    assert unknown is None
    assert "did not start" in caplog.text
    print("A1 switch fallback: unknown device -> None")  # noqa: T201


def test_an_echo_canceller_no_longer_forces_the_python_player() -> None:
    """Echo cancellation on: the native player starts and its tap is the canceller's far end."""
    from jarvis.runtime import inherent_loop  # noqa: PLC0415

    calls: list[int] = []
    canceller = SimpleNamespace(add_playback=lambda _block, rate, _at: calls.append(rate))
    player = inherent_loop._native_streaming_player(  # noqa: SLF001
        echo_canceller=canceller,  # type: ignore[arg-type]
        device=None,
        extra_args=("--null-device",),
        **COMMON,
    )
    assert isinstance(player, NativeAudioStreamPlayer)
    try:
        deadline = time.monotonic() + 5
        while not calls:
            assert time.monotonic() < deadline, "no rendered block reached add_playback"
            time.sleep(0.01)
        assert calls[0] == RATE
    finally:
        player.stop()
    print(f"A1 switch with AEC: native player, {len(calls)} far-end blocks in <5 s")  # noqa: T201
