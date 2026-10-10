"""ADR 0170: ``python -m jarvis terminal`` — a device's end of a brain's device-bound tools.

A terminal is a small client, not a daemon: no port, no event log of the owner's, no model key.
It connects outward to the brain, declares the tools this machine can run, and runs the calls the
brain sends with the very handlers the one-machine daemon uses. Each call gets a throwaway
in-memory log (the handlers write their own observation to one), which is dropped with it.

It holds this Mac's night run (ADR 0192): the brain has no Mac to keep awake, so the run, its
``night.*`` events and the start and end tools the brain's model calls all live here.

It also runs the observers that only read this machine's files and apps (the repos in
``observer.repos`` and TimeSink): the same observer code the daemon runs, with its events sent
to the brain's log instead of a log here. The usage observer needs provider keys and the Claude
sessions page is a live read, so those stay with the brain (ADR 0170).

With ``--voice`` it also speaks and listens (ADR 0172): the brain streams the answer's rows into
a scratch journal here, the same media actor as on one machine plays them, its provider
sessions run on the brain, and the playback rows it writes go back to the brain as events. The
same capture session as a daemon's (wake, VAD, local ASR, barge-in) runs here when this
machine's own config turns ``realtime.single_audio_ingress`` on; each callback that touches
brain state is a call over the link, and a final utterance goes to the brain as one
``utterance.received``. A microphone that cannot be opened leaves a speaking-only terminal.

With ``--serve-ui`` it also listens on 127.0.0.1 and serves this device's UI, the companion's
interface, forwarding what is the brain's to the brain (ADR 0183). That is the one thing here
that listens; the link itself is still outbound only.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import socket
import sys
import tempfile
import uuid
from collections.abc import Generator, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import uvicorn

from jarvis.deployment.launchd import spawned_by_terminal_agent
from jarvis.deployment.models import default_sensevoice_dir, default_silero_vad_path
from jarvis.deployment.night_power import MacPower
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
    _audio_devices,
    _default_audio_device,
    _install_open_path,
    _load_full_config,
    _locate_repo_root,
    _observer_poll_interval_s,
    _observer_repo_paths,
    _obsidian_vault_root,
    _realtime_model_path,
    _screen_tools_config,
    _timesink_db_path,
    _timesink_poll_interval_s,
    _wave1_feature_flags,
    _wave4_response_flags,
    _work_state_timezone,
)
from jarvis.runtime.dictation import Dictation, build_dictation, load_vocab
from jarvis.runtime.inherent_loop import (
    _agents_port,
    _build_echo_canceller,
    _build_tts_pipeline,
    _build_voice_pipeline,
    _claude_sessions_read,
    _ListenPorts,
    _repo_observer_task,
    _restart_soon,
    _single_ingress_activation,
    _spawn_single_ingress_session,
    _timesink_observer_task,
    _tts_watcher,
    _usage_observer_task,
    _usage_poll_interval_s,
    _voice_knobs,
    _voice_models_preflight,
)
from jarvis.runtime.night_run import NightRun, night_settings
from jarvis.runtime.night_watch import NightWatch
from jarvis.runtime.settings import DEVICE_KEYS, Settings, apply_settings
from jarvis.shared import ActionRequest
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl
from jarvis.state import device_reads, job_time, ledger, timesink, timesink_moment
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_report import git_show, local_commits, resolve_zone
from jarvis.state.daily_store import commit_exists
from jarvis.state.event_log import EventLogError, emit_event, open_event_log
from jarvis.state.memory_db import MemorySettings
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.projects import Project, gather, parse_catalog, window_to_wire
from jarvis.surface.claude_sessions import ClaudeSessions
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.repo_observer import RepoObserver
from jarvis.surface.terminal_events import EventOutbox
from jarvis.surface.terminal_link import (
    MAX_FRAME_CHARS,
    Execute,
    TerminalRefusedError,
    run_terminal_client,
)
from jarvis.surface.terminal_listen import (
    LinkedControls,
    LinkedTurn,
    RemotePolish,
    with_voice_commands,
)
from jarvis.surface.terminal_speaker import (
    Journal,
    RemoteTTSProvider,
    VoiceLink,
    forward_playback,
)
from jarvis.surface.terminal_ui import Brain, Device, UiBroadcaster, create_ui_app
from jarvis.surface.timesink_observer import TimesinkObserver, collect
from jarvis.surface.usage_observer import DEVICE_SERVICES, UsageObserver
from jarvis.surface.voice_controls import VoiceControls
from jarvis.surface.voice_ducking import SystemAudioDucker
from jarvis.surface.voice_media import StreamingTTSPipeline

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.shared.realtime import Wave1FeatureFlags, Wave4ResponseFlags
    from jarvis.surface import voice_aec, voice_session

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

    Beside them the reads of its own TimeSink store, repositories and Claude Code sessions, which
    are not menu tools.
    """
    held = {tool.name for tool in registry.get_definitions() if tool.allowed_callers}
    return frozenset(held & TERMINAL_TOOL_NAMES) | {RESOLVE_FILE} | device_reads.TERMINAL_READS


