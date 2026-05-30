"""Unit tests for L6 Deployment — runtime root + bootstrap + per-run artifacts.

Tests never touch the real `~/.jarvis/` — they either override via the
`root` argument, `monkeypatch.setenv("JARVIS_RUNTIME_ROOT", ...)`, or
`tmp_path` per the ADR Step 3 instructions.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment import RuntimePaths, bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path


def test_explicit_root_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit `root=` argument wins over env var and built-in default."""
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path / "from-env"))
    paths = bootstrap_runtime(root=tmp_path / "from-arg")
    assert paths.root == (tmp_path / "from-arg").resolve()


def test_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When `root` is None, `JARVIS_RUNTIME_ROOT` env var is honored."""
    target = tmp_path / "env-root"
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(target))
    paths = bootstrap_runtime()
    assert paths.root == target.resolve()
    assert paths.root.is_dir()


def test_default_root_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no `root` arg and no env var, default expands to `~/.jarvis/`.

    Uses HOME redirection (`monkeypatch.setenv("HOME", tmp_path)`) so the
    real user dir is never touched — `Path.expanduser()` reads HOME.
    """
    monkeypatch.delenv("JARVIS_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = bootstrap_runtime()
    assert paths.root == (tmp_path / ".jarvis").resolve()
    assert paths.root.is_dir()


def test_empty_env_var_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `JARVIS_RUNTIME_ROOT` is treated like unset (falsy)."""
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = bootstrap_runtime()
    assert paths.root == (tmp_path / ".jarvis").resolve()


def test_paths_shape(tmp_path: Path) -> None:
    """All declared subpaths are derived from `root` per ADR § Configurable paths."""
    paths = bootstrap_runtime(root=tmp_path / "rt")
    assert paths.event_log == paths.root / "mac_events.db"
    assert paths.artifacts_root == paths.root / "artifacts"
    assert paths.registry == paths.root / "registry.json"


def test_bootstrap_creates_root_and_artifacts(tmp_path: Path) -> None:
    """Bootstrap mkdirs `root` and `artifacts_root`; leaves event_log/registry untouched."""
    paths = bootstrap_runtime(root=tmp_path / "fresh")
    assert paths.root.is_dir()
    assert paths.artifacts_root.is_dir()
    # L2 / L4 create these at first write; L6 only places them.
    assert not paths.event_log.exists()
    assert not paths.registry.exists()


def test_bootstrap_idempotent(tmp_path: Path) -> None:
    """Second call must return equal paths and not raise when dirs exist."""
    first = bootstrap_runtime(root=tmp_path / "twice")
    second = bootstrap_runtime(root=tmp_path / "twice")
    assert first == second
    assert first.root.is_dir()
    assert first.artifacts_root.is_dir()


def test_runtime_paths_is_frozen(tmp_path: Path) -> None:
    """`RuntimePaths` is a frozen dataclass — attribute assignment must raise."""
    paths = bootstrap_runtime(root=tmp_path / "frozen")
    with pytest.raises(AttributeError):
        paths.root = tmp_path / "other"  # type: ignore[misc]


def test_artifact_dir_for_run_creates_lazily(tmp_path: Path) -> None:
    """`artifact_dir_for_run` creates the per-run dir on first call (lazy-create)."""
    paths = bootstrap_runtime(root=tmp_path / "lazy")
    run_dir = paths.artifact_dir_for_run("R1")
    assert run_dir == paths.artifacts_root / "run_R1"
    assert run_dir.is_dir()


def test_artifact_dir_for_run_idempotent(tmp_path: Path) -> None:
    """Second call for the same `run_id` returns the same path and does not raise."""
    paths = bootstrap_runtime(root=tmp_path / "idem")
    first = paths.artifact_dir_for_run("R1")
    # Drop a file in to prove subsequent calls don't wipe the dir.
    (first / "diff.json").write_text('{"status": "ok"}', encoding="utf-8")
    second = paths.artifact_dir_for_run("R1")
    assert second == first
    assert (second / "diff.json").read_text(encoding="utf-8") == '{"status": "ok"}'


def test_artifact_dirs_isolated_per_run(tmp_path: Path) -> None:
    """Each `run_id` gets its own subdirectory under `artifacts_root`."""
    paths = bootstrap_runtime(root=tmp_path / "many")
    d1 = paths.artifact_dir_for_run("R1")
    d2 = paths.artifact_dir_for_run("R2")
    assert d1 != d2
    assert d1.parent == paths.artifacts_root
    assert d2.parent == paths.artifacts_root


def test_returned_paths_are_absolute(tmp_path: Path) -> None:
    """Resolution returns absolute paths so downstream layers needn't re-resolve."""
    paths = bootstrap_runtime(root=tmp_path / "abs")
    assert paths.root.is_absolute()
    assert paths.event_log.is_absolute()
    assert paths.artifacts_root.is_absolute()
    assert paths.registry.is_absolute()


def test_bootstrap_does_not_mutate_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L6 reads `JARVIS_RUNTIME_ROOT` but must never write to `os.environ`."""
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path / "noenv"))
    before = dict(os.environ)
    bootstrap_runtime()
    after = dict(os.environ)
    assert before == after


def test_runtime_paths_constructible_directly(tmp_path: Path) -> None:
    """`RuntimePaths` can also be assembled directly (e.g. by tests / composition root).

    This guards against accidental coupling to `bootstrap_runtime` side effects;
    the dataclass itself does no I/O.
    """
    paths = RuntimePaths(
        root=tmp_path,
        event_log=tmp_path / "mac_events.db",
        artifacts_root=tmp_path / "artifacts",
        registry=tmp_path / "registry.json",
    )
    assert paths.root == tmp_path
    # artifact_dir_for_run still works; lazy-creates under the given artifacts_root.
    (tmp_path / "artifacts").mkdir()
    run_dir = paths.artifact_dir_for_run("R7")
    assert run_dir == tmp_path / "artifacts" / "run_R7"
    assert run_dir.is_dir()
