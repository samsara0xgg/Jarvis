"""Unit tests for scripts/bench_llm_v3.py."""
import sys
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bench_llm_v3 as b  # noqa: E402


def test_module_imports():
    """Smoke: module imports and has expected constants."""
    assert b.PRICING_SNAPSHOT_DATE == "2026-04-14"
    assert set(b.TASKS.keys()) == {"simple", "recall", "synthesis"}
    assert "拿铁" in b.NEEDLE_LINE and "Revolver" in b.NEEDLE_LINE
    assert "美式" in b.DISTRACTOR_LINE
    assert len(b.MODEL_CATALOG) == 8