_MAX_READ_CHARS = MAX_FRAME_CHARS - 4096
"""The most a read may answer: one frame, less its envelope."""


def _when(args: Mapping[str, Any], key: str) -> datetime:
    return datetime.fromisoformat(str(args[key]))


def _derived_read(fn: str, args: Mapping[str, Any], store: Path | None) -> Any:  # noqa: ANN401 — one reader's JSON.
    """The reads that are a whole view of the day, not a query: each runs here, whole."""
    if fn == "ledger_screen":
        return ledger.terminal_screen(store, _when(args, "lo"), _when(args, "hi"))
    if fn == "ledger_submissions":
        return ledger.terminal_submissions(store, _when(args, "as_of"))
    if fn == "moment_facts":
        known = job_time.known_from_wire(args["known"])
        return timesink_moment.moment_facts(store, _when(args, "now"), known)
    with timesink.snapshot(store) as snap:
        if fn == "job_time":
            known = job_time.known_from_wire(args["known"])
            return job_time.job_time(snap, known, _when(args, "now"), int(args["days"]))
        if snap is None:
            message = "TimeSink is not readable on this machine"
            raise DailyError(message, "source_unavailable")
        zone = ZoneInfo(str(args["zone"]))
        return window_to_wire(gather(snap, zone, _when(args, "now")))


def _timesink_read(fn: str, args: Mapping[str, Any], store: Path | None) -> Any:  # noqa: ANN401, PLR0911 — one reader's JSON; one return per reader.
    """One TimeSink reader of ``jarvis.state``, run on this machine's own store."""
    if fn in {"moment_facts", "job_time", "project_window", "ledger_screen", "ledger_submissions"}:
        return _derived_read(fn, args, store)
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


def _git_read(
    fn: str, args: Mapping[str, Any], repos: tuple[str, ...], projects: tuple[Project, ...] = (),
) -> Any:  # noqa: ANN401 — one reader's JSON.
    """One git reader of ``jarvis.state.daily_report``, run on this machine's own repositories."""
    if fn == "repos":
        return list(repos)
    if fn == "local_commits":
        found, unreadable = local_commits(repos, _when(args, "since"), _when(args, "until"))
        return {"found": found, "unreadable": unreadable}
    if fn == "project_commits":
        # The brain names a project, never a path: the repositories are this machine's own config.
        project = next((p for p in projects if p.id == str(args["project"])), None)
        if project is None:
            message = f"{args['project']} is not a project this device knows"
            raise DailyError(message, "not_watched")
        found, unreadable = local_commits(project.repos, _when(args, "since"), _when(args, "until"))
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


