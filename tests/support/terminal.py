"""A brain server and a terminal link over a real log, for the terminal tests."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

import numpy as np
import uvicorn
from fastapi.testclient import TestClient
from starlette.types import ASGIApp

from jarvis.execution.tools import (
    ActionLifecycle,
    ToolRegistry,
    build_default_registry,
    make_screen_capture,
)
from jarvis.runtime.daemon import output_watchers
from jarvis.runtime.terminal import _declared, _observe, _run, _Speaking, _Watched, make_executor
from jarvis.shared import ActionRequest, CallerPrincipal, RawResult
from jarvis.state.device_tokens import device_name_for_token, device_token_matches
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.terminal_events import BrainEvents, EventOutbox
from jarvis.surface.terminal_link import TerminalHub, run_terminal_client
from jarvis.surface.terminal_speaker import REMOTE_ENDPOINTS
from jarvis.surface.terminal_voice import BrainVoice
from jarvis.surface.voice_tts import TTSAudioChunk, TTSResponseSegment, TTSSegmentFinished
from tests.support.net import free_port
from tests.support.runtime_paths import StubRuntimePaths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

    from jarvis.runtime.settings import Settings
    from jarvis.state.projects import Project
    from jarvis.surface import terminal_voice
    from jarvis.surface.claude_sessions import ClaudeSessions

REMOTE = "100.87.250.92"


TERMINAL_WS = "ws://127.0.0.1:8006/terminal/ws"


RATE = 8_000


_END = object()


def segment_speech(segment: TTSResponseSegment) -> list[Any]:
    """What a provider says for one segment: audio, its word times, and the segment's end."""
    samples = 160 * len(segment.text)  # 20 ms a character at 8 kHz
    wave = (np.arange(samples) % 100 * 20).astype("<i2").tobytes()
    half = len(wave) // 2
    boundaries = tuple((i + 1, 20.0 * (i + 1)) for i in range(len(segment.text)))
    return [
        TTSAudioChunk(segment.sequence, wave[:half], RATE),
        TTSAudioChunk(segment.sequence, wave[half:], RATE),
        TTSAudioChunk(
            segment.sequence,
            b"",
            RATE,
            timing_text=segment.text,
            word_boundaries=boundaries,
        ),
        TTSSegmentFinished(segment.sequence, usage={"usage_characters": len(segment.text)}),
    ]


class VoiceSpeechSession:
    """The brain's real half, replaced: records every call, speaks `script(segment)`."""

    def __init__(self, provider: VoiceSpeechProvider, endpoint_index: int, language: str) -> None:
        self.provider = provider
        self.endpoint_index = endpoint_index
        self.language = language
        self.calls: list[tuple[Any, ...]] = []
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.send_error: Exception | None = None

    async def connect(self) -> None:
        self.calls.append(("connect",))
        if self.provider.connect_error is not None:
            raise self.provider.connect_error

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        self.calls.append(("open", response_id, playback_generation_id))

    async def send(self, segment: TTSResponseSegment) -> None:
        self.calls.append(("send", segment))
        if self.send_error is not None:
            raise self.send_error
        for event in self.provider.script(segment):
            self.events.put_nowait(event)

    def audio_events(self) -> AsyncIterator[Any]:
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        while True:
            item = await self.events.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def finish(self) -> None:
        self.calls.append(("finish",))
        await self.close()

    async def abort(self, reason: str) -> None:
        self.calls.append(("abort", reason))
        await self.close()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.calls.append(("close",))
            self.events.put_nowait(_END)


class VoiceSpeechProvider:
    def __init__(self, script: Callable[[TTSResponseSegment], list[Any]] = segment_speech) -> None:
        self.script = script
        self.sessions: list[VoiceSpeechSession] = []
        self.connect_error: Exception | None = None

    @property
    def streaming_candidate_count(self) -> int:
        return REMOTE_ENDPOINTS

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        language: str,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> VoiceSpeechSession:
        del idle_close_s, command_queue_capacity, audio_queue_capacity
        session = VoiceSpeechSession(self, endpoint_index, language)
        self.sessions.append(session)
        return session

    def request_close(self) -> None:
        return


class NoRows:
    def high_water(self) -> int:
        return 0

    def cursor(self, after: int) -> NoRows:
        del after
        return self

    def poll(self) -> terminal_voice.RowBatch:
        return 0, []


def brain_log(path: Path) -> sqlite3.Connection:
    """The brain's event log, usable from the server's own thread."""
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


def brain_client(
    tmp_path: Path,
    *,
    provider: VoiceSpeechProvider | None = None,
    speaks: bool = True,
) -> tuple[TestClient, TerminalHub, Path]:
    log = tmp_path / "events.db"
    conn = brain_log(log)
    hub = TerminalHub(events=BrainEvents(conn))
    if speaks:
        hub.voice = BrainVoice(
            cast("Any", provider or VoiceSpeechProvider()),
            output_watchers.SpokenRows(conn),
            poll_interval_s=0.01,
        )
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=hub,
            phone_events=hub.events,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000)), hub, log


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hello_frame(**extra: Any) -> str:  # noqa: ANN401
    return json.dumps({"type": "hello", "tools": ["read_clipboard"], **extra})


