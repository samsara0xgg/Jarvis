"""Unit tests for scripts/observer_bench.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import observer_bench as ob  # noqa: E402


def test_module_imports():
    """Smoke: module imports cleanly + imports v3 as pure helper."""
    assert hasattr(ob, "v3")
    assert hasattr(ob.v3, "ModelSpec")
    assert hasattr(ob.v3, "calc_cost")
    assert hasattr(ob.v3, "extract_cache_metrics")
