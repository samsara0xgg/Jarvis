"""Unit tests for L6 Deployment — fcntl.flock-based per-runtime-root process lock.

Tests use `tmp_path` for lock files and never touch a real runtime root.
Stale-pid recovery is exercised by writing a pid that os.kill(pid, 0) raises
ProcessLookupError on (a child we already waited on).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import multiprocessing
import os
import time
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment.process_lock import (
    ProcessLockHeld,
    acquire_exclusive,
    holder_pid,
    is_held,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- holder_pid edges -------------------------------------------------------


def test_holder_pid_returns_none_when_file_missing(tmp_path: Path) -> None:
    """No file at lock_path => holder_pid is None (nothing to read)."""
    assert holder_pid(tmp_path / "absent.lock") is None


def test_holder_pid_returns_none_for_empty_file(tmp_path: Path) -> None:
    """Empty file => unreadable pid => None per the contract."""
    p = tmp_path / "empty.lock"
    p.write_text("", encoding="utf-8")
    assert holder_pid(p) is None


def test_holder_pid_returns_none_for_whitespace_only(tmp_path: Path) -> None:
    """A file containing only whitespace strips to '' which is unparseable."""
    p = tmp_path / "ws.lock"
    p.write_text("   \n\t  \n", encoding="utf-8")
    assert holder_pid(p) is None


def test_holder_pid_returns_none_for_negative_int(tmp_path: Path) -> None:
    """Negative pids are not 'valid' (positive int) per the contract."""
    p = tmp_path / "neg.lock"
    p.write_text("-42", encoding="utf-8")
    assert holder_pid(p) is None


def test_holder_pid_returns_none_for_zero(tmp_path: Path) -> None:
    """Zero is not a positive int, so not a valid holder pid."""
    p = tmp_path / "zero.lock"
    p.write_text("0", encoding="utf-8")
    assert holder_pid(p) is None


def test_holder_pid_returns_none_for_non_int_garbage(tmp_path: Path) -> None:
    """Non-numeric content fails int() parse => None."""
    p = tmp_path / "garbage.lock"
    p.write_text("not-a-pid", encoding="utf-8")
    assert holder_pid(p) is None


def test_holder_pid_parses_plain_pid(tmp_path: Path) -> None:
    """A well-formed pid integer is returned as int."""
    p = tmp_path / "ok.lock"
    p.write_text("12345", encoding="utf-8")
    assert holder_pid(p) == 12345


def test_holder_pid_parses_pid_with_trailing_newline(tmp_path: Path) -> None:
    """Surrounding whitespace (incl. trailing newline) is stripped before parse."""
    p = tmp_path / "newline.lock"
    p.write_text("  7777  \n", encoding="utf-8")
    assert holder_pid(p) == 7777


# --- is_held probe ----------------------------------------------------------


def test_is_held_returns_false_when_file_missing(tmp_path: Path) -> None:
    """No file => nothing could possibly hold it => False."""
    assert is_held(tmp_path / "missing.lock") is False


def test_is_held_returns_false_when_no_one_holds_flock(tmp_path: Path) -> None:
    """File exists but no live process has flock'd it => caller could acquire."""
    p = tmp_path / "stale.lock"
    p.write_text(str(os.getpid()), encoding="utf-8")
    assert is_held(p) is False


def test_is_held_returns_false_when_holder_pid_dead(tmp_path: Path) -> None:
    """File exists, holder pid is dead, and no one holds the flock => False."""
    # Spawn + wait => pid is guaranteed reaped and dead in our pid namespace.
    dead_pid = _spawn_short_lived_child_and_wait()
    p = tmp_path / "dead.lock"
    p.write_text(str(dead_pid), encoding="utf-8")
    assert is_held(p) is False


def test_is_held_returns_true_when_live_holder(tmp_path: Path) -> None:
    """Real holder process owns the flock + is alive => is_held returns True."""
    p = tmp_path / "live.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lock_until_released,
        args=(str(p), ready, release),
    )
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "holder failed to acquire lock"
        assert is_held(p) is True
    finally:
        release.set()
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5.0)


