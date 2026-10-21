"""A runtime built from a settings file, as a brain daemon reads it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.runtime import bootstrap_runtime_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def brain_runtime(tmp_path: Path, settings: str, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    return bootstrap_runtime_app(runtime_root=root)
