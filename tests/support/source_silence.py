"""A runtime root as the Mac fills it: TimeSink's copy (``imports/shipped.db``), the lifelog pieces.

The copy is written through the brain's own intake (``Shipments.receive_entries``) and the pieces
are named by the archive's own ``piece_name``, so a fixture is what a Mac can send. Time is
absolute (``NOW`` of ``tests.support.check_log``); nothing waits.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.state.lifelog import AUDIO_DIRNAME, LIFELOG_DIRNAME, piece_name
from jarvis.state.shipments import Shipments
from tests.support.check_log import MIN, NOW

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

HOUR = 60 * MIN
DAY = 24 * HOUR
MARKER = "WORDS-ON-HIS-SCREEN"
"""Text the copy carries (a window title, a screen's text); a report must never repeat it."""


def utc_text(epoch_ms: int) -> str:
    """The text TimeSink's ``DATETIME`` columns hold: ``2026-10-19 05:01:47.282``."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class MacWorld:
    """What the Mac has sent so far; ``ship`` puts the TimeSink rows on the brain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lines: list[dict[str, object]] = []
        self._count = 0

    def _add(self, kind: str, at_ms: int, fields: dict[str, object], text: str = "") -> None:
        self._count += 1
        self._lines.append({
            "key": f"{kind}:{self._count}:{'0' * 12}", "kind": kind, "zone": "UTC",
            "at_ms": at_ms, "text": text, "fields": {"id": self._count, **fields},
        })

    def state(self, at_ms: int, kind: str) -> None:
        """A TimeSink state event: ``lock``, ``unlock``, ``sleep``, ``wake``, ``idle``..."""
        self._add("stateEvent", at_ms, {"at": utc_text(at_ms), "kind": kind})

    def span(self, at_ms: int) -> None:
        """A span that starts at ``at_ms``."""
        self._add(
            "span", at_ms,
            {"start": utc_text(at_ms), "end": utc_text(at_ms + MIN), "title": MARKER},
            MARKER,
        )

    def ocr(self, at_ms: int, *, ok: int = 10, failed: int = 0) -> None:
        """A 10-minute capture row with ``ok`` OCR runs that worked and ``failed`` that did not."""
        self._add(
            "captureHealth", at_ms,
            {
                "windowStart": utc_text(at_ms), "windowEnd": utc_text(at_ms + 10 * MIN),
                "ocrRuns": ok + failed, "ocrFailed": failed, "checks": ok + failed,
            },
        )

    def piece(self, end_ms: int, device: str = "macbook", length_ms: int = 10 * MIN) -> None:
        """A kept recording piece that ended at ``end_ms``, as the archive files it."""
        start = end_ms - length_ms
        day = datetime.fromtimestamp(start / 1000, tz=UTC).strftime("%Y-%m-%d")
        folder = self.root / LIFELOG_DIRNAME / AUDIO_DIRNAME / device / day
        folder.mkdir(parents=True, exist_ok=True)
        (folder / piece_name(start, length_ms, f"p{end_ms}", "m4a")).write_bytes(b"\0")

    def ship(self) -> None:
        """Put the TimeSink rows written so far into ``imports/shipped.db``."""
        body = b"\n".join(json.dumps(one).encode() for one in self._lines)
        Shipments(self.root / "imports", min_free_bytes=0).receive_entries("timesink", body)
        self._lines = []

    def live(
        self, start_ms: int, end_ms: int, *,
        dark: dict[str, Iterable[tuple[int, int]]] | None = None,
        failing: Iterable[tuple[int, int]] = (),
    ) -> None:
        """Spans every 5 minutes, a capture row every 10 and a piece every 10, from ``start_ms``.

        ``dark`` names, per source (``spans``, ``ocr``, ``mic``), spans of time with no arrival at
        all; ``failing`` are spans where the capture rows come but every OCR run fails.
        """
        dark = dark or {}
        failing = list(failing)

        def inside(name: str, at: int) -> bool:
            return any(a <= at < b for a, b in dark.get(name, ()))

        for at in range(start_ms, end_ms, 5 * MIN):
            if not inside("spans", at):
                self.span(at)
        for at in range(start_ms, end_ms, 10 * MIN):
            if not inside("ocr", at):
                broken = any(a <= at < b for a, b in failing)
                self.ocr(at, ok=0 if broken else 10, failed=10 if broken else 0)
            if not inside("mic", at):
                self.piece(at + 10 * MIN)


def window_start() -> int:
    """Two days before the instant the check runs."""
    return NOW - 2 * DAY
