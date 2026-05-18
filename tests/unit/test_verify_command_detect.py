"""Unit tests for `jarvis.execution.verify_command_detect.detect_verify_command`.

Covers the four detection rules + priority order + edge cases per
ADR-0002 Step 4 build-order row. All tests use `tmp_path` to construct
real (tiny) repo shapes — the detector walks the filesystem, so a
real-fs fixture is the lightest faithful test (no `unittest.mock`
gymnastics, deterministic outcomes).

The four pinned command strings are referenced verbatim against the
module docstring contract:

    Python  → "uv run pytest -x"
    Rust    → "cargo test"
    JS      → "npx jest --bail"
    Make    → "make test"
    Unknown → None
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.execution.verify_command_detect import detect_verify_command

if TYPE_CHECKING:
    from pathlib import Path


# --- Repo-shape builders ----------------------------------------------------


def _python_with_tests_dir(repo: Path) -> None:
    """Python repo: pyproject.toml + tests/ directory."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")


def _python_with_pytest_ini(repo: Path) -> None:
    """Python repo: pyproject.toml + pytest.ini (no tests/ dir)."""
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")


def _rust_with_tests_dir(repo: Path) -> None:
    """Rust repo: Cargo.toml + tests/ directory."""
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "integ.rs").write_text("fn x() {}\n", encoding="utf-8")


def _rust_with_test_attribute(repo: Path) -> None:
    """Rust repo: Cargo.toml + a src/*.rs file containing `#[test]`."""
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text(
        "#[test]\nfn it_works() { assert_eq!(2 + 2, 4); }\n",
        encoding="utf-8",
    )


def _js_with_jest_js(repo: Path) -> None:
    """JS repo: package.json + jest.config.js."""
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (repo / "jest.config.js").write_text("module.exports = {};\n", encoding="utf-8")


def _js_with_jest_ts(repo: Path) -> None:
    """JS repo: package.json + jest.config.ts."""
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (repo / "jest.config.ts").write_text("export default {};\n", encoding="utf-8")


def _js_with_jest_json(repo: Path) -> None:
    """JS repo: package.json + jest.config.json."""
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (repo / "jest.config.json").write_text("{}\n", encoding="utf-8")


def _make_with_test_target(repo: Path) -> None:
    """Make repo: Makefile declaring a `test:` target."""
    (repo / "Makefile").write_text(
        ".PHONY: test\n\ntest:\n\techo running\n",
        encoding="utf-8",
    )


def _just_readme(repo: Path) -> None:
    """Unknown shape: only a README, no recognized build manifest."""
    (repo / "README.md").write_text("# nothing here\n", encoding="utf-8")


# --- Parametrized detection table -------------------------------------------


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (_python_with_tests_dir, "uv run pytest -x"),
        (_python_with_pytest_ini, "uv run pytest -x"),
        (_rust_with_tests_dir, "cargo test"),
        (_rust_with_test_attribute, "cargo test"),
        (_js_with_jest_js, "npx jest --bail"),
        (_js_with_jest_ts, "npx jest --bail"),
        (_js_with_jest_json, "npx jest --bail"),
        (_make_with_test_target, "make test"),
        (_just_readme, None),
    ],
)
def test_detect_recognizes_shape(
    tmp_path: Path,
    builder: object,
    expected: str | None,
) -> None:
    """Each repo shape maps to its pinned verify_command (or None)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    builder(repo)  # type: ignore[operator]

    assert detect_verify_command(repo) == expected


# --- Edge cases / tie-break --------------------------------------------------


def test_detect_python_wins_over_rust(tmp_path: Path) -> None:
    """When BOTH Python and Rust manifests exist, Python wins (priority order)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _python_with_tests_dir(repo)
    # Layer a Rust shape on top.
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("#[test]\nfn t() {}\n", encoding="utf-8")

    assert detect_verify_command(repo) == "uv run pytest -x"


def test_detect_rust_without_tests_dir_or_attribute_is_unknown(tmp_path: Path) -> None:
    """Cargo.toml alone (no tests/, no `#[test]`) does not qualify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")

    assert detect_verify_command(repo) is None


def test_detect_python_without_tests_dir_or_pytest_ini_is_unknown(tmp_path: Path) -> None:
    """pyproject.toml alone (no tests/, no pytest.ini) does not qualify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    assert detect_verify_command(repo) is None


def test_detect_js_without_jest_config_is_unknown(tmp_path: Path) -> None:
    """package.json alone (no jest.config.*) does not qualify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")

    assert detect_verify_command(repo) is None


def test_detect_makefile_without_test_target_is_unknown(tmp_path: Path) -> None:
    """A Makefile without a `test:` target does not qualify."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("build:\n\techo build\n", encoding="utf-8")

    assert detect_verify_command(repo) is None


def test_detect_empty_directory_returns_none(tmp_path: Path) -> None:
    """An empty directory matches no rule → None."""
    repo = tmp_path / "repo"
    repo.mkdir()

    assert detect_verify_command(repo) is None


def test_detect_nonexistent_path_returns_none(tmp_path: Path) -> None:
    """A non-existent path returns None rather than raising."""
    repo = tmp_path / "does-not-exist"

    assert detect_verify_command(repo) is None


def test_detect_file_instead_of_directory_returns_none(tmp_path: Path) -> None:
    """A file (not a directory) returns None — caller's path is wrong."""
    not_a_dir = tmp_path / "stray.txt"
    not_a_dir.write_text("hi\n", encoding="utf-8")

    assert detect_verify_command(not_a_dir) is None
