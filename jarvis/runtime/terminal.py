"""ADR 0170: ``python -m jarvis terminal`` — a device's end of a brain's device-bound tools.

A terminal is a small client, not a daemon: no port, no event log of its own, no model key. It
connects outward to the brain, declares the tools this machine can run, and runs the calls the
brain sends with the very handlers the one-machine daemon uses. Each call gets a throwaway
in-memory log (the handlers write their own observation to one), which is dropped with it.

It also runs the observers that only read this machine's files and apps (the repos in
``observer.repos`` and TimeSink): the same observer code the daemon runs, with its events sent
to the brain's log instead of a log here. The usage observer needs provider keys and the Claude
sessions page is a live read, so those stay with the brain (ADR 0170).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.execution.tools import (
    TERMINAL_TOOL_NAMES,
    ActionLifecycle,
    ToolRegistry,
    build_default_registry,
    make_screen_capture,
)
from jarvis.runtime import (
    _DEFAULT_CONFIG_FILENAME,
    RuntimeBootstrapError,
    _install_open_path,
    _load_full_config,
    _locate_repo_root,
    _observer_poll_interval_s,
    _observer_repo_paths,
    _obsidian_vault_root,
    _screen_tools_config,
    _timesink_db_path,
    _timesink_poll_interval_s,
)
from jarvis.runtime.inherent_loop import _repo_observer_task, _timesink_observer_task
from jarvis.runtime.settings import apply_settings
from jarvis.shared import ActionRequest
from jarvis.state import device_reads, timesink
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_report import git_show, local_commits
from jarvis.state.daily_store import commit_exists
from jarvis.state.event_log import EventLogError, emit_event, open_event_log
from jarvis.surface.repo_observer import RepoObserver
from jarvis.surface.terminal_events import EventOutbox
from jarvis.surface.terminal_link import (
    MAX_FRAME_CHARS,
    Execute,
    TerminalRefusedError,
    run_terminal_client,
)
from jarvis.surface.timesink_observer import TimesinkObserver, collect

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger("jarvis.runtime.terminal")

RESOLVE_FILE = "resolve_file"
"""Not a menu tool: the brain asks the terminal to turn a spoken file name into a path here."""


@dataclass(frozen=True)
class _ScratchPaths:
    """What the handlers read from ``RuntimePaths``: nowhere a terminal keeps anything."""

    event_log: Path
    artifacts_root: Path


def _declared(registry: ToolRegistry) -> frozenset[str]:
    """The tools this machine can run: the device tools it holds that a caller may reach.

    Beside them the reads of its own TimeSink store and repositories, which are not menu tools.
    """
    held = {tool.name for tool in registry.get_definitions() if tool.allowed_callers}
    return frozenset(held & TERMINAL_TOOL_NAMES) | {RESOLVE_FILE} | device_reads.DEVICE_READS


_MAX_READ_CHARS = MAX_FRAME_CHARS - 4096
"""The most a read may answer: one frame, less its envelope."""


def _when(args: Mapping[str, Any], key: str) -> datetime:
    return datetime.fromisoformat(str(args[key]))


def _timesink_read(fn: str, args: Mapping[str, Any], store: Path | None) -> Any:  # noqa: ANN401, PLR0911 — one reader's JSON; one return per reader.
    """One TimeSink reader of ``jarvis.state.timesink``, run on this machine's own store."""
    if fn in {"read_capture", "read_span"}:
        reader = timesink.read_capture if fn == "read_capture" else timesink.read_span
        return reader(store, str(args["reference"]))
    if fn == "head":
        return asdict(collect(store))
    queries = {
        "query_spans": timesink.query_spans,
        "query_captures": timesink.query_captures,
        "query_state": timesink.query_state,
    }
    if fn not in {"open", "capture_texts", "capture_matches", "search_captures", *queries}:
        message = f"this terminal has no TimeSink read {fn!r}"
        raise DailyError(message, "unknown_read")
    with timesink.snapshot(store) as snap:
        if fn == "open":
            return None if snap is None else snap.identity
        if fn == "capture_texts":
            return {} if snap is None else timesink.capture_texts(snap, args["ids"])
        if fn == "capture_matches":
            return [] if snap is None else timesink.capture_matches(
                snap, list(args["ids"]), list(args["terms"]),
            )
        start, end = _when(args, "start"), _when(args, "end")
        if fn == "search_captures":
            return timesink.search_captures(
                snap, start, end, list(args["terms"]), limit=int(args["limit"]),
            )
        return queries[fn](snap, start, end, watermark=args.get("watermark"))