def _claude_read(
    fn: str, args: Mapping[str, Any], claude: ClaudeSessions | None,
) -> Any:  # noqa: ANN401 — one read's JSON.
    """One read of the Agents page, or its reply, run on this machine's own Claude Code state."""
    if claude is None:
        message = (
            "reading Claude Code's files is off on this device (observer.claude_sessions.enabled)"
        )
        raise DailyError(message, "disabled")
    try:
        if fn == "board":
            return claude.raw()
        if fn == "conversation":
            return claude.conversation(str(args["session_id"]))
        if fn == "reply":
            claude.reply(str(args["session_id"]), str(args["text"]))
            return None
    except LookupError as exc:
        message = f"no such session: {exc}"
        raise DailyError(message, "not_found") from exc
    except (OSError, RuntimeError) as exc:
        message = str(exc)[:200] or type(exc).__name__
        raise DailyError(message, "failed") from exc
    message = f"this terminal has no Claude Code read {fn!r}"
    raise DailyError(message, "unknown_read")


def _read_device(  # noqa: PLR0913 — what this machine has, for the brain's reads of it.
    op: str, arguments: Mapping[str, Any], store: Path | None, repos: tuple[str, ...],
    projects: tuple[Project, ...] = (), claude: ClaudeSessions | None = None,
) -> dict[str, Any]:
    """Answer a brain's read of this machine's TimeSink, git or Claude Code: JSON or the refusal."""
    raw = arguments.get("args")
    args = raw if isinstance(raw, dict) else {}
    fn = str(arguments.get("fn", ""))
    try:
        if op == device_reads.TIMESINK_READ:
            result = _timesink_read(fn, args, store)
        elif op == device_reads.CLAUDE_READ:
            result = _claude_read(fn, args, claude)
        else:
            result = _git_read(fn, args, repos, projects)
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
    projects: tuple[Project, ...] = (), claude: ClaudeSessions | None = None,
) -> Execute:
    """The runner a terminal hands its link: one call in, ``ok`` + ``output`` or a failure out.

    Only declared tools run. The brain has already gated and, where it must, confirmed the
    call; nothing here asks again. ``timesink_store`` and ``repos`` are what this machine's
    config says it has, which the brain's reads of them (``timesink_read``, ``git_read``) use;
    ``projects`` is its own ``projects`` catalog, whose repositories the brain's dashboard reads;
    ``claude`` is its Claude Code sessions, or ``None`` when its config does not allow reading
    them.
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
        if tool in device_reads.TERMINAL_READS:
            return _read_device(tool, arguments, timesink_store, repos, projects, claude)
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
    usage_interval_s: float | None = None
    """Seconds between polls of the Claude Code and Codex logins; ``None``: not observed."""


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
        if watched.usage_interval_s is not None:
            usage = UsageObserver(
                scratch, services=DEVICE_SERVICES, emit_event=outbox.emit_event,
            )
            usage.recover_baselines()
            tasks.append(asyncio.create_task(
                _usage_observer_task(usage, interval_s=watched.usage_interval_s),
            ))
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("terminal: the observers stopped")


JOURNAL = Path("terminal") / "voice-journal.db"
"""Where a voice terminal's scratch journal lives, under its runtime root."""
NIGHT_LOG = Path("terminal") / "night-events.db"
"""Where this Mac's night run writes its ``night.*`` events, under the runtime root."""
_SPEECH_CLOSE_S = 5.0
_LISTEN_CLOSE_S = 15.0


@dataclass(frozen=True)
class _SeatPaths:
    event_log: Path  # the journal: the only log a terminal has
    root: Path


@dataclass(frozen=True)
class _Seat:
    """What the voice builders read from their host; a terminal's is its journal and config."""

    config: Mapping[str, Any]
    wave1_features: Wave1FeatureFlags
    response_flags: Wave4ResponseFlags
    runtime_paths: _SeatPaths
    conn: sqlite3.Connection
    memory: MemorySettings | None = None
    voice_settings: None = None
    voice_cues: None = None


