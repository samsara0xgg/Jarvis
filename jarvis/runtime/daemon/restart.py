"""Restarting the daemon: on request, once it is idle, and after the first-boot models arrive."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from typing import TYPE_CHECKING

from jarvis.deployment import models
from jarvis.deployment.launchd import supervised

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


_IDLE_POLL_S = 5.0
restarting: set[asyncio.Task[None]] = set()


async def restart_when_idle(idle: Callable[[], bool]) -> None:
    """ADR 0227: restart once no conversation or speech is going on; checked every few seconds."""
    while not idle():  # noqa: ASYNC110 — idle is a plain predicate, there is no event for it.
        await asyncio.sleep(_IDLE_POLL_S)
    LOGGER.info("settings: restarting to take in the settings the owner chose")
    restart_soon()


def fetch_models_then_restart(
    sensevoice_dir: Path,
    silero_path: Path,
    loop: asyncio.AbstractEventLoop,
    progress: models.Progress,
) -> None:
    """First boot: fetch the speech models in the background, then come back with voice."""

    def run() -> None:
        try:
            models.fetch_missing(sensevoice_dir, silero_path, progress)
        except Exception:
            progress.failed = True
            LOGGER.exception("models: download failed; voice stays off until the next boot")
            return
        if supervised():
            LOGGER.info("models: ready; restarting so voice comes up")
            loop.call_soon_threadsafe(restart_soon)
        else:
            LOGGER.warning("models: ready; restart Jarvis to turn voice on")

    threading.Thread(target=run, name="jarvis-models", daemon=True).start()


def restart_soon() -> None:
    """``POST /inherent/restart``: TERM ourselves once the answer is out.

    launchd's KeepAlive or systemd's ``Restart=always`` (ADR 0227) brings us back.
    """
    asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
