"""ADR-0005 voice_ducking — refcounted AppleScript mute/restore."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.surface import voice_ducking


@pytest.fixture(autouse=True)
def _force_darwin_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the module to think it is on darwin even when tests run on linux CI.
    monkeypatch.setattr(voice_ducking, "_PLATFORM_OVERRIDE", "darwin", raising=False)


def test_single_duck_restore_cycle_invokes_osascript_twice() -> None:
    """One duck, one restore => exactly two osascript subprocess invocations."""
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        d.duck()
        d.restore()
        assert run.call_count == 2


def test_nested_duck_restore_uses_refcount() -> None:
    """Two ducks + two restores => only ONE actual mute and ONE restore."""
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        d.duck()
        d.duck()
        d.restore()
        assert run.call_count == 2, "inner restore must not call osascript"
        d.restore()
        assert run.call_count == 3, "outer restore restores OS state"


def test_context_manager_duck_yields_to_caller() -> None:
    """``duck_scope()`` is a context manager that ducks on enter and restores on exit."""
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        with d.duck_scope():
            pass
        assert run.call_count == 2
