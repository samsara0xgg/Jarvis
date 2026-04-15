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


import tempfile
from pathlib import Path


def test_generate_fake_notes_deterministic():
    """Same seed + size → byte-identical output across calls."""
    notes_a = b.generate_fake_notes(2000, seed=42)
    notes_b = b.generate_fake_notes(2000, seed=42)
    assert notes_a == notes_b


def test_generate_fake_notes_token_target():
    """Output tokens within [target-200, target] (cl100k_base)."""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    notes = b.generate_fake_notes(2000, seed=42)
    n_tokens = len(enc.encode(notes))
    assert 1800 <= n_tokens <= 2000, f"got {n_tokens}"


def test_needle_present_in_middle():
    """针在 45-55% 位置."""
    notes = b.generate_fake_notes(10000, seed=42)
    lines = notes.split("\n")
    needle_idx = next(i for i, ln in enumerate(lines) if ln == b.NEEDLE_LINE)
    pct = needle_idx / len(lines)
    assert 0.45 <= pct <= 0.55, f"needle at {pct:.2%}"


def test_distractor_precedes_needle():
    """干扰项在针之前 10-20 行."""
    notes = b.generate_fake_notes(10000, seed=42)
    lines = notes.split("\n")
    needle_idx = next(i for i, ln in enumerate(lines) if ln == b.NEEDLE_LINE)
    distractor_idx = next(i for i, ln in enumerate(lines) if ln == b.DISTRACTOR_LINE)
    gap = needle_idx - distractor_idx
    assert 10 <= gap <= 20, f"distractor at gap {gap}"


def test_load_notes_caches_to_disk():
    """Second call reads from disk, not regenerates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fx_dir = Path(tmpdir)
        notes_a = b.load_notes(2000, fx_dir)
        # Manually corrupt the file — second load should still return corrupted version
        (fx_dir / "fake_notes_2k.txt").write_text("CORRUPTED", encoding="utf-8")
        notes_b = b.load_notes(2000, fx_dir)
        assert notes_b == "CORRUPTED"


def test_verify_recall_needs_both_keywords():
    """"拿铁" alone is not enough — must have brand word too."""
    assert b.verify_recall("Allen 最喜欢喝拿铁，尤其是 Revolver 的豆") is True
    assert b.verify_recall("Allen 可能喜欢喝拿铁") is False      # 拿铁 only
    assert b.verify_recall("Allen 喜欢喝 Revolver 咖啡") is False # brand only
    assert b.verify_recall("Allen 喜欢喝美式咖啡") is False       # distractor
    assert b.verify_recall("Allen 喝拿铁，用耶加雪菲豆") is True   # 拿铁 + 耶加
