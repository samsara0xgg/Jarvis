"""ADR-0005 §5.3 regression: _tts_watcher must not break real TTSPipeline.

The original _tts_watcher dispatched pipeline methods on the main event loop,
which made TTSPipeline._speak's asyncio.run() crash with
'asyncio.run() cannot be called from a running event loop'.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_tts

if TYPE_CHECKING:
    from pathlib import Path


def test_tts_watcher_drives_real_tts_pipeline_without_asyncio_run_crash(
    tmp_path: Path,
) -> None:
    """Real TTSPipeline driven by _tts_watcher must reach synthesize without crashing.

    Wires a real :class:`TTSPipeline` (with a mocked WS provider) into the
    watcher and asserts ``handle_chunk`` reaches ``synthesize`` without
    tripping the ``asyncio.run`` from-running-loop ``RuntimeError`` that
    ADR-0005 final review caught.
    """

    async def _body() -> None:
        db_path = tmp_path / "events.db"
        conn = open_event_log(db_path)

        # Real TTSPipeline; mocked provider returns PCM bytes.
        provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
        provider.synthesize = AsyncMock(return_value=b"\x00" * 1920)
        player = MagicMock(spec=voice_tts.AudioStreamPlayer)
        player.bytes_pending.return_value = 0
        pipeline = voice_tts.TTSPipeline(
            provider=provider,
            player=player,
            fallback=None,
        )

        task = asyncio.create_task(
            inherent_loop._tts_watcher(  # noqa: SLF001 — watcher is module-private by design.
                conn=conn,
                pipeline=pipeline,
                poll_interval_s=0.05,
            ),
        )
        await asyncio.sleep(0.1)

        emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T1",
                "query": "hi",
                "kind": "text",
                "required_gate_mode": "sentence",
            },
            correlation={"turn_id": "T1"},
        )
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={"turn_id": "T1", "text": "你好。"},
            correlation={"turn_id": "T1"},
        )
        # Sentence-mode now aggregates inside <voice>...</voice> regions and
        # flushes on close OR end_turn (Bug 3 fix); a plain-text chunk with
        # no closing tag waits for response_emitted to flush.
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={"turn_id": "T1", "text": "你好。"},
            correlation={"turn_id": "T1"},
        )
        await asyncio.sleep(0.3)  # generous; allow owned daemon synth + write
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert provider.synthesize.await_count == 1, (
            f"synthesize was awaited {provider.synthesize.await_count} times; "
            "TTSPipeline daemon worker did not own provider execution"
        )
        assert pipeline.close(wait_timeout_s=1.0)

    asyncio.run(_body())
