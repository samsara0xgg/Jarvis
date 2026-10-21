"""A voice terminal whose microphone did not open does not stay deaf (2026-10-21 04:03).

Acceptance checks against the real terminal code, a capture session over the injected
microphone (:class:`FakeBackend`), the waits stepped by the test and no brain on the link:

- a first open that failed for good is tried again after the backoff the daemon uses for a
  microphone that stays shut (ADR 0054), the next wait is longer, and the terminal then listens;
- a wake of the Mac, and a change of the devices to be on, cut the wait short; a device poll
  that finds nothing changed does not;
- a close that was not confirmed means the failed open may still hold the microphone: there is
  no second open, however long the terminal runs or what wakes it;
- a terminal that ends while it waits ends at once, and takes its watches with it;
- every way a terminal ends up unable to listen says why in its own log, at warning or above.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

from jarvis.runtime import terminal as terminal_module
from jarvis.runtime.daemon import devices
from jarvis.runtime.daemon.devices import MIC_RETRY_S
from jarvis.runtime.terminal import _Listening, _start_listening, _stop_listening
from jarvis.surface import voice_asr, voice_audio, voice_backend, voice_session, voice_wake
from jarvis.surface.voice_backend import BackendStartStatus, BackendStopStatus
from tests.support.stepped_sleep import SteppedSleep
from tests.support.terminal_listen import heard_session, terminal_without_a_brain
from tests.support.voice_input import FakeBackend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.terminal import _Speaking, _Speech

_GUARD_S = 5.0
"""How long a test waits for the terminal to go on before it fails; not a time anything keeps."""
_ALL_START_STATUSES = [
    BackendStartStatus.FAILED_CLOSED,
    BackendStartStatus.OWNER_BUSY,
    BackendStartStatus.OPEN_UNCERTAIN,
]


def _start(
    speaking: _Speaking, speech: _Speech, sleep: SteppedSleep,
) -> asyncio.Task[_Listening | None]:
    """Start the capture session the way ``_run`` does, with this test's wait."""
    task = asyncio.create_task(_start_listening(speaking, speech, sleep=sleep))
    speech.listening = task
    return task


async def _listens(task: asyncio.Task[_Listening | None]) -> _Listening:
    listening = await asyncio.wait_for(task, _GUARD_S)
    assert listening is not None
    return listening


