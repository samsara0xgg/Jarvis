"""Shared test helpers — config loading and repo root path."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """Load the project config.yaml from the repository root."""
    with (REPO_ROOT / "config.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
