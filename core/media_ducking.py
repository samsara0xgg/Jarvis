"""System output ducking helpers for microphone capture."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

ScriptRunner = Callable[[str], str]


@dataclass(frozen=True)
class VolumeSnapshot:
    output_volume: int
    output_muted: bool


class SystemAudioDucker:
    """Temporarily silence macOS system output and restore it exactly once."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        platform: str | None = None,
        runner: ScriptRunner | None = None,
    ) -> None:
        self.enabled = enabled
        self.platform = platform or sys.platform
        self._runner = runner or self._run_osascript
        self._lock = threading.Lock()
        self._depth = 0
        self._snapshot: VolumeSnapshot | None = None

    @classmethod
    def from_config(cls, config: Any) -> "SystemAudioDucker":
        audio_config = config.get("audio_ducking", {}) if isinstance(config, dict) else {}
        if not isinstance(audio_config, dict):
            audio_config = {}
        return cls(enabled=bool(audio_config.get("enabled", True)))

    @property
    def active(self) -> bool:
        with self._lock:
            return self._depth > 0

    def duck(self) -> bool:
        if not self._available():
            return False

        with self._lock:
            if self._depth > 0:
                self._depth += 1
                return True

            snapshot: VolumeSnapshot | None = None
            try:
                snapshot = self._read_snapshot()
                self._snapshot = snapshot
                self._runner(
                    """
                    set volume output volume 0
                    try
                      set volume output muted true
                    end try
                    """
                )
            except Exception:
                if snapshot is not None:
                    try:
                        self._restore_snapshot(snapshot)
                    except Exception:
                        LOGGER.debug("[audio-ducking] rollback restore failed", exc_info=True)
                self._snapshot = None
                LOGGER.warning("[audio-ducking] failed to duck system output", exc_info=True)
                return False

            self._depth = 1
            return True

    def restore(self) -> None:
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
            self._restore_snapshot(snapshot)
        except Exception:
            LOGGER.warning("[audio-ducking] failed to restore system output", exc_info=True)

    def restore_all(self) -> None:
        with self._lock:
            snapshot = self._snapshot
            self._depth = 0
            self._snapshot = None

        if snapshot is None:
            return
        try:
            self._restore_snapshot(snapshot)
        except Exception:
            LOGGER.warning("[audio-ducking] failed to restore system output", exc_info=True)

    def _available(self) -> bool:
        return self.enabled and self.platform == "darwin"

    def _read_snapshot(self) -> VolumeSnapshot:
        raw = self._runner(
            """
            set s to get volume settings
            return (output volume of s as text) & "," & (output muted of s as text)
            """
        )
        return self.parse_snapshot(raw)

    def _restore_snapshot(self, snapshot: VolumeSnapshot) -> None:
        muted = "true" if snapshot.output_muted else "false"
        self._runner(
            f"""
            set volume output volume {snapshot.output_volume}
            try
              set volume output muted {muted}
            end try
            """
        )

    @staticmethod
    def parse_snapshot(raw: str) -> VolumeSnapshot:
        parts = [part.strip() for part in raw.strip().split(",")]
        if len(parts) != 2:
            raise ValueError(f"unexpected volume settings: {raw!r}")
        return VolumeSnapshot(
            output_volume=max(0, min(100, int(float(parts[0])))),
            output_muted=parts[1].lower() == "true",
        )

    @staticmethod
    def _run_osascript(script: str) -> str:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
