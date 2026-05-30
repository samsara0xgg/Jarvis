r"""Pure helper — auto-detect a repo's `verify_command` for the Task Ledger.

Per ADR-0002 Step 4 build-order row + D11: when L3's `create_task` tool
materializes a new task with a `repo_path`, it stores a `verify_command`
string in `task.created.optional_payload["verify_command"]`. L4 will
eventually feed that string verbatim to `/bin/sh -c` (Step 11), so the
four candidate strings are pinned — each chooses a first-failure-exit
flag so the `exit_code == 0` predicate is a tight pass/fail signal.

Detection rules, in priority order (the first match wins):

    1. Python  — `pyproject.toml` AND (`tests/` OR `pytest.ini`) →
                 `"uv run pytest -x"`
                 (`uv run` is Allen's mandated launcher per the global
                 CLAUDE.md "Package Management" rule; `-x` exits on the
                 first failing test.)
    2. Rust    — `Cargo.toml` AND (`tests/` OR any `*.rs` file under the
                 repo contains a `#[test]` attribute) → `"cargo test"`
                 (cargo defaults to bail on first failing crate, no flag
                 needed.)
    3. JS      — `package.json` AND any of
                 `jest.config.{js,ts,json}` exists → `"npx jest --bail"`
                 (`--bail` mirrors pytest `-x`.)
    4. Make    — `Makefile` exists AND the file declares a `test:` target
                 (literal `^test\\s*:` line) → `"make test"`.
    5. None    — no rule matches → `None` (downgrade path: L3 records a
                 Limitation Claim at `level=reported`; no `verify_command`
                 is stored on `task.created`).

Hard rules:

- Stdlib only (canary H13 — the execution layer DAG forbids third-party
  deps in detection helpers).
- The Rust `#[test]` scan walks `repo_path.rglob("*.rs")` and **breaks on
  the first match**. Cargo monorepos can be hundreds of MB; an unbounded
  scan would be a Tier-1 wall-clock landmine.
- A non-existent or non-directory `repo_path` returns `None`. Callers
  pass an LLM-supplied path that may be wrong; surprise exceptions in
  the L4 handler would mask the genuine downgrade signal.
- The function is pure: no side effects, no logging, no globals.
- Priority order (Python > Rust > JS > Make) is part of the contract —
  a repo with BOTH `pyproject.toml` + `Cargo.toml` resolves to
  `"uv run pytest -x"`, never `"cargo test"`. The unit test
  `test_verify_command_detect.py::test_detect_python_wins_over_rust`
  pins this tie-break.

Layer boundary (`.importlinter` + canary H13): stdlib only. No imports
from sibling layers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path


# --- Pinned command strings --------------------------------------------------
#
# These four constants are the ONLY strings the function may return
# alongside None. They are exported via `__all__` so the unit test can
# reference them by symbol rather than re-declaring the literal in three
# places.

_PYTHON_VERIFY_COMMAND: Final[str] = "uv run pytest -x"
_RUST_VERIFY_COMMAND: Final[str] = "cargo test"
_JS_VERIFY_COMMAND: Final[str] = "npx jest --bail"
_MAKE_VERIFY_COMMAND: Final[str] = "make test"


# Makefile `test:` target detector. Matches a line that begins with
# `test` followed by optional whitespace and a colon. Multi-line MODE so
# `^` anchors at each newline; we read the Makefile as a single string.
_MAKE_TEST_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"^test\s*:", re.MULTILINE)

# Rust `#[test]` attribute detector. The attribute appears immediately
# above the test function; conventional spelling is `#[test]` with no
# spacing variations in `cargo`-managed projects. We use a substring
# check rather than a regex because the parse target is "does any file
# contain this token at all" — false positives in comments are accepted
# as a Day-2 trade-off (a project that puts `#[test]` in a doc comment
# already declared itself a test-bearing crate by carrying the literal).
_RUST_TEST_ATTRIBUTE: Final[str] = "#[test]"


def detect_verify_command(repo_path: Path) -> str | None:
    """Return the verify_command string for `repo_path`, or None.

    See module docstring for the four detection rules + priority order.

    Args:
        repo_path: Path to the target repo's root directory. May be a
            relative or absolute path; the function does not normalize.

    Returns:
        One of the four pinned command strings, or `None` if no rule
        matched. A non-existent or non-directory path also returns
        `None` (callers handle the downgrade — Limitation Claim).
    """
    if not repo_path.is_dir():
        return None

    if _is_python_repo(repo_path):
        return _PYTHON_VERIFY_COMMAND
    if _is_rust_repo(repo_path):
        return _RUST_VERIFY_COMMAND
    if _is_js_repo(repo_path):
        return _JS_VERIFY_COMMAND
    if _is_make_repo(repo_path):
        return _MAKE_VERIFY_COMMAND
    return None


def _is_python_repo(repo_path: Path) -> bool:
    """Python rule: `pyproject.toml` + (`tests/` dir OR `pytest.ini`)."""
    if not (repo_path / "pyproject.toml").is_file():
        return False
    if (repo_path / "tests").is_dir():
        return True
    return (repo_path / "pytest.ini").is_file()


def _is_rust_repo(repo_path: Path) -> bool:
    """Rust rule: `Cargo.toml` + (`tests/` dir OR any `*.rs` has `#[test]`)."""
    if not (repo_path / "Cargo.toml").is_file():
        return False
    if (repo_path / "tests").is_dir():
        return True
    # Bounded rglob: break on first `*.rs` that contains `#[test]`.
    for rs_path in repo_path.rglob("*.rs"):
        try:
            text = rs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable file (permissions / weird symlink); skip rather
            # than fail the whole detection.
            continue
        if _RUST_TEST_ATTRIBUTE in text:
            return True
    return False


def _is_js_repo(repo_path: Path) -> bool:
    """JS rule: `package.json` + any `jest.config.{js,ts,json}`."""
    if not (repo_path / "package.json").is_file():
        return False
    return any(
        (repo_path / f"jest.config.{ext}").is_file() for ext in ("js", "ts", "json")
    )


def _is_make_repo(repo_path: Path) -> bool:
    """Make rule: `Makefile` declares a `test:` target."""
    makefile = repo_path / "Makefile"
    if not makefile.is_file():
        return False
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _MAKE_TEST_TARGET_RE.search(text) is not None


__all__ = ["detect_verify_command"]
