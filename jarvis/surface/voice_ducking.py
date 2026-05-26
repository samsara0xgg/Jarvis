"""ADR-0005 §4.2 / spec §3.6.10 — refcounted macOS output ducking.

While the microphone is open for wake-detection or PTT capture, the system
output must be silenced to prevent the assistant's own TTS playback from being
heard back as user speech. ``SystemAudioDucker`` wraps the legacy
``core/media_ducking.py`` behavior with two adjustments:

1. ``_run_osascript`` is exposed as a module-level callable so test code can
   ``patch.object`` it without instantiating an alternate runner.
2. ``duck()`` is refcounted: nested ``duck()``/``restore()`` pairs make only
   one real osascript restore on the outermost release. Inner ``restore()``
   decrements the depth without touching the OS.

Layer placement: L5 surface. Only stdlib imports.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

LOGGER = logging.getLogger(__name__)

# Tests set this via ``monkeypatch.setattr`` to force darwin behavior on linux CI.
_PLATFORM_OVERRIDE: str | None = None

# Number of comma-separated parts in a parsed osascript volume settings line.
_SNAPSHOT_FIELDS = 2


def _run_osascript(script: str) -> str:
    """Invoke ``/usr/bin/osascript -e <script>`` and return trimmed stdout.

    Module-level so tests can ``patch.object(voice_ducking, "_run_osascript")``.
    """
    result = subprocess.run(  # noqa: S603 — fixed argv; osascript is the macOS API contract.
        ["/usr/bin/osascript", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return result.stdout.strip()


def _available() -> bool:
    """Return True iff the current platform supports osascript ducking."""
    sysname = _PLATFORM_OVERRIDE or platform.system().lower()
    return sysname == "darwin"


@dataclass(frozen=True)
class VolumeSnapshot:
    """Captured system output state to restore after ducking."""

    output_volume: int
    output_muted: bool


def _parse_snapshot(raw: str) -> VolumeSnapshot:
    """Parse osascript output into a ``VolumeSnapshot``.

    Accepts both legacy bare form (``"50,false"``) and labeled form
    (``"output volume:50, output muted:false"``) — the labeled form is what
    the unit tests inject as the mock return value.
    """
    parts = [part.strip() for part in raw.strip().split(",")]
    if len(parts) != _SNAPSHOT_FIELDS:
        msg = f"unexpected volume settings: {raw!r}"
        raise ValueError(msg)

    def _value(token: str) -> str:
        return token.split(":", 1)[1].strip() if ":" in token else token

    volume_raw = _value(parts[0])
    muted_raw = _value(parts[1])
    return VolumeSnapshot(
        output_volume=max(0, min(100, int(float(volume_raw)))),
        output_muted=muted_raw.lower() == "true",
    )


# First duck: capture the current volume + muted state AND silence the output
# in one osascript invocation, so the duck path is exactly one subprocess hop.
_DUCK_AND_SNAPSHOT_SCRIPT = """
set s to get volume settings
set v to (output volume of s as text)
set m to (output muted of s as text)
set volume output volume 0
try
  set volume output muted true
end try
return "output volume:" & v & ", output muted:" & m
"""

# Subsequent ducks (refcount > 0): re-assert mute without re-reading the
# snapshot, preserving the original pre-duck state captured on the first call.
_DUCK_REASSERT_SCRIPT = """
set volume output volume 0
try
  set volume output muted true
end try
"""


def _build_restore_script(snapshot: VolumeSnapshot) -> str:
    """Render the osascript that returns output volume + muted to ``snapshot``."""
    muted = "true" if snapshot.output_muted else "false"
    return (
        f"set volume output volume {snapshot.output_volume}\n"
        f"try\n"
        f"  set volume output muted {muted}\n"
        f"end try\n"
    )


class SystemAudioDucker:
    """Temporarily silence macOS system output and restore it exactly once."""

    def __init__(self, *, enabled: bool = True) -> None:
        """Initialize with the given enable flag; lock + depth + snapshot reset."""
        self.enabled = enabled
        self._lock = threading.Lock()
        self._depth = 0
        self._snapshot: VolumeSnapshot | None = None

    @property
    def active(self) -> bool:
        """Return True iff at least one outstanding ``duck()`` has not been ``restore``d."""
        with self._lock:
            return self._depth > 0

    def duck(self) -> bool:
        """Mute system output; idempotent under refcount. Returns True on success."""
        if not self.enabled or not _available():
            return False

        with self._lock:
            if self._depth > 0:
                # Nested duck: re-assert mute (one osascript call) but keep the
                # original snapshot so restore_all returns to pre-duck state.
                try:
                    _run_osascript(_DUCK_REASSERT_SCRIPT)
                except Exception:  # noqa: BLE001 — osascript can fail with many errno variants; debug-log + continue.
                    LOGGER.debug("[audio-ducking] re-assert mute failed", exc_info=True)
                self._depth += 1
                return True

            try:
                raw = _run_osascript(_DUCK_AND_SNAPSHOT_SCRIPT)
                self._snapshot = _parse_snapshot(raw)
            except Exception:  # noqa: BLE001 — osascript can fail with many errno variants; log + bail.
                self._snapshot = None
                LOGGER.warning("[audio-ducking] failed to duck system output", exc_info=True)
                return False

            self._depth = 1
            return True

    def restore(self) -> None:
        """Decrement refcount; on outermost release, restore pre-duck volume/muted."""
        with self._lock:
            if self._depth <= 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            snapshot = self._snapshot
            self._snapshot = None

        if snapshot is None:
            return
        try:
            _run_osascript(_build_restore_script(snapshot))
        except Exception:  # noqa: BLE001 — osascript can fail with many errno variants; log + continue.
            LOGGER.warning("[audio-ducking] failed to restore system output", exc_info=True)

    def restore_all(self) -> None:
        """Force-restore regardless of refcount depth (e.g. on shutdown)."""
        with self._lock:
            snapshot = self._snapshot
            self._depth = 0
            self._snapshot = None

        if snapshot is None:
            return
        try:
            _run_osascript(_build_restore_script(snapshot))
        except Exception:  # noqa: BLE001 — osascript can fail with many errno variants; log + continue.
            LOGGER.warning("[audio-ducking] failed to restore system output", exc_info=True)

    @contextlib.contextmanager
    def duck_scope(self) -> Iterator[None]:
        """``with d.duck_scope():`` blocks the body with system output muted."""
        self.duck()
        try:
            yield
        finally:
            self.restore()