@dataclass(frozen=True)
class _Speaking:
    """A terminal that was asked to speak: the config its media actor is built from."""

    config: Mapping[str, Any]
    runtime_root: Path
    config_dir: Path | None = None  # where relative model paths in the config resolve from
    broadcaster: InherentBroadcaster | None = None  # the companion's sockets, with ``--serve-ui``
    controls: VoiceControls | None = None  # this device's mic and speech switches, likewise


class _QuietBroadcaster(InherentBroadcaster):
    """The broadcaster of a terminal, which has no surface of its own yet.

    The session's partial captions, listening and transcribing faces and capability changes are
    logged at debug and dropped: without ``--serve-ui`` no surface connects here. With it,
    :class:`~jarvis.surface.terminal_ui.UiBroadcaster` takes its place.
    """

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        """Drop one voice phase."""
        LOGGER.debug("voice phase %s turn=%s %s", phase, turn_id, sorted(payload))

    def broadcast_voice_capability_sync(  # noqa: PLR0913 - explicit wire schema
        self,
        *,
        version: int,
        state: str,
        stream_epoch: int | None,
        reason: str,
        wake_available: bool,
        local_capture_available: bool,
        ptt_upload_available: bool,
        text_available: bool,
        route_kind: str = "unknown",
        allowed_barge_mode: str = "ptt",
    ) -> None:
        """Drop one capability snapshot."""
        del (
            stream_epoch, wake_available, local_capture_available, ptt_upload_available,
            text_available, route_kind, allowed_barge_mode,
        )
        LOGGER.debug("voice capability v%s %s (%s)", version, state, reason)

    def broadcast_op_sync(self, op: str, **payload: object) -> None:
        """Drop one op."""
        LOGGER.debug("op %s %s", op, sorted(payload))


@dataclass
class _Speech:
    """What a terminal that was asked to speak holds; empty when it is mute."""

    link: VoiceLink | None = None
    pipeline: StreamingTTSPipeline | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    seat: _Seat | None = None
    broadcaster: InherentBroadcaster | None = None
    canceller: voice_aec.EchoCanceller | None = None
    listening: asyncio.Task[_Listening | None] | None = None
    ducker: SystemAudioDucker | None = None
    """The one that mutes the system while a line plays; the night run waits for it."""


@dataclass
class _Listening:
    """A running capture session, and what keeps it and the brain in step."""

    session: voice_session.DuplexVoiceSession
    controls: LinkedControls
    dictation: Dictation | None = None  # recording on this microphone, for the UI's dictation


def _listening_requested(config: Mapping[str, Any]) -> bool:
    """Whether this machine's own config turns the capture session on."""
    realtime = config.get("realtime")
    ingress = realtime.get("single_audio_ingress") if isinstance(realtime, Mapping) else None
    return isinstance(ingress, Mapping) and ingress.get("enabled") is True


