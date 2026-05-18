"""Canary -- spawned codex app-server gets an isolated ``CODEX_HOME``.

P-0009 in ``docs/live-run-bugs.md``: the default ``CODEX_HOME`` is
``~/.codex/`` and the worker reads ``~/.codex/AGENTS.md`` (which on
Allen's box imports ``RTK.md``) at every turn, contaminating every
shell command the worker issues. The fix is environmental, not
prompt-level (Allen's standing rule: fix at Pre-emit Gate /
Result Interpreter / spawn env -- never accrete instructions in the
prompt). This canary AST-scans the L4 spawn driver and pins the
fix against regression.

Witness: ``jarvis/execution/codex_action.py`` must contain the
literal ``"CODEX_HOME"`` as a string constant. If somebody removes
the env injection in a future refactor, this test fails first.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from pathlib import Path


# The witness: the env-variable name Codex honors at spawn time to
# pick its config + AGENTS.md root. If this token is no longer a
# string literal in the spawn driver, the per-spawn isolation is gone.
_WITNESS: str = "CODEX_HOME"


def _spawn_driver_path() -> Path:
    """Return the absolute path to ``jarvis/execution/codex_action.py``."""
    return repo_root() / "jarvis" / "execution" / "codex_action.py"


def _string_literals(path: Path) -> list[str]:
    """Return every ``str`` :class:`ast.Constant.value` in ``path``."""
    return [
        node.value
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_canary_codex_action_injects_codex_home() -> None:
    """``codex_action.py`` must inject ``CODEX_HOME`` at spawn time.

    The literal ``"CODEX_HOME"`` must appear as a string constant in
    ``jarvis/execution/codex_action.py`` -- it is the structural
    witness that the per-spawn driver overrides the inherited
    environment so the spawned worker does not read
    ``~/.codex/AGENTS.md``.
    """
    path = _spawn_driver_path()
    literals = _string_literals(path)
    assert _WITNESS in literals, (
        f"{relative_to_repo(path)} must contain the string literal "
        f"{_WITNESS!r} -- it is the structural witness that the L4 "
        "spawn driver injects an isolated CODEX_HOME per spawn "
        "(P-0009). Without this, the worker inherits the user's "
        "~/.codex/AGENTS.md and gets contaminated by user-local "
        "Codex config."
    )
