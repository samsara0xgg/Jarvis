"""A runtime wired for lifecycle commentary, its watcher on a fake clock, and what it logged."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Self

import pytest

from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime
from jarvis.runtime.daemon import commentary
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import fetch_events_after, open_event_log
from jarvis.surface.cli import parse_response_channels
from tests.support.clock import FakeClock
from tests.support.model import ScriptedModel, ScriptedSessions
from tests.support.runtime_config import runtime_config

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event


def _observer_config(*, commentary: bool) -> dict[str, Any]:
    """Config for a runtime with D6 commentary."""
    return {
        "realtime": {
            "enabled": True,
            "response": {"cancel_timeout_ms": 500},
            "commentary": {"enabled": commentary},
        },
    }


_LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "default_preset": "fast",
    "presets": {
        "fast": {
            "provider": "openai",
            "model": "gpt-fast",
            "base_url": "https://example.invalid/fast",
            "max_tokens": 64,
        },
    },
}


FINAL_ANSWER = "现在是下午三点。"
"""What the scripted model answers when a turn is driven on a commentary runtime."""


def commentary_runtime(
    tmp_path: Path,
    *,
    commentary: bool = True,
    model: ScriptedModel | None = None,
    clock: FakeClock | None = None,
) -> JarvisRuntime:
    """Assemble the runtime the daemon's watchers receive; ``model`` answers its turns.

    Its clock is ``clock``, else a fresh :class:`FakeClock`: nothing in it waits for real time.
    """
    model = ScriptedModel(otherwise=FINAL_ANSWER) if model is None else model
    paths = bootstrap_runtime(tmp_path)
    config = _observer_config(commentary=commentary)
    parsed = runtime_config(config, root=tmp_path)
    return JarvisRuntime(
        config=parsed,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=model.as_llm_client(),
        system_prompt="",
        response_flags=parsed.realtime.response.flags,
        llm_session_factory=ScriptedSessions(model, _LLM_CONFIG),
        response_runs=ResponseRunRegistry(),
        committed_event_bus=CommittedEventBus(),
        clock=FakeClock() if clock is None else clock,
    )


def fake_clock(runtime: JarvisRuntime) -> FakeClock:
    """The fake clock of a runtime :func:`commentary_runtime` built."""
    assert isinstance(runtime.clock, FakeClock)
    return runtime.clock


def typed_payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return the decoded payloads of one event type in append order."""
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def spoken_phrases(conn: sqlite3.Connection) -> list[str]:
    """Return the voice text of every commentary response, tags stripped."""
    return [
        parse_response_channels(payload["text"]).voice
        for payload in typed_payloads(conn, "surface.response_emitted")
        if payload.get("phase") == "commentary"
    ]


def only_phrase(conn: sqlite3.Connection) -> str:
    """The single commentary phrase this log holds, asserting there is one."""
    spoken = spoken_phrases(conn)
    assert len(spoken) == 1, spoken
    return spoken[0]


class _CountingExecutor(ThreadPoolExecutor):
    """The loop's executor, counting the thread hops that have started and not yet ended."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(thread_name_prefix="test-commentary-hop")
        self._clock = clock
        self._count = threading.Lock()
        self.running = 0
        self.submitted = 0

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        with self._count:
            self.running += 1
            self.submitted += 1
        self._clock.poke()

        def hop() -> T:
            try:
                return fn(*args, **kwargs)
            finally:
                with self._count:
                    self.running -= 1
                self._clock.poke()

        return super().submit(hop)


class CommentaryObserver:
    """The commentary watcher the way the daemon runs it, on a loop of its own, on the fake clock.

    The watcher anchors at the log's high-water mark when it starts, so a row only reaches it if
    it is emitted inside the ``with`` block, which is also what keeps a historical action from
    speaking fake progress. Nothing here waits for real time: :meth:`quiesce` returns when the
    watcher has read every row emitted before the call and every thread it started has either
    finished or is held by the clock, and :meth:`advance` moves the clock and then does that.
    """

    _GUARD_S = 5.0
    """How long the test waits for the watcher to catch up before it fails; not a time it keeps."""
    _POLLS_TO_READ = 3
    """Polls after a call that surely read every row emitted before it, and handled them."""
    _YIELDS = 10
    _RELEASE_S = 1.0e6
    """Past every hold the watcher keeps, to free its threads after a failure."""
    """Loop turns that let a chain of task and thread hand-offs run to its next await."""

    def __init__(self, runtime: JarvisRuntime) -> None:
        """Bind the runtime (with a fake clock) whose event log the watcher will poll."""
        self.clock = fake_clock(runtime)
        self._runtime = runtime
        self._executor = _CountingExecutor(self.clock)
        self._polled = threading.Condition()
        self._polls = 0
        self._watcher_conn: sqlite3.Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = threading.Event()
        self._patch = pytest.MonkeyPatch()
        self._thread = threading.Thread(
            target=self._run,
            name="test-commentary-observer",
            daemon=True,  # a watcher that cannot stop must fail its test, not hold the process
        )

    def _run(self) -> None:
        conn = open_event_log(self._runtime.runtime_paths.event_log)
        self._watcher_conn = conn
        runtime = replace(self._runtime, conn=conn)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(self._executor)
            self._loop = loop
            self._task = asyncio.create_task(
                commentary.commentary_watcher(runtime, poll_interval_s=0.005),
            )
            self._ready.set()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

        try:
            asyncio.run(_main())
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _counting_fetch(self) -> Callable[..., list[tuple[int, Event]]]:
        fetch = fetch_events_after

        def counting(conn: sqlite3.Connection, **kwargs: Any) -> list[tuple[int, Event]]:  # noqa: ANN401
            if conn is self._watcher_conn:
                with self._polled:
                    self._polls += 1
                    self._polled.notify_all()
            return fetch(conn, **kwargs)

        return counting

    def __enter__(self) -> Self:
        """Start the watcher and wait for its first poll, which is after it took its anchor."""
        self._patch.setattr(commentary, "fetch_events_after", self._counting_fetch())
        self._thread.start()
        assert self._ready.wait(timeout=self._GUARD_S)
        self._wait_for_polls(1)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Cancel the watcher the way ``serve_inherent``'s teardown does."""
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        self._thread.join(timeout=self._GUARD_S)
        stopped = not self._thread.is_alive()
        self._patch.undo()
        if not stopped:
            self.clock.advance(self._RELEASE_S)  # frees any hold, so the executor can end
        self._executor.shutdown(wait=True)
        assert stopped, "the watcher did not stop when cancelled"

    def _wait_for_polls(self, count: int) -> None:
        with self._polled:
            assert self._polled.wait_for(lambda: self._polls >= count, self._GUARD_S), (
                "the watcher did not poll the log"
            )

    def _idle(self) -> bool:
        return self._executor.running <= self.clock.blocked

    def _yield_loop(self) -> None:
        async def turns() -> None:
            for _ in range(self._YIELDS):
                await asyncio.sleep(0)

        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(turns(), self._loop).result(timeout=self._GUARD_S)

    def quiesce(self) -> None:
        """Return once the watcher has done all it can without the clock moving."""
        with self._polled:
            seen = self._polls
        self._wait_for_polls(seen + self._POLLS_TO_READ)
        while True:
            hops = self._executor.submitted
            self.clock.wait_until(self._idle, "the watcher's threads to finish or wait")
            self._yield_loop()
            if self._executor.submitted == hops and self._idle():
                return

    def advance(self, seconds: float) -> None:
        """Move the clock, then let the watcher do all it now can."""
        self.clock.advance(seconds)
        self.quiesce()