def _start_speech(speaking: _Speaking, outbox: EventOutbox) -> _Speech:
    """Build the media actor over a fresh journal, and the tasks that feed and report it.

    No link when the actor did not start: a mute terminal must not tell the brain it speaks.
    When the config asks for listening, the echo canceller is built first and handed to the
    player as the far end of its reference: it can only cancel what the player plays.

    Runs on the loop that carries the link: the journal belongs to that thread, and the
    actor's own log connection is opened on its thread by the pipeline.
    """
    journal = Journal(speaking.runtime_root / JOURNAL)
    link = VoiceLink(journal)
    broadcaster = speaking.broadcaster or _QuietBroadcaster()
    broadcaster.attach_loop(asyncio.get_running_loop())
    seat = _Seat(
        speaking.config,
        _wave1_feature_flags(speaking.config),
        _wave4_response_flags(speaking.config),
        _SeatPaths(journal.path, speaking.runtime_root),
        journal.conn,
        MemorySettings.from_config(
            speaking.config.get("memory"), runtime_root=speaking.runtime_root,
        ),
    )
    canceller = None
    if _listening_requested(speaking.config):
        try:
            canceller = _build_echo_canceller(seat)
        except Exception:
            LOGGER.exception("echo cancellation could not start; this terminal listens without it")
    ducker = SystemAudioDucker()
    pipeline = _build_tts_pipeline(
        seat, broadcaster, ducker=ducker, voice=_voice_knobs(speaking.config),
        echo_canceller=canceller, remote=RemoteTTSProvider(link),
        network_lost_dir=speaking.runtime_root / "terminal",
    )
    if not isinstance(pipeline, StreamingTTSPipeline):
        LOGGER.warning("voice is on but the media actor did not start; this terminal is mute")
        return _Speech()
    if speaking.controls is not None:
        # ADR-0015 D2, as a daemon binds it: speech mute is the player's output gain.
        speaking.controls.on_speech_muted = lambda muted: pipeline.set_output_gain(
            0.0 if muted else 1.0,
        )
    watcher = _tts_watcher(conn=journal.conn, pipeline=pipeline, broadcaster=broadcaster)
    return _Speech(
        link, pipeline,
        [asyncio.create_task(watcher), asyncio.create_task(forward_playback(journal.path, outbox))],
        seat, broadcaster, canceller, ducker=ducker,
    )


async def _start_listening(speaking: _Speaking, speech: _Speech) -> _Listening | None:
    """Run the capture session of this terminal, or say why this terminal only speaks.

    One attempt, no retry: a microphone another process holds stays held, and trying again in
    a loop would only fight it. The words the owner says reach the brain as utterances and
    every callback that touches brain state is a call over the link (ADR 0172). Models load
    and the device opens on a worker thread, so the link comes up meanwhile.
    """
    link, pipeline, seat = speech.link, speech.pipeline, speech.seat
    if link is None or pipeline is None or seat is None or speech.broadcaster is None:
        return None
    config = speaking.config
    if not _single_ingress_activation(seat, tts=pipeline).requested:
        LOGGER.info("listening is off in this terminal's config "
                    "(realtime.single_audio_ingress.enabled); it speaks only")
        return None
    config_dir = speaking.config_dir or speaking.runtime_root
    sensevoice_dir = _realtime_model_path(
        config, key="sensevoice_dir", config_dir=config_dir,
        fallback=default_sensevoice_dir(speaking.runtime_root),
    )
    silero_path = _realtime_model_path(
        config, key="silero_vad_path", config_dir=config_dir,
        fallback=default_silero_vad_path(speaking.runtime_root),
    )
    models_ok, missing = _voice_models_preflight(
        sensevoice_dir=sensevoice_dir, silero_path=silero_path,
    )
    if not models_ok:
        LOGGER.error("voice models are missing, so this terminal speaks but does not listen: %s",
                     "; ".join(missing))
        return None
    turn, controls = LinkedTurn(link), LinkedControls(link)
    ports = _ListenPorts(
        answer_words=turn.answer_words, turn_working=turn.turn_working,
        recent_speech=turn.recent_speech, ask_words=turn.ask_words, note_words=turn.note_words,
        begin_line=turn.begin_line, interrupt=turn.interrupt, hold_runs=turn.hold_runs,
        supersede_unspoken=turn.supersede, cancel_voice_runs=turn.cancel_runs,
    )
    broadcaster = speech.broadcaster

    def build() -> tuple[voice_session.DuplexVoiceSession | None, bool, Dictation | None]:
        recognizer_pipeline = _build_voice_pipeline(
            seat, broadcaster=broadcaster, sensevoice_dir=sensevoice_dir,
            emit=turn.emit_utterance,
        )
        session, attempted = _spawn_single_ingress_session(
            runtime=seat, pipeline=recognizer_pipeline, broadcaster=broadcaster,
            silero_path=silero_path, tts=pipeline, voice=_voice_knobs(config),
            mic_muted=lambda: False if speaking.controls is None else speaking.controls.mic_muted,
            conversation=controls.conversation, set_conversation=controls.set_conversation,
            set_quiet=controls.set_quiet, echo_canceller=speech.canceller, ports=ports,
        )
        if session is None:
            return None, attempted, None
        # ADR 0183: dictation records on this microphone and hears with this machine's
        # recognizer; its polish, which needs a model key, is the brain's.
        vocab_path = Path(str((config.get("dictation") or {}).get("vocab_path", "")))
        dictation = build_dictation(
            config=config, ingress=session.ingress, silero_path=silero_path,
            transcribe=recognizer_pipeline.transcribe,
            polisher=RemotePolish(link, functools.partial(load_vocab, vocab_path)),
            vocab_path=vocab_path,
            recordings=(
                seat.memory.audio_dir
                if seat.memory is not None and seat.memory.retain_audio else None
            ),
        )
        return session, attempted, dictation

    try:
        session, attempted, dictation = await asyncio.to_thread(build)
    except Exception:
        LOGGER.exception("the capture session could not be built; this terminal speaks only")
        return None
    if session is None:
        LOGGER.error(
            "the microphone could not be %s, so this terminal speaks but does not listen. If "
            "another process holds it (the Mac's own jarvis daemon), stop that one and restart "
            "this terminal; it does not try again by itself.",
            "opened" if attempted else "prepared (see the warning above)",
        )
        return None
    controls.on_surface_exit = session.dismiss
    controls.start()
    LOGGER.info("this terminal listens: wake, VAD and ASR run here; utterances go to the brain")
    return _Listening(session, controls, dictation)