def test_is_held_returns_false_on_unexpected_flock_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_held must be a TOTAL predicate — unexpected flock errnos => False.

    The CLI probe must never leak obscure POSIX errnos to its caller; if
    the probe itself fails it should yield False and let the subsequent
    acquire_exclusive surface the real error.
    """
    p = tmp_path / "weird.lock"
    p.write_text(str(os.getpid()), encoding="utf-8")

    def _boom(_fd: int, _op: int) -> None:
        raise OSError(errno.EBADF, "bad fd")

    monkeypatch.setattr(fcntl, "flock", _boom)
    assert is_held(p) is False


# --- acquire_exclusive happy path ------------------------------------------


def test_acquire_exclusive_writes_pid_and_retains_file_on_exit(tmp_path: Path) -> None:
    """Happy path: pid is written during the with-block, file persists after.

    Per the TOCTOU-fix invariant, the lock file is NOT unlinked on release —
    the same inode must remain on disk so future acquirers ``O_CREAT`` open
    it and flock the same inode (flock is per-inode). The stale pid left
    in the file is harmless: subsequent acquirers either flock-contend with
    a live holder, or run stale-pid recovery against a dead one.
    """
    p = tmp_path / "happy.lock"
    with acquire_exclusive(p):
        assert p.exists()
        assert holder_pid(p) == os.getpid()
    assert p.exists()
    assert holder_pid(p) == os.getpid()


def test_acquire_exclusive_releases_flock_on_exception(tmp_path: Path) -> None:
    """Exception inside the with-block still releases the flock.

    After the exception bubbles, a second acquire on the same path must
    succeed (proves the fd was closed and the flock released). The file
    itself is allowed to persist — the post-fix contract is "release-only,
    no unlink" — so we only assert the second acquire works.
    """
    p = tmp_path / "boom.lock"

    def _enter_and_raise() -> None:
        with acquire_exclusive(p):
            assert p.exists()
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        _enter_and_raise()
    # Second acquire works — fd was released. File may or may not exist;
    # what matters is the flock is released so a fresh acquire succeeds.
    with acquire_exclusive(p):
        assert holder_pid(p) == os.getpid()


def test_acquire_exclusive_releases_fd_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_write_pid failure (e.g. ENOSPC) must not leak the fd or the flock.

    Simulates a full / quota-exceeded ~/.jarvis by making os.ftruncate
    raise ENOSPC on the first acquire. Then:
      1. acquire_exclusive propagates the OSError.
      2. A fresh acquire on the same path succeeds, proving the previous
         fd was closed + flock released — no leak.
    """
    p = tmp_path / "nospace.lock"
    calls = {"n": 0}
    real_ftruncate = os.ftruncate

    def _ftruncate_enospc_once(fd: int, length: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "no space left on device")
        real_ftruncate(fd, length)

    monkeypatch.setattr(os, "ftruncate", _ftruncate_enospc_once)

    with pytest.raises(OSError, match="no space"), acquire_exclusive(p):
        pytest.fail("should not enter the with-block when _write_pid fails")

    # Fresh acquire on the same path works — the prior fd was released.
    with acquire_exclusive(p):
        assert holder_pid(p) == os.getpid()


def test_release_does_not_unlink_lock_file(tmp_path: Path) -> None:
    """TOCTOU invariant: lock_path must persist on disk after release.

    Closes the window where daemon B opens the path (same inode) while A
    is mid-release, then A unlinks and daemon C ``O_CREAT``s a fresh inode
    — B and C end up flocking different inodes and both claim ownership.
    The fix: never unlink on release. New acquirers always see the same
    inode and flock serializes correctly.
    """
    p = tmp_path / "persist.lock"
    with acquire_exclusive(p):
        assert p.exists()
    assert p.exists(), "lock file must remain on disk to preserve flock inode identity"


