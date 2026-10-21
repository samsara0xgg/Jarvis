"""What serve_inherent stops when serving ends, and the time it has to (ADR 0006, 0009)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Final

from jarvis.runtime.turn.cancel import make_response_cancel_callable
from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.daemon.voice_power import VoiceInputOwners
    from jarvis.surface import voice_media, voice_session

LOGGER = logging.getLogger(__name__)

# How long serve_inherent's teardown may take from the moment serving ends. launchd kills
# a daemon 20 s after its stop (ExitTimeOut's default; the agent's plist sets none), so the
# watchers' wait and the stops of the children it started must end well inside that. A
# systemd unit waits longer (the brain's sets none, so 90 s); one bound serves both.
SHUTDOWN_BUDGET_S: Final[float] = 15.0

# What the stop of a child gets (the codex app-server, the MCP clients) when the budget is
# already spent by a turn that would not end: a child left running outlives the daemon.
CHILD_STOP_FLOOR_S: Final[float] = 2.0


def shutdown_tts(
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Bound TTS owners, then stop the player and release PortAudio.

    The caller has already installed the pipeline's close gate and cancelled
    the watcher. TTS provider/fallback work belongs to the media actor's daemon
    worker rather than asyncio's default executor; :meth:`StreamingTTSPipeline.close`
    cancels its owned task/process and joins that worker up to a hard bound.
    """
    if tts_pipe is None:
        return
    try:
        tts_pipe.close()
    except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
        LOGGER.debug("tts pipeline close failed", exc_info=True)


def _request_tts_close(
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Install the late-output gate before watcher cancellation; never raise."""
    if tts_pipe is None:
        return
    try:
        tts_pipe.request_close()
    except Exception:  # noqa: BLE001 — continue the remaining teardown.
        LOGGER.debug("tts pipeline close request failed", exc_info=True)


def _shutdown_duplex_voice_session(
    session: voice_session.DuplexVoiceSession | None,
) -> voice_session.VoiceSessionCloseResult | None:
    """Close Wave-3 input ownership and retain a typed shutdown trace."""
    if session is None:
        return None
    try:
        result = session.close()
    except Exception:
        LOGGER.exception("duplex voice session close raised")
        return None
    if not result.definitively_closed:
        LOGGER.error(
            "duplex voice session close incomplete: backend=%s workers=%s "
            "pending_detections=%d pending_commits=%d",
            result.ingress.backend_result,
            result.alive_threads,
            result.pending_detections,
            result.pending_commits,
        )
    return result


def request_voice_input_branch_shutdown(
    owners: VoiceInputOwners,
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Revoke input before output, the order serve uses (ADR-0006 F14)."""
    if owners.duplex_session is not None:
        _shutdown_duplex_voice_session(owners.duplex_session)
    _request_tts_close(tts_pipe)


def leave_unfinished_at_shutdown(
    runtime: JarvisRuntime, unfinished: Collection[asyncio.Task[None]], *, budget_s: float,
) -> None:
    """Record what the shutdown budget ran out on, and close the runs it leaves open.

    A task still running after the cancel is a turn in a model call that cannot be
    interrupted: its thread lives as long as the process does. Its response run is
    cancelled with the closed reason ``shutdown`` (ADR-0008 D10), so the log says why it
    ended instead of leaving it for the next boot to close as ``daemon_restart``.
    """
    names = sorted(task.get_name() for task in unfinished)
    LOGGER.warning(
        "serve_inherent: %d task(s) still running %.1f s into shutdown, left behind: %s",
        len(names), budget_s, ", ".join(names),
    )
    record_realtime_trace("shutdown_deadline", budget_s=budget_s, unfinished=", ".join(names))
    registry = runtime.response_runs
    if registry is None:
        return
    cancel = make_response_cancel_callable(runtime)
    for run in registry.open_runs():
        with contextlib.suppress(Exception):  # a locked log leaves it to the next boot
            cancel(run.response_id, "generation", "shutdown")


async def stop_children(runtime: JarvisRuntime, *, timeout_s: float) -> None:
    """Stop what the daemon started: each stop gets ``timeout_s`` off the loop; log, never raise."""
    stops: list[tuple[str, Callable[[], object]]] = []
    if runtime.workers is not None:
        # ADR 0019: the codex app-server child goes with the daemon; its open worker_edges
        # rows become closed.
        stops.append(("workers", runtime.workers.stop))
    if runtime.plugin_connections is not None:
        # ADR 0031: every MCP client exits on its own task, then its loop thread ends.
        stops.append(("plugin connections", runtime.plugin_connections.stop))
    if runtime.mcp_servers is not None:
        stops.append(("mcp servers", runtime.mcp_servers.stop))
    for name, stop in stops:
        try:
            await asyncio.wait_for(asyncio.to_thread(stop), timeout=timeout_s)
        except TimeoutError:
            LOGGER.warning("serve_inherent: %s did not stop in %.1f s; leaving it", name, timeout_s)
        except Exception:
            LOGGER.exception("serve_inherent: stopping %s failed", name)
