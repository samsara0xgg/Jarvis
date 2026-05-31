"""Unit coverage for ``CodexAppServerClient`` lifecycle probes.

``close()`` and ``is_alive()`` had zero direct coverage: every other
Codex test drives the driver through a ``FakeClient``. This file pins the
real teardown semantics that ADR-0002 J6 depends on — once the driver
calls ``close()`` (which happens inside ``run_codex_action`` before it
returns, so before the downstream verify_diff step runs), the subprocess
is dead and ``is_alive()`` reports ``False``.

The full client cannot be constructed without spawning ``codex
app-server``; ``close()`` / ``is_alive()`` only touch ``self._proc`` and
``self._closed``, so the tests bind those two attributes onto a bare
instance around a trivial throwaway subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.codex_client import CodexAppServerClient

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def live_subprocess_client() -> Iterator[tuple[CodexAppServerClient, subprocess.Popen[bytes]]]:
    """A real long-running subprocess wired into a bare client's ``_proc``.

    Bypasses ``__init__`` (which would spawn ``codex app-server``) and binds
    only the two attributes ``close()`` / ``is_alive()`` read.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
    )
    client = object.__new__(CodexAppServerClient)
    client._proc = proc  # noqa: SLF001 - bind only what close()/is_alive() touch
    client._closed = False  # noqa: SLF001
    try:
        yield client, proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3.0)


def test_is_alive_true_while_subprocess_runs(
    live_subprocess_client: tuple[CodexAppServerClient, subprocess.Popen[bytes]],
) -> None:
    """``is_alive()`` is ``True`` while the subprocess is still running."""
    client, _proc = live_subprocess_client
    assert client.is_alive() is True


def test_close_makes_is_alive_false(
    live_subprocess_client: tuple[CodexAppServerClient, subprocess.Popen[bytes]],
) -> None:
    """``close()`` tears down the subprocess so ``is_alive()`` flips to ``False`` (J6).

    This is the unobservable half of ADR-0002 J6: at the moment the
    downstream verify_diff action emits ``action.result_observed``, the
    Codex subprocess is already dead because ``run_codex_action`` closed it
    before returning. The Event Log carries no row for the ``is_alive()``
    state itself, so the invariant is pinned here rather than in the
    scenario suite (whose J6 test asserts the observable ordering shadow).
    """
    client, proc = live_subprocess_client
    assert client.is_alive() is True

    client.close(timeout=3.0)

    assert client.is_alive() is False
    assert proc.poll() is not None
