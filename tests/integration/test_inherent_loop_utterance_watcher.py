"""ADR-0005 §5.1 wiring: _user_intent_watcher also consumes ``utterance.received``.

Inherent Voice migration Task 17. Phase 5 (TTS) is complete; this is the
first wiring task of Phase 6 — the watcher that drives a turn from a
user intent must accept BOTH the keyboard surface event
(``surface.user_intent``, emitted by ``cli/__main__.py`` and the daemon
``/inherent/submit`` HTTP handler) AND the voice surface event
(``utterance.received``, emitted by the ASR pipeline / wake listener
when a recognized utterance arrives).

Spec basis: ADR-0005 §5.1 "Important wiring detail" + §4.3 table — the
voice surface lands the recognized transcript on ``utterance.received``;
the inherent-loop watcher folds that into a turn EXACTLY the same way
as the keyboard ``surface.user_intent`` path, so the L3 / L4 / L5
downstream is identical regardless of input channel.

Test approach: stand up the watcher against a real on-disk SQLite Event
Log (no LLM, no daemon HTTP boot), monkeypatch
:func:`jarvis.runtime.inherent_loop._drive_turn_in_worker_thread` to a
recording stub, emit one event of each type, and assert both reach the
stub. Mirrors the established ``test_inherent_loop.py`` unit-test
pattern (``asyncio.run(_body())`` wrapper, ``_seed_briefly`` settle
loop) so the integration test matches the rest of the inherent-loop
suite.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, inherent_loop
from jarvis.runtime.inherent_loop import _user_intent_watcher
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import Event


def _make_runtime(tmp_path: Path) -> JarvisRuntime:
    """Build a minimal :class:`JarvisRuntime` with a real Event Log.

    Mirrors the helper in ``tests/unit/test_inherent_loop.py``: the
    watcher only exercises ``runtime.conn`` and ``runtime.runtime_paths``,
    so the rest (config / llm_client / system_prompt) get sentinel
    values because the worker-thread dispatcher is monkeypatched.
    """
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    return JarvisRuntime(
        config={},
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=None,  # type: ignore[arg-type]
        system_prompt="",
    )


async def _spin_briefly() -> None:
    """Yield to the loop a few times so the watcher polls."""
    for _ in range(20):
        await asyncio.sleep(0.005)


async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
    """Cancel a watcher task and swallow the cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_user_intent_watcher_drives_turn_on_utterance_received(
    tmp_path: Path,
) -> None:
    """Both ``surface.user_intent`` and ``utterance.received`` reach the worker.

    ADR-0005 §5.1: the voice surface and the keyboard surface both
    drive a turn through the same composition-root code path. The
    watcher MUST accept both event types; the downstream
    ``drive_turn`` (and thus L3 / L4 / L5) is identical.
    """
    runtime = _make_runtime(tmp_path)
    driven_types: list[str] = []

    def _fake_drive(
        _rt: JarvisRuntime,
        *,
        user_intent_event: Event,
        **__: object,
    ) -> None:
        driven_types.append(user_intent_event.type)

    async def _body() -> None:
        with patch.object(
            inherent_loop, "_drive_turn_in_worker_thread", side_effect=_fake_drive,
        ):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)

            # Emit one of each event type. The watcher must drive a
            # turn for BOTH.
            emit_event(
                runtime.conn,
                type="surface.user_intent",
                payload={
                    "transcript": "hi",
                    "turn_id": "Tcli",
                    "channel": "cli_stdin",
                    "language": "en",
                },
                correlation={"turn_id": "Tcli"},
            )
            emit_event(
                runtime.conn,
                type="utterance.received",
                payload={
                    "transcript": "你好",
                    "turn_id": "Tvoice",
                    "channel": "inherent_wake",
                    "language": "zh-CN",
                },
                correlation={"turn_id": "Tvoice"},
            )

            await _spin_briefly()
            await _cancel_and_await(task)

        assert "surface.user_intent" in driven_types, (
            f"keyboard intent did not drive a turn (driven={driven_types!r})"
        )
        assert "utterance.received" in driven_types, (
            f"voice utterance did not drive a turn (driven={driven_types!r})"
        )

    try:
        asyncio.run(_body())
    finally:
        runtime.conn.close()
