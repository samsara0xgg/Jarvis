"""ADR-0005 §4.2 / spec §3.6.10 — refcounted macOS output ducking.

While the microphone is open for wake-detection or PTT capture, the system
output must be silenced to prevent the assistant's own TTS playback from being
heard back as user speech. ``SystemAudioDucker`` wraps the legacy
``core/media_ducking.py`` behavior with three adjustments:

1. ``_run_osascript`` is exposed as a module-level callable so test code can
   ``patch.object`` it without instantiating an alternate runner.
2. ``duck()`` is refcounted: nested ``duck()``/``restore()`` pairs make only
   one real osascript restore on the outermost release. Inner ``restore()``
   decrements the depth without touching the OS.
3. TTS can hold an output lease across provider I/O and queued playback;
   ``duck()`` refuses to mute while such a lease exists.

Layer placement: L5 surface. Only stdlib imports.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import Iterator

LOGGER = logging.getLogger(__name__)

# Tests set this via ``monkeypatch.setattr`` to force darwin behavior on linux CI.
_PLATFORM_OVERRIDE: str | None = None

# Number of comma-separated parts in a parsed osascript volume settings line.
_SNAPSHOT_FIELDS = 2
_OUTPUT_LEASE_WAIT_S = 2.5

type RestoreState = Literal["ready", "restoring", "failed"]


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
    """Arbitrate assistant output against temporary macOS output muting."""

    def __init__(self, *, enabled: bool = True) -> None:
        """Initialize with the given enable flag; lock + depth + snapshot reset."""
        self.enabled = enabled
        self._lock = threading.Lock()
        self._output_idle = threading.Condition(self._lock)
        self._depth = 0
        self._output_depth = 0
        self._snapshot: VolumeSnapshot | None = None
        self._restore_state: RestoreState = "ready"

    @property
    def active(self) -> bool:
        """Return True while output is muted, restoring, or failed closed."""
        with self._lock:
            return self._depth > 0 or self._restore_state != "ready"

    @property
    def restore_state(self) -> RestoreState:
        """Expose the output-readiness state for health checks and live burns."""
        with self._lock:
            return self._restore_state

    def duck(self) -> bool:
        """Mute system output; idempotent under refcount. Returns True on success."""
        if not self.enabled or not _available():
            return False

        with self._lock:
            if self._restore_state != "ready":
                record_realtime_trace(
                    "system_output_duck_refused",
                    reason=f"restore_{self._restore_state}",
                )
                return False
            # TTS registers its provider-I/O + playback lifetime here before
            # producing PCM. Refuse to mute under the same lock so a wake
            # capture cannot silence an answer that is about to play.
            if self._output_depth > 0:
                return False
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

    def enter_output(self, *, timeout_s: float = _OUTPUT_LEASE_WAIT_S) -> bool:
        """Acquire an output lease only after macOS restore is known successful.

        A failed restore is fail-closed: the caller is told that output is not
        safe instead of synthesizing into a still-muted system.  A hung restore
        is bounded by ``timeout_s`` for the same reason.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        blocked_reason: str | None = None
        with self._output_idle:
            while self._depth > 0 or self._restore_state == "restoring":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    blocked_reason = (
                        "restore_timeout"
                        if self._restore_state == "restoring"
                        else "duck_timeout"
                    )
                    break
                self._output_idle.wait(timeout=remaining)
            if blocked_reason is None and self._restore_state == "failed":
                blocked_reason = "restore_failed"
            if blocked_reason is None:
                self._output_depth += 1
                return True

        record_realtime_trace(
            "system_output_lease_refused",
            reason=blocked_reason,
            timeout_s=timeout_s,
        )
        return False

    def leave_output(self) -> None:
        """Release one output lease acquired by :meth:`enter_output`."""
        with self._output_idle:
            if self._output_depth <= 0:
                msg = "leave_output called without an active output lease"
                raise RuntimeError(msg)
            self._output_depth -= 1
            if self._output_depth == 0:
                self._output_idle.notify_all()

    def restore(self) -> None:
        """Decrement refcount; on outermost release, restore pre-duck volume/muted."""
        with self._output_idle:
            if self._depth <= 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            snapshot = self._snapshot
            if snapshot is None:
                self._restore_state = "ready"
                self._output_idle.notify_all()
                return
            self._restore_state = "restoring"

        record_realtime_trace("system_output_restore_started", restore_kind="balanced")
        self._complete_restore(snapshot, restore_kind="balanced")

    def _complete_restore(
        self,
        snapshot: VolumeSnapshot,
        *,
        restore_kind: str,
    ) -> None:
        """Run one restore subprocess, then atomically publish its result."""
        try:
            _run_osascript(_build_restore_script(snapshot))
        except Exception as exc:  # noqa: BLE001 — errno/subprocess failures share fail-closed handling.
            with self._output_idle:
                # Retain the snapshot so restore_all() can retry synchronously.
                self._restore_state = "failed"
                self._output_idle.notify_all()
            record_realtime_trace(
                "system_output_restore_completed",
                restore_kind=restore_kind,
                success=False,
                failure_type=type(exc).__name__,
                output_ready=False,
            )
            LOGGER.warning(
                "[audio-ducking] failed to restore system output; output leases fail closed",
                exc_info=True,
            )
            return

        with self._output_idle:
            self._snapshot = None
            self._restore_state = "ready"
            self._output_idle.notify_all()
        record_realtime_trace(
            "system_output_restore_completed",
            restore_kind=restore_kind,
            success=True,
            output_ready=True,
        )

    def restore_all(self) -> None:
        """Force-restore regardless of refcount depth (e.g. on shutdown)."""
        with self._output_idle:
            if self._restore_state == "restoring":
                completed = self._output_idle.wait_for(
                    lambda: self._restore_state != "restoring",
                    timeout=_OUTPUT_LEASE_WAIT_S,
                )
                if not completed:
                    record_realtime_trace(
                        "system_output_restore_all_bounded",
                        reason="restore_in_progress_timeout",
                        output_ready=False,
                    )
                    return
            snapshot = self._snapshot
            self._depth = 0
            if snapshot is None:
                self._restore_state = "ready"
                self._output_idle.notify_all()
                return
            self._restore_state = "restoring"

        record_realtime_trace("system_output_restore_started", restore_kind="forced")
        self._complete_restore(snapshot, restore_kind="forced")

    @contextlib.contextmanager
    def duck_scope(self) -> Iterator[None]:
        """``with d.duck_scope():`` blocks the body with system output muted."""
        self.duck()
        try:
            yield
        finally:
            self.restore()
