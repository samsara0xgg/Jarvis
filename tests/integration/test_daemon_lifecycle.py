"""The daemon fails safely and stops on time.

A failed setup closes the mic; a turn that never ends cannot hold the shutdown.

``serve_inherent`` runs in this process on a throwaway runtime root, with uvicorn stood in for
by a stub that returns when the test says so (it stands for the signal that ends a daemon).
Everything else is the real composition: the intent pump, the watchers, the boot reconcilers.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import types
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

import jarvis.runtime as runtime_pkg
from jarvis.runtime import inherent_loop
from jarvis.runtime.daemon import intent_pump, voice_power
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_runtime_event_log
from jarvis.surface import voice_media
from jarvis.surface.cli import emit_surface_user_intent
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _FakeVoiceSession:
    """What ``DuplexVoiceSession`` is to the process: non-daemon threads that run until closed."""

    ingress = None

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._stop.wait, name="jarvis-fake-ingress")
        self.thread.start()
        self.close_calls = 0

    def dismiss(self) -> None:
        pass

    def close(self) -> types.SimpleNamespace:
        self.close_calls += 1
        self._stop.set()
        self.thread.join(timeout=5)
        return types.SimpleNamespace(definitively_closed=not self.thread.is_alive())


class _StubServer:
    """Stands in for ``uvicorn.Server``: ``serve`` returns once the test releases it."""

    release: Callable[[], Any] | None = None

    def __init__(self, _config: object) -> None:
        pass

    async def serve(self, sockets: object = None) -> None:  # noqa: ARG002
        assert _StubServer.release is not None
        await _StubServer.release()


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        "observer:\n  timesink:\n    enabled: false\n  repos: []\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return boot_runtime(root)


def _fake_voice(monkeypatch: pytest.MonkeyPatch) -> _FakeVoiceSession:
    """Make the daemon believe the models are present and hand it a started input session."""
    session = _FakeVoiceSession()
    monkeypatch.delenv("JARVIS_VOICE_DISABLE_WAKE", raising=False)
    monkeypatch.setattr(inherent_loop, "_voice_models_preflight", lambda **_kw: (True, []))
    monkeypatch.setattr(inherent_loop, "_build_voice_pipeline", lambda *_a, **_kw: object())
    monkeypatch.setattr(inherent_loop, "_build_voice_pipeline_callable", lambda _pipe: None)
    monkeypatch.setattr(inherent_loop, "_build_echo_canceller", lambda _runtime: None)
    monkeypatch.setattr(inherent_loop, "_build_tts_pipeline", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        inherent_loop,
        "_spawn_voice_input_owners",
        lambda **_kw: voice_power.VoiceInputOwners(
            duplex_session=session,  # type: ignore[arg-type]
            single_ingress_attempted=True,
            reason="test",
        ),
    )
    return session


_LOCKED = "database is locked"


def _locked(*_args: object, **_kwargs: object) -> int:
    raise sqlite3.OperationalError(_LOCKED)


async def _locked_async(*_args: object, **_kwargs: object) -> list[asyncio.Task[None]]:
    raise sqlite3.OperationalError(_LOCKED)


@pytest.mark.parametrize(
    ("step", "failure"),
    [
        # Right after the mic started: a boot reconciler meets a locked database.
        ("_reconcile_open_playback_in_thread", _locked),
        # Late: the intent pump and the response watchers are already running.
        ("_start_sweep_control_plane", _locked_async),
    ],
)
def test_a_setup_failure_after_the_mic_started_closes_it_and_ends_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, failure: Callable[..., Any],
) -> None:
    """A setup step failing must not leave the mic open, tasks running and the process alive."""
    runtime = _runtime(tmp_path, monkeypatch)
    session = _fake_voice(monkeypatch)
    monkeypatch.setattr(inherent_loop, step, failure)
    monkeypatch.setattr(inherent_loop, "_Server", _StubServer)

    async def run() -> list[asyncio.Task[Any]]:
        try:
            await inherent_loop.serve_inherent(runtime, port=0)
        except sqlite3.OperationalError:
            current = asyncio.current_task()
            return [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        msg = "the setup failure was swallowed"
        raise AssertionError(msg)

    try:
        pending = asyncio.run(run())
        assert session.close_calls >= 1, "the input session was never closed"
        assert not session.thread.is_alive(), "its threads are still running"
        assert pending == [], "tasks the failed setup started are still running"
    finally:
        session.close()
        runtime.conn.close()


def test_a_turn_that_never_ends_cannot_hold_the_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon stops within its budget, and records the run it had to leave as cancelled."""
    runtime = _runtime(tmp_path, monkeypatch)
    entered, forever = threading.Event(), threading.Event()

    def stuck_turn(worker_runtime: Any, *, user_intent_event: Any, **_kw: object) -> None:  # noqa: ANN401
        """A model call that does not return: it opens its run, then waits for ``forever``."""
        runtime_pkg._start_drive_turn_response(  # noqa: SLF001
            worker_runtime,
            user_intent_event=user_intent_event,
            turn_id=str(user_intent_event.payload["turn_id"]),
        )
        entered.set()
        forever.wait()

    class _EndsWhenTheTurnIsRunning(_StubServer):
        async def serve(self, sockets: object = None) -> None:  # noqa: ARG002
            def ask() -> None:
                conn = open_runtime_event_log(runtime.runtime_paths.event_log)
                try:
                    emit_surface_user_intent(conn, transcript="hello", turn_id="T-stuck")
                finally:
                    conn.close()

            await asyncio.to_thread(ask)
            assert await asyncio.to_thread(entered.wait, 30), "the turn never started"

    monkeypatch.setattr(intent_pump, "drive_turn", stuck_turn)
    monkeypatch.setattr(inherent_loop, "_Server", _EndsWhenTheTurnIsRunning)

    async def run() -> None:
        try:
            # The outer bound only turns a hang into a failure; the budget is what ends the wait.
            await asyncio.wait_for(
                inherent_loop.serve_inherent(runtime, port=0, shutdown_budget_s=0.05),
                timeout=30,
            )
        finally:
            # serve_inherent has returned with the turn still stuck; free its thread so the
            # loop can close (a real process waits for it until the supervisor's kill).
            forever.set()

    reset_realtime_trace()
    try:
        asyncio.run(run())
    finally:
        forever.set()
        runtime.conn.close()

    log = runtime.runtime_paths.event_log
    with closing(sqlite3.connect(f"file:{log}?mode=ro", uri=True)) as db:
        cancelled = [
            json.loads(payload)
            for (payload,) in db.execute(
                "SELECT payload_json FROM events WHERE type = 'response.cancelled'",
            )
        ]
    assert [(row["turn_id"], row["reason"]) for row in cancelled] == [("T-stuck", "shutdown")]
    left = [p for p in realtime_trace_snapshot() if p.name == "shutdown_deadline"]
    assert len(left) == 1
    assert "intent_worker" in str(left[0].attributes["unfinished"])


