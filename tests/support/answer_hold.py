"""The capture-side pipeline and answer readers for the tests of the answer hold (ADR 0053)."""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import response_run
from jarvis.runtime import answer_hold
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_pipeline
from tests.support.arbitration import emit_whole_response
from tests.support.clock import FakeClock
from tests.support.hold import HANG_S
from tests.support.media import terminal_rows
from tests.support.voice_input import RecordingPipeline, wait_until_true

if TYPE_CHECKING:
    from collections.abc import Iterator
    from concurrent.futures import Future
    from pathlib import Path

    from jarvis.shared import Event


class AcceptingPipeline(RecordingPipeline):
    """ASR that accepts every utterance, or finds nothing in it."""

    def __init__(
        self,
        record: list[str],
        *,
        empty: bool = False,
        said: str = "明天上午十点提醒我",
    ) -> None:
        super().__init__()
        self._record = record
        self._empty = empty
        self._said = said

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        if self._empty:
            msg = "silence"
            raise voice_pipeline.VoicePipelineEmptyError(msg)
        kwargs["before_emit"](self._said)
        self._record.append("utterance.received")
        return super().run_turn(**kwargs)


def played_texts(db_path: Path) -> list[str]:
    return [
        str(payload["response_id"])
        for kind, payload in terminal_rows(open_event_log(db_path))
        if kind == "surface.playback_completed"
    ]


def answer_of(conn: Any, response_id: str, turn_id: str) -> Any:  # noqa: ANN401
    return emit_whole_response(
        conn,
        response_id=response_id,
        group_id=f"G{turn_id}",
        turn_id=turn_id,
        text="好的",
    )


def hold_on_clock(monkeypatch: pytest.MonkeyPatch, heard: Event) -> FakeClock:
    """Count the answer hold (ADR 0272) on a clock that starts when ``heard`` was stamped.

    The hold waits in slices against the monotonic clock. Here a slice waits on the clock itself,
    so a held answer stays held until the test advances it, however long the machine takes to
    get the answer ready: :func:`wait_held` is the moment it is ready and waiting, and
    ``clock.advance`` is the time passing. The wall clock the hold counts from is the clock too,
    so the hold ends ``hold`` seconds after the stamp, less what the device said it had waited.
    """
    clock = FakeClock(datetime.fromtimestamp(heard.ts_epoch_ms / 1000, UTC))
    never = threading.Event()  # only the clock ends a slice
    monkeypatch.setattr(answer_hold, "time", clock)
    monkeypatch.setattr(
        response_run,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=lambda s: clock.wait(never, s)),
    )
    return clock


@contextlib.contextmanager
def held_turns(clock: FakeClock) -> Iterator[None]:
    """On the way out, let every turn still held on ``clock`` go: a failed test ends, not hangs.

    Use it inside the ``with`` of the pool the turns run on, which waits for them on its way out.
    """
    try:
        yield
    finally:
        clock.advance(HANG_S)


def wait_held(clock: FakeClock, count: int = 1) -> None:
    """Block until ``count`` answers (or writes) are ready and waiting for the hold to end."""
    wait_until_true(lambda: clock.blocked >= count, timeout_s=15.0)


def finish_unheld(turn: Future[Any]) -> None:
    """The turn ends while its clock stands still: nothing of it waited for a hold."""
    try:
        turn.result(timeout=HANG_S)
    except TimeoutError:
        pytest.fail("the turn waited for a hold")
