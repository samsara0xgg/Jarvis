"""The command line and the daemon's root: a second ``serve`` is refused, a one-shot opens nothing.

``python -m jarvis "<text>"`` hands the turn to a brain or to the daemon on this machine and never
assembles a runtime of its own (ADR 0341), so with no brain it must leave every root as it found
it. A second ``serve`` on a held root is refused before it touches a file (ADR 0318).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from jarvis.cli import main
from jarvis.deployment import data
from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.shared.lang import Language, t
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


def _snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under ``root`` with its mode, size and modification time."""
    return {
        str(one.relative_to(root)): (one.stat().st_mode, one.stat().st_size, one.stat().st_mtime_ns)
        for one in [root, *root.rglob("*")]
    }


@pytest.mark.parametrize(("utterance", "language"), [("hello", "en"), ("你好", "zh")])
def test_the_one_shot_with_no_brain_exits_nonzero_with_the_hint_and_touches_no_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    utterance: str, language: Language,
) -> None:
    """No daemon here and no --brain: the hint in the utterance's language, and nothing opened."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the default root and the agent's plist are here
    monkeypatch.delenv("JARVIS_RUNTIME_ROOT", raising=False)
    left_behind = tmp_path / "left-behind"  # a root an earlier daemon ran on: a log with history
    left_behind.mkdir(mode=0o700)
    open_event_log(left_behind / "mac_events.db").close()
    before = _snapshot(left_behind)

    for argv in (["--runtime-root", str(left_behind), utterance], [utterance]):
        assert main(argv) != 0
        assert t("cli.no_brain", lang=language) in capsys.readouterr().err

    assert _snapshot(left_behind) == before
    assert not (tmp_path / ".jarvis").exists(), "the default root was not made either"


def test_a_second_serve_on_a_held_root_exits_2_and_changes_nothing(tmp_path: Path) -> None:
    """The lock comes before the erase and the bootstrap: not a file under the root is touched."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "memory.db").write_bytes(b"conversations")
    (root / "settings.yaml").write_text("assistant_name: Jarvis\n")
    data.request_erase(root)

    with acquire_exclusive(root / "daemon.lock"):
        before = _snapshot(root)
        result = subprocess.run(  # noqa: S603 — sys.executable is trusted; argv is fully controlled.
            [sys.executable, "-m", "jarvis", "serve", "--runtime-root", str(root), "--port", "0"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        after = _snapshot(root)

    assert result.returncode == 2, result.stderr
    assert f"daemon already running at pid {os.getpid()}" in result.stderr, result.stderr
    assert after == before
    assert (root / "memory.db").read_bytes() == b"conversations"
