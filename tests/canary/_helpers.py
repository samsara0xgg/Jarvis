"""Shared helpers for the H1-H13 anti-bypass canary suite.

Per ADR 0001 § Acceptance criterion H. Canaries are stdlib-only static
analyses; they never import ``unittest.mock`` and never instantiate the
LLM. The helpers exposed here:

- :func:`repo_root` — absolute path to the worktree root (one level above
  the ``jarvis/`` package).
- :func:`iter_jarvis_py_files` — yields every ``.py`` under ``jarvis/``.
- :func:`iter_all_py_files` — yields every ``.py`` under ``jarvis/`` and
  ``tests/``.
- :func:`parse` — ``ast.parse(path.read_text())``.

Canary files import only from this module and from ``jarvis.*`` public
modules; never from ``unittest.mock``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def repo_root() -> Path:
    """Return the worktree root (parent of ``tests/`` and ``jarvis/``).

    Walks up from this file until both ``jarvis/`` and ``tests/`` exist
    as siblings, so the canary helper works regardless of which pytest
    invocation cwd happens to be in use.
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "jarvis").is_dir() and (parent / "tests").is_dir():
            return parent
    msg = f"could not locate repo root from {current!s}"
    raise RuntimeError(msg)


def iter_jarvis_py_files() -> Iterator[Path]:
    """Yield every ``.py`` file under ``jarvis/`` (recursive).

    Skips ``__pycache__/`` directories. Order is deterministic
    (``Path.rglob`` is filesystem-order on most platforms, sorted here
    for reproducibility across runs).
    """
    jarvis_dir = repo_root() / "jarvis"
    for path in sorted(jarvis_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def iter_all_py_files() -> Iterator[Path]:
    """Yield every ``.py`` file under ``jarvis/`` and ``tests/`` (recursive).

    Skips ``__pycache__/`` and any ``.venv``-style directories.
    """
    root = repo_root()
    for base in ("jarvis", "tests"):
        base_dir = root / base
        for path in sorted(base_dir.rglob("*.py")):
            parts = path.parts
            if "__pycache__" in parts:
                continue
            if any(p.startswith(".") for p in parts):
                continue
            yield path


def parse(path: Path) -> ast.Module:
    """Return ``ast.parse(path.read_text())`` for ``path``.

    The filename is forwarded into ``ast.parse`` so syntax errors report
    against the real file. Reads as UTF-8 (every source file in the
    repository is UTF-8).
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def relative_to_repo(path: Path) -> str:
    """Return ``path`` rendered relative to the repo root, posix-style."""
    return str(path.relative_to(repo_root())).replace("\\", "/")
