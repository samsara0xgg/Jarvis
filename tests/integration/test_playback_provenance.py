"""Real media actor failures before the durable speech/source mapping exists."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.surface import voice_media
from tests.integration.test_wave2_streaming_media import (
    _config,
    _emit_response,
    _FakeProvider,
    _player,
    _submit_response,
    _terminal_rows,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import Event


@pytest.mark.parametrize(
    "failed_fact", ["surface.playback_started", "surface.playback_segment_prepared"]
)
def test_uncommitted_playback_mapping_never_reaches_provider_or_player(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_fact: str,
) -> None:
    """A durable mapping failure terminates the lease without issuing a TTS request."""
    path = tmp_path / "events.db"
    provider = _FakeProvider()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=_player(),
        conn_factory=lambda: open_event_log(path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )

    def fail_mapping(*args: Any, **kwargs: Any) -> Event:  # noqa: ANN401 - injected append boundary
        if kwargs.get("type") == failed_fact:
            message = "synthetic mapping persistence failure"
            raise RuntimeError(message)
        return emit_event(*args, **kwargs)

    monkeypatch.setattr(voice_media, "emit_event", fail_mapping)
    try:
        with contextlib.closing(open_event_log(path)) as conn:
            rows = _emit_response(
                conn, response_id="R", group_id="G", turn_id="T", text="Mapped sentence."
            )
            asyncio.run(_submit_response(pipeline, rows))
            deadline = time.monotonic() + 1
            while not _terminal_rows(conn) and time.monotonic() < deadline:
                time.sleep(0.005)
            terminals = _terminal_rows(conn)
            assert len(terminals) == 1
            kind, payload = terminals[0]
            assert kind == "surface.playback_failed"
            assert payload["submitted_samples"] == 0
            assert payload["heard_text"] == ""
            assert provider.opened == []
            assert provider.sent == []
            assert all(event.type != failed_fact for event in iter_events(conn))
    finally:
        pipeline.close()
