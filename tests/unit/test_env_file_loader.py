"""Unit tests for the ``${runtime_root}/env`` fill-only loader (ADR-0009 D1).

The launchd context strips Allen's shell environment, so bootstrap
loads ``${runtime_root}/env`` (``KEY=VALUE`` lines) before any surface
preflight reads the environment. Contract pinned here:

- parsed: ``KEY=VALUE`` lines land in ``os.environ``;
- fill-only: an existing variable is NEVER overridden;
- absent file: no-op (missing keys keep the existing degradation —
  voice preflight drops to text-only, unchanged);
- keyed by runtime_root: a temp-root bootstrap reads its OWN env file,
  so test daemons can never flip themselves to Allen's live keys.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from jarvis.deployment import load_env_file
from jarvis.runtime import bootstrap_runtime_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"


def test_env_file_parsed_and_applied(tmp_path: Path) -> None:
    """KEY=VALUE lines land in os.environ; applied keys are returned."""
    key_a, key_b = "JARVIS_T1_ALPHA", "JARVIS_T1_BETA"
    (tmp_path / "env").write_text(
        f"{key_a}=hello\n{key_b}=with spaces  \n", encoding="utf-8"
    )
    try:
        applied = load_env_file(tmp_path)
        assert applied == {key_a: "hello", key_b: "with spaces"}
        assert os.environ[key_a] == "hello"
        assert os.environ[key_b] == "with spaces"
    finally:
        os.environ.pop(key_a, None)
        os.environ.pop(key_b, None)


def test_fill_only_never_overrides(tmp_path: Path) -> None:
    """An existing environment variable wins over the file's value."""
    key = "JARVIS_T1_FILL_ONLY"
    (tmp_path / "env").write_text(f"{key}=from_file\n", encoding="utf-8")
    os.environ[key] = "from_shell"
    try:
        applied = load_env_file(tmp_path)
        assert key not in applied
        assert os.environ[key] == "from_shell"
    finally:
        os.environ.pop(key, None)


def test_absent_file_is_noop(tmp_path: Path) -> None:
    """No env file → empty result, environment untouched."""
    before = dict(os.environ)
    assert load_env_file(tmp_path) == {}
    assert dict(os.environ) == before


def test_comments_blank_and_malformed_lines_skipped(tmp_path: Path) -> None:
    """``#`` comments, blank lines, and =-less lines are ignored."""
    key = "JARVIS_T1_VALID"
    (tmp_path / "env").write_text(
        f"# a comment\n\nNOT_A_PAIR\n=novalue\n{key}=ok\n", encoding="utf-8"
    )
    try:
        applied = load_env_file(tmp_path)
        assert applied == {key: "ok"}
        assert "NOT_A_PAIR" not in os.environ
    finally:
        os.environ.pop(key, None)


def test_bootstrap_loads_env_from_its_own_runtime_root(tmp_path: Path) -> None:
    """``bootstrap_runtime_app`` applies ``${runtime_root}/env`` (temp-root isolated).

    The env file sits under the runtime root handed to bootstrap — NOT
    under any default/home location — so a temp-root test daemon can
    never pick up Allen's real keys.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(_CONFIG_PATH, config_dir / "jarvis.yaml")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    shutil.copyfile(_PROMPT_PATH, prompt_dir / "jarvis_v1.md")

    rt_root = tmp_path / "rt"
    rt_root.mkdir()
    key = "JARVIS_T1_BOOTSTRAP_WIRED"
    (rt_root / "env").write_text(f"{key}=via_bootstrap\n", encoding="utf-8")
    try:
        runtime = bootstrap_runtime_app(
            config_path=config_dir / "jarvis.yaml", runtime_root=rt_root
        )
        runtime.conn.close()
        assert os.environ[key] == "via_bootstrap"
    finally:
        os.environ.pop(key, None)