async def _stop_listening(task: asyncio.Task[_Listening | None] | None) -> None:
    """End the capture session: the microphone first, then the brain's switches."""
    if task is None:
        return
    await asyncio.wait({task}, timeout=_LISTEN_CLOSE_S)
    if not task.done() or task.cancelled() or task.exception() is not None:
        return
    listening = task.result()
    if listening is None:
        return
    await asyncio.to_thread(listening.controls.stop)
    await asyncio.to_thread(listening.session.close)


async def _stop_speech(speech: _Speech) -> None:
    await _stop_listening(speech.listening)
    for task in speech.tasks:
        task.cancel()
    if speech.tasks:
        await asyncio.wait(speech.tasks)
    if speech.pipeline is not None:
        await asyncio.to_thread(speech.pipeline.close, wait_timeout_s=_SPEECH_CLOSE_S)


@dataclass(frozen=True)
class _Ui:
    """``--serve-ui``: the loopback socket this terminal listens on, and what answers behind it."""

    sock: socket.socket
    config: Mapping[str, Any]
    runtime_root: Path


@dataclass(frozen=True)
class _DeviceSettings:
    """This machine's microphone and speaker choices, as the Settings page keeps them."""

    settings: Settings
    keys: frozenset[str] = DEVICE_KEYS

    def read(self) -> dict[str, Any]:
        return self.settings.read()

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        return self.settings.update(changes)


def bind_ui(port: int) -> socket.socket:
    """Listen on 127.0.0.1:``port``, or say why this terminal cannot serve the UI.

    A port that answers is held: a daemon runs there, and only one of the two may serve this
    device's UI (ADR 0183). Connecting first also catches a daemon bound to every address,
    which a bind to this one alone would not refuse on every platform.

    Raises:
        OSError: the port is held.
    """
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise OSError(_HELD.format(port=port))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise OSError(_HELD.format(port=port)) from None
    sock.listen(128)
    return sock


_HELD = (
    "127.0.0.1:{port} is held by another process, probably this machine's Jarvis daemon. A "
    "terminal serves this device's UI only where no daemon does: stop the daemon, or pick "
    "another port with --port."
)


