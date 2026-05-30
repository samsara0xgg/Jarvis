"""Per-runtime-root POSIX flock + stale-pid recovery for the Jarvis daemon.

Per ADR-0003 D4 + spec §3.7.2 the runtime root may host at most one live
``jarvis serve`` daemon. This module is the primitive both the daemon
(``runtime/inherent_loop.py`` via :func:`acquire_exclusive`) and the CLI
(``cli/__main__.py`` via :func:`is_held` + :func:`holder_pid`) call to
detect and recover from contention.

Design choices:

- ``fcntl.flock`` (NOT ``fcntl.lockf``, NOT advisory-naming): the spec
  picks the kernel-level OFD-style advisory lock so the kernel releases
  it on process death — no orphan-lock recovery path needed.
- ``LOCK_NB`` always: we never block on contention. The non-blocking
  failure is what triggers the stale-pid recovery branch below.
- One retry only on stale recovery: if the second flock attempt still
  fails, something else (race) re-acquired between our unlink and our
  retry — surface that as ``ProcessLockHeld`` with best-effort holder pid.

Layer boundary: stdlib-only, no ``jarvis.*`` imports. The deployment
layer's stricter "no jarvis.state import" rule (per
``jarvis/deployment/__init__.py``) is satisfied trivially.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# Lock-file permission bits, shared by both os.open call sites in
# _open_and_acquire. Lifted to a module constant so the two opens stay
# in sync and the magic number gets a name.
_LOCK_FILE_MODE = 0o644


# ADR D7 specifies the exact public name ``ProcessLockHeld`` — the
# ``-Error`` suffix N818 wants would break the documented contract that
# downstream surfaces (``cli/__main__.py``, ``runtime/inherent_loop.py``)
# import. Keep the spec name; silence the lint at the source.
class ProcessLockHeld(RuntimeError):  # noqa: N818 — spec-mandated public name
    """Raised by acquire_exclusive when the lock is held by another live process."""

    def __init__(self, holder_pid: int) -> None:  # noqa: D107
        super().__init__(f"process lock held by pid {holder_pid}")
        self.holder_pid = holder_pid


def holder_pid(lock_path: Path) -> int | None:
    """Read the pid integer from lock_path. Returns None if missing/empty/invalid.

    A 'valid' pid is parseable as a positive int. Surrounding whitespace
    is stripped; negative/zero/non-int values map to None. Does NOT
    verify the pid points at a live process.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return pid


def _is_pid_live(pid: int) -> bool:
    """Return True iff os.kill(pid, 0) indicates a live process.

    ``os.kill(pid, 0)`` returns None for a live process owned by us,
    raises ``PermissionError`` for a live process owned by another uid
    (still alive => still 'held'), and raises ``ProcessLookupError`` for
    a dead pid.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Conservative: treat unexpected errno as 'maybe live' so we
        # never falsely declare a stale lock and unlink someone else's.
        return True
    return True


def _holder_is_live(lock_path: Path) -> bool:
    """True iff the pid in lock_path is parseable and points at a live process."""
    pid = holder_pid(lock_path)
    if pid is None:
        return False
    return _is_pid_live(pid)


def is_held(lock_path: Path) -> bool:
    """Non-blocking probe — True iff another live process holds the lock.

    Pure read: never writes, never unlinks. Used by the CLI to refuse
    bootstrap when a daemon already owns the runtime root.
    """
    if not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except (FileNotFoundError, OSError):
        # File raced out from under us, or unreadable for us. Treat as
        # not-held so the caller can attempt acquisition and surface a
        # real error there.
        return False
    try:
        # is_held is a CLI probe and must be a TOTAL predicate — if the
        # flock syscall itself fails on an unexpected errno (EBADF, EIO,
        # ...), assume not-held and let the subsequent acquire_exclusive
        # surface the real error instead of leaking it from the probe.
        try:
            acquired = _try_flock_nb(fd)
        except OSError:
            return False
        if acquired:
            # We acquired => no one held it. Release immediately.
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        return _holder_is_live(lock_path)
    finally:
        os.close(fd)


def _try_flock_nb(fd: int) -> bool:
    """Attempt LOCK_EX|LOCK_NB on fd; True on success, False on contention."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    return True


def _write_pid(fd: int) -> None:
    """Truncate the lock file and write our pid (ascii)."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.fsync(fd)


def _open_and_acquire(lock_path: Path) -> int:
    """Open lock_path and flock it, performing stale-pid recovery on contention.

    Returns the open fd holding the flock. Raises :class:`ProcessLockHeld`
    if a live holder owns the lock, or if a single stale-recovery retry
    still cannot acquire it.
    """
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)
    if _try_flock_nb(fd):
        return fd
    # Contention — decide live vs stale.
    if _holder_is_live(lock_path):
        os.close(fd)
        raise ProcessLockHeld(holder_pid(lock_path) or 0)
    # Stale: drop our fd, unlink the file, retry once with a fresh fd.
    os.close(fd)
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()
    # Retry after stale unlink — second and final attempt per module docstring.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)
    if _try_flock_nb(fd):
        return fd
    retry_pid = holder_pid(lock_path) or 0
    os.close(fd)
    raise ProcessLockHeld(retry_pid)


@contextmanager
def acquire_exclusive(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive flock on lock_path; write our pid; release on exit.

    On contention, reads the existing pid; if it points at a live
    process raises ``ProcessLockHeld``. If the holder is dead (stale
    lock), unlinks the file and retries the flock acquisition exactly
    once. If the retry still fails, raises ``ProcessLockHeld`` with the
    best-effort holder pid (0 if unreadable).

    On exit the fd is closed (which atomically releases the flock per
    POSIX). The lock file itself is intentionally NOT unlinked: keeping
    the on-disk inode stable closes the TOCTOU window where a concurrent
    acquirer could open the path between our flock release and our
    unlink, then a third acquirer would ``O_CREAT`` a fresh inode and
    both would believe they hold the lock. New acquirers always
    ``O_RDWR | O_CREAT`` the same inode and flock serializes per-inode.
    The stale pid left in the file is reclaimed by the stale-recovery
    branch in ``_open_and_acquire``.
    """
    fd = _open_and_acquire(lock_path)
    try:
        # Inside the try so a failed pid write (ENOSPC / EIO / EDQUOT on a
        # full or remote-mounted ~/.jarvis) still releases the flock and
        # closes the fd via the finally below — no fd leak on disk pressure.
        _write_pid(fd)
        yield
    finally:
        # Release flock by closing the fd (POSIX-atomic). Do NOT unlink
        # the lock_path — see the TOCTOU rationale in the docstring above.
        with contextlib.suppress(OSError):
            os.close(fd)


__all__ = [
    "ProcessLockHeld",
    "acquire_exclusive",
    "holder_pid",
    "is_held",
]