# `jarvis serve` as the CLI runs it, told when it is serving: uvicorn has bound the port, so
# its signal handlers are in place. Patched on uvicorn itself, so the daemon's own server
# class (a subclass) inherits it.
_SERVE_AND_SAY_SO = """
import logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
import uvicorn
from jarvis.cli import main

startup = uvicorn.Server.startup


async def say_so(self, sockets=None):
    await startup(self, sockets)
    print("serving", flush=True)


uvicorn.Server.startup = say_so
sys.exit(main(sys.argv[1:]))
"""


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_runs_the_daemons_whole_teardown_and_ends_it_clean(
    tmp_path: Path, stop: signal.Signals,
) -> None:
    """A stop signal ends serve and the teardown runs; the process then leaves cleanly."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text("runtime:\n  role: brain\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.endswith(("_API_KEY", "_ADMIN_KEY")) and name != "JARVIS_RUNTIME_ROOT"
    }
    env["HOME"] = str(tmp_path)
    log = tmp_path / "daemon.log"
    with log.open("w") as sink:
        daemon = subprocess.Popen(  # noqa: S603 — sys.executable and a fixed script.
            [sys.executable, "-c", _SERVE_AND_SAY_SO, "serve", "--runtime-root", str(root),
             "--port", str(port)],
            stdout=subprocess.PIPE, stderr=sink, text=True, env=env,
        )
        try:
            assert daemon.stdout is not None
            assert daemon.stdout.readline() == "serving\n", log.read_text(encoding="utf-8")[-2000:]
            daemon.send_signal(stop)
            returncode = daemon.wait(timeout=60)
        finally:
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait()

    text = log.read_text(encoding="utf-8")
    assert "serve_inherent: shutdown complete" in text, text[-2000:]
    assert returncode == 0, text[-2000:]


def test_shutdown_tts_closes_pipeline() -> None:
    """``_shutdown_tts`` must call ``tts_pipe.close()`` to release the PortAudio device."""
    tts_pipe = MagicMock(spec=voice_media.StreamingTTSPipeline)
    inherent_loop._shutdown_tts(tts_pipe)  # noqa: SLF001
    tts_pipe.close.assert_called_once_with()


def test_shutdown_tts_none_is_noop() -> None:
    """``_shutdown_tts(None)`` must not raise (voice subsystem may be absent)."""
    inherent_loop._shutdown_tts(None)  # noqa: SLF001 — must not raise
