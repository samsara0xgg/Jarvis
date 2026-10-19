"""The Mac ships the day reports of his Claude Code work whole (ADR 0299).

``<runtime root>/claude-code-days/<date>.md`` (``material/`` beside them is the input of the
report task and never leaves the Mac) and ``dialogue/<date>.md`` (the day's cut dialogue, kept on
the brain as an archive) are sent under source ``claude-code-daily`` when their bytes differ from
the last version the brain confirmed. A file touched in the last ``QUIET_MS`` is left for the
next pass, so a file still being written is not sent half done. State is one ``meta`` row per
file, moved only after the brain's SHA-256 matches.
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
MAX_DIALOGUE_BYTES: Final = 8 * 1024 * 1024
DIALOGUE: Final = "dialogue"
QUIET_MS: Final = 60 * 1000


def ship(directory: Path, state: State, sink: Sink, report: Report, now_ms: int) -> None:
    """One pass over the reports and the dialogue files in ``directory``.

    Raises:
        ShipFailed: the brain did not keep a file; its state stays and the next pass resends.
    """
    for folder, prefix, cap in (
        (directory, "", MAX_BYTES),
        (directory / DIALOGUE, f"{DIALOGUE}/", MAX_DIALOGUE_BYTES),
    ):
        for path in sorted(folder.glob("*.md")):
            _ship_one(path, prefix + path.name, cap, state, sink, report, now_ms)


def _ship_one(  # noqa: PLR0913 - the file, its name on the brain, its cap and the pass's state
    path: Path, relpath: str, cap: int, state: State, sink: Sink, report: Report, now_ms: int
) -> None:
    stat = path.stat()
    if not path.is_file() or now_ms - stat.st_mtime_ns // 1_000_000 < QUIET_MS:
        return
    if stat.st_size > cap:
        report.skipped["claude-code-days/too-large"] += 1
        return
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = f"day-report:{relpath}"
    if state.meta(key) == sha:
        return
    if sink.send_file(SOURCE, relpath, data) != sha:
        msg = "the brain's digest of a day report does not match"
        raise ShipFailed(msg)
    state.apply([(SET_META, (key, sha))])
    report.files += 1
    report.file_bytes += len(data)
