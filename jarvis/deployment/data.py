"""L6 — how long Jarvis keeps what it records, and taking it all out (ADR 0067).

Recordings and screenshots expire by file age; logs are cut at a size and
only the newest few kept; an export zips the user's data; erasing
everything waits for the next boot, before anything under the runtime root
is open. Stdlib only, like the rest of ``jarvis.deployment``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

LOGGER = logging.getLogger(__name__)

ERASE_MARKER = "erase-requested"
# Kept by an erase: the speech models are the app's, not the user's (fetched
# again otherwise), and ``logs`` is where the running daemon's stderr goes
# (the Electron app creates it only once per launch) and holds no content.
_KEPT_ON_ERASE = frozenset({"models", "logs", ERASE_MARKER})
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_FILES_KEPT = 3
_DAY_S = 86400


def delete_older_than(directory: Path, days: int | None, *, now: float | None = None) -> int:
    """Delete the files in ``directory`` last written more than ``days`` ago; None keeps all."""
    if days is None or not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - days * _DAY_S
    gone = 0
    for path in directory.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def clear_files(directories: Iterable[Path]) -> int:
    """Delete every file in each directory; the directories stay."""
    gone = 0
    for directory in directories:
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
                    gone += 1
    return gone


def rotate_logs(logs_dir: Path, max_bytes: int = LOG_MAX_BYTES, kept: int = LOG_FILES_KEPT) -> int:
    """Cut each ``*.log`` past ``max_bytes`` into ``.1``, keeping ``kept`` files in all.

    Copy then truncate, not rename: launchd, the Electron app and the MCP
    servers the daemon starts all hold the file open for append, so a
    renamed file would keep growing under its new name. With O_APPEND the
    next write after the truncate lands at offset 0. Lines written between
    the copy and the truncate are lost.
    """
    cut = 0
    for log in sorted(logs_dir.glob("*.log")):
        if log.stat().st_size <= max_bytes:
            continue
        for index in range(kept - 1, 1, -1):
            older = log.with_name(f"{log.name}.{index - 1}")
            if older.exists():
                older.replace(log.with_name(f"{log.name}.{index}"))
        shutil.copyfile(log, log.with_name(f"{log.name}.1"))
        os.truncate(log, 0)
        cut += 1
    return cut


def export_zip(root: Path, paths: Iterable[Path]) -> Path:
    """Zip ``paths`` (files, directories, SQLite databases) into a temp file under ``root``.

    Each database goes in as a backup-API copy, consistent even while the
    daemon writes it. Entries are named relative to ``root``; a path outside
    it keeps only its own name. The caller deletes the returned file.
    """
    handle, name = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=root)
    target = Path(name)
    try:
        with (
            open(handle, "wb") as raw,  # noqa: PTH123 — wraps mkstemp's descriptor.
            zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive,
            tempfile.TemporaryDirectory(dir=root) as scratch,
        ):
            for path in paths:
                arc = path.relative_to(root) if path.is_relative_to(root) else Path(path.name)
                if path.is_dir():
                    for one in sorted(path.rglob("*")):
                        if one.is_file():
                            archive.write(one, arc / one.relative_to(path))
                elif path.suffix == ".db" and path.is_file():
                    copy = Path(scratch) / path.name
                    with (
                        closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source,
                        closing(sqlite3.connect(copy)) as dest,
                    ):
                        source.backup(dest)
                    archive.write(copy, arc)
                elif path.is_file():
                    archive.write(path, arc)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def request_erase(root: Path) -> None:
    """Mark ``root`` to be emptied the next time Jarvis starts."""
    (root / ERASE_MARKER).touch()


def erase_if_requested(root: Path) -> bool:
    """Empty ``root`` (all but the models and logs) when an erase was asked for.

    Runs at boot, before anything under ``root`` is opened. The marker goes
    last, so an erase cut short by a crash runs again on the next boot.
    Refuses a root that is the home directory itself.
    """
    marker = root / ERASE_MARKER
    if not marker.exists():
        return False
    if root == Path.home():
        LOGGER.error("erase: refusing to empty the home directory %s", root)
        marker.unlink()
        return False
    for path in root.iterdir():
        if path.name in _KEPT_ON_ERASE:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    marker.unlink()
    LOGGER.warning("erase: every piece of user data under %s was deleted", root)
    return True
