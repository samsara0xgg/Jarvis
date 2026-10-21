"""The daemon's periodic observers: repositories, usage, screen time and the meaning index."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from jarvis.deployment import models
from jarvis.runtime import (
    JarvisRuntime,
    _observer_poll_interval_s,
    _observer_repo_paths,
    _positive_float,
    _timesink_db_path,
    _timesink_poll_interval_s,
)
from jarvis.state import embedding, meaning
from jarvis.surface.repo_observer import RepoObserver
from jarvis.surface.timesink_observer import TimesinkHead, TimesinkObserver
from jarvis.surface.usage_observer import KEYED_SERVICES, SERVICES, UsageObserver, latest_usage

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


async def _poll_one_repo(observer: RepoObserver, repo_path: str) -> None:
    """Poll ONE repo: collect off the loop thread, emit on it.

    The thread split is the pin, and it is the only reason this is a
    function rather than two lines inline. ``collect`` runs three ``git``
    subprocesses and touches no database, so it goes through
    :func:`asyncio.to_thread`; ``emit`` writes to ``runtime.conn``, which
    :func:`jarvis.state.event_log.open_event_log` opened with
    ``check_same_thread=True``, so it MUST stay on the loop thread. Move
    the ``emit`` inside the ``to_thread`` call and SQLite raises
    ``ProgrammingError`` on the first observed change — the failure would
    surface as "the observer sees nothing", one poll interval later.

    F5/F6 are already total inside ``collect`` (a hung, failing, or
    missing repo returns ``None``, never raises), so ``None`` here just
    means "skip this repo this cycle". The two ``except`` arms cover what
    ``collect`` does not own: an ``emit`` that fails on the log, and any
    unforeseen raise from the thread hop. Either way this repo is skipped
    and the next one in the cycle still runs.
    """
    try:
        poll = await asyncio.to_thread(observer.collect, repo_path)
    except Exception:  # one bad repo must not kill the observer task.
        LOGGER.exception("repo_observer: collect failed for %s; skipping cycle.", repo_path)
        return
    if poll is None:
        return
    try:
        observer.emit(poll)
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("repo_observer: emit failed for %s; baseline unchanged.", repo_path)


async def repo_observer_task(observer: RepoObserver, *, interval_s: float) -> None:
    """Background task: poll every watched repo every ``interval_s`` seconds.

    Polls FIRST, then sleeps — the mirror image of
    :func:`_supervisor_sweep_task`, and for the mirror-image reason. No
    bootstrap pass covers t=0 here, and the headline case of ADR-0009 D5
    is exactly the delta that accumulated while the daemon was down; a
    sleep-first loop would sit on the overnight commits for a full
    interval after every restart.

    Cancellation: re-raises :class:`asyncio.CancelledError` so
    :func:`serve_inherent`'s ``finally`` can await it cleanly.
    """
    LOGGER.info(
        "repo_observer started (%d repo(s), interval=%.0fs)",
        len(observer.repo_paths),
        interval_s,
    )
    try:
        while True:
            for repo_path in observer.repo_paths:
                await _poll_one_repo(observer, repo_path)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("repo_observer cancelled")
        raise


def start_repo_observer(runtime: JarvisRuntime) -> list[asyncio.Task[None]]:
    """Wire the ADR-0009 D5 repo observer. Returns its task, or none at all.

    Two ordered statements, and the order is the pin: baselines are
    recovered from the event log BEFORE the task can run its first poll.
    Skip the recovery and every repo looks like a first-ever observation
    on restart — one spurious ``repo.state_observed`` per repo, and the
    ``project.commit_seen`` history for the down window is lost for good
    (a first-ever observation deliberately walks no commits).

    Runs on the loop thread, synchronously, so the ``recover_baselines``
    read of ``runtime.conn`` is on the connection's owning thread.

    An empty ``observer.repos`` starts no task: the observer is opt-in
    perception, and a zero-repo poll loop would be pure wakeups.
    """
    repo_paths = _observer_repo_paths(runtime.config)
    if not repo_paths:
        LOGGER.info("repo_observer: observer.repos is empty; observer not started.")
        return []
    observer = RepoObserver(runtime.conn, repo_paths)
    baselines = observer.recover_baselines()
    LOGGER.info(
        "repo_observer: %d baseline(s) recovered from the event log for %d watched repo(s)",
        len(baselines),
        len(repo_paths),
    )
    return [
        asyncio.create_task(
            repo_observer_task(
                observer,
                interval_s=_observer_poll_interval_s(runtime.config),
            ),
            name="repo_observer",
        ),
    ]


# --- ADR-0018 usage observer ---------------------------------------------------

_FALLBACK_USAGE_POLL_INTERVAL_S: Final[float] = 300.0
_AGENTS_PORT: Final = 8016


def agents_port() -> int:
    """The agent host's port (ADR 0073): ``JARVIS_AGENTS_PORT``, 8016 by default."""
    raw = os.environ.get("JARVIS_AGENTS_PORT", "")
    return int(raw) if raw.isdigit() else _AGENTS_PORT


