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


def test_seed_dataclass_fields():
    seed = ob.Seed(
        id="fx_001",
        category="smart_home",
        scene="test",
        user_emotion_hint="tired",
        tone_hint="casual",
        dialogue_length_hint="3-4 turns",
        must_capture=["a", "b"],
        must_not_hallucinate=["x"],
    )
    assert seed.id == "fx_001"
    assert seed.category == "smart_home"


def test_expected_observation_dataclass():
    exp = ob.ExpectedObservation(
        priority="🔴",
        must_contain_any_of=[["拿铁", "客厅"], ["偏好"]],
        semantic_description="用户偏好客厅灯暖黄",
    )
    assert exp.priority == "🔴"
    assert len(exp.must_contain_any_of) == 2


def test_fixture_dataclass():
    fx = ob.Fixture(
        id="fx_001",
        category="smart_home",
        seed_id="fx_001",
        generated_by="claude-opus-4-6",
        dialogue=[{"role": "user", "time": "14:28", "content": "hi"}],
        expected_observations=[
            ob.ExpectedObservation("🔴", [["hi"]], "greeting")
        ],
        must_not_contain_globally=["bad"],
    )
    assert fx.id == "fx_001"
    assert len(fx.dialogue) == 1
    assert len(fx.expected_observations) == 1


def test_scores_dataclass_defaults():
    s = ob.Scores(
        tool_success=False,
        precision=0.0, recall=0.0, f1=0.0,
        priority_accuracy=0.0, hallucination=False, extra_count=0,
    )
    assert s.tool_success is False
    assert s.f1 == 0.0


import tempfile
import json as _json
from pathlib import Path as _Path


def test_load_seeds_parses_yaml(tmp_path):
    seeds_yaml = tmp_path / "seeds.yaml"
    seeds_yaml.write_text("""
- id: fx_001
  category: smart_home
  scene: "智能家居 + 疲惫语气"
  user_emotion_hint: tired
  tone_hint: "口语化"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "偏好: 暖黄"
  must_not_hallucinate:
    - "蓝光"
""", encoding="utf-8")
    seeds = ob.load_seeds(seeds_yaml)
    assert len(seeds) == 1
    assert seeds[0].id == "fx_001"
    assert seeds[0].category == "smart_home"
    assert seeds[0].must_capture == ["偏好: 暖黄"]


def test_load_seeds_rejects_unknown_category(tmp_path):
    seeds_yaml = tmp_path / "seeds.yaml"
    seeds_yaml.write_text("""
- id: fx_001
  category: BOGUS
  scene: x
  user_emotion_hint: x
  tone_hint: x
  dialogue_length_hint: x
  must_capture: []
  must_not_hallucinate: []
""", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="unknown category"):
        ob.load_seeds(seeds_yaml)


def test_load_approved_fixtures_ignores_draft(tmp_path):
    """.draft.json files must be skipped (Allen hasn't approved them)."""
    approved = tmp_path / "fx_001.json"
    draft = tmp_path / "fx_002.draft.json"
    approved.write_text(_json.dumps({
        "id": "fx_001", "category": "smart_home", "seed_id": "fx_001",
        "generated_by": "claude-opus-4-6",
        "dialogue": [{"role": "user", "time": "14:28", "content": "hi"}],
        "expected_observations": [
            {"priority": "🔴", "must_contain_any_of": [["hi"]], "semantic_description": "x"}
        ],
        "must_not_contain_globally": [],
    }), encoding="utf-8")
    draft.write_text('{"id": "fx_002"}', encoding="utf-8")
    fxs = ob.load_approved_fixtures(tmp_path)
    assert len(fxs) == 1
    assert fxs[0].id == "fx_001"


def test_save_draft_fixture_writes_draft_suffix(tmp_path):
    fx = ob.Fixture(
        id="fx_003", category="preference", seed_id="fx_003",
        generated_by="claude-opus-4-6",
        dialogue=[], expected_observations=[], must_not_contain_globally=[],
    )
    path = ob.save_draft_fixture(fx, tmp_path)
    assert path.name == "fx_003.draft.json"
    assert path.exists()
    data = _json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == "fx_003"


def test_build_observer_prompt_renders_all_roles():
    fx = ob.Fixture(
        id="fx_001", category="smart_home", seed_id="fx_001",
        generated_by="claude-opus-4-6",
        dialogue=[
            {"role": "user", "time": "14:28", "emotion": "tired", "content": "调灯"},
            {"role": "assistant", "time": "14:28", "content": "好的"},
            {"role": "tool", "name": "hue.set_color",
             "args": {"room": "living"}, "result": "ok"},
        ],
        expected_observations=[], must_not_contain_globally=[],
    )
    system, user_msg = ob.build_observer_prompt(fx)
    assert "record_observations" in system   # part of OBSERVER_SYSTEM_PROMPT
    assert "USER (14:28)" in user_msg
    assert "[情绪: tired]" in user_msg
    assert "ASSISTANT (14:28): 好的" in user_msg
    assert "TOOL_CALL hue.set_color" in user_msg
    assert "room" in user_msg and "living" in user_msg


def test_build_tool_call_kwargs_anthropic():
    kw = ob.build_tool_call_kwargs("anthropic")
    assert kw["tool_choice"] == {"type": "tool", "name": "record_observations"}
    assert kw["tools"][0]["name"] == "record_observations"
    assert "input_schema" in kw["tools"][0]


def test_build_tool_call_kwargs_openai_compat():
    for provider in ("openai", "xai", "groq", "deepseek"):
        kw = ob.build_tool_call_kwargs(provider)
        assert kw["tool_choice"]["type"] == "function"
        assert kw["tool_choice"]["function"]["name"] == "record_observations"
        assert kw["tools"][0]["type"] == "function"


def test_build_tool_call_kwargs_gemini():
    kw = ob.build_tool_call_kwargs("google")
    assert "function_declarations" in kw["tools"][0]
    assert kw["tool_config"]["function_calling_config"]["mode"] == "ANY"
    assert kw["tool_config"]["function_calling_config"]["allowed_function_names"] == [
        "record_observations"
    ]


def test_openai_token_param_gpt5_uses_max_completion_tokens():
    assert ob._openai_token_param_for_model("openai", "gpt-5") == "max_completion_tokens"
    assert ob._openai_token_param_for_model("openai", "gpt-5-mini") == "max_completion_tokens"


def test_openai_token_param_o1_o3_uses_max_completion_tokens():
    assert ob._openai_token_param_for_model("openai", "o1-preview") == "max_completion_tokens"
    assert ob._openai_token_param_for_model("openai", "o3-mini") == "max_completion_tokens"


def test_openai_token_param_gpt4_uses_max_tokens():
    assert ob._openai_token_param_for_model("openai", "gpt-4o") == "max_tokens"
    assert ob._openai_token_param_for_model("openai", "gpt-4o-mini") == "max_tokens"


def test_openai_token_param_other_providers_use_max_tokens():
    """xAI / Groq / DeepSeek use max_tokens even for GPT-5-like names."""
    assert ob._openai_token_param_for_model("xai", "grok-4") == "max_tokens"
    assert ob._openai_token_param_for_model("groq", "llama-3.3-70b-versatile") == "max_tokens"
    assert ob._openai_token_param_for_model("deepseek", "deepseek-chat") == "max_tokens"
