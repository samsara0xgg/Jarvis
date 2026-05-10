"""Tests for structured tool result helpers."""

from core.tool_result import (
    FAILURE,
    LEGACY_UNVERIFIED,
    SCHEMA_VERSION,
    SUCCESS,
    make_tool_result,
    normalize_tool_result,
    parse_tool_result,
    tool_message,
    tool_succeeded,
)


def test_make_and_parse_success_result() -> None:
    raw = make_tool_result(SUCCESS, "done", data={"id": "x"})
    parsed = parse_tool_result(raw)
    assert parsed["status"] == SUCCESS
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["message"] == "done"
    assert parsed["data"] == {"id": "x"}
    assert tool_succeeded(raw) is True
    assert tool_message(raw) == "done"


def test_parse_legacy_failure_text() -> None:
    raw = "Device not found: desk_lamp"
    parsed = parse_tool_result(raw)
    assert parsed["status"] == FAILURE
    assert parsed["message"] == raw
    assert tool_succeeded(raw) is False


def test_normalize_structured_result_adds_evidence_fields() -> None:
    raw = make_tool_result(SUCCESS, "done")
    out = normalize_tool_result(
        raw,
        skill_name="smart_home_control",
        caller="regex_router",
        read_only=False,
        destructive=True,
    )
    parsed = parse_tool_result(out)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["skill_name"] == "smart_home_control"
    assert parsed["caller"] == "regex_router"
    assert parsed["call_id"].startswith("call_")
    assert parsed["outcome"] == {
        "type": "changed",
        "verified": True,
        "verification_source": "controller_ack",
    }
    assert parsed["claim_policy"]["allowed_claims"] == ["changed"]
    assert tool_succeeded(out) is True


def test_normalize_medium_side_effect_plain_text_is_unverified() -> None:
    out = normalize_tool_result(
        "已输入",
        skill_name="type_to_focused",
        caller="llm",
        read_only=False,
        destructive=True,
        risk_level="medium",
        action_type="macos_paste",
    )
    parsed = parse_tool_result(out)
    assert parsed["status"] == SUCCESS
    assert parsed["message"] == "工具返回了旧格式结果，但没有可验证完成证据。"
    assert parsed["data"]["legacy_raw_result"] == "已输入"
    assert parsed["outcome"]["type"] == LEGACY_UNVERIFIED
    assert parsed["outcome"]["verified"] is False
    assert "typed" in parsed["claim_policy"]["forbidden_claims"]
    assert tool_succeeded(out) is False


def test_normalize_high_risk_plain_text_fails_contract() -> None:
    out = normalize_tool_result(
        "已输入",
        skill_name="type_to_focused",
        caller="llm",
        read_only=False,
        destructive=True,
        risk_level="high",
        action_type="macos_paste",
    )
    parsed = parse_tool_result(out)
    assert parsed["status"] == FAILURE
    assert parsed["error_code"] == "untyped_side_effect_result"
    assert parsed["outcome"]["type"] == "failed"
    assert parsed["data"]["legacy_raw_result"] == "已输入"


def test_normalize_known_legacy_ack_preserves_delivery_message() -> None:
    out = normalize_tool_result(
        "已触发 cc /status",
        skill_name="cc_slash",
        caller="frontend_direct",
        read_only=False,
        destructive=True,
        action_type="zellij_send",
    )
    parsed = parse_tool_result(out)
    assert parsed["status"] == SUCCESS
    assert parsed["message"] == "已触发 cc /status"
    assert parsed["outcome"] == {
        "type": "delivered",
        "verified": True,
        "verification_source": "delivery_ack",
    }
    assert parsed["claim_policy"]["forbidden_claims"] == [
        "task_completed",
        "code_modified",
        "bug_fixed",
    ]
