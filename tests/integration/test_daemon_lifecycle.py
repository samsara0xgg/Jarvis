"""The daemon fails safely and stops on time.

A failed setup closes the mic; a turn that never ends cannot hold the shutdown.

``serve_inherent`` runs in this process on a throwaway runtime root, with uvicorn stood in for
by a stub that returns when the test says so (it stands for the signal that ends a daemon).
Everything else is the real composition: the intent pump, the watchers, the boot reconcilers.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import types
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn

import jarvis.runtime as runtime_pkg
from jarvis.runtime import bootstrap_runtime_app, inherent_loop
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_runtime_event_log
from jarvis.surface.cli import emit_surface_user_intent

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
    return bootstrap_runtime_app(runtime_root=root)


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
        lambda **_kw: inherent_loop._VoiceInputOwners(  # noqa: SLF001
            duplex_session=session,  # type: ignore[arg-type]
            wake_listener=None,
            wake_stream=None,
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
    monkeypatch.setattr(uvicorn, "Server", _StubServer)

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

    monkeypatch.setattr(inherent_loop, "drive_turn", stuck_turn)
    monkeypatch.setattr(uvicorn, "Server", _EndsWhenTheTurnIsRunning)

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
