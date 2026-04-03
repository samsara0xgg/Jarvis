"""Tests for skills.health_skill.HealthSkill."""

from core.health import ComponentTracker
from skills.health_skill import HealthSkill, _friendly_name


def _make_skill(failure_threshold: int = 2) -> tuple[HealthSkill, ComponentTracker]:
    tracker = ComponentTracker({
        "health": {"circuit_breaker": {"failure_threshold": failure_threshold}},
    })
    return HealthSkill(tracker), tracker


class TestHealthSkill:
    def test_skill_name(self):
        skill, _ = _make_skill()
        assert skill.skill_name == "health"

    def test_tool_definitions(self):
        skill, _ = _make_skill()
        tools = skill.get_tool_definitions()
        assert len(tools) == 1
        assert tools[0]["name"] == "get_system_health"

    def test_all_healthy(self):
        skill, tracker = _make_skill()
        tracker.record_success("tts.openai")
        tracker.record_success("intent.groq")
        result = skill.execute("get_system_health", {})
        assert "正常" in result

    def test_with_degraded(self):
        skill, tracker = _make_skill()
        tracker.record_success("intent.groq")
        tracker.record_failure("tts.openai", "timeout")
        tracker.record_failure("tts.openai", "timeout")
        result = skill.execute("get_system_health", {})
        assert "降级" in result
        assert "timeout" in result
        assert "正常" in result

    def test_specific_component_healthy(self):
        skill, tracker = _make_skill()
        tracker.record_success("tts.openai")
        result = skill.execute("get_system_health", {"component": "tts.openai"})
        assert "正常" in result

    def test_specific_component_degraded(self):
        skill, tracker = _make_skill()
        tracker.record_failure("tts.openai", "API error")
        tracker.record_failure("tts.openai", "API error")
        result = skill.execute("get_system_health", {"component": "tts.openai"})
        assert "降级" in result
        assert "2" in result

    def test_unknown_tool(self):
        skill, _ = _make_skill()
        result = skill.execute("bogus_tool", {})
        assert "Unknown" in result


class TestFriendlyName:
    def test_tts_openai(self):
        assert _friendly_name("tts.openai") == "语音合成(OPENAI)"

    def test_intent_groq(self):
        assert _friendly_name("intent.groq") == "意图路由(GROQ)"

    def test_unknown_prefix(self):
        assert _friendly_name("foo.bar") == "foo(BAR)"

    def test_no_engine(self):
        assert _friendly_name("tts") == "语音合成"
