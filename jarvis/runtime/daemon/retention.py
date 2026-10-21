"""What the daemon keeps and for how long: the data sweep, pictures, and the data routes."""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
from typing import TYPE_CHECKING

from jarvis.deployment import data
from jarvis.execution.tools import SCREEN_ARTIFACTS_DIRNAME
from jarvis.runtime import JarvisRuntime, picture_files
from jarvis.runtime.daemon.restart import restart_soon
from jarvis.runtime.settings import SETTINGS_FILE
from jarvis.state import picture_notes
from jarvis.state.memory_db import HISTORY_PICTURES, recent_image_ids, retention_days

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.decision.llm_session import LLMSessionFactory
    from jarvis.state.attachments import AttachmentRef

LOGGER = logging.getLogger(__name__)


_DATA_SWEEP_INTERVAL_S = 3600.0
_ATTACHMENT_DAYS = 30  # ADR 0211: unless ``attachments.retention_days`` says otherwise


def media_dirs(runtime: JarvisRuntime) -> dict[Path, int | None]:
    """ADR 0067: the recordings, screenshots and phone attachments, each with its days to keep."""
    tools = runtime.config.get("tools") or {}
    screen = tools.get("screen") if isinstance(tools, dict) else None
    days = retention_days(screen.get("retention_days")) if isinstance(screen, dict) else None
    dirs = {runtime.runtime_paths.artifacts_root / SCREEN_ARTIFACTS_DIRNAME: days}
    sent = runtime.config.get("attachments")
    if runtime.attachments is not None:
        dirs[runtime.attachments.directory] = retention_days(
            sent.get("retention_days", _ATTACHMENT_DAYS) if isinstance(sent, dict)
            else _ATTACHMENT_DAYS,
        )
    if runtime.memory is not None:
        dirs[runtime.memory.audio_dir] = runtime.memory.audio_retention_days
    return dirs


def link_earlier_pictures(runtime: JarvisRuntime) -> int:
    """ADR 0255: link his earlier records to the files still kept (see ``picture_files``)."""
    if runtime.attachments is None or runtime.memory is None:
        return 0
    sent = runtime.config.get("attachments")
    days = retention_days(
        sent.get("retention_days", _ATTACHMENT_DAYS) if isinstance(sent, dict)
        else _ATTACHMENT_DAYS,
    )
    linked = picture_files.link_kept_pictures(
        runtime.memory.db_path, runtime.runtime_paths.event_log, runtime.attachments, days=days,
    )
    if runtime.picture_files is not None and (client := _upload_client(runtime)) is not None:
        for attachment_id in recent_image_ids(runtime.memory.db_path, HISTORY_PICTURES):
            runtime.picture_files.start(client, attachment_id)  # the backlog, newest first
    return linked


@functools.cache
def _default_anthropic(factory: LLMSessionFactory) -> LLMClient | None:
    return (
        factory.create(factory.snapshot(factory.default_preset), response_id="pictures")
        if factory.snapshot(factory.default_preset).provider == "anthropic" else None
    )


def _upload_client(runtime: JarvisRuntime) -> LLMClient | None:
    """The client that uploads pictures: on the default preset, when that is Anthropic's."""
    return _default_anthropic(runtime.llm_session_factory)


def start_upload(runtime: JarvisRuntime, ref: AttachmentRef) -> None:
    """ADR 0255: a picture was just saved; upload it now so the next turn finds its id."""
    client = _upload_client(runtime)
    if runtime.picture_files is not None and client is not None:
        runtime.picture_files.start(client, ref.id)


def _files_client(runtime: JarvisRuntime) -> LLMClient | None:
    """A client on an Anthropic preset, whatever preset is active, or ``None`` without one."""
    if runtime.llm_client.provider == "anthropic":
        return runtime.llm_client
    factory = runtime.llm_session_factory
    for name, spec in runtime.llm_client.get_presets().items():
        if str(spec.get("provider", "")).lower() == "anthropic":
            return factory.create(factory.snapshot(name), response_id="pictures")
    return None


def release_uploaded_pictures(runtime: JarvisRuntime, *, everything: bool = False) -> int:
    """ADR 0255: delete the uploaded copies of pictures whose file is gone (or all of them)."""
    if runtime.picture_files is None:
        return 0
    try:
        return runtime.picture_files.release(_files_client(runtime), everything=everything)
    except Exception:
        LOGGER.exception("pictures: could not delete uploaded copies")
        return 0


def sweep_data(dirs: dict[Path, int | None], logs: Path) -> None:
    """Delete what has passed its days and cut each log past its size (ADR 0067)."""
    for directory, days in dirs.items():
        if gone := data.delete_older_than(directory, days):
            LOGGER.info("retention: deleted %d files older than %s days from %s",
                        gone, days, directory.name)
    if cut := data.rotate_logs(logs):
        LOGGER.info("retention: started %d new log files", cut)


async def data_sweep_task(  # noqa: PLR0913 — the brain's two hooks, a terminal's gate, the wait.
    dirs: dict[Path, int | None], logs: Path, link_pictures: Callable[[], object] | None = None,
    release_pictures: Callable[[], object] | None = None, *,
    swept_elsewhere: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """At boot and then every hour; a failed pass waits for the next one.

    ``link_pictures`` runs once, first (ADR 0255): the files must be there to be linked.
    ``release_pictures`` runs after each sweep: the copies of the pictures it removed go too.
    ``swept_elsewhere`` (ADR 0334) is a terminal's: while it says a daemon holds this root, the
    daemon sweeps it and the pass is skipped; it is asked again at every pass.
    """
    if link_pictures is not None:
        try:
            await asyncio.to_thread(link_pictures)
        except (OSError, sqlite3.Error):
            LOGGER.exception("pictures: could not link earlier records to their files")
    while True:
        try:
            if swept_elsewhere is None or not await asyncio.to_thread(swept_elsewhere):
                await asyncio.to_thread(sweep_data, dirs, logs)
                if release_pictures is not None:
                    await asyncio.to_thread(release_pictures)
        except OSError:
            LOGGER.exception("retention: sweep failed; next try in an hour")
        await sleep(_DATA_SWEEP_INTERVAL_S)


def clear_media(runtime: JarvisRuntime) -> int:
    """``POST /inherent/data/clear-recordings``: the files and the pictures' notes go (ADR 0223)."""
    gone = data.clear_files(list(media_dirs(runtime)))
    if runtime.memory is not None:
        picture_notes.delete_all(runtime.memory.db_path)
    release_uploaded_pictures(runtime)
    return gone


def export_data(runtime: JarvisRuntime) -> Path:
    """``GET /inherent/data/export``: conversations, event log, settings, recordings, screenshots.

    Keys, sign-ins, the browser profile and the speech models stay out.
    """
    paths = runtime.runtime_paths
    memory = [runtime.memory.db_path] if runtime.memory is not None else []
    return data.export_zip(
        paths.root,
        [*memory, paths.event_log, paths.settings, paths.root / SETTINGS_FILE,
         *media_dirs(runtime)],
    )


def erase_data(runtime: JarvisRuntime) -> None:
    """``POST /inherent/data/erase``: everything goes at the next boot, which starts now.

    The uploaded copies of his pictures go first: the files that name them go at that boot.
    """
    release_uploaded_pictures(runtime, everything=True)
    data.request_erase(runtime.runtime_paths.root)
    restart_soon()
