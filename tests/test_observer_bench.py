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


def test_observer_candidates_8_models():
    """Exactly 8 candidate Observer models per spec §8."""
    assert len(ob.OBSERVER_CANDIDATES) == 8
    expected = {
        "gemini-2.5-flash",
        "gemini-3-pro-preview",
        "gpt-5-mini",
        "grok-4-1-fast-non-reasoning",
        "grok-4.20-0309-non-reasoning",
        "llama-3.3-70b-versatile",
        "claude-haiku-4-5-20251001",
        "deepseek-chat",
    }
    assert set(ob.OBSERVER_CANDIDATES) == expected


def test_system_prompt_has_key_rules():
    """OBSERVER_SYSTEM_PROMPT must include the 8 critical sections per spec §6.1."""
    p = ob.OBSERVER_SYSTEM_PROMPT
    assert "PRIORITY EMOJI" in p
    assert "🔴" in p and "🟡" in p and "🟢" in p and "✅" in p
    assert "DISTINGUISH USER ASSERTIONS FROM QUESTIONS" in p
    assert "STATE CHANGES" in p
    assert "USER ASSERTIONS ARE AUTHORITATIVE" in p
    assert "PRESERVE UNUSUAL PHRASING" in p
    assert "PRECISE VERBS" in p
    assert "DETAILS IN ASSISTANT" in p
    assert "EMOTION" in p
    assert "中文" in p  # bilingual requirement


def test_tool_def_schema_shape():
    """OBSERVER_TOOL_DEF schema matches spec §6.2."""
    td = ob.OBSERVER_TOOL_DEF
    assert td["name"] == "record_observations"
    params = td["parameters"]
    assert params["type"] == "object"
    obs = params["properties"]["observations"]
    assert obs["type"] == "array"
    item = obs["items"]
    assert set(item["required"]) == {"priority", "time", "text"}
    assert set(item["properties"]["priority"]["enum"]) == {"🔴", "🟡", "🟢", "✅"}
    assert item["properties"]["time"]["pattern"] == r"^[0-2][0-9]:[0-5][0-9]$"
