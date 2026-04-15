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
    assert len(b.MODEL_CATALOG) == 14  # 8 core + 4 xAI variants + 2 observer bench adds


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


def _get_spec(provider: str, primary: str) -> b.ModelSpec:
    for s in b.MODEL_CATALOG:
        if s.provider == provider and s.primary_id == primary:
            return s
    raise LookupError(primary)


def test_cost_anthropic_three_segment():
    """Sonnet 4.6 @ 30k prompt with 5k cache_write + 25k cache_read, 200 output."""
    spec = _get_spec("anthropic", "claude-sonnet-4-6")
    cost = b.calc_cost(
        cache_write_tokens=5000,
        cache_read_tokens=25000,
        prompt_total_tokens=30000,
        output_tokens=200,
        spec=spec,
    )
    # regular input = 30000 - 5000 - 25000 = 0
    # cache_write: 5000 * 3.00 * 1.25 / 1e6 = 0.01875
    # cache_read:  25000 * 3.00 * 0.10 / 1e6 = 0.0075
    # output:      200 * 15.00 / 1e6 = 0.003
    # total: 0.02925
    assert abs(cost - 0.02925) < 1e-6


def test_cost_groq_no_cache():
    """Groq never hits cache; all tokens count as regular input."""
    spec = _get_spec("groq", "llama-3.3-70b-versatile")
    cost = b.calc_cost(
        cache_write_tokens=0,
        cache_read_tokens=0,
        prompt_total_tokens=30000,
        output_tokens=200,
        spec=spec,
    )
    # regular: 30000 * 0.59 / 1e6 = 0.0177
    # output:  200 * 0.79 / 1e6 = 0.000158
    # total: 0.017858
    assert abs(cost - 0.017858) < 1e-6


def test_cost_openai_cache_read_only():
    """OpenAI has cache_read but not cache_write fee."""
    spec = _get_spec("openai", "gpt-5")
    cost = b.calc_cost(
        cache_write_tokens=0,
        cache_read_tokens=10000,
        prompt_total_tokens=30000,
        output_tokens=100,
        spec=spec,
    )
    # regular: 20000 * 2.50 / 1e6 = 0.05
    # cache_read: 10000 * 2.50 * 0.50 / 1e6 = 0.0125
    # output: 100 * 10.00 / 1e6 = 0.001
    # total: 0.0635
    assert abs(cost - 0.0635) < 1e-6


from types import SimpleNamespace


def _ns(d):
    """Recursively convert dict → SimpleNamespace to mimic SDK response shape."""
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in d.items()})
    return d


def test_extract_anthropic_cache_metrics():
    resp = _ns({"usage": {
        "input_tokens": 1200,
        "cache_creation_input_tokens": 5000,
        "cache_read_input_tokens": 25000,
        "output_tokens": 200,
    }})
    m = b.extract_cache_metrics("anthropic", resp)
    assert m == {
        "cache_write_tokens": 5000,
        "cache_read_tokens":  25000,
        "prompt_total_tokens": 31200,  # input + write + read
        "output_tokens": 200,
    }


def test_extract_openai_cache_metrics():
    resp = _ns({"usage": {
        "prompt_tokens": 30000,
        "prompt_tokens_details": {"cached_tokens": 10000},
        "completion_tokens": 100,
    }})
    m = b.extract_cache_metrics("openai", resp)
    assert m == {
        "cache_write_tokens": 0,
        "cache_read_tokens":  10000,
        "prompt_total_tokens": 30000,
        "output_tokens": 100,
    }


def test_extract_xai_uses_openai_shape():
    resp = _ns({"usage": {
        "prompt_tokens": 20000,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens": 50,
    }})
    m = b.extract_cache_metrics("xai", resp)
    assert m["cache_read_tokens"] == 0
    assert m["prompt_total_tokens"] == 20000


def test_extract_xai_missing_details_fields_returns_zero():
    """If xAI response omits prompt_tokens_details, report read=0 (not error)."""
    resp = _ns({"usage": {
        "prompt_tokens": 20000,
        "completion_tokens": 50,
    }})
    m = b.extract_cache_metrics("xai", resp)
    assert m["cache_read_tokens"] == 0


def test_extract_gemini_cache_metrics():
    resp = _ns({
        "usage_metadata": {
            "prompt_token_count": 15000,
            "cached_content_token_count": 12000,
            "candidates_token_count": 80,
        }
    })
    m = b.extract_cache_metrics("google", resp)
    assert m == {
        "cache_write_tokens": 0,
        "cache_read_tokens": 12000,
        "prompt_total_tokens": 15000,
        "output_tokens": 80,
    }


def test_extract_groq_always_zero_cache():
    resp = _ns({"usage": {
        "prompt_tokens": 5000,
        "completion_tokens": 40,
    }})
    m = b.extract_cache_metrics("groq", resp)
    assert m["cache_write_tokens"] == 0
    assert m["cache_read_tokens"] == 0
    assert m["prompt_total_tokens"] == 5000
    assert m["output_tokens"] == 40


def test_make_bust_prefix_unique_per_call():
    p1 = b.make_bust_prefix()
    p2 = b.make_bust_prefix()
    assert p1 != p2
    assert p1.startswith("# Session: ")
    assert "\n# Timestamp: " in p1
    assert p1.endswith("\n\n")


def test_make_bust_prefix_token_size_small():
    """Prefix should be tiny — sub-30 tokens so context_size accuracy stays high."""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    p = b.make_bust_prefix()
    assert len(enc.encode(p)) < 30


def test_write_csv_all_fields(tmp_path):
    spec = _get_spec("anthropic", "claude-sonnet-4-6")
    r = b.CallResult(
        timestamp="2026-04-14T15:30:00+00:00",
        model="claude-sonnet-4-6",
        model_is_fallback=False,
        provider="anthropic",
        nominal_tokens_cl100k=30000,
        actual_input_tokens_api=31200,
        task="recall",
        cache_state="warm",
        run_idx=1,
        ttft_ms=452.3,
        total_ms=1240.0,
        output_tokens=85,
        tokens_per_second=68.5,
        answer="Allen 最喜欢拿铁和 Revolver 的耶加",
        answer_correct=True,
        cache_actually_hit=True,
        cache_write_tokens=5000,
        cache_read_tokens=25000,
        cache_hit_ratio=0.801,
        cost_usd=0.0293,
    )
    path = b.write_csv([r], tmp_path)
    assert path.name == "results.csv"
    content = path.read_text(encoding="utf-8")
    assert "claude-sonnet-4-6" in content
    assert "25000" in content    # cache_read_tokens
    assert "Allen" in content