def _git_read(fn: str, args: Mapping[str, Any], repos: tuple[str, ...]) -> Any:  # noqa: ANN401 — one reader's JSON.
    """One git reader of ``jarvis.state.daily_report``, run on this machine's own repositories."""
    if fn == "repos":
        return list(repos)
    if fn == "local_commits":
        found, unreadable = local_commits(repos, _when(args, "since"), _when(args, "until"))
        return {"found": found, "unreadable": unreadable}
    if fn in {"show", "exists"}:
        repo = str(args["repo"])
        if repo not in repos:  # the brain names where to look; only a watched repository is read
            message = f"{repo} is not a repository this device watches"
            raise DailyError(message, "not_watched")
        sha = str(args["sha"])
        return git_show(repo, sha) if fn == "show" else commit_exists(repo, sha)
    message = f"this terminal has no git read {fn!r}"
    raise DailyError(message, "unknown_read")


def _read_device(
    op: str, arguments: Mapping[str, Any], store: Path | None, repos: tuple[str, ...],
) -> dict[str, Any]:
    """Answer a brain's read of this machine's TimeSink or git: the JSON, or the refusal."""
    raw = arguments.get("args")
    args = raw if isinstance(raw, dict) else {}
    fn = str(arguments.get("fn", ""))
    try:
        result = (
            _timesink_read(fn, args, store)
            if op == device_reads.TIMESINK_READ
            else _git_read(fn, args, repos)
        )
    except DailyError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "code": "bad_request", "message": f"{fn}: {type(exc).__name__}"}
    output = {"result": result}
    if len(json.dumps(output, ensure_ascii=False)) > _MAX_READ_CHARS:
        return {"ok": False, "code": "result_too_large",
                "message": f"{fn} has more to send than one reply holds"}
    return {"ok": True, "output": output}