class _UiServer(uvicorn.Server):
    """uvicorn inside the terminal's loop; Ctrl-C and SIGTERM stay the terminal's own."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def _dictation_of(speech: _Speech) -> Dictation | None:
    """The recording session, once the capture session is up; ``None`` before and without one."""
    task = speech.listening
    if task is None or not task.done() or task.cancelled() or task.exception() is not None:
        return None
    listening = task.result()
    return None if listening is None else listening.dictation


def _start_night(night: NightRun | None, speech: _Speech) -> asyncio.Task[None] | None:
    """Tick this Mac's night run; it mutes after the goodnight line, never under a playing one."""
    if night is None:
        return None
    night.busy = lambda: (d := speech.ducker) is not None and (d.active or d.outputting)
    return asyncio.create_task(night.run(), name="night_run")


def _ui_device(
    ui: _Ui, speaking: _Speaking | None, speech: Callable[[], _Speech],
    night: NightRun | None = None,
) -> Device:
    """What this machine answers itself behind the UI (ADR 0183)."""
    return Device(
        controls=None if speaking is None else speaking.controls,
        settings=(
            None if speaking is None
            else _DeviceSettings(
                Settings(ui.runtime_root, ui.config, _audio_devices, _default_audio_device),
            )
        ),
        restart=_restart_soon if spawned_by_terminal_agent() else None,
        dictation=None if speaking is None else lambda: _dictation_of(speech()),
        speaks=lambda: speech().pipeline is not None,
        night=night,
    )


async def _run(  # noqa: PLR0913 — one keyword per thing a terminal runs.
    base_url: str, token: str, *, tools: frozenset[str], execute: Execute,
    watched: _Watched | None, speaking: _Speaking | None = None, ui: _Ui | None = None,
    night: NightRun | None = None,
) -> None:
    """The link, and beside it the observers, the voice and the UI when there is anything to run."""
    outbox = EventOutbox() if watched is not None or speaking is not None else None
    observing = None if outbox is None or watched is None else asyncio.create_task(
        _observe(outbox, watched),
    )
    speech = _Speech()
    serving: asyncio.Task[None] | None = None
    server: _UiServer | None = None
    brain: Brain | None = None
    if ui is not None:
        broadcaster = UiBroadcaster()
        if speaking is not None:
            speaking = replace(speaking, broadcaster=broadcaster, controls=VoiceControls())
        brain = Brain(base_url, token)
        app = create_ui_app(
            brain,
            authorize=functools.partial(local_key_matches, local_key(ui.runtime_root)),
            broadcaster=broadcaster,
            device=_ui_device(ui, speaking, lambda: speech, night),
        )
        server = _UiServer(uvicorn.Config(app, log_level="warning", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[ui.sock]))
        LOGGER.info("this terminal serves the UI on 127.0.0.1:%d", ui.sock.getsockname()[1])
    if speaking is not None and outbox is not None:
        try:
            speech = _start_speech(speaking, outbox)
        except Exception:
            LOGGER.exception("voice could not start; this terminal runs without it")
    if speaking is not None and speech.pipeline is not None:
        execute = with_voice_commands(execute, speech.pipeline)
        speech.listening = asyncio.create_task(_start_listening(speaking, speech))
    nights = _start_night(night, speech)
    try:
        await run_terminal_client(
            base_url, token, tools=tools, execute=execute, events=outbox, voice=speech.link,
        )
    finally:
        if nights is not None:
            nights.cancel()
            await asyncio.wait({nights})
        if observing is not None:
            observing.cancel()
            await asyncio.wait({observing})
        await _stop_speech(speech)
        if server is not None and serving is not None:
            server.should_exit = True
            await asyncio.wait({serving})
        if brain is not None:
            await brain.close()


def _configure_realtime_trace(runtime_root: Path) -> None:
    """``JARVIS_REALTIME_TRACE_JSONL`` as the daemon reads it: the latency trace of this process.

    A relative path lands under the runtime root. The terminal's trace holds the same points a
    daemon's does (endpoint, ASR, first audible callback) on this machine's own clock.
    """
    raw = os.environ.get("JARVIS_REALTIME_TRACE_JSONL")
    if not raw:
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = runtime_root / path
    try:
        configure_realtime_trace_jsonl(path)
    except OSError as exc:
        LOGGER.warning("cannot open the realtime trace %s: %s", path, exc)
    else:
        LOGGER.info("realtime trace: %s", path)


