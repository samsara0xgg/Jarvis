"""ADR-0005 §5.3: ``_tts_watcher`` dispatches surface.response_* into TTSPipeline.

Inherent Voice migration Task 18. Adds a second background watcher on the
runtime composition root that polls the same single-cursor
``surface.response_{open,chunk,emitted}`` stream that
:func:`jarvis.runtime.inherent_loop._response_watcher` consumes (for the
text-surface broadcaster) and dispatches each row to the corresponding
:class:`jarvis.surface.voice_tts.TTSPipeline` method
(``begin_turn`` / ``handle_chunk`` / ``handle_emitted``).

Spec basis: ADR-0005 §5.3 + §4.3 table row ``_tts_watcher`` — the TTS path
must observe the three Inherent response events in their L2-insertion
order; folding all three into ONE cursor (``WHERE type IN (...) ORDER BY
id``) is the same D16-style race-elimination argument the broadcaster
watcher uses.

Test approach: stand up the watcher against a real on-disk SQLite Event
Log (no LLM, no daemon HTTP boot), pass a :class:`unittest.mock.MagicMock`
as the pipeline, emit each of the three event types, and assert the
three dispatch targets were called with the expected arguments. Mirrors
``test_inherent_loop_utterance_watcher.py`` (Task 17): ``asyncio.run``
wrapper, polled settle window, ``contextlib.suppress`` cancellation
swallow.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path


def test_tts_watcher_dispatches_three_event_types(tmp_path: Path) -> None:
    """One ``open`` + one ``chunk`` + one ``emitted`` row hit the three pipeline methods.

    ADR-0005 §5.3: the watcher folds the three response event types into
    one cursor and dispatches each row by ``event.type`` to the matching
    :class:`TTSPipeline` method. The header carries ``required_gate_mode``
    (here ``"sentence"``) which the watcher forwards as the ``gate_mode``
    kwarg of ``begin_turn``.
    """
    async def _body() -> None:
        db_path = tmp_path / "events.db"
        conn = open_event_log(db_path)
        pipeline = MagicMock()
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
            payload={"turn_id": "T1", "text": "hi."},
            correlation={"turn_id": "T1"},
        )
        emit_event(
            conn,
            type="surface.response_emitted",
            # ``text`` is a required-payload field per the event-log registry
            # (jarvis/state/event_log.py L432); the watcher itself does not
            # read it but the L2 INSERT path validates the schema.
            payload={"turn_id": "T1", "text": "hi."},
            correlation={"turn_id": "T1"},
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        pipeline.begin_turn.assert_called_once_with("T1", gate_mode="sentence")
        pipeline.handle_chunk.assert_called_once_with("T1", "hi.")
        pipeline.handle_emitted.assert_called_once_with("T1")

    asyncio.run(_body())


def test_tts_watcher_defaults_gate_mode_to_sentence_when_missing(
    tmp_path: Path,
) -> None:
    """A ``surface.response_open`` row without ``required_gate_mode`` falls back to ``"sentence"``.

    ADR-0005 §5.3: ``required_gate_mode`` is optional on
    ``surface.response_open`` so legacy emitters and event-log unit-test
    seeds that omit it still drive the safe full-sentence TTS path. The
    watcher must default the kwarg, not crash on ``KeyError``.
    """
    async def _body() -> None:
        db_path = tmp_path / "events.db"
        conn = open_event_log(db_path)
        pipeline = MagicMock()
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
            payload={"turn_id": "T2", "query": "hi", "kind": "text"},
            correlation={"turn_id": "T2"},
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        pipeline.begin_turn.assert_called_once_with("T2", gate_mode="sentence")

    asyncio.run(_body())
