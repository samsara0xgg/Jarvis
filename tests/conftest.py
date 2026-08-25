"""Pytest session bootstrap.

Three layers of defence against tests reaching real machine resources —
audio devices, and (ADR-0009) the kernel's power-notification port:

1. **Env clamp** — unset ``MINIMAX_API_KEY`` and set
   ``JARVIS_VOICE_DISABLE_WAKE=1`` so the ADR-0005 §12 voice subsystem
   short-circuits inside ``serve_inherent`` (no TTS pipeline construction,
   no wake-listener spawn).
2. **Seam patch** — replace every module-level PortAudio / AppleScript
   factory with a hard failure stub. Any test path that bypasses the env
   clamp (e.g. directly constructs ``AudioStreamPlayer(lazy_open=False)``
   or calls ``capture_utterance`` without patching ``_open_input_stream``)
   immediately raises instead of silently opening the device.

Why this lives at session scope:

- The developer shell sets ``MINIMAX_API_KEY``; the repo also ships
  ``data/sensevoice-small-int8`` + ``data/silero_vad.onnx`` symlinks, so
  the ADR-0005 §12 preflight passes inside the test process and the
  daemon would otherwise open real audio devices during unit tests
  (e.g. ``test_serve_inherent_spawns_response_watcher_under_named_task``).
- Real device opens cost two things tests cannot afford: (1) audible
  speaker clicks, mic activity, and macOS volume-change tick sounds on
  every ``pytest`` run, and (2) a daemon-thread wake listener that
  keeps polling ``sd.RawInputStream`` after the test exits — that thread
  has been observed segfaulting later tests when the device is contested
  by an external ``jarvis serve``.

3. **Power-observer seam** — replace
   :func:`jarvis.deployment.sleep_wake._real_observer_factory` with an
   inert stub, so no test registers with IOKit. ADR-0009 Step 4 wired
   :func:`~jarvis.deployment.sleep_wake.install_power_observer` into
   ``serve_inherent``; without this, every test that boots the daemon
   (``test_serve_inherent_spawns_response_watcher_under_named_task``,
   integration ``test_wake_listener_wiring``) calls a real
   ``IORegisterForSystemPower`` and starts a real ``jarvis-power-observer``
   CFRunLoop thread. Those tests pass either way, but a Mac that sleeps
   mid-``pytest`` would then deliver a genuine kernel notification into
   the test process, which marshals a ``mac.sleeping`` emit plus
   ``reconcile_after_wake`` into whichever temp event log happens to be
   live — an unreproducible cross-test flake.

Tests that exercise voice paths (``test_voice_*``, integration
``test_wake_listener_wiring`` etc.) already ``patch.object`` these seams
locally; the session fixture and the local patches stack cleanly because
both target the same module attributes. The same is true of the
power-observer seam: ADR-0009 Step 4's ``serve_inherent`` tests patch
``inherent_loop.install_power_observer`` one layer above it, and the
``sleep_wake`` unit tests pass an explicit ``observer_factory=`` (which
takes precedence over the module default by contract — see
``tests/canary/test_canary_power_observer_seam.py``). A test that
genuinely needs the real factory must restore it explicitly.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import sleep_wake
from jarvis.runtime import inherent_loop
from jarvis.surface import notify, voice_audio, voice_ducking, voice_tts

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class _InertPowerObserver:
    """Satisfies :class:`jarvis.deployment.sleep_wake.PowerObserver`, does nothing.

    Stands in for :class:`~jarvis.deployment.sleep_wake._IOKitPowerObserver`
    so ``install_power_observer`` still runs for real — callbacks wired,
    ``loop=`` marshaling applied, teardown exercised — while nothing
    registers with IOKit and no CFRunLoop thread starts. The callbacks are
    kept (not dropped) so a test that wants to fire them can, exactly as
    ``StubPowerObserver`` does in ``tests/unit/test_sleep_wake.py``.
    """

    def __init__(self) -> None:
        """Initialize with no callbacks wired."""
        self.before_sleep: Callable[[], None] | None = None
        self.on_wake: Callable[[], None] | None = None
        self.shutdown_called = False

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Record the callbacks; subscribe to nothing."""
        self.before_sleep = before_sleep
        self.on_wake = on_wake

    def shutdown(self) -> None:
        """Nothing to de-register; record the call."""
        self.shutdown_called = True


