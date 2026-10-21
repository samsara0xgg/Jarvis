"""Daemon entrypoint: :func:`serve_inherent` composes the daemon modules and serves.

Composition root (``runtime/``). Owns the order the daemon starts in and, through
:func:`jarvis.runtime.daemon.teardown.shutdown`, the reverse order it stops in; every concern it
starts lives in its own module under :mod:`jarvis.runtime.daemon` (ADR 0339).

Layer rules
-----------

``runtime/`` is the composition root and may import from every sibling
layer (``deployment``, ``state``, ``surface``); ``lint-imports`` allows
this and refuses any new cross-sibling edges.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.deployment import models, rotate_inherent_v2_token
from jarvis.runtime.daemon.background import start_background, start_power
from jarvis.runtime.daemon.controls import build_controls, set_quiet
from jarvis.runtime.daemon.deps import build_deps, build_setup
from jarvis.runtime.daemon.observers import make_usage_observer
from jarvis.runtime.daemon.output_watchers import turn_devices
from jarvis.runtime.daemon.phone_brain import build_phone_hub
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S
from jarvis.runtime.daemon.reconcile import reconcile_at_boot
from jarvis.runtime.daemon.server import build_server, listen_sockets
from jarvis.runtime.daemon.teardown import SHUTDOWN_BUDGET_S, Running, shutdown
from jarvis.runtime.daemon.voice_boot import Voice, boot_dictation, boot_voice
from jarvis.runtime.daemon.voice_wiring import voice_knobs
from jarvis.runtime.spend_cap import SpendCapSettings
from jarvis.surface.inherent_output import InherentBroadcaster

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.surface.codex_sessions import CodexSession

# Default port the Inherent client connects to. Picked to NOT collide
# with the legacy ``ui/web/server.py`` default (8000) or the
# inherent-swift dev server (8001).
_DEFAULT_PORT: int = 8006


async def serve_inherent(
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    shutdown_budget_s: float = SHUTDOWN_BUDGET_S,
) -> None:
    """Run the Inherent daemon (text + voice). Blocks until SIGINT / SIGTERM.

    The caller holds the daemon lock (:func:`jarvis.deployment.daemon_session`,
    ADR 0318): it is the OUTERMOST scope, so its release is the very LAST
    cleanup step, after this function has returned and the Event Log is closed.

    Lifecycle:

    1. (the caller) :func:`jarvis.deployment.daemon_session` — raises
       :class:`jarvis.deployment.process_lock.ProcessLockHeld` if
       another live daemon already holds it, before it erases or opens anything.
    2. Build :class:`InherentBroadcaster` and attach the running
       event loop via :meth:`InherentBroadcaster.attach_loop` (ADR-0005
       §4.2 worker-thread → broadcaster bridge), then the mute switches
       (:func:`build_controls`) and the :class:`Voice` the rest of boot fills.
    3. Register :func:`shutdown` on the exit stack: from here a failing step, or serving
       ending, stops what had started. It tears the power observer down
       FIRST (its closed flag must be set before the loop starts winding
       down), then stops the voice input (if running), cancels
       every watcher task and waits for them, so a watcher that crashed
       during runtime does not mask the shutdown path. The whole teardown
       is bounded by ``shutdown_budget_s``: a turn still in a model call at
       the deadline is left running, its response run cancelled as
       ``shutdown``, and the children are stopped anyway.
    4. ADR-0005 §12 pre-flight, the voice pipeline, the single audio ingress and GPT-Live
       (:func:`boot_voice`). Missing models or any construction failure: log ERROR and
       continue text-only — the text path stays healthy.
    5. Close the responses and playbacks the last run left open (:func:`reconcile_at_boot`).
    6. Build the FastAPI app (:func:`build_server`) over :func:`build_deps`, with
       ``lifespan="off"`` because this module owns the lifecycle.
    7. Start the intent pump and the watchers (:func:`start_background`) BEFORE
       :meth:`uvicorn.Server.serve` so an early intent post is observed. ADR-0009 D4: the
       sweep control plane anchors the system-trigger watcher, runs the one-shot bootstrap
       sweep, then starts the periodic sweep; ADR-0009 D5: the observers follow it.
    8. ADR-0009 D3 — :func:`start_power`: install the power observer with ``loop=`` the
       running loop, so its CFRunLoop-thread notifications marshal their ``mac.sleeping`` /
       ``mac.awake`` emits onto this loop. Any failure is logged and swallowed (F4: the
       daemon serves text + voice without sleep/wake rather than dying).
    9. ``await server.serve()`` — blocks until uvicorn returns (signal
       received).

    Env vars:
        ``MINIMAX_API_KEY``         — gates the TTS subsystem.
        ``JARVIS_VOICE_DISABLE_WAKE`` — when ``"1"``, skip the
            microphone owner even if models are present (CI / smoke tests).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        host: Bind interface for the FastAPI app.
        port: TCP port for the FastAPI app.
        poll_interval_s: Watcher poll cadence (default 10 ms).
        shutdown_budget_s: How long the teardown waits for the watchers (and the turns they
            drive) before it leaves them behind and stops the children regardless.
    """
    async with contextlib.AsyncExitStack() as unwind:
        if runtime.decision_state is not None:
            runtime.decision_state.warm()  # ADR 0164: the first turn then reads only a delta
        if runtime.ledger is not None:
            runtime.ledger.warm(lambda: datetime.now(UTC))  # its standing text is a 38-day read
        broadcaster = InherentBroadcaster()
        broadcaster.attach_loop(asyncio.get_running_loop())
        broadcaster.turn_device = turn_devices(runtime.conn)  # ADR 0222

        voice = Voice(controls=build_controls(runtime, broadcaster))
        voice.controls.on_speech_muted = voice.apply_speech_mute
        knobs = voice_knobs(runtime.config.raw)
        # A failed setup is a stopped daemon, not a half-alive one: the voice owners started
        # below hold non-daemon threads, and nothing else ends the process. The same teardown
        # runs on a setup failure and on a normal stop; what is not built yet is None.
        running = Running(voice)
        unwind.push_async_callback(
            functools.partial(shutdown, runtime, running, budget_s=shutdown_budget_s),
        )

        model_fetch = models.Progress()
        boot_voice(runtime, voice, broadcaster=broadcaster, knobs=knobs, model_fetch=model_fetch)

        await reconcile_at_boot(runtime)

        # ADR-0014 D5: a fresh 256-bit bearer token every boot, written
        # 0600 under the runtime root. Rotating INSIDE the process lock is
        # what makes it safe — no second daemon can be mid-read of the
        # file this one is replacing.
        v2_token = rotate_inherent_v2_token(runtime.runtime_paths.inherent_v2_token)
        # ADR-0018 — usage observer: constructed here (baselines recovered on
        # the loop thread) so the dashboard routes can hold its read model
        # and on-demand poll; its periodic task joins the watchers.
        usage_observer = make_usage_observer(runtime)
        setup = build_setup(runtime, knobs, model_fetch)
        boot_dictation(runtime, voice)

        # ADR 0019: one Codex board, filled by the hooks' route and read by the night run.
        codex_board: dict[str, CodexSession] = {}
        if runtime.view is not None:  # ADR 0176: her ``present`` op reaches the companion
            runtime.view.push = lambda sent: broadcaster.broadcast_op_sync("present", **sent)
        spend = SpendCapSettings.from_config(runtime.config.raw.get("spend_cap"))
        built_phone = (
            build_phone_hub(runtime, knobs, set_quiet=functools.partial(set_quiet, voice.controls))
            if runtime.config.listen_addresses
            else None
        )
        phone_hub = None if built_phone is None else built_phone[0]
        server = build_server(
            runtime,
            build_deps(
                runtime,
                broadcaster=broadcaster,
                voice=voice,
                setup=setup,
                usage_observer=usage_observer,
                codex_board=codex_board,
                phone_hub=phone_hub,
                spend=spend,
                v2_token=v2_token,
                port=port,
            ),
            host=host,
            port=port,
        )

        await start_background(
            runtime,
            running,
            broadcaster=broadcaster,
            setup=setup,
            usage_observer=usage_observer,
            codex_board=codex_board,
            phone_hub=phone_hub,
            phone_ends=None if built_phone is None else built_phone[1],
            spend=spend,
            poll_interval_s=poll_interval_s,
        )
        start_power(runtime, running)

        await server.serve(
            sockets=listen_sockets(host, port, runtime.config.listen_addresses)
            if runtime.config.listen_addresses
            else None,
        )


__all__ = ["serve_inherent"]
