"""The runtime's parsed config (ADR 0370) and a runtime booted over it, for tests.

``boot_runtime`` takes the steps a daemon's entry point takes, in its order: lay out the root,
load its environment, parse the config, assemble. ``runtime_config`` parses a mapping alone, for
a test that builds the runtime's parts by hand; whatever the mapping leaves out reads as that
section's defaults. ``reconfigure`` is a booted runtime over the same config with whole sections
replaced.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.deployment import bootstrap_runtime
from jarvis.runtime import bootstrap_runtime_app, load_runtime_env_and_trace
from jarvis.runtime.config import RuntimeConfig, load_runtime_config, parse_runtime_config

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.runtime import JarvisRuntime

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"


def boot_runtime(root: Path, *, config_path: Path | None = None) -> JarvisRuntime:
    """A runtime over ``root`` (its ``settings.yaml`` laid over the shipped or given config)."""
    paths = bootstrap_runtime(root)
    load_runtime_env_and_trace(paths)
    return bootstrap_runtime_app(load_runtime_config(config_path, paths.root), paths)


def runtime_config(
    raw: Mapping[str, Any] | None = None, *, root: Path = Path("/nonexistent-runtime-root"),
) -> RuntimeConfig:
    """The config ``raw`` parses to, with the shipped file's place and ``root`` as the root."""
    return parse_runtime_config(raw or {}, config_path=SHIPPED, runtime_root=root)


def reconfigure(runtime: JarvisRuntime, **sections: Any) -> JarvisRuntime:  # noqa: ANN401 - YAML
    """``runtime`` over its own config with each named top-level section replaced whole."""
    config = runtime.config
    parsed = parse_runtime_config(
        {**config.raw, **sections}, config_path=config.config_path,
        runtime_root=runtime.runtime_paths.root, prompt_path=config.prompt_path,
    )
    return replace(runtime, config=parsed)