def claude_sessions_read(config: Mapping[str, Any]) -> bool:
    """``observer.claude_sessions.enabled``: may the Agents page read Claude Code's files."""
    block = config.get("observer")
    sessions = block.get("claude_sessions") if isinstance(block, Mapping) else None
    return isinstance(sessions, Mapping) and sessions.get("enabled") is True


def claude_sessions_served(runtime: JarvisRuntime) -> bool:
    """Whether the Agents page and the Claude hooks routes answer on this daemon.

    A brain has no sessions of its own and ignores its own setting (ADR 0225): it serves the page
    whenever a terminal can connect, and the terminal's setting decides what is read.
    """
    if runtime.role == "brain":
        return bool(runtime.listen_addresses)
    return claude_sessions_read(runtime.config)


def _usage_observer_block(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return ``observer.usage`` when ``enabled: true``; None means off."""
    block = config.get("observer")
    usage = block.get("usage") if isinstance(block, Mapping) else None
    if not isinstance(usage, Mapping) or usage.get("enabled") is not True:
        return None
    return usage


def usage_poll_interval_s(config: Mapping[str, Any]) -> float | None:
    """The seconds between usage polls when ``observer.usage`` is on; ``None`` when it is off."""
    usage = _usage_observer_block(config)
    if usage is None:
        return None
    return _positive_float(usage.get("poll_interval_s"), _FALLBACK_USAGE_POLL_INTERVAL_S)


def make_usage_observer(runtime: JarvisRuntime) -> UsageObserver | None:
    """Build the observer with baselines recovered on the loop thread."""
    usage = _usage_observer_block(runtime.config)
    if usage is None:
        LOGGER.info("usage_observer: observer.usage disabled; observer not started.")
        return None
    # A brain has no Claude Code or Codex login of its own: a terminal reports those two.
    observer = UsageObserver(
        runtime.conn,
        services=KEYED_SERVICES if runtime.role == "brain" else SERVICES,
    )
    baselines = observer.recover_baselines()
    LOGGER.info("usage_observer: %d baseline(s) recovered from the event log", len(baselines))
    return observer


async def _poll_usage_once(observer: UsageObserver) -> None:
    """Collect off the loop thread, emit on it — the same split as the repo observer."""
    try:
        snapshots = await asyncio.to_thread(observer.collect)
    except Exception:  # one bad cycle must not kill the observer task.
        LOGGER.exception("usage_observer: collect failed; skipping cycle.")
        return
    try:
        observer.emit([*snapshots, *observer.observe_recorded()])
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("usage_observer: emit failed; baselines unchanged.")


async def refresh_usage_now(
    observer: UsageObserver, conn: sqlite3.Connection
) -> dict[str, Any]:
    """``POST /inherent/usage/refresh``: one poll, then the read model."""
    await _poll_usage_once(observer)
    return latest_usage(conn)


async def usage_observer_task(observer: UsageObserver, *, interval_s: float) -> None:
    """Background task: poll every source every ``interval_s`` seconds, poll first."""
    LOGGER.info("usage_observer started (interval=%.0fs)", interval_s)
    try:
        while True:
            await _poll_usage_once(observer)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("usage_observer cancelled")
        raise


def start_usage_observer(
    observer: UsageObserver | None, config: Mapping[str, Any]
) -> list[asyncio.Task[None]]:
    """Start the periodic task for an observer :func:`make_usage_observer` built."""
    interval_s = usage_poll_interval_s(config)
    if observer is None or interval_s is None:
        return []
    return [
        asyncio.create_task(
            usage_observer_task(observer, interval_s=interval_s),
            name="usage_observer",
        ),
    ]


async def poll_timesink_once(
    observer: TimesinkObserver, on_checked: Callable[[TimesinkHead], None] | None = None
) -> None:
    """Read the head off the loop thread, emit on it — the usage observer's split.

    ``on_checked`` sees every read, changed or not: the work-state view's
    "checked" clock must move even when the head did not.
    """
    try:
        head = await asyncio.to_thread(observer.collect)
    except Exception:  # one bad cycle must not kill the observer task.
        LOGGER.exception("timesink_observer: collect failed; skipping cycle.")
        return
    if on_checked is not None:
        on_checked(head)
    try:
        observer.emit(head)
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("timesink_observer: emit failed; baseline unchanged.")


async def timesink_observer_task(
    observer: TimesinkObserver,
    *,
    interval_s: float,
    on_checked: Callable[[TimesinkHead], None] | None = None,
) -> None:
    """ADR 0023 background sync: poll first, then every ``interval_s``; never a model call."""
    LOGGER.info("timesink_observer started (interval=%.0fs)", interval_s)
    try:
        while True:
            await poll_timesink_once(observer, on_checked)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("timesink_observer cancelled")
        raise


def start_timesink_observer(runtime: JarvisRuntime) -> list[asyncio.Task[None]]:
    """Start the head poll when ``observer.timesink`` is enabled; baseline from the log."""
    path = _timesink_db_path(runtime.config)
    if path is None:
        LOGGER.info("timesink_observer: observer.timesink disabled; observer not started.")
        return []
    observer = TimesinkObserver(runtime.conn, path)
    observer.recover_baseline()
    return [
        asyncio.create_task(
            timesink_observer_task(
                observer,
                interval_s=_timesink_poll_interval_s(runtime.config),
                on_checked=None if runtime.work_state is None else runtime.work_state.note_checked,
            ),
            name="timesink_observer",
        ),
    ]


_MEANING_IDLE_S: Final = 300.0
_MEANING_BUSY_S: Final = 2.0


def _index_meaning(db_path: Path, directory: Path) -> int:
    """One small batch of ADR 0260's index; nothing to do while the model is not on disk."""
    embedder = embedding.shared(directory)
    return meaning.index_pending(db_path, embedder) if embedder.available else 0


async def _meaning_index_task(db_path: Path, directory: Path) -> None:
    """ADR 0260: embed new and changed text in small batches, off the loop; never fatal.

    Backfill and new rows take the same path: a batch, a short pause while more is left, a long
    one when caught up.
    """
    LOGGER.info("meaning_index started (%s)", directory)
    try:
        while True:
            left = 0
            try:
                left = await asyncio.to_thread(_index_meaning, db_path, directory)
            except Exception:
                LOGGER.exception("meaning_index: batch failed; trying again later")
            await asyncio.sleep(_MEANING_BUSY_S if left else _MEANING_IDLE_S)
    except asyncio.CancelledError:
        LOGGER.info("meaning_index cancelled")
        raise


def start_meaning_index(runtime: JarvisRuntime) -> list[asyncio.Task[None]]:
    """Start the index task when there is a memory store."""
    if runtime.memory is None:
        return []
    directory = models.default_meaning_dir(runtime.runtime_paths.root)
    return [
        asyncio.create_task(
            _meaning_index_task(runtime.memory.db_path, directory), name="meaning_index",
        ),
    ]
