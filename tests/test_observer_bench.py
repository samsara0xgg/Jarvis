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


def test_api_key_env_obs_covers_all_providers():
    """All providers in OBSERVER_CANDIDATES have API key env vars."""
    provider_envs = ob.API_KEY_ENV_OBS
    assert set(provider_envs.keys()) >= {"anthropic", "openai", "google", "groq", "xai", "deepseek"}
    assert provider_envs["deepseek"] == "DEEPSEEK_API_KEY"


def test_openai_compat_base_urls():
    """All OpenAI-compat providers have base URLs registered."""
    urls = ob.OPENAI_COMPAT_BASE_URLS
    assert urls["openai"] == "https://api.openai.com/v1"
    assert urls["xai"] == "https://api.x.ai/v1"
    assert urls["groq"] == "https://api.groq.com/openai/v1"
    assert urls["deepseek"] == "https://api.deepseek.com/v1"


def test_is_rate_limit_obs():
    assert ob._is_rate_limit_obs(Exception("429 Too Many Requests")) is True
    assert ob._is_rate_limit_obs(Exception("Rate limit exceeded")) is True
    assert ob._is_rate_limit_obs(Exception("quota exceeded")) is True
    assert ob._is_rate_limit_obs(Exception("server overloaded")) is True
    assert ob._is_rate_limit_obs(Exception("401 unauthorized")) is False
    assert ob._is_rate_limit_obs(Exception("500 internal")) is False


def test_is_fatal_obs():
    assert ob._is_fatal_obs(Exception("401 unauthorized")) is True
    assert ob._is_fatal_obs(Exception("403 Forbidden")) is True
    assert ob._is_fatal_obs(Exception("invalid api key")) is True
    assert ob._is_fatal_obs(Exception("400 Bad Request")) is True
    assert ob._is_fatal_obs(Exception("429 rate limit")) is False
    assert ob._is_fatal_obs(Exception("500 error")) is False


def _make_fx_one_expected():
    """Helper: fixture with 2 expected observations + 2 hallucination words."""
    return ob.Fixture(
        id="fx_test", category="smart_home", seed_id="fx_test",
        generated_by="test",
        dialogue=[],
        expected_observations=[
            ob.ExpectedObservation(
                priority="🔴",
                must_contain_any_of=[["拿铁", "客厅"], ["暖黄"]],  # OR of AND
                semantic_description="x",
            ),
            ob.ExpectedObservation(
                priority="🟡",
                must_contain_any_of=[["累"], ["疲惫"]],
                semantic_description="y",
            ),
        ],
        must_not_contain_globally=["蓝光", "卧室"],
    )


def test_evaluate_tool_success_false_when_none():
    """model_obs=None → all scores 0, halluc False (spec §7.1 guard)."""
    fx = _make_fx_one_expected()
    s = ob.evaluate(None, fx)
    assert s.tool_success is False
    assert s.precision == 0.0
    assert s.recall == 0.0
    assert s.f1 == 0.0
    assert s.priority_accuracy == 0.0
    assert s.hallucination is False
    assert s.extra_count == 0


def test_evaluate_perfect_match():
    """Both expected observations matched by correct keywords + priority."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户偏好客厅灯暖黄色"},
        {"priority": "🟡", "time": "14:28", "text": "用户表达疲惫"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.tool_success is True
    assert s.precision == 1.0
    assert s.recall == 1.0
    assert s.f1 == 1.0
    assert s.priority_accuracy == 1.0
    assert s.hallucination is False
    assert s.extra_count == 0


def test_evaluate_keyword_or_semantics():
    """must_contain_any_of OR: matching any one sub-list is enough."""
    fx = _make_fx_one_expected()
    # First expected: matches "暖黄" only (second sub-list)
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "暖黄色灯光设置"},
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0


def test_evaluate_hallucination_triggered():
    """must_not_contain_globally word in any obs → halluc=True."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户在卧室想要暖黄"},  # "卧室" triggers
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.hallucination is True


def test_evaluate_partial_recall_no_halluc():
    """Only 1 of 2 matched, no halluc words."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "用户偏好客厅拿铁"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 0.5
    assert s.precision == 1.0
    assert abs(s.f1 - 2/3) < 0.01
    assert s.hallucination is False


def test_evaluate_priority_wrong_still_counts_recall():
    """Model hit keywords but wrong priority → recall OK, priority_acc reduced."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🟡", "time": "14:28", "text": "客厅暖黄"},  # should be 🔴
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0
    assert s.priority_accuracy == 0.5  # 1 of 2 priorities matched


def test_evaluate_extra_observations():
    """Extra observations boost total but do not block recall."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "🔴", "time": "14:28", "text": "客厅暖黄"},
        {"priority": "🟡", "time": "14:28", "text": "用户累"},
        {"priority": "🟢", "time": "14:28", "text": "用户住在温哥华"},  # extra
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.recall == 1.0
    assert abs(s.precision - 2/3) < 0.01
    assert s.extra_count == 1


def test_evaluate_tool_success_invalid_priority():
    """Invalid priority emoji → tool_success=False."""
    fx = _make_fx_one_expected()
    model_obs = [
        {"priority": "❓", "time": "14:28", "text": "bogus priority"},
    ]
    s = ob.evaluate(model_obs, fx)
    assert s.tool_success is False


def test_write_observer_csv(tmp_path):
    r = ob.ObserverResult(
        timestamp="2026-04-15T14:30:00+00:00",
        model="gemini-2.5-flash", model_is_fallback=False,
        provider="google", fixture_id="fx_001",
        fixture_category="smart_home",
        tool_success=True,
        precision=0.9, recall=0.8, f1=0.85,
        priority_accuracy=0.75,
        hallucination=False, extra_count=0,
        expected_count=3, matched_count=2,
        observer_latency_ms=1200.0,
        actual_input_tokens_api=500, output_tokens=80,
        cost_usd=0.0005,
        model_output_raw='{"observations":[...]}',
    )
    path = ob.write_observer_csv([r], tmp_path)
    assert path.name == "results.csv"
    content = path.read_text(encoding="utf-8")
    assert "gemini-2.5-flash" in content
    assert "fx_001" in content
    assert "smart_home" in content
    assert "0.85" in content   # f1


def test_compute_pilot_pass_rules():
    """Spec §9.4: pass iff tool_success_rate >= 0.80 AND mean_f1 >= 0.30."""
    scores_pass = [
        ob.Scores(tool_success=True, precision=0.5, recall=0.5, f1=0.5,
                  priority_accuracy=0.5, hallucination=False, extra_count=0)
        for _ in range(5)
    ]
    assert ob.compute_pilot_pass(scores_pass) is True

    # 60% tool success → fail
    scores_low_tool = scores_pass[:3] + [
        ob.Scores(tool_success=False, precision=0.0, recall=0.0, f1=0.0,
                  priority_accuracy=0.0, hallucination=False, extra_count=0)
        for _ in range(2)
    ]
    assert ob.compute_pilot_pass(scores_low_tool) is False

    # F1 avg 0.2 → fail
    scores_low_f1 = [
        ob.Scores(tool_success=True, precision=0.2, recall=0.2, f1=0.2,
                  priority_accuracy=0.5, hallucination=False, extra_count=0)
        for _ in range(5)
    ]
    assert ob.compute_pilot_pass(scores_low_f1) is False
