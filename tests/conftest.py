"""Pytest session bootstrap.

Two layers of defence against tests producing audible side effects on
the developer machine:

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

Tests that exercise voice paths (``test_voice_*``, integration
``test_wake_listener_wiring`` etc.) already ``patch.object`` these seams
locally; the session fixture and the local patches stack cleanly because
both target the same module attributes.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import inherent_loop
from jarvis.surface import notify, voice_audio, voice_ducking, voice_tts

if TYPE_CHECKING:
    from collections.abc import Iterator


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
