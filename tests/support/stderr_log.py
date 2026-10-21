"""Process fd 2 as a terminal's launchd gives it, and as sounddevice's start-up takes it away."""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_STDERR_FD = 2


@contextlib.contextmanager
def stderr_in(path: Path) -> Iterator[None]:
    """Fd 2 appends to ``path`` for the block, and the log writes to it as ``basicConfig`` does.

    Lines read ``<logger name>: <message>``. The handler is the root's own, so whatever
    re-points it (``keep_log_off_portaudio_stderr``) is what the file shows.
    """
    saved = os.dup(_STDERR_FD)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(fd, _STDERR_FD)
    os.close(fd)
    original = os.fdopen(_STDERR_FD, "w", buffering=1, closefd=False)
    handler = logging.StreamHandler(original)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        handler.flush()
        if handler.stream is not original:
            handler.stream.close()
        os.dup2(saved, _STDERR_FD)
        os.close(saved)


@contextlib.contextmanager
def stderr_sent_nowhere() -> Iterator[None]:
    """What ``sounddevice._initialize`` does to fd 2 for as long as ``Pa_Initialize`` takes."""
    kept = os.dup(_STDERR_FD)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, _STDERR_FD)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(kept, _STDERR_FD)
        os.close(kept)