def _own_projects(config: Mapping[str, Any]) -> tuple[Project, ...]:
    """This machine's ``projects`` catalog; a malformed one is the brain's to refuse at boot."""
    try:
        return parse_catalog(config.get("projects"))
    except ValueError:
        LOGGER.warning("this machine's projects catalog is malformed; the brain gets no commits")
        return ()


def run_terminal(  # noqa: PLR0913 — one keyword per switch of the command.
    base_url: str,
    token: str,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
    observers: bool = True,
    voice: bool = False,
    serve_ui: int | None = None,
) -> int:
    """Hold this device's link to the brain until interrupted; the exit code of the command.

    Reads only this machine's own config (the shipped YAML and the runtime root's
    ``settings.yaml``); it opens no database, no env file and no key. With ``observers`` the
    repos and TimeSink that config turns on are observed here and reported to the brain.
    With ``voice`` this terminal also plays the brain's spoken answers (ADR 0172). With
    ``serve_ui`` (a port) it also serves this device's UI on 127.0.0.1 (ADR 0183); a port that
    is held ends the command before anything else starts.
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
        usage_interval_s = _usage_poll_interval_s(config) if observers else None
    except (RuntimeBootstrapError, ValueError) as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    ui = None
    if serve_ui is not None:
        try:
            ui = _Ui(bind_ui(serve_ui), config, runtime_root)
        except OSError as exc:
            sys.stderr.write(f"jarvis terminal: {exc}\n")
            return 1
    _vision_preset, max_width_px = _screen_tools_config(config)
    claude = ClaudeSessions() if _claude_sessions_read(config) else None
    # ADR 0192: this Mac's night run, held here because a brain has no Mac to keep awake. Its
    # events are the one log a terminal keeps; it recovers an open run from them at start.
    night_log = runtime_root / NIGHT_LOG
    night_log.parent.mkdir(parents=True, exist_ok=True)
    open_event_log(night_log).close()
    night = NightRun(
        night_log, night_settings(config), MacPower(),
        zone=resolve_zone(None, _work_state_timezone(config))[1],
    )
    # It holds the Mac past the deadline while a session it can see still works. Codex's board
    # is filled by hooks into the brain, so none is read here.
    night.watch = NightWatch(
        port=_agents_port(), key=functools.partial(local_key, runtime_root), claude=claude,
        codex={},
    )
    registry = build_default_registry(
        obsidian_vault_root=_obsidian_vault_root(config), night=night,
    )
    registry.register(make_screen_capture(max_width_px))
    tools = _declared(registry)
    LOGGER.info("this terminal runs %s", sorted(tools))
    watched = (
        _Watched(
            repos, _observer_poll_interval_s(config), store, _timesink_poll_interval_s(config),
            usage_interval_s,
        )
        if repos or store is not None or usage_interval_s is not None
        else None
    )
    if watched is not None:
        LOGGER.info("this terminal observes %d repo(s)%s%s", len(repos),
                    "" if store is None else " and TimeSink",
                    "" if usage_interval_s is None else " and Claude Code / Codex usage")
    try:
        execute = make_executor(
            registry, timesink_store=configured_store, repos=configured_repos,
            projects=_own_projects(config),
            claude=claude,
        )
        if voice:
            _configure_realtime_trace(runtime_root)
        asyncio.run(_run(
            base_url, token, tools=tools, execute=execute, watched=watched,
            speaking=_Speaking(config, runtime_root, config_path.parent) if voice else None,
            ui=ui, night=night,
        ))
    except TerminalRefusedError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    except KeyboardInterrupt:
        LOGGER.info("terminal stopped")
    return 0
