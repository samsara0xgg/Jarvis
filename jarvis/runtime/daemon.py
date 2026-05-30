"""POSIX double-fork + setsid daemonization helper.

Per ADR-0002 § Daemon / CLI contract (lines 1046-1098).

The CLI parent prints the ack, calls :func:`fork_detach`, and either
``os._exit(0)`` (parent) or proceeds into bootstrap (child). SQLite is
NEVER opened before the fork — a connection would leak across the
address-space split and fail in the child.

Day-2 implementation: standard POSIX double-fork. The first fork detaches
from the controlling terminal; ``setsid`` creates a new session; the
second fork prevents reacquiring a TTY. The child's
stdin/stdout/stderr are redirected to ``/dev/null``; cwd is set to ``/``
so the child does not pin a removable mountpoint.

References:
- ADR-0002 § Daemon / CLI contract
- POSIX double-fork pattern (Advanced Programming in the Unix Environment §13.3)
"""

from __future__ import annotations

import os
import sys
from typing import Literal


def fork_detach() -> Literal["parent", "child"]:
    """Double-fork + setsid. Parent returns ``"parent"``; child returns ``"child"``.

    Caller pattern (CLI entrypoint per ADR-0002 § Daemon / CLI contract)::

        if fork_detach() == "parent":
            os._exit(0)
        # Child path: stdin/stdout/stderr → /dev/null, cwd=/.
        runtime = bootstrap_runtime_app(...)
        ...

    The child has its file descriptors redirected to ``/dev/null`` and
    ``cwd`` set to ``/``. It is reparented to init (PID 1 on Linux;
    launchd on macOS).

    Returns:
        ``"parent"`` — caller must ``os._exit(0)`` (do NOT return through
            normal cleanup; SQLite connections, open file handles, etc.
            would double-close on the address-space split).
        ``"child"`` — caller proceeds into runtime bootstrap. The child's
            stdin/stdout/stderr are now ``/dev/null``; logging must go
            through the runtime logger or be appended to a file.

    Raises:
        OSError: On fork failure.
    """
    # First fork: detach from controlling terminal.
    pid = os.fork()
    if pid > 0:
        # Parent of first fork — return "parent" so caller exits.
        return "parent"

    # Child of first fork: create new session, becoming session leader.
    os.setsid()

    # Second fork: prevent reacquiring a TTY (a session leader can open
    # a controlling terminal; the grandchild — non-leader — cannot).
    pid = os.fork()
    if pid > 0:
        # Intermediate process exits so the grandchild is reparented
        # to init / launchd.
        os._exit(0)

    # Grandchild: redirect FDs and cwd.
    _redirect_standard_streams()
    os.chdir("/")
    return "child"


def _redirect_standard_streams() -> None:
    """Replace stdin/stdout/stderr with ``/dev/null`` in the daemonized child."""
    # Open /dev/null read+write, then duplicate that FD over the three
    # standard streams. dup2 atomically closes the target FD before
    # replacing it, so any inherited stdio handles are released here.
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())
    # The original /dev/null FD is now redundant (each dup2 call kept
    # its own kernel-level reference). Close only if it isn't one of
    # 0/1/2 itself, which can happen in odd FD-table states.
    if devnull > 2:  # noqa: PLR2004 — 2 is the canonical stderr FD number, not a magic value.
        os.close(devnull)


__all__ = ["fork_detach"]