def _said(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the terminal itself logged at warning or above, with any traceback."""
    return [
        caplog.handler.format(record)
        for record in caplog.records
        if record.name == "jarvis.runtime.terminal" and record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize("status", _ALL_START_STATUSES)
def test_a_microphone_that_did_not_open_is_opened_when_the_wait_is_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: BackendStartStatus,
) -> None:
    """The terminal waits as a daemon does for a shut microphone, then opens it and listens.

    The foreign open that may own the device is the incident's: its close was confirmed, so
    nothing holds the microphone and the second open is safe.
    """
    backend = FakeBackend()
    backend.fail_starts(status)
    heard_session(monkeypatch, lambda: backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            await sleep.parked()
            assert not task.done()  # deaf, and waiting to try again
            assert backend.active_owner_count == 0
            sleep.release()
            await _listens(task)
            assert backend.start_count == 2
            assert backend.active_owner_count == 1  # the microphone is open

    asyncio.run(main())
    assert sleep.waits == [MIC_RETRY_S[0]]


def test_each_wait_is_longer_until_the_last_which_then_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A microphone that stays shut is asked for less and less often, never given up on.

    Nor are the recognizer's models loaded again for each try.
    """
    laps = len(MIC_RETRY_S) + 2
    backend = FakeBackend()
    backend.fail_starts(*[BackendStartStatus.FAILED_CLOSED] * laps)
    heard_session(monkeypatch, lambda: backend)
    recognizers: list[object] = []
    ears = voice_asr.SenseVoiceRecognizer  # the stand-in the session was given

    def counted(**kwargs: Any) -> object:  # noqa: ANN401
        recognizers.append(object())
        return ears(**kwargs)

    monkeypatch.setattr(voice_asr, "SenseVoiceRecognizer", counted)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            for _ in range(laps):
                await sleep.parked()
                sleep.release()
            await _listens(task)

    asyncio.run(main())
    assert sleep.waits == [*MIC_RETRY_S, *[MIC_RETRY_S[-1]] * (laps - len(MIC_RETRY_S))]
    assert len(recognizers) == 1


def test_a_wake_of_the_mac_opens_the_microphone_without_waiting_out_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system's wake is the observer's hook, as a listening terminal's coordinator gets it."""
    backend = FakeBackend()
    backend.fail_starts(BackendStartStatus.FAILED_CLOSED)
    power = heard_session(monkeypatch, lambda: backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            await sleep.parked()
            assert power.hooks is not None
            _before_sleep, on_wake = power.hooks
            on_wake()
            await _listens(task)  # the wait was never released
            assert backend.start_count == 2
            assert power.shutdowns == 1  # the deaf terminal's observer gave way to the session's
            assert power.installs == 2

    asyncio.run(main())
    assert sleep.waits == [MIC_RETRY_S[0]]


def test_a_change_of_the_devices_opens_the_microphone_and_a_poll_that_finds_none_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0054's look at the devices to be on, as the daemon takes it, ends the wait early."""
    backend = FakeBackend()
    backend.fail_starts(BackendStartStatus.FAILED_CLOSED)
    heard_session(monkeypatch, lambda: backend)
    default_mic = [1]
    monkeypatch.setattr(
        voice_backend, "coreaudio_devices",
        lambda kind: (default_mic[0] if kind == "input" else 9, {}),
    )
    poll = SteppedSleep()
    monkeypatch.setattr(
        terminal_module, "start_device_change_watch",
        functools.partial(devices.start_device_change_watch, sleep=poll),
    )
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            await sleep.parked()
            await poll.parked()  # the watch has looked once
            poll.release()
            await poll.parked()  # and again: nothing changed
            assert not task.done()
            assert backend.start_count == 1
            default_mic[0] = 2  # the microphone the system defaults to has changed
            poll.release()
            await _listens(task)
            assert backend.start_count == 2

    asyncio.run(main())
    assert sleep.waits == [MIC_RETRY_S[0]]


def test_a_close_that_was_not_confirmed_is_never_followed_by_a_second_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A second owner beside one that may still hold the microphone would fight it (ADR 0006).

    The terminal does not wait, does not listen for a wake or a device, and says so, with the
    state and the reason of the open that failed.
    """
    backend = FakeBackend(stop_status=BackendStopStatus.CLOSE_UNCERTAIN)
    backend.fail_starts(BackendStartStatus.OPEN_UNCERTAIN)
    power = heard_session(monkeypatch, lambda: backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            assert await asyncio.wait_for(task, _GUARD_S) is None

    with caplog.at_level(logging.INFO, logger="jarvis"):
        asyncio.run(main())
    assert sleep.waits == []
    assert backend.start_count == 1
    assert power.installs == 0  # nothing to wake it: a wake would find the same refusal
    said = _said(caplog)
    assert len(said) == 1
    assert "close_uncertain" in said[0]
    assert "injected_open_uncertain" in said[0]
    assert next(
        record for record in caplog.records if record.name == "jarvis.runtime.terminal"
        and record.levelno >= logging.WARNING
    ).levelno == logging.ERROR  # a terminal that stays deaf until restarted is an error


def test_a_terminal_that_ends_while_it_waits_ends_at_once_and_takes_its_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is not the 15 s a capture session in the middle of opening is given."""
    backend = FakeBackend()
    backend.fail_starts(BackendStartStatus.FAILED_CLOSED)
    power = heard_session(monkeypatch, lambda: backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            await sleep.parked()
            assert power.shutdowns == 0
            await asyncio.wait_for(_stop_listening(task, speech.retry), _GUARD_S)
            assert task.done()
            assert task.result() is None
            assert power.shutdowns == 1
            assert power.log is not None

    asyncio.run(main())
    assert backend.start_count == 1


class _Raises:
    """A stand-in that fails with ``text`` where it is called."""

    def __init__(self, text: str, error: type[Exception] = RuntimeError) -> None:
        self.text, self.error = text, error

    def __call__(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise self.error(self.text)


class _VadThatCannotBeBuilt:
    thresholds = staticmethod(voice_audio.SileroVad.thresholds)  # what the knobs read at start-up

    def __init__(self, **_ignored: object) -> None:
        msg = "the-vad-is-gone"
        raise RuntimeError(msg)


class _WakeThatCannotStart:
    def start(self) -> NoReturn:
        msg = "the-wake-model-is-gone"
        raise RuntimeError(msg)

    def close(self) -> None:
        return None


class _EarsThatCannotWarm:
    def __init__(self, **_ignored: object) -> None:
        pass

    def prewarm(self) -> NoReturn:
        msg = "the-recognizer-cannot-warm"
        raise RuntimeError(msg)


def _models_missing(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(
        terminal_module, "voice_models_preflight", lambda **_k: (False, ["silero-is-missing"]),
    )


def _wake_cannot_start(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(voice_wake, "MicroWakeWordEngine", _WakeThatCannotStart)


def _recognizer_cannot_warm(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(voice_asr, "SenseVoiceRecognizer", _EarsThatCannotWarm)


def _vad_cannot_be_built(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(voice_audio, "SileroVad", _VadThatCannotBeBuilt)


def _session_cannot_prepare(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(
        voice_session.DuplexVoiceSession, "start",
        _Raises("the-session-cannot-prepare", voice_session.VoiceSessionPreDeviceError),
    )


def _session_cannot_start(monkeypatch: pytest.MonkeyPatch, _backend: FakeBackend) -> None:
    monkeypatch.setattr(
        voice_session.DuplexVoiceSession, "start", _Raises("the-session-cannot-start"),
    )


def _dictation_cannot_be_built(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> None:
    del backend
    monkeypatch.setattr(terminal_module, "build_dictation", _Raises("dictation-is-gone"))


def _open_fails(status: BackendStartStatus) -> Callable[[pytest.MonkeyPatch, FakeBackend], None]:
    def fail(_monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> None:
        backend.fail_starts(status)

    return fail


def _open_may_hold(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> None:
    del monkeypatch
    backend.stop_status = BackendStopStatus.CLOSE_UNCERTAIN
    backend.fail_starts(BackendStartStatus.OPEN_UNCERTAIN)


_DEAF_PATHS: list[tuple[str, Callable[[pytest.MonkeyPatch, FakeBackend], None], list[str]]] = [
    ("models missing", _models_missing, ["silero-is-missing"]),
    ("wake model cannot start", _wake_cannot_start, ["the-wake-model-is-gone"]),
    ("recognizer cannot warm", _recognizer_cannot_warm, ["the-recognizer-cannot-warm"]),
    ("vad cannot be built", _vad_cannot_be_built, ["the-vad-is-gone"]),
    ("session cannot prepare", _session_cannot_prepare, ["the-session-cannot-prepare"]),
    ("session cannot start", _session_cannot_start, ["the-session-cannot-start"]),
    ("dictation cannot be built", _dictation_cannot_be_built, ["dictation-is-gone"]),
    (
        "open failed",
        _open_fails(BackendStartStatus.FAILED_CLOSED),
        ["local_capture_unavailable", "injected_failed_closed"],
    ),
    (
        "microphone busy",
        _open_fails(BackendStartStatus.OWNER_BUSY),
        ["local_capture_unavailable", "injected_owner_busy"],
    ),
    (
        "open may hold the microphone",
        _open_may_hold,
        ["close_uncertain", "injected_open_uncertain"],
    ),
]


@pytest.mark.parametrize(
    ("setup", "reasons"),
    [pytest.param(setup, reasons, id=name) for name, setup, reasons in _DEAF_PATHS],
)
def test_every_way_a_terminal_ends_up_unable_to_listen_says_why_in_its_own_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    setup: Callable[[pytest.MonkeyPatch, FakeBackend], None],
    reasons: list[str],
) -> None:
    """The line of the terminal, not only the lines the layers under it wrote.

    On 2026-10-21 the layers' lines were lost and the terminal's said only that the microphone
    could not be opened. Whatever the cause, its line carries the state and the reason.
    """
    backend = FakeBackend()
    heard_session(monkeypatch, lambda: backend)
    setup(monkeypatch, backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            task = _start(speaking, speech, sleep)
            waiting = asyncio.ensure_future(sleep.parked())
            await asyncio.wait(
                {task, waiting}, timeout=_GUARD_S, return_when=asyncio.FIRST_COMPLETED,
            )
            assert task.done() or waiting.done(), "the terminal neither gave up nor waited"
            waiting.cancel()
            if task.done():
                assert task.result() is None

    with caplog.at_level(logging.INFO, logger="jarvis"):
        asyncio.run(main())
    said = "\n".join(_said(caplog))
    for reason in reasons:
        assert reason in said


def test_a_terminal_that_listens_the_first_time_waits_for_nothing_and_watches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry costs a terminal whose microphone opens nothing: one observer, no wait."""
    backend = FakeBackend()
    power = heard_session(monkeypatch, lambda: backend)
    sleep = SteppedSleep()

    async def main() -> None:
        async with terminal_without_a_brain(tmp_path / "root") as (speaking, speech):
            await _listens(_start(speaking, speech, sleep))
            assert power.installs == 1

    asyncio.run(main())
    assert sleep.waits == []
    assert backend.start_count == 1

