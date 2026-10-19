"""The Mac ships the day reports of his Claude Code work whole (ADR 0299).

``<runtime root>/claude-code-days/<date>.md`` (top level only; ``material/`` beside them is the
input of the report task and never leaves the Mac) is sent under source ``claude-code-daily`` when
its bytes differ from the last version the brain confirmed. A file touched in the last
``QUIET_MS`` is left for the next pass, so a report still being written is not sent half done.
State is one ``meta`` row per file, moved only after the brain's SHA-256 matches.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from jarvis.surface.mac_ship_core import SET_META, Report, ShipFailed, Sink, State

if TYPE_CHECKING:
    from pathlib import Path

SOURCE: Final = "claude-code-daily"
DIRNAME: Final = "claude-code-days"
MAX_BYTES: Final = 256 * 1024
QUIET_MS: Final = 60 * 1000


def ship(directory: Path, state: State, sink: Sink, report: Report, now_ms: int) -> None:
    """One pass over the reports in ``directory``.

    Raises:
        ShipFailed: the brain did not keep a report; its state stays and the next pass resends.
    """
    for path in sorted(directory.glob("*.md")):
        stat = path.stat()
        if not path.is_file() or now_ms - stat.st_mtime_ns // 1_000_000 < QUIET_MS:
            continue
        if stat.st_size > MAX_BYTES:
            report.skipped["claude-code-days/too-large"] += 1
            continue
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        key = f"day-report:{path.name}"
        if state.meta(key) == sha:
            continue
        if sink.send_file(SOURCE, path.name, data) != sha:
            msg = "the brain's digest of a day report does not match"
            raise ShipFailed(msg)
        state.apply([(SET_META, (key, sha))])
        report.files += 1
        report.file_bytes += len(data)