def playback_frame(
    uid: str,
    kind: str = "surface.playback_completed",
    **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {
        "surface.playback_completed": {
            "session_id": "S",
            "response_id": "R",
            "turn_id": "T",
            "playback_generation_id": 1,
            "heard_through_sequence": 0,
            "submitted_samples": 160,
            "heard_text_hash": "h",
            "speech_text_hash": "s",
            "heard_text": "Hi.",
        },
        "tts.usage_observed": {
            "provider": "minimax",
            "characters": 3,
            "response_id": "R",
            "sequence": 0,
            "actor": "observer",
        },
        "surface.speech_dropped": {"response_id": "R", "turn_id": "T", "reason": "stopped"},
    }
    return {
        "type": "event",
        "event_uid": uid,
        "event_type": kind,
        "ts_epoch_ms": int(time.time() * 1000),
        "payload": payloads[kind],
        **extra,
    }


def ack_frame(ws: Any, frame: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    ws.send_text(json.dumps(frame))
    return dict(ws.receive_json())


class _Stream:
    """A PortAudio stream with nobody on the other end: `start` begins pulling blocks."""

    active = True
    latency = 0.0

    def __init__(self, callback: Callable[..., None], *, pull: bool) -> None:
        self._callback = callback
        self._pull = pull
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if self._pull:
            self._thread.start()

    def _run(self) -> None:
        out = np.zeros((128, 1), dtype=np.float32)
        while not self._stop.is_set():
            self._callback(out, 128, None, None)
            time.sleep(0.001)

    def stop(self) -> None:
        self._stop.set()
        self.active = False

    def close(self) -> None:
        self.stop()


def open_stream(*, pull: bool) -> Callable[..., _Stream]:
    def open_stream(*, callback: Callable[..., None], **_ignored: Any) -> _Stream:  # noqa: ANN401
        return _Stream(callback, pull=pull)

    return open_stream


SPEECH_CONFIG: dict[str, Any] = {
    "realtime": {
        "enabled": True,
        "streaming_output": {
            "canonical_sample_rate_hz": RATE,
            "presentation_poll_s": 0.002,
            "ring_retry_s": 0.001,
            "enable_macos_say_fallback": False,
            "prefetch_network_lost_line": False,
            "response_timeout_s": 10.0,
            "session_idle_close_s": 5.0,
        },
    },
}


class AsRemote:
    """ASGI shim: the server sees every peer as a device on the tailnet, not on loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": (REMOTE, 50000)}
        await self.app(scope, receive, send)


def wait_for(condition: Callable[[], bool], what: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


class Brain:
    """A real uvicorn server carrying a hub on ``/terminal/ws``, over a real event log."""

    def __init__(
        self,
        root: Path,
        log: Path,
        deps: Callable[[TerminalHub], dict[str, Any]] | None = None,
        tap: Callable[[ASGIApp], ASGIApp] | None = None,
    ) -> None:
        self.hub = TerminalHub(events=BrainEvents(_brain_log(log)))
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, root),
                **({} if deps is None else deps(self.hub)),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app if tap is None else tap(app)),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            ),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()),
            daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _brain_log(path: Path) -> sqlite3.Connection:
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


class Terminal:
    """A terminal's link as ``run_terminal`` builds it: the real executor over its own files."""

    def __init__(  # noqa: PLR0913 — the link, the device's files and what watches them.
        self,
        url: str,
        token: str,
        *,
        store: Path | None,
        repos: tuple[str, ...],
        watched: _Watched | None = None,
        projects: tuple[Project, ...] = (),
        claude: ClaudeSessions | None = None,
    ) -> None:
        registry = build_default_registry(obsidian_vault_root=None)
        registry.register(make_screen_capture(800))
        self._args = (
            url,
            token,
            _declared(registry),
            make_executor(
                registry,
                timesink_store=store,
                repos=repos,
                projects=projects,
                claude=claude,
            ),
            watched,
        )
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, tools, execute, watched = self._args
            outbox = None if watched is None else EventOutbox()
            observing = (
                None
                if watched is None or outbox is None
                else asyncio.create_task(
                    _observe(outbox, watched),
                )
            )
            self.task = asyncio.create_task(
                run_terminal_client(
                    url,
                    token,
                    tools=tools,
                    execute=execute,
                    events=outbox,
                )
            )
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                if observing is not None:
                    observing.cancel()
                    await asyncio.wait({observing})

        self.loop.run_until_complete(main())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=10)

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def dispatch_tool(
    registry: ToolRegistry,
    root: Path,
    tool: str,
    arguments: Mapping[str, Any],
    entity: str | None = None,
) -> RawResult:
    """Run one tool the way the decision layer does; always on a worker thread."""
    conn = open_event_log(root / "events.db")
    try:
        lifecycle = ActionLifecycle()
        action_id = uuid.uuid4().hex
        lifecycle.register(action_id)
        lifecycle.transition(action_id, "authorized")
        request = ActionRequest(
            action_id=action_id,
            tool_name=tool,
            target_entity_ref=entity,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L0",
            arguments=dict(arguments),
            authorization_lease=None,
            run_id=None,
            turn_id=None,
        )
        paths = StubRuntimePaths(root / "events.db", root / "artifacts")
        return registry.dispatch(request, conn, paths, lifecycle).slots[0]
    finally:
        conn.close()


class TokenBrain:
    """A real uvicorn server with the terminal route and device-token check."""

    def __init__(self, tmp_path: Path) -> None:
        self.hub = TerminalHub(call_timeout_s=10)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, tmp_path),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(tmp_path)),
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            ),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()),
            daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server never started"
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 — a fixed git argv on a temp repo.
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    commit_file(path, "first")
    return path