class _RealAudioDeviceBlockedError(RuntimeError):
    """Raised when a test attempts to touch a real PortAudio/osascript seam."""


def _block_audio_seam(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
    """Replacement for any audio-device factory; refuses to open the device."""
    msg = (
        "Test attempted to open a real audio device (PortAudio or osascript). "
        "Patch the relevant seam in your test fixture: "
        "voice_audio._open_input_stream, voice_tts._open_output_stream, "
        "inherent_loop._open_wake_input_stream, voice_ducking._run_osascript."
    )
    raise _RealAudioDeviceBlockedError(msg)


@pytest.fixture(scope="session", autouse=True)
def _voice_subsystem_disabled_in_tests() -> Iterator[None]:
    """Clamp env so ``serve_inherent`` skips TTS construction and wake spawn."""
    saved_minimax = os.environ.pop("MINIMAX_API_KEY", None)
    saved_disable_wake = os.environ.get("JARVIS_VOICE_DISABLE_WAKE")
    os.environ["JARVIS_VOICE_DISABLE_WAKE"] = "1"
    try:
        yield
    finally:
        if saved_minimax is not None:
            os.environ["MINIMAX_API_KEY"] = saved_minimax
        if saved_disable_wake is None:
            os.environ.pop("JARVIS_VOICE_DISABLE_WAKE", None)
        else:
            os.environ["JARVIS_VOICE_DISABLE_WAKE"] = saved_disable_wake


@pytest.fixture(autouse=True)
def _no_real_power_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the kernel's power-notification port (ADR-0009).

    Patches the FACTORY, not ``install_power_observer`` itself, for two
    reasons: the install's own logic (callback wiring, ``loop=``
    marshaling, the partial-registration unwind) stays under test, and
    every caller is covered at once — ``serve_inherent`` and the
    ``cli`` fork-detach child alike — with no test-detection branch in
    production code.
    """
    monkeypatch.setattr(sleep_wake, "_real_observer_factory", _InertPowerObserver)


@pytest.fixture(autouse=True)
def _block_real_audio_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail any test path that reaches a real audio device."""
    # PortAudio seams (mic + speaker). Tests that touch the recorder /
    # player wrap their own ``patch.object`` for these; the session-wide
    # block here only fires when a test path forgot to mock.
    monkeypatch.setattr(voice_audio, "_open_input_stream", _block_audio_seam)
    monkeypatch.setattr(voice_tts, "_open_output_stream", _block_audio_seam)
    monkeypatch.setattr(inherent_loop, "_open_wake_input_stream", _block_audio_seam)
    # Ducker's AppleScript seam — replace with a safe stub returning a
    # parseable snapshot so any accidental ``duck()`` call neither mutes
    # the real system (which plays a macOS volume-change tick sound) nor
    # crashes the test by failing the snapshot parse.
    monkeypatch.setattr(
        voice_ducking,
        "_run_osascript",
        lambda _script: "output volume:50, output muted:false",
    )
    # Surface delivery primitives (``say`` voice + osascript notification
    # banner) — these are the OTHER macOS audio paths. ``render_response``
    # tests can still assert that the dispatch was *attempted* because
    # they patch ``subprocess.Popen`` / ``subprocess.run`` at the module
    # level themselves; this fallback ensures any test path that misses
    # that mock does not actually invoke ``/usr/bin/say`` or
    # ``/usr/bin/osascript``.
    monkeypatch.setattr(notify, "deliver_voice", lambda *_a, **_kw: None)
    monkeypatch.setattr(notify, "deliver_banner", lambda *_a, **_kw: None)
    # F7 ``macos_say`` fallback inside the TTS pipeline.
    monkeypatch.setattr(voice_tts, "macos_say_fallback", lambda *_a, **_kw: None)