def make_executor(
    registry: ToolRegistry, *, timesink_store: Path | None = None, repos: tuple[str, ...] = (),
) -> Execute:
    """The runner a terminal hands its link: one call in, ``ok`` + ``output`` or a failure out.

    Only declared tools run. The brain has already gated and, where it must, confirmed the
    call; nothing here asks again. ``timesink_store`` and ``repos`` are what this machine's
    config says it has, which the brain's reads of them (``timesink_read``, ``git_read``) use.
    """
    declared = _declared(registry)
    definitions = {tool.name: tool for tool in registry.get_definitions()}

    def execute(
        tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        reply = run(tool, arguments, target_entity_ref)
        LOGGER.info("ran %s: %s", tool, "ok" if reply["ok"] else reply["code"])
        return reply

    def run(
        tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        if tool not in declared:
            return {"ok": False, "code": "unknown_tool", "message": f"this terminal has no {tool}"}
        if tool in device_reads.DEVICE_READS:
            return _read_device(tool, arguments, timesink_store, repos)
        conn = open_event_log(Path(":memory:"))  # opened here: a log belongs to its thread
        try:
            if tool == RESOLVE_FILE:
                found = resolve_file_entity(str(arguments.get("query", "")), "file", conn)
                if found is None:
                    return {"ok": False, "code": "target_not_found", "message": "no file matched"}
                return {"ok": True, "output": {"path": str(found.path), "source": found.source}}
            definition = definitions[tool]
            action_id = f"terminal-{uuid.uuid4().hex}"
            lifecycle = ActionLifecycle()
            lifecycle.register(action_id)
            lifecycle.transition(action_id, "authorized")
            request = ActionRequest(
                action_id=action_id,
                tool_name=tool,
                target_entity_ref=target_entity_ref,
                caller_principal=min(definition.allowed_callers, key=lambda c: c.value),
                risk_level=definition.risk_level,
                arguments=dict(arguments),
                authorization_lease=None,
                run_id=None,
                turn_id=None,
            )
            scratch = _ScratchPaths(Path(":memory:"), Path(tempfile.gettempdir()))
            result = registry.dispatch(request, conn, scratch, lifecycle).slots[0]
        finally:
            conn.close()
        if result.error is None:
            return {"ok": True, "output": dict(result.payload)}
        try:
            message = str(json.loads(result.tool_output or "")["error"])
        except (ValueError, KeyError, TypeError):
            message = result.error
        return {"ok": False, "code": result.error, "message": message}

    return execute


@dataclass(frozen=True)
class _Watched:
    """What this machine's config asks the terminal to observe."""

    repos: tuple[str, ...]
    repo_interval_s: float
    timesink: Path | None
    timesink_interval_s: float


async def _observe(outbox: EventOutbox, watched: _Watched) -> None:
    """Run the observers until cancelled, their events going to ``outbox``.

    They wait for the first connection to the brain: their change-baselines are what the
    brain's log last heard, so they cannot start without it. After that they poll whether or
    not the link is up.
    """
    try:
        scratch = open_event_log(Path(":memory:"))  # only seeds the baselines; nothing is kept
        for row in await outbox.baseline():
            try:
                emit_event(scratch, type=row["event_type"], payload=row["payload"])
            except (EventLogError, KeyError, TypeError):
                LOGGER.warning("terminal: ignored an unreadable baseline row from the brain")
        tasks: list[asyncio.Task[None]] = []
        if watched.repos:
            repo_observer = RepoObserver(scratch, watched.repos, emit_event=outbox.emit_event)
            repo_observer.recover_baselines()
            tasks.append(asyncio.create_task(
                _repo_observer_task(repo_observer, interval_s=watched.repo_interval_s),
            ))
        if watched.timesink is not None:
            timesink = TimesinkObserver(scratch, watched.timesink, emit_event=outbox.emit_event)
            timesink.recover_baseline()
            tasks.append(asyncio.create_task(
                _timesink_observer_task(timesink, interval_s=watched.timesink_interval_s),
            ))
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("terminal: the observers stopped")


async def _run(
    base_url: str, token: str, *, tools: frozenset[str], execute: Execute,
    watched: _Watched | None,
) -> None:
    """The link, and beside it the observers when there is anything to observe."""
    outbox = None if watched is None else EventOutbox()
    observing = None if outbox is None or watched is None else asyncio.create_task(
        _observe(outbox, watched),
    )
    try:
        await run_terminal_client(base_url, token, tools=tools, execute=execute, events=outbox)
    finally:
        if observing is not None:
            observing.cancel()
            await asyncio.wait({observing})


def run_terminal(
    base_url: str,
    token: str,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    observers: bool = True,
) -> int:
    """Hold this device's link to the brain until interrupted; the exit code of the command.

    Reads only this machine's own config (the shipped YAML and the runtime root's
    ``settings.yaml``); it opens no database, no env file and no key. With ``observers`` the
    repos and TimeSink that config turns on are observed here and reported to the brain.
    """
    if config_path is None:
        config_path = _locate_repo_root(Path(__file__).parent) / _DEFAULT_CONFIG_FILENAME
    try:
        config = apply_settings(
            _load_full_config(config_path, runtime_root / "settings.yaml"), runtime_root,
        )
        _install_open_path(config)
        configured_repos = _observer_repo_paths(config)
        configured_store = _timesink_db_path(config)
        repos = configured_repos if observers else ()
        store = configured_store if observers else None
    except (RuntimeBootstrapError, ValueError) as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    _vision_preset, max_width_px = _screen_tools_config(config)
    registry = build_default_registry(obsidian_vault_root=_obsidian_vault_root(config))
    registry.register(make_screen_capture(max_width_px))
    tools = _declared(registry)
    LOGGER.info("this terminal runs %s", sorted(tools))
    watched = (
        _Watched(
            repos, _observer_poll_interval_s(config), store, _timesink_poll_interval_s(config),
        )
        if repos or store is not None
        else None
    )
    if watched is not None:
        LOGGER.info("this terminal observes %d repo(s)%s", len(repos),
                    "" if store is None else " and TimeSink")
    try:
        execute = make_executor(
            registry, timesink_store=configured_store, repos=configured_repos,
        )
        asyncio.run(_run(base_url, token, tools=tools, execute=execute, watched=watched))
    except TerminalRefusedError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    except KeyboardInterrupt:
        LOGGER.info("terminal stopped")
    return 0
