"""The native_player switch (ADR 0129) never costs her voice."""

from __future__ import annotations

import shutil

import pytest

RATE = 48_000
pytestmark = pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")


def test_the_switch_keeps_the_python_player_when_the_helper_cannot_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """native_player on, but an unknown device or an echo canceller: one warning, no helper."""
    from jarvis.runtime import inherent_loop  # noqa: PLC0415

    common = {"sample_rate_hz": RATE, "ring_seconds": 0.25, "volume": 1.0}
    with caplog.at_level("WARNING"):
        unknown = inherent_loop._native_streaming_player(  # noqa: SLF001
            echo_canceller=None, device="No Such Output Device", **common,
        )
        echo = inherent_loop._native_streaming_player(  # noqa: SLF001
            echo_canceller=object(),  # type: ignore[arg-type]
            device=None,
            **common,
        )
    assert unknown is None
    assert echo is None
    assert "did not start" in caplog.text
    assert "echo cancellation needs" in caplog.text
    print("A1 switch fallback: unknown device -> None, echo canceller -> None")  # noqa: T201