def test_concurrent_daemon_acquire_serializes(tmp_path: Path) -> None:
    """Two acquirers on the same lock path: second must raise ProcessLockHeld.

    Spawns a holder process that flocks the file and blocks on a release
    event. While the holder is alive, a second ``acquire_exclusive`` from
    THIS test process must raise ``ProcessLockHeld`` with the holder's pid
    — proving the flock serializes acquirers across processes even when
    the lock file exists from a prior session (post-fix: it always does).
    """
    p = tmp_path / "concurrent.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lock_until_released,
        args=(str(p), ready, release),
    )
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "holder failed to acquire lock"

        def _attempt() -> None:
            with acquire_exclusive(p):
                pytest.fail("second acquire must not succeed while holder is alive")

        with pytest.raises(ProcessLockHeld) as exc_info:
            _attempt()
        assert exc_info.value.holder_pid == proc.pid
    finally:
        release.set()
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5.0)


def test_stale_lock_persists_after_recovered_acquire(tmp_path: Path) -> None:
    """Stale-pid recovery + post-fix release: file still on disk after exit.

    Complements ``test_acquire_exclusive_recovers_from_stale_pid``. The
    stale-recovery branch internally unlinks the dead lock file, retries
    flock, and writes our pid. On exit the lock file must STILL exist
    (no unlink-on-release) so subsequent acquirers see the same inode.
    """
    dead_pid = _spawn_short_lived_child_and_wait()
    p = tmp_path / "stale-persist.lock"
    p.write_text(str(dead_pid), encoding="utf-8")
    with acquire_exclusive(p):
        assert holder_pid(p) == os.getpid()
    assert p.exists(), "lock file must persist after stale-recovery release"
    assert holder_pid(p) == os.getpid()


# --- acquire_exclusive contended path --------------------------------------


def test_acquire_exclusive_raises_when_held_by_live_process(tmp_path: Path) -> None:
    """Another live holder => ProcessLockHeld(holder_pid=<their pid>)."""
    p = tmp_path / "contended.lock"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lock_until_released,
        args=(str(p), ready, release),
    )
    proc.start()
    try:
        assert ready.wait(timeout=5.0), "holder failed to acquire lock"

        def _enter() -> None:
            with acquire_exclusive(p):
                pytest.fail("should not have entered the with-block")

        with pytest.raises(ProcessLockHeld) as exc_info:
            _enter()
        assert exc_info.value.holder_pid == proc.pid
    finally:
        release.set()
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5.0)


# --- acquire_exclusive stale-pid recovery ----------------------------------


def test_acquire_exclusive_recovers_from_stale_pid(tmp_path: Path) -> None:
    """Pre-existing lock_path with a dead pid => unlink + retry => succeed."""
    dead_pid = _spawn_short_lived_child_and_wait()
    p = tmp_path / "stale.lock"
    # Pre-seed the lock file with a dead pid; no one holds the flock.
    p.write_text(str(dead_pid), encoding="utf-8")
    with acquire_exclusive(p):
        # Recovered: our pid is now written into the file.
        assert holder_pid(p) == os.getpid()


# --- helpers ---------------------------------------------------------------


def _spawn_short_lived_child_and_wait() -> int:
    """Spawn a process that exits immediately, wait for it, return its pid.

    After ``join`` the pid is guaranteed not to refer to a live process in
    our pid namespace, so ``os.kill(pid, 0)`` raises ``ProcessLookupError``.
    """
    proc = multiprocessing.Process(target=_noop)
    proc.start()
    proc.join(timeout=5.0)
    pid = proc.pid
    assert pid is not None
    # Sanity: confirm the OS no longer reports this pid as live.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        time.sleep(0.01)
    msg = f"child pid {pid} still alive after join — cannot use for stale-pid test"
    raise RuntimeError(msg)


def _noop() -> None:
    """Process target: do nothing, exit immediately."""


def _hold_lock_until_released(
    lock_path_str: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    """Process target: flock the file, signal ready, block until release.

    Writes our pid to the file (same shape as acquire_exclusive's happy
    path) so the test can assert ``ProcessLockHeld.holder_pid`` matches.
    """
    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
        ready.set()
        release.wait(timeout=30.0)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