def commit_file(repo: Path, subject: str) -> None:
    """Add a file and commit it with ``subject``."""
    (repo / f"{subject}.txt").write_text(subject, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)


def log_rows(path: Path, *types: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(type, ingestion_node, payload)`` of the log's rows, oldest first."""
    marks = ",".join("?" for _ in types)
    with sqlite3.connect(path) as conn:
        found = conn.execute(
            f"SELECT type, ingestion_node, payload_json FROM events WHERE type IN ({marks}) "  # noqa: S608
            "ORDER BY id",
            types,
        ).fetchall()
    return [(kind, node, json.loads(payload)) for kind, node, payload in found]


def observer_brain_client(tmp_path: Path) -> tuple[TestClient, TerminalHub, Path]:
    log = tmp_path / "events.db"
    hub = TerminalHub(events=BrainEvents(brain_log(log)))
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=hub,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000)), hub, log


def unstamped(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != "observed_at_ms"}


def append_answer(
    log: Path, turn: str, text: str = "Hello.", *, channel: str | None = None
) -> list[str]:
    """Append one answer's three rows to the brain's log; their uids."""
    conn = open_event_log(log)
    try:
        group = f"G-{turn}"
        extra = {} if channel is None else {"attention_channel": channel}
        events = [
            emit_event(
                conn,
                type="surface.response_open",
                payload={
                    "turn_id": turn,
                    "query": "q",
                    "kind": "text",
                    "response_id": f"R-{turn}",
                    "response_group_id": group,
                    "phase": "final",
                    "channel": "speech",
                    **extra,
                },
            ),
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": turn,
                    "text": text,
                    "response_id": f"R-{turn}",
                    "response_group_id": group,
                    "sequence": 0,
                    "phase": "final",
                    "channel": "speech",
                    "segment_hash": "h",
                },
            ),
            emit_event(
                conn,
                type="surface.response_emitted",
                payload={
                    "turn_id": turn,
                    "text": text,
                    "response_id": f"R-{turn}",
                    "response_group_id": group,
                    "phase": "final",
                    "channel": "speech",
                },
            ),
        ]
    finally:
        conn.close()
    return [event.event_uid for event in events]


def ws_rows(ws: Any, count: int) -> list[dict[str, Any]]:  # noqa: ANN401
    return [dict(ws.receive_json()) for _ in range(count)]


class VoiceBrain:
    """A real uvicorn server: the terminal route, a brain log, and a fake speech provider."""

    def __init__(self, root: Path) -> None:
        self.port = free_port()
        self.log = root / "events.db"
        conn = brain_log(self.log)
        self.provider = VoiceSpeechProvider()
        self.hub = TerminalHub(events=BrainEvents(conn))
        self.hub.voice = BrainVoice(
            cast("Any", self.provider),
            output_watchers.SpokenRows(conn),
            poll_interval_s=0.01,
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, root),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            ),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()),
            daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class VoiceTerminal:
    """`python -m jarvis terminal --voice` on its own thread and loop: the real wiring."""

    def __init__(
        self,
        url: str,
        token: str,
        root: Path,
        config: dict[str, Any] | None = None,
        page: Settings | None = None,
    ) -> None:
        self.page = page
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.error: BaseException | None = None
        self._args = (url, token, root)
        self._config = SPEECH_CONFIG if config is None else config
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, root = self._args
            self.task = asyncio.create_task(
                _run(
                    url,
                    token,
                    tools=frozenset({"read_clipboard"}),
                    execute=lambda *_: {"ok": True, "output": {}},
                    watched=None,
                    speaking=_Speaking(self._config, root),
                    page=self.page,
                )
            )
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self.task
                except BaseException as exc:  # noqa: BLE001 — handed to the test
                    self.error = exc

        self.loop.run_until_complete(main())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()


def logged_types(log: Path) -> list[str]:
    with sqlite3.connect(log) as conn:
        return [row[0] for row in conn.execute("SELECT type FROM events ORDER BY id")]
